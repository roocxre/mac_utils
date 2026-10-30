"""The local assistant: press Ctrl+Option+Space, type what you want, it does it.

    open safari · open ~/Downloads · ~/Doc (Go to Folder, completes a prefix)
    find thesis · ls ~/Archive · read notes.txt · status · restart clipboard
    install firefox (looked up in Homebrew; runs in Terminal once you say yes)
    free up memory (quits spotify, messages, discord, system utils; lists the next biggest)
    battery helper (quits apps unused for a day, opens the battery dashboard) · power saving on|off

Plain code decides what a request means and does it. A request that isn't worded as a command
is restated as one by a small encoder (atlas/intent.py), then goes through the same code as if you
had typed it. What still isn't a command is reported, with the closest commands from the
catalogue (atlas/catalogue.py). No language model is involved.
"""
import threading
import time

from atlas import assistant_core as core
from atlas import battery_helper
from atlas import dnd
from atlas import intent
from atlas import memory_helper
from lib import components
from lib.common import ask_text, banner, choose_from, confirm, run_main, show_alert
from lib.hotkey import CONTROL, OPTION, register_hotkey

KEY_CODE = 49                      # Space
MODIFIERS = CONTROL | OPTION
TITLE = "atlas"
LONG_MESSAGE_CHARS = 100
BANNER_SECONDS = 3                 # how long a memory report stays in the corner


def toast(title, text, seconds=4):
    """Show a short message that closes itself, without waiting for it.

    A dialog rather than a banner notification: macOS silently drops banners from a
    background script unless notification permission was granted, so errors went unseen.
    """
    threading.Thread(target=show_alert, args=(title, text), kwargs={"give_up_after": seconds},
                     daemon=True).start()


def present(outcome, note, ui, seconds=4, as_banner=False):
    """Show an outcome, following choices (folders open further listings) until done.

    note: put before the text (what loose wording was taken to mean).
    as_banner: show the final text as a banner in the corner instead of a dialog.
    """
    while True:
        if outcome.question:
            if not ui.confirm(TITLE, outcome.question, outcome.yes_label):
                return
            outcome = outcome.on_yes()
            note = ""
            continue
        if outcome.choices:
            picked = ui.choose(outcome.choices, outcome.message or "pick one", TITLE,
                               **({"multiple": True} if outcome.multiple else {}))
            if picked is None:
                return
            outcome = outcome.on_choice(picked)
            note = ""
            continue
        text = outcome.message
        if as_banner and (text or note):
            ui.banner(TITLE, f"{note}\n{text}" if note and text else (text or note), BANNER_SECONDS)
        elif text and ("\n" in text or len(text) > LONG_MESSAGE_CHARS):
            ui.alert(TITLE, f"{note}\n\n{text}" if note else text)
        elif text or note:
            ui.notify(TITLE, f"{note} — {text}" if note and text else (text or note), seconds)
        return


def process(text, ui, log):
    """Handle one typed request. Returns a short description of what happened (for the log)."""
    plan = core.interpret(text)
    log.info("request %r -> %s", text, plan.kind)
    note = ""
    if plan.kind == "miss":
        # Not worded as a command: see whether it is one in other words. If not, the miss is shown
        # with the closest commands.
        command = intent.translate(text)
        restated = core.interpret(command) if command else None
        if restated is not None and restated.kind not in ("miss", "help", "yours"):
            log.info("understood %r as %r -> %s", text, command, restated.kind)
            plan, note = restated, f"[{command}]"     # what the loose wording was taken to mean

    # Errors and misses stay up a little longer.
    present(plan.execute(), note, ui, seconds=7 if plan.kind in ("error", "denied", "miss") else 4,
            as_banner=plan.kind in ("memory", "battery", "power", "dashboard"))
    return "not a command" if plan.kind == "miss" else "done"


class SystemUI:
    ask = staticmethod(lambda prompt: ask_text(prompt, title=TITLE))
    choose = staticmethod(choose_from)
    confirm = staticmethod(confirm)
    alert = staticmethod(show_alert)
    notify = staticmethod(toast)
    banner = staticmethod(banner)


def main(log):
    from AppKit import NSApplication, NSApplicationActivationPolicyAccessory

    busy = threading.Lock()

    def on_hotkey():
        def work():
            if not busy.acquire(blocking=False):
                return
            try:
                text = SystemUI.ask("what do you want?")
                if text and text.strip():
                    process(text, SystemUI, log)
            except Exception:
                log.exception("request failed")
                SystemUI.notify(TITLE, "that broke. details are in the log.", 7)
            finally:
                busy.release()
        threading.Thread(target=work, daemon=True).start()

    app = NSApplication.sharedApplication()
    app.setActivationPolicy_(NSApplicationActivationPolicyAccessory)
    status = register_hotkey(KEY_CODE, MODIFIERS, on_hotkey, hotkey_id=2)
    if status != 0:
        raise RuntimeError(f"could not register Ctrl+Option+Space (status {status}); is it already taken?")
    watcher = memory_helper.PressureWatcher(
        memory_helper.memory_pressure, lambda text: None if dnd.evaluate()[0] else SystemUI.banner(TITLE, text, BANNER_SECONDS), log)

    usage = battery_helper.UsageTracker()     # which app is in front, for the battery helper's idle quit

    def watch_memory():
        while True:
            try:
                watcher.tick()
            except Exception:
                log.exception("memory watcher")
            try:
                usage.tick()
            except Exception:
                log.exception("usage tracker")
            time.sleep(15)

    threading.Thread(target=watch_memory, daemon=True).start()
    # Only for the parts that are installed (lib/components.py): nothing of the others is imported.
    if components.installed("unsub"):
        from atlas import inbox_dashboard
        inbox_dashboard.listen()              # a click on the inbox agent's banners opens its dashboard
    if components.installed("brokers"):
        from atlas import broker_dashboard
        broker_dashboard.listen()             # and the broker agent's weekly banner opens its own
        from atlas import broker_watch
        broker_watch.listen()                 # `python -m brokers debug` opens the live watch window
    log.info("started; press Ctrl+Option+Space")
    app.run()


if __name__ == "__main__":
    run_main("assistant", main)
