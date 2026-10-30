"""Copies each new Desktop screenshot (Cmd+Shift+3/4/5) to the clipboard as an image.

Triggered by launchd when ~/Desktop changes. For text instead of the image, see screen_text.py.
"""
import os
import time

from lib.common import run_main, state_path

DESKTOP_DIR = os.path.expanduser("~/Desktop")
PROCESSED_LOG = state_path("screenshots_copied.txt")
PROCESSED_KEEP = 200
# launchd may hold a run back for ~10s after the previous one, so allow some slack.
MAX_AGE_SECONDS = 60
IMAGE_TYPES = {".png": "public.png", ".jpg": "public.jpeg", ".jpeg": "public.jpeg"}


def is_screenshot(name):
    return os.path.splitext(name)[1].lower() in IMAGE_TYPES and ("Screen Shot" in name or "Screenshot" in name)


def copy_image_to_clipboard(path, pasteboard=None):
    from AppKit import NSImage, NSPasteboard, NSPasteboardItem
    from Foundation import NSData

    data = NSData.dataWithContentsOfFile_(path)
    image = NSImage.alloc().initWithData_(data) if data else None
    if image is None:
        raise RuntimeError(f"could not read {path}")
    item = NSPasteboardItem.alloc().init()
    item.setData_forType_(data, IMAGE_TYPES[os.path.splitext(path)[1].lower()])
    # Some apps only accept TIFF from the clipboard.
    item.setData_forType_(image.TIFFRepresentation(), "public.tiff")
    pasteboard = pasteboard or NSPasteboard.generalPasteboard()
    pasteboard.clearContents()
    if not pasteboard.writeObjects_([item]):
        raise RuntimeError("clipboard refused the image")


def load_processed():
    try:
        with open(PROCESSED_LOG, encoding="utf-8") as f:
            return [line.rstrip("\n") for line in f if line.strip()]
    except OSError:
        return []


def save_processed(paths):
    with open(PROCESSED_LOG, "w", encoding="utf-8") as f:
        f.writelines(path + "\n" for path in paths[-PROCESSED_KEEP:])


def main(log):
    paths = [os.path.join(DESKTOP_DIR, name) for name in os.listdir(DESKTOP_DIR) if is_screenshot(name)]
    if not paths:
        return
    newest = max(paths, key=os.path.getmtime)
    processed = load_processed()
    if newest in processed or time.time() - os.path.getmtime(newest) > MAX_AGE_SECONDS:
        return
    # Recorded only after the copy worked, so a failure gets another try on the next trigger.
    copy_image_to_clipboard(newest)
    log.info("copied %s", os.path.basename(newest))
    save_processed(processed + [newest])


if __name__ == "__main__":
    run_main("screenshot_copy", main)
