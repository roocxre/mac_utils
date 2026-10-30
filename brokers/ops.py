r"""The broker agent's commands, at the assistant's prompt (atlas/mail_commands.py) and in a terminal
(`python -m brokers`). Answers are compact JSON with every list paged. Details in them are masked
(`ja…@example.com`, `…1234`), and your details can only be entered in the dashboard.

    brokers status                         totals by state, queue, last and next pass, approval mode
    brokers report group=approval page=2   the board, one group (approval auto manual refused removed
                                           maintenance oneoffs); bare, it opens the broker dashboard
    brokers decide approve=spokeo,whitepages      approve or reject by broker id
    brokers decide approve=all group=approval filter=method:email,category:people-search
    brokers mode standing|per_request      the approval-mode switch
    brokers info spokeo                    registry entry, what it holds on you, history, ledger
    brokers scan spokeo                    search a site for your listing now (bare: the next one due)
    brokers done task=12 outcome=done      mark a manual task complete (outcome: done, listed, not_listed,
                                           yes, no, completed, refused, no_record, skip); task=dmachoice
                                           ticks off a one-time opt-out
    brokers override spokeo                cancel its automatic actions and withdraw its standing approval
    brokers ledger broker=spokeo element=dob     which details went to whom
    brokers validate strict=yes            the registry validator
    brokers run                            one pass of the agent now

Underscore names work too: set_mode, broker_info, manual_done, registry_validate…
"""
import json
import os
import re
import subprocess
import time

from brokers import plan, registry, replies, report, scan, send, vault
from mail import ops as mail_ops

PER_PAGE = 10
MAX_PER_PAGE = 50
GROUPS = report.GROUPS
_ARG = re.compile(r"""(\w+)\s*=\s*(?:"([^"]*)"|'([^']*)'|(\S+))""")

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


def _ago(t):
    if not t:
        return None
    seconds = time.time() - t
    return f"{round(seconds / 60)} min" if seconds < 7200 else f"{round(seconds / 3600, 1)} h" if seconds < 172800 \
        else f"{round(seconds / 86400)} d"


def _ready():
    if not vault.has_identity():
        raise Problem("the broker agent has no details yet: add your name and current address in the broker "
                      "dashboard (say \"broker report\", then Your details)")


# ------------------------------------------------------------ looking

def status():
    with vault.db() as conn:
        states = {r["state"]: r["n"] for r in conn.execute("SELECT state, COUNT(*) AS n FROM brokers GROUP BY state")}
        queue = {r["method"]: r["n"] for r in conn.execute("SELECT method, COUNT(*) AS n FROM requests WHERE "
                                                           "status = 'queued' GROUP BY method")}
        sent_today = send._today_sent(conn)
        tasks = conn.execute("SELECT COUNT(*) FROM tasks WHERE done_at IS NULL").fetchone()[0]
    last = vault.get("last_pass_at")
    cfg = plan.config()
    return {"ready": vault.has_identity(), "mode": plan.approval_mode(), "states": states, "queue": queue,
            "emails_today": f"{sent_today}/{cfg['sends_per_day']}", "manual_tasks": tasks,
            "last_pass_ago": _ago(last), "next_pass": "within 10 min" if last else "when the agent is installed",
            "last_pass": vault.get("last_pass"), "registry": len(registry.load()),
            "jurisdiction": plan.home_states(cfg), "jurisdiction_checked": cfg["jurisdiction"].get("checked") or None}


def _compact(item, group):
    keep = {"approval": ("id", "name", "category", "tier", "state", "listed", "pending"),
            "auto": ("id", "name", "kind", "status", "method", "at", "detail", "request"),
            "manual": ("id", "name", "task", "what", "title", "link", "since"),
            "refused": ("id", "name", "state", "since", "detail"),
            "removed": ("id", "name", "since", "state"), "waiting": ("id", "name", "state", "since", "detail"), "maintenance": ("id", "name", "detail", "file"),
            "oneoffs": ("id", "name", "url", "what")}[group]
    out = {k: item[k] for k in keep if item.get(k) not in (None, "", [])}
    if group == "approval" and item.get("proposal"):
        p = item["proposal"]
        out["send"] = {"method": p["method"], "details": p["values"]} if not p.get("manual") else \
            {"method": "manual", "why": p["manual"]}
        if p.get("missing"):
            out["missing"] = p["missing"]
    return out


def report_view(group=None, page=1, per=PER_PAGE):
    data = report.board(full=False, group=group if group in GROUPS else None)
    if group and group not in GROUPS:
        raise Problem(f"group is one of {', '.join(GROUPS)}")
    out = {"totals": data["totals"], "changes": data["changes"], "mode": data["mode"], "ready": data["ready"],
           "counts": {g: len(items) for g, items in data["groups"].items()}}
    show = group or next((g for g in GROUPS if data["groups"].get(g)), "approval")
    out["group"] = show
    out.update(_paged([_compact(i, show) for i in data["groups"].get(show, [])], page, per))
    return out


def broker_info(ref):
    broker_id = _broker(ref)
    entry = registry.broker(broker_id)
    found = vault.identity()
    rows = plan.ensure_rows() if vault.has_identity(found) else {}
    out = {"registry": {k: v for k, v in entry.items() if k not in ("notes", "selectors")},
           "todo_fields": registry.todo_fields(entry)}
    if broker_id in rows:
        with vault.db() as conn:
            row = dict(conn.execute("SELECT * FROM brokers WHERE id = ?", (broker_id,)).fetchone())
            item = report.entry_for(conn, broker_id, row, entry, found, full=False)
            out.update(state=item["state"], listed=item["listed"], tier=item["tier"], holds=item["shown"],
                       proposal={k: item["proposal"][k] for k in ("method", "values", "missing", "manual")
                                 if item["proposal"].get(k)},
                       approved=item.get("approved"), pending=item.get("pending"),
                       history=report._history(conn, broker_id, 15))
    rows_, total = vault.ledger(broker_id, limit=20)
    out["ledger"] = {"total": total, "items": [{"element": r["element"], "value": vault.mask(r["element"], r["value"]),
                                                "method": r["method"], "sent": report._date(r["sent_at"])} for r in rows_]}
    return out


def _broker(ref):
    ref = (ref or "").strip().lower()
    if not ref:
        raise Problem("which broker? give its id (spokeo) or website")
    entries = registry.load()
    if ref in entries:
        return ref
    from lib import psl
    domain = psl.registrable_domain(ref)
    by_domain = registry.by_domain()
    if domain in by_domain:
        return by_domain[domain]
    named = [b for b, e in entries.items() if str(e.get("name", "")).lower() == ref]
    if named:
        return named[0]
    close = [b for b in entries if ref in b][:5]
    raise Problem(f"no broker {ref}" + (f". close: {', '.join(close)}" if close else ""))


def ledger_view(broker=None, element=None, page=1, per=PER_PAGE):
    broker_id = _broker(broker) if broker else None
    rows, total = vault.ledger(broker_id, element, (page - 1) * per, per)
    items = [{"broker": r["broker_id"], "element": r["element"], "value": vault.mask(r["element"], r["value"]),
              "method": r["method"], "sent": report._date(r["sent_at"])} for r in rows]
    return {"items": items, "page": page, "pages": max(1, -(-total // per)), "total": total}


# ------------------------------------------------------------ deciding

def _select(value, group, filters, data=None):
    """Broker ids from approve=a,b or approve=all (with group= and filter=key:value,…)."""
    words = [w.strip().lower() for w in value.split(",") if w.strip()]
    if words != ["all"]:
        return [_broker(w) for w in words]
    data = data or report.board(full=False, group=group or "approval")
    items = data["groups"].get(group or "approval", [])
    wanted = {}
    for part in (filters or "").split(","):
        if ":" in part:
            key, val = part.split(":", 1)
            wanted.setdefault(key.strip().lower(), set()).add(val.strip().lower())

    def keep(item):
        for key, values in wanted.items():
            actual = str(item.get("proposal", {}).get("method") if key == "method" else item.get(key, "")).lower()
            if actual not in values:
                return False
        return True

    return [i["id"] for i in items if keep(i)]


def decide(approve=(), reject=(), mode=None):
    """Approve or reject brokers (their proposal, or what is pending for them). Returns what happened."""
    _ready()
    found = vault.identity()
    plan.ensure_rows()
    done, errors, later = [], [], []
    for broker_id in approve:
        entry = registry.broker(broker_id)
        with vault.db() as conn:
            row = dict(conn.execute("SELECT * FROM brokers WHERE id = ?", (broker_id,)).fetchone() or {})
            if not row:
                errors.append(f"{broker_id}: not in play (defunct?)")
                continue
            pending = json.loads(row["pending"]) if row.get("pending") else None
            if pending:
                result = _approve_pending(conn, broker_id, row, entry, pending, found, later)
            else:
                result = _approve_new(conn, broker_id, row, entry, found)
            conn.execute("UPDATE brokers SET rejected_at = NULL WHERE id = ?", (broker_id,))
            conn.execute("UPDATE items SET status = 'approved', decided_at = ? WHERE broker_id = ? AND status = 'pending'",
                         (time.time(), broker_id))
        (errors if result.startswith("!") else done).append(f"{broker_id}: {result.lstrip('!')}")
    for broker_id in reject:
        with vault.db() as conn:
            conn.execute("UPDATE brokers SET rejected_at = ?, pending = NULL WHERE id = ?", (time.time(), broker_id))
            conn.execute("UPDATE requests SET status = 'cancelled', detail = 'you said no' WHERE broker_id = ? AND "
                         "status = 'queued'", (broker_id,))
            conn.execute("UPDATE items SET status = 'rejected', decided_at = ? WHERE broker_id = ? AND status = 'pending'",
                         (time.time(), broker_id))
            vault.history(conn, broker_id, "you said no", "")
        done.append(f"{broker_id}: left alone")
    for job in later:                  # confirmation links: a browser, after the database is free
        done.append(job())
    return {"done": done, "errors": errors}


def _approve_new(conn, broker_id, row, entry, found):
    proposal = plan.propose(broker_id, entry, row, found, scan.listing_urls(conn, broker_id))
    if proposal["missing"]:
        return f"!add your {', '.join(proposal['missing'])} in Your details first"
    if proposal.get("manual"):
        reason = proposal["manual"]
        steps = {"id": "They ask for ID. Redact everything except your name and photo if you send it.",
                 "phone": "They verify by phone: you'll get a call with a code.",
                 "account": "They want an account; the agent never makes one.",
                 "no-method": "There's no automatic way to ask them."}.get(reason, reason)
        plan.approve(conn, broker_id, proposal)
        plan.set_state(conn, broker_id, "needs_manual", steps)
        scan.open_task(conn, broker_id, reason if reason != "no-method" else "form", f"Opt out at {entry['name']}",
                       [steps, "Give them only: " + ", ".join(proposal["elements"]) + ".",
                        "Mark this done when you have."], proposal.get("url"))
        return "manual task made"
    plan.approve(conn, broker_id, proposal)
    send.queue(conn, broker_id, "deletion")
    return f"queued: {proposal['method']} with {', '.join(proposal['elements'])}"


def _approve_pending(conn, broker_id, row, entry, pending, found, later):
    approved = json.loads(row["approved"]) if row.get("approved") else None
    kind = pending["kind"]
    if kind == "more-info":
        if pending.get("grows") and approved:
            approved["elements"] = sorted(set(approved["elements"]) | set(pending["asked"]))
            conn.execute("UPDATE brokers SET approved = ? WHERE id = ?", (json.dumps(approved), broker_id))
            vault.history(conn, broker_id, "approval widened", ", ".join(pending["asked"]))
        request = replies._open_request(conn, broker_id)
        replies.queue_more_info(conn, broker_id, pending, request, auto=False)
        return f"answering with {', '.join(pending['asked'])}"
    if kind in ("follow-up", "resubmit"):
        if not approved:
            plan.set_pending(conn, broker_id, None)
            return _approve_new(conn, broker_id, row, entry, found)
        if pending.get("grows") and pending.get("asked"):         # their form needs more of your details
            approved["elements"] = sorted(set(approved["elements"]) | set(pending["asked"]))
            conn.execute("UPDATE brokers SET approved = ? WHERE id = ?", (json.dumps(approved), broker_id))
            vault.history(conn, broker_id, "approval widened", ", ".join(pending["asked"]))
        send.queue(conn, broker_id, kind)
        plan.set_pending(conn, broker_id, None)
        return f"queued: {kind}"
    if kind == "confirm":
        url, reply_id = pending["url"], pending.get("reply")
        later.append(lambda: f"{broker_id}: " + replies.confirm(broker_id, entry, url, reply_id, plan.config()))
        return "opening the confirmation link"
    return f"!don't know how to approve {kind}"


def override(ref):
    """Cancel a broker's automatic actions and withdraw its standing approval."""
    broker_id = _broker(ref)
    with vault.db() as conn:
        cancelled = conn.execute("UPDATE requests SET status = 'cancelled', detail = 'overridden' WHERE broker_id = ? "
                                 "AND status = 'queued'", (broker_id,)).rowcount
        conn.execute("UPDATE brokers SET approved = NULL, pending = NULL WHERE id = ?", (broker_id,))
        vault.history(conn, broker_id, "override", f"standing approval withdrawn; {cancelled} queued cancelled")
    return {"broker": broker_id, "cancelled": cancelled,
            "note": "anything more for this broker will ask you first; what was already sent can't be unsent"}


def set_mode(mode):
    mode = (mode or "").strip().lower().replace("-", "_")
    if mode in ("perrequest", "per", "request", "off"):
        mode = "per_request"
    if mode in ("on",):
        mode = "standing"
    if mode not in ("standing", "per_request"):
        raise Problem("mode is standing or per_request")
    vault.put("approval_mode", mode)
    return {"mode": mode}


# Tasks that ask you to do the opt-out yourself, and what you can say happened.
ACTION_TASKS = ("captcha", "id", "phone", "account", "form", "no-form", "field", "residency", "payment", "confirm",
                "failed", "bounced")
OUTCOMES = {"done": "submitted", "submitted": "submitted", "waiting": "submitted", "sent": "submitted",
            "removed": "removed", "success": "removed", "completed": "removed", "confirmed": "removed",
            "rejected": "rejected", "refused": "rejected", "denied": "rejected"}


def _record_outcome(conn, broker_id, entry, outcome, cfg):
    """What happened with an opt-out you did by hand (or an answer you got outside email).

    submitted  you sent it; nothing back yet. Waiting for an answer, with the follow-up timers.
    removed    the site says it's done. Removed ("they say so"); a site with a search is searched
               again in 30-45 days to verify.
    rejected   the site refused. Refused, with a complaint draft.
    The broker's queued requests are cancelled either way: you did what they were going to do."""
    plan.cancel_requests(conn, broker_id, "you did it by hand")
    now = time.time()
    if not conn.execute("SELECT 1 FROM requests WHERE broker_id = ? AND method = 'hand' AND status IN ('sent', "
                        "'acknowledged', 'refused')", (broker_id,)).fetchone():
        conn.execute("INSERT INTO requests (broker_id, kind, method, elements, status, queued_at, sent_at, detail) "
                     "VALUES (?, 'deletion', 'hand', '[]', 'sent', ?, ?, 'you did it by hand')", (broker_id, now, now))
    if outcome == "removed":
        replies._acknowledge(conn, broker_id, entry, cfg, "you did it by hand; the site says it's done")
        conn.execute("UPDATE requests SET status = 'acknowledged' WHERE broker_id = ? AND method = 'hand'", (broker_id,))
        return "removed (they say so)"
    if outcome == "rejected":
        plan.set_state(conn, broker_id, "refused", "you did it by hand; the site refused")
        conn.execute("UPDATE requests SET status = 'refused' WHERE broker_id = ? AND method = 'hand'", (broker_id,))
        return "rejected"
    plan.set_state(conn, broker_id, "submitted", "you did it by hand; waiting for their answer")
    conn.execute("UPDATE requests SET status = 'sent', sent_at = ? WHERE broker_id = ? AND method = 'hand'",
                 (now, broker_id))
    conn.execute("UPDATE brokers SET follow_up_at = ?, followed_up = 0, acknowledged_at = NULL, verify_at = NULL "
                 "WHERE id = ?", (now + cfg["follow_up_days"] * 86400, broker_id))
    return "submitted, waiting for their answer"


def mark(ref, outcome):
    """Say what happened with a broker, after its task is closed: submitted, removed or rejected."""
    broker_id = _broker(ref)
    key = OUTCOMES.get((outcome or "").strip().lower())
    if not key:
        raise Problem("outcome is submitted, removed or rejected")
    with vault.db() as conn:
        note = _record_outcome(conn, broker_id, registry.broker(broker_id) or {}, key, plan.config())
        vault.history(conn, broker_id, "you marked it", note)
    return {"broker": broker_id, "outcome": note}


def _skip(conn, broker_id, entry, cfg):
    """Skipped: close the broker's open tasks without saying what happened. Nothing is recorded as
    sent, removed or refused, so the broker isn't let off: one stuck on a manual step goes back to
    the start, unapproved and off the board until its next search (or, with no search, the same
    wait), and a pass that finds it holding your data asks again."""
    now = time.time()
    until = now + registry.get(entry, "recheck_days", cfg["recheck_days"]) * 86400
    conn.execute("UPDATE tasks SET done_at = ?, outcome = 'skipped' WHERE broker_id = ? AND done_at IS NULL",
                 (now, broker_id))
    row = conn.execute("SELECT state FROM brokers WHERE id = ?", (broker_id,)).fetchone()
    if row and row["state"] == "needs_manual":
        plan.set_state(conn, broker_id, "scanned" if scan.searchable(entry) else "planned",
                       "you skipped the manual step; asked again after the next search")
        conn.execute("UPDATE brokers SET approved = NULL, approved_at = NULL, next_scan_at = ? WHERE id = ?",
                     (until, broker_id))
        held = conn.execute("SELECT value FROM kv WHERE key = 'skipped_until'").fetchone()
        held = json.loads(held["value"]) if held else {}
        held[broker_id] = until
        conn.execute("INSERT OR REPLACE INTO kv (key, value) VALUES ('skipped_until', ?)", (json.dumps(held),))
    return "skipped"


def manual_done(task, outcome="done"):
    """Close a manual task with what happened (or tick off a one-time opt-out). outcome=skipped
    closes it (and the broker's other open tasks) without letting the broker off (_skip)."""
    outcome = (outcome or "done").strip().lower().replace("-", "_")
    keys = {o["key"] for o in report.ONEOFFS}
    if str(task) in keys:
        with vault.db() as conn:
            conn.execute("INSERT OR REPLACE INTO oneoffs VALUES (?, ?)", (task, time.time()))
        return {"oneoff": task, "done": True}
    try:
        task_id = int(str(task).lstrip("t#"))
    except ValueError:
        raise Problem(f"task is a number (from the manual group) or one of {', '.join(sorted(keys))}")
    cfg = plan.config()
    with vault.db() as conn:
        row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        if row is None:
            raise Problem(f"no task {task_id}")
        broker_id, kind = row["broker_id"], row["kind"]
        entry = registry.broker(broker_id) or {}
        note = outcome
        if outcome == "skipped":
            note = _skip(conn, broker_id, entry, cfg)
            vault.history(conn, broker_id, f"task skipped: {kind}", note)
            return {"task": task_id, "broker": broker_id, "outcome": note}
        if kind == "possible_match":
            verdict = "confirmed" if outcome in ("yes", "me", "confirmed", "done", "listed") else "rejected"
            conn.execute("UPDATE listings SET verdict = ? WHERE id = ?", (verdict, row["ref"]))
            if verdict == "confirmed":
                conn.execute("UPDATE brokers SET listed = 'listed' WHERE id = ?", (broker_id,))
            note = "it's you" if verdict == "confirmed" else "not you"
        elif kind == "search":
            if outcome in ("listed", "not_listed"):
                conn.execute("UPDATE brokers SET listed = ? WHERE id = ?", (outcome, broker_id))
        elif kind == "reply" and outcome in ("completed", "refused", "no_record", "needs_id"):
            label = {"refused": "refused_other"}.get(outcome, outcome)
            reply = conn.execute("SELECT * FROM replies WHERE id = ?", (row["ref"] or 0,)).fetchone()
            if reply:
                conn.execute("UPDATE replies SET label = ?, handled = 'you sorted it' WHERE id = ?", (label, reply["id"]))
            if label == "completed":
                replies._acknowledge(conn, broker_id, entry, cfg, "you read their reply: done")
            elif label == "refused_other":
                plan.set_state(conn, broker_id, "refused", "you read their reply: refused")
                plan.cancel_requests(conn, broker_id, "they refused")
            elif label == "no_record":
                plan.set_state(conn, broker_id, "verified_removed", "you read their reply: no record")
        elif kind in ACTION_TASKS and outcome in OUTCOMES:
            note = _record_outcome(conn, broker_id, entry, OUTCOMES[outcome], cfg)
        conn.execute("UPDATE tasks SET done_at = ?, outcome = ? WHERE id = ?", (time.time(), outcome, task_id))
        vault.history(conn, broker_id, f"task done: {kind}", note)
    return {"task": task_id, "broker": broker_id, "outcome": note}


def scan_now(ref=None):
    """Start a search in a process of its own (it takes a minute). Returns at once."""
    _ready()
    args = ["scan"] + ([_broker(ref)] if ref else [])
    _spawn(args)
    return {"started": f"searching {ref or 'the next site due'}; the result shows in the board when it's done"}


def run_pass():
    _ready()
    _spawn(["run"])
    return {"started": "one pass of the broker agent; the board shows what it did"}


def handoff(task):
    """Open the task's page in a visible window: you pass the check, the agent does the rest (brokers/handoff.py)."""
    _ready()
    _spawn(["handoff", str(task)])
    return {"started": "a browser window opens: pass the check there, and the agent fills in and sends the rest"}


def _spawn(args):
    import manage
    from lib.identity import LOG_DIR
    os.makedirs(LOG_DIR, exist_ok=True)
    log = os.path.join(LOG_DIR, "broker_agent.log")
    with open(log, "a") as out:
        subprocess.Popen([manage.PYTHON, "-m", "brokers", *args], stdout=out, stderr=out, stdin=subprocess.DEVNULL,
                         cwd=manage.ROOT, start_new_session=True)


def validate(strict=False):
    result = registry.validate(strict)
    return {"ok": result["ok"], "brokers": result["brokers"], "problems": len(result["problems"]),
            "first_problems": result["problems"][:15], "todo_verify": result["todo"]}


# ------------------------------------------------------------ the text form

OPS = {
    # name: (keys, first key, looks only, summary)
    "brokers status": ((), None, True, "the broker agent: totals by state, queue, last pass, approval mode"),
    "brokers report": (("group", "page", "per"), "group", True,
                       "the broker board, one group, paged (bare, it opens the broker dashboard instead)"),
    "brokers decide": (("approve", "reject", "group", "filter"), None, False, "approve or reject brokers by id"),
    "brokers mode": (("mode",), "mode", False, "switch approvals between standing and per_request"),
    "brokers info": (("broker",), "broker", True, "one broker: registry entry, what it holds, history, ledger"),
    "brokers scan": (("broker",), "broker", False, "search a people-search site for your listing now"),
    "brokers done": (("task", "outcome"), "task", False, "mark a manual task (or a one-time opt-out) done; outcome=skipped closes it but keeps the broker due"),
    "brokers override": (("broker",), "broker", False, "cancel a broker's automatic actions, withdraw its approval"),
    "brokers mark": (("broker", "outcome"), "broker", False,
                     "say what happened with a broker: outcome=submitted|removed|rejected"),
    "brokers ledger": (("broker", "element", "page", "per"), "broker", True, "which details went to which broker"),
    "brokers validate": (("strict",), "strict", True, "check the broker registry"),
    "brokers run": ((), None, False, "one pass of the broker agent now"),
}
OTHER_NAMES = {"set_mode": "mode", "broker_info": "info", "manual_done": "done", "registry_validate": "validate",
                 "sender": "info", "show": "info", "approve": "decide", "reject": "decide", "pass": "run",
                 "verify": "validate", "task": "done", "log": "ledger"}
_CALL = re.compile(r"^(brokers?|broker_\w+|set_mode|manual_done|registry_validate|optout)(?:[ _](\w+))?\b\s*(.*)$",
                   re.IGNORECASE | re.DOTALL)


def parse(text):
    """(op, {key: value}) for a `brokers` command, or None if the text is not one. Raises Problem."""
    match = _CALL.match(text.strip())
    if not match:
        return None
    head, verb, rest = match.group(1).lower(), (match.group(2) or "").lower(), match.group(3)
    if head in OTHER_NAMES or head.startswith("broker_"):
        verb, rest = head.replace("broker_", "") if head.startswith("broker_") else head, \
            ((match.group(2) + " ") if match.group(2) else "") + rest
    verb = OTHER_NAMES.get(verb, verb or "report")
    op = "brokers " + verb
    if op not in OPS:
        if head in ("brokers", "broker"):
            return None                   # "brokers sell my data?": talk
        raise Problem(f"no brokers {verb}. " + usage())
    keys, first_key, _looks, _summary = OPS[op]
    args = {m.group(1).lower(): next(g for g in m.groups()[1:] if g is not None) for m in _ARG.finditer(rest)}
    loose = _ARG.sub(" ", rest).strip().strip("\"'")
    if op == "brokers decide" and loose:          # `brokers decide approve spokeo whitepages`
        words = loose.split()
        verbs = [w.lower() for w in words if w.lower() in ("approve", "reject")]
        if match.group(2) and match.group(2).lower() in ("approve", "reject"):
            verbs = [match.group(2).lower()]
        ids = [w for w in words if w.lower() not in ("approve", "reject")]
        if len(verbs) != 1 or not ids:
            raise Problem("say it as: brokers decide approve=spokeo,whitepages reject=acxiom")
        args[verbs[0]] = ",".join([*args.get(verbs[0], "").split(","), *ids]).strip(",")
    elif loose:
        if not first_key or first_key in args:
            raise Problem(f"{op} does not take {loose!r}. it takes: {', '.join(k + '=' for k in keys) or 'nothing'}")
        args[first_key] = loose
    unknown = [k for k in args if k not in keys]
    if unknown:
        raise Problem(f"{op} has no {unknown[0]}=. it takes: {', '.join(k + '=' for k in keys) or 'nothing'}")
    return op, args


def looks_only(op, args=None):
    return OPS[op][2]


def _yes(value):
    return str(value or "").strip().lower() in ("yes", "y", "true", "1", "strict")


def run(op, args):
    try:
        if op == "brokers status":
            result = status()
        elif op == "brokers report":
            page, per = _paging(args)
            result = report_view(args.get("group"), page, per)
        elif op == "brokers decide":
            if not args.get("approve") and not args.get("reject"):
                raise Problem("decide what? brokers decide approve=spokeo reject=acxiom")
            data = None
            approve = _select(args.get("approve", ""), args.get("group"), args.get("filter"), data) \
                if args.get("approve") else []
            reject = _select(args.get("reject", ""), args.get("group"), args.get("filter"), data) \
                if args.get("reject") else []
            result = decide(approve, reject)
        elif op == "brokers mode":
            result = set_mode(args["mode"]) if args.get("mode") else {"mode": plan.approval_mode()}
        elif op == "brokers info":
            result = broker_info(args.get("broker"))
        elif op == "brokers scan":
            result = scan_now(args.get("broker"))
        elif op == "brokers done":
            if not args.get("task"):
                raise Problem("which task? brokers done task=12 outcome=done")
            result = manual_done(args["task"], args.get("outcome", "done"))
        elif op == "brokers override":
            result = override(args.get("broker"))
        elif op == "brokers mark":
            result = mark(args.get("broker"), args.get("outcome"))
        elif op == "brokers ledger":
            page, per = _paging(args)
            result = ledger_view(args.get("broker"), args.get("element"), page, per)
        elif op == "brokers validate":
            result = validate(_yes(args.get("strict")))
        elif op == "brokers run":
            result = run_pass()
        else:
            raise Problem(f"no operation {op}")
    except Problem as problem:
        return str(problem)
    except vault.Locked as error:
        return as_json({"error": str(error)})
    return as_json(result)


def call(text):
    try:
        parsed = parse(text)
    except Problem as problem:
        return str(problem)
    if parsed is None:
        return "not a brokers command. " + usage()
    return run(*parsed)


def usage():
    return "commands: " + " · ".join(f"{op} {' '.join(k + '=' for k in spec[0])}".strip() for op, spec in OPS.items())


def summary_line():
    return report.summary_line()
