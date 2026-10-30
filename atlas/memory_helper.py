"""Frees memory: quits apps that can always go, and lists what else is using it.

Apps on ALWAYS_QUIT are asked to quit (so anything with unsaved work still prompts) without
asking you. Everything else is only listed, biggest first, for you to pick. Leftover dev
processes (orphaned node/python/etc whose terminal is gone) are listed too, flagged.
Finder and the Dock are never touched, and only the always-quit list is quit without asking.
An always-quit app is left alone while it is playing or recording audio (or on a call), and
both runs (`free up memory` and the memory-pressure watcher) also leave the app you are in at
that moment, looking past the assistant's own windows to the app behind it.
"""
import ctypes
import ctypes.util
import os
import re
import signal
import subprocess
import time

from AppKit import NSApplicationActivationPolicyRegular, NSWorkspace

# Matched against the app's display name, case-insensitively. Edit freely.
ALWAYS_QUIT = {
    # media and chat
    "spotify", "music", "podcasts", "tv", "news", "messages", "facetime", "discord", "slack",
    "telegram", "whatsapp", "signal", "zoom.us",
    # read-only system utilities
    "activity monitor", "calendar", "system settings", "system information", "console",
    "font book", "contacts", "maps", "weather", "stocks", "clock", "calculator", "dictionary",
    "tips", "books", "image capture", "photo booth", "disk utility", "digital color meter",
}
# Never offered for quitting. Browsers and editors are listed (you pick them; they quit politely,
# except FORCE_KILL below).
NEVER = {"finder", "dock"}
# Picked from the list, these are killed outright (every process in the bundle) instead of asked:
# asked politely, they come straight back.
FORCE_KILL = {"microsoft edge"}
ORPHAN_NAMES = {"node", "python", "python3", "ruby", "java", "npm", "yarn", "pnpm", "deno", "bun",
                "vite", "next-server", "esbuild", "webpack", "uvicorn", "gunicorn", "flask"}
_SHELLS = {"sh", "zsh", "bash", "dash", "fish"}
MIN_LISTED_MB = 20          # regular apps below this aren't worth a line
MAX_LISTED = 40
_SYSTEM_PREFIXES = ("/System/", "/Library/Apple/", "/usr/", "/bin/", "/sbin/")
PRESSURE_COOLDOWN_SECONDS = 600
# This project's folder: its own processes (the agents, their banners) run from here and are never offered.
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_libc = ctypes.CDLL(ctypes.util.find_library("c"))


def _mb(kb):
    return kb / 1024


def _fmt(mb):
    return f"{mb / 1024:.1f} GB" if mb >= 1024 else f"{mb:.0f} MB"


def _ps():
    """[(pid, ppid, rss_kb, command)] for this user's processes."""
    out = subprocess.run(["ps", "-U", str(os.getuid()), "-axo", "pid=,ppid=,rss=,command="],
                         capture_output=True, text=True, timeout=10).stdout
    rows = []
    for line in out.splitlines():
        parts = line.split(None, 3)
        if len(parts) == 4 and parts[0].isdigit():
            rows.append((int(parts[0]), int(parts[1]), int(parts[2]), parts[3]))
    return rows


_APP_PATH = re.compile(r"^(.*?\.app)/")


def running_apps():
    """{display name: {'app': NSRunningApplication, 'mb': total MB, 'procs': n}} for regular apps."""
    apps = {}
    for app in NSWorkspace.sharedWorkspace().runningApplications():
        if app.activationPolicy() != NSApplicationActivationPolicyRegular:
            continue
        name = str(app.localizedName() or "")
        url = app.bundleURL()
        if not name or url is None:
            continue
        apps[str(url.path())] = {"name": name, "path": str(url.path()), "app": app, "mb": 0.0, "procs": 0}
    # Helpers live inside the app bundle, so group every process under its outermost .app.
    for _pid, _ppid, rss, command in _ps():
        match = _APP_PATH.match(command)
        if match and match.group(1) in apps:
            entry = apps[match.group(1)]
            entry["mb"] += _mb(rss)
            entry["procs"] += 1
    return list(apps.values())


def background_items(shown):
    """Helpers, updaters, Finder extensions and menu-bar agents: processes inside an app bundle
    that isn't a regular running app (so not already listed). Grouped by bundle.
    [(name, [pids], mb)]. `shown` is the set of regular-app bundle paths."""
    rows = _ps()
    ours = {pid for pid, ppid, _r, c in rows if ROOT in c or "/.vscode/" in c or "Python.framework" in c}
    groups = {}
    for pid, _ppid, rss, command in rows:
        match = _APP_PATH.match(command)
        if not match or pid in ours or pid == os.getpid():
            continue
        path = match.group(1)
        if path in shown or path.startswith(_SYSTEM_PREFIXES):
            continue
        name = os.path.basename(path)[:-4]
        if name.lower() in NEVER:
            continue
        entry = groups.setdefault(path, [name, [], 0.0])
        entry[1].append(pid)
        entry[2] += _mb(rss)
    return [tuple(v) for v in groups.values()]


def orphans():
    """Dev processes re-parented to launchd (their shell is gone). [(pid, name, mb)]."""
    found = []
    rows = _ps()
    by_pid = {pid: (ppid, command) for pid, ppid, _rss, command in rows}

    def adrift(ppid):
        """True when nothing but shells stands between a process and launchd (sh -c 'x | java …')."""
        for _ in range(8):
            if ppid == 1:
                return True
            parent = by_pid.get(ppid)
            if parent is None or os.path.basename(parent[1].split()[0]).lstrip("-") not in _SHELLS:
                return False
            ppid = parent[0]
        return False

    for pid, ppid, rss, command in rows:
        # Homebrew's python lives in Python.framework/…/Python.app: an interpreter, not an app with a window.
        in_app = ".app/" in command.split(" -")[0] and "Python.framework" not in command
        # Ours, or the parent of something of ours (a launcher may not name the folder itself).
        ours = any(ROOT in c for p, _pp, _r, c in rows if p == pid or _pp == pid)
        if not adrift(ppid) or in_app or ours:
            continue
        name = os.path.basename(command.split()[0]).lower()      # Homebrew's python runs as "Python"
        if re.sub(r"[\d.]+$", "", name) in ORPHAN_NAMES or name in ORPHAN_NAMES:
            found.append((pid, name, _mb(rss)))
    return found


def _is_always(entry):
    return entry["name"].lower() in ALWAYS_QUIT


def _is_never(entry):
    return entry["name"].lower() in NEVER


# Apps that stay open while in use. The signal is macOS's own: while a process has an audio
# device open, coreaudiod holds a power assertion "Created for PID: n" with audio-in/out
# resources (listed by `pmset -g assertions`; no permission needed). That covers Spotify
# playing, a Discord call (mic and speakers), a video, and so on.
_AUDIO_ASSERTION = re.compile(r"Created for PID: (\d+)\.\s*\n\s*Resources:[^\n]*audio-(?:in|out)")


# Calls are carried by a system daemon, so the audio assertion is created for the daemon's pid
# rather than the app's. A call through the daemon keeps its app open.
_CALL_DAEMONS = {"avconferenced": "facetime"}


def audio_active_apps():
    """Set of app bundle paths that currently have an audio device open (or a call in progress)."""
    try:
        out = subprocess.run(["pmset", "-g", "assertions"], capture_output=True, text=True,
                             timeout=10).stdout
    except (OSError, subprocess.SubprocessError):
        return set()
    pids = {int(pid) for pid in _AUDIO_ASSERTION.findall(out)}
    bundles = set()
    call_apps = set()
    for pid, _ppid, _rss, command in _ps():
        match = _APP_PATH.match(command)
        if pid in pids and match:
            bundles.add(match.group(1))
        elif pid in pids and os.path.basename(command.split()[0]) in _CALL_DAEMONS:
            call_apps.add(_CALL_DAEMONS[os.path.basename(command.split()[0])])
    for entry in running_apps():
        if entry["name"].lower() in call_apps:
            bundles.add(entry["path"])
    return bundles


def discord_in_call():
    """A connected UDP socket is what a voice call uses (backup for the audio check)."""
    try:
        out = subprocess.run(["lsof", "-nP", "-a", "-c", "Discord", "-i", "UDP"],
                             capture_output=True, text=True, timeout=10).stdout
    except (OSError, subprocess.SubprocessError):
        return False
    return any("->" in line for line in out.splitlines())


def front_pids():
    """Pids of the app you are in right now: the one with keyboard focus and the one whose
    window is on top (normally the same app). Our own windows (the prompt this runs from)
    don't count: the app you are in is the one behind them."""
    me = os.getpid()
    pids = set()
    front = NSWorkspace.sharedWorkspace().frontmostApplication()
    if front is not None and front.processIdentifier() != me:
        pids.add(front.processIdentifier())
    try:
        import Quartz

        windows = Quartz.CGWindowListCopyWindowInfo(
            Quartz.kCGWindowListOptionOnScreenOnly | Quartz.kCGWindowListExcludeDesktopElements,
            Quartz.kCGNullWindowID)
        for window in windows or []:
            bounds = window.get("kCGWindowBounds") or {}
            if (window.get("kCGWindowLayer") == 0 and window.get("kCGWindowAlpha")
                    and window["kCGWindowOwnerPID"] != me
                    and bounds.get("Width", 0) >= 100 and bounds.get("Height", 0) >= 100):
                pids.add(window["kCGWindowOwnerPID"])
                break
    except Exception:
        pass
    return pids


kept_last_run = []


def _keep(entry, audio_bundles):
    """Whether an always-quit app is in use right now and should be left alone."""
    if entry["path"] in audio_bundles:
        return True
    return entry["name"].lower() == "discord" and discord_in_call()


def quit_always(spare_front=False):
    """Ask every always-quit app to quit, except those in use. Returns [(name, mb)] asked.

    spare_front: also leave the app you are in right now (used by the automatic run).
    """
    asked = []
    kept_last_run.clear()
    audio = audio_active_apps()
    front = front_pids() if spare_front else set()
    for entry in running_apps():
        if _is_always(entry) and not _is_never(entry):
            if _keep(entry, audio) or entry["app"].processIdentifier() in front:
                kept_last_run.append(entry["name"])
                continue
            entry["app"].terminate()
            asked.append((entry["name"], entry["mb"]))
    return asked


def candidates():
    """Biggest apps that are neither always-quit nor protected, plus orphans. List of dicts."""
    items = []
    apps = running_apps()
    for name, pids, mb in background_items({e["path"] for e in apps}):
        items.append({"label": f"background: {name} — {_fmt(mb)}", "kind": "background",
                      "pids": pids, "mb": mb})
    for entry in apps:
        if _is_always(entry) or _is_never(entry) or entry["mb"] < MIN_LISTED_MB:
            continue
        label = f"{entry['name']} — {_fmt(entry['mb'])}"
        if entry["procs"] > 1:
            label += f" ({entry['procs']} processes)"
        items.append({"label": label, "kind": "app", "app": entry["app"], "path": entry["path"],
                      "name": entry["name"], "mb": entry["mb"]})
    items.sort(key=lambda i: -i["mb"])
    for pid, name, mb in sorted(orphans(), key=lambda o: -o[2]):
        items.append({"label": f"leftover: {name} (pid {pid}) — {_fmt(mb)}", "kind": "orphan",
                      "pid": pid, "mb": mb})
    return items[:MAX_LISTED]


def _kill_bundle(path):
    """SIGKILL every process running from inside an app bundle. True if any were found."""
    found = False
    for pid, _ppid, _rss, command in _ps():
        match = _APP_PATH.match(command)
        if match and match.group(1) == path:
            found = True
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
    return found


def stop(item):
    """Quit one listed candidate. Returns a short description."""
    if item["kind"] == "app" and item.get("name", "").lower() in FORCE_KILL:
        return f"killed {item['name']}" if _kill_bundle(item["path"]) else "already gone"
    if item["kind"] == "app":
        item["app"].terminate()
        return f"asked {item['label'].split(' — ')[0]} to quit"
    if item["kind"] == "background":
        for pid in item["pids"]:
            try:
                os.kill(pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
        return f"stopped {item['label'].split(' — ')[0].replace('background: ', '')}"
    try:
        os.kill(item["pid"], signal.SIGTERM)
    except ProcessLookupError:
        return "already gone"
    return f"stopped {item['label'].split(' (pid')[0].replace('leftover: ', '')}"


def report_text(asked):
    kept = f"; kept {', '.join(kept_last_run)} (in use)" if kept_last_run else ""
    if not asked:
        return ("nothing on the always-quit list was running" if not kept else "nothing quit" + kept)
    total = sum(mb for _n, mb in asked)
    names = ", ".join(sorted({n for n, _mb in asked}, key=str.lower))
    return f"quit {names} (about {_fmt(total)})" + kept


def memory_pressure():
    """macOS's own verdict: 1 normal, 2 warning, 4 critical. 1 if it can't be read."""
    value, size = ctypes.c_int(0), ctypes.c_size_t(ctypes.sizeof(ctypes.c_int))
    ok = _libc.sysctlbyname(b"kern.memorystatus_vm_pressure_level", ctypes.byref(value),
                            ctypes.byref(size), None, 0) == 0
    return value.value if ok and value.value else 1


class PressureWatcher:
    """Quits the always-quit apps by itself when macOS reports memory pressure."""

    def __init__(self, get_pressure, on_quit, log):
        self.get_pressure, self.on_quit, self.log = get_pressure, on_quit, log
        self.last = 0.0

    def tick(self):
        if self.get_pressure() < 2 or time.time() - self.last < PRESSURE_COOLDOWN_SECONDS:
            return
        self.last = time.time()
        asked = quit_always(spare_front=True)
        self.log.info("memory pressure: %s", report_text(asked))
        if asked:
            self.on_quit(report_text(asked))
