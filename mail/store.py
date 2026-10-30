"""The email layer's SQLite database and its file locks.

One file, mail.db in the state folder, shared by the poller, the assistant and the email agents.
WAL mode and a busy timeout let them use it at the same time. Other agents keep their own
databases and reach these tables only through ops.py (and the event queue below).

Event queue: the poller appends one row per thing an agent should hear about; an agent takes its
rows in order with take(agent) and they are marked taken. Rows are pruned after PRUNE_DAYS.
"""
import fcntl
import json
import os
import sqlite3
import time
from contextlib import contextmanager

from lib.common import state_path

PATH = state_path("mail.db")
PRUNE_DAYS = 30             # taken events, cached message headers and old routes
UNTAKEN_DAYS = 60           # events no agent ever took (an agent that is not built yet)

SCHEMA = """
CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT);

CREATE TABLE IF NOT EXISTS messages (
    id TEXT PRIMARY KEY, thread_id TEXT, internal_date REAL, labels TEXT, headers TEXT,
    snippet TEXT, size INTEGER, fetched_at REAL);

CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT, agent TEXT, kind TEXT, message_id TEXT, thread_id TEXT,
    payload TEXT, created_at REAL, taken_at REAL);
CREATE INDEX IF NOT EXISTS events_open ON events (agent, taken_at, id);

CREATE TABLE IF NOT EXISTS routes (
    message_id TEXT, agent TEXT, created_at REAL, PRIMARY KEY (message_id, agent));

CREATE TABLE IF NOT EXISTS watch_threads (
    thread_id TEXT, agent TEXT, created_at REAL, PRIMARY KEY (thread_id, agent));
CREATE TABLE IF NOT EXISTS watch_domains (
    domain TEXT, agent TEXT, PRIMARY KEY (domain, agent));
"""
# A database from an older version may hold more tables; nothing reads them, so they are left alone.

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
            _ready.add(PATH)
        yield conn
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()


@contextmanager
def file_lock(name, wait=True):
    """An exclusive lock between processes. Yields True when held; with wait=False, False if busy."""
    handle = open(state_path(f"{name}.lock"), "a")
    try:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | (0 if wait else fcntl.LOCK_NB))
        except BlockingIOError:
            yield False
            return
        yield True
    finally:
        handle.close()


# ------------------------------------------------------------ small values

def get(key, default=None):
    with db() as conn:
        row = conn.execute("SELECT value FROM kv WHERE key = ?", (key,)).fetchone()
    return json.loads(row["value"]) if row else default


def put(key, value):
    with db() as conn:
        conn.execute("INSERT OR REPLACE INTO kv (key, value) VALUES (?, ?)", (key, json.dumps(value)))


# ------------------------------------------------------------ cached message metadata

def save_message(meta, labels=None):
    """Keep the parts of a Gmail metadata response the layer and the agents look at."""
    headers = [[h["name"], h["value"]] for h in meta.get("payload", {}).get("headers", [])]
    with db() as conn:
        conn.execute("INSERT OR REPLACE INTO messages VALUES (?, ?, ?, ?, ?, ?, ?, ?)", (
            meta["id"], meta.get("threadId"), int(meta.get("internalDate", 0)) / 1000,
            json.dumps(labels if labels is not None else meta.get("labelIds", [])), json.dumps(headers),
            meta.get("snippet", ""), meta.get("sizeEstimate", 0), time.time()))


def message(message_id):
    """The cached metadata: {id, thread_id, internal_date, labels, headers: [[name, value]], snippet}."""
    with db() as conn:
        row = conn.execute("SELECT * FROM messages WHERE id = ?", (message_id,)).fetchone()
    if row is None:
        return None
    found = dict(row)
    found["labels"], found["headers"] = json.loads(found["labels"]), json.loads(found["headers"])
    return found


def seen(message_id):
    with db() as conn:
        return conn.execute("SELECT 1 FROM messages WHERE id = ?", (message_id,)).fetchone() is not None


# ------------------------------------------------------------ the event queue

def emit(conn, agent, kind, message_id, thread_id, payload=None):
    conn.execute("INSERT INTO events (agent, kind, message_id, thread_id, payload, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                 (agent, kind, message_id, thread_id, json.dumps(payload or {}), time.time()))


def take(agent, limit=200):
    """The agent's next events, oldest first, marked taken. Each: {id, kind, message_id, thread_id, payload, created_at}."""
    with db() as conn:
        rows = conn.execute("SELECT * FROM events WHERE agent = ? AND taken_at IS NULL ORDER BY id LIMIT ?",
                            (agent, limit)).fetchall()
        if rows:
            conn.execute(f"UPDATE events SET taken_at = ? WHERE id IN ({','.join('?' * len(rows))})",
                         (time.time(), *[r["id"] for r in rows]))
    return [{**dict(r), "payload": json.loads(r["payload"])} for r in rows]


def release(event_ids):
    """Put taken events back in the queue (the agent could not handle them now)."""
    if event_ids:
        with db() as conn:
            conn.execute(f"UPDATE events SET taken_at = NULL WHERE id IN ({','.join('?' * len(event_ids))})",
                         tuple(event_ids))


def queue_depth():
    with db() as conn:
        rows = conn.execute("SELECT agent, COUNT(*) AS n FROM events WHERE taken_at IS NULL GROUP BY agent").fetchall()
    return {r["agent"]: r["n"] for r in rows}


def prune():
    now = time.time()
    with db() as conn:
        conn.execute("DELETE FROM events WHERE taken_at IS NOT NULL AND taken_at < ?", (now - PRUNE_DAYS * 86400,))
        conn.execute("DELETE FROM events WHERE created_at < ?", (now - UNTAKEN_DAYS * 86400,))
        conn.execute("DELETE FROM messages WHERE fetched_at < ?", (now - PRUNE_DAYS * 86400,))
        conn.execute("DELETE FROM routes WHERE created_at < ?", (now - PRUNE_DAYS * 86400,))
    # A message pruned from the cache could otherwise come back as new in a rescan; a rescan
    # never reaches back further than PRUNE_DAYS (poller.RESCAN_MAX_DAYS), so that can't happen.


def size_text():
    try:
        return f"{os.path.getsize(PATH) / 1024 ** 2:.1f} MB"
    except OSError:
        return "0 MB"
