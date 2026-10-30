"""The names this project uses on the Mac, in one place.

Every name another copy of these agents could also use (launchd labels, Keychain items, distributed
notifications, the browser helper, folders) is built here, so two installs on one Mac never collide.
"""
import os

APP_ID = "io.github.roocxre.macutils"   # reverse-DNS prefix for labels, Keychain services and notifications
APP_NAME = "Mac Utils"                    # shown in dialogs, banners and window titles
FOLDER = "MacUtils"                       # folder name under ~/Library

SUPPORT_DIR = os.path.expanduser(f"~/Library/Application Support/{FOLDER}")
STATE_DIR = os.path.join(SUPPORT_DIR, "state")     # databases, caches, queues; kept across reinstalls
INSTALL_DIR = os.path.join(SUPPORT_DIR, "app")     # the code and its virtualenv when installed (not a checkout)
LOG_DIR = os.path.expanduser(f"~/Library/Logs/{FOLDER}")


def label(name):
    """The launchd label of an agent, e.g. io.github.roocxre.macutils.clipboard."""
    return f"{APP_ID}.{name}"


def keychain_service(name):
    """The Keychain service name of a secret, e.g. io.github.roocxre.macutils.gmail."""
    return f"{APP_ID}.{name}"


def notification(name):
    """A distributed notification name, e.g. io.github.roocxre.macutils.open-inbox-report."""
    return f"{APP_ID}.{name}"
