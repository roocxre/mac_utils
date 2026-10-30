"""Phase 1: keep every new inbox message and what you do with it.

Runs after each mail poll (agents/mail_poller.py CONSUMERS) and takes the `inbox` events:

    added    a message that arrived with INBOX (Promotions and Social included): its headers, the
             text of its body (zlib, at most 256 KB, no attachments) and its facts (rules.py). If
             its category from that sender is blocked (store.blocks), it goes to Trash
    labels   a later label change: opened (UNREAD off), deleted (TRASH on, and whether it was still
             unread), archived (INBOX off without TRASH or SPAM in the same poll), starred, marked spam
    sent     a message you sent: its thread's messages count as replied, and its recipients'
             domains (or addresses, for free-mail) as people you write to

Blocks act here, not as Gmail filters, because a Gmail filter can't tell a receipt from an ad.
So a blocked ad can sit in the inbox until the next poll (5 minutes), and only while the Mac is on.

backfill() reads Trash and the inbox for the 30 days before capture started; the first analysis
runs it, once. Mail that arrives and is deleted during a week or more with the Mac off is not
seen: the poller's catch-up only finds what is still in the inbox.

backfill(spam=True) is the Spam pass (`unsub spamfolder`): Gmail's Spam folder never reaches the
inbox, so capture never sees it. It reads what is in Spam (Gmail keeps 30 days) the same way, marked
spam_folder. That is Gmail's verdict, not yours, so it is not counted as you marking it spam.

Times are the poll's, so to within its 5 minutes. If Gmail can't be reached the events go back
in the queue for the next run.
"""
import json
import time

from lib import psl
from mail import gmail, headers as hdr, ops as mail_ops, poller, store as mail_store
from unsub import body, rules, store

BATCH = 200
SAME_POLL_SECONDS = 30


def _upsert_sender(conn, facts, arrived):
    key = rules.sender_key(facts)
    row = conn.execute("SELECT id FROM senders WHERE key = ?", (key,)).fetchone()
    label = rules.label(facts, key)
    if row is None:
        conn.execute("INSERT INTO senders (key, label, from_domain, addresses, first_seen, last_seen, latest, recent) "
                     "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                     (key, label, facts["from_domain"], json.dumps([facts["sender"]]), arrived, arrived,
                      json.dumps(facts), json.dumps(store.remember([], facts.get("message_id"), arrived))))
        return conn.execute("SELECT id FROM senders WHERE key = ?", (key,)).fetchone()["id"]
    sender = store.sender(conn, row["id"])
    addresses = sender["addresses"]
    if facts["sender"] and facts["sender"] not in addresses:
        addresses = (addresses + [facts["sender"]])[-10:]
    newer = arrived >= (sender["latest"].get("arrived") or 0)
    conn.execute("UPDATE senders SET label = ?, from_domain = ?, addresses = ?, first_seen = MIN(first_seen, ?), "
                 "last_seen = MAX(last_seen, ?), latest = ?, recent = ? WHERE id = ?",
                 (label if newer else sender["label"], facts["from_domain"] if newer else sender["from_domain"],
                  json.dumps(addresses), arrived, arrived,
                  json.dumps(facts) if newer else json.dumps(sender["latest"]),
                  json.dumps(store.remember(sender["recent"], facts.get("message_id"), arrived)), row["id"]))
    return row["id"]


def read(api, message_id, labels, cfg, thread_id=None):
    """(facts, headers, text, snippet) for one message, fetched in full; None if Gmail no longer has it."""
    try:
        full = api.message(message_id, "full")
        payload = full.get("payload", {})
        headers = [[h["name"], h["value"]] for h in payload.get("headers", [])]
        text, links = body.extract(payload)
        arrived, snippet = int(full.get("internalDate", 0)) / 1000, full.get("snippet", "")
        thread_id = full.get("threadId") or thread_id
        labels = full.get("labelIds", labels) if labels is None else labels
    except gmail.NotFound:                    # deleted for good since the poll: keep the headers it saved
        meta = mail_ops.message_meta(message_id)
        if meta is None:
            return None
        headers, text, links = meta["headers"], "", []
        arrived, snippet, thread_id = meta["internal_date"], meta["snippet"], meta["thread_id"]
        labels = meta["labels"] if labels is None else labels
    facts = rules.message_facts(headers, labels, (text, links), cfg)
    facts.update(message_id=message_id, thread_id=thread_id, arrived=arrived)
    return facts, headers, text, snippet


def _added(api, event, cfg):
    message_id = event["message_id"]
    with store.db() as conn:
        if conn.execute("SELECT 1 FROM messages WHERE id = ?", (message_id,)).fetchone():
            return "seen"
    labels = event["payload"].get("labels", [])
    found = read(api, message_id, labels, cfg, event["thread_id"])
    if found is None:
        return "gone"
    facts, headers, text, snippet = found
    arrived, thread_id = facts["arrived"], facts["thread_id"]
    unread = "UNREAD" in labels
    with store.db() as conn:
        sender_id = _upsert_sender(conn, facts, arrived)
        conn.execute("INSERT OR IGNORE INTO messages (id, thread_id, sender_id, arrived, captured_at, labels, subject, "
                     "snippet, facts, body, body_chars, unread, opened_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                     (message_id, thread_id, sender_id, arrived, time.time(), json.dumps(labels),
                      hdr.first(headers, "Subject")[:300], snippet[:300], json.dumps(facts), store.pack(text),
                      len(text), int(unread), None if unread else arrived))
        blocked = store.blocked(conn, rules.category(rules.sender_key(facts)), facts)
    # Blocks are enforced here, by what the message is: a blocked category goes to Trash as it
    # arrives, and the sender's other categories (its receipts above all) are never touched.
    if blocked:
        api.trash(message_id)
        return "trashed"
    return "captured"


def _labels(event, later):
    """Record what a label change means. `later`: the rest of this batch (to see a TRASH that came with an INBOX removal)."""
    added, removed = set(event["payload"].get("added", [])), set(event["payload"].get("removed", []))
    at, message_id = event["created_at"], event["message_id"]
    with store.db() as conn:
        row = conn.execute("SELECT * FROM messages WHERE id = ?", (message_id,)).fetchone()
        if row is None:
            return False
        change = {}
        unread = row["unread"]
        if "UNREAD" in removed and unread:
            change.update(unread=0, opened_at=row["opened_at"] or at)
            unread = 0
        if "UNREAD" in added:
            change["unread"] = unread = 1
        if "TRASH" in added and not row["deleted_at"]:
            change.update(deleted_at=at, deleted_unread=unread)
        if "SPAM" in added and not row["spam_at"]:
            change["spam_at"] = at
        if "STARRED" in added and not row["starred_at"]:
            change["starred_at"] = at
        if "INBOX" in removed and not (row["deleted_at"] or row["spam_at"] or row["archived_at"]) \
                and "TRASH" not in added and "SPAM" not in added:
            gone = any(e["message_id"] == message_id and e["kind"] == "labels"
                       and {"TRASH", "SPAM"} & set(e["payload"].get("added", []))
                       and abs(e["created_at"] - at) <= SAME_POLL_SECONDS for e in later)
            if not gone:
                change.update(archived_at=at, archived_unread=unread)
        if change:
            conn.execute(f"UPDATE messages SET {', '.join(k + ' = ?' for k in change)} WHERE id = ?",
                         (*change.values(), message_id))
    return bool(change)


def _sent(event, cfg):
    meta = mail_ops.message_meta(event["message_id"])
    at = (meta or {}).get("internal_date") or event["created_at"]
    with store.db() as conn:
        if meta and rules.unsubscribe_request(event["message_id"], meta["headers"], set(store.get("own_sent", [])),
                                              rules.unsubscribe_mailtos(conn)):
            return 0                     # an unsubscribe request is neither a reply nor writing to them
        replied = conn.execute("UPDATE messages SET replied_at = ? WHERE thread_id = ? AND replied_at IS NULL "
                               "AND arrived <= ?", (at, event["thread_id"], at)).rowcount
        for address in hdr.addresses(meta["headers"], "To", "Cc", "Bcc") if meta else []:
            domain = psl.registrable_domain(address)
            if domain:
                conn.execute("INSERT OR REPLACE INTO sent_domains VALUES (?, 1, 'sent', ?)",
                             (address if domain in cfg["freemail"] else domain, time.time()))
    return replied


BACKFILL_DAYS = 30          # Gmail empties Trash after 30 days
SKIP_LABELS = {"SENT", "DRAFT", "CHAT", "SPAM"}


def backfill(log, api=None, days=BACKFILL_DAYS, spam=False):
    """Capture what is in Trash and the inbox from the last `days` days that capture never saw: mail
    deleted before the agent started, or while the Mac was off. What was done with it comes from its
    labels: in Trash is deleted (still UNREAD there: deleted unread, the strongest signal), not
    UNREAD is opened, STARRED is starred. When it was deleted is not known. spam=True reads the Spam
    folder instead (facts spam_folder). Returns counts."""
    api = api or gmail.client()
    cfg = rules.config()
    found = {}
    queries = (f"in:spam newer_than:{days}d",) if spam else (f"in:trash newer_than:{days}d", f"in:inbox newer_than:{days}d")
    skip = SKIP_LABELS - {"SPAM"} if spam else SKIP_LABELS
    for query in queries:
        for item in api.list_messages(query, include_spam_trash=True, limit=5000):
            found[item["id"]] = item
    with store.db() as conn:
        known = {r[0] for r in conn.execute("SELECT id FROM messages UNION SELECT id FROM analysed")}
    counts = {"looked_at": len(found), "captured": 0, "skipped": 0}
    for message_id, item in found.items():
        if message_id in known:
            continue
        meta = mail_ops.message_meta(message_id)
        if meta is None or skip & set(meta["labels"]):
            counts["skipped"] += 1
            continue
        got = read(api, message_id, None, cfg, item.get("threadId"))
        if got is None:
            continue
        facts, headers, text, snippet = got
        labels = meta["labels"]
        facts["backfilled"] = True
        if spam:
            facts["spam_folder"] = True
        arrived, unread, trashed = facts["arrived"], "UNREAD" in labels, "TRASH" in labels
        with store.db() as conn:
            sender_id = _upsert_sender(conn, facts, arrived)
            conn.execute("INSERT OR IGNORE INTO messages (id, thread_id, sender_id, arrived, captured_at, labels, subject, "
                         "snippet, facts, body, body_chars, unread, opened_at, deleted_at, deleted_unread, starred_at) "
                         "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                         (message_id, facts["thread_id"], sender_id, arrived, time.time(), json.dumps(labels),
                          hdr.first(headers, "Subject")[:300], snippet[:300], json.dumps(facts), store.pack(text),
                          len(text), int(unread), None if unread else arrived, arrived if trashed else None,
                          int(unread) if trashed else None, arrived if "STARRED" in labels else None))
        counts["captured"] += 1
    if counts["captured"]:
        log.info("backfill%s: %s", " (Spam)" if spam else "", counts)
    return counts


def run(log, api=None):
    """Take everything queued for this agent. Returns counts."""
    counts = {"captured": 0, "actions": 0, "replies": 0, "gone": 0, "trashed": 0}
    cfg = rules.config()
    api = api or gmail.client()
    while True:
        events = mail_store.take(poller.INBOX_AGENT, BATCH)
        if not events:
            break
        for index, event in enumerate(events):
            try:
                if event["kind"] == "added":
                    result = _added(api, event, cfg)
                    if result != "seen":
                        counts[result] += 1
                    if result == "trashed":
                        log.info("trashed %s: an ad from a blocked sender that got past the filter", event["message_id"])
                elif event["kind"] == "labels":
                    counts["actions"] += _labels(event, events[index + 1:])
                elif event["kind"] == "sent":
                    counts["replies"] += _sent(event, cfg)
            except (gmail.GmailError, gmail.NotSignedIn, OSError) as error:
                mail_store.release([e["id"] for e in events[index:]])
                log.warning("capture stopped, %d events back in the queue: %s", len(events) - index, error)
                store.put("last_capture_error", str(error)[:300])
                return counts
    store.put("last_capture_at", time.time())
    store.put("last_capture_error", None)
    if any(counts.values()):
        store.put("last_capture", counts)
    return counts


def pending():
    return mail_store.queue_depth().get(poller.INBOX_AGENT, 0)

