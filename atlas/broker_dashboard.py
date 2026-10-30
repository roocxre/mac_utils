"""The broker dashboard: the data broker agent's report, to read and decide on (brokers/).

A window in the assistant process, like the inbox dashboard: built once, hidden when closed. The
report's groups (Needs approval, Done by the agent, Manual tasks, Refused or no response,
Verified removed, Registry maintenance, One-time opt-outs) are collapsible sections; inside each,
every broker is a collapsible section of its own, collapsed by default, whose line shows its name,
kind, state and the proposed action. Opened, it shows what the broker does, what it has on you,
exactly which details would be sent, and its history; the pane on the right shows the whole
section, with the listing's screenshot when there is one.

Pick brokers (Cmd/Shift-click for several) and press Approve or Reject, or use "Approve all…" for
a batch (every email request, every people-search site, a tier). Manual tasks have their link and
Done; possible matches have It's me / Not me. "Your details" is the only place your details are
entered: they go into the encrypted vault (brokers/vault.py).

It opens with "broker report" (or "brokers", "data brokers"…) and when the weekly banner is
clicked: that posts brokers.OPEN_DASHBOARD, which listen() hears.
"""
import threading

import objc
from AppKit import (NSApp, NSApplicationActivateAllWindows, NSApplicationActivateIgnoringOtherApps,
                    NSBackingStoreBuffered, NSButton, NSColor, NSFont, NSImage, NSImageView, NSMakeRect,
                    NSOutlineView, NSPasteboard, NSPasteboardTypeString, NSPopUpButton, NSRunningApplication,
                    NSScrollView, NSTableColumn, NSTextField, NSTextView, NSViewHeightSizable, NSViewMaxXMargin,
                    NSViewMaxYMargin, NSViewMinXMargin, NSViewMinYMargin, NSViewWidthSizable, NSWindow,
                    NSWindowCollectionBehaviorFullScreenAuxiliary, NSWindowCollectionBehaviorMoveToActiveSpace,
                    NSWindowStyleMaskClosable, NSWindowStyleMaskMiniaturizable, NSWindowStyleMaskResizable,
                    NSWindowStyleMaskTitled, NSWorkspace)
from Foundation import NSData, NSDistributedNotificationCenter, NSIndexSet, NSObject, NSString, NSURL
from PyObjCTools import AppHelper

from atlas.battery_dashboard import _button, _label, _tile
from brokers import OPEN_DASHBOARD
from brokers.templates import LABELS
from lib.identity import APP_ID, APP_NAME

TITLE = f"{APP_NAME} · broker report"
WIDTH, HEIGHT = 1160, 780
PAD = 16
LIST_WIDTH = 640
REFRESH_SECONDS = 15
GROUPS = [("approval", "Needs approval"), ("auto", "Done by the agent"), ("manual", "Manual tasks"),
          ("waiting", "Waiting for an answer"), ("refused", "Refused or no response"), ("removed", "Removed"),
          ("maintenance", "Registry maintenance"), ("oneoffs", "One-time opt-outs")]
OPEN_AT_START = ("approval", "auto", "manual", "waiting", "oneoffs")
# The buttons each group offers, and the one Return presses.
# "outcome" is the Mark as… menu: submitted (waiting), removed, rejected, and for manual tasks
# skipped (off the task list, the broker still due: ops._skip).
OUTCOME_CHOICES = [("submitted", "Submitted, waiting for them"), ("removed", "Submitted, removed"),
                   ("rejected", "Submitted, rejected"), ("skipped", "Skipped")]
TASK_ONLY_OUTCOMES = ("skipped",)
OFFERS = {"approval": (("reject", "approve"), "approve"), "auto": (("override",), None),
          "manual": (("handoff", "email", "open", "not_me", "its_me", "not_listed", "listed", "outcome"), None),
          "waiting": (("outcome",), None),
          "refused": (("outcome", "regulator", "copy"), "copy"), "removed": (("outcome",), None), "maintenance": (("file",), "file"),
          "oneoffs": (("open", "done"), "done")}
TIERS = {1: "tier 1: confirmed exposure", 2: "tier 2: crucial people-search", 3: "tier 3: people-search",
         4: "tier 4: identity / marketing", 5: "tier 5: long tail"}
METHOD_WORDS = {"email": "email", "direct-form": "their form", "search-form": "their form", "generic": "their opt-out page",
                "privacy-manager": "their privacy portal", "manual": "you, by hand"}


def _action(item):
    """The proposed action, in a few words."""
    if item.get("pending"):
        kind = item["pending"]["kind"]
        return {"confirm": "→ open their confirmation link", "more-info": "→ answer: " + ", ".join(
            item["pending"].get("asked", [])), "follow-up": "→ follow up", "resubmit": "→ ask again" + (" with your " + ", ".join(
            _name(e).lower() for e in item["pending"]["asked"]) if item["pending"].get("asked") else "")}.get(kind, kind)
    proposal = item.get("proposal") or {}
    if proposal.get("missing"):
        return "needs your " + ", ".join(_name(e).lower() for e in proposal["missing"])
    if proposal.get("manual"):
        return "→ manual (" + {"id": "asks for ID", "phone": "phone call", "account": "account",
                                "no-method": "no automatic way"}.get(proposal["manual"], proposal["manual"]) + ")"
    return f"→ {METHOD_WORDS.get(proposal.get('method'), proposal.get('method'))}: " + ", ".join(
        _name(e).lower() for e in proposal.get("elements", []))


def _name(element):
    return LABELS.get(element, element)


def _line(group, item):
    """The one-line summary of an entry."""
    if group == "approval":
        return f"{item['name']}  ·  {item['category']}  ·  {_action(item)}"
    if group == "auto":
        return f"{item['name']}  ·  {item['kind']} ({item.get('method') or ''}) {item.get('at') or ''}"
    if group == "manual":
        return f"{item['name']}  ·  {item['what']}: {item['title']}"
    if group == "oneoffs":
        return f"{item['name']}  ·  {item['what']}"
    if group == "maintenance":
        return f"{item['name']}  ·  {item['detail']}"
    return f"{item['name']}  ·  {item.get('category', '')}  ·  since {item.get('since') or '?'}"


def _status(group, item):
    if group == "removed":
        return "verified" if item["state"] == "verified_removed" else "they say so"
    if group == "approval":
        return item["listed"] if item.get("listed") not in (None, "unknown") else item["state"]
    if group == "auto":
        return item.get("status") or ""
    if group == "manual":
        return f"since {item.get('since')}"
    return item.get("state", "")


def _children(group, item):
    """The lines inside an entry's section (its expanded view in the outline)."""
    if group in ("approval", "waiting", "refused", "removed"):
        lines = [f"does: {item.get('summary') or 'not described in the registry yet'}"
                 + (f"  ({item['parent']})" if item.get("parent") else "")]
        shown = item.get("shown") or {}
        listings = shown.get("listings") or []
        if listings:
            lines.append("has on you: " + "; ".join(
                f"{l['verdict']} listing ({l['score']}): {', '.join((l['fields'].get('places') or [])[:3])}"
                for l in listings[:3]))
        elif shown.get("data_categories"):
            lines.append("files that it holds: " + ", ".join(shown["data_categories"][:8]))
        else:
            lines.append("has on you: unknown (no public search; the request asks them)" if group == "approval"
                         else "has on you: " + (item.get("listed") or "unknown"))
        proposal = item.get("proposal") or {}
        if group == "approval" and proposal and not proposal.get("manual"):
            where = proposal.get("to") or proposal.get("url") or ""
            lines.append(f"would send by {METHOD_WORDS.get(proposal['method'], proposal['method'])} {where}: "
                         + " · ".join(f"{_name(k)}: {v}" for k, v in proposal.get("values", {}).items()))
        lines.append(f"{TIERS.get(item.get('tier'), '')} · state {item['state']} since {item.get('since')}")
        return lines
    if group == "manual":
        return list(item.get("steps") or [])[:4] + ([item["link"]] if item.get("link") else [])
    if group == "oneoffs":
        return [item["steps"], item["url"]]
    if group == "auto":
        return [item.get("detail") or ""]
    return []


def _section(group, item, history=None):
    """The whole section, for the pane on the right."""
    out = [item["name"]]
    if group in ("approval", "waiting", "refused", "removed"):
        out.append(f"{item['category']} · {TIERS.get(item.get('tier'), '')}" + (f" · {item['parent']}" if item.get("parent")
                                                                               else ""))
        out.append(item.get("website") or "")
        out.append("")
        out.append("What it does")
        out.append("  " + (item.get("summary") or "Not described in the registry yet."))
        out.append("")
        out.append("What it has on you")
        shown = item.get("shown") or {}
        for listing in shown.get("listings") or []:
            fields = listing["fields"]
            out.append(f"  {listing['verdict']} listing, score {listing['score']}: {listing['url']}")
            for key in ("age", "places", "streets", "phones", "emails", "relatives"):
                if fields.get(key):
                    value = fields[key] if not isinstance(fields[key], list) else ", ".join(fields[key])
                    out.append(f"    {key}: {value}")
            if fields.get("why"):
                out.append("    why it's you: " + "; ".join(fields["why"]))
        if shown.get("data_categories"):
            out.append("  Its registry filing says it collects: " + ", ".join(shown["data_categories"]))
        for sent in shown.get("files") or []:
            if sent.get("kinds"):
                out.append(f"  The file it sent you ({sent['filename']}, {sent['date']}): {sent['filled']} of "
                           f"{sent['fields']} fields filled in")
                out.append("    " + ", ".join(sent["kinds"]))
                out += [f"    {field}: {value}" for field, value in sent.get("values") or []]
            else:
                out.append(f"  A file it sent you: {sent['filename']}, {sent['date']} ("
                           + ("kept in the vault" if sent["kept"] else "too big to keep; it's in their email") + ")")
        if shown.get("access_reply"):
            out.append("  Its reply about what it holds:\n    " + shown["access_reply"][:2000].replace("\n", "\n    "))
        if not shown.get("listings") and not shown.get("data_categories") and not shown.get("access_reply") \
                and not shown.get("files"):
            out.append("  Unknown: it has no public search" + (", and the request asks them." if group == "approval"
                                                               else "."))
        out.append("")
        proposal = item.get("proposal") or {}
        if item.get("pending"):
            out.append("Waiting for you: " + _action(item).lstrip("→ "))
            if item["pending"].get("grows"):
                out.append("  This would send details beyond what you approved before.")
        if group == "approval" and proposal:
            out.append("Proposed: " + _action(item).lstrip("→ "))
            where = proposal.get("to") or proposal.get("url")
            if where:
                out.append(f"  to {where}")
            if proposal.get("values"):
                out.append("  Exactly these details, and nothing else:")
                for key, value in proposal["values"].items():
                    out.append(f"    {_name(key)}: {value}".replace("\n", "\n      "))
            if proposal.get("access"):
                out.append("  The email also asks what they hold on you, and who they sold it to.")
        if item.get("approved"):
            approved = item["approved"]
            out.append(f"Approved: {METHOD_WORDS.get(approved.get('method'), approved.get('method'))} with "
                       + ", ".join(_name(e).lower() for e in approved.get("elements", [])))
        if item.get("todo"):
            out.append("")
            out.append("Registry fields nobody has checked yet: " + ", ".join(item["todo"]))
        if item.get("complaint"):
            out.append("")
            out.append("Complaint draft (you file it; Copy puts it on the clipboard):")
            out.append(item["complaint"])
    elif group == "manual":
        out.append(item["title"])
        out.append("")
        out += [f"  {step}" for step in item.get("steps") or []]
        if item.get("listing"):
            fields = item["listing"]["fields"]
            out.append("")
            out.append(f"The listing (score {item['listing']['score']}): {item['listing']['url']}")
            for key in ("age", "places", "phones", "relatives", "why"):
                if fields.get(key):
                    out.append(f"  {key}: {fields[key] if not isinstance(fields[key], list) else ', '.join(fields[key])}")
        if item.get("link"):
            out.append("")
            out.append(item["link"])
        if item.get("email"):
            out.append("Their email: Gmail, label Data brokers (Open email)")
    elif group == "oneoffs":
        out += [item["what"], "", item["steps"], "", item["url"]]
    elif group == "auto":
        out += [f"{item['kind']} · {item.get('status')} · {item.get('at')}", item.get("detail") or "",
                "", "Override cancels what's still queued and withdraws this broker's standing approval: "
                    "anything more for it asks you first."]
    elif group == "maintenance":
        out += [item["detail"], "", f"Registry file: {item['file']}", item.get("website") or ""]
    if history:
        out.append("")
        out.append("History")
        out += [f"  {h['at']}  {h['event']}" + (f": {h['detail']}" if h.get("detail") else "") for h in history]
    return "\n".join(out)


class _BrokerController(NSObject):
    """The outline's data source and delegate, and the target of the window's controls."""

    def initWithOwner_(self, owner):
        self = objc.super(_BrokerController, self).init()
        if self is not None:
            self.owner = owner
        return self

    # ---- outline data
    def outlineView_numberOfChildrenOfItem_(self, _view, node):
        return len(self.owner.kids(node))

    def outlineView_child_ofItem_(self, _view, index, node):
        return self.owner.kids(node)[index]

    def outlineView_isItemExpandable_(self, _view, node):
        return bool(self.owner.kids(node))

    def outlineView_objectValueForTableColumn_byItem_(self, _view, column, node):
        return self.owner.cell(node, str(column.identifier()))

    def outlineViewSelectionDidChange_(self, _note):
        self.owner.selection_changed()

    def outlineView_shouldSelectItem_(self, _view, node):
        return not str(node).startswith("d|")

    # ---- controls
    def decide_(self, sender):
        self.owner.decide(str(sender.identifier()))

    def outcome_(self, sender):
        self.owner.mark(sender.indexOfSelectedItem())

    def batch_(self, sender):
        self.owner.batch(sender.indexOfSelectedItem())

    def mode_(self, _sender):
        self.owner.toggle_mode()

    def details_(self, _sender):
        self.owner.open_details()

    def run_(self, _sender):
        self.owner.run_now()

    def open_(self, _note):               # the distributed notification from a banner click
        open_dashboard()

    def save_(self, _sender):
        self.owner.save()

    def cancel_(self, _sender):
        self.owner.window.orderOut_(None)


class Dashboard:
    def __init__(self):
        from atlas.edit_menu import install_edit_menu

        install_edit_menu()
        self.data = None
        self.busy = False
        self.nodes = {}                 # key -> the NSString the outline holds
        self.items = {}                 # key -> (group, entry)
        self.histories = {}
        self.first = True
        self.controller = _BrokerController.alloc().initWithOwner_(self)
        style = (NSWindowStyleMaskTitled | NSWindowStyleMaskClosable | NSWindowStyleMaskResizable
                 | NSWindowStyleMaskMiniaturizable)
        self.window = NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
            NSMakeRect(0, 0, WIDTH, HEIGHT), style, NSBackingStoreBuffered, False)
        self.window.setTitle_(TITLE)
        self.window.setReleasedWhenClosed_(False)
        self.window.setMinSize_((960, 620))
        self.window.center()
        self.window.setFrameAutosaveName_(f"{APP_ID}.brokers")
        self.window.setCollectionBehavior_(NSWindowCollectionBehaviorMoveToActiveSpace
                                           | NSWindowCollectionBehaviorFullScreenAuxiliary)
        content = self.window.contentView()
        caption = NSColor.secondaryLabelColor()
        top = HEIGHT - PAD
        pinned_top = NSViewMinYMargin

        # ---- heading, totals, details, run, mode
        self.heading = _label(NSMakeRect(PAD, top - 30, 420, 30), 22, 0.6)
        self.heading.setStringValue_("Broker report")
        self.heading.setAutoresizingMask_(pinned_top)
        content.addSubview_(self.heading)
        self.subtitle = _label(NSMakeRect(PAD, top - 54, WIDTH - 2 * PAD - 520, 18), 12, color=caption)
        self.subtitle.setAutoresizingMask_(pinned_top | NSViewWidthSizable)
        content.addSubview_(self.subtitle)
        x = WIDTH - PAD
        for key, title, action, tip, width in (
                ("run", "Run now", "run:", "one pass now: replies, a search if one is due, what's queued to send", 100),
                ("details", "Your details", "details:", "your name, addresses, phones… kept encrypted on this Mac", 120)):
            x -= width
            button = _button(NSMakeRect(x, top - 34, width, 32), title, self.controller, action)
            button.setToolTip_(tip)
            button.setAutoresizingMask_(pinned_top | NSViewMinXMargin)
            content.addSubview_(button)
            setattr(self, f"{key}_button", button)
            x -= 8
        try:
            from AppKit import NSSwitch
            self.switch = NSSwitch.alloc().initWithFrame_(NSMakeRect(x - 44, top - 30, 40, 24))
        except ImportError:
            self.switch = NSButton.alloc().initWithFrame_(NSMakeRect(x - 44, top - 30, 40, 24))
            self.switch.setButtonType_(3)
            self.switch.setTitle_("")
        self.switch.setTarget_(self.controller)
        self.switch.setAction_("mode:")
        self.switch.setAutoresizingMask_(pinned_top | NSViewMinXMargin)
        self.switch.setToolTip_("on (standing): once you approve a broker, re-submissions, follow-ups, answers that "
                                "only use what you approved, and confirmation links happen without asking, and are "
                                "listed under Done by the agent.\noff (per request): every action asks.")
        content.addSubview_(self.switch)
        switch_label = _label(NSMakeRect(x - 44 - 170, top - 27, 164, 18), 12, 0.3)
        switch_label.setStringValue_("Standing approvals")
        switch_label.setAlignment_(2)
        switch_label.setAutoresizingMask_(pinned_top | NSViewMinXMargin)
        content.addSubview_(switch_label)

        # ---- bottom: message, buttons
        self.message = _label(NSMakeRect(PAD, PAD, WIDTH - 2 * PAD, 18), 12, color=caption)
        self.message.setAutoresizingMask_(NSViewWidthSizable | NSViewMaxYMargin)
        content.addSubview_(self.message)
        self.row_y = PAD + 26
        self.batch_menu = NSPopUpButton.alloc().initWithFrame_pullsDown_(NSMakeRect(PAD, self.row_y + 2, 260, 28), True)
        self.batch_menu.setTarget_(self.controller)
        self.batch_menu.setAction_("batch:")
        self.batch_menu.setAutoresizingMask_(NSViewMaxYMargin | NSViewMaxXMargin)
        content.addSubview_(self.batch_menu)
        self.buttons = {}
        for key, title, tip in (
                ("approve", "Approve", "send exactly what the section lists, by the method it says"),
                ("reject", "Reject", "leave this broker alone; it won't be proposed again"),
                ("override", "Override", "cancel what's still queued and withdraw the standing approval"),
                ("done", "Done", "you've done it (a form, a CAPTCHA, a call): the agent follows up from here"),
                ("listed", "I'm listed", "you searched and found yourself"),
                ("not_listed", "Not listed", "you searched and you're not there"),
                ("its_me", "It's me", "this listing is you: the opt-out will include it"),
                ("not_me", "Not me", "someone else with your name: it's left alone"),
                ("open", "Open link", "open the page in your browser"),
                ("email", "Open email", "their email, in Gmail under the Data brokers label"),
                ("handoff", "Pass the check", "a window of the agent's browser opens on this page: you pass the "
                                              "check (a CAPTCHA, Cloudflare), the agent fills in and sends the rest"),
                ("copy", "Copy complaint", "put the complaint draft on the clipboard"),
                ("regulator", "Open FTC", "the FTC's complaint site (your attorney general's is in the draft once "
                                          "you add it to brokers/config.json)"),
                ("file", "Open registry file", "the broker's YAML entry, to fix it")):
            button = _button(NSMakeRect(0, self.row_y, 150 if key in ("copy", "file") else 130 if key == "handoff" else 110, 32), title,
                             self.controller, "decide:")
            button.setIdentifier_(key)
            button.setToolTip_(tip)
            button.setAutoresizingMask_(NSViewMinXMargin | NSViewMaxYMargin)
            content.addSubview_(button)
            self.buttons[key] = button
        menu = NSPopUpButton.alloc().initWithFrame_pullsDown_(NSMakeRect(0, self.row_y + 2, 210, 28), True)
        menu.addItemWithTitle_("Mark as…")                # a pull-down's first item is its title
        for _key, title in OUTCOME_CHOICES:
            menu.addItemWithTitle_(title)
        menu.setTarget_(self.controller)
        menu.setAction_("outcome:")
        menu.setToolTip_("what happened with the opt-out: sent and waiting (the agent follows up), the site says "
                         "it's removed, or the site refused; or skipped: off the task list, but the agent asks again "
                         "if a later search finds them holding your data")
        menu.setAutoresizingMask_(NSViewMinXMargin | NSViewMaxYMargin)
        content.addSubview_(menu)
        self.buttons["outcome"] = menu

        # ---- the outline (left) and the section pane (right)
        body_bottom = self.row_y + 32 + 12
        body_top = top - 66
        scroll = NSScrollView.alloc().initWithFrame_(NSMakeRect(PAD, body_bottom, LIST_WIDTH, body_top - body_bottom))
        scroll.setHasVerticalScroller_(True)
        scroll.setBorderType_(1)
        scroll.setAutoresizingMask_(NSViewWidthSizable | NSViewHeightSizable)
        self.outline = NSOutlineView.alloc().initWithFrame_(scroll.bounds())
        for ident, title, width in (("item", "Broker", LIST_WIDTH - 150), ("status", "State", 120)):
            column = NSTableColumn.alloc().initWithIdentifier_(ident)
            column.headerCell().setStringValue_(title)
            column.setWidth_(width)
            self.outline.addTableColumn_(column)
            if ident == "item":
                self.outline.setOutlineTableColumn_(column)
        self.outline.setAllowsMultipleSelection_(True)
        self.outline.setUsesAlternatingRowBackgroundColors_(True)
        self.outline.setRowHeight_(22)
        self.outline.setDataSource_(self.controller)
        self.outline.setDelegate_(self.controller)
        scroll.setDocumentView_(self.outline)
        content.addSubview_(scroll)

        pane_x = PAD + LIST_WIDTH + 12
        pane_w = WIDTH - pane_x - PAD
        self.pane = _tile(NSMakeRect(pane_x, body_bottom, pane_w, body_top - body_bottom))
        self.pane.setAutoresizingMask_(NSViewMinXMargin | NSViewHeightSizable)
        content.addSubview_(self.pane)
        inner = self.pane.contentView()
        self.shot = NSImageView.alloc().initWithFrame_(NSMakeRect(8, body_top - body_bottom - 228, pane_w - 16, 220))
        self.shot.setImageScaling_(3)                    # proportionally up or down
        self.shot.setAutoresizingMask_(NSViewMinYMargin | NSViewWidthSizable)
        self.shot.setHidden_(True)
        inner.addSubview_(self.shot)
        self.text_scroll = NSScrollView.alloc().initWithFrame_(NSMakeRect(4, 4, pane_w - 8, body_top - body_bottom - 8))
        self.text_scroll.setHasVerticalScroller_(True)
        self.text_scroll.setDrawsBackground_(False)
        self.text_scroll.setAutoresizingMask_(NSViewWidthSizable | NSViewHeightSizable)
        self.text = NSTextView.alloc().initWithFrame_(self.text_scroll.bounds())
        self.text.setEditable_(False)
        self.text.setSelectable_(True)
        self.text.setRichText_(False)
        self.text.setDrawsBackground_(False)
        self.text.setFont_(NSFont.systemFontOfSize_(12))
        self.text.setTextContainerInset_((6, 6))
        self.text.setAutoresizingMask_(NSViewWidthSizable)
        self.text_scroll.setDocumentView_(self.text)
        inner.addSubview_(self.text_scroll)

        self.subtitle.setStringValue_("loading…")
        self.details = None
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
        from brokers import report
        self._in_background(lambda: report.board(full=True), self._update)

    # ---- the outline's nodes: "g|group", "b|group|id|n", "d|group|id|n|line"

    def node(self, key):
        if key not in self.nodes:
            self.nodes[key] = NSString.stringWithString_(key)
        return self.nodes[key]

    def kids(self, node):
        if self.data is None:
            return []
        if node is None:
            return [self.node(f"g|{g}") for g, _t in GROUPS]
        key = str(node)
        if key.startswith("g|"):
            group = key[2:]
            return [self.node(k) for k in self.group_keys.get(group, [])]
        if key.startswith("b|"):
            group, entry = self.items[key]
            return [self.node(f"d|{key[2:]}|{i}") for i in range(len(_children(group, entry)))]
        return []

    def cell(self, node, column):
        key = str(node)
        if key.startswith("g|"):
            group = key[2:]
            count = len(self.group_keys.get(group, []))
            if column == "item":
                return f"{dict(GROUPS)[group]}  ({count})"
            return ""
        if key.startswith("b|"):
            group, entry = self.items[key]
            return _line(group, entry) if column == "item" else _status(group, entry)
        if column != "item":
            return ""
        parent = "b|" + key[2:].rsplit("|", 1)[0]
        index = int(key.rsplit("|", 1)[1])
        group, entry = self.items.get(parent, (None, None))
        lines = _children(group, entry) if entry else []
        return lines[index] if index < len(lines) else ""

    def _update(self, data):
        self.busy = False
        if "error" in data:
            self.set_message(f"could not read the board: {data['error']}")
            return
        picked = {k for k in self._picked_keys()}
        expanded = {str(n) for n in self.nodes.values() if self.outline.isItemExpanded_(n)}
        self.data = data
        self.group_keys, self.items = {}, {}
        for group, _title in GROUPS:
            keys = []
            for n, entry in enumerate(data["groups"].get(group, [])):
                ident = entry.get("task") or entry.get("request") or entry["id"]
                key = f"b|{group}|{ident}"
                self.items[key] = (group, entry)
                keys.append(key)
            self.group_keys[group] = keys
        self.switch.setState_(1 if data["mode"] == "standing" else 0)
        t, c = data["totals"], data["changes"]
        delta = lambda k: f" ({c[k]:+d})" if c.get(k) else ""
        if not data["ready"]:
            self.subtitle.setStringValue_("Add your name and current address in Your details to start. Nothing is "
                                          "searched or sent before that.")
        else:
            self.subtitle.setStringValue_(
                f"{t['listed']} with listings{delta('listed')} · {t['removed']} removed{delta('removed')} · "
                f"{t['in_progress']} in progress{delta('in_progress')} · {t['refused']} refused or silent"
                f"{delta('refused')} · {t['manual']} manual{delta('manual')}"
                + (f" · {data['unsearched']} sites still to search" if data.get("unsearched") else "")
                + (f" · last report {data['last_report']}" if data.get("last_report") else ""))
        self.outline.reloadData()
        for group, _title in GROUPS:
            node = self.node(f"g|{group}")
            if (self.first and group in OPEN_AT_START) or str(node) in expanded:
                self.outline.expandItem_(node)
        for key in expanded:
            if key.startswith("b|") and key in self.items:
                self.outline.expandItem_(self.node(key))
        self.first = False
        keep = NSIndexSet.indexSet().mutableCopy()
        for key in picked:
            if key in self.items:
                row = self.outline.rowForItem_(self.node(key))
                if row >= 0:
                    keep.addIndex_(row)
        self.outline.selectRowIndexes_byExtendingSelection_(keep, False)
        self._fill_batch()
        self.selection_changed()

    def _fill_batch(self):
        self.batch_menu.removeAllItems()
        approval = [e for _g, e in (self.items[k] for k in self.group_keys.get("approval", []))]
        options = [("Approve all…", None)]
        ready = [e for e in approval if not (e.get("proposal") or {}).get("missing")]
        emails = [e["id"] for e in ready if (e.get("proposal") or {}).get("method") == "email" and not e.get("pending")]
        people = [e["id"] for e in ready if e["category"] in ("people-search", "background-check")]
        if emails:
            options.append((f"all {len(emails)} email requests", emails))
        if people:
            options.append((f"all {len(people)} people-search sites", people))
        for tier in sorted({e["tier"] for e in ready}):
            ids = [e["id"] for e in ready if e["tier"] == tier]
            options.append((f"all of {TIERS.get(tier, f'tier {tier}')} ({len(ids)})", ids))
        if len(ready) > 1:
            options.append((f"everything that needs approval ({len(ready)})", [e["id"] for e in ready]))
        self.batch_options = options
        for title, _ids in options:
            self.batch_menu.addItemWithTitle_(title)
        self.batch_menu.setEnabled_(len(options) > 1)

    def _picked_keys(self):
        indexes = self.outline.selectedRowIndexes()
        found, i = [], indexes.firstIndex()
        while i is not None and i < 1 << 62:
            node = self.outline.itemAtRow_(i)
            if node is not None and str(node).startswith("b|"):
                found.append(str(node))
            i = indexes.indexGreaterThanIndex_(i)
        return found

    def _picked(self):
        return [self.items[k] for k in self._picked_keys() if k in self.items]

    def selection_changed(self):
        picked = self._picked()
        groups = {g for g, _e in picked}
        group = groups.pop() if len(groups) == 1 else None
        offers, default = OFFERS.get(group, ((), None))
        single = picked[0][1] if len(picked) == 1 else None
        x = self.window.contentView().frame().size.width - PAD
        for key, button in self.buttons.items():
            show = key in offers
            if show and group == "manual":
                kind = single.get("kind") if single else None
                show = {"its_me": kind == "possible_match", "not_me": kind == "possible_match",
                        "listed": kind == "search", "not_listed": kind == "search",
                        "open": bool(single and single.get("link")), "email": bool(single and single.get("email")),
                        "handoff": kind == "captcha" and bool(single and single.get("link")),
                        "done": kind not in ("possible_match",
                                                                                          "search")}.get(key, True)
            if key == "outcome" and show:
                # Skipped only closes tasks; a possible match or a search gets nothing else from this menu.
                answers = group != "manual" or (single or {}).get("kind") not in ("possible_match", "search")
                for i, (outcome, _title) in enumerate(OUTCOME_CHOICES, 1):
                    button.itemAtIndex_(i).setHidden_(not answers if outcome not in TASK_ONLY_OUTCOMES
                                                      else group != "manual")
            button.setHidden_(not show)
            if show:
                x -= button.frame().size.width
                button.setFrameOrigin_((x, self.row_y))
                x -= 6
            enabled = bool(picked) and (len(picked) == 1 or key in ("approve", "reject", "override", "done", "outcome"))
            button.setEnabled_(enabled)
            if key != "outcome":
                button.setKeyEquivalent_("\r" if key == default else "")
        self.batch_menu.setHidden_(group not in (None, "approval") or not self.data or not self.data["ready"])
        self.shot.setHidden_(True)
        if single:
            key = self._picked_keys()[0]
            history = self.histories.get(key)
            self._show_text(_section(group, single, history))
            if history is None and single.get("id") and group in ("approval", "waiting", "refused", "removed", "manual", "auto"):
                self._load_history(key, single["id"])
            self._load_shot(group, single)
        elif picked:
            names = ", ".join(e["name"] for _g, e in picked[:15])
            self._show_text(f"{len(picked)} picked: {names}" + ("…" if len(picked) > 15 else ""))
        elif self.data and not self.data["ready"]:
            self._show_text("Nothing happens until the agent knows who to look for.\n\nPress Your details and add at "
                            "least your name and current address (more helps it tell your listings from a namesake's). "
                            "They're kept in an encrypted database on this Mac whose key is in your Keychain, and only "
                            "the details you approve for each broker are ever sent.")
        else:
            self._show_text("Pick a broker to see its whole section: what it does, what it has on you, exactly which "
                            "details would be sent, and its history. Click the triangles to open groups and brokers; "
                            "Cmd-click or Shift-click for several.")

    def _show_text(self, text):
        self.text.setString_(text)
        self.text.scrollRangeToVisible_((0, 0))

    def _load_history(self, key, broker_id):
        def job():
            from brokers import report, vault
            with vault.db() as conn:
                return {"key": key, "history": report._history(conn, broker_id, 25)}

        def done(result):
            if "error" not in result:
                self.histories[result["key"]] = result["history"]
                if self._picked_keys() == [result["key"]]:
                    self.selection_changed()

        self._in_background(job, done)

    def _load_shot(self, group, entry):
        listings = ((entry.get("shown") or {}).get("listings") or []) if group != "manual" else \
            ([{"id": None, "url": entry["listing"]["url"]}] if entry.get("listing") else [])
        if not listings:
            return
        broker_id, url = entry["id"], listings[0]["url"]

        def job():
            from brokers import vault
            with vault.db() as conn:
                row = conn.execute("SELECT screenshot FROM listings WHERE broker_id = ? AND url = ?",
                                   (broker_id, url)).fetchone()
            return {"png": bytes(row["screenshot"]) if row and row["screenshot"] else None, "url": url}

        def done(result):
            if result.get("png"):
                image = NSImage.alloc().initWithData_(NSData.dataWithBytes_length_(result["png"], len(result["png"])))
                self.shot.setImage_(image)
                self.shot.setHidden_(False)
                frame = self.text_scroll.frame()
                self.text_scroll.setFrame_(NSMakeRect(frame.origin.x, 4, frame.size.width,
                                                      self.pane.contentView().frame().size.height - 240))

        self.text_scroll.setFrame_(NSMakeRect(4, 4, self.pane.contentView().frame().size.width - 8,
                                              self.pane.contentView().frame().size.height - 8))
        self._in_background(job, done)

    def decide(self, key):
        picked = self._picked()
        if not picked:
            self.set_message("pick one or more first")
            return
        from brokers import ops
        group, first = picked[0]
        if key == "handoff":
            try:
                ops.handoff(first["task"])
                self.set_message("a browser window is opening: pass the check there, and the agent does the rest "
                                 "(you can follow it with \"brokers debug\")")
            except ops.Problem as problem:
                self.set_message(str(problem))
            return
        if key == "email":
            NSWorkspace.sharedWorkspace().openURL_(NSURL.URLWithString_(first.get("email")))
            self.set_message("opened their email in Gmail")
            return
        if key == "open":
            NSWorkspace.sharedWorkspace().openURL_(NSURL.URLWithString_(first.get("link") or first.get("url")))
            self.set_message("opened in your browser: press Done when you've finished there")
            return
        if key == "copy":
            board = NSPasteboard.generalPasteboard()
            board.clearContents()
            board.setString_forType_(first.get("complaint", ""), NSPasteboardTypeString)
            self.set_message("the complaint draft is on the clipboard: paste it into the FTC's or your attorney "
                             "general's form")
            return
        if key == "regulator":
            from brokers import plan
            url = plan.config()["jurisdiction"]["regulators"]["FTC"]
            NSWorkspace.sharedWorkspace().openURL_(NSURL.URLWithString_(url))
            return
        if key == "file":
            import os
            from brokers import ROOT
            NSWorkspace.sharedWorkspace().openFile_(os.path.join(os.path.dirname(ROOT), first["file"]))
            return
        ids = [e["id"] for _g, e in picked]
        if key == "approve":
            job = lambda: ops.decide(ids, [])
        elif key == "reject":
            job = lambda: ops.decide([], ids)
        elif key == "override":
            job = lambda: {"done": [str(ops.override(i)["cancelled"]) + f" cancelled for {i}" for i in ids]}
        else:
            outcome = {"done": "done", "listed": "listed", "not_listed": "not_listed", "its_me": "yes",
                       "not_me": "no"}[key]
            refs = [e.get("task") or e["id"] for _g, e in picked]
            job = lambda: {"done": [str(ops.manual_done(r, outcome).get("outcome", "done")) for r in refs]}
        self.set_message({"approve": "approving", "reject": "rejecting", "override": "overriding"}.get(key, "noting")
                         + f" {len(picked)}…")
        self._in_background(job, self._decided)

    def mark(self, index):
        """The Mark as… menu: what happened, for picked tasks (closing them) or brokers."""
        picked = self._picked()
        if index <= 0 or index > len(OUTCOME_CHOICES) or not picked:
            return
        outcome = OUTCOME_CHOICES[index - 1][0]
        if outcome in TASK_ONLY_OUTCOMES:
            picked = [(g, e) for g, e in picked if e.get("task")]
            if not picked:
                return
        from brokers import ops
        jobs = [(lambda e=e: ops.manual_done(e["task"], outcome)) if e.get("task") else (lambda e=e: ops.mark(e["id"], outcome))
                for _g, e in picked]
        self.set_message(f"marking {len(picked)} as {OUTCOME_CHOICES[index - 1][1].lower()}…")
        self._in_background(lambda: {"done": [f"{r['broker']}: {r['outcome']}" for r in (job() for job in jobs)]},
                            self._decided)

    def batch(self, index):
        if index <= 0 or index >= len(self.batch_options):
            return
        title, ids = self.batch_options[index]
        from brokers import ops
        self.set_message(f"approving {title}…")
        self._in_background(lambda: ops.decide(ids, []), self._decided)

    def _decided(self, result):
        if "error" in result:
            self.set_message(f"that broke: {result['error']}")
        else:
            done, errors = result.get("done", []), result.get("errors", [])
            text = " · ".join(done[:3]) + (f" · and {len(done) - 3} more" if len(done) > 3 else "")
            if errors:
                text = (text + " · " if text else "") + "not done: " + " · ".join(errors[:3])
            self.set_message(text or "done")
        self.histories.clear()
        self.refresh()

    def toggle_mode(self):
        from brokers import ops
        mode = "standing" if self.switch.state() else "per_request"
        ops.set_mode(mode)
        self.set_message("standing approvals: follow-ups, re-submissions and confirmations happen without asking"
                         if mode == "standing" else "per request: every action waits for you under Needs approval")
        self.refresh()

    def run_now(self):
        from brokers import ops
        try:
            ops.run_pass()
            self.set_message("one pass started: this updates as it goes")
        except ops.Problem as problem:
            self.set_message(str(problem))

    def open_details(self):
        if self.details is None:
            self.details = Details(self)
        self.details.show()


# ------------------------------------------------------------ your details

FIELDS = [  # key, label, lines, hint
    ("first_name", "First name", 1, ""), ("middle_name", "Middle name", 1, ""), ("last_name", "Last name", 1, ""),
    ("name_variants", "Other names", 1, "maiden name, nicknames: comma between them"),
    ("dob", "Date of birth", 1, "YYYY-MM-DD: helps tell your listings from a namesake's"),
    ("street", "Street", 1, "your current address"), ("city", "City", 1, ""), ("state", "State", 1, "two letters, e.g. NY"),
    ("zip", "ZIP code", 1, ""),
    ("past", "Past addresses", 3, "one a line: 12 Elm St, Anytown, NY 12345 (or just Anytown, NY)"),
    ("phones", "Phones", 2, "one a line"),
    ("emails", "Other emails", 2, "one a line (your Gmail address is used for requests already)"),
    ("relatives", "Relatives", 3, "one a line: only to recognise your listings; never sent to anyone"),
]


def _parse_address(line):
    parts = [p.strip() for p in line.split(",") if p.strip()]
    if not parts:
        return None
    last = parts[-1].split()
    state, zip_code = (last[0], last[1] if len(last) > 1 else "") if last and len(last[0]) == 2 else ("", "")
    if len(parts) >= 3:
        return {"street": ", ".join(parts[:-2]), "city": parts[-2], "state": state.upper(), "zip": zip_code}
    if len(parts) == 2:
        return {"city": parts[0], "state": state.upper(), "zip": zip_code}
    return {"city": parts[0]}


class Details:
    """Your details, kept in the vault. The only way they get in."""

    def __init__(self, owner):
        self.owner = owner
        self.controller = _BrokerController.alloc().initWithOwner_(self)
        width, height = 600, 700
        style = NSWindowStyleMaskTitled | NSWindowStyleMaskClosable
        self.window = NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
            NSMakeRect(0, 0, width, height), style, NSBackingStoreBuffered, False)
        self.window.setTitle_(f"{APP_NAME} · your details")
        self.window.setReleasedWhenClosed_(False)
        self.window.center()
        content = self.window.contentView()
        caption = NSColor.secondaryLabelColor()
        y = height - PAD - 36
        note = _label(NSMakeRect(PAD, y, width - 2 * PAD, 34), 11, color=caption, wrap=True)
        note.setStringValue_("Kept encrypted on this Mac (brokers.db; the key is in your Keychain). Each broker gets "
                             "only the details you approve for it, and the ledger records every one sent.")
        content.addSubview_(note)
        y -= 8
        self.fields = {}
        for key, label, lines, hint in FIELDS:
            box_height = 24 if lines == 1 else 18 * lines + 8
            y -= box_height + 8
            name = _label(NSMakeRect(PAD, y + box_height - 20, 130, 18), 12, 0.3)
            name.setStringValue_(label)
            name.setAlignment_(2)
            content.addSubview_(name)
            if lines == 1:
                box = NSTextField.alloc().initWithFrame_(NSMakeRect(PAD + 140, y, width - 2 * PAD - 140, box_height))
                box.setPlaceholderString_(hint)
                content.addSubview_(box)
            else:
                scroll = NSScrollView.alloc().initWithFrame_(NSMakeRect(PAD + 140, y, width - 2 * PAD - 140, box_height))
                scroll.setBorderType_(2)
                scroll.setHasVerticalScroller_(True)
                box = NSTextView.alloc().initWithFrame_(scroll.bounds())
                box.setRichText_(False)
                box.setFont_(NSFont.systemFontOfSize_(12))
                box.setToolTip_(hint)
                scroll.setDocumentView_(box)
                content.addSubview_(scroll)
                tip = _label(NSMakeRect(PAD, y, 130, 30), 10, color=caption, wrap=True)
                tip.setStringValue_(hint.split(":")[0])
                tip.setAlignment_(2)
                content.addSubview_(tip)
            self.fields[key] = box
        save = _button(NSMakeRect(width - PAD - 100, PAD, 100, 32), "Save", self.controller, "save:")
        save.setKeyEquivalent_("\r")
        content.addSubview_(save)
        cancel = _button(NSMakeRect(width - PAD - 206, PAD, 100, 32), "Cancel", self.controller, "cancel:")
        content.addSubview_(cancel)
        self.status = _label(NSMakeRect(PAD, PAD + 6, width - 2 * PAD - 220, 18), 12, color=caption)
        content.addSubview_(self.status)

    def _set(self, key, value):
        box = self.fields[key]
        if isinstance(box, NSTextField):
            box.setStringValue_(value or "")
        else:
            box.setString_(value or "")

    def _get(self, key):
        box = self.fields[key]
        return str(box.stringValue() if isinstance(box, NSTextField) else box.string()).strip()

    def show(self):
        from brokers import vault
        try:
            found = vault.identity()
        except Exception as error:
            found = {}
            self.status.setStringValue_(f"could not open the vault: {error}")
        here = vault.current_address(found) or {}
        for key in ("first_name", "middle_name", "last_name", "dob"):
            self._set(key, found.get(key, ""))
        self._set("name_variants", ", ".join(found.get("name_variants") or []))
        for key in ("street", "city", "state", "zip"):
            self._set(key, here.get(key, ""))
        past = [a for a in found.get("addresses") or [] if a is not here]
        self._set("past", "\n".join(", ".join(x for x in (a.get("street"), a.get("city"),
                                                            f"{a.get('state', '')} {a.get('zip', '')}".strip()) if x)
                                    for a in past))
        for key in ("phones", "emails", "relatives"):
            self._set(key, "\n".join(found.get(key) or []))
        self.window.makeKeyAndOrderFront_(None)
        NSApp.activateIgnoringOtherApps_(True)

    def save(self):
        import re
        from brokers import vault
        dob = self._get("dob")
        if dob and not re.match(r"^\d{4}-\d{2}-\d{2}$", dob):
            self.status.setStringValue_("date of birth as YYYY-MM-DD, please")
            return
        state = self._get("state").upper()
        if state and len(state) != 2:
            self.status.setStringValue_("state as two letters (NY, say)")
            return
        current = {"street": self._get("street"), "city": self._get("city"), "state": state, "zip": self._get("zip"),
                   "current": True}
        addresses = ([current] if current["city"] else []) + [a for a in (_parse_address(line) for line in
                                                                          self._get("past").splitlines()) if a]
        lines = lambda key: [line.strip() for line in self._get(key).splitlines() if line.strip()]
        values = {"first_name": self._get("first_name"), "middle_name": self._get("middle_name"),
                  "last_name": self._get("last_name"), "dob": dob,
                  "name_variants": [v.strip() for v in self._get("name_variants").split(",") if v.strip()],
                  "addresses": addresses, "phones": lines("phones"), "emails": lines("emails"),
                  "relatives": lines("relatives")}
        try:
            vault.save_identity(values)
        except Exception as error:
            self.status.setStringValue_(f"not saved: {error}")
            return
        self.window.orderOut_(None)
        self.owner.set_message("your details are saved (encrypted)" + ("" if vault.has_identity()
                                                                       else ": add your name and current city to start"))
        self.owner.refresh()


_dashboard = {}


def open_dashboard(message=""):
    """Show the dashboard (building it the first time), from any thread."""

    def on_main():
        if "window" not in _dashboard:
            _dashboard["window"] = Dashboard()
        _dashboard["window"].show(message)

    AppHelper.callAfter(on_main)


def listen():
    """Open the dashboard whenever brokers.OPEN_DASHBOARD is posted (a banner click). Call once, on the main thread."""
    listener = _BrokerController.alloc().initWithOwner_(None)
    _dashboard["listener"] = listener
    NSDistributedNotificationCenter.defaultCenter().addObserver_selector_name_object_(
        listener, "open:", OPEN_DASHBOARD, None)


def post_open():
    """Ask the running assistant to open the dashboard (from another process)."""
    NSDistributedNotificationCenter.defaultCenter().postNotificationName_object_userInfo_deliverImmediately_(
        OPEN_DASHBOARD, None, None, True)
