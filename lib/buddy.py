"""Lets two launchd agents keep each other (and themselves) alive.

Each agent watches its partner and reloads it if it has been unloaded, disabled or had its
plist removed. When an agent is told to stop, it leaves behind a short-lived detached
reviver that reloads both. launchd's own KeepAlive already covers a plain kill.

While the stand-down file (buddy-stand-down in the state folder) exists, nothing is reloaded
or revived. manage.py creates it before uninstalling the pair and deletes it when installing
them, so an uninstall sticks.

    buddy.py revive <name> [<name> ...]   reload the named agents, then exit
"""
import os
import plistlib
import signal
import subprocess
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import manage  # noqa: E402  (the plist definitions live with the installer)
from lib.common import state_path  # noqa: E402

STAND_DOWN = state_path("buddy-stand-down")
WATCH_SECONDS = 5
REVIVE_DELAY_SECONDS = 2
REVIVE_ATTEMPTS = 5


def standing_down():
    return os.path.exists(STAND_DOWN)


def is_loaded(name):
    return manage.launchctl("print", f"{manage.DOMAIN}/{manage.label(name)}").returncode == 0


def ensure_loaded(name, log=None):
    """Reload an agent if launchd no longer has it. Returns True if it had to be restored."""
    # The flag is checked after is_loaded: manage.py makes it before unloading anything.
    if is_loaded(name) or standing_down():
        return False
    expected = plistlib.dumps(manage.build_plist(name, manage.AGENTS[name]))
    path = manage.plist_path(name)
    try:
        with open(path, "rb") as f:
            intact = f.read() == expected
    except OSError:
        intact = False
    if not intact:
        with open(path, "wb") as f:
            f.write(expected)
    manage.launchctl("enable", f"{manage.DOMAIN}/{manage.label(name)}")
    result = manage.launchctl("bootstrap", manage.DOMAIN, path)
    if log:
        log.info("%s was unloaded; %s", name,
                 "reloaded it" if result.returncode == 0 else f"reload failed: {result.stderr.strip()}")
    return True


def spawn_reviver(names):
    """Start a process outside our launchd job that reloads the given agents (not while standing down)."""
    if standing_down():
        return
    subprocess.Popen(
        [sys.executable, os.path.abspath(__file__), "revive", *names],
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        start_new_session=True)  # its own session, so it survives launchd tearing the job down


def stay_alive(own_name, partner_name, log):
    """Call once from an agent's main thread: watch the partner and revive on shutdown."""
    names = [own_name, partner_name]

    def on_stop(signum, _frame):
        if standing_down():
            log.info("asked to stop (signal %d); standing down, so not reviving", signum)
        else:
            log.info("asked to stop (signal %d); leaving a reviver behind", signum)
            spawn_reviver(names)
        os._exit(0)

    for signum in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP, signal.SIGQUIT):
        signal.signal(signum, on_stop)

    def watch():
        while True:
            try:
                ensure_loaded(partner_name, log)
            except Exception:
                log.exception("could not check %s", partner_name)
            time.sleep(WATCH_SECONDS)

    threading.Thread(target=watch, daemon=True).start()


def revive(names):
    # Ignore the signals a bulk kill would send while we wait for launchd to finish unloading.
    for signum in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP, signal.SIGQUIT):
        signal.signal(signum, signal.SIG_IGN)
    for _ in range(REVIVE_ATTEMPTS):
        time.sleep(REVIVE_DELAY_SECONDS)
        if standing_down():
            return
        for name in names:
            try:
                ensure_loaded(name)
            except Exception:
                pass
        if all(is_loaded(name) for name in names):
            return


if __name__ == "__main__":
    if len(sys.argv) > 2 and sys.argv[1] == "revive":
        revive(sys.argv[2:])
