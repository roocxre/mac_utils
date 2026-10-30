"""Sundays at 9: the inbox unsubscribe agent's weekly analysis (unsub/analyze.py), then a banner
with the report's counts. Capture runs all week inside the mail poller (agents/mail_poller.py).

Asleep at 9, it runs when the Mac wakes. Not signed in to Gmail: it logs that and stops.
"""
from lib.common import run_main
from mail import auth
from unsub import analyze


def main(log):
    if not auth.health().get("signed_in"):
        log.warning("not signed in to Gmail; no analysis")
        return
    log.info("%s", analyze.analyze(log, notify=True))


if __name__ == "__main__":
    run_main("unsubscriber", main)
