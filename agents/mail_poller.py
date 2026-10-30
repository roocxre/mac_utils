"""Every 5 minutes: one Gmail history poll for all the email agents (mail/poller.py), then each
installed agent takes what the poll queued for it.

Not signed in yet: `.venv/bin/python -m mail setup CLIENT_FILE`. Until then each run only notes
that (once a day in the log).
"""
import time

from lib import components
from lib.common import run_main
from mail import poller, store


def _unsub_capture(log):
    from unsub import capture
    capture.run(log)


def _broker_mail(log):
    from brokers import replies
    replies.capture(log)


# The agents that read the queue, in order: (component, name, function taking the logger). Each
# one's own job is cheap when its queue is empty; one that isn't installed is skipped.
CONSUMERS = [("unsub", "unsub capture", _unsub_capture), ("brokers", "broker mail", _broker_mail)]


def main(log):
    result = poller.poll_once(log)
    if "error" in result:
        if time.time() - (store.get("logged_error_at") or 0) > 86400 or store.get("logged_error") != result["error"]:
            log.warning("%s", result["error"])
            store.put("logged_error_at", time.time())
            store.put("logged_error", result["error"])
        return
    store.put("logged_error", None)
    busy = {k: v for k, v in result.items() if v}
    if busy:
        log.info("poll: %s", busy)
    for component, name, consume in CONSUMERS:
        if not components.installed(component):
            continue
        try:
            consume(log)
        except Exception:
            log.exception("%s failed", name)


if __name__ == "__main__":
    run_main("mail_poller", main)
