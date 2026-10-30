"""What a broker had on you, from the files it sends: a data-access or portability answer
(a your_data.json, say) is kept in the vault with the reply it came with, and JSON and CSV
files are read into what kinds of data they held, filled-in fields only.

Other files (PDF, spreadsheets, zips) are kept as they are and never opened here.
"""
import csv
import io
import json
import re
import time

from brokers import vault

MAX_BYTES = 10 * 1024 * 1024
MAX_PARSE = 2 * 1024 * 1024
MAX_VALUES = 400
# Kinds of data by field name (the last part of its path, lower case), tried in this order: the
# prefixed interest and purchase flags first (buy_home_gard is a purchase, not a home).
KINDS = [
    ("purchases", r"^buy_|purchase"),
    ("interests", r"^int_|^read_|^ent_|^life_|^mags$|hob"),
    ("politics", r"party|donr_pol"),
    ("donations", r"donr|donor"),
    ("name", r"(?:^|_)(?:first|last|middle|full|given|family|sur)_?name$|^name$"),
    ("email address", r"e_?mail(?!_?md5|_?sha|_?hash)"),
    ("phone", r"phone|mobile|cell|telephone"),
    ("postal address", r"addr|street|^city$|^state$|zip|postal|dpbc|carrier_route|walk_seq"),
    ("birth date or age", r"dob|birth|(?:^|_)age$|exact_age"),
    ("gender", r"gender|(?:^|_)sex$"),
    ("income and net worth", r"income|net_worth|wealth"),
    ("credit and loans", r"credit|^cc_|loan|lendr|^mr2?_|^p[12]_"),
    ("investments", r"invest"),
    ("home and property", r"home|dwell|^prop_|length_of_residence|property"),
    ("household and children", r"kids|child|adults|hh_size|generations|marital|parent|_in_hh"),
    ("job and education", r"^occ|occupation|education|employer|^job"),
    ("location codes", r"(?:^|_)lat|lon(?:g|$)|fips|county|census|msa|cbsa|geo"),
    ("sign-up record (IP, site, date)", r"signup|(?:^|_)ip(?:_|$)|user_agent|device|cookie"),
    ("email activity", r"mailbox|bounce|is_bot|md5|sha|hash"),
]
# How they're listed: most sensitive first.
SHOWN = ["name", "email address", "phone", "postal address", "birth date or age", "gender", "income and net worth",
         "credit and loans", "investments", "home and property", "household and children", "politics", "donations",
         "job and education", "purchases", "interests", "location codes", "sign-up record (IP, site, date)",
         "email activity", "other"]


def _filled(value):
    return value not in (None, "", [], {}) and str(value).strip() != ""


def _flatten(value, path="", out=None):
    out = [] if out is None else out
    if isinstance(value, dict):
        for key, inner in value.items():
            _flatten(inner, f"{path}.{key}" if path else str(key), out)
    elif isinstance(value, list) and value and all(isinstance(v, (dict, list)) for v in value):
        for index, inner in enumerate(value):
            _flatten(inner, f"{path}[{index}]", out)
    else:
        out.append((path, ", ".join(map(str, value)) if isinstance(value, list) else value))
    return out


def _kind(path):
    key = re.sub(r"\[\d+\]", "", path).rpartition(".")[2].lower()
    for kind, pattern in KINDS:
        if re.search(pattern, key):
            return kind
    return "other"


def read(filename, mime, data):
    """{"fields": n, "filled": n, "kinds": [...], "values": [[field, value], …]}, or None when the
    file isn't JSON or CSV (or can't be read)."""
    name = (filename or "").lower()
    if len(data) > MAX_PARSE:
        return None
    try:
        text = data.decode("utf-8-sig")
        if name.endswith(".json") or "json" in (mime or ""):
            pairs = _flatten(json.loads(text))
        elif name.endswith(".csv") or "csv" in (mime or ""):
            rows = list(csv.reader(io.StringIO(text)))
            if len(rows) < 2:
                return None
            pairs = [(f"{header}" if len(rows) == 2 else f"[{index}].{header}", value)
                     for index, row in enumerate(rows[1:]) for header, value in zip(rows[0], row)]
        else:
            return None
    except (UnicodeDecodeError, ValueError, csv.Error):
        return None
    # A top-level wrapper ({"status": …, "data": {…}}) isn't one of their fields.
    pairs = [(re.sub(r"^data\.", "", path), value) for path, value in pairs if path != "status"]
    filled = [(path, value) for path, value in pairs if _filled(value)]
    kinds = []
    for path, _value in filled:
        kind = _kind(path)
        if kind not in kinds:
            kinds.append(kind)
    return {"fields": len(pairs), "filled": len(filled), "kinds": sorted(kinds, key=SHOWN.index),
            "values": [[path, str(value)[:300]] for path, value in filled[:MAX_VALUES]]}


def _inline(part):
    """A picture drawn inside the email (a logo, a bounce notice's icon), not a file they sent."""
    names = {h["name"].lower(): h["value"].lower() for h in part.get("headers") or []}
    return "content-id" in names or names.get("content-disposition", "").startswith("inline")


def _parts(payload):
    """The attachments in a message payload, at any depth."""
    if payload.get("filename") and payload.get("body", {}).get("attachmentId") and not _inline(payload):
        yield payload
    for part in payload.get("parts") or []:
        yield from _parts(part)


def save(api, reply_id, broker_id, raw, log=None):
    """Keep every attachment of one broker email. Returns how many were new."""
    import base64
    saved = 0
    for part in _parts(raw.get("payload", {})):
        size = part["body"].get("size") or 0
        with vault.db() as conn:
            if conn.execute("SELECT 1 FROM files WHERE message_id = ? AND filename = ?",
                            (raw["id"], part["filename"])).fetchone():
                continue
        if size > MAX_BYTES:
            data, summary = None, None
        else:
            body = api.call("GET", f"/messages/{raw['id']}/attachments/{part['body']['attachmentId']}", "attachments.get")
            data = base64.urlsafe_b64decode(body["data"] + "==")
            summary = read(part["filename"], part.get("mimeType"), data)
        with vault.db() as conn:
            conn.execute("INSERT OR IGNORE INTO files (broker_id, reply_id, message_id, filename, mime, size, data, "
                         "held, saved_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                         (broker_id, reply_id, raw["id"], part["filename"], part.get("mimeType"), size, data,
                          json.dumps(summary) if summary else None, time.time()))
            what = (f"{summary['filled']} of {summary['fields']} fields filled: " + ", ".join(summary["kinds"])) \
                if summary else ("too big to keep, still in Gmail" if data is None else "kept")
            vault.history(conn, broker_id, f"file: {part['filename']}", what)
        saved += 1
        if log:
            log.info("%s: kept %s (%s)", broker_id, part["filename"], what)
    return saved


def files(conn, broker_id, values=True):
    """The broker's files for the board: name, date, size, what they held (values only when asked)."""
    out = []
    for row in conn.execute("SELECT filename, size, held, saved_at, data IS NOT NULL AS kept FROM files WHERE "
                            "broker_id = ? ORDER BY saved_at", (broker_id,)):
        held = json.loads(row["held"]) if row["held"] else None
        item = {"filename": row["filename"], "size": row["size"], "kept": bool(row["kept"]),
                "date": time.strftime("%Y-%m-%d", time.localtime(row["saved_at"]))}
        if held:
            item.update(fields=held["fields"], filled=held["filled"], kinds=held["kinds"])
            if values:
                item["values"] = held["values"]
        out.append(item)
    return out


def backfill(log=None, api=None):
    """Once: the attachments of every broker email already in the vault."""
    from mail import gmail
    api = api or gmail.client()
    with vault.db() as conn:
        replies = [dict(r) for r in conn.execute("SELECT id, broker_id, message_id FROM replies")]
    done = {"replies": 0, "files": 0, "gone": 0}
    for reply in replies:
        try:
            raw = api.message(reply["message_id"], "full")
        except gmail.NotFound:
            done["gone"] += 1
            continue
        done["files"] += save(api, reply["id"], reply["broker_id"], raw, log)
        done["replies"] += 1
    return done
