"""Phase 4: carrying out what you approved, or what a saved rule says, one row per action in the actions log.

    unsubscribe  in order, stopping at the first that works:
                 1. RFC 8058 one-click: one POST to the List-Unsubscribe https URL
                 2. mailto: an email from your Gmail with the URI's subject and body. Skipped for
                    mail through Apple's Hide My Email: it would tie your Gmail address to the
                    relay address
                 3. the https link (List-Unsubscribe, else the body's unsubscribe link) in the
                    headless browser (browser.py)
                 Each category is its own sender, so this is the link from that category's own
                 mail (an ad's List-Unsubscribe, for the ads).
                 4. none worked: the sender goes to Manual in the latest report right away (and
                    in the next report's, until you handle it), with its link and the offer to block
                    instead. A valid sender is never blocked unasked.
                 Links are only followed for mail whose authentication lines up.
    block        one category (rules.sender_key: ads, other mail, or a list's mail) from the
                 sender's address (or, for spam from its own domain, the domain): new mail of that
                 category goes to Trash as capture reads it. No Gmail filter, since a filter can't
                 tell a receipt from an ad; nothing else from the sender is ever touched.
                 trash=yes also trashes what of that category is in the inbox now.

Actions are queued (decide, or the analysis in permanent mode) and run by work(): in the weekly
agent, or in a process of its own (`python -m unsub work --notify`) started by `unsub decide`,
which shows a banner when it is done.
"""
import json
import os
import subprocess
import sys
import time
from urllib.parse import parse_qs, unquote, urlsplit

from lib.common import LOG_DIR, banner
from mail import auth, gmail, ops as mail_ops, store as mail_store
from unsub import OPEN_DASHBOARD, browser, rules, store

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ONE_CLICK_TIMEOUT = 20
ACTION_BANNER_SECONDS = 5   # the "done" banner after actions closes itself; the weekly report banner stays


def queue(conn, sender_id, action, options=None, auto=False, rule=None, report_id=None):
    """Add an action to the log as queued (not if the same one is already waiting). Its id."""
    found = conn.execute("SELECT id FROM actions WHERE sender_id = ? AND action = ? AND status IN ('queued', 'running')",
                         (sender_id, action)).fetchone()
    if found:
        return found["id"]
    return conn.execute("INSERT INTO actions (sender_id, action, options, auto, rule, report_id, queued_at) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?)", (sender_id, action, json.dumps(options or {}), int(auto),
                                                         rule, report_id, time.time())).lastrowid


def block_scope(sender, section, cfg):
    """'domain' for spam from a domain that is really the sender's (auth lines up, or a lookalike), else 'address'."""
    latest = sender["latest"]
    own = latest.get("auth") == "aligned" or latest.get("lookalike")
    if section == "block" and own and sender["from_domain"] and sender["from_domain"] not in cfg["freemail"]:
        return "domain"
    return "address"


def block_targets(sender, scope):
    """[(kind, value)] a block of this sender covers: its list, its domain (spam from its own domain),
    or its addresses. Always together with the sender's one category."""
    latest = sender["latest"]
    if sender["key"].startswith("list:"):
        return [("list", latest.get("list_id") or rules.base_key(sender["key"])[len("list:"):])]
    if scope == "domain" and sender["from_domain"]:
        return [("domain", sender["from_domain"])]
    return [("address", a) for a in (sender["addresses"] or [latest.get("sender")]) if a]


def trash_existing(sender, targets, limit=100):
    """Trash what is in the inbox now in the sender's blocked category; its other mail stays. How many."""
    from unsub import capture

    api = gmail.client()
    cfg = rules.config()
    category = rules.category(sender["key"])
    words = [value for kind, value in targets if kind in ("address", "domain")] or sender["addresses"]
    if not words:
        return 0
    trashed = 0
    for item in api.list_messages("in:inbox from:(" + " OR ".join(words) + ")", limit=limit):
        found = capture.read(api, item["id"], None, cfg)
        if found and rules.category(rules.sender_key(found[0])) == category:
            with store.db() as conn:
                hit = store.blocked(conn, category, found[0])
            if hit:
                api.trash(item["id"])
                trashed += 1
    return trashed


# ------------------------------------------------------------ unsubscribing

def _one_click(url):
    import requests

    response = requests.post(url, data="List-Unsubscribe=One-Click", timeout=ONE_CLICK_TIMEOUT, allow_redirects=False,
                             headers={"Content-Type": "application/x-www-form-urlencoded",
                                      "User-Agent": browser.USER_AGENT})
    return 200 <= response.status_code < 400, f"HTTP {response.status_code}"


def _mailto(uri):
    parts = urlsplit(uri)
    query = parse_qs(parts.query)
    to = unquote(parts.path) or query.get("to", [""])[0]
    if "@" not in to:
        return False, "the mailto: has no address"
    subject = query.get("subject", ["unsubscribe"])[0] or "unsubscribe"
    text = query.get("body", ["unsubscribe"])[0] or "unsubscribe"
    sent = gmail.client().send(gmail.compose(to, subject, text))
    own = store.get("own_sent", [])                  # so capture and the Sent search never count it as writing to them
    store.put("own_sent", (own + [sent.get("id")])[-500:])
    return True, f"sent to {to}"


def unsubscribe(sender):
    """(status, method, detail): status done or manual."""
    import requests

    latest = sender["latest"]
    options = latest.get("unsub") or {}
    link = options.get("https") or options.get("link")
    if latest.get("auth") != "aligned":
        return "manual", None, "its authentication does not line up, so its links were not followed"
    tried = []
    if options.get("one_click"):
        try:
            ok, detail = _one_click(options["one_click"])
        except requests.RequestException as error:
            ok, detail = False, type(error).__name__
        if ok:
            return "done", "one-click", detail
        tried.append(f"one-click: {detail}")
    if options.get("mailto"):
        if latest.get("relay_sender"):
            tried.append("mailto skipped: it would come from your Gmail address and tie it to the "
                         "Hide My Email address")
        else:
            try:
                ok, detail = _mailto(options["mailto"])
            except (gmail.GmailError, auth.NotSignedIn) as error:
                ok, detail = False, str(error)[:120]
            if ok:
                return "done", "mailto", detail
            tried.append(f"mailto: {detail}")
    if link:
        ok, detail = browser.unsubscribe(link, sorted({sender["from_domain"], *latest.get("dkim", [])} - {"", None}))
        if ok:
            return "done", "browser", detail
        tried.append(f"browser: {detail}")
    return "manual", None, "; ".join(tried) or "no unsubscribe option"


# ------------------------------------------------------------ running the queue

def manual_evidence(sender):
    """A Manual entry's evidence: why the unsubscribe failed and the link to do it yourself."""
    detail = json.loads(sender["status_detail"] or "{}") if (sender["status_detail"] or "").startswith("{") else {}
    return {"sender": sender["label"], "why": detail.get("why", ""), "link": detail.get("link", ""),
            "note": "unsubscribe yourself at the link, or block (their mail goes to Trash)"}


def to_manual(conn, sender):
    """Put a sender whose unsubscribe failed under Manual in the latest report, unless it is there already."""
    report = conn.execute("SELECT MAX(id) FROM reports").fetchone()[0]
    if report is None or conn.execute("SELECT 1 FROM items WHERE report_id = ? AND sender_id = ? AND section = 'manual' "
                                      "AND status = 'pending'", (report, sender["id"])).fetchone():
        return
    conn.execute("INSERT INTO items (report_id, section, sender_id, proposal, evidence, status) "
                 "VALUES (?, 'manual', ?, 'block', ?, 'pending')", (report, sender["id"], json.dumps(manual_evidence(sender))))


def record_done(conn, sender_id, report_id=None):
    """You unsubscribed by hand (a Manual entry's Done): the sender counts as unsubscribed from now, so
    it is held to noncompliant_days like any other, and the actions log says how."""
    store.set_status(conn, sender_id, "unsubscribed", "by hand")
    now = time.time()
    conn.execute("INSERT INTO actions (sender_id, action, status, method, detail, report_id, queued_at, done_at) "
                 "VALUES (?, 'unsubscribe', 'done', 'by hand', 'you unsubscribed at their page', ?, ?, ?)",
                 (sender_id, report_id, now, now))


def _run(row):
    """(status, method, detail) for one queued action. Updates the sender."""
    options = json.loads(row["options"] or "{}")
    with store.db() as conn:
        sender = store.sender(conn, row["sender_id"])
    if sender is None:
        return "failed", None, "no such sender"
    action = row["action"]
    if action == "unsubscribe":
        status, method, detail = unsubscribe(sender)
        with store.db() as conn:
            if status == "done":
                store.set_status(conn, sender["id"], "unsubscribed", method)
            else:
                link = (sender["latest"].get("unsub") or {})
                store.set_status(conn, sender["id"], "manual",
                                 json.dumps({"why": detail, "link": link.get("https") or link.get("link") or ""}))
                to_manual(conn, store.sender(conn, sender["id"]))
        return status, method, detail
    if action == "block":
        category = rules.category(sender["key"])
        if category == "notices":
            return "failed", None, "receipts and notices are never blocked"
        targets = block_targets(sender, options.get("scope", "address"))
        with store.db() as conn:
            for kind, value in targets:
                conn.execute("INSERT OR REPLACE INTO blocks VALUES (?, ?, ?, ?, ?)",
                             (category, kind, value, sender["id"], time.time()))
            store.set_status(conn, sender["id"], "blocked", options.get("scope", "address"))
            conn.execute("UPDATE senders SET blocked = ? WHERE id = ?",
                         (json.dumps([f"{kind} {value}" for kind, value in targets]), sender["id"]))
        trashed = trash_existing(sender, targets) if options.get("trash") else 0
        detail = (f"their {rules.CATEGORY_NAMES[category]} from {', '.join(v for _k, v in targets)} goes to Trash as it "
                  "arrives; nothing else from them is touched" + (f"; trashed {trashed} from the inbox" if trashed else ""))
        return "done", "category block", detail
    return "failed", None, f"unknown action {action}"


def work(log, notify=False, wait=False):
    """Run every queued action. {"done": [...], "text": summary} (or {"skipped": why})."""
    with mail_store.file_lock("unsub-work", wait=wait) as held:
        if not held:
            return {"skipped": "another run is working through the queue"}
        with store.db() as conn:          # we hold the lock, so anything 'running' was cut off
            conn.execute("UPDATE actions SET status = 'queued' WHERE status = 'running'")
        done = []
        while True:
            with store.db() as conn:
                row = conn.execute("SELECT * FROM actions WHERE status = 'queued' ORDER BY id LIMIT 1").fetchone()
                if row is None:
                    break
                conn.execute("UPDATE actions SET status = 'running' WHERE id = ?", (row["id"],))
            try:
                status, method, detail = _run(row)
            except (mail_ops.Problem, gmail.GmailError, auth.NotSignedIn, OSError) as error:
                status, method, detail = "failed", None, str(error)[:300]
            except Exception as error:                # keep the queue moving; the log has the traceback
                log.exception("action %s failed", row["id"])
                status, method, detail = "failed", None, f"{type(error).__name__}: {str(error)[:200]}"
            with store.db() as conn:
                conn.execute("UPDATE actions SET status = ?, method = ?, detail = ?, done_at = ? WHERE id = ?",
                             (status, method, detail[:500] if detail else detail, time.time(), row["id"]))
            log.info("action %s: %s s%s -> %s %s %s", row["id"], row["action"], row["sender_id"], status,
                     method or "", detail or "")
            done.append({"id": f"s{row['sender_id']}", "action": row["action"], "status": status, "method": method})
    text = summary_text(done)
    if notify and done:
        banner("Inbox agent", text, ACTION_BANNER_SECONDS, post=OPEN_DASHBOARD)
    return {"done": done, "text": text}


def summary_text(done):
    if not done:
        return "nothing was waiting"
    unsubscribed = [d for d in done if d["action"] == "unsubscribe" and d["status"] == "done"]
    methods = {}
    for d in unsubscribed:
        methods[d["method"]] = methods.get(d["method"], 0) + 1
    parts = []
    if unsubscribed:
        parts.append(f"unsubscribed {len(unsubscribed)} (" + ", ".join(f"{m} {n}" for m, n in methods.items()) + ")")
    blocked = sum(d["action"] == "block" and d["status"] == "done" for d in done)
    if blocked:
        parts.append(f"blocked {blocked}")
    manual = sum(d["status"] == "manual" for d in done)
    if manual:
        parts.append(f"{manual} need{'s' if manual == 1 else ''} a manual unsubscribe (unsub report section=manual)")
    failed = sum(d["status"] == "failed" for d in done)
    if failed:
        parts.append(f"{failed} failed (unsub actions status=failed)")
    return " · ".join(parts)


def start_worker():
    """Run the queue in a process of its own (it may open a browser), with a banner at the end."""
    with open(os.path.join(LOG_DIR, "unsub_work.log"), "a") as out:
        subprocess.Popen([sys.executable, "-m", "unsub", "work", "--notify"], stdout=out, stderr=out,
                         stdin=subprocess.DEVNULL, cwd=ROOT, start_new_session=True)


def undo_block(sender):
    """Lift the sender's block. What it had covered."""
    with store.db() as conn:
        rows = conn.execute("SELECT category, kind, value FROM blocks WHERE sender_id = ?", (sender["id"],)).fetchall()
        conn.execute("DELETE FROM blocks WHERE sender_id = ?", (sender["id"],))
        conn.execute("UPDATE senders SET blocked = NULL WHERE id = ?", (sender["id"],))
        store.set_status(conn, sender["id"], None)
    return [f"{rules.CATEGORY_NAMES[r['category']]} from {r['value']}" for r in rows]

