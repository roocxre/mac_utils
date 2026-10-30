"""The identity vault, the disclosure ledger, and the agent's own tables: one SQLCipher database
(brokers.db in the state folder), encrypted whole. Its key is 32 random bytes in the login
Keychain (service KEY_SERVICE), made on first use; without the key the file is noise.

    identity   your details, entered in the dashboard's "Your details" window: legal name and
               variants, addresses (current and past), phones, emails, date of birth, relatives
               (relatives are only for matching listings; never sent to anyone)
    ledger     one row per detail sent: which value went to which broker, when, how, for which request
    brokers    each broker's state (plan.STATES), tier, scan result, approval and timers
    requests   every request: email or form, what it sent, its Gmail thread
    replies    broker mail, classified (replies.py)
    listings   what a people-search site shows, with a screenshot (scan.py)
    tasks      manual tasks: CAPTCHAs, ID checks, phone calls, possible matches, unclear replies
    history    every state change and event, per broker
    reports, items   the weekly reports and their entries
    exposure   tier-1 signals: broker scripts the browser extension (dataflow/) saw on pages where
               you typed your email
    oneoffs    the one-time federal and industry opt-outs you have ticked off
"""
import json
import secrets
import time
from contextlib import contextmanager
from datetime import date

from lib import keychain
from lib.common import state_path
from lib.identity import keychain_service

PATH = state_path("brokers.db")
KEY_SERVICE = keychain_service("brokers")
KEY_ACCOUNT = "vault-key"

SCHEMA = """
CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS identity (field TEXT PRIMARY KEY, value TEXT, updated_at REAL);
CREATE TABLE IF NOT EXISTS ledger (
    id INTEGER PRIMARY KEY AUTOINCREMENT, request_id INTEGER, broker_id TEXT, element TEXT, value TEXT,
    method TEXT, sent_at REAL);
CREATE INDEX IF NOT EXISTS ledger_broker ON ledger (broker_id);
CREATE TABLE IF NOT EXISTS brokers (
    id TEXT PRIMARY KEY, state TEXT DEFAULT 'discovered', state_at REAL, tier INTEGER, listed TEXT DEFAULT 'unknown',
    scanned_at REAL, next_scan_at REAL, approved TEXT, approved_at REAL, acknowledged_at REAL, verify_at REAL,
    recheck_at REAL, follow_up_at REAL, followed_up INTEGER DEFAULT 0, detail TEXT, broken TEXT,
    pending TEXT, rejected_at REAL);
CREATE TABLE IF NOT EXISTS requests (
    id INTEGER PRIMARY KEY AUTOINCREMENT, broker_id TEXT, kind TEXT, method TEXT, elements TEXT, status TEXT,
    auto INTEGER DEFAULT 0, queued_at REAL, sent_at REAL, to_address TEXT, subject TEXT, gmail_id TEXT,
    thread_id TEXT, message_id_header TEXT, detail TEXT);
CREATE INDEX IF NOT EXISTS requests_broker ON requests (broker_id);
CREATE INDEX IF NOT EXISTS requests_status ON requests (status);
CREATE TABLE IF NOT EXISTS replies (
    id INTEGER PRIMARY KEY AUTOINCREMENT, broker_id TEXT, request_id INTEGER, message_id TEXT UNIQUE, thread_id TEXT,
    received_at REAL, sender TEXT, subject TEXT, text TEXT, signed_by TEXT, matched_by TEXT, label TEXT,
    confidence REAL, asks TEXT, links TEXT, handled TEXT, handled_at REAL);
CREATE TABLE IF NOT EXISTS listings (
    id INTEGER PRIMARY KEY AUTOINCREMENT, broker_id TEXT, url TEXT, fields TEXT, score REAL, verdict TEXT,
    screenshot BLOB, found_at REAL, seen_at REAL, gone_at REAL, UNIQUE (broker_id, url));
CREATE TABLE IF NOT EXISTS tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT, broker_id TEXT, kind TEXT, title TEXT, steps TEXT, link TEXT,
    ref INTEGER, created_at REAL, done_at REAL, outcome TEXT);
CREATE TABLE IF NOT EXISTS history (
    id INTEGER PRIMARY KEY AUTOINCREMENT, broker_id TEXT, at REAL, event TEXT, detail TEXT);
CREATE INDEX IF NOT EXISTS history_broker ON history (broker_id);
CREATE TABLE IF NOT EXISTS reports (id INTEGER PRIMARY KEY AUTOINCREMENT, created_at REAL, summary TEXT);
CREATE TABLE IF NOT EXISTS items (
    id INTEGER PRIMARY KEY AUTOINCREMENT, report_id INTEGER, grp TEXT, broker_id TEXT, task_id INTEGER,
    proposal TEXT, status TEXT DEFAULT 'pending', decided_at REAL);
CREATE INDEX IF NOT EXISTS items_report ON items (report_id, grp);
CREATE TABLE IF NOT EXISTS exposure (
    domain TEXT, source TEXT, detail TEXT, seen_at REAL, PRIMARY KEY (domain, source));
CREATE TABLE IF NOT EXISTS oneoffs (key TEXT PRIMARY KEY, done_at REAL);
CREATE TABLE IF NOT EXISTS files (
    id INTEGER PRIMARY KEY AUTOINCREMENT, broker_id TEXT, reply_id INTEGER, message_id TEXT, filename TEXT, mime TEXT,
    size INTEGER, data BLOB, held TEXT, saved_at REAL, UNIQUE (message_id, filename));
CREATE INDEX IF NOT EXISTS files_broker ON files (broker_id);
"""

# Your details, as the dashboard's form keeps them (every field optional).
IDENTITY_FIELDS = ("first_name", "middle_name", "last_name", "name_variants", "dob", "emails", "phones",
                   "addresses", "relatives")
LIST_FIELDS = ("name_variants", "emails", "phones", "addresses", "relatives")

_ready = set()
_key = {}


class Locked(Exception):
    """The vault's key is not in the Keychain and could not be made."""


def _hex_key():
    if "hex" not in _key:
        found = keychain.get(KEY_SERVICE, KEY_ACCOUNT)
        if not found:
            import os
            if os.path.exists(PATH) and os.path.getsize(PATH) > 0:
                raise Locked("brokers.db exists but its key is gone from the Keychain; the vault can't be opened")
            found = secrets.token_hex(32)
            keychain.put(KEY_SERVICE, KEY_ACCOUNT, found)
        _key["hex"] = found
    return _key["hex"]


@contextmanager
def db():
    """An encrypted connection that commits on success, rolls back on error, and is always closed."""
    import sqlcipher3

    key = _hex_key()                  # before connecting: connecting makes the file
    conn = sqlcipher3.connect(PATH, timeout=15)
    conn.row_factory = sqlcipher3.Row
    try:
        conn.execute(f"PRAGMA key = \"x'{key}'\"")
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


def get(key, default=None):
    with db() as conn:
        row = conn.execute("SELECT value FROM kv WHERE key = ?", (key,)).fetchone()
    return json.loads(row["value"]) if row else default


def put(key, value):
    with db() as conn:
        conn.execute("INSERT OR REPLACE INTO kv (key, value) VALUES (?, ?)", (key, json.dumps(value)))


def history(conn, broker_id, event, detail=""):
    conn.execute("INSERT INTO history (broker_id, at, event, detail) VALUES (?, ?, ?, ?)",
                 (broker_id, time.time(), event, detail if isinstance(detail, str) else json.dumps(detail)))


# ------------------------------------------------------------ your details

def identity():
    """{field: value} with lists for LIST_FIELDS. Addresses: [{street, city, state, zip, current}]."""
    with db() as conn:
        rows = conn.execute("SELECT field, value FROM identity").fetchall()
    found = {r["field"]: json.loads(r["value"]) for r in rows}
    for field in LIST_FIELDS:
        found.setdefault(field, [])
    return found


def save_identity(values):
    """Replace the given fields (the dashboard saves the whole form)."""
    with db() as conn:
        for field, value in values.items():
            if field not in IDENTITY_FIELDS:
                raise ValueError(f"no field {field}")
            conn.execute("INSERT OR REPLACE INTO identity VALUES (?, ?, ?)", (field, json.dumps(value), time.time()))


def has_identity(found=None):
    found = found if found is not None else identity()
    return bool(found.get("first_name") and found.get("last_name") and current_address(found))


def current_address(found):
    addresses = found.get("addresses") or []
    return next((a for a in addresses if a.get("current")), addresses[0] if addresses else None)


def _age(dob):
    try:
        born = date.fromisoformat(dob)
    except (TypeError, ValueError):
        return None
    today = date.today()
    return today.year - born.year - ((today.month, today.day) < (born.month, born.day))


def element_values(found, real_email, listing_urls=()):
    """{element: value} for every element the vault can fill (registry.ELEMENTS). The email is your
    real one, the address the request emails come from, for a form too."""
    here = current_address(found) or {}
    first, last = found.get("first_name", ""), found.get("last_name", "")
    values = {"first_name": first, "last_name": last, "full_name": f"{first} {last}".strip(),
              "email": real_email or "", "city": here.get("city", ""), "state": here.get("state", ""),
              "zip": here.get("zip", ""), "city_state": ", ".join(x for x in (here.get("city"), here.get("state")) if x),
              "address": ", ".join(x for x in (here.get("street"), here.get("city"),
                                                f"{here.get('state', '')} {here.get('zip', '')}".strip()) if x),
              "phone": (found.get("phones") or [""])[0], "dob": found.get("dob", ""),
              "age": str(_age(found.get("dob")) or ""), "listing_url": "\n".join(listing_urls)}
    return {k: v for k, v in values.items() if v}


# ------------------------------------------------------------ the ledger

def record_disclosure(conn, request_id, broker_id, sent, method):
    """sent: {element: value} exactly as it went out."""
    now = time.time()
    for element, value in sent.items():
        conn.execute("INSERT INTO ledger (request_id, broker_id, element, value, method, sent_at) VALUES (?, ?, ?, ?, ?, ?)",
                     (request_id, broker_id, element, value, method, now))


def ledger(broker_id=None, element=None, offset=0, limit=20):
    """(rows, total), newest first. Values are shown masked; the dashboard shows them whole."""
    where, args = [], []
    if broker_id:
        where.append("broker_id = ?")
        args.append(broker_id)
    if element:
        where.append("element = ?")
        args.append(element)
    clause = (" WHERE " + " AND ".join(where)) if where else ""
    with db() as conn:
        total = conn.execute(f"SELECT COUNT(*) FROM ledger{clause}", args).fetchone()[0]
        rows = conn.execute(f"SELECT * FROM ledger{clause} ORDER BY sent_at DESC, id LIMIT ? OFFSET ?",
                            (*args, limit, offset)).fetchall()
    return [dict(r) for r in rows], total


def mask(element, value):
    """A detail as the commands show it: enough to tell which, not the thing itself."""
    value = str(value or "")
    if element in ("email",) and "@" in value:
        local, domain = value.split("@", 1)
        return f"{local[:2]}…@{domain}"
    if element in ("phone",):
        return "…" + value[-4:]
    if element in ("dob",):
        return value[:4] + "-…"
    if element in ("listing_url",):
        return f"{value.count(chr(10)) + 1} listing link(s)"
    if element in ("address",):
        return "street address"
    return value if element in ("state", "city", "city_state", "first_name", "last_name", "full_name", "age") else "…"
