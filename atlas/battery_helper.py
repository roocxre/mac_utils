"""Battery: what is drawing power, how long is left, and a power-saving mode that undoes itself.

Opening it (`battery helper`) first quits apps that have sat unused for over a day, then shows
the dashboard (atlas/battery_dashboard.py). Power saving (`power saving on|off`) is one switch:

    on   quits the idle apps (if that hasn't just been done), turns on Low Power Mode on battery,
         sends the display to sleep after 2 idle minutes on battery, turns off Power Nap on
         battery and dims the screen to at most half brightness
    off  puts every one of those settings back as it was (quit apps stay quit)

"Unused" comes from UsageTracker, which notes the app you are in every 15 seconds. An app counts
as idle when it has not been in front for IDLE_HOURS, and it is not playing or recording audio,
not keeping the mac awake, not busy right now, and not one you work in without looking at it
(terminals and the like, in KEEP). Only Dock apps are quit, politely (unsaved work still asks).

The pmset settings need root. They go through a sudoers rule that allows exactly those settings
and nothing else (SUDOERS_RULE, installed once by hand with SETUP_COMMAND); without it, power
saving still dims the screen and quits the idle apps, and says what it skipped.
"""
import ctypes
import ctypes.util
import getpass
import json
import os
import re
import signal
import subprocess
import threading
import time

from atlas import memory_helper
from lib.common import state_path

IDLE_HOURS = 24
BUSY_CPU = 3.0                  # % CPU right now that counts as working, whatever the history says
IDLE_RERUN_SECONDS = 1800       # power saving reruns the idle quit only if it is older than this
SAVING = {"lowpowermode": 1, "displaysleep": 2, "powernap": 0}      # pmset -b values while saving
SAVING_BRIGHTNESS = 0.5
# Apps you work in without them being in front, or that hold a session open: never idle.
KEEP = {"finder", "dock", "terminal", "iterm", "iterm2", "warp", "ghostty", "kitty", "alacritty",
        "wezterm", "hyper", "tabby", "screen sharing", "1password", "bitwarden", "keychain access",
        "tailscale", "amphetamine", "pock", "xcode", "simulator", "docker", "docker desktop", "utm",
        "parallels desktop", "virtualbox", "vmware fusion", "obs"}
_STAY_AWAKE = {"PreventUserIdleSystemSleep", "PreventUserIdleDisplaySleep", "PreventSystemSleep",
               "NoIdleSleepAssertion", "NoDisplaySleepAssertion"}

# Rough shape of an Apple silicon laptop's draw, for the estimate only.
DISPLAY_FLOOR_W = 0.5           # panel on at the lowest brightness
DISPLAY_RANGE_W = 4.5           # extra at full brightness
BASE_W = 1.2                    # everything that keeps running however idle the mac is
LOW_POWER_SHARE = 0.12          # of the busy part of the draw, saved by Low Power Mode
MIN_W = 1.5

USER = getpass.getuser()
# No dot in the name: sudo skips files in sudoers.d whose names contain one.
SUDOERS_FILE = "/etc/sudoers.d/macutils-power"
# Exact values only (no * in the arguments, which in sudoers would also match extra arguments).
SUDOERS_RULE = (f"{USER} ALL=(root) NOPASSWD: /usr/bin/pmset -b lowpowermode [01], /usr/bin/pmset -b powernap [01], "
                "/usr/bin/pmset -b displaysleep [0-9], /usr/bin/pmset -b displaysleep [0-9][0-9], "
                "/usr/bin/pmset -b displaysleep [0-9][0-9][0-9]")
SETUP_COMMAND = (f"echo '{SUDOERS_RULE}' | sudo tee {SUDOERS_FILE} >/dev/null && sudo chmod 440 {SUDOERS_FILE} "
                 f"&& sudo visudo -cf {SUDOERS_FILE}")

USAGE_FILE = "app-usage.json"
SAVING_FILE = "power-saving.json"
_lock = threading.Lock()


def _load(name, default):
    try:
        with open(state_path(name)) as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def _save(name, data):
    path = state_path(name)
    with open(path + ".tmp", "w") as f:
        json.dump(data, f)
    os.replace(path + ".tmp", path)


def _run(cmd, timeout=15):
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout).stdout
    except (OSError, subprocess.SubprocessError):
        return ""


def fmt_hours(hours):
    if hours is None:
        return "—"
    minutes = int(round(hours * 60))
    return f"{minutes // 60}h {minutes % 60:02d}m"


def _ago(seconds):
    days = seconds / 86400
    return f"{days:.0f} days" if days >= 1.95 else f"{seconds / 3600:.0f} hours"


# ---------------------------------------------------------------- the battery itself

def _ioreg():
    out = _run(["ioreg", "-rn", "AppleSmartBattery"])
    values = {}
    for key, value in re.findall(r'^\s*"(\w+)" = (\S+)$', out, re.MULTILINE):
        values[key] = value
    return values


def _signed(text):
    value = int(text)
    return value - 2 ** 64 if value >= 2 ** 63 else value


def battery(samples=3):
    """Charge, draw and time left. Draw is averaged over a few readings (one alone jumps about)."""
    readings = []
    for i in range(samples):
        if i:
            time.sleep(0.5)
        values = _ioreg()
        if values:
            readings.append(values)
    if not readings:
        return None
    last = readings[-1]
    volts = int(last.get("Voltage", 0)) / 1000
    amps = sum(_signed(r.get("InstantAmperage", r.get("Amperage", "0"))) for r in readings) / len(readings) / 1000
    left_mah = int(last.get("AppleRawCurrentCapacity", 0))
    full_mah = int(last.get("AppleRawMaxCapacity", 0)) or 1
    plugged = last.get("ExternalConnected") == "Yes"
    info = {
        "percent": int(last.get("CurrentCapacity", 0)),
        "plugged": plugged,
        "charging": last.get("IsCharging") == "Yes",
        "full": last.get("FullyCharged") == "Yes",
        "left_wh": left_mah * volts / 1000,
        "full_wh": full_mah * volts / 1000,
        "watts": max(-amps * volts, 0.0) if not plugged else None,
        "cycles": int(last.get("CycleCount", 0)),
        "health": round(100 * full_mah / int(last.get("DesignCapacity", full_mah) or full_mah)),
        "to_full_minutes": int(last.get("AvgTimeToFull", 65535)),
    }
    if info["watts"] is None or info["watts"] < 0.3:
        # On the adapter (or a reading that caught nothing): use what it drew lately on battery.
        info["watts_source"] = "recent"
        info["watts"] = _load(USAGE_FILE, {}).get("watts")
    else:
        info["watts_source"] = "now"
    return info


# ---------------------------------------------------------------- display brightness (built-in screen)

def _display():
    try:
        services = ctypes.CDLL("/System/Library/PrivateFrameworks/DisplayServices.framework/DisplayServices")
        graphics = ctypes.CDLL(ctypes.util.find_library("CoreGraphics"))
        graphics.CGMainDisplayID.restype = ctypes.c_uint32
        for display in [graphics.CGMainDisplayID()] + list(range(1, 4)):
            if graphics.CGDisplayIsBuiltin(ctypes.c_uint32(display)):
                return services, ctypes.c_uint32(display)
    except OSError:
        pass
    return None, None


def brightness():
    """The built-in screen's brightness, 0..1, or None (lid closed, or no built-in screen)."""
    services, display = _display()
    if services is None:
        return None
    value = ctypes.c_float()
    if services.DisplayServicesGetBrightness(display, ctypes.byref(value)) != 0:
        return None
    return float(value.value)


def set_brightness(value):
    services, display = _display()
    if services is None:
        return False
    services.DisplayServicesSetBrightness.argtypes = [ctypes.c_uint32, ctypes.c_float]
    return services.DisplayServicesSetBrightness(display, ctypes.c_float(value)) == 0


# ---------------------------------------------------------------- who is drawing power

def power_by_process():
    """{pid: (energy impact, cpu %)} from a second `top` sample (the first has no rates yet)."""
    out = _run(["top", "-l", "2", "-s", "1", "-o", "power", "-n", "200", "-stats", "pid,power,cpu"], timeout=20)
    sample = out.split("PID")[-1].splitlines()[1:] if "PID" in out else []
    found = {}
    for line in sample:
        parts = line.split()
        if len(parts) == 3 and parts[0].isdigit():
            try:
                found[int(parts[0])] = (float(parts[1]), float(parts[2]))
            except ValueError:
                continue
    return found


def _programs():
    """{pid: the program's own path}; unlike the command line it is not split by spaces in a path."""
    out = _run(["ps", "-U", str(os.getuid()), "-axo", "pid=,comm="])
    found = {}
    for line in out.splitlines():
        pid, _, program = line.strip().partition(" ")
        if pid.isdigit():
            found[int(pid)] = program.strip()
    return found


def ranked(power=None):
    """Your processes, grouped by app, most power first, plus the share macOS itself is using.

    Returns (items, system_share). Each item: name, kind (app|background|process), impact, cpu,
    share (of all energy impact), pids, and app (an NSRunningApplication for Dock apps)."""
    power = power if power is not None else power_by_process()
    apps = {e["path"]: e for e in memory_helper.running_apps()}
    me = {os.getpid()}
    groups, total, system = {}, 0.0, 0.0
    rows = memory_helper._ps()
    owned = {pid for pid, _pp, _r, _c in rows}       # this user's
    programs = _programs()
    for pid, _ppid, _rss, command in rows:
        impact, cpu = power.get(pid, (0.0, 0.0))
        total += impact
        match = memory_helper._APP_PATH.match(command)
        program = programs.get(pid, "")
        if match:
            path = match.group(1)
        else:   # a bare name (aslmanager, the Core Audio drivers) is a part of macOS run as you
            path = program if "/" in program else command.split()[0] if command.startswith("/") else ""
        if not path or path.startswith(memory_helper._SYSTEM_PREFIXES + ("/Library/SystemExtensions/",)) \
                or pid in me or memory_helper.ROOT in command or "/.vscode/" in command \
                or "Python.framework" in command or os.path.basename(path) in ("top", "ps"):
            system += impact
            continue
        if match:
            app = apps.get(path)
            name, kind = (app["name"], "app") if app else (os.path.basename(path)[:-4], "background")
        else:
            name, kind, app = os.path.basename(path), "process", None
        if name.lower() in memory_helper.NEVER:
            system += impact
            continue
        entry = groups.setdefault(path, {"name": name, "kind": kind, "impact": 0.0, "cpu": 0.0, "pids": [],
                                         "app": app["app"] if app else None, "path": path})
        entry["impact"] += impact
        entry["cpu"] += cpu
        entry["pids"].append(pid)
    # Everything not yours (WindowServer, kernel_task and the daemons) is macOS too.
    system += sum(impact for pid, (impact, _cpu) in power.items() if pid not in owned)
    total += sum(impact for pid, (impact, _cpu) in power.items() if pid not in owned)
    items = sorted(groups.values(), key=lambda e: (-e["impact"], -e["cpu"], e["name"].lower()))
    for entry in items:
        entry["share"] = entry["impact"] / total if total else 0.0
    return items, (system / total if total else 0.0)


def quit_item(item):
    """Quit one dashboard row. Dock apps are asked politely; anything else gets SIGTERM."""
    if item.get("app") is not None:
        item["app"].terminate()
        return f"asked {item['name']} to quit"
    for pid in item["pids"]:
        try:
            os.kill(pid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            pass
    return f"stopped {item['name']}"


# ---------------------------------------------------------------- idle apps

class UsageTracker:
    """Notes which app is in front, and what the mac draws on battery. tick() every ~15 seconds."""

    SAVE_EVERY = 300

    def __init__(self):
        self.data = _load(USAGE_FILE, {})
        self.data.setdefault("since", time.time())
        self.data.setdefault("seen", {})
        self.saved = 0.0

    def tick(self):
        from AppKit import NSWorkspace

        now = time.time()
        front = NSWorkspace.sharedWorkspace().frontmostApplication()
        if front is not None and front.processIdentifier() != os.getpid() and front.bundleURL() is not None:
            self.data["seen"][str(front.bundleURL().path())] = now
        info = battery(samples=1)
        if info and info["watts_source"] == "now":
            last = self.data.get("watts")
            self.data["watts"] = info["watts"] if last is None else 0.95 * last + 0.05 * info["watts"]
        if now - self.saved >= self.SAVE_EVERY:
            with _lock:
                fresh = _load(USAGE_FILE, {})
                self.data["seen"] = {**fresh.get("seen", {}), **self.data["seen"]}
                _save(USAGE_FILE, self.data)
            self.saved = now


def _awake_pids():
    """Processes holding the mac (or its display) awake: a download, a render, a presentation."""
    out = _run(["pmset", "-g", "assertions"])
    return {int(pid) for pid, kind in re.findall(r"pid (\d+)\([^)]*\):\s+\[\S+\]\s+\S+\s+(\w+)", out)
            if kind in _STAY_AWAKE}


def idle_apps(power=None):
    """Dock apps unused for IDLE_HOURS that nothing is depending on. [(entry, idle seconds)]."""
    usage = _load(USAGE_FILE, {})
    since, seen = usage.get("since", time.time()), usage.get("seen", {})
    now = time.time()
    power = power if power is not None else power_by_process()
    audio = memory_helper.audio_active_apps()
    awake = _awake_pids()
    front = memory_helper.front_pids()
    rows = memory_helper._ps()
    found = []
    for entry in memory_helper.running_apps():
        name = entry["name"].lower()
        app = entry["app"]
        if name in KEEP or name in memory_helper.NEVER or app.processIdentifier() == os.getpid():
            continue
        launched = app.launchDate()
        launched = launched.timeIntervalSince1970() if launched is not None else since
        last = max(seen.get(entry["path"], 0), launched, since)
        if now - last < IDLE_HOURS * 3600:
            continue
        pids = {pid for pid, _pp, _r, command in rows if command.startswith(entry["path"] + "/")}
        busy = sum(power.get(pid, (0.0, 0.0))[1] for pid in pids)
        if entry["path"] in audio or pids & awake or pids & front or busy >= BUSY_CPU:
            continue
        if name == "discord" and memory_helper.discord_in_call():
            continue
        found.append((dict(entry, pids=sorted(pids)), now - last))
    return sorted(found, key=lambda pair: -pair[1])


def quit_idle(power=None):
    """Quit the idle apps. Returns [(name, idle seconds)] asked to quit."""
    asked = []
    for entry, idle in idle_apps(power):
        entry["app"].terminate()
        asked.append((entry["name"], idle))
    state = _load(SAVING_FILE, {})
    state["idle_quit_at"] = time.time()
    state["idle_quit"] = [name for name, _idle in asked]
    _save(SAVING_FILE, state)
    return asked


def idle_text(asked):
    if not asked:
        return f"no app has sat unused for {IDLE_HOURS} hours"
    return "quit " + ", ".join(f"{name} (unused {_ago(idle)})" for name, idle in asked)


# ---------------------------------------------------------------- power saving

def _pmset_battery():
    """The battery profile's current pmset values, as ints."""
    out = _run(["pmset", "-g", "custom"])
    section = out.split("Battery Power:")[-1].split("AC Power:")[0] if "Battery Power:" in out else ""
    return {key: int(value) for key, value in re.findall(r"^\s*(\w+)\s+(\d+)\s*$", section, re.MULTILINE)}


def can_set_power():
    """True when the sudoers rule is in place (sudo -n then works without a password)."""
    result = subprocess.run(["sudo", "-n", "-l", "/usr/bin/pmset", "-b", "lowpowermode", "1"],
                            capture_output=True, text=True)
    return result.returncode == 0


def _pmset(key, value):
    result = subprocess.run(["sudo", "-n", "/usr/bin/pmset", "-b", key, str(value)], capture_output=True, text=True)
    return result.returncode == 0


def saving_on():
    return bool(_load(SAVING_FILE, {}).get("on"))


def plan_saving():
    """What turning power saving on would change now: [(what, from, to)]."""
    changes = []
    current = _pmset_battery()
    labels = {"lowpowermode": "Low Power Mode", "displaysleep": "display sleep", "powernap": "Power Nap"}
    for key, wanted in SAVING.items():
        have = current.get(key)
        if key == "displaysleep":
            if have is not None and (have == 0 or have > wanted):
                changes.append((labels[key], f"{have or 'never'} min" if have else "never", f"{wanted} min"))
        elif have is not None and have != wanted:
            changes.append((labels[key], "on" if have else "off", "on" if wanted else "off"))
    level = brightness()
    if level is not None and level > SAVING_BRIGHTNESS + 0.02:
        changes.append(("brightness", f"{level:.0%}", f"{SAVING_BRIGHTNESS:.0%}"))
    return changes


def turn_on():
    """Turn power saving on. Returns a sentence saying what happened."""
    with _lock:
        state = _load(SAVING_FILE, {})
        if state.get("on"):
            return "power saving is already on"
    notes = []
    if time.time() - state.get("idle_quit_at", 0) > IDLE_RERUN_SECONDS:
        notes.append(idle_text(quit_idle()))
    with _lock:
        state = _load(SAVING_FILE, {})
        saved, changed, skipped = {}, [], []
        current = _pmset_battery()
        root = can_set_power()
        for key, wanted in SAVING.items():
            have = current.get(key)
            if have is None or have == wanted or (key == "displaysleep" and 0 < have <= wanted):
                continue
            if root and _pmset(key, wanted):
                saved[key] = have
                changed.append(key)
            else:
                skipped.append(key)
        level = brightness()
        if level is not None and level > SAVING_BRIGHTNESS + 0.02 and set_brightness(SAVING_BRIGHTNESS):
            saved["brightness"] = level
            changed.append("brightness")
        state.update(on=True, saved=saved, at=time.time())
        _save(SAVING_FILE, state)
    words = {"lowpowermode": "Low Power Mode on", "displaysleep": f"display sleeps after {SAVING['displaysleep']} min",
             "powernap": "Power Nap off", "brightness": f"screen dimmed to {SAVING_BRIGHTNESS:.0%}"}
    notes.append("power saving on: " + (", ".join(words[k] for k in changed) if changed else "nothing needed changing"))
    if skipped:
        notes.append("skipped " + ", ".join(words[k] for k in skipped) + " (needs the one-time setup in the battery dashboard)")
    return "\n".join(notes)


def turn_off():
    """Put back everything power saving changed. Quit apps stay quit."""
    with _lock:
        state = _load(SAVING_FILE, {})
        if not state.get("on"):
            return "power saving is already off"
        saved = state.get("saved", {})
        restored, failed = [], []
        for key, value in saved.items():
            if key == "brightness":
                now = brightness()
                # Only if the screen is still where we put it: a level you chose since stays.
                if now is not None and abs(now - SAVING_BRIGHTNESS) <= 0.03:
                    (restored if set_brightness(value) else failed).append(key)
            elif _pmset(key, value):
                restored.append(key)
            else:
                failed.append(key)
        state.update(on=False, saved={k: v for k, v in saved.items() if k in failed})
        if failed:
            state["on"] = True      # keep what is still to undo, so a second `off` can finish it
        _save(SAVING_FILE, state)
    names = {"lowpowermode": "Low Power Mode", "displaysleep": "display sleep", "powernap": "Power Nap",
             "brightness": "brightness"}
    text = "power saving off: put back " + (", ".join(names[k] for k in restored) if restored else "nothing")
    if failed:
        text += "\ncould not put back " + ", ".join(names[k] for k in failed) + " (sudo rule missing?)"
    return text


def status_text():
    if not saving_on():
        return "power saving is off"
    saved = _load(SAVING_FILE, {}).get("saved", {})
    return "power saving is on" + (f" (changed: {', '.join(saved)})" if saved else "")


# ---------------------------------------------------------------- the estimate

def estimate(info, items, idle_names=()):
    """(hours now, hours with power saving, watts now, watts with saving). Hours are None without a draw."""
    watts = info.get("watts") if info else None
    if not watts:
        return None, None, None, None
    left = info["left_wh"]
    level = brightness()
    display = DISPLAY_FLOOR_W + DISPLAY_RANGE_W * level if level is not None else 0.0
    busy = max(watts - display - BASE_W, 0.0)
    saving = 0.0
    if not saving_on():
        if level is not None and level > SAVING_BRIGHTNESS:
            saving += DISPLAY_RANGE_W * (level - SAVING_BRIGHTNESS)
        quit_share = sum(item["share"] for item in items if item["name"] in idle_names)
        saving += busy * quit_share
        if _pmset_battery().get("lowpowermode") != 1:
            saving += busy * (1 - quit_share) * LOW_POWER_SHARE
    after = max(watts - saving, min(watts, MIN_W))
    return left / watts, left / after, watts, after


def summary_text():
    """One look at the battery, for the prompt: the same numbers the dashboard shows."""
    info = battery()
    if info is None:
        return "no battery found"
    items, _system = ranked()
    idle = [entry["name"] for entry, _s in idle_apps()]
    now, saved, watts, after = estimate(info, items, idle)
    state = "charging" if info["charging"] else "on the adapter" if info["plugged"] else "on battery"
    lines = [f"battery {info['percent']}%, {state}, health {info['health']}%, {info['cycles']} cycles"]
    if now is not None:
        source = "" if info["watts_source"] == "now" else " (its recent draw on battery)"
        lines.append(f"left at {watts:.1f} W{source}: {fmt_hours(now)}; with power saving: about {fmt_hours(saved)}")
    lines.append(status_text())
    lines.append("using the most power: " + ", ".join(f"{i['name']} {i['share']:.0%}" for i in items[:5] if i["impact"] > 0))
    return "\n".join(lines)
