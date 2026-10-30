"""The broker registry: one YAML file per broker in brokers/registry/, and its validator.

A field nobody has checked holds the string TODO_VERIFY. The agent treats it as unknown (the
cautious default: get() below), `validate` lists them, and `validate --strict` fails on any.

    id, name, website, parent_company   brokers with the same parent share one request where
                                        `covers` says the parent honours it for the others
    category        people-search, marketing, b2b, background-check, identity-resolution, other
    summary         what it does and sells, one or two sentences (shown in the report)
    data_categories from its registry filing or privacy policy
    method          email, direct-form, search-form, privacy-manager, manual
    privacy_email, opt_out_url, search_url
    listing_pattern a regex a listing's URL matches (search-form)
    selectors       {"fields": {css: element}, "submit": css, "confirm": regex, "listing": css}
    required_elements  the least it needs to find the record (ELEMENTS)
    email_purpose   identifier (it finds the record by email), contact (only to confirm), unknown
    captcha_likely, id_required, phone_required, account_required, paid   flags for the manual queue
    honors_nonresidents   yes, no, unknown (learned from outcomes)
    recheck_days    default 90
    domains         every domain it uses for sites, mail and scripts (reply matching, the extension's block list)
    priority        crucial, high, normal (the Big-Ass list's tiers)
    sources, last_verified, notes, registry_stats (deletion requests it reported to California)

search_url takes {first} {last} {full_name} {city} {state} (URL-encoded when filled in).
"""
import glob
import os
import re

from brokers import REGISTRY_DIR
from lib import psl

TODO = "TODO_VERIFY"
CATEGORIES = ("people-search", "marketing", "b2b", "background-check", "identity-resolution", "other")
METHODS = ("email", "direct-form", "search-form", "privacy-manager", "manual")
EMAIL_PURPOSES = ("identifier", "contact", "unknown")
ELEMENTS = ("full_name", "first_name", "last_name", "email", "city_state", "city", "state", "zip", "address",
            "phone", "dob", "age", "listing_url")
FLAGS = ("captcha_likely", "id_required", "phone_required", "account_required", "paid")
PRIORITIES = ("crucial", "high", "normal")
FIELDS = ("id", "name", "website", "parent_company", "category", "summary", "data_categories", "method",
          "privacy_email", "opt_out_url", "search_url", "listing_pattern", "selectors", "required_elements",
          "email_purpose") + FLAGS + ("honors_nonresidents", "recheck_days", "domains", "covers", "priority",
                                      "sources", "last_verified", "notes", "registry_stats", "status")
DEFAULTS = {"category": "other", "method": "manual", "email_purpose": "unknown", "honors_nonresidents": "unknown",
            "recheck_days": 90, "priority": "normal", "domains": [], "data_categories": [], "covers": [],
            "required_elements": [], "status": "active", **{flag: False for flag in FLAGS}}
# What a request needs when the entry doesn't say (TODO_VERIFY): the least that finds a record.
FALLBACK_ELEMENTS = {"email": ["full_name", "email", "city_state"], "people-search": ["full_name", "city_state",
                                                                                         "listing_url"]}
SEARCH_KEYS = ("first", "last", "full_name", "city", "state")
# Domains many companies send from or host on: never one broker's (a privacy address on Zendesk, mail
# through SendGrid). Listing one would route everyone's mail there and let its DKIM vouch for a broker.
SHARED_DOMAINS = {"zendesk.com", "freshdesk.com", "helpscout.net", "zohodesk.com", "zoho.com", "gmail.com",
                  "googlemail.com", "google.com", "outlook.com", "hotmail.com", "live.com", "yahoo.com", "aol.com",
                  "icloud.com", "me.com", "protonmail.com", "proton.me", "onetrust.com", "trustarc.com", "osano.com",
                  "salesforce.com", "force.com", "my.site.com", "intercom.io", "typeform.com", "jotform.com",
                  "google-groups.com", "wufoo.com", "formstack.com", "hubspotemail.net", "github.com"}


def shared(domain):
    from mail import headers as hdr
    return domain in SHARED_DOMAINS or domain in hdr.PLATFORMS


_ID = re.compile(r"^[a-z0-9][a-z0-9-]*$")
_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[a-z]{2,}$", re.IGNORECASE)

_cache = {}


def _yaml():
    import yaml            # here, not above: the extension (dataflow/) reads the registry without the brokers extras
    return yaml


def path(broker_id):
    return os.path.join(REGISTRY_DIR, f"{broker_id}.yaml")


def is_todo(value):
    return value == TODO or (isinstance(value, str) and TODO in value)


def get(entry, field, default=None):
    """A field's value, or the default when it is missing or TODO_VERIFY."""
    value = entry.get(field)
    if value is None or is_todo(value):
        return DEFAULTS.get(field, default) if default is None else default
    return value


def todo_fields(entry):
    out = []
    for field, value in entry.items():
        if is_todo(value):
            out.append(field)
        elif isinstance(value, dict) and any(is_todo(v) or is_todo(k) for k, v in _walk(value)):
            out.append(field)
        elif isinstance(value, list) and any(is_todo(v) for v in value):
            out.append(field)
    return out


def _walk(mapping):
    for key, value in mapping.items():
        if isinstance(value, dict):
            yield from _walk(value)
        else:
            yield key, value


def load(fresh=False):
    """{id: entry} for every broker file. Cached per process until a file changes."""
    files = sorted(glob.glob(os.path.join(REGISTRY_DIR, "*.yaml")))
    stamp = (len(files), max((os.path.getmtime(f) for f in files), default=0))
    if not fresh and _cache.get("stamp") == stamp:
        return _cache["entries"]
    entries = {}
    for name in files:
        with open(name, encoding="utf-8") as f:
            entry = _yaml().safe_load(f) or {}
        entries[entry.get("id") or os.path.basename(name)[:-5]] = entry
    _cache.update(stamp=stamp, entries=entries)
    return entries


def broker(broker_id):
    return load().get(broker_id)


def write(entry):
    """Save one entry, fields in the documented order."""
    ordered = {field: entry[field] for field in FIELDS if field in entry}
    ordered.update({k: v for k, v in entry.items() if k not in ordered})
    os.makedirs(REGISTRY_DIR, exist_ok=True)
    with open(path(entry["id"]), "w", encoding="utf-8") as f:
        _yaml().safe_dump(ordered, f, sort_keys=False, allow_unicode=True, width=110)
    _cache.clear()


def update(broker_id, **fields):
    """Change fields of one entry on disk (outcomes: honors_nonresidents, a broken selector…)."""
    entry = dict(load()[broker_id])
    entry.update(fields)
    write(entry)
    return entry


def by_domain():
    """{registrable domain: broker id} over every broker's domains. A domain that is some broker's own
    website belongs to that broker, even when a sister site lists it too."""
    out = {}
    entries = load()
    for broker_id, entry in entries.items():
        own = psl.registrable_domain(entry.get("website") or "")
        if own:
            out.setdefault(own, broker_id)
    for broker_id, entry in entries.items():
        for domain in get(entry, "domains", []):
            if not shared(domain):
                out.setdefault(domain, broker_id)
    return out


def required_elements(entry):
    """The details a request to this broker sends: its required_elements, or the cautious fallback."""
    found = get(entry, "required_elements", [])
    if found:
        return list(found)
    if get(entry, "category") == "people-search" and get(entry, "method") != "email":
        return list(FALLBACK_ELEMENTS["people-search"])
    return list(FALLBACK_ELEMENTS["email"])


# ------------------------------------------------------------ the validator

def _problems(entry, file_id, all_ids):
    out = []

    def bad(text):
        out.append(f"{file_id}: {text}")

    if entry.get("id") != file_id:
        bad(f"id {entry.get('id')!r} does not match the file name")
    if not _ID.match(str(entry.get("id", ""))):
        bad("id must be lower case letters, digits and dashes")
    unknown = [k for k in entry if k not in FIELDS]
    if unknown:
        bad(f"unknown field {unknown[0]}")
    for field in ("name", "website"):
        if not entry.get(field):
            bad(f"{field} is missing")

    def check(field, allowed):
        value = entry.get(field)
        if value is not None and not is_todo(value) and value not in allowed:
            bad(f"{field} {value!r} is not one of {', '.join(map(str, allowed))}")

    check("category", CATEGORIES)
    check("method", METHODS)
    check("email_purpose", EMAIL_PURPOSES)
    check("honors_nonresidents", ("yes", "no", "unknown"))
    check("priority", PRIORITIES)
    check("status", ("active", "defunct"))
    for flag in FLAGS:
        check(flag, (True, False))
    days = entry.get("recheck_days")
    if days is not None and not is_todo(days) and (not isinstance(days, int) or days < 7):
        bad("recheck_days is a whole number of days, 7 or more")
    elements = entry.get("required_elements")
    if isinstance(elements, list):
        for element in elements:
            if element not in ELEMENTS:
                bad(f"required_elements: {element!r} is not one of {', '.join(ELEMENTS)}")
    elif elements is not None and not is_todo(elements):
        bad("required_elements is a list")
    email = entry.get("privacy_email")
    if email and not is_todo(email) and not _EMAIL.match(email):
        bad(f"privacy_email {email!r} is not an address")
    for field in ("website", "opt_out_url", "search_url"):
        value = entry.get(field)
        if value and not is_todo(value) and not str(value).startswith("https://") and not str(value).startswith("http://"):
            bad(f"{field} is not a web address")
    domains = entry.get("domains")
    if isinstance(domains, list):
        own = psl.registrable_domain(entry.get("website") or "")
        for domain in domains:
            if psl.registrable_domain(domain) != domain:
                bad(f"domains: {domain!r} is not a registrable domain")
            elif shared(domain) and domain != own:
                bad(f"domains: {domain} is shared by many companies (a mail or form platform), not this broker's")
    elif domains is not None and not is_todo(domains):
        bad("domains is a list")
    pattern = entry.get("listing_pattern")
    if pattern and not is_todo(pattern):
        try:
            re.compile(pattern)
        except re.error as error:
            bad(f"listing_pattern does not compile: {error}")
    search = entry.get("search_url")
    if search and not is_todo(search):
        for key in re.findall(r"{(\w+)}", search):
            if key not in SEARCH_KEYS:
                bad(f"search_url: {{{key}}} is not one of {', '.join(SEARCH_KEYS)}")
    selectors = entry.get("selectors")
    if isinstance(selectors, dict):
        for css, element in (selectors.get("fields") or {}).items():
            if element not in ELEMENTS and not is_todo(element):
                bad(f"selectors: {css} fills {element!r}, which is not one of the elements")
        if selectors.get("confirm") and not is_todo(selectors["confirm"]):
            try:
                re.compile(selectors["confirm"])
            except re.error as error:
                bad(f"selectors.confirm does not compile: {error}")
    method = entry.get("method")
    if not is_todo(method):
        needs = {"email": ("privacy_email",), "direct-form": ("opt_out_url", "selectors"),
                 "search-form": ("search_url", "listing_pattern", "selectors"), "privacy-manager": ("opt_out_url",),
                 "manual": ()}.get(method, ())
        for field in needs:
            if not entry.get(field):
                bad(f"method {method} needs {field}")
    for other in (entry.get("covers") or []) if isinstance(entry.get("covers"), list) else []:
        if other not in all_ids:
            bad(f"covers {other!r}, which is not in the registry")
    return out


def validate(strict=False):
    """{"brokers", "problems": [...], "todo": {field: count}, "ok"}. strict: TODO_VERIFY is a problem too."""
    entries = load(fresh=True)
    problems, todo = [], {}
    names = {}
    for broker_id, entry in entries.items():
        problems += _problems(entry, broker_id, entries)
        for field in todo_fields(entry):
            todo[field] = todo.get(field, 0) + 1
            if strict:
                problems.append(f"{broker_id}: {field} is TODO_VERIFY")
        names.setdefault(str(entry.get("name", "")).lower(), []).append(broker_id)
    for name, ids in names.items():
        if len(ids) > 1:
            problems.append(f"{', '.join(ids)}: the same name {name!r}")
    return {"brokers": len(entries), "problems": problems, "todo": dict(sorted(todo.items(), key=lambda kv: -kv[1])),
            "ok": not problems}
