"""Do not disturb: when popup messages should wait instead of showing up on screen.

It is on by itself while any of these holds (each can be switched off below):
- the screen is being shared or recorded,
- an external display (monitor, projector, AirPlay, Sidecar) is connected,
- the lid is closed while the Mac is kept awake,
- the screen is locked or the display is asleep.

`dnd on` forces it on and `dnd off` forces it off, both until changed; `dnd auto` (the
default) goes back to the rules above.

While it is on, messagepopup holds messages in the inbox and shows them once it ends. The
state is also written to dnd.json in the state folder, for other programs that send you messages
(they can hold theirs, or tell the sender you can't be reached). The file says only that you are
unreachable, never why.
"""
import ctypes
import json
import os
import subprocess
import time

from lib.common import state_path

# The automatic rules. Set one to False to stop it from turning do not disturb on.
WHEN_SCREEN_SHARED = True
WHEN_EXTERNAL_DISPLAY = True
WHEN_LID_CLOSED = True
WHEN_LOCKED_OR_ASLEEP = True

SETTINGS_PATH = state_path("dnd_settings.json")
STATE_PATH = state_path("dnd.json")
MODES = ("auto", "on", "off")
FRESH_SECONDS = 20     # how old dnd.json may be and still be believed by a reader (it is rewritten every few seconds)


def mode():
    try:
        with open(SETTINGS_PATH, encoding="utf-8") as f:
            value = json.load(f).get("mode")
    except (OSError, ValueError, AttributeError):
        return "auto"
    return value if value in MODES else "auto"


def screen_shared():
    """Whether some program is capturing the screen (a share, a recording, remote viewing)."""
    try:
        core_graphics = ctypes.CDLL("/System/Library/Frameworks/CoreGraphics.framework/CoreGraphics")
        check = core_graphics.CGSIsScreenWatcherPresent
        check.restype = ctypes.c_bool
        return bool(check())
    except (OSError, AttributeError):
        return False


def _displays():
    import Quartz

    error, ids, _count = Quartz.CGGetActiveDisplayList(16, None, None)
    return list(ids or []) if error == 0 else []


def external_display():
    import Quartz

    return any(not Quartz.CGDisplayIsBuiltin(display) for display in _displays())


def lid_closed():
    try:
        out = subprocess.run(["ioreg", "-r", "-k", "AppleClamshellState", "-d", "1"],
                             capture_output=True, text=True, timeout=5).stdout
    except (OSError, subprocess.SubprocessError):
        return False
    return '"AppleClamshellState" = Yes' in out


def locked_or_asleep():
    import Quartz

    session = Quartz.CGSessionCopyCurrentDictionary() or {}
    if session.get("CGSSessionScreenIsLocked") or not session.get("kCGSSessionOnConsoleKey", True):
        return True
    displays = _displays()
    return bool(displays) and all(Quartz.CGDisplayIsAsleep(display) for display in displays)


_RULES = (
    (lambda: WHEN_SCREEN_SHARED, screen_shared, "the screen is being shared or recorded"),
    (lambda: WHEN_EXTERNAL_DISPLAY, external_display, "an external display is connected"),
    (lambda: WHEN_LID_CLOSED, lid_closed, "the lid is closed"),
    (lambda: WHEN_LOCKED_OR_ASLEEP, locked_or_asleep, "the screen is locked or asleep"),
)


def automatic_reasons():
    reasons = []
    for enabled, check, reason in _RULES:
        try:
            if enabled() and check():
                reasons.append(reason)
        except Exception:
            pass
    return reasons


def evaluate():
    """(on?, [why]) right now."""
    current = mode()
    if current == "on":
        return True, ["you turned it on"]
    if current == "off":
        return False, []
    reasons = automatic_reasons()
    return bool(reasons), reasons


def publish():
    """Work out the state, write it to dnd.json for other programs, and return (on?, [why])."""
    active, reasons = evaluate()
    tmp = STATE_PATH + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"unreachable": active, "ts": time.time()}, f)
        os.replace(tmp, STATE_PATH)
    except OSError:
        pass
    return active, reasons


def set_mode(value):
    with open(SETTINGS_PATH, "w", encoding="utf-8") as f:
        json.dump({"mode": value}, f)
    publish()
    return status()


def status():
    active, reasons = evaluate()
    current = mode()
    if current == "on":
        return "do not disturb is on until you say `dnd off` or `dnd auto`. messages wait."
    if current == "off":
        would = automatic_reasons()
        note = f" (it would be on otherwise: {', '.join(would)})" if would else ""
        return f"do not disturb is off and stays off until you say `dnd auto`{note}."
    if active:
        return f"do not disturb is on by itself: {', '.join(reasons)}. messages wait. `dnd off` overrides."
    return "do not disturb is off. it turns on by itself for a shared screen, an external display, a closed lid or a locked screen."
