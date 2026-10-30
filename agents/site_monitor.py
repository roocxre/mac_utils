"""Quits the frontmost browser when its active tab is on a domain in the OISD NSFW blocklist.

Browsers are recognized by what they are, not by name: any app that registers itself as an
http/https handler counts, wherever it runs from (a fresh install, a mounted disk image, a
folder in Downloads). The address is read by AppleScript where the browser supports it and
through the Accessibility API otherwise (Firefox and its relatives), which needs Python to
be allowed under System Settings > Privacy & Security > Accessibility.

The blocklist is refreshed daily and cached in the state folder (nsfw_blocklist.txt), so a
failed download never leaves the monitor running with an empty list.

It runs as a pair with site_keeper.py: each reloads the other if it is unloaded (lib/buddy.py),
so switching it off by hand doesn't stick. manage.py uninstall stands the pair down.
"""
import os
import plistlib
import re
import signal
import time
import urllib.parse
import urllib.request
from collections import namedtuple

from lib.common import run, run_main, state_path

OISD_URL = "https://nsfw.oisd.nl/"
CHECK_INTERVAL_SECONDS = 0.5
REFRESH_INTERVAL_SECONDS = 86400
RETRY_INTERVAL_SECONDS = 600
MIN_PLAUSIBLE_DOMAINS = 1000
CACHE_FILE = state_path("nsfw_blocklist.txt")
AX_PROMPTED_FLAG = state_path("accessibility_prompted")
KILL_GRACE_SECONDS = 3

# Ways scriptable browsers expose the current address: Chromium family, Safari family, generic.
SCRIPT_EXPRESSIONS = (
    "URL of active tab of front window",
    "URL of current tab of front window",
    "URL of front document",
)
SCRIPT_TEMPLATE = """
tell application "{path}"
    if (count of windows) is 0 then return ""
    return {expression}
end tell
"""
# Firefox-based browsers have no scripting dictionary at all; go straight to Accessibility.
GECKO_HINTS = ("mozilla", "firefox", "torbrowser", "torproject", "librewolf", "waterfox",
               "zen-browser", "mullvad", "floorp")
ADDRESS_RE = re.compile(r"^(https?://)?[\w-]+(\.[\w-]+)+(:\d+)?(/\S*)?$", re.IGNORECASE)
AX_MAX_NODES = 300
AX_MAX_DEPTH = 12

App = namedtuple("App", "name bundle_id path pid")


def clean_domain_rule(line):
    """Extract the domain from an Adblock ('||domain^') or hosts-file style line."""
    line = line.strip()
    if not line or line.startswith(("!", "#", "[")):
        return None
    if line.startswith("||"):
        line = line[2:]
    line = line.rstrip("^").rstrip("/")
    parts = line.split()
    if not parts:
        return None
    target = parts[-1].lower()
    if target in ("localhost", "0.0.0.0", "127.0.0.1"):
        return None
    return target


def parse_blocklist(content):
    return {domain for domain in map(clean_domain_rule, content.splitlines()) if domain}


def download_blocklist(log):
    """Fetch and cache the list. Returns the domain set, or None on failure."""
    try:
        request = urllib.request.Request(OISD_URL, headers={"User-Agent": "SiteMonitorAgent/1.0"})
        with urllib.request.urlopen(request, timeout=30) as response:
            content = response.read().decode("utf-8", errors="ignore")
    except Exception as error:
        log.warning("blocklist download failed: %s", error)
        return None
    domains = parse_blocklist(content)
    if len(domains) < MIN_PLAUSIBLE_DOMAINS:
        log.warning("blocklist download looked wrong (%d domains), ignoring", len(domains))
        return None
    tmp = CACHE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(content)
    os.replace(tmp, CACHE_FILE)
    log.info("blocklist refreshed: %d domains", len(domains))
    return domains


def load_cached_blocklist(log):
    try:
        with open(CACHE_FILE, encoding="utf-8") as f:
            domains = parse_blocklist(f.read())
    except OSError:
        return set()
    log.info("using cached blocklist: %d domains", len(domains))
    return domains


def extract_host(raw_url):
    if not raw_url:
        return ""
    if not raw_url.startswith(("http://", "https://")):
        raw_url = "http://" + raw_url
    try:
        return (urllib.parse.urlparse(raw_url).hostname or "").lower()
    except ValueError:
        return ""


def is_blocked(host, blocked_domains):
    """True if the host or any parent domain (down to the registrable part) is listed."""
    if not host:
        return False
    parts = host.split(".")
    return any(".".join(parts[i:]) in blocked_domains for i in range(max(1, len(parts) - 1)))


def describe_app(running_app):
    url = running_app.bundleURL()
    return App(str(running_app.localizedName() or ""), str(running_app.bundleIdentifier() or ""),
               str(url.path()) if url else "", running_app.processIdentifier())


def frontmost_app():
    """The app owning the frontmost normal window, found without needing a run loop."""
    import Quartz
    from AppKit import NSRunningApplication

    windows = Quartz.CGWindowListCopyWindowInfo(
        Quartz.kCGWindowListOptionOnScreenOnly | Quartz.kCGWindowListExcludeDesktopElements,
        Quartz.kCGNullWindowID)
    for window in windows or []:
        bounds = window.get("kCGWindowBounds") or {}
        if (window.get("kCGWindowLayer") != 0 or not window.get("kCGWindowAlpha")
                or bounds.get("Width", 0) < 100 or bounds.get("Height", 0) < 100):
            continue
        running_app = NSRunningApplication.runningApplicationWithProcessIdentifier_(
            window["kCGWindowOwnerPID"])
        return describe_app(running_app) if running_app else None
    return None


def handles_web_links(bundle_path):
    """True if the app's Info.plist marks it as a browser: it takes http(s) links and HTML files.

    (Media players such as VLC also take http links, but not HTML documents.)
    """
    try:
        with open(os.path.join(bundle_path, "Contents", "Info.plist"), "rb") as f:
            info = plistlib.load(f)
    except Exception:
        return False
    schemes = {scheme.lower() for url_type in info.get("CFBundleURLTypes", [])
               for scheme in url_type.get("CFBundleURLSchemes", [])}
    opens_html = any(
        "public.html" in doc_type.get("LSItemContentTypes", [])
        or {"html", "htm"} & {ext.lower() for ext in doc_type.get("CFBundleTypeExtensions", [])}
        for doc_type in info.get("CFBundleDocumentTypes", []))
    return bool(schemes & {"http", "https"}) and opens_html


def accessibility_trusted():
    from ApplicationServices import AXIsProcessTrusted

    return bool(AXIsProcessTrusted())


def _ax(element, attribute):
    from ApplicationServices import AXUIElementCopyAttributeValue

    error, value = AXUIElementCopyAttributeValue(element, attribute, None)
    return value if error == 0 else None


def accessibility_url(pid):
    """Read the focused window's address through the Accessibility tree.

    Returns '' when the app has no window open, and None when it has one we can't read.
    """
    from ApplicationServices import AXUIElementCreateApplication, AXUIElementSetMessagingTimeout

    app = AXUIElementCreateApplication(pid)
    AXUIElementSetMessagingTimeout(app, 0.5)
    window = _ax(app, "AXFocusedWindow") or _ax(app, "AXMainWindow")
    if window is None:
        windows = _ax(app, "AXWindows")
        if windows is None:
            return None
        if not windows:
            return ""
        window = windows[0]
    document = _ax(window, "AXDocument")
    if document and str(document).startswith(("http://", "https://")):
        return str(document)

    # Otherwise look for the page itself (a web area knows its URL), falling back to
    # whatever the address bar shows.
    address_bar = None
    queue = [(window, 0)]
    visited = 0
    while queue and visited < AX_MAX_NODES:
        element, depth = queue.pop(0)
        visited += 1
        role = _ax(element, "AXRole")
        if role == "AXWebArea":
            url = _ax(element, "AXURL")
            if url is not None:
                return str(url.absoluteString() if hasattr(url, "absoluteString") else url)
            continue
        if role in ("AXTextField", "AXComboBox") and address_bar is None:
            value = _ax(element, "AXValue")
            if isinstance(value, str) and ADDRESS_RE.match(value.strip()):
                address_bar = value.strip()
        if depth < AX_MAX_DEPTH:
            queue.extend((child, depth + 1) for child in (_ax(element, "AXChildren") or []))
    return address_bar


class Monitor:
    def __init__(self, log):
        self.log = log
        self.reported = {}
        self.browser_cache = {}   # bundle path -> is it a browser
        self.script_style = {}    # bundle path -> index into SCRIPT_EXPRESSIONS, or None if unscriptable
        self.pending_kills = {}   # pid -> time after which to force-kill

    def warn_hourly(self, key, message, *args, **kwargs):
        if time.time() - self.reported.get(key, 0) > 3600:
            self.reported[key] = time.time()
            self.log.warning(message, *args, **kwargs)

    def is_browser(self, app):
        if not app.path:
            return False
        if app.path not in self.browser_cache:
            self.browser_cache[app.path] = handles_web_links(app.path)
            if self.browser_cache[app.path]:
                self.log.info("watching browser %s (%s)", app.name, app.path)
        return self.browser_cache[app.path]

    def applescript_url(self, app):
        """Returns the address ('' for no window), or None if AppleScript can't provide it."""
        if any(hint in app.bundle_id.lower() for hint in GECKO_HINTS):
            return None
        if app.path in self.script_style:
            style = self.script_style[app.path]
            candidates = [] if style is None else [style]
        else:
            candidates = range(len(SCRIPT_EXPRESSIONS))

        unsupported = 0
        path = app.path.replace("\\", "\\\\").replace('"', '\\"')
        for index in candidates:
            script = SCRIPT_TEMPLATE.format(path=path, expression=SCRIPT_EXPRESSIONS[index])
            result = run(["osascript", "-e", script], timeout=2)
            if result is None:
                return None  # timed out, e.g. a permission prompt is waiting for an answer
            if result.returncode == 0:
                self.script_style[app.path] = index
                return result.stdout.strip()
            error = result.stderr.strip()
            if "syntax error" in error or "(-27" in error:
                unsupported += 1  # the app has no such scripting vocabulary
            else:
                # Typically a denied Automation permission (-1743).
                self.warn_hourly(("script", app.path), "cannot script %s: %s", app.name, error)
                return None
        if candidates and unsupported == len(candidates):
            self.script_style[app.path] = None
        return None

    def read_url(self, app):
        """Returns the address ('' if there is none), or None if it cannot be read at all."""
        url = self.applescript_url(app)
        if url is not None:
            return url
        if accessibility_trusted():
            url = accessibility_url(app.pid)
            if url is not None:
                return url
        self.warn_hourly(("unreadable", app.path),
                         "cannot read the address in %s (%s); allow Python under System Settings > "
                         "Privacy & Security > Accessibility%s", app.name, app.path,
                         "" if not accessibility_trusted() else " (already allowed; the app exposes no address)")
        return None

    def quit_app(self, app):
        try:
            os.kill(app.pid, signal.SIGTERM)
            self.pending_kills[app.pid] = time.time() + KILL_GRACE_SECONDS
        except OSError as error:
            self.log.warning("could not quit %s: %s", app.name, error)

    def finish_kills(self):
        for pid, deadline in list(self.pending_kills.items()):
            if time.time() < deadline:
                continue
            del self.pending_kills[pid]
            try:
                os.kill(pid, signal.SIGKILL)  # still there after the grace period
            except OSError:
                pass

    def check(self, blocked_domains):
        self.finish_kills()
        app = frontmost_app()
        if app is None or app.pid in self.pending_kills or not self.is_browser(app):
            return
        url = self.read_url(app)
        if url is None:
            return
        host = extract_host(url)
        if is_blocked(host, blocked_domains):
            self.log.info("blocked %s in %s, quitting it", host, app.name)
            self.quit_app(app)

    def self_check(self):
        """Log, for every browser running right now, whether its address can be read."""
        from AppKit import NSWorkspace

        trusted = accessibility_trusted()
        self.log.info("accessibility permission: %s", "granted" if trusted else "NOT granted")
        for running_app in NSWorkspace.sharedWorkspace().runningApplications():
            app = describe_app(running_app)
            if not self.is_browser(app):
                continue
            if self.applescript_url(app) is not None:
                method = "AppleScript"
            elif trusted and accessibility_url(app.pid) is not None:
                method = "Accessibility"
            else:
                method = None
            self.log.info("  %s: %s", app.name, f"readable via {method}" if method else "NOT readable")


def prompt_for_accessibility_once(log):
    """Ask macOS to show its 'allow Accessibility' dialog the first time we find it missing."""
    if accessibility_trusted() or os.path.exists(AX_PROMPTED_FLAG):
        return
    from ApplicationServices import AXIsProcessTrustedWithOptions

    AXIsProcessTrustedWithOptions({"AXTrustedCheckOptionPrompt": True})
    open(AX_PROMPTED_FLAG, "w").close()
    log.info("asked for Accessibility permission")


class Blocklist:
    """The current domain set, re-downloaded daily and cached on disk."""

    def __init__(self, log):
        self.log = log
        self.domains = load_cached_blocklist(log)
        self.next_refresh = 0

    def current(self):
        if time.time() >= self.next_refresh:
            fresh = download_blocklist(self.log)
            if fresh:
                self.domains = fresh
            self.next_refresh = time.time() + (REFRESH_INTERVAL_SECONDS if fresh else RETRY_INTERVAL_SECONDS)
        return self.domains


def main(log):
    from lib import buddy
    import objc

    buddy.stay_alive("sitemonitor", "sitekeeper", log)

    monitor = Monitor(log)
    blocklist = Blocklist(log)
    log.info("started with %d domains", len(blocklist.current()))
    prompt_for_accessibility_once(log)
    monitor.self_check()
    trusted = accessibility_trusted()

    while True:
        blocked_domains = blocklist.current()
        # No run loop here, so Cocoa objects created per check must be released by hand.
        with objc.autorelease_pool():
            try:
                if not trusted and accessibility_trusted():
                    # Granted while running: report what has become readable.
                    trusted = True
                    monitor.reported.clear()
                    monitor.self_check()
                monitor.check(blocked_domains)
            except Exception:
                monitor.warn_hourly("check", "check failed", exc_info=True)
        time.sleep(CHECK_INTERVAL_SECONDS)


if __name__ == "__main__":
    run_main("site_monitor", main)
