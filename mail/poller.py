"""The one Gmail history poller for every email agent.

Each run (the `mailpoller` launchd agent, every 5 minutes) reads users.history.list from the
saved historyId, fetches each new message's metadata once, and writes events for the agents
that need it (store.emit; agents read them with store.take):

    inbox    INBOX_AGENT: messages that arrive with the INBOX label (the Promotions and Social tabs
             included), their later label changes, and messages you send (for reply detection)
    brokers  BROKER_AGENT: replies in threads it registered (watch_thread) and mail from domains it
             registered (watch_domains)

"Deleted" mail is not lost: Gmail's Delete moves it to Trash, where it can still be fetched for
30 days. A message is only missed if it is deleted for good before the next poll.

History records carry no labels-at-arrival and no times. The labels a message arrived with are
its labels now, with this batch's later label changes undone; those changes are then sent as
events in order. Their time is the poll's, so to within one poll interval.

Gmail keeps about a week of history. If the saved historyId has expired (404) the poller
re-scans recent mail (newer_than:Nd, Trash and Spam included) and starts again from now; label
changes from the gap are lost, which the summary says.
"""
import math
import time

from lib import psl
from mail import gmail, headers as hdr, store

INBOX_AGENT = "inbox"
BROKER_AGENT = "brokers"
RESCAN_MAX_DAYS = 29        # below store.PRUNE_DAYS, so a rescan never meets a pruned message as new
SKIP_LABELS = {"DRAFT", "CHAT"}


def watch_thread(thread_id, agent=BROKER_AGENT):
    with store.db() as conn:
        conn.execute("INSERT OR IGNORE INTO watch_threads VALUES (?, ?, ?)", (thread_id, agent, time.time()))


def watch_domains(domains, agent=BROKER_AGENT, replace=False):
    """Mail from any of these registrable domains (From or a passing DKIM signature) goes to the agent."""
    with store.db() as conn:
        if replace:
            conn.execute("DELETE FROM watch_domains WHERE agent = ?", (agent,))
        for domain in domains:
            domain = psl.registrable_domain(domain)
            if domain:
                conn.execute("INSERT OR IGNORE INTO watch_domains VALUES (?, ?)", (domain, agent))


def _changes(records):
    """History records flattened into [(kind, message_id, thread_id, labels)] in order."""
    out = []
    for record in records:
        for key, kind in (("messagesAdded", "added"), ("labelsAdded", "label+"), ("labelsRemoved", "label-")):
            for item in record.get(key, []):
                message = item.get("message", {})
                labels = message.get("labelIds", []) if kind == "added" else item.get("labelIds", [])
                out.append((kind, message.get("id"), message.get("threadId"), labels))
    return out


def _arrival_labels(current, later):
    """Labels at arrival: the current ones with the later changes undone, newest first."""
    labels = set(current)
    for kind, _id, _thread, changed in reversed(later):
        if kind == "label+":
            labels -= set(changed)
        else:
            labels |= set(changed)
    return sorted(labels)


def _brokers_want(conn, meta, auth_domains):
    if conn.execute("SELECT 1 FROM watch_threads WHERE thread_id = ?", (meta["thread_id"],)).fetchone():
        return True
    domains = {psl.registrable_domain(hdr.sender(meta["headers"]))} | set(auth_domains)
    domains.discard("")
    if not domains:
        return False
    marks = ",".join("?" * len(domains))
    return conn.execute(f"SELECT 1 FROM watch_domains WHERE domain IN ({marks})", tuple(domains)).fetchone() is not None


def _handle_new(api, message_id, thread_id, later, log, counts, rescanned=False):
    """Fetch one new message's metadata and route it."""
    try:
        raw = api.message(message_id, "metadata")
    except gmail.NotFound:
        counts["gone"] += 1            # deleted for good before we saw it
        return
    labels = raw.get("labelIds", []) if rescanned else _arrival_labels(raw.get("labelIds", []), later)
    store.save_message(raw, labels)
    meta = store.message(message_id)
    if SKIP_LABELS & set(labels):
        return
    counts["new"] += 1
    auth_domains = hdr.passing_dkim(hdr.trusted_auth(meta["headers"]))
    payload = {"labels": labels, "rescanned": rescanned}
    with store.db() as conn:
        # Broker mail belongs to the broker agent alone: it files it away (out of the inbox, read),
        # which the inbox agent would otherwise count against the sender.
        brokers = "SENT" not in labels and _brokers_want(conn, meta, auth_domains)
        if "SENT" in labels:
            store.emit(conn, INBOX_AGENT, "sent", message_id, thread_id, payload)
            counts["sent"] += 1
        elif "INBOX" in labels and not brokers:
            store.emit(conn, INBOX_AGENT, "added", message_id, thread_id, payload)
            conn.execute("INSERT OR IGNORE INTO routes VALUES (?, ?, ?)", (message_id, INBOX_AGENT, time.time()))
            counts["inbox"] += 1
        if brokers:
            store.emit(conn, BROKER_AGENT, "added", message_id, thread_id, payload)
            counts["brokers"] += 1


def _process(api, changes, log, counts):
    for index, (kind, message_id, thread_id, labels) in enumerate(changes):
        if not message_id:
            continue
        if kind == "added":
            if store.seen(message_id):
                continue
            later = [c for c in changes[index + 1:] if c[1] == message_id and c[0] != "added"]
            _handle_new(api, message_id, thread_id, later, log, counts)
            continue
        with store.db() as conn:
            routed = [r["agent"] for r in conn.execute("SELECT agent FROM routes WHERE message_id = ?", (message_id,))]
            for agent in routed:
                store.emit(conn, agent, "labels", message_id, thread_id,
                           {"added" if kind == "label+" else "removed": labels})
                counts["label_events"] += 1


def _rescan(api, log, counts):
    """The saved historyId expired: take the mailbox's current one, then pick up recent mail."""
    restart = api.profile()["historyId"]
    last = store.get("last_poll_at") or 0
    days = max(1, min(RESCAN_MAX_DAYS, math.ceil((time.time() - last) / 86400) + 1)) if last else 7
    listed = api.list_messages(f"newer_than:{days}d", include_spam_trash=True)
    log.warning("historyId expired; re-scanning %d messages from the last %d days", len(listed), days)
    for item in reversed(listed):     # oldest first
        if not store.seen(item["id"]):
            _handle_new(api, item["id"], item.get("threadId"), [], log, counts, rescanned=True)
    counts["rescanned_days"] = days
    return restart


def poll_once(log, api=None):
    """One pass. Returns a summary dict (also what `mail status` shows as the last poll)."""
    with store.file_lock("mail-poll", wait=False) as held:
        if not held:
            return {"skipped": "another poll is running"}
        api = api or gmail.client()
        counts = {"new": 0, "inbox": 0, "sent": 0, "brokers": 0, "label_events": 0, "gone": 0}
        started = time.time()
        try:
            history_id = store.get("history_id")
            if not history_id:
                history_id = api.profile()["historyId"]
                store.put("history_id", history_id)
                store.put("last_poll_at", started)
                store.put("last_poll_error", None)
                log.info("first poll: starting from historyId %s", history_id)
                return {"started_from": history_id}
            try:
                records, latest = api.history(history_id)
                _process(api, _changes(records), log, counts)
            except gmail.NotFound:
                latest = _rescan(api, log, counts)
            store.put("history_id", latest)
            if latest != history_id:
                store.put("history_advanced_at", started)
        except gmail.NotSignedIn as error:
            store.put("last_poll_error", str(error))
            return {"error": str(error)}
        except gmail.GmailError as error:
            store.put("last_poll_error", str(error))
            log.warning("poll failed: %s", error)
            return {"error": str(error)}
        store.put("last_poll_at", started)
        store.put("last_poll_error", None)
        store.put("last_poll", counts)
        if time.time() - (store.get("pruned_at") or 0) > 86400:
            store.prune()
            store.put("pruned_at", time.time())
        return counts
