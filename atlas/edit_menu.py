"""The Edit menu the dashboards share, so Cmd+C/V/X/A/W work in their windows (the app has no menu bar)."""
from AppKit import NSApp, NSMenu, NSMenuItem


def install_edit_menu():
    """Give the app an Edit menu so Cmd+C/V/X/A/W work in the window (it has no menu bar)."""
    if NSApp.mainMenu() is not None:
        return
    main = NSMenu.alloc().init()
    for title, items in (("App", (("Close Window", "performClose:", "w"),)),
                         ("Edit", (("Cut", "cut:", "x"), ("Copy", "copy:", "c"), ("Paste", "paste:", "v"),
                                   ("Select All", "selectAll:", "a"), ("Undo", "undo:", "z")))):
        holder = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(title, None, "")
        menu = NSMenu.alloc().initWithTitle_(title)
        for name, action, key in items:
            menu.addItem_(NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(name, action, key))
        holder.setSubmenu_(menu)
        main.addItem_(holder)
    NSApp.setMainMenu_(main)
