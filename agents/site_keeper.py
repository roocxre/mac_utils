"""Partner of site_monitor.py: each reloads the other if it is unloaded, until manage.py uninstalls
the pair. See lib/buddy.py."""
import time

from lib import buddy
from lib.common import run_main


def main(log):
    buddy.stay_alive("sitekeeper", "sitemonitor", log)
    log.info("started")
    while True:
        time.sleep(3600)


if __name__ == "__main__":
    run_main("site_keeper", main)
