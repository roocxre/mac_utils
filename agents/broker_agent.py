"""Every 10 minutes: one pass of the data broker removal agent (brokers/work.py): broker replies,
one people-search scan when due, the paced sends, follow-ups and re-checks, and on Sundays from 9
the weekly report with a banner. Broker mail itself is captured all along inside the mail poller.

Does nothing until your details are in the vault (the broker dashboard's Your details). Not
signed in to Gmail: it logs that and stops.
"""
import time

from lib.common import run_main
from mail import auth
from brokers import vault, work


def main(log):
    if not auth.health().get("signed_in"):
        log.warning("not signed in to Gmail; no pass")
        return
    result = work.run_pass(log, notify=True)
    if "waiting" in result:
        if time.time() - (vault.get("logged_waiting_at") or 0) > 86400:
            log.info("%s", result["waiting"])
            vault.put("logged_waiting_at", time.time())
        return
    busy = {k: v for k, v in result.items() if v and k not in ("routed_domains", "blocklist")}
    if any(busy.get(k) for k in ("replies", "scanned", "report")) or any((busy.get("sent") or {}).values()) or \
            any((busy.get("timers") or {}).values()):
        log.info("pass: %s", busy)


if __name__ == "__main__":
    run_main("broker_agent", main)
