"""Finding your listings on people-search sites (brokers whose search_url and listing_pattern are
known), in the agent's own browser profile (forms.session).

The search is the least that finds you: your name plus your state (or city), filled into the
broker's search_url. Each result page's listing links (listing_pattern) are opened, at most
MAX_LISTINGS, and scored against your vault, so a namesake isn't opted out in your place:

    age      the listing's age within a year of yours                    AGE
    places   share of the listing's cities you have lived in             PLACES
    people   any of your relatives named on it                           PEOPLE
    contact  any of your phone numbers or emails on it                   CONTACT

A score of at least match.confirmed_at (config.json) is a listing; from match.possible_at it is a
"possible match" for you to confirm in the report; below that it is someone else. What the
listing shows (addresses, phones, emails, relatives, age) and a screenshot are kept in the vault.

It never pays and never makes an account; a CAPTCHA or "are you human" check stops it, and the
broker's scan becomes a manual task ("search yourself").
"""
import json
import re
import time
from urllib.parse import quote, urljoin

from brokers import forms, plan, registry, vault

MAX_LISTINGS = 4
AGE, PLACES, PEOPLE, CONTACT = 0.25, 0.35, 0.25, 0.3
_PHONE = re.compile(r"\(?\b\d{3}\)?[-.\s]\d{3}[-.\s]\d{4}\b")
_EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
_AGE = re.compile(r"\bage[:\s]+(\d{2})\b|\b(\d{2}) years? old\b", re.IGNORECASE)
_PLACE = re.compile(r"\b([A-Z][a-z]+(?:[ -][A-Z][a-z]+){0,2}),\s*([A-Z]{2})\b")
_STREET = re.compile(r"\b\d{1,6}\s+(?:[A-Z0-9][\w.]*\s){1,4}(?:St|Street|Ave|Avenue|Rd|Road|Blvd|Dr|Drive|Ln|Lane|Ct|"
                     r"Court|Pl|Place|Way|Ter|Terrace|Cir|Pkwy|Hwy)\b\.?(?:\s*(?:Apt|Unit|#)\s*\w+)?", re.IGNORECASE)


def searchable(entry):
    return bool(entry.get("search_url") and entry.get("listing_pattern")) and \
        not registry.is_todo(entry.get("search_url")) and registry.get(entry, "status") == "active"


def search_url(entry, found):
    here = vault.current_address(found) or {}
    values = {"first": found.get("first_name", ""), "last": found.get("last_name", ""),
              "full_name": f"{found.get('first_name', '')} {found.get('last_name', '')}".strip(),
              "city": here.get("city", ""), "state": here.get("state", "")}
    return re.sub(r"{(\w+)}", lambda m: quote(values.get(m.group(1), ""), safe=""), entry["search_url"])


def _digits(text):
    return re.sub(r"\D", "", text or "")[-10:]


def extract(text):
    """What a listing page shows: {"age", "places", "streets", "phones", "emails"}."""
    ages = [int(a or b) for a, b in _AGE.findall(text)]
    return {"age": ages[0] if ages else None,
            "places": sorted({f"{city}, {state}" for city, state in _PLACE.findall(text)})[:30],
            "streets": sorted({" ".join(s.split()) for s in _STREET.findall(text)})[:20],
            "phones": sorted({p for p in _PHONE.findall(text)})[:20],
            "emails": sorted({e.lower() for e in _EMAIL.findall(text)})[:20]}


def score(text, shown, found):
    """(score 0..1, reasons) for a listing page's text against your vault. Your name must be on it."""
    first, last = (found.get("first_name") or "").lower(), (found.get("last_name") or "").lower()
    lowered = text.lower()
    names = [f"{first} {last}"] + [v.lower() for v in found.get("name_variants") or []]
    if not any(n and n in lowered for n in names) and not (first and last and first in lowered and last in lowered):
        return 0.0, ["your name isn't on it"]
    total, reasons = 0.0, []
    mine = vault._age(found.get("dob"))
    if mine and shown["age"]:
        if abs(shown["age"] - mine) <= 1:
            total += AGE
            reasons.append(f"age {shown['age']}")
        else:
            total -= AGE
            reasons.append(f"age {shown['age']}, not yours")
    lived = {f"{a.get('city', '')}, {a.get('state', '')}".lower() for a in found.get("addresses") or [] if a.get("city")}
    if lived and shown["places"]:
        hits = [p for p in shown["places"] if p.lower() in lived]
        if hits:
            total += PLACES * min(1.0, len(hits) / max(1, min(len(lived), 3)))
            reasons.append("lived in " + ", ".join(hits[:3]))
    relatives = [r for r in found.get("relatives") or [] if r and r.lower() in lowered]
    if relatives:
        total += PEOPLE
        reasons.append("relatives " + ", ".join(relatives[:3]))
    phones = {_digits(p) for p in found.get("phones") or []}
    emails = {e.lower() for e in found.get("emails") or []}
    if phones & {_digits(p) for p in shown["phones"]} or emails & set(shown["emails"]):
        total += CONTACT
        reasons.append("your phone or email")
    return max(0.0, min(1.0, total)), reasons


def verdict(value, cfg):
    return "match" if value >= cfg["match"]["confirmed_at"] else "possible" if value >= cfg["match"]["possible_at"] \
        else "not_me"


def scan_one(context, broker_id, entry, found, cfg, log):
    """Search one site. Returns {"listed": listed|possible|not_listed|unknown, "listings": [...], "manual": why}."""
    domains = set(entry.get("domains") or [])
    fine, why = forms.site_ok(search_url(entry, found))
    if not fine:
        return {"listed": "unknown", "listings": [], "manual": why}
    page = context.new_page()
    try:
        page.goto(search_url(entry, found), wait_until="domcontentloaded")
        page.wait_for_timeout(2500)
        forms._stay(page, domains)
        forms.hard_stops(page)
        pattern = re.compile(entry["listing_pattern"])
        links = []
        for href in page.eval_on_selector_all("a[href]", "els => els.map(e => e.href)"):
            absolute = urljoin(page.url, href).split("#")[0]
            if pattern.search(absolute) and absolute not in links and forms.site(absolute) in domains:
                links.append(absolute)
        results = []
        for url in links[:MAX_LISTINGS]:
            page.goto(url, wait_until="domcontentloaded")
            page.wait_for_timeout(1500)
            text = forms.hard_stops(page)
            shown = extract(text)
            value, reasons = score(text, shown, found)
            seen = verdict(value, cfg)
            if seen == "not_me":
                continue
            shown["relatives"] = [r for r in found.get("relatives") or [] if r and r.lower() in text.lower()]
            shot = page.screenshot(full_page=False, type="png")
            results.append({"url": url, "fields": shown, "score": round(value, 2), "verdict": seen, "why": reasons,
                            "screenshot": shot})
        if any(r["verdict"] == "match" for r in results):
            listed = "listed"
        elif results:
            listed = "possible"
        else:
            listed = "not_listed"
        return {"listed": listed, "listings": results, "searched": len(links)}
    except forms.Stop as stop:
        return {"listed": "unknown", "listings": [], "manual": stop.manual, "detail": stop.detail}
    except Exception as error:                       # a timeout or a site that's down: try again later
        log.warning("scan %s failed: %s", broker_id, error)
        return {"listed": "unknown", "listings": [], "error": f"{type(error).__name__}: {str(error)[:160]}"}
    finally:
        page.close()


def record(broker_id, result):
    """Keep a scan's result: listings, the broker's listed state, and a task when it needs you."""
    now = time.time()
    with vault.db() as conn:
        for listing in result["listings"]:
            conn.execute("INSERT INTO listings (broker_id, url, fields, score, verdict, screenshot, found_at, seen_at) "
                         "VALUES (?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT (broker_id, url) DO UPDATE SET fields = excluded.fields, "
                         "score = excluded.score, screenshot = excluded.screenshot, seen_at = excluded.seen_at, "
                         "gone_at = NULL, verdict = CASE WHEN listings.verdict IN ('confirmed', 'rejected') "
                         "THEN listings.verdict ELSE excluded.verdict END",
                         (broker_id, listing["url"], json.dumps({**listing["fields"], "why": listing["why"]}),
                          listing["score"], listing["verdict"], listing["screenshot"], now, now))
        if result["listed"] in ("listed", "possible", "not_listed"):
            urls = [l["url"] for l in result["listings"]]
            marks = ",".join("?" * len(urls)) or "''"
            conn.execute(f"UPDATE listings SET gone_at = ? WHERE broker_id = ? AND gone_at IS NULL AND url NOT IN ({marks})",
                         (now, broker_id, *urls))
        row = conn.execute("SELECT * FROM brokers WHERE id = ?", (broker_id,)).fetchone()
        conn.execute("UPDATE brokers SET listed = ?, scanned_at = ? WHERE id = ?", (result["listed"], now, broker_id))
        vault.history(conn, broker_id, f"scanned: {result['listed']}",
                      result.get("detail") or result.get("error") or f"{len(result['listings'])} listing(s)")
        if row and row["state"] == "discovered" and result["listed"] != "unknown":
            plan.set_state(conn, broker_id, "scanned", result["listed"])
        if result.get("manual"):
            open_task(conn, broker_id, "search",
                      "Search for yourself", [result.get("detail", ""), "The site wouldn't let the agent search. Search "
                      "for your name there yourself; if you're listed, copy the listing's address into Your details' "
                      "notes for this broker, or mark the task done with outcome=listed."],
                      registry.broker(broker_id).get("website"))
        for listing in result["listings"]:
            if listing["verdict"] == "possible":
                ref = conn.execute("SELECT id FROM listings WHERE broker_id = ? AND url = ?",
                                   (broker_id, listing["url"])).fetchone()["id"]
                open_task(conn, broker_id, "possible_match", "Is this you?",
                          [f"Score {listing['score']}: " + "; ".join(listing["why"]),
                           "Confirm it if it's you (the opt-out will include it), or say it's not you."], listing["url"], ref)


def open_task(conn, broker_id, kind, title, steps, link, ref=None):
    """A manual task, unless the same one is already open."""
    if conn.execute("SELECT 1 FROM tasks WHERE broker_id = ? AND kind = ? AND done_at IS NULL AND COALESCE(ref, 0) = ?",
                    (broker_id, kind, ref or 0)).fetchone():
        return
    conn.execute("INSERT INTO tasks (broker_id, kind, title, steps, link, ref, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                 (broker_id, kind, title, json.dumps([s for s in steps if s]), link, ref, time.time()))
    vault.history(conn, broker_id, f"manual task: {kind}", title)


def listing_urls(conn, broker_id):
    """Listings to include in an opt-out: matches and the possible ones you confirmed."""
    rows = conn.execute("SELECT url FROM listings WHERE broker_id = ? AND gone_at IS NULL AND verdict IN ('match', "
                        "'confirmed') ORDER BY score DESC", (broker_id,)).fetchall()
    return [r["url"] for r in rows]
