"""Relaunches Pock (the Touch Bar dock) if it has died. Pass --force to restart it regardless.
Does nothing unless Pock is installed in /Applications."""
import os
import sys
import time

from lib.common import run, run_main

POCK_APP_PATH = "/Applications/Pock.app"


def is_running(process_name):
    result = run(["pgrep", "-x", process_name], timeout=5)
    return result is not None and result.returncode == 0


def restart_pock(log):
    run(["killall", "Pock"], timeout=5)
    # Restarting the Control Strip resets a stuck Touch Bar. (TouchBarServer itself belongs to
    # root and can't be restarted from a user agent.)
    run(["killall", "ControlStrip"], timeout=5)
    time.sleep(1)
    result = run(["open", "-g", "-a", POCK_APP_PATH], timeout=15)
    if result is None or result.returncode != 0:
        log.warning("could not launch Pock: %s", result.stderr.strip() if result else "timed out")
    else:
        log.info("relaunched Pock")


def main(log):
    if not os.path.exists(POCK_APP_PATH):
        return
    if "--force" in sys.argv or not is_running("Pock"):
        restart_pock(log)


if __name__ == "__main__":
    run_main("pock_keeper", main)
