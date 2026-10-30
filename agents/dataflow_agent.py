"""Every hour: the where-does-my-email-go helper's housekeeping (dataflow/). Refreshes the public
lists once a week (Tracker Radar, Disconnect, EasyPrivacy, the Public Suffix List) and rebuilds the
domain index; runs the policy analyses and canary tests the extension queued while Edge was closed
(the native host runs them itself while Edge is open); drops pages older than 90 days.
"""
from lib.common import run_main
from dataflow import lists, store
from dataflow.__main__ import work


def main(log):
    if lists.due():
        log.info("lists: %s", lists.refresh(log=log))
    ran = work(log)
    if ran:
        log.info("ran %d queued job(s)", ran)
    store.prune(lists.config()["pages_keep_days"])


if __name__ == "__main__":
    run_main("dataflow_agent", main)
