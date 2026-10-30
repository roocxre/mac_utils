r"""Four fixed tools for looking at the mac, each written the same way: a name, then key=value.

    files where=downloads kind=pdf sort=newest top=3        files where=code name=*.py show=count
    read  file="meeting notes" part=first lines=5           read file=app.py from=10 lines=15
    text  find=TODO where=my-project show=count             text find="def \w+" regex=yes where=auth.py around=2
    info  about=battery

They cover the looking-around that otherwise takes the same handful of shell commands over and
over: ls, find, du, tree, grep, head, tail, wc.

They spare you macOS's flags and a file's exact path: the code finds what a loose name like
"meeting notes" or "my-project" refers to, says which ones when there are several, and does the
looking itself. Nothing here changes anything.

Anything these do not cover still goes to the terminal ($ ...).
"""
import fnmatch
import os
import re
import stat
import subprocess
import time

MAX_LINES = 20              # of a listing or of matching lines, before "… and N more"
MAX_READ_LINES = 60
MAX_READ_CHARS = 1800
MAX_TEXT_BYTES = 1024 * 1024    # bigger files are not searched for text
MAX_SEEN = 80000            # entries looked at before giving up on a walk
WALK_SECONDS = 4.0
NAME_DEPTH = 5              # how far below ~ a loose name is looked for

TOOLS = ("files", "read", "text", "info")
KEYS = {
    "files": ("where", "name", "kind", "sort", "top", "show", "newer", "older", "over", "under", "deep"),
    "read": ("file", "where", "part", "from", "lines", "show"),
    "text": ("find", "where", "kind", "show", "regex", "around"),
    "info": ("about", "name"),
}
FIRST_KEY = {"files": "where", "read": "file", "text": "find", "info": "about"}
_CALL = re.compile(r"^(files|read|text|info)\b\s*(.*)$", re.IGNORECASE | re.DOTALL)
_ARG = re.compile(r"""(\w+)\s*=\s*(?:"([^"]*)"|'([^']*)'|(\S+))""")

SKIP_DIRS = {"Library", "node_modules", "__pycache__", "site-packages", "Applications"}
SECRET_NAMES = {".ssh", ".gnupg", ".aws", ".netrc", ".env", "Keychains", "id_rsa", "id_ed25519", "id_ecdsa", "id_dsa",
                ".npmrc", ".pypirc"}
KINDS = {
    "pdf": {"pdf"}, "image": {"png", "jpg", "jpeg", "gif", "webp", "heic", "tiff", "bmp", "svg", "nef", "raw"},
    "video": {"mp4", "mov", "mkv", "m4v", "avi", "webm"}, "audio": {"mp3", "wav", "m4a", "flac", "aac", "ogg"},
    "doc": {"doc", "docx", "pages", "rtf", "odt", "txt", "md", "pdf"},
    "code": {"py", "js", "jsx", "ts", "tsx", "sh", "zsh", "c", "h", "cpp", "java", "go", "rs", "rb", "swift", "r", "sql",
             "html", "css"},
    "archive": {"zip", "tar", "gz", "tgz", "7z", "rar", "bz2"}, "installer": {"dmg", "pkg"},
    "sheet": {"csv", "xlsx", "xls", "numbers"}, "slides": {"pptx", "ppt", "key"}, "text": {"txt", "md", "log", "csv"},
}
_KIND_WORDS = {"pdfs": "pdf", "images": "image", "photo": "image", "photos": "image", "picture": "image",
               "pictures": "image", "videos": "video", "movie": "video", "movies": "video", "recording": "video",
               "recordings": "video", "music": "audio", "song": "audio", "songs": "audio", "docs": "doc",
               "document": "doc", "documents": "doc", "script": "code", "scripts": "code", "archives": "archive",
               "zips": "archive", "installers": "installer", "spreadsheet": "sheet", "spreadsheets": "sheet",
               "presentation": "slides", "screenshot": "image", "screenshots": "image"}
_SPAN = re.compile(r"^(\d+)\s*(h|d|w|m|y)\w*$", re.IGNORECASE)
_SPAN_SECONDS = {"h": 3600, "d": 86400, "w": 7 * 86400, "m": 30 * 86400, "y": 365 * 86400}
_SIZE = re.compile(r"^(\d+(?:\.\d+)?)\s*(b|kb?|mb?|gb?)$", re.IGNORECASE)
_SIZE_BYTES = {"b": 1, "k": 1024, "m": 1024 ** 2, "g": 1024 ** 3}


class Problem(Exception):
    """Something to say back in place of a result: a name that fits nothing, or several things."""


def home():
    return os.path.expanduser("~")


def abbr(path):
    base = home()
    return "~" + path[len(base):] if path == base or path.startswith(base + os.sep) else path


def size_text(count):
    for unit, step in (("GB", 1024 ** 3), ("MB", 1024 ** 2), ("KB", 1024)):
        if count >= step:
            value = count / step
            return f"{value:.1f} {unit}" if value < 10 and unit != "KB" else f"{value:.0f} {unit}"
    return f"{count} bytes"


def when_text(stamp):
    return time.strftime("%b %-d %Y %H:%M", time.localtime(stamp))


def _run(command, timeout=15):
    try:
        done = subprocess.run(["/bin/zsh", "-c", command], capture_output=True, text=True, timeout=timeout,
                              stdin=subprocess.DEVNULL, cwd=home())
        return (done.stdout or "").strip()
    except (OSError, subprocess.TimeoutExpired):
        return ""


# ------------------------------------------------------------ finding what a name means

def walk(root, depth, everything=False):
    """Everything under root down to `depth` levels, as (path, is a folder, level).

    Skips hidden things and the folders nobody means (Library, node_modules) unless `everything`.
    """
    started, seen, queue = time.monotonic(), 0, [(root, 0)]
    while queue:
        folder, level = queue.pop(0)
        try:
            entries = sorted(os.scandir(folder), key=lambda e: e.name.lower())
        except OSError:
            continue
        for entry in entries:
            if entry.name.startswith(".") or (not everything and (
                    entry.name in SKIP_DIRS or entry.name.endswith((".app", ".noindex")))):
                continue
            seen += 1
            if seen > MAX_SEEN or time.monotonic() - started > WALK_SECONDS:
                return
            try:
                is_dir = entry.is_dir(follow_symlinks=False)
            except OSError:
                continue
            yield entry.path, is_dir, level + 1
            if is_dir and level + 1 < depth:
                queue.append((entry.path, level + 1))


def _true_case(base, name):
    """base/name if it exists, spelled the way the disk spells it (the disk itself ignores case)."""
    path = base
    for part in [p for p in re.split(r"[/\\]+", name) if p]:
        try:
            match = next((e for e in os.listdir(path) if e.lower() == part.lower()), None)
        except OSError:
            return None
        if match is None:
            return None
        path = os.path.join(path, match)
    return path


def resolve(name, want="any", under=None):
    """The path a loosely written name stands for. want: "dir", "file" or "any". Raises Problem."""
    name = (name or "").strip().strip("\"'")
    base = under or home()
    if name.lower() in ("", "~", "home", "home folder", "my home"):
        return base
    if name.startswith(("~", "/")):
        path = os.path.expanduser(name)
        if os.path.exists(path):
            return path
        name = name.lstrip("~/")
    if name.lower() in ("applications", "apps") and not under:
        return "/Applications"
    direct = _true_case(base, name)
    if direct:
        return direct
    *before, last = [part for part in re.split(r"[/\\]+", name.lower()) if part]
    words = last.split()
    exact, close = [], []
    for path, is_dir, level in walk(base, NAME_DEPTH):
        if (want == "dir" and not is_dir) or (want == "file" and is_dir):
            continue
        leaf = os.path.basename(path).lower()
        if not all(word in leaf for word in words):
            continue
        if before and not all(part in path.lower() for part in before):
            continue
        (exact if leaf == last or os.path.splitext(leaf)[0] == last else close).append((level, len(leaf), path))
    found = sorted(exact) or sorted(close)
    kind = {"dir": "folder", "file": "file"}.get(want, "file or folder")
    if not found:
        tops = sorted((e.name for e in os.scandir(base) if not e.name.startswith(".")), key=str.lower)[:40]
        raise Problem(f"no {kind} called {name} under {abbr(base)}. {abbr(base)} holds: {', '.join(tops)}")
    if len(found) > 1 and not (exact and len(exact) == 1) and found[0][0] == found[1][0]:
        listed = ", ".join(abbr(p) for _l, _n, p in found[:6])
        raise Problem(f"several things are called {name}: {listed}{' and more' if len(found) > 6 else ''}. which one?")
    return found[0][2]


# ------------------------------------------------------------ files

def _kind(value):
    value = value.lower().lstrip(".")
    value = _KIND_WORDS.get(value, value)
    return KINDS.get(value) or KINDS.get(value.rstrip("s")) or {value}


def _span(value, key):
    match = _SPAN.match(value.strip())
    if not match:
        raise Problem(f"{key}={value} is not a length of time. write it like 7d, 2w, 3m or 12h")
    return int(match.group(1)) * _SPAN_SECONDS[match.group(2).lower()]


def _bytes(value, key):
    match = _SIZE.match(value.strip())
    if not match:
        raise Problem(f"{key}={value} is not a size. write it like 10mb, 500kb or 1gb")
    return float(match.group(1)) * _SIZE_BYTES[match.group(2)[0].lower()]


def files(args):
    where = resolve(args.get("where", "~"))
    show = args.get("show", "list").lower()
    sort = args.get("sort", "").lower()
    shows, sorts = ("list", "count", "size", "kinds", "tree"), ("newest", "oldest", "biggest", "smallest", "name", "added")
    # A value given to the wrong key (sort=tree) is said back with the right one, so one more try gets it.
    if show not in shows:
        raise Problem(f"show={show} is not known. it is list, count, size, kinds or tree"
                      + (f". did you mean sort={show}" if show in sorts else ""))
    if sort and sort not in sorts:
        raise Problem(f"sort={sort} is not known. it is newest, oldest, biggest, smallest, name or added"
                      + (f". did you mean show={sort}" if sort in shows else ""))
    if args.get("kind", "").lower().rstrip("s") in ("repo", "repositorie", "git", "git repo"):
        return _repos(where)
    filters = [key for key in ("name", "kind", "newer", "older", "over", "under") if key in args]
    if os.path.isfile(where):
        info = os.stat(where)
        return f"{abbr(where)} — {size_text(info.st_size)} — changed {when_text(info.st_mtime)}"
    if show == "tree":
        return _tree(where, args)
    deep = args.get("deep", "yes" if filters or show == "kinds" or sort in ("biggest", "smallest") else "no").lower() \
        in ("yes", "true", "1")
    if show == "size" and not filters:
        total = _run(f"du -sk {_quote(where)} 2>/dev/null | cut -f1")
        return f"{abbr(where)} is {size_text(int(total) * 1024)}" if total.isdigit() else f"could not size {abbr(where)}"

    pattern = args.get("name", "").lower()
    name = [] if any(mark in pattern for mark in "*?[") else pattern.split()      # *.py is a pattern; anything else, words
    kinds = _kind(args["kind"]) if "kind" in args else None
    now = time.time()
    newer = now - _span(args["newer"], "newer") if "newer" in args else None
    older = now - _span(args["older"], "older") if "older" in args else None
    over = _bytes(args["over"], "over") if "over" in args else None
    under = _bytes(args["under"], "under") if "under" in args else None
    rows = []
    for path, is_dir, _level in walk(where, 12 if deep else 1, everything=not deep):
        leaf = os.path.basename(path)
        if is_dir:
            if not deep and not filters and show == "list":
                rows.append((path, None, os.stat(path).st_mtime))
            elif filters == ["name"] and show == "list" and (
                    all(word in leaf.lower() for word in name) if name else fnmatch.fnmatch(leaf.lower(), pattern)):
                rows.append((path, None, os.stat(path).st_mtime))      # name= finds folders too: it is how a path is found
            continue
        if name and not all(word in leaf.lower() for word in name):
            continue
        if pattern and not name and not fnmatch.fnmatch(leaf.lower(), pattern):
            continue
        if kinds and os.path.splitext(leaf)[1].lower().lstrip(".") not in kinds:
            continue
        try:
            info = os.stat(path)
        except OSError:
            continue
        if (newer and info.st_mtime < newer) or (older and info.st_mtime > older) \
                or (over and info.st_size < over) or (under and info.st_size > under):
            continue
        rows.append((path, info.st_size, info.st_mtime))

    what = " ".join(filter(None, [
        args.get("kind", ""), "files" if filters else "", f"named {args['name']}" if pattern else "",
        f"changed in the last {args['newer']}" if newer else "", f"untouched for {args['older']}" if older else "",
        f"over {args['over']}" if over else "", f"under {args['under']}" if under else ""])).strip()
    place = abbr(where) + (" and its subfolders" if deep else "")
    only_files = [row for row in rows if row[1] is not None]
    if show == "count":
        return f"{len(only_files)} {what or 'files'} in {place}"
    if show == "size":
        return f"{size_text(sum(row[1] for row in only_files))} in {len(only_files)} {what or 'files'} in {place}"
    if show == "kinds":
        counts = {}
        for path, size, _changed in only_files:
            kind = os.path.splitext(path)[1].lower().lstrip(".") or "(no extension)"
            counts[kind] = (counts.get(kind, (0, 0))[0] + 1, counts.get(kind, (0, 0))[1] + size)
        ranked = sorted(counts.items(), key=lambda item: -item[1][0])
        return f"{len(only_files)} {what or 'files'} in {place}, by kind:\n" + "\n".join(
            f"{kind} — {count} — {size_text(total)}" for kind, (count, total) in ranked[:MAX_LINES]) + (
            f"\n… and {len(ranked) - MAX_LINES} more kinds" if len(ranked) > MAX_LINES else "")
    if not rows:
        return f"nothing found: no {what or 'files'} in {place}"
    order = sort or ("newest" if deep else "name")
    if order == "added":
        # When it arrived here, not when it last changed: a folder changes every time something in it does.
        rows = [(path, size, _born(path)) for path, size, _changed in rows]
    rows.sort(key={"newest": lambda r: -r[2], "oldest": lambda r: r[2], "biggest": lambda r: -(r[1] or 0), "added": lambda r: -r[2],
                   "smallest": lambda r: r[1] or 0, "name": lambda r: os.path.basename(r[0]).lower()}[order])
    try:
        top = max(1, min(int(args.get("top", MAX_LINES)), 50))
    except ValueError:
        raise Problem(f"top={args['top']} is not a number")
    if filters == ["name"]:
        what = f"{'thing' if len(rows) == 1 else 'things'} named {args['name']}"
    lines = [f"{len(rows)} {what or 'items'} in {place}" + (
        ", newest added first" if order == "added" else f", {order} first" if order != "name" else "") + ":"]
    for path, size, changed in rows[:top]:
        shown = os.path.relpath(path, where)
        stamp = f"{'added' if order == 'added' else 'changed'} {when_text(changed)}"
        lines.append(f"{shown}/ — {stamp}" if size is None else f"{shown} — {size_text(size)} — {stamp}")
    if len(rows) > top:
        lines.append(f"… and {len(rows) - top} more")
    return "\n".join(lines)


def _repos(where):
    """The git repositories under a folder: every folder that holds a .git, a few levels down."""
    found = []
    if os.path.isdir(os.path.join(where, ".git")):
        found.append(where)
    for path, is_dir, _level in walk(where, 4):
        if is_dir and os.path.isdir(os.path.join(path, ".git")):
            found.append(path)
    place = abbr(where)
    if not found:
        return f"nothing found: no git repositories in {place} or its subfolders"
    lines = [f"{len(found)} git repositor{'y' if len(found) == 1 else 'ies'} in {place} and its subfolders:"]
    for path in found[:MAX_LINES]:
        head = _run(f"git -C {_quote(path)} branch --show-current 2>/dev/null")
        lines.append(f"{abbr(path)}/" + (f" — on {head}" if head else ""))
    return "\n".join(lines + ([f"… and {len(found) - MAX_LINES} more"] if len(found) > MAX_LINES else []))


def _born(path):
    try:
        info = os.stat(path)
        return getattr(info, "st_birthtime", info.st_mtime)
    except OSError:
        return 0


def _tree(where, args):
    """The folders and files under a folder, indented, a few levels down."""
    try:
        depth = max(1, min(int(args.get("deep", 2)) if args.get("deep", "2").isdigit() else 2, 4))
    except ValueError:
        depth = 2
    lines, shown = [abbr(where) + "/"], 0

    def visit(folder, level):
        nonlocal shown
        try:
            entries = sorted(os.scandir(folder), key=lambda e: (not e.is_dir(follow_symlinks=False), e.name.lower()))
        except OSError:
            return
        entries = [e for e in entries if not e.name.startswith(".") and e.name not in SKIP_DIRS]
        for index, entry in enumerate(entries):
            if shown >= 2 * MAX_LINES:
                lines.append("  " * level + f"… and {len(entries) - index} more here")
                return
            is_dir = entry.is_dir(follow_symlinks=False)
            lines.append("  " * level + entry.name + ("/" if is_dir else ""))
            shown += 1
            if is_dir and level < depth:
                visit(entry.path, level + 1)

    visit(where, 1)
    return "\n".join(lines)


def _quote(path):
    return "'" + path.replace("'", "'\\''") + "'"


# ------------------------------------------------------------ read and text

def _readable(path):
    """Why a file's contents won't be shown, or None."""
    if set(path.split(os.sep)) & SECRET_NAMES or os.path.basename(path).startswith(".env"):
        return "not reading that one"
    try:
        mode = os.stat(path).st_mode
    except OSError as error:
        return f"can't read {abbr(path)}: {error.strerror or error}"
    if not stat.S_ISREG(mode):
        return f"{abbr(path)} is not a file"
    if not mode & 0o044:
        return f"{abbr(path)} is private (readable only by you), so it is not shown here"
    return None


def _text_of(path, limit=None):
    with open(path, "rb") as handle:
        data = handle.read(limit) if limit else handle.read()
    return None if b"\x00" in data[:4096] else data.decode("utf-8", errors="replace")


def read(args):
    if "file" not in args:
        raise Problem("read needs file=, the name of the file")
    under = resolve(args["where"], "dir") if "where" in args else None
    path = resolve(args["file"], "file", under)
    reason = _readable(path)
    if reason:
        return reason
    text = _text_of(path, 4 * MAX_TEXT_BYTES)
    if text is None:
        info = os.stat(path)
        return f"{abbr(path)} is not text ({size_text(info.st_size)}, changed {when_text(info.st_mtime)}), so there is nothing to read"
    rows = text.splitlines()
    show = args.get("show", "text").lower()
    if show in ("lines", "words", "count", "size"):
        return f"{abbr(path)}: {len(rows)} lines, {len(text.split())} words, {size_text(os.path.getsize(path))}"
    part = args.get("part", "all").lower()
    try:
        count = max(1, min(int(args.get("lines", MAX_READ_LINES)), MAX_READ_LINES))
    except ValueError:
        raise Problem(f"lines={args['lines']} is not a number")
    if part not in ("all", "first", "last"):
        raise Problem(f"part={part} is not known. it is all, first or last")
    if "from" in args:
        if not args["from"].isdigit() or int(args["from"]) < 1:
            raise Problem(f"from={args['from']} is not a line number")
        start = int(args["from"])
        chosen = rows[start - 1:start - 1 + count]
        body = "\n".join(f"{start + offset}: {line}" for offset, line in enumerate(chosen))[:MAX_READ_CHARS]
        return f"{abbr(path)}, lines {start} to {start + len(chosen) - 1} of {len(rows)}\n\n{body}"
    chosen = rows[-count:] if part == "last" else rows[:count]
    body = "\n".join(chosen)[:MAX_READ_CHARS]
    left = len(rows) - len(chosen)
    note = f"\n… ({left} more lines)" if left > 0 and part != "last" else (f"\n(the last {len(chosen)} of {len(rows)} lines)" if left > 0 else "")
    return f"{abbr(path)}\n\n{body}{note}"


def text(args):
    if "find" not in args:
        raise Problem("text needs find=, the words to look for")
    try:
        matcher = re.compile(args["find"] if args.get("regex", "no").lower() in ("yes", "true", "1")
                             else re.escape(args["find"]), re.IGNORECASE)
        around = max(0, min(int(args.get("around", 0)), 5))
    except (re.error, ValueError) as error:
        raise Problem(f"could not use that: {error}")
    where = resolve(args.get("where", "~"))
    show = args.get("show", "lines").lower()
    if show not in ("lines", "count", "files"):
        raise Problem(f"show={show} is not known. it is lines, count or files")
    kinds = _kind(args["kind"]) if "kind" in args else None
    paths = [where] if os.path.isfile(where) else (p for p, is_dir, _l in walk(where, 12) if not is_dir)
    hits, per_file = [], {}
    for path in paths:
        if kinds and os.path.splitext(path)[1].lower().lstrip(".") not in kinds:
            continue
        try:
            if os.path.getsize(path) > MAX_TEXT_BYTES or _readable(path):
                continue
            content = _text_of(path)
        except OSError:
            continue
        if content is None:
            continue
        rows = content.splitlines()
        for number, line in enumerate(rows, 1):
            if matcher.search(line):
                near = rows[max(0, number - 1 - around):number + around] if around else [line]
                hits.append((path, number, " ⏎ ".join(row.strip()[:160] for row in near)))
                per_file[path] = per_file.get(path, 0) + 1
    place = abbr(where)
    if not hits:
        return f"nothing found: no line in {place} mentions {args['find']}"
    summary = f"{len(hits)} lines mention {args['find']} in {len(per_file)} file{'s' if len(per_file) != 1 else ''} under {place}"
    base = where if os.path.isdir(where) else os.path.dirname(where)
    if show == "count":
        return summary + ": " + ", ".join(f"{os.path.relpath(p, base)} ({n})" for p, n in list(per_file.items())[:8])
    if show == "files":
        return summary + ":\n" + "\n".join(f"{os.path.relpath(p, base)} ({n})" for p, n in list(per_file.items())[:MAX_LINES])
    lines = [summary + ":"] + [f"{os.path.relpath(p, base)}:{n}: {line}" for p, n, line in hits[:MAX_LINES]]
    if len(hits) > MAX_LINES:
        lines.append(f"… and {len(hits) - MAX_LINES} more")
    return "\n".join(lines)


# ------------------------------------------------------------ info

def _top(column, flag, count=5):
    rows = _run(f"ps -Aceo {column},comm {flag} | head -{count + 1}").splitlines()[1:]
    return [row.strip().split(None, 1) for row in rows if len(row.strip().split(None, 1)) == 2]


def _battery(_args):
    now = _run("pmset -g batt | tail -1 | cut -f2")
    health = " ".join(_run("system_profiler SPPowerDataType | grep -E 'Cycle Count|Condition|Maximum Capacity'").split())
    return f"battery: {now}\n{health}" if health else f"battery: {now}"


def _disk(_args):
    return "disk: " + _run("df -h ~ | awk 'NR==2 {print $4 \" free of \" $2 \" (\" $5 \" used)\"}'")


def _memory(_args):
    total = _run("sysctl -n hw.memsize")
    lines = [f"memory: {int(total) // 1024 ** 3} GB installed" if total.isdigit() else "memory:",
             _run("memory_pressure | tail -1"), "swap: " + _run("sysctl -n vm.swapusage"), "using the most:"]
    return "\n".join(lines + [f"{name} — {int(kb) // 1024} MB" for kb, name in _top("rss", "-m") if kb.isdigit()])


def _cpu(_args):
    lines = [f"cpu: {_run('sysctl -n machdep.cpu.brand_string')}, {_run('sysctl -n hw.ncpu')} cores", "busiest now:"]
    return "\n".join(lines + [f"{name} — {share}%" for share, name in _top("pcpu", "-r")])


def _uptime(_args):
    return f"uptime: {_run('uptime')}\nlast started: {' '.join(_run('last reboot | head -1').split()[2:])}"


def _network(_args):
    return "\n".join([f"address: {_run('ipconfig getifaddr en0') or 'none on wi-fi'}",
                      "router: " + _run("route -n get default 2>/dev/null | awk '/gateway/ {print $2}'"),
                      "dns: " + ", ".join(sorted(set(_run("scutil --dns | awk '/nameserver/ {print $3}'").split()))),
                      "wi-fi name: macOS does not report it"])


def _ports(_args):
    rows = _run("lsof -nP -iTCP -sTCP:LISTEN | awk 'NR>1 {print $1, $9}' | sort -u").replace("\\x20", " ").splitlines()
    return "listening:\n" + "\n".join(rows) if rows else "nothing is listening"


def _processes(_args):
    count = _run("ps -A | tail -n +2 | wc -l").strip()
    return "\n".join([f"{count} processes running", "busiest now:"] + [f"{name} — {share}%" for share, name in _top("pcpu", "-r")])


def _system(_args):
    total = _run("sysctl -n hw.memsize")
    return "\n".join([f"macOS {_run('sw_vers -productVersion')}", f"model: {_run('sysctl -n hw.model')}",
                      f"chip: {_run('sysctl -n machdep.cpu.brand_string')}, {_run('sysctl -n hw.ncpu')} cores",
                      f"memory: {int(total) // 1024 ** 3} GB" if total.isdigit() else "memory: unknown",
                      "screen: " + " ".join(_run("system_profiler SPDisplaysDataType | grep Resolution").split()[1:])])


def _time(_args):
    return _run("date '+%A %-d %B %Y, %H:%M %Z, week %V'")


def _apps(args):
    names = sorted(n[:-4] for n in os.listdir("/Applications") if n.endswith(".app"))
    wanted = args.get("name", "").lower()
    if not wanted:
        return f"{len(names)} apps in /Applications:\n" + ", ".join(names)
    matches = [n for n in names if wanted in n.lower()]
    open_now = {name.lower() for name in re.findall(r'^\s*\d+\) "([^"]+)"', _run("lsappinfo list"), re.MULTILINE)}
    running = [n for n in matches if n.lower() in open_now]
    return (f"installed: {', '.join(matches) if matches else 'nothing in /Applications matches ' + wanted}\n"
            f"running: {', '.join(running) if running else 'no'}")


ABOUT = {"battery": _battery, "disk": _disk, "memory": _memory, "cpu": _cpu, "uptime": _uptime, "network": _network,
         "ports": _ports, "processes": _processes, "system": _system, "time": _time, "apps": _apps}
_ABOUT_WORDS = {"power": "battery", "storage": "disk", "space": "disk", "ram": "memory", "mem": "memory",
                "processor": "cpu", "chip": "cpu", "wifi": "network", "wi-fi": "network", "ip": "network",
                "internet": "network", "port": "ports", "process": "processes", "mac": "system", "macos": "system",
                "version": "system", "date": "time", "day": "time", "clock": "time", "app": "apps",
                "applications": "apps", "screen": "system", "display": "system", "reboot": "uptime"}


def info(args):
    about = args.get("about", "").lower()
    about = _ABOUT_WORDS.get(about, about)
    if about not in ABOUT:
        raise Problem(f"about={about or '?'} is not known. it is one of: {', '.join(ABOUT)}")
    return ABOUT[about](args)


# ------------------------------------------------------------ the call itself

RUN = {"files": files, "read": read, "text": text, "info": info}
SYNTAX = (
    "files where= name= kind= sort=newest|oldest|biggest|smallest|name|added top= show=list|count|size|kinds|tree newer=7d older=30d over=10mb\n"
    "read file= where= part=first|last from= lines= show=text|count\n"
    "text find= where= kind= show=lines|count|files regex=yes around=2\n"
    "info about=" + "|".join(ABOUT)
)


def parse(raw):
    """(tool, {key: value}) for a tool call, or None if the line is not one. Raises Problem if it is one, badly."""
    match = _CALL.match(raw.strip())
    if not match:
        return None
    tool, rest = match.group(1).lower(), match.group(2)
    args = {m.group(1).lower(): next(g for g in m.groups()[1:] if g is not None) for m in _ARG.finditer(rest)}
    # Words after the last key=value belong to it: name=tax return is name="tax return" without its quotes.
    found = list(_ARG.finditer(rest))
    trailing = rest[found[-1].end():].strip() if found else ""
    if trailing and found[-1].group(4) and "=" not in trailing:
        args[found[-1].group(1).lower()] += " " + trailing
        rest = rest[:found[-1].end()]
    loose = _ARG.sub(" ", rest).strip().strip("\"'")
    if not args and not (tool == "info" and loose):
        return None             # plain words after the name: one of the older commands, or just talk
    if loose:
        args.setdefault(FIRST_KEY[tool], loose)
    unknown = [key for key in args if key not in KEYS[tool]]
    if unknown:
        raise Problem(f"{tool} has no {unknown[0]}=. it takes: {', '.join(k + '=' for k in KEYS[tool])}")
    return tool, args


def call(raw):
    """Carry out a tool call and return what it found, as text."""
    try:
        tool, args = parse(raw)
        return RUN[tool](args)
    except Problem as problem:
        return str(problem)


def interpret(raw, core):
    """A Plan for a tool call, or None."""
    try:
        if parse(raw) is None:
            return None
    except Problem as problem:
        return core._error(str(problem))
    return core.Plan("tool", f"look: {raw}", lambda: core.Outcome(call(raw)), looks_only=True)
