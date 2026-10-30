"""Finds files by what is in them or what kind they are, for when the name is forgotten.

    search inside lease deposit        files whose text (or name) has both words
    search inside tax pdfs             the same, PDFs only
    search inside videos               the newest videos
    search inside invoice in downloads only under ~/Downloads

`find` matches names; this asks Spotlight, which also indexes the text inside documents and
PDFs. Words must all match; if nothing has them all, files with most of them are shown, and
failing that the newest files of the kind asked for. Newest first, with the matching line
where one can be shown. Only looks, never changes anything.
"""
import os
import re
import subprocess
import time

MAX_RESULTS = 12
MAX_CANDIDATES = 60000
SNIPPET_CHARS = 90
SNIPPET_READ_BYTES = 400_000
SNIPPET_PDF_PAGES = 30

# A word naming a kind of file -> Spotlight content types (any of them).
KINDS = {
    "video": ("public.movie",), "movie": ("public.movie",), "recording": ("public.movie",), "clip": ("public.movie",),
    "pdf": ("com.adobe.pdf",),
    "image": ("public.image",), "photo": ("public.image",), "picture": ("public.image",),
    "screenshot": ("public.image",),
    "audio": ("public.audio",), "song": ("public.audio",), "music": ("public.audio",), "sound": ("public.audio",),
    "document": ("public.composite-content", "public.text"), "doc": ("public.composite-content", "public.text"),
    "note": ("public.text",), "text": ("public.text",),
    "code": ("public.source-code",), "script": ("public.source-code",),
    "spreadsheet": ("public.spreadsheet",), "presentation": ("public.presentation",),
    "slide": ("public.presentation",),
    "archive": ("public.archive",), "zip": ("public.archive",), "installer": ("public.disk-image", "com.apple.installer-package-archive"),
    "folder": ("public.folder",),
}
_FILLER = set("a an and any all my the of about with that which this those these some me i it is are was "
              "file files thing things stuff mention mentions mentioning containing contains contain "
              "says said where find look for from on to".split())
_SKIP = ("/Library/", "/node_modules/", "/.venv/", "/venv/", "/site-packages/", "/.git/", "/.Trash/",
         ".app/", "/__pycache__/", "/.cache/")
# TypeScript sources share the .ts extension with a video format, so Spotlight calls them movies.
_NOT_MEDIA = {".ts", ".tsx", ".mts", ".cts"}
_SECRET_LINE = re.compile(r"token|secret|passw|api[_-]?key|private[_ -]key|bearer|credential", re.IGNORECASE)
_TEXT_EXTENSIONS = {".txt", ".md", ".py", ".js", ".ts", ".json", ".csv", ".html", ".css", ".sh", ".yml",
                    ".yaml", ".toml", ".c", ".h", ".cpp", ".java", ".rs", ".go", ".rb", ".tex", ".log", ".ini",
                    ".cfg", ".xml", ".swift", ".sql", ".rtf", ""}


def _kind_of(word):
    return KINDS.get(word) or (KINDS.get(word[:-1]) if word.endswith("s") else None)


def parse(query, known_folders, expand):
    """(words to look for, content types or None, folder to look under, the kind word used)."""
    folder = os.path.expanduser("~")
    match = re.search(r"\s+(?:in|under|inside)\s+(?:my\s+|the\s+)?([\w ~/.-]+)$", query)
    if match:
        name = match.group(1).strip().lower()
        candidate = expand(known_folders[name]) if name in known_folders else expand(match.group(1).strip())
        if os.path.isdir(candidate):
            folder, query = candidate, query[:match.start()]
    words, kinds, kind_word = [], None, ""
    for word in re.findall(r"[\w.'-]+", query.lower()):
        word = word.strip(".'-")
        if not word or word in _FILLER:
            continue
        found = _kind_of(word)
        if found and kinds is None:
            kinds, kind_word = found, word
        else:
            words.append(word)
    return words, kinds, folder, kind_word


def _quote(word):
    return word.replace("\\", "").replace('"', "").replace("*", "")


def _spotlight(words, kinds, folder):
    parts = [f'(kMDItemTextContent == "{_quote(w)}*"cd || kMDItemFSName == "*{_quote(w)}*"cd)' for w in words]
    if kinds:
        parts.append("(" + " || ".join(f'kMDItemContentTypeTree == "{k}"' for k in kinds) + ")")
    if not parts:
        return []
    try:
        out = subprocess.run(["mdfind", "-onlyin", folder, " && ".join(parts)], capture_output=True, text=True,
                             timeout=20).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    paths = []
    for path in out.splitlines()[:MAX_CANDIDATES]:
        if not path or any(skip in path + "/" for skip in _SKIP) or "/." in path:
            continue
        if kinds and "public.movie" in kinds and os.path.splitext(path)[1].lower() in _NOT_MEDIA:
            continue
        paths.append(path)
    return paths


def _newest(paths):
    dated = []
    for path in paths:
        try:
            dated.append((os.path.getmtime(path), path))
        except OSError:
            pass
    return sorted(dated, reverse=True)


def _snippet(path, words):
    """A line of the file with one of the words in it, or ''.

    Nothing is quoted from a file only you can read (keys, config with passwords), nor a line
    that looks like it holds a secret: what is shown here is put on screen.
    """
    if not words:
        return ""
    try:
        if not os.stat(path).st_mode & 0o044:
            return ""
    except OSError:
        return ""
    ext = os.path.splitext(path)[1].lower()
    text = ""
    try:
        if ext == ".pdf":
            import Quartz
            from Foundation import NSURL

            document = Quartz.PDFDocument.alloc().initWithURL_(NSURL.fileURLWithPath_(path))
            if document is None:
                return ""
            for index in range(min(document.pageCount(), SNIPPET_PDF_PAGES)):
                page = str(document.pageAtIndex_(index).string() or "")
                if any(w in page.lower() for w in words):
                    text = page
                    break
        elif ext in _TEXT_EXTENSIONS and os.path.isfile(path):
            with open(path, "rb") as f:
                text = f.read(SNIPPET_READ_BYTES).decode("utf-8", "replace")
    except Exception:
        return ""
    for line in text.splitlines():
        low = line.lower()
        for word in words:
            at = low.find(word)
            if at != -1 and not _SECRET_LINE.search(line):
                start = max(0, at - SNIPPET_CHARS // 3)
                piece = " ".join(line[start:start + SNIPPET_CHARS].split())
                return ("…" if start else "") + piece
    return ""


def search(query, known_folders, expand, abbr):
    words, kinds, folder, kind_word = parse(query, known_folders, expand)
    if not words and not kinds:
        return "search for what? e.g. `search inside lease deposit` or `search inside videos`"
    note = ""
    paths = _spotlight(words, kinds, folder)
    if not paths and len(words) > 1:
        # Nothing has every word: rank by how many each file has.
        counts = {}
        for word in words:
            for path in _spotlight([word], kinds, folder):
                counts[path] = counts.get(path, 0) + 1
        if counts:
            best = max(counts.values())
            paths = [p for p, n in counts.items() if n == best]
            note = f"nothing has all of {', '.join(words)}; these have {best} of {len(words)}:\n"
    if not paths and kinds and words:
        paths = _spotlight([], kinds, folder)
        if paths:
            one = kind_word[:-1] if kind_word.endswith("s") else kind_word
            note = f"no {one} mentions {', '.join(words)}; the newest are:\n"
    if not paths:
        where = "" if folder == os.path.expanduser("~") else f" under {abbr(folder)}"
        return f"nothing found for {query.strip()}{where}"
    ranked = _newest(paths)
    lines = []
    for modified, path in ranked[:MAX_RESULTS]:
        line = f"{abbr(path)} — {time.strftime('%b %-d %Y', time.localtime(modified))}"
        snippet = _snippet(path, words)
        lines.append(line + (f" — “{snippet}”" if snippet else ""))
    more = f"\n… and {len(ranked) - MAX_RESULTS} more" if len(ranked) > MAX_RESULTS else ""
    return note + "\n".join(lines) + more
