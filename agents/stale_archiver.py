"""Nightly tidy of ~/Desktop and ~/Downloads.

- Anything untouched for 14 days moves to ~/Archive/YYYY-MM (installers go to the Trash).
- Abandoned partial downloads older than 2 days go to the Trash.
- Desktop screenshots move to ~/Archive/Screenshots every night.

"Untouched" looks at when an item was modified, created, moved in or last opened, so a file
copied in yesterday with an old modification date is not swept away.
"""
import os
import shutil
import time
from datetime import datetime

from lib.common import DRY_RUN, run_main, trash, unique_path

# Defaults: the folders tidied, and where archived items go.
DESKTOP_DIR = os.path.expanduser("~/Desktop")
SCAN_DIRS = [DESKTOP_DIR, os.path.expanduser("~/Downloads")]
ARCHIVE_BASE = os.path.expanduser("~/Archive")
SCREENSHOT_ARCHIVE = os.path.join(ARCHIVE_BASE, "Screenshots")

STALE_DAYS = 14
PARTIAL_DAYS = 2
EXCLUDE_EXTENSIONS = {".app", ".localized"}
INSTALLER_EXTENSIONS = {".dmg", ".pkg"}
PARTIAL_EXTENSIONS = {".crdownload", ".part", ".download", ".tmp"}
SCREENSHOT_EXTENSIONS = {".png", ".jpg", ".jpeg", ".heic"}
# Leave one taken moments before the nightly run where it is until the next night.
SCREENSHOT_GRACE_SECONDS = 3600


def last_touched(path):
    """Most recent sign of use: modified, created, or changed (moving or opening bumps this)."""
    stat = os.lstat(path)
    newest = max(stat.st_mtime, stat.st_ctime, getattr(stat, "st_birthtime", 0))
    if os.path.isdir(path) and not os.path.islink(path):
        # A folder's own times don't move when a file inside it is edited.
        try:
            with os.scandir(path) as children:
                for child in children:
                    child_stat = child.stat(follow_symlinks=False)
                    newest = max(newest, child_stat.st_mtime, child_stat.st_ctime)
        except OSError:
            pass
    return newest


def is_screenshot(name):
    return (os.path.splitext(name)[1].lower() in SCREENSHOT_EXTENSIONS
            and ("Screenshot" in name or "Screen Shot" in name))


def archive_screenshots(log):
    now = time.time()
    for entry in sorted(os.scandir(DESKTOP_DIR), key=lambda e: e.name):
        if not entry.is_file(follow_symlinks=False) or not is_screenshot(entry.name):
            continue
        try:
            if now - entry.stat().st_mtime < SCREENSHOT_GRACE_SECONDS:
                continue
            if DRY_RUN:
                log.info("would archive screenshot %s", entry.name)
                continue
            os.makedirs(SCREENSHOT_ARCHIVE, exist_ok=True)
            destination = unique_path(os.path.join(SCREENSHOT_ARCHIVE, entry.name))
            shutil.move(entry.path, destination)
            log.info("archived %s -> %s", entry.path, destination)
        except OSError as error:
            log.warning("skipped %s: %s", entry.path, error)


def process_directory(target_dir, log):
    now = time.time()
    month_folder = os.path.join(ARCHIVE_BASE, datetime.now().strftime("%Y-%m"))

    for entry in sorted(os.scandir(target_dir), key=lambda e: e.name):
        ext = os.path.splitext(entry.name)[1].lower()
        if entry.name.startswith(".") or ext in EXCLUDE_EXTENSIONS:
            continue
        try:
            age_days = (now - last_touched(entry.path)) / 86400
            if ext in PARTIAL_EXTENSIONS:
                action = "trash" if age_days > PARTIAL_DAYS else None
            elif age_days <= STALE_DAYS:
                action = None
            else:
                action = "trash" if ext in INSTALLER_EXTENSIONS else "archive"
            if action is None:
                continue

            if DRY_RUN:
                log.info("would %s %s (%.0f days)", action, entry.path, age_days)
            elif action == "trash":
                if trash(entry.path):
                    log.info("trashed %s", entry.path)
                else:
                    log.warning("could not trash %s", entry.path)
            else:
                os.makedirs(month_folder, exist_ok=True)
                destination = unique_path(os.path.join(month_folder, entry.name))
                shutil.move(entry.path, destination)
                log.info("archived %s -> %s", entry.path, destination)
        except OSError as error:
            log.warning("skipped %s: %s", entry.path, error)


def main(log):
    if os.path.isdir(DESKTOP_DIR):
        archive_screenshots(log)
    for target_dir in SCAN_DIRS:
        if os.path.isdir(target_dir):
            process_directory(target_dir, log)


if __name__ == "__main__":
    run_main("stale_archiver", main)
