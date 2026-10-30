"""The agent's rolling work: one pass every 10 minutes (agents/broker_agent.py). Each pass does
only what is due, so scans and sends spread over the week:

    1. sort new broker mail and act on it (replies.handle)
    2. scan one people-search site that is due (scans_per_day, scan_gap_seconds)
    3. send what is queued (send.run: one email when the pacing allows, one web form)
    4. timers: follow up after follow_up_days, no_response after no_response_days, re-search a
       site verify_after_days after it acknowledged, re-check removed listings every recheck_days;
       relisted in standing mode is re-submitted under the same approval
    5. tell the mail poller which broker domains to route here, and write the browser extension's block list
    6. the weekly report, Sundays from 9 (report.py), with a banner

Nothing at all happens until your details are in the vault (the dashboard's Your details).
"""
import json
import time
from datetime import datetime, timedelta

from brokers import AGENT, plan, registry, replies, report, scan, send, vault, watch
from lib.common import state_path

BLOCKLIST = state_path("broker-blocklist.json")      # the browser extension reads this (dataflow/entities.py)


def _scan_due(conn, cfg, rows, entries):
    """The next broker to search, or None (pacing, the day's limit)."""
    day = time.time() - 86400
    done_today = conn.execute("SELECT COUNT(*) FROM history WHERE event LIKE 'scanned:%' AND at > ?", (day,)).fetchone()[0]
    if done_today >= cfg["scans_per_day"]:
        return None
    last = conn.execute("SELECT MAX(at) FROM history WHERE event LIKE 'scanned:%'").fetchone()[0] or 0
    if time.time() - last < cfg["scan_gap_seconds"]:
        return None
    now = time.time()
    due = []
    for broker_id, row in rows.items():
        entry = entries.get(broker_id)
        if not entry or not scan.searchable(entry) or row.get("broken"):
            continue
        if row["state"] in ("discovered", "scanned") and not row["scanned_at"]:
            due.append((row["tier"], 0, broker_id, "discover"))
        elif row["state"] == "acknowledged" and row["verify_at"] and row["verify_at"] <= now:
            due.append((row["tier"], 1, broker_id, "verify"))
        elif row["state"] == "verified_removed" and row["recheck_at"] and row["recheck_at"] <= now:
            due.append((row["tier"], 2, broker_id, "recheck"))
        elif row["state"] in ("discovered", "scanned") and row["next_scan_at"] and row["next_scan_at"] <= now:
            due.append((row["tier"], 3, broker_id, "rescan"))
    due.sort()
    return due[0][2:] if due else None


def _after_scan(broker_id, why, result, cfg, mode):
    """What a verification or re-check search means for the broker's state."""
    now = time.time()
    with vault.db() as conn:
        row = dict(conn.execute("SELECT * FROM brokers WHERE id = ?", (broker_id,)).fetchone())
        if result["listed"] == "unknown":
            conn.execute("UPDATE brokers SET next_scan_at = ? WHERE id = ?", (now + 3 * 86400, broker_id))
            return
        listed = result["listed"] == "listed"
        if why in ("discover", "rescan"):
            conn.execute("UPDATE brokers SET next_scan_at = ? WHERE id = ?",
                         (now + cfg["recheck_days"] * 86400 if not listed else None, broker_id))
            return
        if why == "verify":
            if listed:
                plan.set_state(conn, broker_id, "still_listed", "still listed after the broker acknowledged")
                _resubmit(conn, broker_id, row, mode, "still listed")
            else:
                plan.set_state(conn, broker_id, "verified_removed", "searched again: no listing")
                conn.execute("UPDATE brokers SET recheck_at = ? WHERE id = ?",
                             (now + registry.get(registry.broker(broker_id), "recheck_days", cfg["recheck_days"]) * 86400,
                              broker_id))
            return
        if why == "recheck":
            if listed:
                plan.set_state(conn, broker_id, "relisted", "listed again")
                _resubmit(conn, broker_id, row, mode, "listed again")
            else:
                conn.execute("UPDATE brokers SET recheck_at = ? WHERE id = ?",
                             (now + registry.get(registry.broker(broker_id), "recheck_days", cfg["recheck_days"]) * 86400,
                              broker_id))
                vault.history(conn, broker_id, "rechecked: still removed")


def _resubmit(conn, broker_id, row, mode, why):
    if row.get("approved") and mode == "standing":
        send.queue(conn, broker_id, "resubmit", auto=True, detail=why)
    else:
        plan.set_pending(conn, broker_id, {"kind": "resubmit", "why": why}, why)


def timers(cfg, mode):
    """Follow-ups and no_response. Returns counts."""
    now = time.time()
    counts = {"follow_ups": 0, "no_response": 0}
    with vault.db() as conn:
        rows = [dict(r) for r in conn.execute("SELECT * FROM brokers WHERE state IN ('submitted', 'resubmitted', "
                                              "'awaiting_confirmation')")]
        for row in rows:
            last_sent = conn.execute("SELECT MAX(sent_at) FROM requests WHERE broker_id = ? AND kind IN ('deletion', "
                                     "'resubmit')", (row["id"],)).fetchone()[0]
            # A receipt ("we got it") isn't an answer: the follow-up and no-response clocks keep running.
            heard = conn.execute("SELECT MAX(received_at) FROM replies WHERE broker_id = ? AND label NOT IN "
                                 "('auto_reply', 'bounced', 'received')", (row["id"],)).fetchone()[0]
            if not last_sent or (heard and heard > last_sent and row["state"] != "awaiting_confirmation"):
                continue
            age = (now - last_sent) / 86400
            if age >= cfg["no_response_days"]:
                plan.set_state(conn, row["id"], "no_response", f"no answer in {int(age)} days")
                counts["no_response"] += 1
            elif row["follow_up_at"] and row["follow_up_at"] <= now and not row["followed_up"]:
                entry = registry.broker(row["id"]) or {}
                approved = json.loads(row["approved"] or "{}")
                if approved.get("form_only") or (approved.get("method") != "email" and not entry.get("privacy_email")):
                    conn.execute("UPDATE brokers SET followed_up = 1 WHERE id = ?", (row["id"],))
                    continue                    # no address to follow up at, or one that only points at the form
                if mode == "standing" and approved.get("method") == "email":
                    send.queue(conn, row["id"], "follow-up", auto=True, detail=f"{int(age)} days without an answer")
                else:
                    plan.set_pending(conn, row["id"], {"kind": "follow-up"}, f"{int(age)} days without an answer")
                conn.execute("UPDATE brokers SET followed_up = 1 WHERE id = ?", (row["id"],))
                counts["follow_ups"] += 1
    return counts


def route_mail(rows, entries):
    """The mail poller sends this agent mail from the domains of brokers with anything in progress."""
    from mail import poller
    active = ("approved", "submitted", "awaiting_confirmation", "acknowledged", "still_listed", "relisted",
              "resubmitted", "needs_manual", "refused", "no_response")
    domains = set()
    for broker_id, row in rows.items():
        if row["state"] in active and broker_id in entries:
            domains.update(d for d in registry.get(entries[broker_id], "domains", []) if not registry.shared(d))
    domains.update(replies.PLATFORM_MAIL)         # capture keeps only what names a broker (replies._platform_broker)
    poller.watch_domains(sorted(domains), AGENT, replace=True)
    return len(domains)


def blocklist(rows=None, entries=None):
    """The browser extension's block rule set: the domains of brokers that refused or never answered."""
    entries = entries or registry.load()
    if rows is None:
        with vault.db() as conn:
            rows = {r["id"]: dict(r) for r in conn.execute("SELECT * FROM brokers WHERE state IN ('refused', "
                                                           "'no_response')")}
    out = {}
    for broker_id, row in rows.items():
        if row["state"] in ("refused", "no_response") and broker_id in entries:
            for domain in registry.get(entries[broker_id], "domains", []):
                out[domain] = {"broker": broker_id, "state": row["state"]}
    return out


def publish_blocklist(rows, entries):
    found = blocklist(rows, entries)
    with open(BLOCKLIST, "w", encoding="utf-8") as f:
        json.dump({"updated": time.time(), "domains": found}, f, indent=1)
    return len(found)


def record_exposure(domain, source, detail=""):
    """A tier-1 signal (the browser extension calls this, dataflow/service.py: a broker's script on a page
    where you typed your email)."""
    from lib import psl
    with vault.db() as conn:
        conn.execute("INSERT OR REPLACE INTO exposure VALUES (?, ?, ?, ?)",
                     (psl.registrable_domain(domain), source, detail, time.time()))


def report_due(cfg):
    now = datetime.now()
    slot = now.replace(hour=cfg["report_hour"], minute=0, second=0, microsecond=0) - \
        timedelta(days=(now.weekday() - cfg["report_weekday"]) % 7)
    if slot > now:
        slot -= timedelta(days=7)
    last = vault.get("last_report_at") or 0
    return last < slot.timestamp()


def run_pass(log, notify=False, scans=True, click=True, api=None):
    """One pass, unless another is running (launchd's, and Run now from a window, must not overlap: a
    form or an email could go twice). Returns a summary."""
    from mail import store as mail_store
    watch.attach(log)
    with mail_store.file_lock("broker-pass", wait=False) as held:
        if not held:
            watch.say("a pass is already running; this one stood down", "pass")
            return {"skipped": "another pass is running"}
        return _pass(log, notify, scans, click, api)


def _pass(log, notify, scans, click, api):
    cfg = plan.config()
    found = vault.identity()
    if not vault.has_identity(found):
        return {"waiting": "add your name and current address in the broker dashboard (Your details)"}
    mode = plan.approval_mode()
    entries = registry.load()
    watch.say("pass started", "pass")
    rows = plan.ensure_rows()
    out = {"replies": replies.handle(log, cfg, click)}
    if scans:
        with vault.db() as conn:
            due = _scan_due(conn, cfg, rows, entries)
        if due:
            broker_id, why = due
            watch.say(f"searching {entries[broker_id].get('name', broker_id)} for you ({why})", "step", broker_id)
            from brokers import forms
            try:
                with forms.session() as context:
                    result = scan.scan_one(context, broker_id, entries[broker_id], found, cfg, log)
            except Exception as error:
                result = {"listed": "unknown", "listings": [], "error": f"{type(error).__name__}: {str(error)[:120]}"}
            scan.record(broker_id, result)
            watch.say(f"search result: {result['listed']}" + (f" ({result['error']})" if result.get("error") else ""),
                      "done", broker_id)
            _after_scan(broker_id, why, result, cfg, mode)
            out["scanned"] = {broker_id: f"{why}: {result['listed']}"}
    out["sent"] = send.run(log, api, cfg)
    out["timers"] = timers(cfg, mode)
    rows = plan.ensure_rows()
    try:
        out["routed_domains"] = route_mail(rows, entries)
    except Exception as error:
        log.warning("could not update mail routing: %s", error)
    out["blocklist"] = publish_blocklist(rows, entries)
    if report_due(cfg):
        out["report"] = report.build(notify=notify)
    vault.put("last_pass_at", time.time())
    vault.put("last_pass", out)
    sent, timed = out["sent"], out["timers"]
    with vault.db() as conn:          # failed attempts wait RETRY_AFTER: say so, or a pass looks like it skipped them
        later = conn.execute("SELECT broker_id, queued_at FROM requests WHERE status = 'queued' AND queued_at > ? "
                             "ORDER BY queued_at", (time.time(),)).fetchall()
    held = (f", {len(later)} retr{'y' if len(later) == 1 else 'ies'} held back after a failure (next: "
            f"{later[0]['broker_id']} at {time.strftime('%H:%M', time.localtime(later[0]['queued_at']))})") if later else ""
    watch.say(f"pass done: {sum((out['replies'] or {}).values())} emails sorted, {sent.get('emails', 0)} sent, "
              f"{sent.get('forms', 0)} forms, {sent.get('waiting', 0)} waiting their turn, "
              f"{timed.get('follow_ups', 0)} follow-ups" + held + (", the weekly report" if "report" in out else ""), "pass")
    return out
