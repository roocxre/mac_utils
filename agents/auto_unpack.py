"""Unpacks archives that land in ~/Downloads into a folder of the same name, then trashes them.

Triggered by launchd when ~/Downloads changes. An archive that fails to extract is recorded
and left alone rather than retried on every change.
"""
import os
import shutil
import tempfile
import time

from lib.common import DRY_RUN, run, run_main, state_path, trash, unique_path

DOWNLOADS_DIR = os.path.expanduser("~/Downloads")
HANDLED_LOG = state_path("unpack_handled.txt")
ARCHIVE_EXTENSIONS = (".zip", ".tar.gz", ".tgz", ".tar.bz2", ".tbz2", ".tar.xz", ".txz", ".tar")
EXTRACT_TIMEOUT = 1800


def fingerprint(path):
    """Identifies one specific download, so a new file reusing a name is still unpacked."""
    stat = os.stat(path)
    return f"{path}\t{stat.st_size}\t{int(stat.st_mtime)}"


def load_handled():
    try:
        with open(HANDLED_LOG, encoding="utf-8") as f:
            return {line.rstrip("\n") for line in f if line.strip()}
    except OSError:
        return set()


def mark_handled(entry):
    with open(HANDLED_LOG, "a", encoding="utf-8") as f:
        f.write(entry + "\n")


def is_file_ready(path):
    """A download still being written is empty or still growing."""
    try:
        size = os.path.getsize(path)
        time.sleep(1)
        return size > 0 and size == os.path.getsize(path)
    except OSError:
        return False


def strip_extension(name):
    for ext in ARCHIVE_EXTENSIONS:
        if name.lower().endswith(ext):
            return name[: -len(ext)]
    return name


def unpack(path, log):
    """Extract into a fresh folder next to the archive. Returns True on success."""
    target = unique_path(os.path.join(DOWNLOADS_DIR, strip_extension(os.path.basename(path))))
    # Extract somewhere hidden first so a failed or half-finished extraction leaves nothing behind.
    staging = tempfile.mkdtemp(prefix=".unpacking-", dir=DOWNLOADS_DIR)
    try:
        if path.lower().endswith(".zip"):
            # ditto preserves macOS metadata that unzip drops.
            cmd = ["ditto", "-x", "-k", path, staging]
        else:
            cmd = ["tar", "-xf", path, "-C", staging]
        result = run(cmd, timeout=EXTRACT_TIMEOUT)
        if result is None or result.returncode != 0:
            detail = result.stderr.strip().splitlines()[-1:] if result else ["timed out"]
            log.warning("could not extract %s: %s", os.path.basename(path), " ".join(detail))
            return False

        entries = [e for e in os.listdir(staging) if e != "__MACOSX"]
        only = os.path.join(staging, entries[0]) if len(entries) == 1 else None
        if only and os.path.isdir(only) and not os.path.islink(only):
            # The archive already wraps everything in one folder; don't nest it in another.
            os.rename(only, target)
        else:
            shutil.rmtree(os.path.join(staging, "__MACOSX"), ignore_errors=True)
            os.rename(staging, target)
            os.chmod(target, 0o755)
        log.info("unpacked %s -> %s", os.path.basename(path), os.path.basename(target))
        return True
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def main(log):
    handled = load_handled()
    for name in sorted(os.listdir(DOWNLOADS_DIR)):
        path = os.path.join(DOWNLOADS_DIR, name)
        if name.startswith(".") or not name.lower().endswith(ARCHIVE_EXTENSIONS) or not os.path.isfile(path):
            continue
        entry = fingerprint(path)
        if entry in handled or not is_file_ready(path):
            continue
        if DRY_RUN:
            log.info("would unpack %s", name)
            continue
        if unpack(path, log) and trash(path):
            continue
        # Failed to extract, or extracted but could not be trashed: don't touch it again.
        mark_handled(entry)


if __name__ == "__main__":
    run_main("auto_unpack", main)
