"""Registrable domains (eTLD+1) from the Public Suffix List.

    registrable_domain("mail.shop.example.co.uk")  -> "example.co.uk"
    registrable_domain("https://news.bbc.co.uk/x") -> "bbc.co.uk"
    host_of("Jane Doe <jane@m.example.com>")       -> "m.example.com"

The list is downloaded to the state folder and refreshed once a week. Without it (first run
offline) the last two labels are used, with a few common two-part suffixes known by heart.
"""
import ipaddress
import os
import re
import time
from urllib.parse import urlsplit

from lib.common import state_path

URL = "https://publicsuffix.org/list/public_suffix_list.dat"
MAX_AGE = 7 * 86400
FALLBACK = {"co.uk", "org.uk", "ac.uk", "gov.uk", "com.au", "net.au", "org.au", "co.jp", "co.nz", "co.za",
            "com.br", "com.mx", "co.in", "co.kr", "com.cn", "com.tw", "com.sg", "com.hk"}

_rules = None   # (normal, wildcard parents, exceptions), loaded once per process


def path():
    return state_path("public_suffix_list.dat")


def refresh(force=False):
    """Download the list if it is missing or a week old. Returns True if the file is now fresh."""
    target = path()
    try:
        if not force and time.time() - os.path.getmtime(target) < MAX_AGE:
            return True
    except OSError:
        pass
    try:
        import requests
        response = requests.get(URL, timeout=20)
        response.raise_for_status()
        if "// ===BEGIN ICANN DOMAINS===" not in response.text:
            return False
        with open(target + ".tmp", "w", encoding="utf-8") as f:
            f.write(response.text)
        os.replace(target + ".tmp", target)
        global _rules
        _rules = None
        return True
    except Exception:
        return False


def _load():
    global _rules
    if _rules is not None:
        return _rules
    refresh()
    normal, wildcard, exception = set(), set(), set()
    try:
        with open(path(), encoding="utf-8") as f:
            for line in f:
                rule = line.strip().split(" ")[0].lower()
                if not rule or rule.startswith("//"):
                    continue
                if rule.startswith("!"):
                    exception.add(rule[1:])
                elif rule.startswith("*."):
                    wildcard.add(rule[2:])
                else:
                    normal.add(rule)
    except OSError:
        normal = set(FALLBACK)
    _rules = (normal, wildcard, exception)
    return _rules


def host_of(text):
    """The host in a URL, an email address (with or without a display name) or a bare domain."""
    text = (text or "").strip().lower()
    if "<" in text and ">" in text:
        text = text[text.rindex("<") + 1:text.rindex(">")]
    if "://" in text:
        text = urlsplit(text).hostname or ""
    elif "@" in text:
        text = text.rsplit("@", 1)[1]
    else:
        text = re.split(r"[/:?#]", text, maxsplit=1)[0]
    return text.strip().strip(".")


def _suffix_length(labels):
    normal, wildcard, exception = _load()
    n = len(labels)
    for i in range(n):  # most labels first, so the first rule that fits is the one that prevails
        candidate = ".".join(labels[i:])
        if candidate in exception:
            return n - i - 1
        if candidate in normal:
            return n - i
        if i + 1 < n and ".".join(labels[i + 1:]) in wildcard:
            return n - i
    return 1


def registrable_domain(text):
    """eTLD+1 of whatever host `text` names; the host itself for an IP address; "" for nothing."""
    host = host_of(text)
    if not host:
        return ""
    try:
        ipaddress.ip_address(host)
        return host
    except ValueError:
        pass
    labels = [label for label in host.split(".") if label]
    suffix = _suffix_length(labels)
    if len(labels) <= suffix:
        return host
    return ".".join(labels[-(suffix + 1):])


def same_site(a, b):
    """Whether two hosts, URLs or addresses share a registrable domain."""
    first, second = registrable_domain(a), registrable_domain(b)
    return bool(first) and first == second
