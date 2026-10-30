"""Builds the broker registry from public lists. `python -m brokers import` (fetches them all).

    cppa      California's data broker registry (cppa.ca.gov, ~600 brokers): name, website, contact
              email, the page on exercising rights, the sensitive data each collects, and how many
              deletion requests it received and denied. Used as a list: DROP doesn't apply to you.
    eraser    github.com/digisamroc/eraser data/brokers.yaml: privacy emails for the long tail
    aidr      github.com/stephenlthorn/auto-identity-remove brokers.js: per-broker method, search
              URL, listing pattern and form selectors
    badbool   github.com/yaelwrites/Big-Ass-Data-Broker-Opt-Out-List: the hand-checked people-search
              list and its tiers (crucial, high priority), ID / phone / payment flags. CC BY-NC-SA.
    state     a CSV you export from Vermont's, Texas's or Oregon's registry (they have no public
              download: Vermont's bulk download needs a login, Texas and Oregon only have search
              pages): `python -m brokers import state=vt file=~/Downloads/vt.csv`. Columns are found
              by name (name, website, email).

Entries are merged by the registrable domain of their website. A field no source states is
TODO_VERIFY. Re-importing never overwrites a value already in a file that isn't TODO_VERIFY, so
checked fields and what the agent learned (honors_nonresidents, a broken selector) stay.
"""
import csv
import io
import re

import yaml

from brokers import registry
from lib import psl

TODO = registry.TODO
URLS = {
    "cppa": "https://cppa.ca.gov/data_broker_registry/registry.csv",
    "eraser": "https://raw.githubusercontent.com/digisamroc/eraser/main/data/brokers.yaml",
    "aidr": "https://raw.githubusercontent.com/stephenlthorn/auto-identity-remove/main/brokers.js",
    "badbool": "https://raw.githubusercontent.com/yaelwrites/Big-Ass-Data-Broker-Opt-Out-List/master/README.md",
}
# The California filing's yes/no columns, by the words that start them, and the category they mean.
CPPA_COLLECTS = {"personal information of minors": "minors' data", "account logins": "account logins or numbers",
                 "government": "government ID numbers", "citizenship": "citizenship or immigration status",
                 "union membership": "union membership", "sexual orientation": "sexual orientation",
                 "gender identity": "gender identity", "biometric": "biometric data",
                 "precise geolocation": "precise geolocation", "reproductive health": "reproductive health data"}
CPPA_SHARED = {"foreign actor": "shared with a foreign actor", "federal government": "shared with the federal government",
               "other state government": "shared with other state governments",
               "law enforcement": "shared with law enforcement", "developer of": "shared with an AI developer"}
# auto-identity-remove's template values -> the registry's elements and search placeholders.
AIDR_ELEMENTS = {"F": "first_name", "L": "last_name", "N": "full_name", "E": "email", "ST": "state", "C": "city",
                 "Z": "zip"}
AIDR_SEARCH = {"F": "{first}", "L": "{last}", "N": "{full_name}", "ST": "{state}", "C": "{city}"}
# A few well-known companies whose kind the lists don't say (from their own descriptions of themselves).
KNOWN_CATEGORY = {"acxiom.com": "marketing", "liveramp.com": "identity-resolution", "epsilon.com": "marketing",
                  "oracle.com": "marketing", "zoominfo.com": "b2b", "clearbit.com": "b2b", "data-axle.com": "marketing",
                  "lexisnexis.com": "identity-resolution", "equifax.com": "marketing", "experian.com": "marketing",
                  "ancestry.com": "other", "familysearch.org": "other", "classmates.com": "other",
                  "pimeyes.com": "other", "facecheck.id": "other", "transunion.com": "identity-resolution",
                  "pipl.com": "identity-resolution"}
# Parent companies the Big-Ass list names ("BeenVerified also owns PeopleLooker and PeopleSmart").
KNOWN_PARENT = {"beenverified.com": "The Lifetime Value Co.", "peoplelooker.com": "The Lifetime Value Co.",
                "peoplesmart.com": "The Lifetime Value Co."}
# Gone: the Big-Ass list took these down (court order, September 2026).
DEFUNCT = {"radaris.com": "down after a court order (Big-Ass list, September 2026)",
           "rehold.com": "down after a court order (Big-Ass list, September 2026)"}
OPT_OUT_WORDS = re.compile(r"opt[\s-]?out|remov|suppress|privacy control|delete|do not sell", re.IGNORECASE)


def fetch(source):
    import requests
    response = requests.get(URLS[source], timeout=60, headers={"User-Agent": "Mozilla/5.0 MacUtils broker import"})
    response.raise_for_status()
    response.encoding = response.encoding or "utf-8"
    return response.text


def _domain(url):
    return psl.registrable_domain(url or "") or ""


def _https(url):
    url = (url or "").strip()
    if not url:
        return ""
    if not re.match(r"^https?://", url, re.IGNORECASE):
        url = "https://" + url.lstrip("/")
    return url


def _sites(text):
    """A cell that may hold several sites ("a.com; https://b.com, c.org") -> their URLs, first first."""
    out = []
    for part in re.split(r"[;,\s]+|(?<=[a-z])(?=https?://)", text or ""):
        url = _https(part.strip())
        if _domain(url) and "." in _domain(url) and url not in out:
            out.append(url)
    return out


def _email(text):
    text = (text or "").strip().replace(" [at] ", "@").replace("[at]", "@").replace(" at ", "@")
    match = re.search(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+", text)
    return match.group(0).lower() if match else ""


# ------------------------------------------------------------ the sources, each as a list of partial entries

def from_cppa(text):
    rows = list(csv.reader(io.StringIO(text.lstrip("﻿"))))
    header = [h.strip() for h in rows[0]]

    def column(*words):
        for i, h in enumerate(header):
            if all(w.lower() in h.lower() for w in words):
                return i
        return None

    name, dba, site = column("broker name"), column("Doing Business As"), column("primary website:")
    email, rights = column("contact email"), column("details on how")
    total, denied = column("Requests to delete", "Total requests received:"), column("Requests to delete", "Denied")
    collects = {cat: column(words) for words, cat in CPPA_COLLECTS.items()}
    shared = {cat: column(words) for words, cat in CPPA_SHARED.items()}
    out = []
    for row in rows[1:]:
        if not row or not row[name].strip():
            continue
        cell = lambda i: row[i].strip() if i is not None and i < len(row) else ""
        legal, trade = cell(name), cell(dba)
        sites = _sites(cell(site))
        if not sites:
            continue
        website = sites[0]
        entry = {"name": trade.split(";")[0].split(",")[0].strip() or legal, "website": website,
                 "parent_company": legal if trade else None, "privacy_email": _email(cell(email)) or None,
                 "opt_out_url": _https(cell(rights)) or None, "sources": ["cppa"],
                 "data_categories": [c for c, i in collects.items() if cell(i).lower() == "yes"]
                 + [c for c, i in shared.items() if cell(i).lower() == "yes"],
                 "domains_extra": [_domain(u) for u in sites[1:]]}
        try:
            received, refused = int(cell(total) or 0), int(cell(denied) or 0)
            entry["registry_stats"] = {"deletion_requests_2025": received, "denied": refused}
        except ValueError:
            pass
        out.append(entry)
    return out


def from_eraser(text):
    out = []
    for item in (yaml.safe_load(text) or {}).get("brokers", []):
        website = _https(item.get("website"))
        if not _domain(website):
            continue
        entry = {"name": item.get("name"), "website": _sites(website)[0] if _sites(website) else website, "privacy_email": _email(item.get("email")) or None,
                 "opt_out_url": _https(item.get("opt_out_url")) or None, "sources": ["eraser"]}
        if item.get("category") in registry.CATEGORIES:
            entry["category"] = item["category"]
        out.append(entry)
    return out


def _js_string(block, key):
    match = re.search(rf"\b{key}:\s*(['`])(.*?)\1", block, re.DOTALL)
    return match.group(2) if match else None


def _aidr_template(value, table):
    """`https://x/${enc(F)}-${ST}` -> `https://x/{first}-{state}` (None if it uses something else)."""
    def swap(match):
        key = match.group(1)
        if key not in table:
            raise KeyError(key)
        return table[key]
    try:
        return re.sub(r"\$\{(?:enc\()?(\w+)\)?\}", swap, value)
    except KeyError:
        return None


def from_aidr(text):
    body = text.split("return [", 1)[-1]
    out = []
    for block in re.split(r"\n  \{\n", body)[1:]:
        block = block.split("\n  },")[0]
        name, method = _js_string(block, "name"), _js_string(block, "method")
        if not name or method not in registry.METHODS:
            continue
        opt_out = _js_string(block, "optOutUrl") or ""
        if "${" in opt_out:
            opt_out = opt_out.split("?")[0]                # never keep your details in a stored URL
        search = _js_string(block, "searchUrl")
        search = _aidr_template(search, AIDR_SEARCH) if search else None
        website = _https(opt_out or search or "")
        website = f"https://{psl.host_of(website)}" if psl.host_of(website) else ""
        entry = {"name": re.sub(r"\s*\((email|marketing)\)$", "", name), "method": method, "sources": ["aidr"],
                 "opt_out_url": opt_out or None, "website": website or None}
        email = _js_string(block, "emailTo")
        if email:
            entry["privacy_email"] = email.lower()
            entry["method"] = "email"
        if search:
            entry["search_url"] = search
        pattern = re.search(r"listingPattern:\s*/(.+?)/i?,", block)
        if pattern:
            entry["listing_pattern"] = "(?i)" + pattern.group(1).replace("\\/", "/")
        fields = re.search(r"formFields:\s*\{(.*?)\}\s*,", block, re.DOTALL)
        if fields:
            mapped = {css: AIDR_ELEMENTS[v] for css, v in re.findall(r"'([^']+)'\s*:\s*(\w+)", fields.group(1))
                      if v in AIDR_ELEMENTS}
            entry["selectors"] = {"fields": mapped, "submit": _js_string(block, "submitSelector") or TODO, "confirm": TODO}
            elements = list(dict.fromkeys(mapped.values()))
            if method == "search-form":
                elements.append("listing_url")
                entry["selectors"]["listing"] = TODO
            entry["required_elements"] = elements
        if "captchaLikely: true" in block:
            entry["captcha_likely"] = True
        sender = _js_string(block, "expectedSender")
        entry["domains_extra"] = [_domain(sender)] if sender else []
        note = _js_string(block, "notes") or _js_string(block, "note")
        if note:
            entry["notes"] = note
        if entry["website"] or entry.get("privacy_email"):
            if not entry["website"]:
                entry["website"] = "https://" + entry["privacy_email"].split("@")[1]
            out.append(entry)
    return out


def from_badbool(text):
    people = text.split("## People Search Sites", 1)[-1].split("\n## ", 1)[0]
    out = []
    for chunk in re.split(r"\n### ", people)[1:]:
        heading, _, body = chunk.partition("\n")
        marks = heading
        name = re.sub(r"[^\w\s.’'&-]", "", heading).strip()
        links = re.findall(r"\[([^\]]*)\]\((https?://[^)\s]+)\)", body)
        emails = [e.lower() for e in re.findall(r"<([^<>@\s]+@[^<>\s]+)>", body)]
        if not links:
            continue
        opt_out = next((u for t, u in links if OPT_OUT_WORDS.search(t)), None) \
            or next((u for _t, u in links if OPT_OUT_WORDS.search(u)), None)
        first_party = [u for _t, u in links if _domain(u) and _domain(u) not in ("onetrust.com",)]
        website = f"https://{psl.host_of(first_party[0])}" if first_party else None
        if not website:
            continue
        entry = {"name": name.replace("’", "'"), "website": website, "opt_out_url": opt_out, "sources": ["badbool"],
                 "category": "people-search", "notes": " ".join(body.split())[:600]}
        if "💐" in marks:
            entry["priority"] = "crucial"
        elif "☠" in marks:
            entry["priority"] = "high"
        if "🎫" in marks:
            entry["id_required"] = True
        if "📞" in marks:
            entry["phone_required"] = True
        if "💰" in marks:
            entry["paid"] = True
        own = [e for e in emails if _domain(e.split("@")[1]) == _domain(website)]
        if own:
            entry["privacy_email"] = own[0]
        entry["domains_extra"] = [_domain(u) for _t, u in links if _domain(u)] + [_domain(e.split("@")[1]) for e in emails]
        out.append(entry)
    return out


def from_state_csv(text, state):
    rows = list(csv.DictReader(io.StringIO(text.lstrip("﻿"))))
    out = []
    for row in rows:
        lowered = {k.lower().strip(): (v or "").strip() for k, v in row.items() if k}
        pick = lambda *words: next((v for k, v in lowered.items() if any(w in k for w in words) and v), "")
        sites = _sites(pick("website", "url", "web"))
        website = sites[0] if sites else ""
        name = pick("dba", "doing business", "name")
        if not _domain(website) or not name:
            continue
        out.append({"name": name, "website": website, "privacy_email": _email(pick("email")) or None,
                    "sources": [f"{state.lower()}-registry"]})
    return out


# ------------------------------------------------------------ merging

def _slug(domain, taken):
    label, _, tld = domain.partition(".")
    base = re.sub(r"[^a-z0-9-]", "-", label.lower()).strip("-") or "broker"
    with_tld = f"{base}-{re.sub(r'[^a-z0-9]', '', tld)}"
    candidate = base if base not in taken and len(base) > 4 else with_tld
    n = 2
    while candidate in taken:
        candidate, n = f"{base}-{n}", n + 1
    return candidate


# Which source wins a field, best first (later sources only fill what earlier ones left empty).
PREFER = {"name": ("aidr", "badbool", "cppa", "eraser"), "opt_out_url": ("aidr", "badbool", "eraser", "cppa"),
          "privacy_email": ("cppa", "aidr", "badbool", "eraser"), "category": ("badbool", "eraser"),
          "website": ("cppa", "badbool", "aidr", "eraser")}


def merge(partials):
    """{domain: merged entry} from every source's partial entries."""
    grouped = {}
    for entry in partials:
        grouped.setdefault(_domain(entry["website"]), []).append(entry)
    merged = {}
    for domain, group in grouped.items():
        def rank(field, e):
            order = PREFER.get(field, ("aidr", "badbool", "cppa", "eraser"))
            source = e["sources"][0].split("-")[0]
            return order.index(source) if source in order else len(order)

        out = {"sources": sorted({s for e in group for s in e["sources"]})}
        fields = {k for e in group for k in e if k not in ("sources", "domains_extra")}
        for field in fields:
            for e in sorted(group, key=lambda e: rank(field, e)):
                if e.get(field) not in (None, "", [], TODO):
                    out[field] = e[field]
                    break
        extra = [d for e in group for d in e.get("domains_extra", [])]
        out["domains"] = sorted({d for d in [domain, _domain(out.get("opt_out_url")), _domain(out.get("search_url")),
                                             _domain((out.get("privacy_email") or "@").split("@")[1])] + extra
                                 if d and d not in ("google.com", "github.com", "onetrust.com", "ko-fi.com")})
        out["domains"] = [d for d in out["domains"] if d == domain or not registry.shared(d)]
        merged[domain] = out
    return _same_names(merged)


def _name_key(name):
    name = re.sub(r"\b(inc|llc|ltd|corp|corporation|co|company|lp|plc|gmbh)\b\.?", "", (name or "").lower())
    return re.sub(r"[^a-z0-9]", "", name)


def _same_names(merged):
    """One entry per company: two domains filed under the same name are one broker with both domains."""
    by_name = {}
    for domain, entry in merged.items():
        by_name.setdefault(_name_key(entry.get("name")), []).append(domain)
    for key, domains in by_name.items():
        if len(domains) < 2 or not key:
            continue
        domains.sort(key=lambda d: (-len(merged[d]["sources"]), d))
        keep = merged[domains[0]]
        for other in domains[1:]:
            entry = merged.pop(other)
            keep["domains"] = sorted(set(keep["domains"]) | set(entry["domains"]))
            keep["sources"] = sorted(set(keep["sources"]) | set(entry["sources"]))
            for field, value in entry.items():
                if keep.get(field) in (None, "", [], TODO) and value not in (None, "", [], TODO):
                    keep[field] = value
    return merged


def _complete(entry, domain):
    """Fill the fields the sources didn't, marking guesses TODO_VERIFY."""
    entry.setdefault("category", KNOWN_CATEGORY.get(domain, TODO))
    if entry.get("category") == TODO and domain in KNOWN_CATEGORY:
        entry["category"] = KNOWN_CATEGORY[domain]
    if domain in KNOWN_PARENT and not entry.get("parent_company"):
        entry["parent_company"] = KNOWN_PARENT[domain]
    entry.setdefault("parent_company", TODO)
    if entry.get("parent_company") is None:
        entry["parent_company"] = TODO
    if domain in DEFUNCT:
        entry["status"] = "defunct"
        entry["notes"] = (DEFUNCT[domain] + ". " + entry.get("notes", "")).strip()
    if "method" not in entry:
        # Only an email address on file: email is the one method nothing has to be checked for.
        entry["method"] = "email" if entry.get("privacy_email") else TODO
    entry.setdefault("summary", TODO)
    if not entry.get("data_categories"):
        entry["data_categories"] = TODO
    entry.setdefault("required_elements", TODO)
    entry.setdefault("email_purpose", TODO)
    entry.setdefault("honors_nonresidents", "unknown")
    entry.setdefault("recheck_days", 90)
    entry.setdefault("priority", "normal")
    for flag in registry.FLAGS:
        entry.setdefault(flag, TODO if flag in ("captcha_likely", "id_required", "account_required") else False)
    entry.setdefault("last_verified", TODO)
    for field in ("privacy_email", "opt_out_url"):
        if not entry.get(field):
            entry.pop(field, None)
    return entry


def build(texts, write=True):
    """texts: {source: raw text} (and "state:vt": csv text). Writes the registry; returns counts."""
    partials, counts = [], {}
    readers = {"cppa": from_cppa, "eraser": from_eraser, "aidr": from_aidr, "badbool": from_badbool}
    for source, text in texts.items():
        found = from_state_csv(text, source.split(":")[1]) if source.startswith("state:") else readers[source](text)
        counts[source] = len(found)
        partials += found
    merged = merge(partials)
    existing = registry.load(fresh=True)
    by_domain = {}
    for broker_id, entry in existing.items():
        for domain in registry.get(entry, "domains", []) or [_domain(entry.get("website"))]:
            by_domain.setdefault(domain, broker_id)
    taken = set(existing)
    written = {"new": 0, "updated": 0}
    for domain, entry in sorted(merged.items()):
        entry = _complete(entry, domain)
        broker_id = by_domain.get(domain)
        if broker_id:
            old = dict(existing[broker_id])
            for field, value in entry.items():
                if field == "sources":
                    old["sources"] = sorted(set(old.get("sources") or []) | set(value))
                elif field == "domains":
                    old["domains"] = sorted(set(registry.get(old, "domains", [])) | set(value))
                elif field == "registry_stats":
                    old[field] = value
                elif field not in old or registry.is_todo(old[field]):
                    old[field] = value
            entry, written["updated"] = old, written["updated"] + 1
        else:
            broker_id = _slug(domain, taken)
            taken.add(broker_id)
            entry = {"id": broker_id, **entry}
            written["new"] += 1
        entry["id"] = broker_id
        if write:
            registry.write(entry)
    return {"read": counts, "brokers": len(merged), **written}


def run(sources=("cppa", "eraser", "aidr", "badbool"), state=None, file=None):
    texts = {}
    for source in sources:
        texts[source] = fetch(source)
    if state and file:
        import os
        with open(os.path.expanduser(file), encoding="utf-8-sig", errors="replace") as f:
            texts[f"state:{state}"] = f.read()
    return build(texts)
