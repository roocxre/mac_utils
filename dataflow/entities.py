"""Entity resolution: a script's or request's URL -> registrable domain (Public Suffix List) -> the
company that owns it -> its categories -> whether that company is in a data-broker registry.

The index is lists.py's dataflow-index.json; the first call builds it if it is missing.
"""
import json
import os
import time

from dataflow import lists
from lib import psl

HIGH = ("identity-resolution", "session-replay", "data-broker")
MEDIUM = ("advertising", "customer-data-platform")
BLOCKED_CATEGORIES = HIGH                  # what the block rule set covers (ordinary loading keeps working)
# A broker-owned domain that runs one of these for the site (HubSpot's forms, Experian's checks) is
# left alone unless it also resolves identities or records sessions: blocking it breaks the page.
FUNCTIONAL = ("email-platform", "payment", "login", "consent", "chat", "fraud-prevention", "cdn", "embedded")

_index = {}


def index():
    try:
        stamp = os.path.getmtime(lists.INDEX)
    except OSError:
        lists.build()
        stamp = os.path.getmtime(lists.INDEX)
    if _index.get("stamp") != stamp:
        with open(lists.INDEX, encoding="utf-8") as f:
            _index.update(json.load(f), stamp=stamp)
        _index["path_rules"] = tuple(_index.get("paths", ()))
    return _index


def resolve(url_or_domain, site=None):
    """{domain, owner, categories, broker, broker_name, first_party, cloaked?} for one URL or host."""
    host = psl.host_of(url_or_domain)
    domain = psl.registrable_domain(host)
    data = index()
    owner, categories, broker = (data["domains"].get(domain) or [None, [], None])
    out = {"domain": domain, "owner": owner, "categories": list(categories), "broker": broker}
    if broker:
        out["broker_name"] = data["brokers"].get(broker)
    if site:
        out["first_party"] = domain == psl.registrable_domain(site)
        if out["first_party"] and host != domain:
            cloaked = _cname(host, data)
            if cloaked:
                out["cloaked"] = cloaked
            elif "://" in str(url_or_domain) and host != "www." + domain and _tracking_path(url_or_domain, data):
                out["cloaked"] = {"why": "a tracking-service path on the site's own subdomain"}
    return out


def _cname(host, data):
    """Tracker Radar's known CNAME-cloaked trackers: a site's subdomain that is really a tracker."""
    target = data["cnames"].get(host)
    if not target:
        return None
    found = resolve(target)
    return {"to": found["domain"], "owner": found["owner"], "categories": found["categories"],
            "broker": found["broker"]}


def _tracking_path(url, data):
    from urllib.parse import urlsplit
    parts = urlsplit(url)
    path = (parts.path + ("?" + parts.query if parts.query else "")).lower()
    return any(rule in path for rule in data["path_rules"])


def risk(categories, first_party=False):
    """high, medium or low for one recipient (the rule the overlay uses too)."""
    if first_party:
        return "low"
    if any(c in HIGH for c in categories):
        return "high"
    if any(c in MEDIUM for c in categories):
        return "medium"
    return "low"


def block_lists(blocklist_path=None):
    """The domains for the extension's block rule set, by category, plus the brokers that refused an
    opt-out (the broker agent's block list, when it is installed)."""
    from lib import components

    brokers = components.installed("brokers")
    if brokers:
        from brokers import registry
    data = index()
    out = {c: [] for c in BLOCKED_CATEGORIES}
    for domain, (_owner, categories, _broker) in data["domains"].items():
        if (brokers and registry.shared(domain)) or not domain or "." not in domain:
            continue
        for category in BLOCKED_CATEGORIES:
            if category in categories:
                if category == "data-broker" and any(c in FUNCTIONAL for c in categories):
                    continue
                out[category].append(domain)
    refused = []
    if brokers:
        try:
            from brokers import work
            with open(blocklist_path or work.BLOCKLIST, encoding="utf-8") as f:
                refused = sorted(d for d in json.load(f).get("domains", {}) if not registry.shared(d))
        except (OSError, ValueError, ImportError):
            pass
    out["refused-opt-out"] = refused
    return {k: sorted(set(v)) for k, v in out.items()}


def age():
    built = index().get("built")
    return round((time.time() - built) / 86400, 1) if built else None
