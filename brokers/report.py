"""The weekly report, and the live board the dashboard and the commands read.

Groups (each broker is one entry; the dashboard shows them as collapsible sections):

    approval     Needs approval: new targets, and anything that needs approving again
    auto         Done by the agent this week: what it sent or submitted for you, and what it did on its own
                 under standing approvals (follow-ups, confirmations, re-submissions), each with an override
    manual       Manual tasks: CAPTCHAs, ID checks, phone calls, possible matches, unclear replies
    waiting      Waiting for an answer: sent (by the agent or by you), no answer yet
    refused      Refused or no response, with a complaint draft for each
    removed      Removed: the broker (or its page, when you did it by hand) says so, or a search verified it
    maintenance  Registry maintenance: broken selectors, bounced emails
    oneoffs      One-time federal and industry opt-outs, until each is ticked off

At the top: brokers found with listings, verified removed, in progress, refused, manual tasks
waiting, and how each changed since last week's report.
"""
import json
import time

from brokers import OPEN_DASHBOARD, held, plan, registry, scan, templates, vault

GROUPS = ("approval", "auto", "manual", "waiting", "refused", "removed", "maintenance", "oneoffs")
TITLES = {"approval": "Needs approval", "auto": "Done by the agent", "manual": "Manual tasks", "waiting": "Waiting for an answer",
          "refused": "Refused or no response", "removed": "Removed", "maintenance": "Registry maintenance",
          "oneoffs": "One-time opt-outs"}
IN_PROGRESS = ("approved", "submitted", "awaiting_confirmation", "still_listed", "relisted",
               "resubmitted")
ONEOFFS = [
    {"key": "optoutprescreen", "name": "OptOutPrescreen", "url": "https://www.optoutprescreen.com/",
     "what": "Stops prescreened credit and insurance offers (the credit bureaus' marketing lists).",
     "steps": "Opt out for 5 years online, or for good by mailing the signed form. It asks for your SSN and date of "
              "birth, which the agent never handles: do it yourself."},
    {"key": "donotcall", "name": "National Do Not Call Registry", "url": "https://www.donotcall.gov/register",
     "what": "Telemarketing calls to your phone numbers.", "steps": "Register each number; confirm by the email link."},
    {"key": "dmachoice", "name": "DMAchoice", "url": "https://www.dmachoice.org/",
     "what": "Direct mail from the ANA's member companies (catalogs, offers).",
     "steps": "Register and pick the categories to stop. It charges a small fee for 10 years."},
    {"key": "google_results", "name": "Google: Results about you", "url": "https://myactivity.google.com/results-about-you",
     "what": "Search results that show your address, phone or email.",
     "steps": "Sign in, let it monitor your details, and ask for removal of each result it finds."},
]
TASK_KINDS = {"captcha": "CAPTCHA", "id": "ID check", "phone": "phone call", "account": "account needed",
              "possible_match": "is this you?", "reply": "unclear reply", "search": "search yourself",
              "confirm": "confirm by link", "bounced": "email bounced", "details": "details missing",
              "payment": "payment asked", "residency": "residency question", "field": "unapproved field",
              "no-form": "no form found", "failed": "failed", "form": "web form"}


def _week_ago():
    return time.time() - 7 * 86400


def _date(t):
    return time.strftime("%Y-%m-%d", time.localtime(t)) if t else None


def _shown(entry, found, conn, broker_id):
    """What the broker has on you: listings, an access-request reply, or its filed data categories."""
    listings = [dict(r) for r in conn.execute("SELECT id, url, fields, score, verdict, found_at FROM listings WHERE "
                                              "broker_id = ? AND gone_at IS NULL ORDER BY score DESC", (broker_id,))]
    for listing in listings:
        listing["fields"] = json.loads(listing["fields"] or "{}")
    access = conn.execute("SELECT text, received_at FROM replies WHERE broker_id = ? AND label IN ('completed', "
                          "'no_record', 'unclear') AND length(text) > 400 ORDER BY received_at DESC LIMIT 1",
                          (broker_id,)).fetchone()
    return {"listings": listings, "data_categories": registry.get(entry, "data_categories", []),
            "access_reply": access["text"][:4000] if access else None, "files": held.files(conn, broker_id)}


def entry_for(conn, broker_id, row, entry, found, full, real=None):
    """One broker as the board shows it. full: actual values (the dashboard); otherwise masked (the commands)."""
    out = {"id": broker_id, "name": entry.get("name", broker_id), "category": registry.get(entry, "category"),
           "parent": registry.get(entry, "parent_company", None), "tier": row["tier"], "state": row["state"],
           "listed": row["listed"], "summary": registry.get(entry, "summary", None),
           "website": entry.get("website"), "since": _date(row["state_at"]), "detail": row["detail"],
           "todo": registry.todo_fields(entry)}
    if row["broken"]:
        out["broken"] = row["broken"]
    if row["pending"]:
        out["pending"] = json.loads(row["pending"])
    if row["approved"]:
        out["approved"] = json.loads(row["approved"])
    shown = _shown(entry, found, conn, broker_id)
    out["shown"] = shown if full else {"listings": len(shown["listings"]), "data_categories": shown["data_categories"],
                                       "files": [{k: v for k, v in f.items() if k != "values"} for f in shown["files"]]}
    proposal = plan.propose(broker_id, entry, row, found, scan.listing_urls(conn, broker_id))
    if row.get("pending") and json.loads(row["pending"]).get("asked"):
        asked = json.loads(row["pending"])["asked"]
        proposal["elements"] = sorted(set(proposal["elements"]) | set(asked))
    if real is None:
        from brokers import send
        real = send._real_email()
    values = vault.element_values(found, real, scan.listing_urls(conn, broker_id))
    proposal["values"] = {e: (values.get(e, "(missing)") if full else vault.mask(e, values.get(e)))
                          for e in proposal["elements"]}
    out["proposal"] = proposal
    return out


REPLY_TASKS = ("confirm", "reply", "id", "form")     # tasks whose ref is a reply


def _email_link(conn, task):
    """The broker's email behind a task, in Gmail (filed under the Data brokers label, out of the inbox)."""
    if task["kind"] not in REPLY_TASKS or not task["ref"]:
        return None
    row = conn.execute("SELECT message_id, thread_id FROM replies WHERE id = ?", (task["ref"],)).fetchone()
    if not row:
        return None
    from urllib.parse import quote
    from brokers import send
    return f"https://mail.google.com/mail/?authuser={quote(send._real_email())}#all/{row['thread_id'] or row['message_id']}"


def _history(conn, broker_id, limit=40):
    return [{"at": _date(r["at"]), "event": r["event"], "detail": r["detail"]}
            for r in conn.execute("SELECT * FROM history WHERE broker_id = ? ORDER BY id DESC LIMIT ?", (broker_id, limit))]


def totals(conn):
    states = {r["state"]: r["n"] for r in conn.execute("SELECT state, COUNT(*) AS n FROM brokers GROUP BY state")}
    return {"listed": conn.execute("SELECT COUNT(DISTINCT broker_id) FROM listings WHERE verdict IN ('match', "
                                   "'confirmed')").fetchone()[0],
            "removed": states.get("verified_removed", 0) + states.get("acknowledged", 0),
            "verified": states.get("verified_removed", 0),
            "in_progress": sum(states.get(s, 0) for s in IN_PROGRESS),
            "refused": states.get("refused", 0) + states.get("no_response", 0),
            "manual": conn.execute("SELECT COUNT(*) FROM tasks WHERE done_at IS NULL").fetchone()[0],
            "brokers": sum(states.values())}


def board(full=True, group=None):
    """{"totals", "changes", "groups": {group: [entries]}, "mode", "ready", "searching"}."""
    found = vault.identity()
    entries = registry.load()
    rows = plan.ensure_rows() if vault.has_identity(found) else {}
    from brokers import send
    real = send._real_email()
    with vault.db() as conn:
        groups = {g: [] for g in GROUPS}
        week = _week_ago()
        unsearched = 0
        tasks = [dict(t) for t in conn.execute("SELECT * FROM tasks WHERE done_at IS NULL ORDER BY created_at")]
        held = conn.execute("SELECT value FROM kv WHERE key = 'skipped_until'").fetchone()
        held = json.loads(held["value"]) if held else {}            # ops._skip: off the board until then
        now = time.time()
        for broker_id, row in sorted(rows.items(), key=lambda kv: (kv[1]["tier"], kv[0])):
            entry = entries.get(broker_id)
            if not entry:
                continue
            state = row["state"]
            wants = None
            if row["pending"]:
                wants = "approval"
            elif state in ("discovered", "scanned", "planned") and not row["approved"]:
                if scan.searchable(entry) and not row["scanned_at"] and not row["rejected_at"]:
                    unsearched += 1
                    continue
                if row["listed"] == "not_listed" or row["rejected_at"] or held.get(broker_id, 0) > now:
                    continue
                wants = "approval"
            elif state in ("refused", "no_response"):
                wants = "refused"
            elif state in ("verified_removed", "acknowledged"):
                wants = "removed"
            elif state in ("submitted", "resubmitted", "awaiting_confirmation", "still_listed", "relisted"):
                wants = "waiting"
            if row["broken"]:
                groups["maintenance"].append(_brief(broker_id, row, entry, row["broken"]))
            if wants is None:
                continue
            if group and wants != group:
                continue
            item = entry_for(conn, broker_id, row, entry, found, full, real)
            if wants == "refused":
                item["complaint"] = complaint(conn, broker_id, row, entry, found) if full else "ready"
            groups[wants].append(item)
        for request in conn.execute("SELECT * FROM requests WHERE auto = 1 AND queued_at > ? ORDER BY queued_at DESC",
                                    (week,)):
            entry = entries.get(request["broker_id"], {})
            groups["auto"].append({"id": request["broker_id"], "name": entry.get("name", request["broker_id"]),
                                   "request": request["id"], "kind": request["kind"], "status": request["status"],
                                   "method": request["method"], "at": _date(request["queued_at"]),
                                   "detail": request["detail"]})
        for request in conn.execute("SELECT * FROM requests WHERE auto = 0 AND method != 'hand' AND sent_at > ? AND "
                                    "status IN ('sent', 'awaiting', 'acknowledged') ORDER BY sent_at DESC", (week,)):
            entry = entries.get(request["broker_id"], {})
            how = f"emailed {request['to_address']}" if request["method"] == "email" else "submitted their opt-out form"
            groups["auto"].append({"id": request["broker_id"], "name": entry.get("name", request["broker_id"]),
                                   "request": request["id"], "kind": request["kind"],
                                   "status": "waiting for their email link" if request["status"] == "awaiting"
                                   else request["status"], "method": request["method"],
                                   "at": _date(request["sent_at"]), "detail": f"{how} (you approved it)"})
        for event in conn.execute("SELECT * FROM history WHERE at > ? AND event LIKE 'reply:%' AND detail LIKE "
                                  "'auto: opened the confirmation link%' ORDER BY at DESC", (week,)):
            entry = entries.get(event["broker_id"], {})
            groups["auto"].append({"id": event["broker_id"], "name": entry.get("name", event["broker_id"]),
                                   "kind": "confirmation", "status": "done", "at": _date(event["at"]),
                                   "detail": event["detail"]})
        for task in tasks:
            entry = entries.get(task["broker_id"], {})
            groups["manual"].append({"id": task["broker_id"], "name": entry.get("name", task["broker_id"]),
                                     "task": task["id"], "kind": task["kind"], "what": TASK_KINDS.get(task["kind"],
                                                                                                       task["kind"]),
                                     "title": task["title"], "steps": json.loads(task["steps"] or "[]"),
                                     "link": task["link"], "email": _email_link(conn, task),
                                     "since": _date(task["created_at"]),
                                     "listing": _listing(conn, task) if task["kind"] == "possible_match" else None})
        done = {r["key"] for r in conn.execute("SELECT key FROM oneoffs WHERE done_at IS NOT NULL")}
        groups["oneoffs"] = [{**o, "id": o["key"]} for o in ONEOFFS if o["key"] not in done]
        now_totals = totals(conn)
    last = _last_summary()
    changes = {k: now_totals[k] - last["totals"].get(k, 0) for k in now_totals} if last else {}
    return {"totals": now_totals, "changes": changes, "groups": groups if not group else {group: groups[group]},
            "mode": plan.approval_mode(), "ready": vault.has_identity(found), "unsearched": unsearched,
            "last_report": last.get("date") if last else None}


def _listing(conn, task):
    row = conn.execute("SELECT url, fields, score FROM listings WHERE id = ?", (task["ref"] or 0,)).fetchone()
    return {"url": row["url"], "fields": json.loads(row["fields"] or "{}"), "score": row["score"]} if row else None


def _brief(broker_id, row, entry, why):
    return {"id": broker_id, "name": entry.get("name", broker_id), "state": row["state"], "detail": why,
            "website": entry.get("website"), "file": f"brokers/registry/{broker_id}.yaml"}


def _last_summary():
    with vault.db() as conn:
        row = conn.execute("SELECT * FROM reports ORDER BY id DESC LIMIT 1").fetchone()
    if not row:
        return None
    return {**json.loads(row["summary"]), "id": row["id"], "date": _date(row["created_at"])}


def complaint(conn, broker_id, row, entry, found):
    """A complaint draft for you to file: the FTC, and your state's attorney general when config.json
    jurisdiction.regulators has a page for the state you live in (plan.home_states)."""
    cfg = plan.config()
    states = plan.home_states(cfg, found)
    regulators = cfg["jurisdiction"]["regulators"]
    to = "Federal Trade Commission (" + regulators["FTC"] + ")"
    for state in states:
        if state in regulators:
            to += f"; {state} Attorney General ({regulators[state]})"
    timeline = []
    for request in conn.execute("SELECT * FROM requests WHERE broker_id = ? AND sent_at IS NOT NULL ORDER BY sent_at",
                                (broker_id,)):
        timeline.append(f"{_date(request['sent_at'])}: {request['kind']} request by {request['method']}"
                        + (f" to {request['to_address']}" if request["to_address"] else ""))
    for reply in conn.execute("SELECT * FROM replies WHERE broker_id = ? ORDER BY received_at", (broker_id,)):
        timeline.append(f"{_date(reply['received_at'])}: reply from {reply['sender']}: {reply['label'] or 'unread'}")
    ledger, seen = [], set()
    for r in conn.execute("SELECT * FROM ledger WHERE broker_id = ? ORDER BY sent_at", (broker_id,)):
        if (r["element"], r["value"]) not in seen:          # each detail once, with the first time it was sent
            seen.add((r["element"], r["value"]))
            ledger.append(f"{templates.LABELS.get(r['element'], r['element'])}: {r['value']} (first sent "
                          f"{_date(r['sent_at'])})")
    days = cfg["no_response_days"]
    name = f"{found.get('first_name', '')} {found.get('last_name', '')}".strip()
    return templates.complaint(to, entry.get("name", broker_id), entry.get("website"), entry.get("opt_out_url"),
                               row["state"] if row["state"] in templates.FAILURES else "refused", timeline, ledger,
                               states[0] if states else "the US",
                               name, days)


def summary_line(data=None):
    data = data or board(full=False)
    if not data["ready"]:
        return "broker dashboard open: add your details to start"
    t, waiting = data["totals"], len(data["groups"]["approval"])
    parts = [f"{waiting} to approve" if waiting else "nothing to approve", f"{t['manual']} manual",
             f"{t['in_progress']} in progress", f"{t['removed']} removed"]
    return "brokers: " + " · ".join(parts)


def build(notify=False):
    """The weekly report: a snapshot of the board's counts and items, and a banner."""
    data = board(full=False)
    summary = {"totals": data["totals"], "changes": data["changes"],
               "groups": {g: len(items) for g, items in data["groups"].items()}}
    with vault.db() as conn:
        report_id = conn.execute("INSERT INTO reports (created_at, summary) VALUES (?, ?)",
                                 (time.time(), json.dumps(summary))).lastrowid
        for group, items in data["groups"].items():
            for item in items:
                conn.execute("INSERT INTO items (report_id, grp, broker_id, task_id, proposal) VALUES (?, ?, ?, ?, ?)",
                             (report_id, group, item.get("id"), item.get("task"),
                              json.dumps(item.get("proposal") or item.get("kind") or "")))
    vault.put("last_report_at", time.time())
    text = banner_text(data)
    if notify:
        from lib.common import banner
        banner("Broker report", text, post=OPEN_DASHBOARD)
    return {"report": report_id, **summary, "text": text}


def banner_text(data):
    t, c = data["totals"], data["changes"]
    sign = lambda k: f" ({c[k]:+d})" if c.get(k) else ""
    return (f"{len(data['groups']['approval'])} to approve · {t['manual']} manual tasks{sign('manual')}\n"
            f"{t['removed']} removed{sign('removed')} · {t['in_progress']} in progress · {t['refused']} refused or silent")
