"""Watch the broker agent work ("brokers debug"): its steps as they happen, and the page its headless
browser has open, live.

A window in the assistant process, like the broker dashboard. The left side follows
brokers/watch.py's event file (every agent process writes to it: a pass, capture inside the mail
poller, a command run by hand). The right side shows the browser's screencast, which the agent only
streams while this window is open (it keeps brokers.watch.WATCHING fresh). Closing the window
deletes the last frame, since a filled-in form shows your details.

"Run a pass now" starts one pass in the background; a pass already running makes it stand down.
"""
import json
import os
import time

from AppKit import (NSApp, NSApplicationActivateAllWindows, NSApplicationActivateIgnoringOtherApps,
                    NSAttributedString, NSBackingStoreBuffered, NSColor, NSFont, NSFontAttributeName,
                    NSForegroundColorAttributeName, NSImage, NSImageView, NSMakeRect, NSRunningApplication,
                    NSScrollView, NSTextView, NSViewHeightSizable, NSViewMaxXMargin,
                    NSViewMinXMargin, NSViewMinYMargin, NSViewWidthSizable, NSWindow,
                    NSWindowCollectionBehaviorFullScreenAuxiliary, NSWindowCollectionBehaviorMoveToActiveSpace,
                    NSWindowStyleMaskClosable, NSWindowStyleMaskMiniaturizable, NSWindowStyleMaskResizable,
                    NSWindowStyleMaskTitled)
from Foundation import NSDistributedNotificationCenter, NSMakeRange, NSObject
from PyObjCTools import AppHelper

from atlas.battery_dashboard import _button, _label, _tile
from brokers import watch
from lib.identity import APP_ID, APP_NAME

TITLE = f"{APP_NAME} · broker agent, live"
WIDTH, HEIGHT = 1280, 800
PAD = 16
LOG_WIDTH = 560
TICK = 0.4
HEARTBEAT = 5
FIRST_LINES = 300
PASS_EVERY = 600                    # the brokeragent's StartInterval
KIND_COLORS = {"pass": "labelColor", "mail": "systemBlueColor", "send": "systemPurpleColor",
               "form": "secondaryLabelColor", "step": "labelColor", "done": "systemGreenColor",
               "stop": "systemOrangeColor", "warn": "systemOrangeColor", "error": "systemRedColor"}

_watch = {}


class _WatchController(NSObject):
    def initWithOwner_(self, owner):
        self = self.init()
        self.owner = owner
        return self

    def run_(self, _sender):
        self.owner.run_now()

    def clear_(self, _sender):
        self.owner.clear()

    def open_(self, _note):               # brokers.watch.OPEN_WATCH, from another process
        open_watch()


class Watch:
    def __init__(self):
        self.controller = _WatchController.alloc().initWithOwner_(self)
        style = (NSWindowStyleMaskTitled | NSWindowStyleMaskClosable | NSWindowStyleMaskResizable
                 | NSWindowStyleMaskMiniaturizable)
        self.window = NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
            NSMakeRect(0, 0, WIDTH, HEIGHT), style, NSBackingStoreBuffered, False)
        self.window.setTitle_(TITLE)
        self.window.setReleasedWhenClosed_(False)
        self.window.setMinSize_((900, 520))
        self.window.center()
        self.window.setFrameAutosaveName_(f"{APP_ID}.brokers.watch")
        self.window.setCollectionBehavior_(NSWindowCollectionBehaviorMoveToActiveSpace
                                           | NSWindowCollectionBehaviorFullScreenAuxiliary)
        content = self.window.contentView()
        caption = NSColor.secondaryLabelColor()
        top = HEIGHT - PAD

        heading = _label(NSMakeRect(PAD, top - 30, 520, 30), 22, 0.6)
        heading.setStringValue_("Broker agent, live")
        heading.setAutoresizingMask_(NSViewMinYMargin)
        content.addSubview_(heading)
        self.status = _label(NSMakeRect(PAD, top - 54, WIDTH - 2 * PAD - 300, 18), 12, color=caption)
        self.status.setAutoresizingMask_(NSViewMinYMargin | NSViewWidthSizable)
        content.addSubview_(self.status)
        x = WIDTH - PAD
        for title, action, tip, width in (
                ("Run a pass now", "run:", "one pass now: replies, a search if due, what's queued to send", 140),
                ("Clear", "clear:", "clear this view (the agent's history is kept)", 80)):
            x -= width
            button = _button(NSMakeRect(x, top - 34, width, 32), title, self.controller, action)
            button.setToolTip_(tip)
            button.setAutoresizingMask_(NSViewMinYMargin | NSViewMinXMargin)
            content.addSubview_(button)
            x -= 8

        body_top, body_bottom = top - 66, PAD
        scroll = NSScrollView.alloc().initWithFrame_(NSMakeRect(PAD, body_bottom, LOG_WIDTH, body_top - body_bottom))
        scroll.setHasVerticalScroller_(True)
        scroll.setBorderType_(1)
        scroll.setAutoresizingMask_(NSViewHeightSizable | NSViewMaxXMargin)
        self.log_scroll = scroll
        self.log = NSTextView.alloc().initWithFrame_(scroll.bounds())
        self.log.setEditable_(False)
        self.log.setSelectable_(True)
        self.log.setRichText_(True)
        self.log.setTextContainerInset_((6, 6))
        self.log.setAutoresizingMask_(NSViewWidthSizable)
        scroll.setDocumentView_(self.log)
        content.addSubview_(scroll)

        pane_x = PAD + LOG_WIDTH + 12
        pane_w = WIDTH - pane_x - PAD
        pane = _tile(NSMakeRect(pane_x, body_bottom, pane_w, body_top - body_bottom))
        pane.setAutoresizingMask_(NSViewWidthSizable | NSViewHeightSizable)
        content.addSubview_(pane)
        inner = pane.contentView()
        pane_h = body_top - body_bottom
        self.page_caption = _label(NSMakeRect(10, pane_h - 26, pane_w - 20, 18), 12, color=caption)
        self.page_caption.setAutoresizingMask_(NSViewMinYMargin | NSViewWidthSizable)
        inner.addSubview_(self.page_caption)
        self.frame_view = NSImageView.alloc().initWithFrame_(NSMakeRect(8, 8, pane_w - 16, pane_h - 40))
        self.frame_view.setImageScaling_(3)              # proportionally up or down
        self.frame_view.setAutoresizingMask_(NSViewWidthSizable | NSViewHeightSizable)
        inner.addSubview_(self.frame_view)

        self.offset = 0
        self.frame_at = 0
        self.beat_at = 0
        self.last_pass = None             # (at, text) of the newest "pass" line
        self.last_url = ""
        self.font = NSFont.monospacedSystemFontOfSize_weight_(11, 0.0)
        self.bold = NSFont.monospacedSystemFontOfSize_weight_(11, 0.6)
        self._load_recent()
        AppHelper.callLater(TICK, self._tick)

    # ------------------------------------------------------------ main thread only

    def show(self):
        self._beat(force=True)            # the agent starts streaming at its next page
        self.window.orderFrontRegardless()
        NSRunningApplication.currentApplication().activateWithOptions_(
            NSApplicationActivateAllWindows | NSApplicationActivateIgnoringOtherApps)
        NSApp.activateIgnoringOtherApps_(True)
        self.window.makeKeyAndOrderFront_(None)

    def _beat(self, force=False):
        if force or time.time() - self.beat_at > HEARTBEAT:
            with open(watch.WATCHING, "w") as f:
                f.write(str(time.time()))
            self.beat_at = time.time()

    def _closed(self):
        """Not watching: stop the stream and drop the last frame (it may show your details)."""
        for path in (watch.WATCHING, watch.FRAME):
            try:
                os.remove(path)
            except OSError:
                pass
        self.frame_at = 0
        self.frame_view.setImage_(None)

    def _tick(self):
        try:
            if self.window.isVisible():
                self._beat()
                self._read_events()
                self._show_frame()
                self._show_status()
            elif os.path.exists(watch.WATCHING) or os.path.exists(watch.FRAME):
                self._closed()
        except Exception:
            pass
        AppHelper.callLater(TICK, self._tick)

    def _load_recent(self):
        try:
            with open(watch.EVENTS, encoding="utf-8") as f:
                lines = f.readlines()
                self.offset = f.tell()
        except OSError:
            lines = []
        self._append([line for line in lines[-FIRST_LINES:]])

    def _read_events(self):
        try:
            size = os.path.getsize(watch.EVENTS)
        except OSError:
            return
        if size < self.offset:            # trimmed: start again from its top
            self.offset = 0
        if size == self.offset:
            return
        with open(watch.EVENTS, encoding="utf-8") as f:
            f.seek(self.offset)
            chunk = f.read()
            self.offset = f.tell()
        lines = chunk.splitlines(keepends=True)
        if lines and not lines[-1].endswith("\n"):          # half-written: read it next time
            self.offset -= len(lines.pop().encode("utf-8"))
        self._append(lines)

    def _append(self, lines):
        storage = self.log.textStorage()
        clip = self.log_scroll.contentView()
        at_bottom = clip.bounds().origin.y + clip.bounds().size.height >= self.log.frame().size.height - 30
        for raw in lines:
            try:
                event = json.loads(raw)
            except ValueError:
                continue
            if event["kind"] == "pass" and event["text"].startswith(("pass started", "pass done")):
                self.last_pass = (event["at"], event["text"])
            if event.get("url"):
                self.last_url = event["url"]
            stamp = time.strftime("%H:%M:%S", time.localtime(event["at"]))
            who = f"{event['broker']}  " if event.get("broker") else ""
            color = getattr(NSColor, KIND_COLORS.get(event["kind"], "labelColor"))()
            font = self.bold if event["kind"] == "pass" else self.font
            text = f"{stamp}  {who}{event['text']}\n"
            if event["kind"] == "pass" and event["text"] == "pass started":
                text = "\n" + text
            storage.appendAttributedString_(NSAttributedString.alloc().initWithString_attributes_(
                text, {NSFontAttributeName: font, NSForegroundColorAttributeName: color}))
        if lines and at_bottom:
            self.log.scrollRangeToVisible_(NSMakeRange(storage.length(), 0))

    def _show_frame(self):
        try:
            changed = os.path.getmtime(watch.FRAME)
        except OSError:
            if self.frame_at:
                self.frame_view.setImage_(None)
                self.frame_at = 0
            if not self.page_caption.stringValue():
                self.page_caption.setStringValue_("The page shows here while the agent has one open (a form, a "
                                                  "search, a confirmation link).")
            return
        if changed != self.frame_at:
            image = NSImage.alloc().initWithContentsOfFile_(watch.FRAME)
            if image:
                self.frame_view.setImage_(image)
                self.frame_at = changed
        age = time.time() - self.frame_at
        where = self.last_url or "the agent's browser"
        self.page_caption.setStringValue_(f"{where}" + ("" if age < 10 else
                                                        f"  ·  last seen {time.strftime('%H:%M:%S', time.localtime(self.frame_at))}"))

    def _show_status(self):
        if not self.last_pass:
            self.status.setStringValue_("No pass seen yet. The agent runs every 10 minutes while the Mac is awake.")
            return
        at, text = self.last_pass
        when = time.strftime("%H:%M:%S", time.localtime(at))
        if text == "pass started" and time.time() - at < 15 * 60:
            self.status.setStringValue_(f"Running a pass, since {when}.")
            return
        due = at + PASS_EVERY
        next_at = time.strftime("%H:%M", time.localtime(due)) if due > time.time() else "any moment (once the Mac is awake)"
        self.status.setStringValue_(f"Idle. Last pass {when}; the next one {'at ' if due > time.time() else ''}{next_at}.")

    def run_now(self):
        from brokers import ops
        try:
            ops.run_pass()
            watch.say("you asked for a pass from the watch window", "step")
        except ops.Problem as problem:
            watch.say(f"couldn't start a pass: {problem}", "warn")

    def clear(self):
        storage = self.log.textStorage()
        storage.deleteCharactersInRange_(NSMakeRange(0, storage.length()))


def open_watch():
    """Show the window (building it the first time), from any thread."""

    def on_main():
        if "window" not in _watch:
            _watch["window"] = Watch()
        _watch["window"].show()

    AppHelper.callAfter(on_main)


def listen():
    """Open the window when brokers.watch.OPEN_WATCH is posted. Call once, on the main thread."""
    listener = _WatchController.alloc().initWithOwner_(None)
    _watch["listener"] = listener
    NSDistributedNotificationCenter.defaultCenter().addObserver_selector_name_object_(
        listener, "open:", watch.OPEN_WATCH, None)


def post_open():
    """Ask the running assistant to open the window (from another process)."""
    NSDistributedNotificationCenter.defaultCenter().postNotificationName_object_userInfo_deliverImmediately_(
        watch.OPEN_WATCH, None, None, True)
