r"""The email layer's operations, for other agents (as functions) and for you (as text:
atlas/mail_commands.py and `python -m mail`). Every answer is compact JSON.

    mail status
    mail setup file=~/Downloads/client_secret_….json
"""
import json
import re
import time

from mail import auth, poller, store

_ARG = re.compile(r"""(\w+)\s*=\s*(?:"([^"]*)"|'([^']*)'|(\S+))""")
_SPAN = re.compile(r"^(\d+)\s*([hdwmy])\w*$", re.IGNORECASE)
_SPAN_SECONDS = {"h": 3600, "d": 86400, "w": 7 * 86400, "m": 30 * 86400, "y": 365 * 86400}


class Problem(Exception):
    """A request that can't be carried out as written; the text says why."""


def as_json(value):
    return json.dumps(value, separators=(",", ":"), ensure_ascii=False, default=str)


def _since(value):
    if not value:
        return None
    match = _SPAN.match(value.strip())
    if match:
        return time.time() - int(match.group(1)) * _SPAN_SECONDS[match.group(2).lower()]
    try:
        return time.mktime(time.strptime(value.strip(), "%Y-%m-%d"))
    except ValueError:
        raise Problem(f"since={value} is not a time: write 7d, 2w, 3m or 2026-10-01")


# ------------------------------------------------------------ the operations (Python API)

def status():
    last = store.get("last_poll_at")
    return {
        "auth": {k: v for k, v in auth.health().items() if v not in (None, [])},
        "last_poll_min_ago": round((time.time() - last) / 60) if last else None,
        "last_poll": store.get("last_poll"),
        "last_poll_error": store.get("last_poll_error"),
        "history_id": store.get("history_id"),
        "history_id_age_h": round((time.time() - last) / 3600, 1) if last else None,
        "queue": store.queue_depth(),
        "db": store.size_text(),
    }


def gmail_client():
    """The shared client wrapper (internal; not one of the commands)."""
    from mail import gmail
    return gmail.client()


def message_meta(message_id):
    """Cached metadata for a message (store.message format), fetched from Gmail if not cached."""
    from mail import gmail

    meta = store.message(message_id)
    if meta is None:
        try:
            store.save_message(gmail.client().message(message_id, "metadata"))
        except gmail.NotFound:
            return None
        meta = store.message(message_id)
    return meta


# ------------------------------------------------------------ the text form

OPS = {
    # name: (keys, first key, looks only, summary)
    "mail status": ((), None, True, "Gmail sign-in, last poll, queues"),
    "mail setup": (("file",), "file", False, "sign in to Gmail with your OAuth client file"),
}
_CALL = re.compile(r"^mail(?:[ _](\w+))?\b\s*(.*)$", re.IGNORECASE | re.DOTALL)


def parse(text):
    """(op, {key: value}) for an email-layer command, or None if the text is not one. Raises Problem."""
    match = _CALL.match(text.strip())
    if not match:
        return None
    op, rest = f"mail {(match.group(1) or 'status').lower()}", match.group(2)
    if op not in OPS:
        return None
    keys, first_key, _looks, _summary = OPS[op]
    found = list(_ARG.finditer(rest))
    args = {m.group(1).lower(): next(g for g in m.groups()[1:] if g is not None) for m in found}
    loose = _ARG.sub(" ", rest).strip().strip("\"'")
    if loose:
        if not first_key or first_key in args:
            raise Problem(f"{op} does not take {loose!r}. it takes: {', '.join(k + '=' for k in keys) or 'nothing'}")
        args[first_key] = loose
    unknown = [k for k in args if k not in keys]
    if unknown:
        raise Problem(f"{op} has no {unknown[0]}=. it takes: {', '.join(k + '=' for k in keys) or 'nothing'}")
    return op, args


def looks_only(op, args=None):
    return OPS[op][2]


def run(op, args):
    """Carry out a parsed command. Returns compact JSON (or a short problem message)."""
    try:
        if op == "mail status":
            result = status()
        elif op == "mail setup":
            if not args.get("file"):
                raise Problem("which file? mail setup file=~/Downloads/client_secret_….json (Google Cloud > "
                              "Credentials > OAuth client ID > Desktop app > Download JSON)")
            try:
                result = auth.setup(args["file"])
            except (OSError, ValueError, PermissionError, TimeoutError) as error:
                raise Problem(f"setup failed: {error}")
        else:
            raise Problem(f"no operation {op}")
    except Problem as problem:
        return str(problem)
    except auth.NotSignedIn as error:
        return as_json({"error": str(error)})
    return as_json(result)


def call(text):
    """Parse and run one command line. Returns its answer as text."""
    try:
        parsed = parse(text)
    except Problem as problem:
        return str(problem)
    if parsed is None:
        return "not an email command. " + usage()
    return run(*parsed)


def usage():
    return "commands: " + " · ".join(f"{op} {' '.join(k + '=' for k in spec[0])}".strip() for op, spec in OPS.items())


def poll(log):
    return poller.poll_once(log)
