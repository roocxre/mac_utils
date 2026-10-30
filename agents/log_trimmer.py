"""Weekly trim of ~/Library/Logs: delete old crash reports and empty runaway log files."""
import os
import time

from lib.common import DRY_RUN, run_main

LOG_DIR = os.path.expanduser("~/Library/Logs")
MAX_LOG_BYTES = 50 * 1024 * 1024
CRASH_RETENTION_DAYS = 7
CRASH_EXTENSIONS = (".crash", ".ips", ".diag", ".hang", ".spin")
LOG_EXTENSIONS = (".log", ".err", ".out", ".txt")


def main(log):
    crash_cutoff = time.time() - CRASH_RETENTION_DAYS * 86400
    deleted = truncated = 0
    for root, _, files in os.walk(LOG_DIR):
        for name in files:
            path = os.path.join(root, name)
            try:
                stat = os.lstat(path)
                if os.path.islink(path):
                    continue
                if name.endswith(CRASH_EXTENSIONS) and stat.st_mtime < crash_cutoff:
                    if not DRY_RUN:
                        os.remove(path)
                    deleted += 1
                elif name.lower().endswith(LOG_EXTENSIONS) and stat.st_size > MAX_LOG_BYTES:
                    log.info("%s %s (%.0f MB)", "would empty" if DRY_RUN else "emptying",
                             path, stat.st_size / 1e6)
                    if not DRY_RUN:
                        # Truncate in place so a process holding the file open keeps writing to it.
                        os.truncate(path, 0)
                    truncated += 1
            except OSError as error:
                log.warning("skipped %s: %s", path, error)
    log.info("%scrash reports deleted: %d, logs emptied: %d",
             "(dry run) " if DRY_RUN else "", deleted, truncated)


if __name__ == "__main__":
    run_main("log_trimmer", main)
