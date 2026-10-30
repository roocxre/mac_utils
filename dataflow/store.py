"""The helper's SQLite database: dataflow.db in the state folder (readable only by you).

    sites       per-site mode (observe, block, strict) and whether the overlay shows
    policies    one row per site: the policy's address, its text hash, signals, analysis
    canaries    queued, running and finished canary tests (alias: the throwaway address typed)
    pages       what the extension saw on each page (fields, their risk, recipients), 90 days
    recipients  per site: every third party seen listening, reading or sending, merged
    blocked     what the extension blocked or warned about
    exposures   broker scripts reported to the broker agent (once per site and broker)
"""
import json
import os
import sqlite3
import time
from contextlib import contextmanager

from lib.common import state_path

PATH = os.environ.get("MACUTILS_DATAFLOW_DB") or state_path("dataflow.db")     # the override is for tests

SCHEMA = """
CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS sites (
    domain TEXT PRIMARY KEY, mode TEXT, overlay INTEGER DEFAULT 1, updated_at REAL);
CREATE TABLE IF NOT EXISTS policies (
    site TEXT PRIMARY KEY, url TEXT, candidates TEXT, text_hash TEXT, status TEXT, signals TEXT, result TEXT,
    queued_at REAL, started_at REAL, fetched_at REAL, analyzed_at REAL, error TEXT);
CREATE TABLE IF NOT EXISTS canaries (
    id INTEGER PRIMARY KEY AUTOINCREMENT, site TEXT, url TEXT, field TEXT, field_type TEXT, alias TEXT,
    status TEXT, why TEXT, queued_at REAL, started_at REAL, ran_at REAL, result TEXT, error TEXT);
CREATE INDEX IF NOT EXISTS canaries_site ON canaries (site);
CREATE TABLE IF NOT EXISTS pages (
    url TEXT PRIMARY KEY, site TEXT, seen_at REAL, risk TEXT, fields TEXT, recipients TEXT);
CREATE INDEX IF NOT EXISTS pages_seen ON pages (seen_at);
CREATE TABLE IF NOT EXISTS recipients (
    site TEXT, domain TEXT, entity TEXT, categories TEXT, broker TEXT, actions TEXT, field_types TEXT,
    last_seen REAL, PRIMARY KEY (site, domain));
CREATE TABLE IF NOT EXISTS blocked (
    id INTEGER PRIMARY KEY AUTOINCREMENT, site TEXT, domain TEXT, kind TEXT, detail TEXT, at REAL);
CREATE INDEX IF NOT EXISTS blocked_site ON blocked (site);
CREATE TABLE IF NOT EXISTS exposures (
    site TEXT, broker TEXT, domain TEXT, at REAL, PRIMARY KEY (site, broker));
"""

_ready = set()


@contextmanager
def db():
    """A connection that commits on success, rolls back on error, and is always closed."""
    new = not os.path.exists(PATH)
    conn = sqlite3.connect(PATH, timeout=15)
    conn.row_factory = sqlite3.Row
    try:
        if PATH not in _ready:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript(SCHEMA)
            _ready.add(PATH)
            if new:
                os.chmod(PATH, 0o600)
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


def loads(text, default=None):
    try:
        return json.loads(text) if text else default
    except ValueError:
        return default


# ------------------------------------------------------------ site settings

def modes():
    """{domain: mode} for every site whose mode isn't the default, and the settings version."""
    with db() as conn:
        rows = conn.execute("SELECT domain, mode, overlay FROM sites").fetchall()
    return ({r["domain"]: r["mode"] for r in rows if r["mode"]},
            [r["domain"] for r in rows if r["overlay"] == 0], get("settings_version", 0))


def set_site(domain, mode=None, overlay=None):
    with db() as conn:
        row = conn.execute("SELECT * FROM sites WHERE domain = ?", (domain,)).fetchone()
        current_mode = row["mode"] if row else None
        current_overlay = row["overlay"] if row else 1
        conn.execute("INSERT OR REPLACE INTO sites (domain, mode, overlay, updated_at) VALUES (?, ?, ?, ?)",
                     (domain, mode if mode is not None else current_mode,
                      (1 if overlay else 0) if overlay is not None else current_overlay, time.time()))
        version = (loads(conn.execute("SELECT value FROM kv WHERE key = 'settings_version'").fetchone()["value"])
                   if conn.execute("SELECT 1 FROM kv WHERE key = 'settings_version'").fetchone() else 0) + 1
        conn.execute("INSERT OR REPLACE INTO kv (key, value) VALUES ('settings_version', ?)", (json.dumps(version),))
    return version


def site_mode(domain):
    from dataflow import DEFAULT_MODE
    with db() as conn:
        row = conn.execute("SELECT mode FROM sites WHERE domain = ?", (domain,)).fetchone()
    return (row["mode"] if row and row["mode"] else None) or DEFAULT_MODE


def prune(days=90):
    cutoff = time.time() - days * 86400
    with db() as conn:
        conn.execute("DELETE FROM pages WHERE seen_at < ?", (cutoff,))
        conn.execute("DELETE FROM blocked WHERE at < ?", (cutoff,))
        conn.execute("DELETE FROM recipients WHERE last_seen < ?", (cutoff,))
