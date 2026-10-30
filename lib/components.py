"""Which components are installed on this Mac, as recorded by manage.py in the state folder.

The installer can add components one at a time, so code that reaches into another component (the
assistant opening the inbox dashboard, the extension telling the broker agent about a broker) asks
here first and says the component isn't installed instead of failing on an import.

Component ids: assistant, audioguard, appblocker, clipboard, sitemonitor, autounpack, screenshotcopy,
screentext, cmdnotify, messagepopup, stalearchiver, devclean, logtrimmer, packagemanagergc, pockkeeper,
mail, unsub, brokers, dataflow.
"""
import json
import os

from lib.identity import STATE_DIR

RECORD = os.path.join(STATE_DIR, "components.json")


def _recorded():
    """The recorded ids, or None when nothing was recorded (a checkout set up by hand)."""
    try:
        with open(RECORD, encoding="utf-8") as f:
            return set(json.load(f).get("installed", {}))
    except (OSError, ValueError, AttributeError):
        return None


def installed(name):
    """True if the component is installed. Without a record (a checkout run by hand) everything counts."""
    recorded = _recorded()
    return recorded is None or name in recorded


def not_installed_message(name, title=None):
    return f"{title or name} isn't installed (add it with the installer, or ./install.sh --components {name})"
