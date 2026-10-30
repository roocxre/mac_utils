"""Shows messages that other programs drop in the inbox as banners in the corner of the screen.

Any program (a script, a scheduled job, a server) can put up a banner by writing one small
JSON file per message into the inbox folder, state_path("inbox"), which is
~/Library/Application Support/MacUtils/state/inbox/. This agent shows each as a banner that
stays until you click it (a few at a time, in order, stacked), and deletes the message once
dismissed. Banners that were up when this agent or the Mac restarted come back. While do not
disturb is on (atlas/dnd.py) nothing is shown: banners are taken down and every message waits
until it ends. Senders don't need this agent to be running: messages simply wait.

A message file, UTF-8 JSON:

    {"sender": "Backups", "text": "Nightly backup finished: 2 files skipped.", "ts": 1767225600}

    sender  optional: the banner's title, shown with the time ("Backups · 9:00 PM"); default "someone"
    text    required: the banner's text; line breaks are kept
    ts      optional: when it was sent, in Unix seconds; default the file's modification time

Control characters are removed, the sender is cut to 60 characters and the text to 600. Only
names ending in .json are read, in name order (start them with a timestamp to keep messages in
order), and never names starting with ".": write the file under such a name, then rename it,
so a half-written message is never read. A file that can't be read or has no text, or whose
banner could not be shown after 3 tries, moves to failed/ inside the inbox. From a shell:

    inbox="$HOME/Library/Application Support/MacUtils/state/inbox"; mkdir -p "$inbox"
    echo '{"sender": "Backups", "text": "Done"}' > "$inbox/.new" && mv "$inbox/.new" "$inbox/$(date +%s)-$$.json"

Every few seconds this agent also writes the do not disturb state to dnd.json in the state
folder, {"unreachable": true or false, "ts": <Unix seconds>}, a status file other programs may
read to tell whether a message would be seen now (it never says why; after 20 seconds it is stale).
"""
import json
import os
import time
import unicodedata

from atlas import dnd
from lib.common import banner, run_main, state_path

INBOX = state_path("inbox")
FAILED = os.path.join(INBOX, "failed")
POLL_SECONDS = 1
MAX_TEXT = 600
MAX_NAME = 60
MAX_ATTEMPTS = 3
DND_CHECK_SECONDS = 3
MAX_SHOWN = 4          # banners up at once; the rest wait their turn


def clean(text, limit, keep_newlines):
    """Drop control and formatting characters (including bidi overrides) and cap the length."""
    out = []
    for ch in str(text):
        if ch == "\n" and keep_newlines:
            out.append(ch)
        elif ch in "\t\r\n":
            out.append(" ")
        elif not unicodedata.category(ch).startswith("C"):
            out.append(ch)
    cleaned = "".join(out).strip()
    return cleaned[:limit - 1] + "…" if len(cleaned) > limit else cleaned


def parse(path):
    """Return (sender, text, ts) or None if the file isn't a usable message."""
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        sender = clean(data.get("sender") or "someone", MAX_NAME, keep_newlines=False) or "someone"
        text = clean(data["text"], MAX_TEXT, keep_newlines=True)
        ts = float(data.get("ts") or os.path.getmtime(path))
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return None
    return (sender, text, ts) if text else None


def move_to_failed(path):
    os.makedirs(FAILED, exist_ok=True)
    try:
        os.replace(path, os.path.join(FAILED, os.path.basename(path)))
    except OSError:
        pass


def show(sender, text, ts):
    """Put the banner up. Returns its process (still running until dismissed), or None."""
    received = time.strftime("%-I:%M %p", time.localtime(ts))
    return banner(f"{sender} · {received}", text)


def main(log):
    os.makedirs(INBOX, exist_ok=True)
    attempts = {}
    showing = {}   # file name -> (banner process, sender, length)
    log.info("started; inbox is %s", INBOX)
    quiet, checked = False, 0.0
    while True:
        if time.time() - checked >= DND_CHECK_SECONDS:
            checked = time.time()
            active, reasons = dnd.publish()
            if active != quiet:
                quiet = active
                log.info("do not disturb on: %s", ", ".join(reasons)) if quiet else log.info("do not disturb off")
                if quiet:
                    # Take down what is showing; the messages stay in the inbox for later.
                    for process, _sender, _length in showing.values():
                        process.terminate()
                    showing.clear()
        if quiet:
            time.sleep(POLL_SECONDS)
            continue
        for name, (process, sender, length) in list(showing.items()):
            status = process.poll()
            if status is None:
                continue
            del showing[name]
            path = os.path.join(INBOX, name)
            if status == 0:
                log.info("dismissed: message from %s (%d characters)", sender, length)
                attempts.pop(name, None)
                try:
                    os.remove(path)
                except OSError:
                    pass
            else:
                attempts[name] = attempts.get(name, 0) + 1
                if attempts[name] >= MAX_ATTEMPTS:
                    log.warning("could not show %s after %d tries, moved to failed/", name, MAX_ATTEMPTS)
                    move_to_failed(path)

        names = sorted(n for n in os.listdir(INBOX) if n.endswith(".json") and not n.startswith("."))
        for name in names:
            if name in showing:
                continue
            if len(showing) >= MAX_SHOWN:
                break
            path = os.path.join(INBOX, name)
            message = parse(path)
            if message is None:
                log.warning("unreadable message %s, moved to failed/", name)
                move_to_failed(path)
                continue
            sender, text, ts = message
            process = show(sender, text, ts)
            if process is None:
                attempts[name] = attempts.get(name, 0) + 1
                if attempts[name] >= MAX_ATTEMPTS:
                    log.warning("could not show %s after %d tries, moved to failed/", name, MAX_ATTEMPTS)
                    move_to_failed(path)
                continue
            log.info("showing: message from %s (%d characters)", sender, len(text))
            showing[name] = (process, sender, len(text))
        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    run_main("message_popup", main)
