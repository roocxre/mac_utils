"""The unsubscribe agent's SQLite database (unsub.db in the state folder).

    messages   one row per captured inbox message, with its body (zlib) and what you did with it.
               Deleted after the weekly analysis that counts it.
    senders    one row per sender, kept for good: running totals, the newest message's facts
               (unsubscribe links, authentication), its recent message ids (to read them
               from the dashboard), and what was done to it
    sent_domains  domains you have written to (from your Sent mail): never unsubscribed or blocked
    reports, items     each weekly report and its entries (by section)
    decisions  every approve / reject, in either mode
    rules      saved decisions (permanent mode): allow or deny with an action
    analysed   ids of messages already counted and deleted (45 days), so a backfill never counts one twice
               (the Spam pass included)
    blocks     what a block covers: one category (ads, other, list) from an address, domain or list
    actions    the actions log: one row per unsubscribe or block, with method and outcome
"""
import json
import os
import sqlite3
import time
import zlib
from contextlib import contextmanager

from lib.common import state_path

PATH = state_path("unsub.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT);

CREATE TABLE IF NOT EXISTS senders (
    id INTEGER PRIMARY KEY AUTOINCREMENT, key TEXT UNIQUE, label TEXT, from_domain TEXT,
    addresses TEXT DEFAULT '[]', first_seen REAL, last_seen REAL,
    total INTEGER DEFAULT 0, opened INTEGER DEFAULT 0, deleted INTEGER DEFAULT 0,
    deleted_unread INTEGER DEFAULT 0, archived INTEGER DEFAULT 0, archived_unread INTEGER DEFAULT 0,
    starred INTEGER DEFAULT 0, replied INTEGER DEFAULT 0, spam_reports INTEGER DEFAULT 0,
    transactional INTEGER DEFAULT 0, promotions INTEGER DEFAULT 0, bulk INTEGER DEFAULT 0,
    aligned INTEGER DEFAULT 0, auth_fail INTEGER DEFAULT 0,
    delete_seconds TEXT DEFAULT '[]', latest TEXT DEFAULT '{}', last_analyzed REAL,
    status TEXT, status_at REAL, status_detail TEXT, blocked TEXT, resubscribe INTEGER DEFAULT 0,
    recent TEXT DEFAULT '[]');

CREATE TABLE IF NOT EXISTS messages (
    id TEXT PRIMARY KEY, thread_id TEXT, sender_id INTEGER, arrived REAL, captured_at REAL,
    labels TEXT, subject TEXT, snippet TEXT, facts TEXT, body BLOB, body_chars INTEGER,
    unread INTEGER, opened_at REAL, deleted_at REAL, deleted_unread INTEGER, archived_at REAL,
    archived_unread INTEGER, starred_at REAL, replied_at REAL, spam_at REAL);
CREATE INDEX IF NOT EXISTS messages_thread ON messages (thread_id);
CREATE INDEX IF NOT EXISTS messages_sender ON messages (sender_id);

CREATE TABLE IF NOT EXISTS sent_domains (domain TEXT PRIMARY KEY, sent INTEGER, source TEXT, checked_at REAL);

CREATE TABLE IF NOT EXISTS reports (
    id INTEGER PRIMARY KEY AUTOINCREMENT, created_at REAL, mode TEXT, window_start REAL,
    window_end REAL, summary TEXT);
CREATE TABLE IF NOT EXISTS items (
    id INTEGER PRIMARY KEY AUTOINCREMENT, report_id INTEGER, section TEXT, sender_id INTEGER,
    proposal TEXT, evidence TEXT, status TEXT DEFAULT 'pending', decision TEXT, decided_at REAL);
CREATE INDEX IF NOT EXISTS items_report ON items (report_id, section);

CREATE TABLE IF NOT EXISTS decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT, sender_id INTEGER, report_id INTEGER, section TEXT,
    proposal TEXT, decision TEXT, action TEXT, mode TEXT, at REAL, promoted INTEGER DEFAULT 0);

CREATE TABLE IF NOT EXISTS rules (
    sender_id INTEGER PRIMARY KEY, kind TEXT, action TEXT, created_at REAL, source TEXT);

CREATE TABLE IF NOT EXISTS analysed (id TEXT PRIMARY KEY, at REAL);

CREATE TABLE IF NOT EXISTS blocks (
    category TEXT, kind TEXT, value TEXT, sender_id INTEGER, created_at REAL,
    PRIMARY KEY (category, kind, value));

CREATE TABLE IF NOT EXISTS actions (
    id INTEGER PRIMARY KEY AUTOINCREMENT, sender_id INTEGER, action TEXT, options TEXT DEFAULT '{}',
    status TEXT DEFAULT 'queued', method TEXT, detail TEXT, auto INTEGER DEFAULT 0, rule TEXT,
    report_id INTEGER, queued_at REAL, done_at REAL);
CREATE INDEX IF NOT EXISTS actions_status ON actions (status);
"""

# Columns added after the first release, for databases made before them.
MIGRATIONS = ["ALTER TABLE senders ADD COLUMN recent TEXT DEFAULT '[]'",
              "ALTER TABLE senders ADD COLUMN spam_folder INTEGER DEFAULT 0"]
RECENT = 25                 # message ids kept per sender, for the dashboard's message reader

_ready = set()


@contextmanager
def db():
    """A connection that commits on success, rolls back on error, and is always closed."""
    conn = sqlite3.connect(PATH, timeout=15)
    conn.row_factory = sqlite3.Row
    try:
        if PATH not in _ready:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript(SCHEMA)
            for change in MIGRATIONS:
                try:
                    conn.execute(change)
                except sqlite3.OperationalError:          # already there
                    pass
            _ready.add(PATH)
        yield conn
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()


def get(key, default=None):
    with db() as conn:
        row = conn.execute("SELECT value FROM kv WHERE key = ?", (key,)).fetchone()
    return json.loads(row["value"]) if row else default


def put(key, value):
    with db() as conn:
        conn.execute("INSERT OR REPLACE INTO kv (key, value) VALUES (?, ?)", (key, json.dumps(value)))


def pack(text):
    return zlib.compress(text.encode("utf-8"), 6) if text else None


def unpack(blob):
    return zlib.decompress(blob).decode("utf-8") if blob else ""


def sender(conn, sender_id):
    row = conn.execute("SELECT * FROM senders WHERE id = ?", (sender_id,)).fetchone()
    if row is None:
        return None
    found = dict(row)
    for key in ("addresses", "delete_seconds", "latest", "recent"):
        found[key] = json.loads(found[key] or ("{}" if key == "latest" else "[]"))
    found["blocked"] = json.loads(found["blocked"]) if found["blocked"] else []
    return found


def remember(recent, message_id, arrived):
    """The recent list ([[id, arrived]], newest first) with one more message."""
    if not message_id or any(m[0] == message_id for m in recent):
        return recent
    return sorted(recent + [[message_id, arrived]], key=lambda m: -(m[1] or 0))[:RECENT]


def set_status(conn, sender_id, status, detail=None):
    conn.execute("UPDATE senders SET status = ?, status_at = ?, status_detail = ? WHERE id = ?",
                 (status, time.time(), detail, sender_id))


def blocked(conn, category, facts):
    """True if mail of this category from this sender, its domain or its list is blocked."""
    return conn.execute("SELECT 1 FROM blocks WHERE category = ? AND ((kind = 'address' AND value = ?) OR "
                        "(kind = 'domain' AND value = ?) OR (kind = 'list' AND value = ?))",
                        (category, facts.get("sender") or "", facts.get("from_domain") or "",
                         facts.get("list_id") or "")).fetchone() is not None


def size_text():
    try:
        return f"{os.path.getsize(PATH) / 1024 ** 2:.1f} MB"
    except OSError:
        return "0 MB"
