"""The public lists, fetched once a week into the state folder (dataflow-lists/), and the compiled
index entities.py reads: dataflow-index.json, {domain: [owner, categories, broker]}.

    DuckDuckGo Tracker Radar (the extension build, CC BY-NC-SA 4.0): domain -> owner, categories,
        and known CNAME-cloaked trackers
    Disconnect's entity and services lists (GPL-3.0): more owners; its Email category is email platforms
    EasyPrivacy (GPL-3.0 / CC BY-SA 3.0): generic tracking paths, to flag a site's own subdomains
        that act as a tracker (an extension can't resolve CNAMEs)
    The Public Suffix List (lib/psl.py keeps it)
    data/platforms.yaml (by hand) and the broker registry (brokers/registry, when the broker agent
        is installed)
"""
import json
import os
import re
import time

import yaml

from dataflow import DATA_DIR
from lib import psl
from lib.common import state_path

LIST_DIR = state_path("dataflow-lists")
INDEX = state_path("dataflow-index.json")
FILES = {"tracker_radar": "tds.json", "disconnect_entities": "disconnect-entities.json",
         "disconnect_services": "disconnect-services.json", "easyprivacy": "easyprivacy.txt"}
# Tracker Radar's categories -> ours. Unlisted ones (Badge, Malware…) are dropped.
TDS_CATEGORIES = {
    "Session Replay": "session-replay", "Advertising": "advertising", "Ad Motivated Tracking": "advertising",
    "Action Pixels": "advertising", "Ad Fraud": "advertising", "Analytics": "analytics",
    "Audience Measurement": "analytics", "Third-Party Analytics Marketing": "analytics", "Tag Manager": "tag-manager",
    "Online Payment": "payment", "Consent Management Platform": "consent", "Social - Share": "social",
    "Social - Comment": "social", "Social Network": "social", "Federated Login": "login", "SSO": "login",
    "Embedded Content": "embedded", "Content Delivery": "cdn", "CDN": "cdn", "Support Chat Widget": "chat",
    "Fraud Prevention": "fraud-prevention", "Unknown High Risk Behavior": "fingerprinting",
}
DISCONNECT_CATEGORIES = {"Advertising": "advertising", "Analytics": "analytics",
                         "FingerprintingInvasive": "fingerprinting",
                         "Social": "social", "ConsentManagers": "consent", "Anti-fraud": "fraud-prevention"}
_SUFFIXES = {"inc", "llc", "ltd", "limited", "corp", "corporation", "co", "company", "holdings", "holding", "group",
             "gmbh", "plc", "sa", "ag", "bv", "sas", "srl", "pty", "lp", "llp", "the"}


def path(name):
    os.makedirs(LIST_DIR, exist_ok=True)
    return os.path.join(LIST_DIR, FILES[name])


def config():
    from lib import settings
    return settings.load("dataflow", os.path.join(os.path.dirname(DATA_DIR), "config.json"))


def platforms():
    with open(os.path.join(DATA_DIR, "platforms.yaml"), encoding="utf-8") as f:
        return yaml.safe_load(f)


def age_days():
    try:
        return (time.time() - os.path.getmtime(INDEX)) / 86400
    except OSError:
        return None


def due():
    age = age_days()
    return age is None or age >= config()["lists_days"]


def refresh(force=False, log=None):
    """Download what is a week old (or everything with force), then rebuild the index. Returns a summary."""
    import requests

    cfg = config()
    out = {}
    for name, url in cfg["lists"].items():
        target = path(name)
        try:
            if not force and time.time() - os.path.getmtime(target) < cfg["lists_days"] * 86400:
                out[name] = "fresh"
                continue
        except OSError:
            pass
        try:
            response = requests.get(url, timeout=60, headers={"User-Agent": "MacUtils dataflow list refresh"})
            response.raise_for_status()
            if name != "easyprivacy":
                json.loads(response.text)            # a truncated download keeps the old copy
            with open(target + ".tmp", "w", encoding="utf-8") as f:
                f.write(response.text)
            os.replace(target + ".tmp", target)
            out[name] = f"{len(response.content) // 1024} KB"
        except Exception as error:
            out[name] = f"failed: {type(error).__name__}"
            if log:
                log.warning("list %s: %s", name, error)
    out["psl"] = "fresh" if psl.refresh(force) else "failed"
    out["index"] = build()
    return out


def normalize(name):
    """A company name for matching: lower case, no punctuation, no Inc/LLC, nothing in brackets."""
    name = re.sub(r"\([^)]*\)", " ", str(name or "").lower())
    words = re.sub(r"[^a-z0-9]+", " ", name.replace("&", " and ")).split()
    while words and words[-1] in _SUFFIXES:
        words.pop()
    while words and words[0] == "the":
        words.pop(0)
    return " ".join(words)


def _load_json(name):
    try:
        with open(path(name), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def easyprivacy_paths(limit=3000):
    """Generic EasyPrivacy rules that name a path, not a host: '/collect?', '/pixel.gif'…"""
    out = []
    try:
        with open(path("easyprivacy"), encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line[0] in "!@[|" or "##" in line or "#@#" in line or "#?#" in line:
                    continue
                rule = line.split("$", 1)[0]
                if (rule.startswith("/") or rule.startswith("_") or rule.startswith(".")) and len(rule) >= 6 and \
                        re.fullmatch(r"[\w./?=&-]+", rule):
                    out.append(rule.lower())
                if len(out) >= limit:
                    break
    except OSError:
        pass
    return out


def build():
    """The compiled index: every domain the lists know, its owner, our categories, and its broker."""
    from lib import components

    tds = _load_json("tracker_radar")
    entities = _load_json("disconnect_entities").get("entities", {})
    services = _load_json("disconnect_services").get("categories", {})
    hand = platforms()
    owners, categories = {}, {}

    def add(domain, category):
        if domain and category:
            categories.setdefault(domain, set()).add(category)

    for domain, owner in (tds.get("domains") or {}).items():
        owners[domain] = owner
    display = {name: info.get("displayName") or name for name, info in (tds.get("entities") or {}).items()}
    for domain, tracker in (tds.get("trackers") or {}).items():
        owner = (tracker.get("owner") or {}).get("name")
        if owner:
            owners.setdefault(domain, owner)
            display.setdefault(owner, (tracker.get("owner") or {}).get("displayName") or owner)
        for category in tracker.get("categories") or []:
            add(domain, TDS_CATEGORIES.get(category))
    for name, info in entities.items():
        for domain in set(info.get("properties", []) + info.get("resources", [])):
            owners.setdefault(psl.registrable_domain(domain), name)
    for category, groups in services.items():
        ours = DISCONNECT_CATEGORIES.get(category)
        for group in groups:
            for company, sites in group.items():
                for _site, domains in sites.items():
                    if not isinstance(domains, list):
                        continue
                    for domain in domains:
                        reg = psl.registrable_domain(domain)
                        owners.setdefault(reg, company)
                        add(reg, ours)
    for category, domains in (hand.get("categories") or {}).items():
        for domain in domains:
            add(domain, category)

    # Data brokers, when the broker agent is installed: a domain in its registry, or a domain whose
    # owner is a registry broker.
    entries, by_domain, by_name, shared = {}, {}, {}, (lambda domain: False)
    if components.installed("brokers"):
        from brokers import registry
        entries, by_domain, shared = registry.load(fresh=True), registry.by_domain(), registry.shared
        for broker_id, entry in entries.items():
            if registry.get(entry, "status") == "defunct":
                continue
            for name in (entry.get("name"), entry.get("parent_company")):
                key = normalize(name) if name and not registry.is_todo(name) else ""
                if len(key) >= 3:
                    by_name.setdefault(key, broker_id)
    aliases = hand.get("entity_aliases") or {}
    owner_broker = {}
    for owner in set(owners.values()):
        key = normalize(owner)
        broker_id = aliases.get(key) or by_name.get(key)
        if broker_id in entries:
            owner_broker[owner] = broker_id
    index = {}
    for domain in set(owners) | set(categories) | set(by_domain):
        if not domain or shared(domain):
            owner_domain_broker = None
        else:
            owner_domain_broker = by_domain.get(domain)
        owner = owners.get(domain)
        broker_id = owner_domain_broker or owner_broker.get(owner)
        cats = set(categories.get(domain, ()))
        if broker_id:
            cats.add("data-broker")
            if not owner:
                owner = entries[broker_id].get("name")
        index[domain] = [display.get(owner, owner) if owner else None, sorted(cats), broker_id]
    out = {"built": time.time(), "domains": index, "cnames": tds.get("cnames") or {},
           "paths": easyprivacy_paths(), "brokers": {b: entries[b].get("name") for b in
                                                      {v[2] for v in index.values() if v[2]}}}
    with open(INDEX + ".tmp", "w", encoding="utf-8") as f:
        json.dump(out, f, separators=(",", ":"))
    os.replace(INDEX + ".tmp", INDEX)
    return {"domains": len(index), "brokers": len(out["brokers"]), "owners_matched_to_brokers": len(owner_broker),
            "cnames": len(out["cnames"]), "paths": len(out["paths"])}
