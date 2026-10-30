"""Keeps audio from blasting out of the wrong device.

- Headphones (Bluetooth, USB or the jack) drop out and output falls back to the built-in
  speakers: pause Spotify/Music and mute.
- The Mac sleeps or the session is switched away: pause and mute.
- A display with speakers is connected while the built-in speakers are in use: switch to it.
  macOS falls back to the speakers on its own when the display goes away.

Set AUDIOGUARD_DRY_RUN=1 to log decisions without acting.
"""
import os
import queue
import threading

from lib import coreaudio
from lib.common import run, run_main

DRY_RUN = bool(os.environ.get("AUDIOGUARD_DRY_RUN"))
MEDIA_APPS = {"Spotify": "com.spotify.client", "Music": "com.apple.Music"}
DISPLAY_WORDS = ("hdmi", "display", "monitor")


def is_display(device):
    name = device.name.lower()
    return device.transport in ("hdmi", "dprt") or any(word in name for word in DISPLAY_WORDS)


def is_speakers(device):
    # On Apple Silicon the headphone jack is its own built-in device ("External Headphones").
    return device.transport == "bltn" and "headphone" not in device.name.lower()


def is_personal(device):
    if is_display(device):
        return False
    if device.transport == "bltn":
        return "headphone" in device.name.lower()
    return device.transport in ("blue", "blea", "usb ")


def should_silence(previous, current):
    """True when output just moved from something private to the built-in speakers."""
    return bool(previous and current and is_personal(previous) and is_speakers(current))


class Guard:
    def __init__(self, log):
        self.log = log
        self.lock = threading.Lock()
        self.events = queue.Queue()
        self.current = coreaudio.describe(coreaudio.default_output())
        self.known = set(coreaudio.output_device_ids())

    def pause_media(self):
        for app, bundle_id in MEDIA_APPS.items():
            found = run(["pgrep", "-x", app], timeout=5)
            if found is not None and found.returncode == 0:
                run(["osascript", "-e", f'tell application id "{bundle_id}" to pause'], timeout=5)

    def silence(self, reason):
        self.log.info("silencing: %s", reason)
        if DRY_RUN:
            return
        with self.lock:
            device = coreaudio.default_output()
            if not (device and coreaudio.set_mute(device, True)):
                run(["osascript", "-e", "set volume with output muted"], timeout=5)
            self.pause_media()

    def on_default_changed(self):
        previous, self.current = self.current, coreaudio.describe(coreaudio.default_output())
        if self.current is None or (previous and previous.id == self.current.id):
            return
        self.log.info("output: %s -> %s (%s)", previous.name if previous else "?",
                      self.current.name, self.current.transport)
        if should_silence(previous, self.current):
            self.silence(f"{previous.name} disconnected")

    def on_devices_changed(self):
        ids = set(coreaudio.output_device_ids())
        added, self.known = ids - self.known, ids
        for device in filter(None, map(coreaudio.describe, added)):
            if is_display(device) and self.current and is_speakers(self.current):
                self.log.info("display audio connected, switching to %s", device.name)
                if not DRY_RUN:
                    coreaudio.set_default_output(device.id)
                return

    def worker(self):
        while True:
            handler = self.events.get()
            try:
                handler()
            except Exception:
                self.log.exception("handler failed")


def main(log):
    from AppKit import (NSWorkspace, NSWorkspaceSessionDidResignActiveNotification,
                        NSWorkspaceWillSleepNotification)
    from Foundation import NSObject
    from PyObjCTools import AppHelper

    guard = Guard(log)
    threading.Thread(target=guard.worker, daemon=True).start()

    # CoreAudio calls back on its own thread; hand the work to ours.
    coreaudio.listen(coreaudio.DEFAULT_OUTPUT, lambda: guard.events.put(guard.on_default_changed))
    coreaudio.listen(coreaudio.DEVICES, lambda: guard.events.put(guard.on_devices_changed))

    class SleepObserver(NSObject):
        # Runs synchronously so the mute lands before the machine actually sleeps.
        def willSleep_(self, _notification):
            guard.silence("sleep")

        def sessionResigned_(self, _notification):
            guard.silence("session switched away")

    observer = SleepObserver.alloc().init()
    center = NSWorkspace.sharedWorkspace().notificationCenter()
    center.addObserver_selector_name_object_(observer, "willSleep:", NSWorkspaceWillSleepNotification, None)
    center.addObserver_selector_name_object_(
        observer, "sessionResigned:", NSWorkspaceSessionDidResignActiveNotification, None)

    log.info("started; output is %s%s", guard.current.name if guard.current else "unknown",
             " (dry run)" if DRY_RUN else "")
    AppHelper.runConsoleEventLoop()


if __name__ == "__main__":
    run_main("audio_guard", main)
