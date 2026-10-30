"""The inbox dashboard: the unsubscribe agent's weekly report, to read and decide on (unsub/).

A window in the assistant process, like the battery dashboard: built once, hidden when closed.
Each row is one category of mail from one sender (its ads, its list, its other mail), and every
choice applies to that category only. Double-click a row to read its messages (Reader). One tab
per report section; pick senders (Cmd/Shift-click for several) and press Keep, Unsubscribe or
Block (in Manual: Mark as… Done, Keep or Block). What you choose runs in a process of its own
(unsub/actions.py, the browser can take a minute) and each row shows how it went as it happens.
The switch at the top is the approval mode: off, decisions apply once (testing); on, they become rules.

It opens with "inbox report" (or "spam", "unsub"…) and when the weekly banner, or the banner after
actions, is clicked: those post unsub.OPEN_DASHBOARD, which listen() hears. `open_dashboard` is
the one call that is safe from any thread.
"""
import threading

import objc
from AppKit import (NSAlert, NSApp, NSApplicationActivateAllWindows, NSApplicationActivateIgnoringOtherApps,
                    NSBackingStoreBuffered, NSButton, NSColor, NSFont, NSMakeRect, NSPopUpButton, NSTextView, NSRunningApplication, NSScrollView,
                    NSSegmentedControl, NSTableColumn, NSTableView, NSViewHeightSizable, NSViewMaxYMargin,
                    NSViewMinXMargin, NSViewMinYMargin, NSViewWidthSizable, NSWindow,
                    NSWindowCollectionBehaviorFullScreenAuxiliary, NSWindowCollectionBehaviorMoveToActiveSpace,
                    NSWindowStyleMaskClosable, NSWindowStyleMaskMiniaturizable, NSWindowStyleMaskResizable,
                    NSWindowStyleMaskTitled, NSWorkspace)
from Foundation import NSDistributedNotificationCenter, NSIndexSet, NSObject, NSURL
from PyObjCTools import AppHelper

from atlas.battery_dashboard import _button, _label, _tile
from lib.identity import APP_ID
from unsub import OPEN_DASHBOARD

TITLE = "inbox report"
WIDTH, HEIGHT = 1000, 720
PAD = 16
DETAIL_HEIGHT = 128
REFRESH_SECONDS = 3
COLUMNS = [("sender", "Sender", 270), ("detail", "Evidence", 500), ("state", "Status", 180)]
SECTIONS = [("unsubscribe", "Unsubscribe"), ("block", "Block"), ("unsure", "Unsure"), ("manual", "Manual"),
            ("noncompliant", "Non-compliant"), ("auto", "Automatic")]
# Which buttons each tab offers, and the one Return presses.
OFFERS = {"unsubscribe": (("keep", "unsubscribe", "block"), "unsubscribe"),
          "block": (("keep", "unsubscribe", "block"), "block"),
          "unsure": (("keep", "unsubscribe", "block"), None),
          "manual": (("mark", "open"), None),
          "noncompliant": (("keep", "block"), "block"),
          "auto": (("override",), "override")}
INTRO = {"unsubscribe": "Senders you don't read, with a working unsubscribe. Unsubscribe uses their own way out.",
         "block": "Spam: failed authentication, lookalike domains, no way out. Block trashes that kind of mail from them.",
         "unsure": "The rules couldn't decide these. Pick what to do, or Keep.",
         "manual": "The automatic unsubscribe failed. Open their page and do it yourself, then Mark as Done. Or Keep, or Block.",
         "noncompliant": "Still mailing 10 days after unsubscribing. Block them.",
         "auto": "Done this week by your saved rules. Override deletes the rule and undoes a block."}


def _detail(entry):
    """The Evidence column."""
    section = entry["section"]
    if section == "manual":
        return entry.get("why") or ""
    if section == "noncompliant":
        return f"unsubscribed {entry.get('unsubscribed')} ({entry.get('method') or '?'}); {entry.get('since')}"
    if section == "auto":
        return f"{entry.get('action')} · {entry.get('rule')}"
    return entry.get("evidence", "")


def _state(entry):
    if entry["state"]:
        return entry["state"]
    if entry["section"] == "unsure":
        return "you decide"
    return f"→ {entry.get('proposal')}" if entry.get("proposal") else ""


def _more(entry):
    """The detail pane for one entry."""
    lines = [entry["sender"], _detail(entry)]
    if entry.get("reason") and entry["section"] in ("unsubscribe", "block", "unsure"):
        scores = entry.get("scores") or {}
        lines.append(f"why: {entry['reason']} (unwanted {scores.get('unwanted')}, spam {scores.get('spam')})")
    for key in ("notices", "note", "link"):
        if entry.get(key):
            lines.append(entry[key])
    if entry.get("detail") and entry["section"] == "auto":
        lines.append(entry["detail"])
    if entry.get("action_detail"):
        lines.append("result: " + entry["action_detail"])
    return "\n".join(line for line in lines if line)


class _InboxController(NSObject):
    """Table data source and delegate, and the target of the window's controls."""

    def initWithOwner_(self, owner):
        self = objc.super(_InboxController, self).init()
        if self is not None:
            self.owner = owner
        return self

    def numberOfRowsInTableView_(self, _table):
        return len(self.owner.rows)

    def tableView_objectValueForTableColumn_row_(self, _table, column, row):
        if row >= len(self.owner.rows):
            return ""
        entry, ident = self.owner.rows[row], str(column.identifier())
        return entry["sender"] if ident == "sender" else _detail(entry) if ident == "detail" else _state(entry)

    def tableViewSelectionDidChange_(self, _note):
        self.owner.selection_changed()

    def section_(self, sender):
        self.owner.pick_section(SECTIONS[sender.selectedSegment()][0])

    def decide_(self, sender):
        self.owner.decide(str(sender.identifier()))

    def mark_(self, sender):              # Manual's pull-down
        key = sender.selectedItem().representedObject() if sender.selectedItem() else None
        if key:
            self.owner.decide(str(key))

    def mode_(self, _sender):
        self.owner.toggle_mode()

    def analyse_(self, _sender):
        self.owner.analyse()

    def open_(self, _note):               # the distributed notification from a banner click
        open_dashboard()

    def read_(self, _sender):             # double-click on a row
        self.owner.read_clicked()

    def older_(self, _sender):
        self.owner.step(1)

    def newer_(self, _sender):
        self.owner.step(-1)


class Dashboard:
    def __init__(self):
        from atlas.edit_menu import install_edit_menu

        install_edit_menu()
        self.data = None
        self.section = None
        self.wanted = None              # a tab asked for before the first data arrived
        self.rows = []
        self.busy = False
        self.controller = _InboxController.alloc().initWithOwner_(self)
        style = (NSWindowStyleMaskTitled | NSWindowStyleMaskClosable | NSWindowStyleMaskResizable
                 | NSWindowStyleMaskMiniaturizable)
        self.window = NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
            NSMakeRect(0, 0, WIDTH, HEIGHT), style, NSBackingStoreBuffered, False)
        self.window.setTitle_(TITLE)
        self.window.setReleasedWhenClosed_(False)
        self.window.setMinSize_((820, 560))
        self.window.center()
        self.window.setFrameAutosaveName_(f"{APP_ID}.inbox")
        self.window.setCollectionBehavior_(NSWindowCollectionBehaviorMoveToActiveSpace
                                           | NSWindowCollectionBehaviorFullScreenAuxiliary)
        content = self.window.contentView()
        caption = NSColor.secondaryLabelColor()
        top = HEIGHT - PAD
        pinned_top = NSViewMinYMargin

        # ---- heading, mode switch, analyse
        self.heading = _label(NSMakeRect(PAD, top - 30, 500, 30), 22, 0.6)
        self.heading.setStringValue_("Inbox report")
        self.heading.setAutoresizingMask_(pinned_top)
        content.addSubview_(self.heading)
        self.subtitle = _label(NSMakeRect(PAD, top - 52, WIDTH - 2 * PAD - 420, 18), 12, color=caption)
        self.subtitle.setAutoresizingMask_(pinned_top | NSViewWidthSizable)
        content.addSubview_(self.subtitle)
        self.analyse_button = _button(NSMakeRect(WIDTH - PAD - 130, top - 34, 130, 32), "Analyse now",
                                      self.controller, "analyse:")
        self.analyse_button.setToolTip_("make a report now from all the mail captured so far")
        self.analyse_button.setAutoresizingMask_(pinned_top | NSViewMinXMargin)
        content.addSubview_(self.analyse_button)
        try:
            from AppKit import NSSwitch
            self.switch = NSSwitch.alloc().initWithFrame_(NSMakeRect(WIDTH - PAD - 190, top - 30, 40, 24))
        except ImportError:
            self.switch = NSButton.alloc().initWithFrame_(NSMakeRect(WIDTH - PAD - 190, top - 30, 40, 24))
            self.switch.setButtonType_(3)
            self.switch.setTitle_("")
        self.switch.setTarget_(self.controller)
        self.switch.setAction_("mode:")
        self.switch.setAutoresizingMask_(pinned_top | NSViewMinXMargin)
        self.switch.setToolTip_("off (testing): each decision applies once, and the sender can come up again.\n"
                                "on: every decision becomes a rule, and matching senders are handled without asking.")
        content.addSubview_(self.switch)
        switch_label = _label(NSMakeRect(WIDTH - PAD - 400, top - 27, 204, 18), 12, 0.3)
        switch_label.setStringValue_("Remember my decisions")
        switch_label.setAlignment_(2)
        switch_label.setAutoresizingMask_(pinned_top | NSViewMinXMargin)
        content.addSubview_(switch_label)

        # ---- the sections
        self.tabs = NSSegmentedControl.alloc().initWithFrame_(NSMakeRect(PAD, top - 92, WIDTH - 2 * PAD, 28))
        self.tabs.setSegmentCount_(len(SECTIONS))
        self.tabs.setTrackingMode_(0)                   # select one
        for index, (_key, name) in enumerate(SECTIONS):
            self.tabs.setLabel_forSegment_(name, index)
            self.tabs.setWidth_forSegment_((WIDTH - 2 * PAD - 12) / len(SECTIONS), index)
        self.tabs.setTarget_(self.controller)
        self.tabs.setAction_("section:")
        self.tabs.setAutoresizingMask_(pinned_top | NSViewWidthSizable)
        content.addSubview_(self.tabs)
        self.intro = _label(NSMakeRect(PAD, top - 118, WIDTH - 2 * PAD, 18), 12, color=caption)
        self.intro.setAutoresizingMask_(pinned_top | NSViewWidthSizable)
        content.addSubview_(self.intro)

        # ---- bottom: message, buttons, detail pane
        self.message = _label(NSMakeRect(PAD, PAD, WIDTH - 2 * PAD, 18), 12, color=caption)
        self.message.setAutoresizingMask_(NSViewWidthSizable | NSViewMaxYMargin)
        content.addSubview_(self.message)
        row_y = PAD + 26
        self.trash = NSButton.alloc().initWithFrame_(NSMakeRect(PAD, row_y + 4, 260, 22))
        self.trash.setButtonType_(3)                    # a checkbox
        self.trash.setTitle_("Block also trashes what of it is in the inbox")
        self.trash.setAutoresizingMask_(NSViewMaxYMargin)
        content.addSubview_(self.trash)
        self.buttons = {}
        self.row_y = row_y
        for key, title, tip in (
                ("keep", "Keep", "leave this category of their mail be (in permanent mode: never propose it again)"),
                ("unsubscribe", "Unsubscribe", "one-click, then their mailto, then their page in a "
                                               "locked-down browser; if all fail it comes back under Manual"),
                ("block", "Block", "this category of their mail (the row's ads, list or other mail) goes to Trash as it "
                                   "arrives; their receipts and other categories are never touched"),
                ("override", "Override", "delete the rule; a block is undone, an unsubscribe can't be (sign up again)"),
                ("open", "Open their page", "open the unsubscribe link in your browser to do it yourself")):
            button = _button(NSMakeRect(0, row_y, 150 if key == "open" else 110, 32), title, self.controller, "decide:")
            button.setIdentifier_(key)
            button.setToolTip_(tip)
            button.setAutoresizingMask_(NSViewMinXMargin | NSViewMaxYMargin)
            content.addSubview_(button)
            self.buttons[key] = button
        # Manual's choices, in one pull-down: Done (you unsubscribed at their page), Keep or Block.
        mark = NSPopUpButton.alloc().initWithFrame_pullsDown_(NSMakeRect(0, row_y, 130, 32), True)
        mark.addItemWithTitle_("Mark as…")              # a pull-down's first item is its title
        for key, title, tip in (
                ("done", "Done: I unsubscribed", "you unsubscribed at their page: they count as unsubscribed now, and "
                                                 "if they still mail after 10 days they come up as non-compliant"),
                ("keep", "Keep", "leave this category of their mail be"),
                ("block", "Block", "this category of their mail goes to Trash as it arrives")):
            mark.addItemWithTitle_(title)
            mark.lastItem().setRepresentedObject_(key)
            mark.lastItem().setToolTip_(tip)
        mark.setTarget_(self.controller)
        mark.setAction_("mark:")
        mark.setAutoresizingMask_(NSViewMinXMargin | NSViewMaxYMargin)
        content.addSubview_(mark)
        self.buttons["mark"] = mark
        detail_y = row_y + 32 + 10
        tile = _tile(NSMakeRect(PAD, detail_y, WIDTH - 2 * PAD, DETAIL_HEIGHT))
        tile.setAutoresizingMask_(NSViewWidthSizable | NSViewMaxYMargin)
        content.addSubview_(tile)
        self.more = _label(NSMakeRect(12, 8, WIDTH - 2 * PAD - 24, DETAIL_HEIGHT - 16), 12, wrap=True)
        self.more.setSelectable_(True)
        self.more.setAutoresizingMask_(NSViewWidthSizable | NSViewHeightSizable)
        tile.contentView().addSubview_(self.more)

        # ---- the table
        table_bottom = detail_y + DETAIL_HEIGHT + 10
        scroll = NSScrollView.alloc().initWithFrame_(NSMakeRect(PAD, table_bottom, WIDTH - 2 * PAD,
                                                                top - 126 - table_bottom))
        scroll.setHasVerticalScroller_(True)
        scroll.setBorderType_(1)
        scroll.setAutoresizingMask_(NSViewWidthSizable | NSViewHeightSizable)
        self.table = NSTableView.alloc().initWithFrame_(scroll.bounds())
        for ident, title, width in COLUMNS:
            column = NSTableColumn.alloc().initWithIdentifier_(ident)
            column.headerCell().setStringValue_(title)
            column.setWidth_(width)
            self.table.addTableColumn_(column)
        self.table.setAllowsMultipleSelection_(True)
        self.table.setUsesAlternatingRowBackgroundColors_(True)
        self.table.setRowHeight_(22)
        self.table.setDataSource_(self.controller)
        self.table.setDelegate_(self.controller)
        self.table.setTarget_(self.controller)
        self.table.setDoubleAction_("read:")
        scroll.setDocumentView_(self.table)
        content.addSubview_(scroll)

        self.subtitle.setStringValue_("loading…")
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

    def _tick(self):
        if self.window.isVisible():
            self.refresh()
        AppHelper.callLater(REFRESH_SECONDS, self._tick)

    def _in_background(self, job, then):
        def work():
            try:
                result = job()
            except Exception as error:          # shown, not raised: the window stays usable
                result = {"error": f"{type(error).__name__}: {error}"}
            AppHelper.callAfter(then, result)

        threading.Thread(target=work, daemon=True).start()

    def refresh(self):
        if self.busy:
            return
        self.busy = True
        from unsub import ops
        self._in_background(ops.board, self._update)

    def _update(self, data):
        self.busy = False
        if "error" in data:
            self.set_message(f"could not read the report: {data['error']}")
            return
        first = self.data is None or (data["report"] or {}).get("id") != (self.data["report"] or {}).get("id")
        self.data = data
        self.switch.setState_(1 if data["mode"] == "permanent" else 0)
        self.analyse_button.setEnabled_(not data["analysing"])
        self.analyse_button.setTitle_("Analysing…" if data["analysing"] else "Analyse now")
        report = data["report"]
        sections = data["sections"]
        waiting = sum(e["pending"] for entries in sections.values() for e in entries if e["section"] != "auto")
        if report is None:
            self.subtitle.setStringValue_("no report yet")
        else:
            self.subtitle.setStringValue_(
                f"report {report['id']} · {report['date']} · {report['messages']} emails from {report['senders']} senders · "
                + (f"{waiting} waiting for you" if waiting else "nothing waiting for you")
                + (f" · {data['busy']} action{'s' if data['busy'] != 1 else ''} running" if data["busy"] else ""))
        for index, (key, name) in enumerate(SECTIONS):
            entries = sections[key]
            pending = sum(e["pending"] for e in entries)
            self.tabs.setLabel_forSegment_(f"{name} {len(entries)}" + (" •" if pending and key != "auto" else ""), index)
        if self.wanted:
            self.section, self.wanted = self.wanted, None
        elif first or self.section is None:
            self.section = next((k for k, _n in SECTIONS if any(e["pending"] for e in sections[k]) and k != "auto"),
                                next((k for k, _n in SECTIONS if sections[k]), "unsubscribe"))
        self._show_rows()

    def _show_rows(self):
        picked = {self.rows[i]["sender_id"] for i in self._selected() if i < len(self.rows)}
        self.tabs.setSelectedSegment_([k for k, _n in SECTIONS].index(self.section))
        self.intro.setStringValue_(INTRO[self.section])
        self.rows = (self.data or {}).get("sections", {}).get(self.section, [])
        self.table.reloadData()
        keep = NSIndexSet.indexSet().mutableCopy()
        for i, entry in enumerate(self.rows):
            if entry["sender_id"] in picked:
                keep.addIndex_(i)
        self.table.selectRowIndexes_byExtendingSelection_(keep, False)
        self.selection_changed()

    def pick_section(self, key):
        self.section = key
        self.table.deselectAll_(None)
        self._show_rows()

    def _selected(self):
        indexes = self.table.selectedRowIndexes()
        found, i = [], indexes.firstIndex()
        while i is not None and i < 1 << 62:
            found.append(i)
            i = indexes.indexGreaterThanIndex_(i)
        return found

    def _picked(self):
        return [self.rows[i] for i in self._selected() if i < len(self.rows)]

    def selection_changed(self):
        picked = self._picked()
        offers, default = OFFERS[self.section]
        x = self.window.contentView().frame().size.width - PAD     # the offered ones, packed from the right
        for key, button in self.buttons.items():
            button.setHidden_(key not in offers)
            if key in offers:
                x -= button.frame().size.width
                button.setFrameOrigin_((x, self.row_y))
                x -= 6
            enabled = bool(picked)
            if key == "open":
                enabled = len(picked) == 1 and bool(picked[0].get("link"))
            button.setEnabled_(enabled)
            button.setKeyEquivalent_("\r" if key == default else "")
        self.trash.setHidden_("block" not in offers and "mark" not in offers)
        if len(picked) == 1:
            self.more.setStringValue_(_more(picked[0]))
        elif picked:
            self.more.setStringValue_(f"{len(picked)} senders picked: " + ", ".join(e["sender"] for e in picked[:12])
                                      + ("…" if len(picked) > 12 else ""))
        elif self.data and self.data["report"] is None:
            self.more.setStringValue_("No report yet. The weekly one comes on Sunday at 9; Analyse now makes one from "
                                      "the mail captured so far.")
        else:
            self.more.setStringValue_("Pick a sender to see why it is here (Cmd-click or Shift-click for several, "
                                      "Cmd-A for all); double-click to read their mail." if self.rows
                                      else "Nothing here this week.")

    def read_clicked(self):
        row = self.table.clickedRow()
        if row is None or row < 0 or row >= len(self.rows):
            return
        entry = self.rows[row]
        reader = _dashboard.get("reader")
        if reader is None:
            reader = _dashboard["reader"] = Reader()
        reader.open(entry)

    def decide(self, key):
        picked = self._picked()
        if not picked:
            self.set_message("pick one or more senders first")
            return
        from unsub import ops
        ids = ",".join(f"s{e['sender_id']}" for e in picked)
        if key == "open":
            NSWorkspace.sharedWorkspace().openURL_(NSURL.URLWithString_(picked[0]["link"]))
            self.set_message("opened in your browser: when you have unsubscribed there, Mark as… Done")
            return
        if key == "override":
            job = lambda: {"done": [ops.override(f"s{e['sender_id']}") for e in picked]}
        else:
            trash = bool(self.trash.state()) if key == "block" else None
            job = lambda: ops.decide({key: ids}, self.section, trash)
        self.set_message({"keep": "keeping", "done": "marking as unsubscribed", "unsubscribe": "unsubscribing from",
                          "block": "blocking", "override": "overriding"}[key] + f" {len(picked)}…")
        self._in_background(job, self._decided)

    def _decided(self, result):
        if "error" in result:
            self.set_message(f"that broke: {result['error']}")
        elif result.get("errors"):
            self.set_message(" · ".join(result["errors"]))
        elif "decided" in result:
            names = ", ".join(d["sender"].split(" <")[0] for d in result["decided"][:4])
            done = {"done": "marked as unsubscribed", "keep": "kept"}
            self.set_message(f"{names}: " + (result.get("working")
                                             or " · ".join(sorted({done.get(d["action"], d["action"]) for d in result["decided"]})))
                             + (" · saved as rules" if result.get("rules") else ""))
        else:
            notes = [o.get("resubscribe") or (f"unblocked {', '.join(o['unblocked'])}" if o.get("unblocked") else "")
                     or "rule deleted" for o in result.get("done", [])]
            self.set_message(" · ".join(n for n in notes if n))
        self.refresh()

    def toggle_mode(self):
        turning_on = bool(self.switch.state())
        from unsub import ops
        if not turning_on:
            ops.set_mode("temporary")
            self.set_message("decisions apply once again; saved rules are kept but not used while this is off")
            self.refresh()
            return
        preview = ops.promote(False, 1, 50)
        rules = "promoted"
        if preview["total"]:
            alert = NSAlert.alloc().init()
            alert.setMessageText_("Remember your decisions from now on?")
            listing = "\n".join(f"{p['sender']}: {p['rule']}" for p in preview["items"][:15])
            more = f"\n… and {preview['total'] - 15} more" if preview["total"] > 15 else ""
            alert.setInformativeText_(f"You decided on {preview['total']} senders while testing. They can become rules "
                                      f"now too:\n\n{listing}{more}")
            alert.addButtonWithTitle_("Turn on and keep these")
            alert.addButtonWithTitle_("Turn on, start fresh")
            alert.addButtonWithTitle_("Cancel")
            answer = alert.runModal()
            if answer == 1002:                          # third button
                self.switch.setState_(0)
                return
            if answer == 1000:
                ops.promote(True)
            else:
                rules = "fresh"
        ops.set_mode("permanent")
        self.set_message("decisions are remembered now: matching senders are handled for you and listed under "
                         "Automatic" + (", your testing decisions included" if rules == "promoted" and preview["total"] else ""))
        self.refresh()

    def analyse(self):
        from unsub import ops
        ops.start_analysis()
        self.analyse_button.setEnabled_(False)
        self.analyse_button.setTitle_("Analysing…")
        self.set_message("analysing: the report appears here when it is ready")


class Reader:
    """A window to read a sender's messages, newest first, as text: double-click a row in the dashboard.

    Messages are read through the Gmail API, which leaves them unread (opening one in Gmail would
    mark it read, and the agent would count that as you opening it), and as text, so no images or
    tracking pixels load."""

    def __init__(self):
        self.controller = _InboxController.alloc().initWithOwner_(self)
        self.ids, self.index, self.entry, self.loading = [], 0, None, False
        style = (NSWindowStyleMaskTitled | NSWindowStyleMaskClosable | NSWindowStyleMaskResizable
                 | NSWindowStyleMaskMiniaturizable)
        width, height = 780, 640
        self.window = NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
            NSMakeRect(0, 0, width, height), style, NSBackingStoreBuffered, False)
        self.window.setTitle_("message")
        self.window.setReleasedWhenClosed_(False)
        self.window.setMinSize_((520, 360))
        self.window.center()
        self.window.setFrameAutosaveName_(f"{APP_ID}.inbox.message")
        self.window.setCollectionBehavior_(NSWindowCollectionBehaviorMoveToActiveSpace
                                           | NSWindowCollectionBehaviorFullScreenAuxiliary)
        content = self.window.contentView()
        caption = NSColor.secondaryLabelColor()
        top = height - PAD
        pinned_top = NSViewMinYMargin
        self.subject = _label(NSMakeRect(PAD, top - 24, width - 2 * PAD, 24), 17, 0.6)
        self.subject.setAutoresizingMask_(pinned_top | NSViewWidthSizable)
        self.headers = _label(NSMakeRect(PAD, top - 82, width - 2 * PAD, 54), 12, color=caption, wrap=True)
        self.headers.setSelectable_(True)
        self.headers.setAutoresizingMask_(pinned_top | NSViewWidthSizable)
        for view in (self.subject, self.headers):
            content.addSubview_(view)
        bottom = PAD + 32 + 10
        scroll = NSScrollView.alloc().initWithFrame_(NSMakeRect(PAD, bottom, width - 2 * PAD, top - 92 - bottom))
        scroll.setHasVerticalScroller_(True)
        scroll.setBorderType_(1)
        scroll.setAutoresizingMask_(NSViewWidthSizable | NSViewHeightSizable)
        self.text = NSTextView.alloc().initWithFrame_(scroll.bounds())
        self.text.setEditable_(False)
        self.text.setSelectable_(True)
        self.text.setRichText_(False)
        self.text.setFont_(NSFont.systemFontOfSize_(13))
        self.text.setTextContainerInset_((8, 8))
        self.text.setAutoresizingMask_(NSViewWidthSizable)
        scroll.setDocumentView_(self.text)
        content.addSubview_(scroll)
        self.older = _button(NSMakeRect(PAD, PAD, 100, 32), "‹ Older", self.controller, "older:")
        self.newer = _button(NSMakeRect(PAD + 106, PAD, 100, 32), "Newer ›", self.controller, "newer:")
        for button in (self.older, self.newer):
            button.setAutoresizingMask_(NSViewMaxYMargin)
            content.addSubview_(button)
        self.position = _label(NSMakeRect(PAD + 220, PAD + 6, width - 2 * PAD - 220, 18), 12, color=caption)
        self.position.setAutoresizingMask_(NSViewWidthSizable | NSViewMaxYMargin)
        content.addSubview_(self.position)

    def open(self, entry):
        from unsub import ops
        self.entry = entry
        self.ids = ops.message_ids(entry["sender_id"], entry.get("message_ids") or ())
        self.index = 0
        self.window.setTitle_(entry["sender"])
        self.window.orderFrontRegardless()
        NSApp.activateIgnoringOtherApps_(True)
        self.window.makeKeyAndOrderFront_(None)
        self.load()

    def step(self, by):
        if not self.loading and 0 <= self.index + by < len(self.ids):
            self.index += by
            self.load()

    def load(self):
        self.older.setEnabled_(False)
        self.newer.setEnabled_(False)
        if not self.ids:
            self.subject.setStringValue_("no message to show")
            self.headers.setStringValue_("this sender's messages were not kept")
            self.text.setString_("")
            self.position.setStringValue_("")
            return
        self.loading = True
        self.subject.setStringValue_("loading…")
        message_id = self.ids[self.index]

        def work():
            from unsub import ops
            try:
                found = ops.read_message(message_id)
            except Exception as error:          # shown, not raised
                found = {"id": message_id, "error": f"{type(error).__name__}: {error}"}
            AppHelper.callAfter(self.show, found)

        threading.Thread(target=work, daemon=True).start()

    def show(self, found):
        self.loading = False
        self.older.setEnabled_(self.index + 1 < len(self.ids))
        self.newer.setEnabled_(self.index > 0)
        self.position.setStringValue_(f"{self.index + 1} of {len(self.ids)}, newest first · read here, it stays unread "
                                      "in Gmail and no images load")
        if found.get("gone") or found.get("error"):
            self.subject.setStringValue_("not in Gmail any more" if found.get("gone") else "could not load it")
            self.headers.setStringValue_(found.get("error") or "deleted for good, or Trash was emptied (after 30 days)")
            self.text.setString_("")
            return
        self.subject.setStringValue_(found["subject"])
        self.headers.setStringValue_(f"from {found['from']}\nto {found['to']}\n{found['date']} · in {found['where']}"
                                     + (" · unread" if found["unread"] else ""))
        self.text.setString_(found["text"])
        self.text.scrollRangeToVisible_((0, 0))


_dashboard = {}


def open_dashboard(message="", section=None):
    """Show the dashboard (building it the first time), from any thread. section: the tab to show."""

    def on_main():
        if "window" not in _dashboard:
            _dashboard["window"] = Dashboard()
        board = _dashboard["window"]
        if section in dict(SECTIONS):
            board.section = section
            board.wanted = section
        board.show(message)

    AppHelper.callAfter(on_main)


def listen():
    """Open the dashboard whenever unsub.OPEN_DASHBOARD is posted (a banner click). Call once, on the main thread."""
    listener = _InboxController.alloc().initWithOwner_(None)
    _dashboard["listener"] = listener
    NSDistributedNotificationCenter.defaultCenter().addObserver_selector_name_object_(
        listener, "open:", OPEN_DASHBOARD, None)


def post_open():
    """Ask the running assistant to open the dashboard (from another process)."""
    NSDistributedNotificationCenter.defaultCenter().postNotificationName_object_userInfo_deliverImmediately_(
        OPEN_DASHBOARD, None, None, True)
