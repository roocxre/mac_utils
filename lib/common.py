"""Helpers shared by the launchd agents and the assistant: logs, the state folder, dialogs and banners.

Dialogs default to APP_NAME as their title; show_alert, notify, confirm and banner take theirs from
the caller."""
import logging
import os
import subprocess
import sys
from logging.handlers import RotatingFileHandler

from lib.identity import APP_NAME, LOG_DIR, STATE_DIR

DRY_RUN = "--dry-run" in sys.argv


def get_logger(name):
    """Logger writing to ~/Library/Logs/MacUtils/<name>.log (and stdout on a dry run)."""
    os.makedirs(LOG_DIR, exist_ok=True)
    logger = logging.getLogger(name)
    if not logger.handlers:
        handler = RotatingFileHandler(
            os.path.join(LOG_DIR, name + ".log"), maxBytes=1_000_000, backupCount=1
        )
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        logger.addHandler(handler)
        if DRY_RUN:
            logger.addHandler(logging.StreamHandler(sys.stdout))
        logger.setLevel(logging.INFO)
    return logger


def state_path(filename):
    os.makedirs(STATE_DIR, exist_ok=True)
    return os.path.join(STATE_DIR, filename)


def run(cmd, timeout=30, env=None):
    """Run a command without raising. Returns CompletedProcess, or None if it could not run."""
    try:
        return subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout, env=env, check=False
        )
    except (OSError, subprocess.SubprocessError):
        return None


def developer_tools_installed():
    """Whether Xcode or its Command Line Tools are installed. Without them, /usr/bin/swiftc, pip3, git
    and the like are stand-ins that only offer to install them, in a dialog."""
    result = run(["xcode-select", "-p"], timeout=10)
    return bool(result and result.returncode == 0 and os.path.isdir(result.stdout.strip()))


def trash(path):
    """Move a file or folder to the Trash. Returns True on success."""
    from Foundation import NSURL, NSFileManager

    ok, _, _ = NSFileManager.defaultManager().trashItemAtURL_resultingItemURL_error_(
        NSURL.fileURLWithPath_(path), None, None
    )
    return bool(ok)


_ALERT_SCRIPT = """on run argv
    display alert (item 1 of argv) message (item 2 of argv) as informational buttons {{"OK"}} default button "OK"{giveup}
end run"""


def show_alert(title, message, give_up_after=None):
    """Show a modal alert and wait for it to be dismissed (or give up after N seconds).

    The text is passed as arguments, never spliced into the script, so it can contain anything.
    Returns the CompletedProcess, or None if osascript could not run.
    """
    giveup = f" giving up after {int(give_up_after)}" if give_up_after else ""
    return run(["osascript", "-e", _ALERT_SCRIPT.format(giveup=giveup), title, message], timeout=None)


def notify(title, message):
    """Show a banner notification (non-blocking). Text is passed as arguments, never spliced."""
    script = ('on run argv\n    display notification (item 2 of argv) with title (item 1 of argv)\nend run')
    return run(["osascript", "-e", script, title, message], timeout=10)


def banner(title, message, seconds=None, wait=False, post=None):
    """Show a banner in the corner of the screen (see lib/banner.py). It stays until clicked unless
    `seconds` is given. post: a distributed notification a click sends (to open a window in the
    assistant). Returns the running process, or its CompletedProcess if wait is set; None if it
    could not start. Exit status 0 means it was shown and is gone.
    """
    args = [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "banner.py"),
            title, message, str(seconds) if seconds else ""] + (["--post", post] if post else [])
    if wait:
        return run(args, timeout=None)
    try:
        return subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError:
        return None


def ask_text(prompt, title=APP_NAME, default=""):
    """Prompt for a line of text. Returns the text, or None if cancelled."""
    script = ('on run argv\n    text returned of (display dialog (item 1 of argv) default answer (item 3 of argv) '
              'with title (item 2 of argv) buttons {"Cancel", "Go"} default button "Go")\nend run')
    result = run(["osascript", "-e", script, prompt, title, default], timeout=None)
    if result is None or result.returncode != 0:
        return None
    return result.stdout.rstrip("\n")


def confirm(title, message, ok_label="OK"):
    """Ask a yes/no question with Cancel as the default button. Returns True only on a yes."""
    script = ('on run argv\n    display dialog (item 2 of argv) with title (item 1 of argv) '
              'buttons {"Cancel", item 3 of argv} default button "Cancel" cancel button "Cancel"\nend run')
    result = run(["osascript", "-e", script, title, message, ok_label], timeout=None)
    return result is not None and result.returncode == 0


def choose_from(items, prompt, title=APP_NAME, multiple=False):
    """Pick one of several strings. Returns the choice, or None if cancelled.

    multiple: allow several (Cmd- or Shift-click); the result is then a list.
    """
    script = ('on run argv\n    set picked to choose from list (paragraphs of (item 1 of argv)) '
              'with prompt (item 2 of argv) with title (item 3 of argv)'
              + (' with multiple selections allowed' if multiple else '') + '\n'
              '    if picked is false then error number -128\n'
              '    set AppleScript\'s text item delimiters to linefeed\n'
              '    return picked as text\nend run')
    result = run(["osascript", "-e", script, "\n".join(items), prompt, title], timeout=None)
    if result is None or result.returncode != 0:
        return None
    picked = result.stdout.rstrip("\n")
    return [line for line in picked.split("\n") if line] if multiple else picked


def unique_path(path):
    """Return path, or 'name 2.ext', 'name 3.ext', ... if it is already taken."""
    if not os.path.lexists(path):
        return path
    base, ext = os.path.splitext(path)
    n = 2
    while os.path.lexists(f"{base} {n}{ext}"):
        n += 1
    return f"{base} {n}{ext}"


def run_main(name, main):
    """Run an agent's main(), logging any crash with a traceback before exiting non-zero."""
    log = get_logger(name)
    try:
        main(log)
    except Exception:
        log.exception("crashed")
        sys.exit(1)
