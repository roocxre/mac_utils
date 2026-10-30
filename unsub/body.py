"""A message's text and its unsubscribe links, from Gmail's `full` format.

The text is the text/plain part, or the visible text of the HTML when there is no plain part.
Attachments are skipped. Links come from the HTML when there is one (anchor text says more than
a bare URL), otherwise from URLs in the plain text.
"""
import base64
import re
from html.parser import HTMLParser
from urllib.parse import urlsplit

MAX_CHARS = 256 * 1024
UNSUB_TEXT = re.compile(r"unsubscribe|opt[- ]?out|(?:manage|update|email|subscription)\s+(?:your\s+)?"
                        r"(?:email\s+)?(?:preferences|settings|subscriptions?)", re.IGNORECASE)
_URL = re.compile(r"https://[^\s<>\"')\]]+")
_BLOCK_TAGS = {"p", "div", "br", "tr", "li", "h1", "h2", "h3", "h4", "h5", "h6", "table", "section", "td"}


class _Html(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.text, self.links, self._skip, self._href, self._anchor = [], [], 0, None, []

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style", "head", "title"):
            self._skip += 1
        elif tag == "a":
            self._href, self._anchor = dict(attrs).get("href"), []
        if tag in _BLOCK_TAGS:
            self.text.append("\n")

    def handle_endtag(self, tag):
        if tag in ("script", "style", "head", "title"):
            self._skip = max(0, self._skip - 1)
        elif tag == "a" and self._href:
            self.links.append((self._href.strip(), " ".join("".join(self._anchor).split())))
            self._href = None
        if tag in _BLOCK_TAGS:
            self.text.append("\n")

    def handle_data(self, data):
        if self._skip:
            return
        self.text.append(data)
        if self._href is not None:
            self._anchor.append(data)


def html_text(html):
    """(visible text, [(href, anchor text)]) of an HTML document."""
    parser = _Html()
    try:
        parser.feed(html)
        parser.close()
    except Exception:          # malformed HTML: keep what was read
        pass
    text = re.sub(r"[ \t\r\f\v]+", " ", "".join(parser.text))
    text = re.sub(r"\n\s*\n+", "\n\n", text)
    return text.strip(), parser.links


def _walk(part):
    yield part
    for sub in part.get("parts") or []:
        yield from _walk(sub)


def _charset(part):
    for header in part.get("headers", []):
        if header["name"].lower() == "content-type":
            match = re.search(r'charset="?([\w.-]+)"?', header["value"], re.IGNORECASE)
            if match:
                return match.group(1)
    return "utf-8"


def _decode(part):
    data = part.get("body", {}).get("data")
    if not data:
        return ""
    raw = base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))
    try:
        return raw.decode(_charset(part), errors="replace")
    except LookupError:
        return raw.decode("utf-8", errors="replace")


def extract(payload):
    """(text capped at MAX_CHARS, [(href, anchor text)]) from a Gmail message payload."""
    plain, html = [], []
    for part in _walk(payload or {}):
        if part.get("filename") or part.get("body", {}).get("attachmentId"):
            continue
        mime = part.get("mimeType", "").lower()
        if mime == "text/plain":
            plain.append(_decode(part))
        elif mime == "text/html":
            html.append(_decode(part))
    links = []
    html_body = "\n".join(html)
    if html_body:
        text, links = html_text(html_body)
    if plain:
        text = "\n".join(plain).strip()
    elif not html_body:
        text = ""
    if not links:
        links = [(url, "") for url in _URL.findall(text)]
    return text[:MAX_CHARS], links


def unsubscribe_link(links, text=""):
    """The https link most likely to be the unsubscribe page, or ''."""
    best, best_rank = "", 0
    for href, anchor in links:
        if not href.lower().startswith("https://"):
            continue
        rank = 0
        if re.search(r"unsubscribe", anchor, re.IGNORECASE):
            rank = 4
        elif UNSUB_TEXT.search(anchor):
            rank = 3
        elif re.search(r"unsub", href, re.IGNORECASE):
            rank = 2
        elif re.search(r"opt-?out|preferences|manage", href, re.IGNORECASE):
            rank = 1
        if rank > best_rank:
            best, best_rank = href, rank
    if not best and text:            # plain text: a URL on the line that says unsubscribe
        for line in text.splitlines():
            if UNSUB_TEXT.search(line):
                found = _URL.findall(line)
                if found:
                    return found[0]
    return best


def host(url):
    try:
        return (urlsplit(url).hostname or "").lower()
    except ValueError:
        return ""
