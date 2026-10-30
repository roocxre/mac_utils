"""Carrying out queued requests: emails through the mail layer's Gmail client, web forms through forms.py.

Emails go from your Gmail address on a schedule of one every
send_gap_minutes between send_hours, at most sends_per_day: spread over the days, so it never
looks like a spam burst and stays far below Gmail's sending limits. The Mac sleeps most of the
time and the agent only runs when it wakes, so a pass catches up on the slots it slept through:
up to catch_up_per_pass emails, CATCH_UP_PAUSE seconds apart. Each sent message's
thread is registered with the mail poller (watch_thread), so replies come back here. A web form is
up to three per pass, in the agent's own browser.

Before anything goes out, the approval is checked again (plan.covers) and the details are taken
fresh from the vault, limited to the approved set. What went out is written to the ledger in the
same transaction that marks the request sent.
"""
import json
import time
from datetime import datetime

import requests as _requests

from brokers import AGENT, forms, plan, registry, scan, templates, vault, watch

MAX_ATTEMPTS = 3
RETRY_AFTER = 3600          # a request that failed for a passing reason waits this long


def queue(conn, broker_id, kind, auto=False, detail=""):
    """Queue a request built from the broker's approval. kind: deletion, follow-up, resubmit."""
    row = conn.execute("SELECT approved FROM brokers WHERE id = ?", (broker_id,)).fetchone()
    approved = json.loads(row["approved"])
    if conn.execute("SELECT 1 FROM requests WHERE broker_id = ? AND status = 'queued' AND kind = ?",
                    (broker_id, kind)).fetchone():
        return
    previous = conn.execute("SELECT * FROM requests WHERE broker_id = ? AND kind IN ('deletion', 'resubmit') "
                            "AND sent_at IS NOT NULL ORDER BY id DESC LIMIT 1", (broker_id,)).fetchone()
    conn.execute("INSERT INTO requests (broker_id, kind, method, elements, status, auto, queued_at, to_address, thread_id, "
                 "message_id_header, detail) VALUES (?, ?, ?, ?, 'queued', ?, ?, ?, ?, ?, ?)",
                 (broker_id, kind, approved["method"], json.dumps(approved["elements"]), int(auto), time.time(),
                  approved.get("to") if approved["method"] == "email" else approved.get("form"), previous["thread_id"] if previous and kind == "follow-up" else None,
                  previous["message_id_header"] if previous and kind == "follow-up" else None, detail or None))
    vault.history(conn, broker_id, f"queued: {kind}", "automatically" if auto else "you approved it")


def _real_email():
    """Your Gmail address: requests are emailed from it, and a form's email field gets it too."""
    from mail import store as mail_store
    return mail_store.get("address") or ""


def values_for(conn, broker_id, elements):
    """({element: value} for exactly these elements, [missing])."""
    found = vault.identity()
    every = vault.element_values(found, _real_email(), scan.listing_urls(conn, broker_id))
    values = {e: every[e] for e in elements if e in every}
    return values, [e for e in elements if e not in every]


def _today_sent(conn):
    midnight = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
    return conn.execute("SELECT COUNT(*) FROM requests WHERE method = 'email' AND sent_at >= ?", (midnight,)).fetchone()[0]


CATCH_UP_PAUSE = 20         # seconds between the emails of one pass


def email_due(conn, cfg, this_pass=0):
    """True if an email may go now: inside send_hours, behind the schedule of one per send_gap_minutes
    since they opened today, under sends_per_day, and under catch_up_per_pass for this pass."""
    now = datetime.now()
    start, end = cfg["send_hours"]
    if not start <= now.hour < end or this_pass >= cfg.get("catch_up_per_pass", 8):
        return False
    opened = now.replace(hour=start, minute=0, second=0, microsecond=0)
    slots = int((now - opened).total_seconds() // (cfg["send_gap_minutes"] * 60)) + 1
    return _today_sent(conn) < min(cfg["sends_per_day"], slots)


def _fail(conn, request, detail, task=None, entry=None):
    attempts = (json.loads(request["detail"]) if (request["detail"] or "").startswith("{") else {}).get("attempts", 0) + 1
    if task or attempts >= MAX_ATTEMPTS:
        conn.execute("UPDATE requests SET status = 'failed', detail = ? WHERE id = ?", (detail, request["id"]))
        plan.set_state(conn, request["broker_id"], "needs_manual", detail)
        kind, title, steps = task or ("failed", "The request couldn't be sent", [detail])
        scan.open_task(conn, request["broker_id"], kind, title, steps, (entry or {}).get("opt_out_url"))
    else:
        conn.execute("UPDATE requests SET detail = ?, queued_at = ? WHERE id = ?",
                     (json.dumps({"attempts": attempts, "last": detail}), time.time() + RETRY_AFTER, request["id"]))


def _body(kind, values, cfg, entry, request, approved, conn):
    if kind == "more-info":
        return "Re: Request to delete my personal information", templates.more_info(values)
    states = plan.home_states(cfg)          # a law is cited only when it is the one where you live
    if kind == "follow-up":
        first = conn.execute("SELECT * FROM requests WHERE broker_id = ? AND kind IN ('deletion', 'resubmit') AND "
                             "sent_at IS NOT NULL ORDER BY id DESC LIMIT 1", (request["broker_id"],)).fetchone()
        original = templates.deletion(values, cfg, registry.get(entry, "category"), approved.get("access"), states)[1]
        sent_on = time.strftime("%B %-d, %Y", time.localtime(first["sent_at"])) if first else "an earlier date"
        return templates.follow_up(values, sent_on, original)
    return templates.deletion(values, cfg, registry.get(entry, "category"), approved.get("access"), states)


def _sent_before(api, request, to, subject):
    """On a retry: the message an earlier attempt sent although Gmail's answer never came back (a read
    timeout after the send went through), so it isn't sent twice. {"id", "threadId"} or None."""
    tried = (json.loads(request["detail"]) if (request["detail"] or "").startswith("{") else {}).get("attempts", 0)
    if not tried:
        return None
    since = int(request["queued_at"] - RETRY_AFTER * tried - 86400)
    query = f'from:me to:{to} subject:"{subject}" after:{since}'
    found = api.list_messages(query, include_spam_trash=True, limit=5)
    return found[-1] if found else None


def send_email(api, conn, request, entry, approved, cfg):
    elements = json.loads(request["elements"])
    if request["kind"] == "more-info":
        elements = elements + [e for e in ("full_name",) if e in approved["elements"] and e not in elements]
    values, missing = values_for(conn, request["broker_id"], elements)
    if missing:
        _fail(conn, request, f"your details have no {', '.join(missing)}",
              ("details", "Add the missing details", [f"The request needs your {', '.join(missing)}. Add it in "
                                                      "Your details, then approve again."]), entry)
        return False
    subject, body = _body(request["kind"], values, cfg, entry, request, approved, conn)
    from mail import gmail
    to = request["to_address"] or entry["privacy_email"]
    sent = _sent_before(api, request, to, subject)
    if not sent:
        message = gmail.compose(to, subject, body, in_reply_to=request["message_id_header"])
        sent = api.send(message, thread_id=request["thread_id"])
    header = ""
    try:
        meta = api.message(sent["id"], "metadata", headers=["Message-ID"])
        header = next((h["value"] for h in meta.get("payload", {}).get("headers", []) if h["name"].lower() == "message-id"),
                      "")
    except gmail.GmailError:
        pass
    conn.execute("UPDATE requests SET status = 'sent', sent_at = ?, gmail_id = ?, thread_id = ?, message_id_header = ?, "
                 "subject = ? WHERE id = ?", (time.time(), sent["id"], sent.get("threadId"), header or None, subject,
                                              request["id"]))
    vault.record_disclosure(conn, request["id"], request["broker_id"], values, "email")
    try:
        from brokers import replies
        replies.file_away(api, sent["id"], [], reply=False)
    except gmail.GmailError:
        pass                                    # only a label: the next request tries again
    from mail import poller
    poller.watch_thread(sent.get("threadId"), AGENT)
    return True


# A form may ask for part of an approved detail in its own box: your first name is part of the full
# name you approved, the city part of city and state. Nothing beyond the approved detail.
PARTS = {"full_name": ("first_name", "last_name"), "city_state": ("city", "state"),
         "address": ("street", "city", "state", "zip")}


def send_form(conn, request, entry, approved, how):
    elements = json.loads(request["elements"])
    values, missing = values_for(conn, request["broker_id"], elements)
    if missing:
        _fail(conn, request, f"your details have no {', '.join(missing)}",
              ("details", "Add the missing details", [f"The form needs your {', '.join(missing)}. Add it in Your "
                                                      "details, then approve again."]), entry)
        return None
    every, _ = values_for(conn, request["broker_id"], list(registry.ELEMENTS))
    every["street"] = (vault.current_address(vault.identity()) or {}).get("street", "")
    for whole, parts in PARTS.items():
        if whole in values:
            values.update({part: every[part] for part in parts if every.get(part) and part not in values})
    return values


def email_instead(conn, request, entry, why):
    """Ask by email what the form couldn't take, with the same approved details. False if the
    broker has no privacy address (or one already bounced), or told us to use its form."""
    broker_id = request["broker_id"]
    row = conn.execute("SELECT broken, approved FROM brokers WHERE id = ?", (broker_id,)).fetchone()
    if not entry.get("privacy_email") or (row and (row["broken"] or "").startswith("email bounced")):
        return False
    if row and json.loads(row["approved"] or "{}").get("form_only"):
        return False        # their mailbox said to use the form: emailing it again would go round in a loop
    if conn.execute("SELECT 1 FROM requests WHERE broker_id = ? AND method = 'email' AND status IN ('queued', 'sent', "
                    "'awaiting')", (broker_id,)).fetchone():
        return True
    elements = [e for e in json.loads(request["elements"]) if e != "listing_url" or scan.listing_urls(conn, broker_id)]
    conn.execute("INSERT INTO requests (broker_id, kind, method, elements, status, auto, queued_at, to_address, detail) "
                 "VALUES (?, ?, 'email', ?, 'queued', 1, ?, ?, ?)",
                 (broker_id, request["kind"] if request["kind"] in ("deletion", "resubmit") else "deletion",
                  json.dumps(elements), time.time(), entry["privacy_email"], f"by email instead: {why}"))
    plan.set_state(conn, broker_id, "approved", f"{why}; asking by email instead")
    return True


def _after_form(conn, request, entry, result, kind):
    broker_id = request["broker_id"]
    if result.sent:
        vault.record_disclosure(conn, request["id"], broker_id, result.sent, "form")
    if result.ok:
        status = "awaiting" if result.outcome == "awaiting_confirmation" else "sent"
        conn.execute("UPDATE requests SET status = ?, sent_at = ?, detail = ? WHERE id = ?",
                     (status, time.time(), result.detail, request["id"]))
        state = "awaiting_confirmation" if status == "awaiting" else ("resubmitted" if kind == "resubmit" else "submitted")
        plan.set_state(conn, broker_id, state, result.detail)
        return
    if result.outcome == "broken":
        conn.execute("UPDATE brokers SET broken = ? WHERE id = ?", (result.detail, broker_id))
        conn.execute("UPDATE requests SET status = 'failed', detail = ? WHERE id = ?", (result.detail, request["id"]))
        plan.set_state(conn, broker_id, "broken", result.detail)
        email_instead(conn, request, entry, "their form has changed")
        return
    if result.outcome == "manual":
        steps = {"captcha": "The page has a CAPTCHA. Open it, fill in only the details listed here, and solve it yourself.",
                 "id": "It asks for an ID upload. The agent never uploads ID; if you choose to, redact everything except "
                       "your name and photo.",
                 "payment": "It asks for payment. Removal should be free: don't pay; look for their free opt-out, or "
                            "leave it.",
                 "account": "It wants an account or a sign-in. The agent never makes one.",
                 "residency": "It asks you to declare residency somewhere. Only answer truthfully: where you "
                              "actually live.",
                 "field": "The form wants a detail you didn't approve.",
                 "no-form": "No usable form on the page.",
                 "blocked": "The site blocked the agent's browser. Open the link in your own browser and do it "
                            "there."}.get(result.manual, result.detail)
        conn.execute("UPDATE requests SET status = 'failed', detail = ? WHERE id = ?", (result.detail, request["id"]))
        if result.manual != "payment" and email_instead(conn, request, entry, result.detail):
            return
        if result.manual == "field" and result.needs:
            # Their form needs more of your details: yours to approve in the dashboard, then it goes again.
            why = f"their form also needs your {', '.join(result.needs)}"
            plan.set_pending(conn, broker_id, {"kind": "resubmit", "asked": result.needs, "grows": True, "why": why},
                             why)
            plan.set_state(conn, broker_id, "planned", why + ": approve it to send")
            return
        plan.set_state(conn, broker_id, "needs_manual", result.detail)
        scan.open_task(conn, broker_id, result.manual or "form", f"Finish the opt-out at {entry['name']}",
                       [result.detail, steps, "Details approved for it: "
                        + ", ".join(json.loads(request["elements"]))], result.url or entry.get("opt_out_url"))
        return
    _fail(conn, request, result.detail, entry=entry)


def run(log, api=None, cfg=None, forms_per_pass=3):
    """Send what is due. Returns {"emails": n, "forms": n, "failed": n}."""
    cfg = cfg or plan.config()
    done = {"emails": 0, "forms": 0, "failed": 0, "waiting": 0}
    with vault.db() as conn:
        # Answers to a broker go before new requests: hundreds of deletions can be queued at once, and a
        # broker that asked a question shouldn't wait behind all of them.
        queued = [dict(r) for r in conn.execute("SELECT * FROM requests WHERE status = 'queued' AND queued_at <= ? "
                                                "ORDER BY CASE kind WHEN 'more-info' THEN 0 WHEN 'resubmit' THEN 1 "
                                                "WHEN 'follow-up' THEN 2 ELSE 3 END, queued_at, id", (time.time(),))]
    emails_this_pass, email_down = 0, False
    for request in queued:
        entry = registry.broker(request["broker_id"])
        with vault.db() as conn:
            row = dict(conn.execute("SELECT * FROM brokers WHERE id = ?", (request["broker_id"],)).fetchone())
            approved = json.loads(row["approved"]) if row["approved"] else None
            if entry is None or approved is None:
                conn.execute("UPDATE requests SET status = 'cancelled', detail = 'no approval' WHERE id = ?",
                             (request["id"],))
                continue
            elements = json.loads(request["elements"])
            fine, why = plan.covers(approved, request["method"], elements)
            if not fine:
                conn.execute("UPDATE requests SET status = 'cancelled', detail = ? WHERE id = ?", (why, request["id"]))
                plan.set_state(conn, request["broker_id"], "planned", f"needs approval again: {why}")
                continue
            if request["method"] == "email":
                if email_down or not email_due(conn, cfg, emails_this_pass):
                    done["waiting"] += 1
                    continue
                if emails_this_pass:
                    time.sleep(CATCH_UP_PAUSE)
                if api is None:
                    from mail import gmail
                    api = gmail.client()
                watch.say(f"emailing {entry.get('name', request['broker_id'])}: {request['kind']} to "
                          f"{request['to_address'] or entry.get('privacy_email')}", "send", request["broker_id"])
                try:
                    if send_email(api, conn, request, entry, approved, cfg):
                        emails_this_pass += 1
                        done["emails"] += 1
                        state = {"deletion": "submitted", "resubmit": "resubmitted"}.get(request["kind"])
                        if state:
                            plan.set_state(conn, request["broker_id"], state, f"emailed {request['to_address']}")
                            conn.execute("UPDATE brokers SET follow_up_at = ?, followed_up = 0 WHERE id = ?",
                                         (time.time() + cfg["follow_up_days"] * 86400, request["broker_id"]))
                        elif request["kind"] == "follow-up":
                            conn.execute("UPDATE brokers SET followed_up = 1 WHERE id = ?", (request["broker_id"],))
                            vault.history(conn, request["broker_id"], "followed up", request["to_address"] or "")
                    else:
                        done["failed"] += 1
                except Exception as error:          # Gmail refused or is down: try again next pass
                    log.warning("send to %s failed: %s", request["broker_id"], error)
                    offline = isinstance(error, OSError) and not isinstance(error, _requests.ReadTimeout)
                    if not offline:
                        # A read timeout may have sent it: the attempt is counted, so the retry looks in
                        # Sent first (_sent_before).
                        _fail(conn, request, f"{type(error).__name__}: {str(error)[:160]}", entry=entry)
                    done["failed"] += 1
                    if isinstance(error, OSError):
                        email_down = True       # no network (the Mac just woke): the other emails wait, unblamed
                continue
            if done["forms"] >= forms_per_pass:
                done["waiting"] += 1
                continue
            values = send_form(conn, request, entry, approved, request["method"])
        if values is None:
            done["failed"] += 1
            continue
        try:
            target = request["to_address"] or ""
            watch.say(f"{request['kind']} on {entry.get('name', request['broker_id'])}'s form ({request['method']})",
                      "send", request["broker_id"])
            # A form they pointed to (replies._redirected) is where the opt-out starts.
            result = forms.run({**entry, "opt_out_url": target} if target.startswith("http") else entry,
                               request["method"], values)
        except Exception as error:
            result = forms.Result(False, "failed", f"{type(error).__name__}: {str(error)[:160]}")
        log.info("form %s: %s %s", request["broker_id"], result.outcome, result.detail)
        watch.say(f"form result: {result.outcome}: {result.detail}", "done" if result.ok else "warn", request["broker_id"])
        with vault.db() as conn:
            _after_form(conn, request, entry, result, request["kind"])
            if result.ok:
                conn.execute("UPDATE brokers SET follow_up_at = ?, followed_up = 0 WHERE id = ?",
                             (time.time() + cfg["follow_up_days"] * 86400, request["broker_id"]))
        done["forms"] += 1
        if not result.ok:
            done["failed"] += 1
    return done
