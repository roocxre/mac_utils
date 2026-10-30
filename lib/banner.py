"""Shows one banner in the top right corner of the screen, like a notification.

    banner.py TITLE TEXT [SECONDS] [--post NAME]

It stays until clicked, or closes by itself after SECONDS if given. With --post, a click also
posts the distributed notification NAME (the assistant opens the matching window). It never takes keyboard
focus. Several can be up at once; they stack downwards. Exits 0 once it is gone.

This is our own small window rather than a Notification Center banner: macOS drops those from
a background script unless it was granted permission, and whether they stay on screen is a
System Settings choice we can't make from here. Use `common.banner` to show one.
"""
import fcntl
import os
import sys
import threading

from AppKit import (NSApplication, NSApplicationActivationPolicyAccessory, NSBackingStoreBuffered,
                    NSColor, NSFloatingWindowLevel, NSFont, NSMakeRect, NSPanel, NSPointInRect,
                    NSScreen, NSTextField, NSVisualEffectBlendingModeBehindWindow,
                    NSVisualEffectMaterialHUDWindow, NSVisualEffectStateActive, NSVisualEffectView,
                    NSWindowCollectionBehaviorCanJoinAllSpaces, NSWindowCollectionBehaviorStationary,
                    NSWindowStyleMaskBorderless, NSWindowStyleMaskNonactivatingPanel)

from lib.common import state_path

WIDTH = 360
PAD = 14
MARGIN = 12            # from the screen edge, and between stacked banners
MAX_BODY_HEIGHT = 260
SLOTS = 6
SLOT_RETRY_SECONDS = 1.0
POST = None             # --post NAME: the notification a click sends
CLOSE = 26              # the close mark's square in the top right corner (only with --post)
_close_rect = None


class _Card(NSVisualEffectView):
    """The banner's background. A click anywhere on it dismisses the banner; with --post, it also
    opens what the banner is about, except a click on the close mark, which only dismisses it."""

    def hitTest_(self, point):
        return self if NSPointInRect(point, self.frame()) else None

    def acceptsFirstMouse_(self, _event):
        return True

    def mouseDown_(self, event):
        point = self.convertPoint_fromView_(event.locationInWindow(), None)
        if POST and not (_close_rect and NSPointInRect(point, _close_rect)):
            from Foundation import NSDistributedNotificationCenter
            NSDistributedNotificationCenter.defaultCenter().postNotificationName_object_userInfo_deliverImmediately_(
                POST, None, None, True)
        os._exit(0)


def _claim_slot(height):
    """Take the highest free place in the stack. Returns (open lock file, distance from the top).

    A place is a lock file holding its banner's height; the lock goes when the banner exits.
    """
    offset = 0
    for index in range(SLOTS):
        handle = open(state_path(f"banner-slot-{index}"), "a+")
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            handle.seek(0)
            try:
                offset += int(handle.read().strip() or 0) + MARGIN
            except ValueError:
                pass
            handle.close()
            continue
        handle.seek(0)
        handle.truncate()
        handle.write(str(int(height)))
        handle.flush()
        return handle, offset
    return None, 0


def _label(text, font, color, lines):
    label = NSTextField.wrappingLabelWithString_(text)
    label.setFont_(font)
    label.setTextColor_(color)
    label.setSelectable_(False)
    label.setMaximumNumberOfLines_(lines)
    return label


def main():
    global POST
    args = sys.argv[1:]
    if "--post" in args:
        at = args.index("--post")
        POST = args[at + 1] if at + 1 < len(args) else None
        del args[at:at + 2]
    if len(args) < 2:
        sys.exit("usage: banner.py TITLE TEXT [SECONDS] [--post NAME]")
    title, text = args[0], args[1]
    seconds = float(args[2]) if len(args) > 2 and args[2] else None

    app = NSApplication.sharedApplication()
    app.setActivationPolicy_(NSApplicationActivationPolicyAccessory)

    inner = WIDTH - 2 * PAD
    head = _label(title, NSFont.boldSystemFontOfSize_(13), NSColor.labelColor(), 1)
    body = _label(text, NSFont.systemFontOfSize_(13), NSColor.labelColor(), 0)
    head_h = head.cell().cellSizeForBounds_(NSMakeRect(0, 0, inner, 10000)).height
    body_h = min(body.cell().cellSizeForBounds_(NSMakeRect(0, 0, inner, 10000)).height, MAX_BODY_HEIGHT)
    height = PAD + head_h + 4 + body_h + PAD

    slot, offset = _claim_slot(height)
    while slot is None:  # the stack is full: wait for one to be dismissed
        threading.Event().wait(SLOT_RETRY_SECONDS)
        slot, offset = _claim_slot(height)

    screen = NSScreen.screens()[0].visibleFrame()
    x = screen.origin.x + screen.size.width - WIDTH - MARGIN
    y = screen.origin.y + screen.size.height - MARGIN - offset - height
    panel = NSPanel.alloc().initWithContentRect_styleMask_backing_defer_(
        NSMakeRect(x, y, WIDTH, height), NSWindowStyleMaskBorderless | NSWindowStyleMaskNonactivatingPanel,
        NSBackingStoreBuffered, False)
    panel.setLevel_(NSFloatingWindowLevel)
    panel.setOpaque_(False)
    panel.setBackgroundColor_(NSColor.clearColor())
    panel.setHasShadow_(True)
    panel.setHidesOnDeactivate_(False)
    panel.setCollectionBehavior_(NSWindowCollectionBehaviorCanJoinAllSpaces | NSWindowCollectionBehaviorStationary)

    card = _Card.alloc().initWithFrame_(NSMakeRect(0, 0, WIDTH, height))
    card.setMaterial_(NSVisualEffectMaterialHUDWindow)
    card.setBlendingMode_(NSVisualEffectBlendingModeBehindWindow)
    card.setState_(NSVisualEffectStateActive)
    card.setWantsLayer_(True)
    card.layer().setCornerRadius_(14)
    card.layer().setMasksToBounds_(True)
    card.setToolTip_("click to open, or ✕ to dismiss" if POST else "click to dismiss")
    head.setFrame_(NSMakeRect(PAD, height - PAD - head_h, inner - (CLOSE if POST else 0), head_h))
    if POST:
        global _close_rect
        _close_rect = NSMakeRect(WIDTH - CLOSE - 6, height - CLOSE - 6, CLOSE, CLOSE)
        mark = _label("✕", NSFont.systemFontOfSize_(13), NSColor.secondaryLabelColor(), 1)
        mark.setAlignment_(1)                 # centred
        mark.setFrame_(NSMakeRect(WIDTH - CLOSE - 6, height - CLOSE - 2, CLOSE, 18))
        card.addSubview_(mark)
    body.setFrame_(NSMakeRect(PAD, PAD, inner, body_h))
    card.addSubview_(head)
    card.addSubview_(body)
    panel.setContentView_(card)
    panel.orderFrontRegardless()

    if seconds:
        timer = threading.Timer(seconds, lambda: os._exit(0))
        timer.daemon = True
        timer.start()
    app.run()


if __name__ == "__main__":
    main()
