"""Turns a typed request into a plan: what will happen, worked out in plain code.

Everything the assistant can do is decided here. A request that isn't a command is reported with
the closest commands (atlas/catalogue.py). All of it is read-only except opening things and starting/stopping this project's own agents:
nothing here writes, moves or deletes a file.
"""
import difflib
import glob
import os
import plistlib
import re
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from typing import Callable, List, Optional

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HOME = os.path.expanduser("~")
sys.path.insert(0, ROOT)
import manage  # noqa: E402  (agent names and the install/uninstall logic)
from lib import components  # noqa: E402

APP_DIRS = ["/Applications", "/Applications/Utilities", "/System/Applications",
            "/System/Applications/Utilities", os.path.join(HOME, "Applications"),
            "/System/Library/CoreServices"]
# Agents this assistant will not stop, restart or start: the site monitor pair is meant to be
# hard to switch off, and the assistant can't usefully restart itself.
PROTECTED_AGENTS = {"sitemonitor", "sitekeeper", "assistant"}
# Never shown by `read`, however it is asked for.
SECRET_NAMES = {".ssh", ".gnupg", ".aws", ".netrc", ".env", "Keychains", "id_rsa", "id_ed25519",
                "id_ecdsa", "id_dsa", ".npmrc", ".pypirc"}
SKIP_IN_SEARCH = ("/Library/", "/node_modules/", "/.git/", "/.venv/", "/__pycache__/", "/.Trash/",
                  "/.cache/", "/site-packages/", "/go/pkg/", "/vendor/", "/DerivedData/")
KNOWN_FOLDERS = {"home": HOME, "desktop": "~/Desktop", "documents": "~/Documents",
                 "downloads": "~/Downloads", "pictures": "~/Pictures", "movies": "~/Movies",
                 "music": "~/Music", "applications": "/Applications", "archive": "~/Archive",
                 "screenshots": "~/Archive/Screenshots"}
APP_ALIASES = {"vs code": "visual studio code", "vscode": "visual studio code", "code": "visual studio code",
               "chrome": "google chrome", "preferences": "system settings", "settings": "system settings",
               "system preferences": "system settings", "terminal": "terminal"}
MAX_CHOICES = 25
MAX_READ_BYTES = 8192
MAX_READ_CHARS = 1800


@dataclass
class Outcome:
    message: str = ""
    choices: List[str] = field(default_factory=list)
    on_choice: Optional[Callable[[str], "Outcome"]] = None
    multiple: bool = False  # several choices may be picked; on_choice then gets a list
    # A yes/no question: `question` is asked with `yes_label`; on yes, on_yes() gives the next outcome.
    question: str = ""
    yes_label: str = "OK"
    on_yes: Optional[Callable[[], "Outcome"]] = None


@dataclass
class Plan:
    kind: str                       # open, find, list, read, agents, install, memory, battery, power,
                                    # dashboard, yours, help, miss, error, denied (and the other modules' own)
    summary: str                    # plain description of what will happen
    execute: Callable[[], Outcome]
    consult: bool = False           # unused (it asked the removed chat model for a remark); kept while callers pass it
    looks_only: bool = False        # changes nothing: it only looks or reports


# ---------------------------------------------------------------- paths and files

def abbr(path):
    return "~" + path[len(HOME):] if path == HOME or path.startswith(HOME + "/") else path


def expand(text):
    text = text.strip().strip("'\"").replace("\\ ", " ")
    # "~notes" is how you write ~/notes; to the shell it means another user's home.
    if re.match(r"^~[^/~]", text) and os.path.expanduser(text) == text:
        text = "~/" + text[1:]
    return os.path.normpath(os.path.expanduser(text)) if text else ""


def looks_like_path(text):
    return bool(re.match(r"^(~|/|\./|\.\./)", text.strip().strip("'\"")))


def complete_path(text):
    """Finder's Go to Folder behaviour: the path itself, else everything it is a prefix of."""
    path = expand(text)
    if not path:
        return []
    if os.path.exists(path):
        return [path]
    directory, prefix = os.path.split(path)
    try:
        names = os.listdir(directory or ".")
    except OSError:
        return []
    prefix = prefix.lower()
    matches = [n for n in names if n.lower().startswith(prefix) and not n.startswith(".")]
    return [os.path.join(directory, n) for n in sorted(matches, key=str.lower)]


def _searchable(path):
    return not any(part in path for part in SKIP_IN_SEARCH) and "/." not in path[len(HOME):]


_LEAD_IN = re.compile(
    r"^(?:(?:hey|hi|ok|okay|yo|so|now|alright)[, ]+)?"
    r"(?:(?:can|could|would|will) (?:you|u) (?:please |pls )?|please |pls |go ahead and |"
    r"i (?:want|need) (?:you )?to |i'd like (?:you )?to |help me |let'?s |let us )+", re.IGNORECASE)
_SIGN_OFF = re.compile(r"(?:[ ,]+(?:please|pls|plz|for me|thanks|thank you|real quick|right now))+[ .!?]*$",
                       re.IGNORECASE)
_KIND_WORDS = {"directory": "dir", "folder": "dir", "dir": "dir", "file": "file", "document": "file",
               "doc": "file", "app": "app", "application": "app", "program": "app"}


def strip_politeness(text):
    """'can you find the scripts directory please' -> 'find the scripts directory'."""
    text = _SIGN_OFF.sub("", text.strip())
    return _LEAD_IN.sub("", text).strip() or text.strip()


def clean_target(target):
    """'the scripts directory' -> ('scripts', 'dir'). Paths are left exactly as typed."""
    # A request may end in a full stop ("find my lab 4 file."); the stop is not part of the name.
    target = target.strip().rstrip(".!?,").strip()
    # "the files in ~/notes", "everything inside downloads" -> the folder itself
    target = re.sub(r"^(?:all\s+)?(?:the\s+|my\s+)?(?:files?|contents?|everything|stuff|things|items)"
                    r"(?:\s+and\s+folders)?\s+(?:in|inside|of|under|from)\s+", "", target, flags=re.IGNORECASE)
    if looks_like_path(target):
        return target, None
    target = re.sub(r"^(?:me |for )?(?:the|my|a|an|that|this)\s+", "", target, flags=re.IGNORECASE)
    target = re.sub(r"^me\s+", "", target, flags=re.IGNORECASE)
    words = target.split()
    kind = _KIND_WORDS.get(words[-1].lower()) if len(words) > 1 else None
    if kind:
        target = " ".join(words[:-1])
    return target.strip(), kind


def outside_home():
    """Files worth finding that Spotlight's home-folder search can't see.

    Disk images that are mounted right now (wherever the image file lives), what is on those
    mounted volumes, and downloads a browser opened straight from its temporary folder.
    """
    paths = set()
    try:
        info = subprocess.run(["hdiutil", "info", "-plist"], capture_output=True, timeout=10).stdout
        for image in plistlib.loads(info).get("images", []):
            if image.get("image-path"):
                paths.add(image["image-path"])
            for entity in image.get("system-entities", []):
                mount = entity.get("mount-point")
                if mount and mount.startswith("/Volumes/"):
                    paths.add(mount)
                    paths.update(glob.glob(os.path.join(mount, "*")))
    except Exception:
        pass
    tmp = tempfile.gettempdir()
    for pattern in ("*Downloads*/*", "*Downloads*/*/*"):
        paths.update(glob.glob(os.path.join(tmp, pattern)))
    # The temp folder is reachable as /var/... and /private/var/...; keep one of each real file.
    unique = {os.path.realpath(p): p for p in paths if os.path.exists(p)}
    return [real for real in unique if not os.path.basename(real).startswith(".")]


def unindexed_roots():
    """Places Spotlight does not index, as (folder, how deep to look) pairs."""
    hidden = [d for d in glob.glob(os.path.join(HOME, ".*"))
              if os.path.isdir(d) and os.path.basename(d) not in (".Trash", ".cache", ".npm", ".nvm", ".cargo", ".rustup")]
    roots = [(HOME, 1)] + [(d, 4) for d in hidden]  # depth 1 on home: dotfiles like ~/.zshrc
    roots += [(tempfile.gettempdir(), 4), ("/private/tmp", 3), ("/usr/local", 4), ("/etc", 2),
              ("/opt/homebrew/bin", 1), ("/opt/homebrew/etc", 3), ("/opt/homebrew/Caskroom", 2),
              ("/opt/homebrew/opt", 1)]
    return [(root, depth) for root, depth in roots if os.path.isdir(root)]


def scan_unindexed(word, seconds=5):
    """Walk the unindexed places for names containing a word. Stops after a few seconds."""
    pattern = "*" + re.sub(r"([\[\]*?\\])", r"\\\1", word) + "*"
    found, deadline = [], time.monotonic() + seconds
    for root, depth in unindexed_roots():
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        cmd = ["find", root, "-maxdepth", str(depth),
               "(", "-name", "node_modules", "-o", "-name", ".git", "-o", "-name", "site-packages", ")", "-prune",
               "-o", "-iname", pattern, "-print"]
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
        try:
            out, _ = proc.communicate(timeout=remaining)
        except subprocess.TimeoutExpired:
            proc.kill()
            out, _ = proc.communicate()
        found.extend(out.splitlines())
    return found


def _location_rank(path):
    """Your own visible files first, then apps and mounted volumes, then clutter, then the system."""
    if path == HOME or path.startswith(HOME + "/"):
        clutter = any(part in path for part in SKIP_IN_SEARCH) or "/." in path[len(HOME):]
        return 2 if clutter else 0
    if path.startswith(("/Applications/", "/Volumes/")) or "/T/" in path and "Downloads" in path:
        return 1
    return 3


def find_files(query, limit=MAX_CHOICES, kind=None):
    """Name search across the whole machine, your own files first.

    Spotlight covers everything it indexes; hidden folders, temp folders and a few system
    places it skips are scanned directly. Nothing is left out, only ranked lower.
    kind: "dir" for folders only, "file" for files only, None for either.
    """
    q = query.lower().strip()
    words = q.split()
    if not words:
        return []
    # Ask for the most distinctive word; the rest must also be in the name, in any order, so
    # "firefox dmg" finds "Firefox 157.0.dmg".
    key = max(words, key=len)
    spotlight = subprocess.run(["mdfind", "-name", key], capture_output=True, text=True, timeout=20).stdout.splitlines()
    candidates = spotlight + outside_home() + (scan_unindexed(key) if len(key) >= 3 else [])

    scored, seen = [], set()
    for path in candidates:
        name = os.path.basename(path.rstrip("/")).lower()
        if not all(word in name for word in words):
            continue
        real = os.path.realpath(path)
        if real in seen or not os.path.exists(real):
            continue
        seen.add(real)
        if kind in ("dir", "file") and os.path.isdir(real) != (kind == "dir"):
            continue
        stem = os.path.splitext(name)[0]
        rank = 0 if name == q or stem == q else 1 if name.startswith(q) else 2
        try:
            modified = os.path.getmtime(real)
        except OSError:
            modified = 0
        scored.append((_location_rank(real), rank, -modified, real))
        if len(scored) >= 5000:
            break
    return [p for _, _, _, p in sorted(scored)[:limit]]


def find_apps(name, name_only=False):
    """Installed apps (and apps run straight from a mounted disk image) matching a name.

    name_only: accept only the app's actual name or a clear start of it (3+ letters), not
    any app that merely contains the text. Used when no command word says "this is an app".
    """
    q = APP_ALIASES.get(name.strip().lower(), name.strip().lower())
    if not q:
        return []
    found = set()
    spot = subprocess.run(
        ["mdfind", f"kMDItemContentType == 'com.apple.application-bundle' && "
                   f"kMDItemDisplayName == '*{q.replace(chr(39), '')}*'cd"],
        capture_output=True, text=True, timeout=15)
    found.update(p for p in spot.stdout.splitlines() if p.endswith(".app"))
    for directory in APP_DIRS:
        found.update(glob.glob(os.path.join(directory, "*.app")))
    # Disk images that were mounted but not installed are often not indexed.
    found.update(glob.glob("/Volumes/*/*.app"))
    found.update(glob.glob("/Volumes/*/*/*.app"))

    scored = []
    for path in found:
        if ".app/" in path or "/AppTranslocation/" in path:
            continue
        base = os.path.basename(path)[:-4].lower()
        words = re.split(r"[\s\-_.]+", base)
        if base == q:
            score = 0
        elif base.startswith(q) or q in words:
            score = 1
        elif all(w in base for w in q.split()):
            score = 2
        elif q in base:
            score = 3
        elif difflib.SequenceMatcher(None, q, base).ratio() >= 0.8:
            score = 4
        else:
            continue
        location = (0 if path.startswith("/Applications/") else 1 if path.startswith("/System/")
                    else 2 if path.startswith(HOME) else 3)
        scored.append((score, location, base, path))
    if name_only:
        scored = [s for s in scored if s[0] == 0 or (s[0] == 1 and len(q) >= 3)]
    if not scored:
        return []
    best = min(s[0] for s in scored)
    group = [s for s in scored if s[0] == best]
    if any(s[1] <= 1 for s in group):
        # Real apps beat same-named helpers tucked away in Library folders.
        group = [s for s in group if s[1] <= 1]
    seen, out = set(), []
    for _, _, base, path in sorted(group):
        if base not in seen:  # same app in two places: keep the best-located one
            seen.add(base)
            out.append(path)
    return out


def open_path(path, reveal=False):
    cmd = ["open", "-R", path] if reveal else ["open", path]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
    if result.returncode != 0:
        return Outcome(f"couldn't open {abbr(path)}: {result.stderr.strip()[:200]}")
    return Outcome("")


def list_directory(path):
    try:
        with os.scandir(path) as entries:
            items = sorted(((e.is_dir(follow_symlinks=False), e.name) for e in entries
                            if not e.name.startswith(".")), key=lambda t: (not t[0], t[1].lower()))
    except OSError as error:
        return Outcome(f"can't list {abbr(path)}: {error.strerror or error}")
    if not items:
        return Outcome(f"{abbr(path)} is empty")
    shown = items[:MAX_CHOICES * 2]
    choices = [("▸ " if is_dir else "   ") + name for is_dir, name in shown]
    more = f" (first {len(shown)} of {len(items)})" if len(items) > len(shown) else ""

    def on_choice(choice):
        child = os.path.join(path, choice[3:] if choice.startswith("   ") else choice[2:])
        return list_directory(child) if os.path.isdir(child) else open_path(child, reveal=True)

    return Outcome(f"{abbr(path)}{more}", choices, on_choice)


def read_text(path):
    parts = set(path.split(os.sep))
    if parts & SECRET_NAMES or os.path.basename(path).startswith(".env"):
        return Outcome("not reading that one")
    if os.path.isdir(path):
        return list_directory(path)
    try:
        # Owner-only files are keys and configs with passwords; what is read here is shown in a
        # dialog on screen, so leave those to an editor.
        if not os.stat(path).st_mode & 0o044:
            return Outcome(f"{abbr(path)} is private (readable only by you), so i won't show it here. "
                           f"`open` it instead.")
        with open(path, "rb") as f:
            head = f.read(MAX_READ_BYTES)
        size = os.path.getsize(path)
    except OSError as error:
        return Outcome(f"can't read {abbr(path)}: {error.strerror or error}")
    if b"\x00" in head:
        return Outcome(f"{os.path.basename(path)} is a binary file, not showing it")
    text = head.decode("utf-8", errors="replace")
    shortened = len(text) > MAX_READ_CHARS or size > len(head)
    return Outcome(f"{abbr(path)}\n\n{text[:MAX_READ_CHARS]}" + ("\n\n… (shortened)" if shortened else ""))


# ---------------------------------------------------------------- agents

def resolve_agent(name):
    key = re.sub(r"[\s_\-]+", "", name.lower())
    names = list(manage.AGENTS)
    if key in names:
        return key
    prefixed = [n for n in names if n.startswith(key)] if key else []
    if len(prefixed) == 1:
        return prefixed[0]
    close = difflib.get_close_matches(key, names, n=1, cutoff=0.75)
    return close[0] if close else None


def run_manage(*args):
    result = subprocess.run([sys.executable, os.path.join(ROOT, "manage.py"), *args],
                            capture_output=True, text=True, timeout=60)
    return (result.stdout + result.stderr).strip()


# ---------------------------------------------------------------- interpretation

HELP_TEXT = ("dnd | dnd on | dnd off | dnd auto   (hold popup messages; auto = shared screen, display, lid)\n"
             "lock keyboard | trackpad | everything <time>   (45s, 2m; 30s default, 10m at most; ctrl+opt+cmd+esc unlocks)\n"
             "$ <command>   (run it in the terminal, never as admin)   ·   shortcuts   (disk space, battery, ip, uptime)\n"
             "free up memory   (quits spotify, messages, discord, system utils; lists what else is big)\n"
             "open <app | file | folder | url>\n"
             "install <program>   (looks it up in Homebrew, asks, then runs it in Terminal)\n"
             "find <name>   ·   ls <folder>   ·   read <file>\n"
             "search inside <words> [pdfs | videos | images | docs…] [in <folder>]   (by contents or kind, newest first)\n"
             "status [agent]   ·   logs <agent>   ·   start | stop | restart <agent>\n"
             "run <agent> now   ·   preview <agent>   (screenshots, downloads, dev, caches, logs, pock)\n"
             "fixpock   ·   audio   ·   inbox   ·   clean clipboard   ·   capture text\n"
             "tools <words>   (which command does that; bare `tools` lists them all)\n"
             "mail status · mail setup file=…   (the email layer: Gmail sign-in and polling)\n"
             "inbox report · spam · unsub   (the inbox dashboard: senders to unsubscribe from or block)\n"
             "broker report · brokers   (the data broker dashboard: removal requests to approve, manual tasks)\n"
             "dataflow site <domain> · dataflow recent · dataflow mode <domain> strict   (where your email goes: the Edge extension)\n"
             "~/path/… opens that folder like Go to Folder")
# Said when a request isn't a command; the closest commands follow it (atlas/catalogue.py).
NOT_A_COMMAND = "i only do commands. type help."


def _error(text, final=False):
    """A request that could not be carried out.

    Kind "error" means the program came up empty (nothing found). final=True makes it "denied": a
    rule said no, and that is the answer.
    """
    return Plan("denied" if final else "error", text, lambda: Outcome(text), looks_only=True)


def _miss(raw):
    """Not a command at all: said so, with the commands closest to what was asked."""
    def execute():
        from atlas import catalogue
        return Outcome(NOT_A_COMMAND + catalogue.suggestion(raw))

    return Plan("miss", f"not a command: {raw}", execute, looks_only=True)


def _open_plan(target):
    target = target.strip()
    if re.match(r"^https?://\S+$", target):
        return Plan("open", f"open the web address {target}", lambda: open_path(target))
    if looks_like_path(target):
        candidates = complete_path(target)
        if not candidates:
            return _error(f"nothing at {target}")
        return _choose_to_open(candidates, f"open {abbr(candidates[0])}" if len(candidates) == 1 else
                               f"open one of {len(candidates)} matches for {target}")
    target, kind = clean_target(target)
    key = target.lower()
    if key in KNOWN_FOLDERS and kind in (None, "dir"):
        folder = expand(KNOWN_FOLDERS[key])
        return Plan("open", f"open the folder {abbr(folder)}", lambda: open_path(folder))
    apps = find_apps(target) if kind in (None, "app") else []
    if apps:
        return _choose_to_open(apps, f"open the app {os.path.basename(apps[0])[:-4]}" if len(apps) == 1
                               else f"open one of {len(apps)} apps matching {target}")
    files = find_files(target, limit=10, kind=kind) if kind != "app" else []
    if files:
        return _choose_to_open(files, f"open {abbr(files[0])}" if len(files) == 1
                               else f"open one of {len(files)} matches for {target}")
    return _error(f"couldn't find anything called {target}")


def _choose_to_open(candidates, summary):
    if len(candidates) == 1:
        return Plan("open", summary, lambda: open_path(candidates[0]))
    labels = {abbr(p): p for p in candidates[:MAX_CHOICES]}
    return Plan("open", summary, lambda: Outcome("which one?", list(labels),
                                                  lambda c: open_path(labels[c])))


def _find_plan(query):
    query, kind = clean_target(query)
    results = find_apps(query) if kind == "app" else find_files(query, kind=kind)
    if not results:
        return _error(f"nothing found for {query}")
    labels = {abbr(p): p for p in results}
    return Plan("find", f"search your files for {query!r} and show matches",
                lambda: Outcome(f"{len(results)} match(es) for {query}", list(labels),
                                lambda c: open_path(labels[c], reveal=True)), looks_only=True)


def _resolve_one(target):
    """A single existing path for a typed name or path, else (None, message)."""
    target, kind = clean_target(target)
    if looks_like_path(target):
        candidates = complete_path(target)
    elif target.lower() in KNOWN_FOLDERS:
        candidates = [expand(KNOWN_FOLDERS[target.lower()])]
    else:
        candidates = find_files(target, limit=5, kind=kind)
    if not candidates:
        return None, f"couldn't find {target}"
    return candidates, None


def _list_plan(target):
    candidates, error = _resolve_one(target)
    if error:
        return _error(error)
    path = candidates[0]
    return Plan("list", f"list the contents of {abbr(path)}", lambda: list_directory(path), looks_only=True)


def _read_plan(target):
    candidates, error = _resolve_one(target)
    if error:
        return _error(error)
    path = candidates[0]
    return Plan("read", f"show the first part of {abbr(path)}", lambda: read_text(path), looks_only=True)


def _agent_plan(verb, name):
    agent = resolve_agent(name)
    if agent is None:
        return None
    if agent in PROTECTED_AGENTS:
        return _error(f"{agent} isn't mine to {verb}", final=True)
    command = "uninstall" if verb in ("stop", "kill", "disable") else "install"
    summary = f"{verb} the {agent} agent"
    return Plan("agents", summary, lambda: Outcome(run_manage(command, agent)))


def _install_plan(name):
    from atlas import brew_install as brew

    if brew.BREW is None:
        return _error("Homebrew isn't installed, and that is what i install through", final=True)

    def offer(pkg):
        if pkg.disabled:
            return Outcome(f"Homebrew has disabled {pkg.token}; not installing it")
        if pkg.installed:
            return Outcome(f"{pkg.name} is already installed")

        def go():
            error = brew.run_in_terminal(pkg)
            return Outcome(error or f"installing {pkg.name} in Terminal")
        return Outcome(question=brew.describe(pkg), yes_label="Install", on_yes=go)

    def execute():
        exact = brew.lookup(brew.normalize(name))
        if len(exact) == 1:
            return offer(exact[0])
        options = ([(p.kind, p.token) for p in exact] or brew.search(name))
        if not options:
            return Outcome(f"Homebrew has nothing called {name}")
        labels = {brew.label(kind, token): (kind, token) for kind, token in options[:MAX_CHOICES]}

        def picked(choice):
            kind, token = labels[choice]
            match = [p for p in brew.lookup(token) if p.kind == kind]
            return offer(match[0]) if match else Outcome(f"couldn't look up {token}")
        return Outcome(f"which one did you mean by {name}?", list(labels), picked)

    return Plan("install", f"look up {name} in Homebrew and offer to install it", execute)


def _dnd_plan(low):
    """Do not disturb. Kind "yours": only run when you type it, never from a guessed restatement."""
    def plan(summary, action):
        return Plan("yours", summary, lambda: Outcome(action()))

    if re.match(r"^(?:dnd|do not disturb|don'?t disturb(?: me)?)(?: status)?\??$", low) and "me" not in low.split():
        from atlas import dnd
        return plan("report do not disturb", dnd.status)
    m = re.match(r"^(?:dnd|do not disturb|don'?t disturb(?: me)?)(?: (on|off|auto|automatic))?$", low) \
        or re.match(r"^(?:turn |switch )?(on|off) (?:dnd|do not disturb)$", low) \
        or re.match(r"^(?:turn |switch )(?:dnd|do not disturb) (on|off)$", low)
    if m or low in ("busy", "i'm busy", "im busy", "i'm back", "im back", "not busy"):
        from atlas import dnd
        value = {"automatic": "auto", None: "on"}.get(m.group(1), m.group(1)) if m else \
            ("on" if low in ("busy", "i'm busy", "im busy") else "auto")
        return plan(f"set do not disturb to {value}", lambda: dnd.set_mode(value))
    return None


_KEYS = r"(?:keyboard|keys)"
_PAD = r"(?:trackpad|touchpad|mouse)"
_KEYBOARD_LOCK = re.compile(
    rf"^(?:(?:lock|disable|freeze|block|clean(?:ing)?)\s+(?:the\s+|my\s+)?"
    rf"(?P<a>{_KEYS}\s+(?:and|\+|&)\s+{_PAD}|{_PAD}\s+(?:and|\+|&)\s+{_KEYS}|input|everything|{_KEYS}|{_PAD})"
    rf"|(?P<b>{_KEYS}|{_PAD}|input)\s+lock|(?:keyboard\s+)?cleaning\s+mode)")
_DURATION = re.compile(r"(\d+(?:\.\d+)?)\s*(h|hr|hrs|hours?|m|min|mins|minutes?|s|sec|secs|seconds?)?\b")


def _keyboard_lock_plan(low):
    """Lock the keyboard, trackpad or both for a while. Always timed; kind "yours", so never from a guess."""
    match = _KEYBOARD_LOCK.match(low)
    if not match:
        return None
    from atlas import keyboard_lock

    named = match.group("a") or match.group("b") or "keyboard"
    has_keys, has_pad = bool(re.search(_KEYS, named)), bool(re.search(_PAD, named))
    what = "both" if named in ("input", "everything") or (has_keys and has_pad) else "trackpad" if has_pad else "keyboard"
    rest = re.sub(r"^\s*(?:for|in)\s+", "", low[match.end():].strip())
    seconds, note = keyboard_lock.DEFAULT_SECONDS, ""
    if rest:
        m = _DURATION.fullmatch(re.sub(r"^(?:a|one)\s+", "1 ", rest).replace("half a minute", "30 s"))
        if not m:
            return _error("how long? e.g. `lock keyboard 2m` or `lock keyboard 45s`", final=True)
        unit = (m.group(2) or "s")[0]
        seconds = float(m.group(1)) * {"h": 3600, "m": 60, "s": 1}[unit]
    if seconds > keyboard_lock.MAX_SECONDS:
        seconds, note = keyboard_lock.MAX_SECONDS, " (the most it allows)"
    if seconds < 1:
        return _error("that's no time at all", final=True)

    def execute():
        script = os.path.join(os.path.dirname(os.path.abspath(__file__)), "keyboard_lock.py")
        process = subprocess.Popen([sys.executable, script, str(seconds), what], stdout=subprocess.DEVNULL,
                                   stderr=subprocess.PIPE, text=True, start_new_session=True)
        try:
            process.wait(timeout=0.8)
        except subprocess.TimeoutExpired:
            return Outcome("")   # running: its banner says the rest
        return Outcome(process.stderr.read().strip() or "the keyboard lock stopped straight away")

    return Plan("yours", f"lock the {keyboard_lock.WHAT[what]} for {keyboard_lock.describe(seconds)}{note}",
                execute)


_MEMORY_WORDS = re.compile(r"^(?:memory|ram|free(?:\s+up)?\s+(?:some\s+)?(?:memory|ram)|clear\s+(?:some\s+)?(?:memory|ram)"
                           r"|(?:clean|clear)\s*up\s+(?:memory|ram)|what'?s\s+using\s+(?:my\s+)?(?:memory|ram)"
                           r"|(?:memory|ram)\s+(?:helper|cleanup|status|hogs?))$")


def _memory_plan():
    from atlas import memory_helper

    def execute():
        asked = memory_helper.quit_always(spare_front=True)
        head = memory_helper.report_text(asked)
        items = memory_helper.candidates()
        by_label = {item["label"]: item for item in items}
        labels = list(by_label)
        if not labels:
            return Outcome(head + "\nnothing else big enough to bother with")

        def picked(chosen):
            done = []
            for label in chosen:
                if label in by_label:
                    done.append(memory_helper.stop(by_label[label]))
            return Outcome("\n".join(done) or "nothing picked")

        return Outcome(message=head + "\npick what else to quit (cmd-click for several), or cancel",
                       choices=labels, on_choice=picked, multiple=True)

    return Plan("memory", "quit the apps on the always-quit list and show what else is using memory",
                execute)


def interpret(text):
    return _interpret((text or "").strip())


_BATTERY_WORDS = re.compile(r"^(?:battery|power)\s+(?:helper|dashboard|usage|drain|hogs?|saver dashboard)$"
                            r"|^what'?s\s+(?:using|draining|eating)\s+(?:my\s+|the\s+)?(?:battery|power)$")
_SAVING_WORDS = re.compile(r"^(?:turn\s+|switch\s+)?(?:power[\s-]?sav(?:ing|er)|battery[\s-]?sav(?:ing|er)|low\s+power(?:\s+mode)?)"
                           r"(?:\s+mode)?\s*(on|off|status)?$"
                           r"|^(?:turn|switch)\s+(on|off)\s+(?:power[\s-]?saving|battery[\s-]?saving|low\s+power(?:\s+mode)?)$")


def _battery_plan():
    from atlas import battery_helper

    def execute():
        head = battery_helper.idle_text(battery_helper.quit_idle())
        from atlas import battery_dashboard
        battery_dashboard.open_dashboard(head)
        return Outcome(head + "\nthe battery dashboard is open\n" + battery_helper.summary_text())

    return Plan("battery", f"quit apps unused for {battery_helper.IDLE_HOURS} hours and open the battery dashboard",
                execute)


def _saving_plan(which):
    from atlas import battery_helper

    if which == "status":
        return Plan("power", "say whether power saving is on", lambda: Outcome(battery_helper.status_text()),
                    looks_only=True)

    def execute():
        text = battery_helper.turn_on() if which == "on" else battery_helper.turn_off()
        from atlas import battery_dashboard
        battery_dashboard.refresh_if_open()
        return Outcome(text)

    summary = ("turn power saving on: quit unused apps, Low Power Mode, short display sleep, Power Nap off, dimmer screen"
               if which == "on" else "turn power saving off and put every setting it changed back")
    return Plan("power", summary, execute)


# The email commands (atlas/mail_commands.py carries them out), matched as that module matches them, by the
# part they need (lib/components.py). A part that isn't installed is said so here instead of being imported.
_EMAIL_PARTS = (
    ("dataflow", "the email extension (dataflow)", re.compile(
        r"^(?:dataflow|site_report|recent_findings|set_site_mode|canary_run|canary_results|policy_findings)\b",
        re.IGNORECASE)),
    ("brokers", "the data broker agent", re.compile(
        r"^(?:brokers?|broker_\w+|manual_done|registry_validate|optout)\b"
        r"|^(?:open\s+|show\s+(?:me\s+)?)?(?:the\s+|my\s+)?(?:data\s+)?(?:brokers?|people[\s-]search|opt[\s-]?outs?|"
        r"data\s+removal)(?:\s+(?:report|dashboard|review|removal|requests))?$"
        r"|^(?:open\s+|show\s+(?:me\s+)?)?(?:the\s+)?(?:data\s+)?brokers?(?:\s+agent)?\s+(?:debug|watch|live|monitor)$"
        r"|^watch\s+(?:the\s+)?(?:data\s+)?brokers?(?:\s+agent)?$", re.IGNORECASE)),
    ("unsub", "the inbox agent", re.compile(
        r"^(?:unsub\w*|spam)\b|^(?:open\s+|show\s+(?:me\s+)?)?(?:the\s+|my\s+)?"
        r"(?:inbox|e-?mail|mail|spam|junk|unsubscribe)\s+(?:report|dashboard|review)$", re.IGNORECASE)),
    ("mail", "the email layer", re.compile(r"^mail\b", re.IGNORECASE)),
)


def _email_part_missing(raw):
    """A plan saying so when raw is an email command whose part isn't installed, else None."""
    line = raw.strip()
    for part, title, pattern in _EMAIL_PARTS:
        if pattern.match(line):
            return None if components.installed(part) else _error(components.not_installed_message(part, title),
                                                                    final=True)
    return None


def _interpret(raw):
    raw = strip_politeness(raw)
    low = raw.lower()
    if not raw or low in ("help", "?", "commands"):
        return Plan("help", "show the list of commands", lambda: Outcome(HELP_TEXT), looks_only=True)
    if looks_like_path(raw) or re.match(r"^https?://\S+$", raw):
        return _open_plan(raw)
    if _MEMORY_WORDS.match(low):
        return _memory_plan()
    if _BATTERY_WORDS.match(low):
        return _battery_plan()
    m = _SAVING_WORDS.match(low)
    if m:
        return _saving_plan(m.group(1) or m.group(2) or "on")
    from atlas import catalogue
    catalogue_plan = catalogue.interpret(raw, low, sys.modules[__name__])
    if catalogue_plan is not None:
        return catalogue_plan
    from atlas import tools
    tool_plan = tools.interpret(raw, sys.modules[__name__])
    if tool_plan is not None:
        return tool_plan
    missing = _email_part_missing(raw)
    if missing is not None:
        return missing
    from atlas import mail_commands
    mail_plan = mail_commands.interpret(raw, low, sys.modules[__name__])
    if mail_plan is not None:
        return mail_plan
    from atlas import terminal
    terminal_plan = terminal.interpret(raw, low, sys.modules[__name__])
    if terminal_plan is not None:
        return terminal_plan
    keyboard_plan = _keyboard_lock_plan(low)
    if keyboard_plan is not None:
        return keyboard_plan
    dnd_plan = _dnd_plan(low)
    if dnd_plan is not None:
        return dnd_plan
    from atlas import agent_commands
    command = agent_commands.interpret(raw, low, sys.modules[__name__])
    if command is not None:
        return command
    if re.match(r"^(status|what'?s running|agents|daemons)$", low):
        return Plan("agents", "show the status of all background agents",
                    lambda: Outcome(run_manage("status")), looks_only=True)

    m = re.match(r"^(?:install|brew install|get me|download)\s+(.+)$", raw, re.IGNORECASE)
    if m:
        return _install_plan(m.group(1))

    m = re.match(r"^(restart|reload|stop|kill|disable|start|enable)\s+(.+)$", low)
    if m:
        plan = _agent_plan(*m.groups())
        if plan is not None:
            return plan
        if m.group(1) in ("start", "enable"):
            return _open_plan(m.group(2))
        return _error(f"don't know an agent called {m.group(2)}")
    m = re.match(r"^(?:open|launch|go to|goto|cd)\s+(.+)$", raw, re.IGNORECASE)
    if m:
        return _open_plan(m.group(1))
    m = re.match(r"^(?:search (?:inside|in files|in text|text|contents?)(?: for)?|grep|look inside(?: for)?"
                 r"|(?:find |show |list )?(?:files?|things?|stuff) (?:containing|mentioning|about|that mentions?|with the words?))"
                 r"\s+(.+)$", raw, re.IGNORECASE)
    if m:
        from atlas import content_search
        query = m.group(1)
        return Plan("search", f"search inside files for {query}",
                    lambda: Outcome(content_search.search(query, KNOWN_FOLDERS, expand, abbr)),
                    looks_only=True)
    m = re.match(r"^(?:find|search(?: for)?|locate|look for|where is|where'?s|where did i (?:put|save|leave))\s+(.+)$",
                 raw, re.IGNORECASE)
    if m:
        return _find_plan(m.group(1))
    # "do i have a firefox dmg anywhere", "is there a resume on here": a question about files only
    # when it says where ("do i have time" is not one).
    m = re.match(r"^(?:do i (?:still )?have|have i got|is there|are there)\s+(?:a |an |any |the |my |some )?(.+?)\s+"
                 r"(?:anywhere|somewhere|around|on here|on this (?:mac|machine|computer|laptop)|lying around)[ ?]*$",
                 raw, re.IGNORECASE)
    if m:
        return _find_plan(m.group(1))
    m = re.match(r"^(?:i'?m looking for|looking for)\s+(?:a |an |any |the |my |some )?(.+?)[ ?]*$", raw, re.IGNORECASE)
    if m:
        return _find_plan(m.group(1))
    m = re.match(r"^(?:ls|list|dir|what'?s in|whats in|what is in|contents of)\s+(.+)$", raw, re.IGNORECASE)
    if m:
        return _list_plan(m.group(1))
    m = re.match(r"^(?:read|cat|view|show|head|peek at)\s+(.+)$", raw, re.IGNORECASE)
    if m:
        return _read_plan(m.group(1))

    # A bare one- or two-word name that is clearly an app: just open it.
    if len(raw.split()) <= 2:
        apps = find_apps(raw, name_only=True)
        if apps:
            return _choose_to_open(apps, f"open the app {os.path.basename(apps[0])[:-4]}" if len(apps) == 1
                                   else f"open one of {len(apps)} apps matching {raw}")
    return _miss(raw)
