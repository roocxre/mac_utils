"""Weekly cleanup of rebuildable developer clutter under the home folder.

- Python tool caches (__pycache__, .pytest_cache, ...) are removed outright.
- node_modules / target are removed only when they belong to a real project (package.json,
  Cargo.toml or pom.xml alongside) that has not been worked on for 30 days.

~/Library, app bundles, virtualenvs, hidden folders and the folders in SKIP_TOP_LEVEL are never
entered.
"""
import os
import shutil
import time

from lib.common import DRY_RUN, run_main

HOME = os.path.expanduser("~")
STALE_DAYS = 30
ACTIVITY_SCAN_DEPTH = 3

# Defaults: folders in the home folder never searched (media libraries, stale_archiver's ~/Archive,
# Go's module cache in ~/go, and cloud-synced folders whose files may only be placeholders).
SKIP_TOP_LEVEL = {"Library", "Applications", "Movies", "Music", "Pictures", "Public", "Archive", "go"}
SKIP_TOP_LEVEL_PREFIXES = ("Creative Cloud Files",)
CACHE_DIRS = {"__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache"}
# Dependency/build folder -> files that prove the parent is a project that can rebuild it.
DEPENDENCY_DIRS = {"node_modules": ("package.json",), "target": ("Cargo.toml", "pom.xml")}


def last_project_activity(project_dir):
    """Newest modification time in the project's own files (and .git), a few levels deep."""
    newest = 0
    stack = [(project_dir, 0)]
    while stack:
        directory, depth = stack.pop()
        try:
            entries = list(os.scandir(directory))
        except OSError:
            continue
        for entry in entries:
            if entry.name in DEPENDENCY_DIRS or entry.name in CACHE_DIRS:
                continue
            if entry.name.startswith(".") and entry.name != ".git":
                continue
            try:
                newest = max(newest, entry.stat(follow_symlinks=False).st_mtime)
                # .git's own timestamp moves on every commit, checkout or status; no need to enter it.
                if entry.name != ".git" and depth < ACTIVITY_SCAN_DEPTH and entry.is_dir(follow_symlinks=False):
                    stack.append((entry.path, depth + 1))
            except OSError:
                continue
    return newest


def should_skip(parent, name, path):
    if parent == HOME and (name in SKIP_TOP_LEVEL or name.startswith(SKIP_TOP_LEVEL_PREFIXES)):
        return True
    if name.startswith(".") or name == "site-packages" or name.endswith((".app", ".photoslibrary")):
        return True
    return os.path.exists(os.path.join(path, "pyvenv.cfg"))


def purge(path, reason, log):
    if DRY_RUN:
        log.info("would remove %s (%s)", path, reason)
        return
    shutil.rmtree(path, ignore_errors=True)
    if os.path.exists(path):
        log.warning("could not fully remove %s", path)
    else:
        log.info("removed %s (%s)", path, reason)


def main(log):
    cutoff = time.time() - STALE_DAYS * 86400
    for root, dirs, _ in os.walk(HOME, topdown=True):
        for name in list(dirs):
            path = os.path.join(root, name)
            if os.path.islink(path):
                dirs.remove(name)
            elif name in CACHE_DIRS:
                purge(path, "cache", log)
                dirs.remove(name)
            elif name in DEPENDENCY_DIRS:
                # Never descend into these, whether or not they get removed.
                dirs.remove(name)
                is_project = any(os.path.exists(os.path.join(root, marker)) for marker in DEPENDENCY_DIRS[name])
                if is_project:
                    activity = last_project_activity(root)
                    if activity < cutoff:
                        idle_days = (time.time() - activity) / 86400
                        purge(path, f"project idle {idle_days:.0f} days", log)
            elif should_skip(root, name, path):
                dirs.remove(name)
    log.info("finished%s", " (dry run)" if DRY_RUN else "")


if __name__ == "__main__":
    run_main("dev_cleaner", main)
