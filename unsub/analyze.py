"""Phases 2, 3 and 5: the weekly analysis, the report and the banner.

One run (the `unsubscriber` agent on Sundays, or `unsub analyze`):
    1. takes whatever capture has not yet (capture.run); the first time only, also reads Trash and
       the inbox for the 30 days before capture started (capture.backfill)
    2. folds each captured message older than settle_hours into its sender's running totals and
       deletes it; younger ones count in this report as they stand now, and are kept so what you
       do with them still counts next time
    3. sorts every sender that had mail into keep / unsubscribe / block / unsure (rules.py);
       one about to be proposed is first checked against your Sent mail (never touch someone
       you have written to)
    4. adds what else needs you: Manual (an unsubscribe that failed) and Non-compliant (still
       mailing noncompliant_days after an unsubscribe)
    5. permanent mode: senders matching a saved rule are handled now, and so are non-compliant
       ones (blocked); all of it is listed under Applied automatically
    6. stores the report, deletes the folded messages (bodies included; sender totals stay) and
       shows a banner with the counts
"""
import json
import time

from lib.common import banner
from mail import gmail, store as mail_store
from unsub import OPEN_DASHBOARD, actions, capture, rules, store

SECTIONS = ("unsubscribe", "block", "unsure", "manual", "auto", "noncompliant")
SECTION_NAMES = {"unsubscribe": "to unsubscribe", "block": "to block", "unsure": "unsure", "manual": "manual",
                 "auto": "applied automatically", "noncompliant": "non-compliant"}
PROPOSAL = {"unsubscribe": "unsubscribe", "block": "block", "unsure": None, "manual": "block",
            "noncompliant": "block"}
SENT_RECHECK_DAYS = 30
ANALYSED_KEEP_DAYS = 45     # longer than Gmail keeps Trash (30), so a backfill never meets a forgotten one
SENT_LOOK_AT = 10           # sent messages read per domain, to tell correspondence from unsubscribe requests


def mode():
    return store.get("mode", "temporary")


def _date(t):
    return time.strftime("%Y-%m-%d", time.localtime(t)) if t else None


def wrote_to(sender, cfg, api):
    """True if your Sent mail has anything to the sender's domain (its address, for free-mail) other than
    unsubscribe requests (rules.unsubscribe_request). None if Gmail can't say."""
    keys = sender["addresses"] if sender["from_domain"] in cfg["freemail"] else [sender["from_domain"]]
    own = set(store.get("own_sent", []))
    with store.db() as conn:
        mailtos = rules.unsubscribe_mailtos(conn)
    for key in [k for k in keys if k]:
        with store.db() as conn:
            row = conn.execute("SELECT sent, checked_at FROM sent_domains WHERE domain = ?", (key,)).fetchone()
        if row and row["sent"]:
            return True
        if row and time.time() - row["checked_at"] < SENT_RECHECK_DAYS * 86400:
            continue
        try:
            found = False
            for item in api.list_messages(f"in:sent to:{key}", limit=SENT_LOOK_AT):
                try:
                    meta = api.message(item["id"], "metadata", headers=("To", "Cc", "Bcc", "Subject"))
                except gmail.NotFound:
                    continue
                headers = [[h["name"], h["value"]] for h in meta.get("payload", {}).get("headers", [])]
                if not rules.unsubscribe_request(item["id"], headers, own, mailtos):
                    found = True
                    break
        except (gmail.GmailError, gmail.NotSignedIn, OSError):
            return None
        with store.db() as conn:
            conn.execute("INSERT OR REPLACE INTO sent_domains VALUES (?, ?, 'search', ?)", (key, int(found), time.time()))
        if found:
            return True
    return False


def _counts(messages):
    """What a sender's messages add to its totals, and their delete-after times."""
    facts = [m["facts"] for m in messages]
    add = {
        "total": len(messages), "opened": sum(bool(m["opened_at"]) for m in messages),
        "deleted": sum(bool(m["deleted_at"]) for m in messages),
        "deleted_unread": sum(bool(m["deleted_at"] and m["deleted_unread"]) for m in messages),
        "archived": sum(bool(m["archived_at"]) for m in messages),
        "archived_unread": sum(bool(m["archived_at"] and m["archived_unread"]) for m in messages),
        "starred": sum(bool(m["starred_at"]) for m in messages),
        "replied": sum(bool(m["replied_at"]) for m in messages),
        "spam_reports": sum(bool(m["spam_at"]) for m in messages),
        "transactional": sum(bool(f.get("transactional")) for f in facts),
        "promotions": sum(bool(f.get("promotions")) for f in facts), "bulk": sum(bool(f.get("bulk")) for f in facts),
        "aligned": sum(f.get("auth") == "aligned" for f in facts), "auth_fail": sum(f.get("auth") == "fails" for f in facts),
        "spam_folder": sum(bool(f.get("spam_folder")) for f in facts),
    }
    # Backfilled mail was found already deleted: when is unknown, so it has no delete time.
    return add, [m["deleted_at"] - m["arrived"] for m in messages if m["deleted_at"] and not m["facts"].get("backfilled")]


def _group(rows):
    by_sender = {}
    for row in rows:
        by_sender.setdefault(row["sender_id"], []).append(row)
    return by_sender


def _fold(conn, rows):
    """Add settled messages to their senders' totals for good. {sender_id: [message rows]}."""
    by_sender = _group(rows)
    for sender_id, messages in by_sender.items():
        sender = store.sender(conn, sender_id)
        add, seconds = _counts(messages)
        conn.execute(f"UPDATE senders SET {', '.join(f'{k} = {k} + ?' for k in add)}, delete_seconds = ?, "
                     "last_analyzed = ? WHERE id = ?",
                     (*add.values(), json.dumps((sender["delete_seconds"] + seconds)[-50:]), time.time(), sender_id))
    return by_sender


def _with_fresh(sender, messages):
    """The sender as it would be with its fresh (not yet settled) messages counted too; nothing is saved."""
    if not messages:
        return sender
    add, seconds = _counts(messages)
    view = dict(sender)
    for key, value in add.items():
        view[key] = (view[key] or 0) + value
    view["delete_seconds"] = sender["delete_seconds"] + seconds
    view["last_seen"] = max(view["last_seen"] or 0, max(m["arrived"] for m in messages))
    return view


def notices_from(sender):
    """How many receipts and notices the sender's own transactional stream has had (0 if none)."""
    if sender["key"].endswith(rules.NOTICES):
        return 0
    with store.db() as conn:
        row = conn.execute("SELECT id, total FROM senders WHERE key = ?", (rules.base_key(sender["key"]) + rules.NOTICES,)).fetchone()
        if row is None:
            return 0
        return row["total"] + conn.execute("SELECT COUNT(*) FROM messages WHERE sender_id = ? AND arrived > "
                                           "COALESCE((SELECT last_analyzed FROM senders WHERE id = ?), 0)",
                                           (row["id"], row["id"])).fetchone()[0]


def _proposal_evidence(sender, info, this_week):
    out = {"sender": sender["label"], "evidence": rules.evidence(sender, info, this_week), "reason": info["why"],
           "scores": {"unwanted": info["unwanted"], "spam": info["spam"]}}
    notices = notices_from(sender)
    if notices:
        out["notices"] = (f"they also sent {notices} receipt{'s' if notices != 1 else ''} or notice"
                          f"{'s' if notices != 1 else ''}: whatever you decide here does not touch those")
    return out


def analyze(log, notify=True, api=None):
    """One analysis. The report's summary (or {"skipped": why})."""
    with mail_store.file_lock("unsub-analyze", wait=False) as held:
        if not held:
            return {"skipped": "an analysis is already running"}
        api = api or gmail.client()
        capture.run(log, api)
        if not store.get("last_backfill_at"):         # once: capture covers everything after it
            try:
                found = capture.backfill(log, api)
                store.put("last_backfill_at", time.time())
                log.info("backfill: %s", found)
            except (gmail.GmailError, gmail.NotSignedIn, OSError) as error:
                log.warning("backfill failed, analysing without it (it tries again next time): %s", error)
        return _analyze(log, notify, api)


def spam_pass(log, notify=True, api=None):
    """The Spam pass: read Gmail's Spam folder (capture.backfill spam=True), then analyse, so its
    senders that can be unsubscribed from safely land in the report. Ids already counted are skipped,
    so running it again only adds what arrived in Spam since."""
    with mail_store.file_lock("unsub-analyze", wait=False) as held:
        if not held:
            return {"skipped": "an analysis is already running"}
        api = api or gmail.client()
        found = capture.backfill(log, api, spam=True)
        store.put("last_spam_pass_at", time.time())
        store.put("last_spam_pass", found)
        summary = _analyze(log, notify, api)
    return {"spam_folder": found, **summary}


def _analyze(log, notify, api):
    cfg = rules.config()
    current_mode = mode()
    started = time.time()
    window_start = store.get("last_analysis_at") or 0
    cutoff = started - cfg["settle_hours"] * 3600
    with store.db() as conn:
        everything = [dict(r) for r in conn.execute("SELECT * FROM messages WHERE arrived <= ? ORDER BY arrived", (started,))]
        for row in everything:
            row["facts"] = json.loads(row["facts"] or "{}")
        # Settled mail is added to the totals and then deleted; fresh mail (younger than settle_hours)
        # is counted in this report as it stands now, and kept, so what you do with it still counts.
        rows = [r for r in everything if r["arrived"] < cutoff]
        fresh = _group([r for r in everything if r["arrived"] >= cutoff])
        _fold(conn, rows)
        by_sender = _group(everything)
        saved_rules = {r["sender_id"]: dict(r) for r in conn.execute("SELECT * FROM rules")}
        senders = {sid: _with_fresh(store.sender(conn, sid), fresh.get(sid, [])) for sid in by_sender}
        manual = [store.sender(conn, r["id"]) for r in conn.execute("SELECT id FROM senders WHERE status = 'manual'")]
        report_id = conn.execute("INSERT INTO reports (created_at, mode, window_start, window_end) VALUES (?, ?, ?, ?)",
                                 (started, current_mode, window_start, started)).lastrowid

    items, automatic = [], []
    for sender_id, messages in by_sender.items():
        sender, rule = senders[sender_id], saved_rules.get(sender_id)
        if sender["status"] in ("blocked", "manual"):
            continue
        if sender["status"] == "unsubscribed":
            grace = (sender["status_at"] or 0) + cfg["noncompliant_days"] * 86400
            late = [m for m in messages if m["arrived"] > grace]
            if late:
                evidence = {"sender": sender["label"], "unsubscribed": _date(sender["status_at"]),
                            "method": sender["status_detail"], "since": f"{len(late)} email{'s' if len(late) != 1 else ''} "
                            f"after {cfg['noncompliant_days']} days, last {_date(late[-1]['arrived'])}"}
                if current_mode == "permanent":
                    automatic.append((sender_id, "block", {"scope": "address"}, "non-compliant after unsubscribing"))
                else:
                    items.append(("noncompliant", sender_id, "block", evidence))
            continue
        if rule and rule["kind"] == "deny" and current_mode == "permanent":
            options = {"scope": "address"} if rule["action"] == "block" else {}
            automatic.append((sender_id, rule["action"], options, f"rule from {_date(rule['created_at'])}"))
            continue
        verdict, info = rules.classify(sender, cfg, False, rule)
        if verdict in ("unsubscribe", "block", "unsure"):
            wrote = wrote_to(sender, cfg, api)
            if wrote:
                continue
            if wrote is None and verdict != "unsure":
                verdict, info["why"] = "unsure", info["why"] + "; your Sent mail could not be checked"
        if verdict == "keep":
            continue
        items.append((verdict, sender_id, PROPOSAL[verdict], _proposal_evidence(sender, info, len(messages))))

    with store.db() as conn:          # an automatic unsubscribe that failed above is in Manual already
        listed = {r[0] for r in conn.execute("SELECT sender_id FROM items WHERE report_id = ? AND section = 'manual'",
                                             (report_id,))}
    for sender in manual:
        if sender["id"] not in listed:
            items.append(("manual", sender["id"], "block", actions.manual_evidence(sender)))

    if automatic:
        with store.db() as conn:
            for sender_id, action, options, rule_text in automatic:
                actions.queue(conn, sender_id, action, options, auto=True, rule=rule_text, report_id=report_id)
        actions.work(log, wait=True)
    with store.db() as conn:
        for row in conn.execute("SELECT * FROM actions WHERE report_id = ? AND auto = 1", (report_id,)).fetchall():
            sender = store.sender(conn, row["sender_id"])
            items.append(("auto", row["sender_id"], row["action"], {
                "sender": sender["label"] if sender else f"s{row['sender_id']}", "rule": row["rule"],
                "action": row["action"], "outcome": row["status"], "method": row["method"], "detail": row["detail"],
                "override": f"unsub override s{row['sender_id']}"}))
        for section, sender_id, proposal, evidence in items:
            conn.execute("INSERT INTO items (report_id, section, sender_id, proposal, evidence, status) VALUES (?, ?, ?, ?, ?, ?)",
                         (report_id, section, sender_id, proposal, json.dumps(evidence),
                          "done" if section == "auto" else "pending"))
        counts = {s: sum(1 for i in items if i[0] == s) for s in SECTIONS}
        summary = {"report": report_id, "mode": current_mode, "messages": len(everything), "senders": len(by_sender),
                   "fresh": len(everything) - len(rows),
                   "sections": {s: n for s, n in counts.items() if n}, "auto": counts["auto"]}
        conn.execute("UPDATE reports SET summary = ? WHERE id = ?", (json.dumps(summary), report_id))
        if rows:
            ids = [r["id"] for r in rows]
            for start in range(0, len(ids), 500):
                chunk = ids[start:start + 500]
                conn.execute(f"DELETE FROM messages WHERE id IN ({','.join('?' * len(chunk))})", chunk)
                conn.executemany("INSERT OR IGNORE INTO analysed VALUES (?, ?)", [(i, started) for i in chunk])
        conn.execute("DELETE FROM analysed WHERE at < ?", (started - ANALYSED_KEEP_DAYS * 86400,))
    store.put("last_analysis_at", started)
    store.put("last_analysis", summary)
    log.info("analysis: %s", summary)
    if notify:
        banner("Inbox report", banner_text(counts), post=OPEN_DASHBOARD)
    return summary


def banner_text(counts):
    asks = [f"{counts[s]} {SECTION_NAMES[s]}" for s in SECTIONS if s != "auto" and counts.get(s)]
    head = " · ".join(asks) if asks else "nothing to decide this week"
    return f"{head}\n{counts.get('auto', 0)} applied automatically. click to open the report"
