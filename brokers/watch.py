"""What the broker agent is doing, for the watch window ("brokers debug": atlas/broker_watch.py).

Every process of the agent (the brokeragent pass, capture inside the mail poller, a command run by
hand) appends one JSON line per step to broker-watch.jsonl: what it's doing, never your details (it
names a field, "first name", not what went into it).

While the window is open it keeps WATCHING fresh, and only then does the agent's headless browser
stream what it shows: Chromium's screencast, each frame written over FRAME. The window deletes the
frame when it closes, as a filled-in form on screen holds your details.
"""
import base64
import json
import logging
import os
import time

from lib.common import state_path
from lib.identity import notification

EVENTS = state_path("broker-watch.jsonl")
FRAME = state_path("broker-watch.jpg")
WATCHING = state_path("broker-watch.on")
OPEN_WATCH = notification("brokers.watch")       # a distributed notification: open the window
MAX_BYTES = 1_000_000
KEEP_LINES = 2000
FRESH_SECONDS = 15


def say(text, kind="step", broker=None, url=None):
    """One line for the window. kind: pass, step, mail, send, form, stop, done, warn, error."""
    line = {"at": time.time(), "pid": os.getpid(), "kind": kind, "text": str(text)[:500]}
    if broker:
        line["broker"] = broker
    if url:
        line["url"] = url[:300]
    try:
        with open(EVENTS, "a", encoding="utf-8") as f:
            f.write(json.dumps(line) + "\n")
        if os.path.getsize(EVENTS) > MAX_BYTES:
            _trim()
    except OSError:
        pass


def _trim():
    with open(EVENTS, encoding="utf-8") as f:
        lines = f.readlines()[-KEEP_LINES:]
    tmp = EVENTS + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.writelines(lines)
    os.replace(tmp, EVENTS)


def watching():
    """True while the watch window is open (it touches WATCHING every few seconds)."""
    try:
        return time.time() - os.path.getmtime(WATCHING) < FRESH_SECONDS
    except OSError:
        return False


def _frame(data):
    tmp = FRAME + ".tmp"
    with open(tmp, "wb") as f:
        f.write(data)
    os.replace(tmp, FRAME)


def screencast(context, page):
    """Stream the page to the window while it's open. Frames arrive whenever the page changes."""
    if not watching():
        return
    try:
        cdp = context.new_cdp_session(page)

        def on_frame(params):
            try:
                if watching():
                    _frame(base64.b64decode(params["data"]))
                cdp.send("Page.screencastFrameAck", {"sessionId": params["sessionId"]})
            except Exception:
                pass

        cdp.on("Page.screencastFrame", on_frame)
        cdp.send("Page.startScreencast", {"format": "jpeg", "quality": 70, "maxWidth": 1280, "maxHeight": 1000})
    except Exception as error:
        say(f"no live view: {type(error).__name__}", "warn")


class Handler(logging.Handler):
    """The agent's own log lines, in the window too."""

    def emit(self, record):
        kind = {"WARNING": "warn", "ERROR": "error", "CRITICAL": "error"}.get(record.levelname, "step")
        try:
            say(record.getMessage(), kind)
        except Exception:
            pass


def attach(log):
    if not any(isinstance(h, Handler) for h in log.handlers):
        log.addHandler(Handler(logging.WARNING))      # info lines would repeat what say() already shows
