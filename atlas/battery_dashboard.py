"""The battery dashboard: charge, time left now and with power saving, and who is drawing power.

A window in the assistant process: built once, hidden when closed. The
numbers are gathered on a worker thread (`top` needs two seconds to measure) and refreshed every
REFRESH_SECONDS while it is open. Rows can be picked (Cmd/Shift-click for several) and quit.
`open_dashboard` is the one call that is safe from any thread.
"""
import threading

import objc
from AppKit import (NSApp, NSApplicationActivateAllWindows, NSApplicationActivateIgnoringOtherApps, NSBackingStoreBuffered,
                    NSBezelStyleRounded, NSBox, NSBoxCustom, NSButton, NSColor, NSFont, NSLevelIndicator,
                    NSLevelIndicatorStyleContinuousCapacity, NSLineBreakByWordWrapping, NSMakeRect, NSPasteboard,
                    NSPasteboardTypeString, NSRunningApplication, NSScrollView, NSTableColumn, NSTableView,
                    NSTextField, NSViewHeightSizable, NSViewMaxYMargin, NSViewMinXMargin, NSViewMinYMargin,
                    NSViewWidthSizable, NSWindow, NSWindowCollectionBehaviorFullScreenAuxiliary,
                    NSWindowCollectionBehaviorMoveToActiveSpace, NSWindowStyleMaskClosable,
                    NSWindowStyleMaskMiniaturizable, NSWindowStyleMaskResizable, NSWindowStyleMaskTitled)
from Foundation import NSIndexSet, NSObject
from PyObjCTools import AppHelper

from atlas import battery_helper as bh
from lib.identity import APP_ID

TITLE = "battery"
WIDTH, HEIGHT = 860, 700
PAD = 16
TILE_HEIGHT = 150
REFRESH_SECONDS = 20
COLUMNS = [("name", "Process", 280), ("kind", "Kind", 120), ("share", "Power share", 90),
           ("impact", "Energy impact", 100), ("cpu", "CPU", 60), ("note", "", 160)]


def _label(frame, size=13, weight=0.0, color=None, wrap=False):
    field = NSTextField.alloc().initWithFrame_(frame)
    field.setBezeled_(False)
    field.setDrawsBackground_(False)
    field.setEditable_(False)
    field.setSelectable_(False)
    field.setFont_(NSFont.systemFontOfSize_weight_(size, weight))
    field.setTextColor_(color or NSColor.labelColor())
    if wrap:
        field.cell().setWraps_(True)
        field.cell().setLineBreakMode_(NSLineBreakByWordWrapping)
    return field


def _button(frame, title, target, action):
    button = NSButton.alloc().initWithFrame_(frame)
    button.setTitle_(title)
    button.setBezelStyle_(NSBezelStyleRounded)
    button.setTarget_(target)
    button.setAction_(action)
    return button


def _tile(frame):
    box = NSBox.alloc().initWithFrame_(frame)
    box.setBoxType_(NSBoxCustom)
    box.setCornerRadius_(10)
    box.setBorderWidth_(1)
    box.setBorderColor_(NSColor.separatorColor())
    box.setFillColor_(NSColor.controlBackgroundColor())
    box.setTitlePosition_(0)            # NSNoTitle
    box.setContentViewMargins_((0, 0))
    return box


class _BatteryController(NSObject):
    """Table data source and the target of the window's buttons."""

    def initWithOwner_(self, owner):
        self = objc.super(_BatteryController, self).init()
        if self is not None:
            self.owner = owner
        return self

    def numberOfRowsInTableView_(self, _table):
        return len(self.owner.rows)

    def tableView_objectValueForTableColumn_row_(self, _table, column, row):
        if row >= len(self.owner.rows):
            return ""
        return self.owner.cell(self.owner.rows[row], str(column.identifier()))

    def quit_(self, _sender):
        self.owner.quit_selected()

    def refresh_(self, _sender):
        self.owner.refresh()

    def toggle_(self, _sender):
        self.owner.toggle()

    def copySetup_(self, _sender):
        board = NSPasteboard.generalPasteboard()
        board.clearContents()
        board.setString_forType_(bh.SETUP_COMMAND, NSPasteboardTypeString)
        self.owner.set_message("setup command copied: paste it in Terminal and enter your password once")


class Dashboard:
    def __init__(self):
        from atlas.edit_menu import install_edit_menu

        install_edit_menu()
        self.rows = []
        self.idle = {}
        self.busy = False
        self.controller = _BatteryController.alloc().initWithOwner_(self)
        style = (NSWindowStyleMaskTitled | NSWindowStyleMaskClosable | NSWindowStyleMaskResizable
                 | NSWindowStyleMaskMiniaturizable)
        self.window = NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
            NSMakeRect(0, 0, WIDTH, HEIGHT), style, NSBackingStoreBuffered, False)
        self.window.setTitle_(TITLE)
        self.window.setReleasedWhenClosed_(False)
        self.window.setMinSize_((720, 560))
        self.window.center()
        self.window.setFrameAutosaveName_(f"{APP_ID}.battery")
        self.window.setCollectionBehavior_(NSWindowCollectionBehaviorMoveToActiveSpace
                                           | NSWindowCollectionBehaviorFullScreenAuxiliary)
        content = self.window.contentView()
        top = HEIGHT - PAD
        pinned_top = NSViewMinYMargin

        # ---- charge
        self.percent = _label(NSMakeRect(PAD, top - 64, 190, 64), 52, 0.6)
        self.percent.setAutoresizingMask_(pinned_top)
        content.addSubview_(self.percent)
        self.state = _label(NSMakeRect(PAD + 200, top - 34, WIDTH - 2 * PAD - 200, 22), 15, 0.3)
        self.state.setAutoresizingMask_(pinned_top | NSViewWidthSizable)
        content.addSubview_(self.state)
        self.detail = _label(NSMakeRect(PAD + 200, top - 58, WIDTH - 2 * PAD - 200, 20), 12,
                             color=NSColor.secondaryLabelColor())
        self.detail.setAutoresizingMask_(pinned_top | NSViewWidthSizable)
        content.addSubview_(self.detail)
        self.level = NSLevelIndicator.alloc().initWithFrame_(NSMakeRect(PAD, top - 92, WIDTH - 2 * PAD, 18))
        self.level.setLevelIndicatorStyle_(NSLevelIndicatorStyleContinuousCapacity)
        self.level.setMinValue_(0)
        self.level.setMaxValue_(100)
        self.level.setWarningValue_(20)
        self.level.setCriticalValue_(10)
        self.level.setAutoresizingMask_(pinned_top | NSViewWidthSizable)
        content.addSubview_(self.level)

        # ---- the two estimates
        tile_y = top - 108 - TILE_HEIGHT
        tile_w = (WIDTH - 3 * PAD) / 2
        now = _tile(NSMakeRect(PAD, tile_y, tile_w, TILE_HEIGHT))
        now.setAutoresizingMask_(pinned_top | NSViewWidthSizable)
        saving = _tile(NSMakeRect(2 * PAD + tile_w, tile_y, tile_w, TILE_HEIGHT))
        saving.setAutoresizingMask_(pinned_top | NSViewWidthSizable | NSViewMinXMargin)
        content.addSubview_(now)
        content.addSubview_(saving)
        inner = tile_w - 2 * PAD
        caption = NSColor.secondaryLabelColor()
        for box, title in ((now, "Left at the current draw"), (saving, "Left with power saving")):
            box.contentView().addSubview_(_label(NSMakeRect(PAD, TILE_HEIGHT - 32, inner, 18), 12, 0.4, caption))
            box.contentView().subviews()[-1].setStringValue_(title.upper())
        self.now_time = _label(NSMakeRect(PAD, TILE_HEIGHT - 84, inner, 48), 38, 0.5)
        self.now_sub = _label(NSMakeRect(PAD, PAD, inner, 40), 12, color=caption, wrap=True)
        now.contentView().addSubview_(self.now_time)
        now.contentView().addSubview_(self.now_sub)
        self.saving_time = _label(NSMakeRect(PAD, TILE_HEIGHT - 84, inner - 130, 48), 38, 0.5,
                                  NSColor.systemGreenColor())
        self.saving_sub = _label(NSMakeRect(PAD, 8, inner, 50), 11, color=caption, wrap=True)
        self.saving_sub.setAutoresizingMask_(NSViewWidthSizable)
        saving.contentView().addSubview_(self.saving_time)
        saving.contentView().addSubview_(self.saving_sub)
        try:
            from AppKit import NSSwitch
            self.switch = NSSwitch.alloc().initWithFrame_(NSMakeRect(inner - 30, TILE_HEIGHT - 70, 40, 24))
        except ImportError:
            self.switch = NSButton.alloc().initWithFrame_(NSMakeRect(inner - 30, TILE_HEIGHT - 70, 40, 24))
            self.switch.setButtonType_(3)       # NSSwitchButton (a checkbox)
            self.switch.setTitle_("")
        self.switch.setTarget_(self.controller)
        self.switch.setAction_("toggle:")
        self.switch.setAutoresizingMask_(NSViewMinXMargin)
        saving.contentView().addSubview_(self.switch)
        switch_label = _label(NSMakeRect(inner - 140, TILE_HEIGHT - 67, 104, 18), 12, 0.3)
        switch_label.setStringValue_("Power saving")
        switch_label.setAlignment_(2)           # right
        switch_label.setAutoresizingMask_(NSViewMinXMargin)
        saving.contentView().addSubview_(switch_label)
        self.setup = _button(NSMakeRect(inner - 150, 6, 166, 30), "Copy setup command",
                             self.controller, "copySetup:")
        self.setup.setAutoresizingMask_(NSViewMinXMargin)
        self.setup.setHidden_(True)
        saving.contentView().addSubview_(self.setup)

        # ---- who is drawing power
        heading_y = tile_y - 30
        heading = _label(NSMakeRect(PAD, heading_y, 400, 18), 12, 0.4, caption)
        heading.setStringValue_("USING POWER NOW (YOURS; PICK SEVERAL WITH CMD-CLICK)")
        heading.setAutoresizingMask_(pinned_top)
        content.addSubview_(heading)
        self.system = _label(NSMakeRect(PAD + 400, heading_y, WIDTH - 2 * PAD - 400, 18), 12, color=caption)
        self.system.setAlignment_(2)
        self.system.setAutoresizingMask_(pinned_top | NSViewWidthSizable)
        content.addSubview_(self.system)
        bottom = PAD + 32 + PAD
        scroll = NSScrollView.alloc().initWithFrame_(NSMakeRect(PAD, bottom, WIDTH - 2 * PAD, heading_y - 6 - bottom))
        scroll.setHasVerticalScroller_(True)
        scroll.setBorderType_(1)               # NSLineBorder
        scroll.setAutoresizingMask_(NSViewWidthSizable | NSViewHeightSizable)
        self.table = NSTableView.alloc().initWithFrame_(scroll.bounds())
        for ident, title, width in COLUMNS:
            column = NSTableColumn.alloc().initWithIdentifier_(ident)
            column.headerCell().setStringValue_(title)
            column.setWidth_(width)
            if ident in ("share", "impact", "cpu"):
                column.dataCell().setAlignment_(2)
            self.table.addTableColumn_(column)
        self.table.setAllowsMultipleSelection_(True)
        self.table.setUsesAlternatingRowBackgroundColors_(True)
        self.table.setRowHeight_(22)
        self.table.setDataSource_(self.controller)
        self.table.setTarget_(self.controller)
        scroll.setDocumentView_(self.table)
        content.addSubview_(scroll)

        # ---- bottom bar
        self.message = _label(NSMakeRect(PAD, PAD + 6, WIDTH - 3 * PAD - 260, 20), 12, color=caption)
        self.message.setAutoresizingMask_(NSViewWidthSizable | NSViewMaxYMargin)
        content.addSubview_(self.message)
        refresh = _button(NSMakeRect(WIDTH - PAD - 250, PAD, 110, 32), "Refresh", self.controller, "refresh:")
        refresh.setAutoresizingMask_(NSViewMinXMargin | NSViewMaxYMargin)
        content.addSubview_(refresh)
        self.quit = _button(NSMakeRect(WIDTH - PAD - 134, PAD, 134, 32), "Quit selected", self.controller, "quit:")
        self.quit.setAutoresizingMask_(NSViewMinXMargin | NSViewMaxYMargin)
        self.quit.setKeyEquivalent_("\r")
        content.addSubview_(self.quit)

        self.percent.setStringValue_("…")
        self.state.setStringValue_("measuring")
        AppHelper.callLater(REFRESH_SECONDS, self._tick)

    # ------------------------------------------------------------ main thread only

    def show(self, message=""):
        self.window.orderFrontRegardless()
        NSRunningApplication.currentApplication().activateWithOptions_(
            NSApplicationActivateAllWindows | NSApplicationActivateIgnoringOtherApps)
        NSApp.activateIgnoringOtherApps_(True)
        self.window.makeKeyAndOrderFront_(None)
        if message:
            self.set_message(message)
        self.refresh()

    def set_message(self, text):
        self.message.setStringValue_(text.replace("\n", " · "))
        self.message.setToolTip_(text)

    def cell(self, row, ident):
        if ident == "name":
            return row["name"]
        if ident == "kind":
            return {"app": "app", "background": "background app", "process": "process"}[row["kind"]]
        if ident == "share":
            return f"{row['share']:.0%}" if row["share"] >= 0.005 else "–"
        if ident == "impact":
            return f"{row['impact']:.1f}"
        if ident == "cpu":
            return f"{row['cpu']:.0f}%"
        if ident == "note":
            return f"unused {bh._ago(self.idle[row['path']])}" if row["path"] in self.idle else ""
        return ""

    def _tick(self):
        if self.window.isVisible():
            self.refresh()
        AppHelper.callLater(REFRESH_SECONDS, self._tick)

    def refresh(self):
        if self.busy:
            return
        self.busy = True

        def work():
            try:
                info = bh.battery()
                power = bh.power_by_process()
                items, system = bh.ranked(power)
                idle = {entry["path"]: seconds for entry, seconds in bh.idle_apps(power)}
                names = [item["name"] for item in items if item["path"] in idle]
                estimate = bh.estimate(info, items, names) if info else (None,) * 4
                facts = dict(info=info, items=items, system=system, idle=idle, estimate=estimate,
                             on=bh.saving_on(), changes=bh.plan_saving(), root=bh.can_set_power())
            except Exception as error:      # shown, not raised: the window stays usable
                facts = {"error": str(error)}
            AppHelper.callAfter(self._update, facts)

        threading.Thread(target=work, daemon=True).start()

    def _update(self, facts):
        self.busy = False
        if "error" in facts:
            self.set_message(f"could not measure: {facts['error']}")
            return
        info = facts["info"]
        if info is None:
            self.percent.setStringValue_("—")
            self.state.setStringValue_("no battery found")
            return
        self.percent.setStringValue_(f"{info['percent']}%")
        self.level.setDoubleValue_(info["percent"])
        if info["charging"]:
            to_full = info["to_full_minutes"]
            state = "charging" + (f", full in {bh.fmt_hours(to_full / 60)}" if to_full < 65535 else "")
        elif info["plugged"]:
            state = "on the power adapter" + (", charged" if info["full"] else ", not charging")
        else:
            state = "on battery"
        self.state.setStringValue_(state)
        self.detail.setStringValue_(f"{info['left_wh']:.1f} of {info['full_wh']:.1f} Wh left · health {info['health']}% "
                                    f"· {info['cycles']} cycles")

        hours_now, hours_saving, watts, after = facts["estimate"]
        recent = info.get("watts_source") != "now"
        self.now_time.setStringValue_(bh.fmt_hours(hours_now))
        if watts is None:
            self.now_sub.setStringValue_("no draw measured on battery yet")
        else:
            self.now_sub.setStringValue_(f"drawing {watts:.1f} W" + (" (recent average on battery; plugged in now)"
                                                                      if recent else ""))
        on = facts["on"]
        self.switch.setState_(1 if on else 0)
        if on:
            self.saving_time.setStringValue_(bh.fmt_hours(hours_now))
            self.saving_sub.setStringValue_("on now: the time on the left already includes it. switch off to put "
                                            "every setting back")
        else:
            self.saving_time.setStringValue_(("≈ " + bh.fmt_hours(hours_saving)) if hours_saving else "—")
            changes = "; ".join(f"{what} {old} → {new}" for what, old, new in facts["changes"])
            idle = len(facts["idle"])
            parts = [f"≈ {after:.1f} W" if after else "", changes,
                     f"quits {idle} unused app{'s' if idle != 1 else ''}" if idle else ""]
            self.saving_sub.setStringValue_(" · ".join(p for p in parts if p) or "nothing left to save")
        needs_root = not facts["root"] and any(what != "brightness" for what, _o, _n in facts["changes"])
        self.setup.setHidden_(not needs_root)
        width = self.saving_sub.superview().frame().size.width - 2 * PAD
        self.saving_sub.setFrameSize_((width - (170 if needs_root else 0), 50))
        if needs_root:
            self.setup.setToolTip_("Low Power Mode, display sleep and Power Nap need root. This copies a command that "
                                   "allows exactly those pmset settings without a password:\n\n" + bh.SETUP_COMMAND)

        selected = {self.rows[i]["path"] for i in self._selected_rows() if i < len(self.rows)}
        self.rows, self.idle = facts["items"], facts["idle"]
        self.system.setStringValue_(f"macOS itself (WindowServer, kernel, daemons): {facts['system']:.0%}")
        self.table.reloadData()
        keep = NSIndexSet.indexSet().mutableCopy()
        for i, row in enumerate(self.rows):
            if row["path"] in selected:
                keep.addIndex_(i)
        self.table.selectRowIndexes_byExtendingSelection_(keep, False)

    def _selected_rows(self):
        indexes = self.table.selectedRowIndexes()
        found, i = [], indexes.firstIndex()
        while i != 0x7FFFFFFFFFFFFFFF and i is not None and i < 1 << 62:
            found.append(i)
            i = indexes.indexGreaterThanIndex_(i)
        return found

    def quit_selected(self):
        picked = [self.rows[i] for i in self._selected_rows() if i < len(self.rows)]
        if not picked:
            self.set_message("pick one or more rows first")
            return
        self.set_message("; ".join(bh.quit_item(row) for row in picked))
        self.table.deselectAll_(None)
        AppHelper.callLater(1.5, self.refresh)

    def toggle(self):
        turning_on = bool(self.switch.state())
        self.switch.setEnabled_(False)
        self.set_message("turning power saving on…" if turning_on else "putting the settings back…")

        def work():
            try:
                text = bh.turn_on() if turning_on else bh.turn_off()
            except Exception as error:
                text = f"that broke: {error}"
            AppHelper.callAfter(self._toggled, text)

        threading.Thread(target=work, daemon=True).start()

    def _toggled(self, text):
        self.switch.setEnabled_(True)
        self.set_message(text)
        self.refresh()


_dashboard = {}


def open_dashboard(message=""):
    """Show the dashboard (building it the first time), from any thread."""

    def on_main():
        if "window" not in _dashboard:
            _dashboard["window"] = Dashboard()
        _dashboard["window"].show(message)

    AppHelper.callAfter(on_main)


def refresh_if_open():
    def on_main():
        board = _dashboard.get("window")
        if board is not None and board.window.isVisible():
            board.refresh()

    AppHelper.callAfter(on_main)
