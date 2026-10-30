r"""The inbox unsubscribe agent's commands, at the assistant's prompt (atlas/mail_commands.py) and in a
terminal (`python -m unsub`). Answers are compact JSON with every list paged; message bodies never.

    unsub status                          last capture and analysis, mode, pending decisions
    unsub report                          the latest report: counts, and the first section's items
    unsub report section=unsure page=2    one section (unsubscribe block unsure manual auto noncompliant)
    unsub report format=text              the whole report as lines to read
    unsub decide approve=s3,s5 reject=s7  do (or don't do) what the report proposes
    unsub decide block=s9 trash=yes       choose for yourself: unsubscribe= block= keep=
    unsub decide done=s4                  a Manual one you unsubscribed from yourself, at their page
    unsub decide approve=all section=unsubscribe
    unsub mode permanent                  decisions become rules (temporary: they only apply once)
    unsub promote                         the rules your testing decisions would make; confirm=yes saves them
    unsub override s12                    delete s12's rule; undo its block
    unsub sender s12                      one sender's history (also by address, domain or list id)
    unsub actions status=failed           the actions log
    unsub capture · unsub analyze         run a capture pass, or this week's analysis, now
    unsub spamfolder                      read Gmail's Spam folder too, then analyse: its senders that
                                          can be unsubscribed from safely go in the report

`spam` works wherever `unsub` does (`spam decide block=s4`). The bare word, and `unsub report`
with nothing after it, open the inbox dashboard instead (atlas/mail_commands.py). The function
names work too: set_mode, sender_info, report, decide…
"""
import json
import re
import time

from mail import auth, ops as mail_ops
from unsub import actions, analyze, capture, rules, store

PER_PAGE = 10
MAX_PER_PAGE = 50
VERBS = ("approve", "reject", "keep", "unsubscribe", "block", "done")
_ARG = re.compile(r"""(\w+)\s*=\s*(?:"([^"]*)"|'([^']*)'|(\S+))""")
_ID = re.compile(r"^s?(\d+)$", re.IGNORECASE)

Problem = mail_ops.Problem
as_json = mail_ops.as_json


def _paging(args):
    try:
        page = max(1, int(args.get("page", 1)))
        per = max(1, min(MAX_PER_PAGE, int(args.get("per", PER_PAGE))))
    except ValueError:
        raise Problem("page= and per= are numbers")
    return page, per


def _paged(rows, page, per):
    return {"items": rows[(page - 1) * per:page * per], "page": page, "pages": max(1, -(-len(rows) // per)),
            "total": len(rows)}


def _date(t):
    return time.strftime("%Y-%m-%d", time.localtime(t)) if t else None


def _ago(t):
    return None if not t else f"{round((time.time() - t) / 60)} min" if time.time() - t < 7200 else \
        f"{round((time.time() - t) / 3600, 1)} h" if time.time() - t < 172800 else f"{round((time.time() - t) / 86400)} d"


def find_sender(ref):
    """A sender row from s12, 12, an address, a list id or a domain. Raises Problem."""
    ref = (ref or "").strip().lower()
    if not ref:
        raise Problem("which sender? s12, an address or a domain")
    with store.db() as conn:
        match = _ID.match(ref)
        if match:
            found = store.sender(conn, int(match.group(1)))
            if found:
                return found
            raise Problem(f"no sender {ref}")
        rows = conn.execute("SELECT id, label FROM senders WHERE key IN (?, ?, ?, ?, ?) ORDER BY last_seen DESC",
                            tuple(p + ref + s for p, s in (("addr:", ""), ("addr:", rules.ADS), ("addr:", rules.NOTICES),
                                                           ("list:", ""), ("list:", rules.NOTICES)))).fetchall()
        if len(rows) > 1:
            raise Problem(f"{ref} sends {len(rows)} kinds of mail: " + ", ".join(f"s{r['id']} {r['label']}" for r in rows))
        row = rows[0] if rows else None
        if row is None:
            rows = conn.execute("SELECT id, label FROM senders WHERE from_domain = ? ORDER BY last_seen DESC",
                                (ref,)).fetchall()
            if len(rows) == 1:
                row = rows[0]
            elif rows:
                raise Problem(f"{len(rows)} senders at {ref}: " + ", ".join(f"s{r['id']} {r['label']}" for r in rows[:8]))
        if row is None:
            raise Problem(f"no sender {ref}")
        return store.sender(conn, row["id"])


def _latest_report(conn):
    return conn.execute("SELECT * FROM reports ORDER BY id DESC LIMIT 1").fetchone()


def _item_json(row):
    item = {"id": f"s{row['sender_id']}", **json.loads(row["evidence"] or "{}")}
    if row["section"] not in ("auto",):
        item["proposal"] = row["proposal"]
        item["status"] = row["status"] if row["status"] == "pending" else f"{row['status']}: {row['decision']}"
    return item


# ------------------------------------------------------------ the operations

def status():
    with store.db() as conn:
        report = _latest_report(conn)
        pending = conn.execute("SELECT COUNT(*) FROM items WHERE report_id = ? AND status = 'pending'",
                               (report["id"] if report else -1,)).fetchone()[0]
        queued = dict(conn.execute("SELECT status, COUNT(*) FROM actions WHERE status IN ('queued', 'running') "
                                   "GROUP BY status").fetchall())
        failed = conn.execute("SELECT COUNT(*) FROM actions WHERE status = 'failed' AND done_at > ?",
                              (time.time() - 7 * 86400,)).fetchone()[0]
        waiting = conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
        senders = conn.execute("SELECT COUNT(*) FROM senders").fetchone()[0]
        saved = dict(conn.execute("SELECT kind, COUNT(*) FROM rules GROUP BY kind").fetchall())
    return {
        "mode": analyze.mode(),
        "last_capture_ago": _ago(store.get("last_capture_at")), "capture_queue": capture.pending(),
        "capture_error": store.get("last_capture_error"),
        "last_analysis": _date(store.get("last_analysis_at")), "last_report": store.get("last_analysis"),
        "pending_decisions": pending, "actions": {**queued, "failed_7d": failed},
        "messages_waiting": waiting, "senders": senders, "rules": saved, "db": store.size_text(),
    }


def report(section=None, page=1, per=PER_PAGE, fmt="json"):
    with store.db() as conn:
        row = _latest_report(conn)
        if row is None:
            return {"report": None, "note": "no report yet: the first comes on Sunday, or say unsub analyze"}
        items = conn.execute("SELECT * FROM items WHERE report_id = ? ORDER BY id", (row["id"],)).fetchall()
    summary = json.loads(row["summary"] or "{}")
    by_section = {s: [i for i in items if i["section"] == s] for s in analyze.SECTIONS}
    if fmt == "text":
        return _text_report(row, summary, by_section, per)
    head = {"report": row["id"], "date": _date(row["created_at"]), "mode": row["mode"],
            "sections": {s: {"total": len(v), "pending": sum(i["status"] == "pending" for i in v)}
                         for s, v in by_section.items() if v}, "auto": len(by_section["auto"])}
    if section is None:
        section = next((s for s in analyze.SECTIONS if any(i["status"] == "pending" for i in by_section[s])),
                       next((s for s in analyze.SECTIONS if by_section[s]), None))
        if section is None:
            return {**head, "note": "nothing to decide"}
    if section not in analyze.SECTIONS:
        raise Problem(f"no section {section}: " + " ".join(analyze.SECTIONS))
    return {**head, "section": section, **_paged([_item_json(i) for i in by_section[section]], page, per),
            "decide": "unsub decide approve=s1,s2 reject=s3 (or unsubscribe= block= keep=)"}


_DONE_TEXT = {"unsubscribe": "unsubscribed", "block": "blocked"}


def _state(item, action):
    """What the dashboard says about an entry: '' while it waits for you, else what came of it."""
    if item["section"] == "auto":
        if item["status"] == "overridden":
            return "overridden"
        evidence = json.loads(item["evidence"] or "{}")
        return f"{evidence.get('outcome')}" + (f" ({evidence['method']})" if evidence.get("method") else "")
    if item["status"] == "pending":
        return ""
    if item["status"] == "rejected":
        return "kept"
    if action is None or (item["decided_at"] and action["queued_at"] < item["decided_at"] - 1):
        return f"{item['decision']}: queued"
    if action["status"] in ("queued", "running"):
        return {"unsubscribe": "unsubscribing…", "block": "blocking…"}.get(action["action"], "working…")
    if action["status"] == "done":
        return _DONE_TEXT.get(action["action"], "done") + (f" ({action['method']})" if action["action"] == "unsubscribe" else "")
    if action["status"] == "manual":
        return "moved to Manual"
    return "failed"


def board():
    """Everything the dashboard shows, in one read: the latest report's entries by section, each with
    its state, plus the mode and whether anything is still running."""
    from mail import store as mail_store

    with mail_store.file_lock("unsub-analyze", wait=False) as free:
        analysing = not free
    with store.db() as conn:
        report = _latest_report(conn)
        busy = conn.execute("SELECT COUNT(*) FROM actions WHERE status IN ('queued', 'running')").fetchone()[0]
        out = {"mode": analyze.mode(), "analysing": analysing, "busy": busy, "report": None,
               "sections": {s: [] for s in analyze.SECTIONS}}
        if report is None:
            return out
        items = conn.execute("SELECT * FROM items WHERE report_id = ? ORDER BY id", (report["id"],)).fetchall()
        latest = {}
        for row in conn.execute("SELECT * FROM actions WHERE sender_id IN (SELECT sender_id FROM items WHERE report_id = ?) "
                                "ORDER BY id", (report["id"],)):
            latest[row["sender_id"]] = row
        detail = {r["sender_id"]: (r["detail"] or "") for r in latest.values()}
    summary = json.loads(report["summary"] or "{}")
    out["report"] = {"id": report["id"], "date": _date(report["created_at"]), "mode": report["mode"],
                     "messages": summary.get("messages", 0), "senders": summary.get("senders", 0)}
    for item in items:
        if item["status"] == "rejected":          # kept: dismissed from the dashboard
            continue
        entry = _item_json(item)
        entry.update(sender_id=item["sender_id"], section=item["section"], pending=item["status"] == "pending",
                     state=_state(item, latest.get(item["sender_id"])),
                     action_detail=detail.get(item["sender_id"], "") if item["status"] != "pending" else "")
        out["sections"][item["section"]].append(entry)
    return out


def message_ids(sender_id, extra=()):
    """A sender's messages to read, newest first: its recent list, plus any it still has waiting."""
    with store.db() as conn:
        sender = store.sender(conn, sender_id)
        waiting = [r["id"] for r in conn.execute("SELECT id FROM messages WHERE sender_id = ? ORDER BY arrived DESC",
                                                  (sender_id,))]
    found = list(extra) + [m[0] for m in (sender or {}).get("recent", [])] + waiting
    latest = ((sender or {}).get("latest") or {}).get("message_id")
    if latest:
        found.append(latest)
    return list(dict.fromkeys(i for i in found if i))


def read_message(message_id):
    """One message to read in the dashboard: headers and its text. Reading through the API leaves it
    unread in Gmail (so it doesn't count as opened), and the text carries no remote images."""
    from mail import gmail, headers as hdr
    from unsub import body

    try:
        full = gmail.client().message(message_id, "full")
    except gmail.NotFound:
        return {"id": message_id, "gone": True}
    headers = [[h["name"], h["value"]] for h in full.get("payload", {}).get("headers", [])]
    text, _links = body.extract(full.get("payload", {}))
    labels = full.get("labelIds", [])
    return {"id": message_id, "from": hdr.first(headers, "From"), "to": hdr.first(headers, "To"),
            "date": hdr.first(headers, "Date"), "subject": hdr.first(headers, "Subject") or "(no subject)",
            "text": text or full.get("snippet", ""), "where": "Trash" if "TRASH" in labels else
            "inbox" if "INBOX" in labels else "archived", "unread": "UNREAD" in labels}


def summary_line():
    """One line for a banner: the latest report's counts."""
    with store.db() as conn:
        row = _latest_report(conn)
        if row is None:
            return "inbox dashboard open: no report yet (Analyse now makes one)"
        counts = dict(conn.execute("SELECT section, COUNT(*) FROM items WHERE report_id = ? AND status = 'pending' "
                                   "GROUP BY section", (row["id"],)).fetchall())
    waiting = [f"{counts[s]} {analyze.SECTION_NAMES[s]}" for s in analyze.SECTIONS if s != "auto" and counts.get(s)]
    return "inbox dashboard open: " + (" · ".join(waiting) if waiting else "nothing waiting for you")


def _text_report(row, summary, by_section, per):
    lines = [f"Inbox report {row['id']}, {_date(row['created_at'])} ({row['mode']} mode): "
             f"{summary.get('messages', 0)} emails from {summary.get('senders', 0)} senders"]
    for section in analyze.SECTIONS:
        entries = by_section[section]
        if not entries:
            continue
        lines.append(f"\n{analyze.SECTION_NAMES[section].upper()} ({len(entries)})")
        for entry in entries[:per]:
            item = _item_json(entry)
            if section == "auto":
                lines.append(f"  {item['id']} {item['sender']}: {item['action']} -> {item['outcome']}"
                             f"{' (' + item['method'] + ')' if item.get('method') else ''}; {item['rule']}")
            elif section == "manual":
                lines.append(f"  {item['id']} {item['sender']}: {item['why']}. {item['link'] or 'no link'}")
            elif section == "noncompliant":
                lines.append(f"  {item['id']} {item['sender']}: unsubscribed {item['unsubscribed']}, {item['since']}")
            else:
                lines.append(f"  {item['id']} {item['sender']}: {item['evidence']}"
                             + (f" [{item['reason']}]" if section == "unsure" else ""))
            if entry["status"] != "pending" and section != "auto":
                lines[-1] += f"  ({entry['status']}: {entry['decision']})"
        if len(entries) > per:
            lines.append(f"  … {len(entries) - per} more: unsub report section={section} page=2")
    lines.append("\nunsub decide approve=s1,s2 reject=s3   ·   or unsubscribe= block= keep=")
    return "\n".join(lines)


def _ids(value, conn, section, report_id):
    if value.strip().lower() == "all":
        if not section:
            raise Problem("approve=all needs a section: approve=all section=unsubscribe")
        return [r["sender_id"] for r in conn.execute("SELECT sender_id FROM items WHERE report_id = ? AND section = ? "
                                                      "AND status = 'pending'", (report_id, section))]
    out = []
    for part in re.split(r"[,\s]+", value.strip()):
        if not part:
            continue
        match = _ID.match(part)
        if not match:
            raise Problem(f"{part} is not a sender id (s12)")
        out.append(int(match.group(1)))
    return out


def decide(choices, section=None, trash=None):
    """choices: {verb: "s3,s5"}. Queues the actions and starts the worker."""
    cfg = rules.config()
    current_mode = analyze.mode()
    trash = cfg["trash_existing_on_block"] if trash is None else trash
    done, errors, queued = [], [], 0
    with store.db() as conn:
        report = _latest_report(conn)
        report_id = report["id"] if report else None
        for verb, value in choices.items():
            for sender_id in _ids(value, conn, section, report_id):
                sender = store.sender(conn, sender_id)
                if sender is None:
                    errors.append(f"s{sender_id}: no such sender")
                    continue
                query = "SELECT * FROM items WHERE report_id = ? AND sender_id = ? AND section != 'auto'"
                params = [report_id, sender_id]
                if section:
                    query += " AND section = ?"
                    params.append(section)
                item = conn.execute(query + " ORDER BY status = 'pending' DESC, id LIMIT 1", params).fetchone()
                if verb in ("approve", "reject") and item is None:
                    errors.append(f"s{sender_id} is not in the latest report: say unsubscribe=, block= or keep=")
                    continue
                action = item["proposal"] if verb == "approve" else None if verb in ("reject", "keep") else verb
                if verb == "approve" and action is None:
                    errors.append(f"s{sender_id} is unsure, there is no proposal: say unsubscribe=, block= or keep=")
                    continue
                if action in ("unsubscribe", "block") and rules.category(sender["key"]) == "notices":
                    errors.append(f"s{sender_id} is receipts and notices: those are never unsubscribed from or blocked")
                    continue
                options = {}
                if action == "block":
                    options = {"scope": actions.block_scope(sender, item["section"] if item else None, cfg),
                               "trash": bool(trash)}
                if action == "done":
                    actions.record_done(conn, sender_id, report_id)
                elif action:
                    actions.queue(conn, sender_id, action, options, report_id=report_id)
                    queued += 1
                if item is not None:
                    conn.execute("UPDATE items SET status = ?, decision = ?, decided_at = ? WHERE id = ?",
                                 ("rejected" if action is None else "approved", action or "keep", time.time(), item["id"]))
                conn.execute("INSERT INTO decisions (sender_id, report_id, section, proposal, decision, action, mode, at) "
                             "VALUES (?, ?, ?, ?, ?, ?, ?, ?)", (sender_id, report_id, item["section"] if item else None,
                                                                 item["proposal"] if item else None, verb, action or "keep",
                                                                 current_mode, time.time()))
                if action is None and sender["status"] == "manual":
                    store.set_status(conn, sender_id, None)      # handled: it leaves Manual for good
                if current_mode == "permanent" and action != "done":   # done: the unsubscribed status covers it
                    _save_rule(conn, sender_id, action, "decision")
                done.append({"id": f"s{sender_id}", "sender": sender["label"], "action": action or "keep"})
    if queued:
        actions.start_worker()
    out = {"decided": done, "mode": current_mode}
    if queued:
        out["working"] = f"{queued} action{'s' if queued != 1 else ''} running now; a banner says when they are done"
    if current_mode == "permanent" and any(d["action"] != "done" for d in done):
        out["rules"] = "saved: these senders are handled without asking from now on"
    if errors:
        out["errors"] = errors
    return out


def _save_rule(conn, sender_id, action, source):
    conn.execute("INSERT OR REPLACE INTO rules VALUES (?, ?, ?, ?, ?)",
                 (sender_id, "allow" if action is None else "deny", action or "keep", time.time(), source))


def set_mode(mode):
    mode = (mode or "").strip().lower()
    if mode not in ("temporary", "permanent"):
        raise Problem("mode is temporary (decisions apply once) or permanent (decisions become rules)")
    store.put("mode", mode)
    out = {"mode": mode}
    if mode == "permanent":
        with store.db() as conn:
            waiting = conn.execute("SELECT COUNT(DISTINCT sender_id) FROM decisions WHERE promoted = 0 AND mode = 'temporary' "
                                   "AND sender_id NOT IN (SELECT sender_id FROM rules)").fetchone()[0]
        if waiting:
            out["promote"] = f"{waiting} senders you decided on while testing could become rules: unsub promote"
    return out


def promote(confirm=False, page=1, per=PER_PAGE):
    with store.db() as conn:
        rows = conn.execute("SELECT d.* FROM decisions d JOIN (SELECT sender_id, MAX(id) AS last FROM decisions "
                            "WHERE promoted = 0 GROUP BY sender_id) l ON d.id = l.last "
                            "WHERE d.sender_id NOT IN (SELECT sender_id FROM rules) ORDER BY d.id").fetchall()
        proposed = []
        for row in rows:
            sender = store.sender(conn, row["sender_id"])
            proposed.append({"id": f"s{row['sender_id']}", "sender": sender["label"] if sender else "?",
                             "rule": "allow" if row["action"] == "keep" else f"deny: {row['action']}",
                             "decided": _date(row["at"])})
        if not confirm:
            return {**_paged(proposed, page, per), "confirm": "unsub promote confirm=yes saves all of these as rules"}
        for row in rows:
            _save_rule(conn, row["sender_id"], None if row["action"] == "keep" else row["action"], "promote")
        conn.execute("UPDATE decisions SET promoted = 1 WHERE promoted = 0")
    return {"saved": len(rows), "mode": analyze.mode(),
            "note": None if analyze.mode() == "permanent" else "rules only act in permanent mode: unsub mode permanent"}


def override(ref):
    sender = find_sender(ref)
    out = {"id": f"s{sender['id']}", "sender": sender["label"]}
    with store.db() as conn:
        rule = conn.execute("SELECT * FROM rules WHERE sender_id = ?", (sender["id"],)).fetchone()
        if rule:
            conn.execute("DELETE FROM rules WHERE sender_id = ?", (sender["id"],))
            out["rule_deleted"] = f"{rule['kind']} {rule['action']}"
        conn.execute("UPDATE items SET status = 'overridden' WHERE sender_id = ? AND section = 'auto'", (sender["id"],))
    if sender["status"] == "blocked":
        out["unblocked"] = actions.undo_block(sender)
    elif sender["status"] == "unsubscribed":
        link = (sender["latest"].get("unsub") or {})
        with store.db() as conn:
            conn.execute("UPDATE senders SET resubscribe = 1 WHERE id = ?", (sender["id"],))
        out["resubscribe"] = "an unsubscribe can't be undone from here: sign up again on their site if you want their mail"
        if link.get("link") or link.get("https"):
            out["link"] = link.get("link") or link.get("https")
    if len(out) == 2:
        out["note"] = "no rule and nothing to undo"
    return out


def sender_info(ref):
    sender = find_sender(ref)
    cfg = rules.config()
    verdict, info = rules.classify(sender, cfg) if sender["total"] else (None, None)
    latest = sender["latest"]
    with store.db() as conn:
        waiting = conn.execute("SELECT COUNT(*) FROM messages WHERE sender_id = ?", (sender["id"],)).fetchone()[0]
        rule = conn.execute("SELECT kind, action, created_at FROM rules WHERE sender_id = ?", (sender["id"],)).fetchone()
        done = conn.execute("SELECT action, status, method, detail, done_at, auto FROM actions WHERE sender_id = ? "
                            "ORDER BY id DESC LIMIT 5", (sender["id"],)).fetchall()
    out = {"id": f"s{sender['id']}", "sender": sender["label"], "key": sender["key"], "domain": sender["from_domain"],
           "addresses": sender["addresses"], "first_seen": _date(sender["first_seen"]), "last_seen": _date(sender["last_seen"]),
           "waiting_for_analysis": waiting, "status": sender["status"], "status_since": _date(sender["status_at"])}
    if info:
        out["stats"] = rules.evidence(sender, info, sender["total"])
        out["now_would_be"] = {"verdict": verdict, "why": info["why"], "unwanted": info["unwanted"], "spam": info["spam"]}
    base = rules.base_key(sender["key"])
    with store.db() as conn:
        others = conn.execute("SELECT id, label FROM senders WHERE key IN (?, ?, ?) AND id != ?",
                              (base, base + rules.ADS, base + rules.NOTICES, sender["id"])).fetchall()
    out["category"] = rules.CATEGORY_NAMES[rules.category(sender["key"])]
    if others:
        out["their_other_categories"] = [f"s{o['id']} {o['label']}" for o in others]
    out["latest"] = {k: latest.get(k) for k in ("auth", "platform", "content", "transactional", "lookalike")
                     if latest.get(k)}
    out["latest"]["unsubscribe"] = [k for k, v in (latest.get("unsub") or {}).items() if v]
    if rule:
        out["rule"] = f"{rule['kind']} {rule['action']} since {_date(rule['created_at'])}"
    if sender["blocked"]:
        out["blocked"] = sender["blocked"]
    if sender["resubscribe"]:
        out["resubscribe"] = "override asked for: re-subscribe yourself if you want their mail"
    if done:
        out["actions"] = [{"action": a["action"], "status": a["status"], "method": a["method"], "date": _date(a["done_at"]),
                           "auto": bool(a["auto"]), "detail": (a["detail"] or "")[:160]} for a in done]
    return out


def actions_log(status=None, page=1, per=PER_PAGE):
    with store.db() as conn:
        query, params = "SELECT a.*, s.label FROM actions a LEFT JOIN senders s ON s.id = a.sender_id", []
        if status:
            query += " WHERE a.status = ?"
            params.append(status)
        rows = conn.execute(query + " ORDER BY a.id DESC", params).fetchall()
    items = [{"id": f"s{r['sender_id']}", "sender": r["label"], "action": r["action"], "status": r["status"],
              "method": r["method"], "auto": bool(r["auto"]), "date": _date(r["done_at"] or r["queued_at"]),
              "detail": (r["detail"] or "")[:160]} for r in rows]
    return _paged(items, page, per)


def run_capture():
    import logging
    return capture.run(logging.getLogger("unsubscriber"))


def start_analysis():
    import os
    import subprocess
    import sys
    from lib.common import LOG_DIR

    with open(os.path.join(LOG_DIR, "unsub_work.log"), "a") as out:
        subprocess.Popen([sys.executable, "-m", "unsub", "analyze", "--notify"], stdout=out, stderr=out,
                         stdin=subprocess.DEVNULL, cwd=actions.ROOT, start_new_session=True)
    return {"analysing": "in the background; a banner says when the report is ready (then: unsub report)"}


def start_spam_pass():
    import os
    import subprocess
    import sys
    from lib.common import LOG_DIR

    with open(os.path.join(LOG_DIR, "unsub_work.log"), "a") as out:
        subprocess.Popen([sys.executable, "-m", "unsub", "spamfolder", "--notify"], stdout=out, stderr=out,
                         stdin=subprocess.DEVNULL, cwd=actions.ROOT, start_new_session=True)
    return {"reading_spam": "in the background, then the analysis; a banner says when the report is ready"}


# ------------------------------------------------------------ the text form

OPS = {
    # name: (keys, first key, looks only, summary)
    "unsub status": ((), None, True, "the inbox agent: last capture and analysis, mode, pending decisions"),
    "unsub report": (("section", "page", "per", "format"), "section", True,
                     "the latest inbox report's entries, paged (bare, it opens the inbox dashboard instead)"),
    "unsub decide": (VERBS + ("section", "trash"), None, False, "approve or reject inbox report items by sender id"),
    "unsub mode": (("mode",), "mode", False, "switch the inbox agent between temporary and permanent decisions"),
    "unsub promote": (("confirm", "page", "per"), "confirm", False, "turn testing decisions into saved rules"),
    "unsub override": (("sender",), "sender", False, "delete a sender's rule and undo its block"),
    "unsub sender": (("sender",), "sender", True, "one sender's history and stats"),
    "unsub actions": (("status", "page", "per"), "status", True, "the inbox agent's actions log"),
    "unsub capture": ((), None, False, "take the newly polled inbox mail now"),
    "unsub analyze": ((), None, False, "run this week's inbox analysis now"),
    "unsub spamfolder": ((), None, False, "read the Spam folder for senders to unsubscribe from, then analyse"),
}
OTHER_NAMES = {"set_mode": "mode", "sender_info": "sender", "analyse": "analyze", "analysis": "analyze",
               "rules": "promote", "info": "sender", "log": "actions", "action": "actions",
               "spam_folder": "spamfolder", "spampass": "spamfolder", "spam_pass": "spamfolder"}
_CALL = re.compile(r"^(unsub|unsubscribe|unsubscriber|spam)(?:[ _](\w+))?\b\s*(.*)$", re.IGNORECASE | re.DOTALL)


def parse(text):
    """(op, {key: value}) for an `unsub` command, or None if the text is not one. Raises Problem."""
    match = _CALL.match(text.strip())
    if not match:
        return None
    head, verb, rest = match.group(1).lower(), (match.group(2) or "report").lower(), match.group(3)
    op = "unsub " + OTHER_NAMES.get(verb, verb)
    if op not in OPS:
        if head == "unsub":
            raise Problem(f"no unsub {verb}. " + usage())
        return None                       # "unsubscribe me from…", "spam filters are…": not a command
    keys, first_key, _looks, _summary = OPS[op]
    args = {m.group(1).lower(): next(g for g in m.groups()[1:] if g is not None) for m in _ARG.finditer(rest)}
    loose = _ARG.sub(" ", rest).strip().strip("\"'")
    if op == "unsub decide" and loose:    # `unsub decide s3 s5 approve`, `unsub decide block s9`
        words = loose.split()
        verbs = [w.lower() for w in words if w.lower() in VERBS]
        ids = [w for w in words if w.lower() not in VERBS]
        if len(verbs) != 1 or not ids:
            raise Problem("say it as: unsub decide approve=s3,s5 reject=s7 (or unsubscribe= block= keep=)")
        args[verbs[0]] = ",".join(args.get(verbs[0], "").split(",") + ids).strip(",")
    elif loose:
        if not first_key or first_key in args:
            raise Problem(f"{op} does not take {loose!r}. it takes: {', '.join(k + '=' for k in keys) or 'nothing'}")
        args[first_key] = loose
    unknown = [k for k in args if k not in keys]
    if unknown:
        raise Problem(f"{op} has no {unknown[0]}=. it takes: {', '.join(k + '=' for k in keys) or 'nothing'}")
    return op, args


def looks_only(op, args=None):
    if op == "unsub promote":
        return not _yes((args or {}).get("confirm"))
    return OPS[op][2]


def _yes(value):
    return str(value or "").strip().lower() in ("yes", "y", "true", "1", "confirm")


def run(op, args):
    try:
        if op == "unsub status":
            result = status()
        elif op == "unsub report":
            page, per = _paging(args)
            result = report(args.get("section"), page, per, args.get("format", "json"))
            if isinstance(result, str):
                return result
        elif op == "unsub decide":
            choices = {v: args[v] for v in VERBS if args.get(v)}
            if not choices:
                raise Problem("decide what? unsub decide approve=s3,s5 reject=s7")
            trash = _yes(args["trash"]) if "trash" in args else None
            result = decide(choices, args.get("section"), trash)
        elif op == "unsub mode":
            result = set_mode(args.get("mode")) if args.get("mode") else {"mode": analyze.mode()}
        elif op == "unsub promote":
            page, per = _paging(args)
            result = promote(_yes(args.get("confirm")), page, per)
        elif op == "unsub override":
            result = override(args.get("sender"))
        elif op == "unsub sender":
            result = sender_info(args.get("sender"))
        elif op == "unsub actions":
            page, per = _paging(args)
            result = actions_log(args.get("status"), page, per)
        elif op == "unsub capture":
            result = run_capture()
        elif op == "unsub analyze":
            result = start_analysis()
        elif op == "unsub spamfolder":
            result = start_spam_pass()
        else:
            raise Problem(f"no operation {op}")
    except Problem as problem:
        return str(problem)
    except auth.NotSignedIn as error:
        return as_json({"error": str(error)})
    return as_json(result)


def call(text):
    try:
        parsed = parse(text)
    except Problem as problem:
        return str(problem)
    if parsed is None:
        return "not an unsub command. " + usage()
    return run(*parsed)


def usage():
    return "commands: " + " · ".join(f"{op} {' '.join(k + '=' for k in spec[0])}".strip() for op, spec in OPS.items())
