"""Ignores the keyboard, the trackpad, or both for a set time, e.g. to wipe them down.

    keyboard_lock.py SECONDS [keyboard|trackpad|both]

Key presses (including the volume and brightness keys) and/or clicks, scrolling and gestures
are swallowed until the time is up. There is no way to leave it on: it always runs on a
timer, capped at MAX_SECONDS. A banner in the corner says how long. To unlock early press
Ctrl+Option+Command+Esc, or click the banner while the trackpad is not locked.
If this process dies for any reason everything works again at once. The power / Touch ID
button can't be blocked, the pointer may still move (clicks go nowhere), and system swipes
between desktops are handled below this and may still work. A mouse counts as the trackpad.
Needs Accessibility for Python (the assistant already has it).
"""
import os
import sys
import threading

import Quartz

from lib.common import banner

DEFAULT_SECONDS = 30
MAX_SECONDS = 600
_SYSTEM_DEFINED = 14   # media, volume and brightness keys
_KEY_TYPES = {Quartz.kCGEventKeyDown, Quartz.kCGEventKeyUp, Quartz.kCGEventFlagsChanged, _SYSTEM_DEFINED}
_POINTER_TYPES = {
    Quartz.kCGEventLeftMouseDown, Quartz.kCGEventLeftMouseUp, Quartz.kCGEventLeftMouseDragged,
    Quartz.kCGEventRightMouseDown, Quartz.kCGEventRightMouseUp, Quartz.kCGEventRightMouseDragged,
    Quartz.kCGEventOtherMouseDown, Quartz.kCGEventOtherMouseUp, Quartz.kCGEventOtherMouseDragged,
    Quartz.kCGEventMouseMoved, Quartz.kCGEventScrollWheel,
    18, 29, 30, 31, 32, 34,   # rotate, gesture, magnify, swipe, smart magnify, pressure
}
WHAT = {"keyboard": "keyboard", "trackpad": "trackpad", "both": "keyboard and trackpad"}
_ESCAPE_KEY = 53
_ESCAPE_FLAGS = (Quartz.kCGEventFlagMaskControl | Quartz.kCGEventFlagMaskAlternate
                 | Quartz.kCGEventFlagMaskCommand)


def describe(seconds):
    seconds = int(round(seconds))
    if seconds < 60:
        return f"{seconds} second{'s' if seconds != 1 else ''}"
    minutes, rest = divmod(seconds, 60)
    text = f"{minutes} minute{'s' if minutes != 1 else ''}"
    return text + (f" {rest} s" if rest else "")


def main():
    try:
        seconds = float(sys.argv[1])
    except (IndexError, ValueError):
        sys.exit("usage: keyboard_lock.py SECONDS")
    seconds = max(1.0, min(seconds, MAX_SECONDS))
    what = sys.argv[2] if len(sys.argv) > 2 else "keyboard"
    if what not in WHAT:
        sys.exit("usage: keyboard_lock.py SECONDS [keyboard|trackpad|both]")
    blocked = (_KEY_TYPES if what != "trackpad" else set()) | (_POINTER_TYPES if what != "keyboard" else set())
    mask = 0
    for kind in blocked | {Quartz.kCGEventKeyDown}:   # key presses are always watched, for the way out
        mask |= 1 << kind
    state = {}

    def finish():
        """Unlock: take the banner down with us, then go."""
        shown = state.get("banner")
        if shown is not None and shown.poll() is None:
            shown.terminate()
        os._exit(0)

    def swallow(_proxy, kind, event, _refcon):
        if kind in (Quartz.kCGEventTapDisabledByTimeout, Quartz.kCGEventTapDisabledByUserInput):
            Quartz.CGEventTapEnable(state["tap"], True)
            return event
        if (kind == Quartz.kCGEventKeyDown
                and Quartz.CGEventGetIntegerValueField(event, Quartz.kCGKeyboardEventKeycode) == _ESCAPE_KEY
                and Quartz.CGEventGetFlags(event) & _ESCAPE_FLAGS == _ESCAPE_FLAGS):
            finish()
        return None if kind in blocked else event

    tap = Quartz.CGEventTapCreate(Quartz.kCGSessionEventTap, Quartz.kCGHeadInsertEventTap,
                                  Quartz.kCGEventTapOptionDefault, mask, swallow, None)
    if tap is None:
        sys.exit(f"could not lock the {WHAT[what]}: Python needs Accessibility permission")
    state["tap"] = tap

    # The hard stop, whatever else happens.
    stop = threading.Timer(seconds, finish)
    stop.daemon = True
    stop.start()

    def show():
        way_out = "ctrl+opt+cmd+esc unlocks now." if what != "keyboard" else \
            "click here or press ctrl+opt+cmd+esc to unlock now."
        state["banner"] = banner(f"{WHAT[what]} locked", f"for {describe(seconds)}. {way_out}", seconds)
        if state["banner"] is not None:
            state["banner"].wait()
        finish()   # clicked (or the banner could not show: don't lock without telling you)

    threading.Thread(target=show, daemon=True).start()

    source = Quartz.CFMachPortCreateRunLoopSource(None, tap, 0)
    Quartz.CFRunLoopAddSource(Quartz.CFRunLoopGetCurrent(), source, Quartz.kCFRunLoopCommonModes)
    Quartz.CGEventTapEnable(tap, True)
    Quartz.CFRunLoopRun()


if __name__ == "__main__":
    main()
