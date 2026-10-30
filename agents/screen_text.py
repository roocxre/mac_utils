"""Global hotkey (Cmd+Shift+2) that copies the text in a selected screen region.

Shows the same crosshair as Cmd+Shift+4, runs text recognition on the selection and puts the
text on the clipboard; no file is left behind. Needs Screen Recording permission for Python,
which macOS asks for on first use. If the recognition helper is missing (see lib/ocr.py), a
banner says how to get it.
"""
import os
import subprocess
import tempfile
import threading

from lib import ocr
from lib.common import banner, run_main
from lib.hotkey import CMD, SHIFT, register_hotkey
from lib.identity import APP_NAME

# Virtual key code and modifiers for the hotkey. 19 is the "2" key (18 is "1", 20 is "3").
KEY_CODE = 19
MODIFIERS = CMD | SHIFT


def copy_to_clipboard(text):
    from AppKit import NSPasteboard, NSPasteboardTypeString

    pasteboard = NSPasteboard.generalPasteboard()
    pasteboard.clearContents()
    pasteboard.setString_forType_(text, NSPasteboardTypeString)


def capture_text(log):
    """Let the user drag a selection, then copy its text. Returns the text ('' if none/cancelled)."""
    fd, path = tempfile.mkstemp(prefix="screen-text-", suffix=".png")
    os.close(fd)
    os.remove(path)
    try:
        result = subprocess.run(["screencapture", "-i", path], capture_output=True, text=True,
                                timeout=300, check=False)
        if not os.path.exists(path):
            # Esc leaves no file and no message; anything on stderr is a real failure.
            if result.stderr.strip():
                log.warning("screencapture failed: %s", result.stderr.strip())
            return ""
        text = ocr.recognize_text(path)
        if text:
            copy_to_clipboard(text)
            log.info("copied %d characters", len(text))
        else:
            log.info("no text found in selection")
        return text
    finally:
        if os.path.exists(path):
            os.remove(path)


def main(log):
    from AppKit import NSApplication, NSApplicationActivationPolicyAccessory

    busy = threading.Lock()

    def on_hotkey():
        # The handler runs on the main thread; the selection UI must not block it.
        def work():
            if not busy.acquire(blocking=False):
                return
            try:
                capture_text(log)
            except ocr.HelperMissing as error:
                log.error("%s", error)
                banner(APP_NAME, str(error), seconds=20)
            except Exception:
                log.exception("capture failed")
            finally:
                busy.release()
        threading.Thread(target=work, daemon=True).start()

    app = NSApplication.sharedApplication()
    app.setActivationPolicy_(NSApplicationActivationPolicyAccessory)
    status = register_hotkey(KEY_CODE, MODIFIERS, on_hotkey)
    if status != 0:
        raise RuntimeError(f"could not register the hotkey (status {status}); is it already taken?")
    log.info("started; hotkey registered")
    app.run()


if __name__ == "__main__":
    run_main("screen_text", main)
