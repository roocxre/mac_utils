"""Canary tests: does what you type into a field leave the page before you submit?

In a fresh, throwaway headless Chromium (no extension, no blocking, no cookies of yours):

1. Load the page. Don't touch a consent banner; just note whether one showed.
2. Make up a canary address, canary-<random>@example.com (example.com is reserved by RFC 2606
   and takes no mail, so the test never hands out an address anyone can write to).
3. Find the field (the extension's CSS path, else the same keyword rules as the extension),
   focus it, type the canary with human timing, blur it, wait about 10 seconds. Never submit.
4. Every request, request body and WebSocket frame is searched for the canary: plain,
   URL-encoded, Base64, MD5/SHA-1/SHA-256, and its unique local part on its own (keystroke
   capture sends it before it's complete).

The evidence is the network capture. Pages behind your login can't be tested, and the visit
comes from your own IP address.
"""
import base64
import hashlib
import json
import random
import secrets
import time
from urllib.parse import quote

from dataflow import EXTENSION_DIR, entities, lists, store
from lib import psl

CONSENT = ("#onetrust-banner-sdk", "#onetrust-consent-sdk", "#CybotCookiebotDialog", ".fc-consent-root",
           "#truste-consent-track", "#didomi-host", "#usercentrics-root", ".osano-cm-dialog", "#qc-cmp2-container",
           "[id*=cookie-banner]", "[class*=cookie-banner]", "[id*=cookieConsent]", "[aria-label*=cookie i]",
           "[id^=sp_message_container]", ".cky-consent-container", "#iubenda-cs-banner")
USER_AGENT = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) "
              "Chrome/140.0.0.0 Safari/537.36 Edg/140.0.0.0")


def canary_address():
    """A new throwaway address for one test: canary-<random>@example.com."""
    return f"canary-{secrets.token_hex(6)}@example.com"


def forms(address):
    """{needle: how} for every encoded form of the canary, and whether it is case-sensitive."""
    from dataflow import service
    raw = address.lower().encode()
    out = {address.lower(): ("plain", False), quote(address.lower(), safe=""): ("url-encoded", False),
           hashlib.md5(raw).hexdigest(): ("md5", False), hashlib.sha1(raw).hexdigest(): ("sha1", False),
           hashlib.sha256(raw).hexdigest(): ("sha256", False), address.split("@")[0].lower(): ("typed so far", False),
           base64.b64encode(hashlib.sha256(raw).digest()).decode().rstrip("="): ("sha256", True)}
    for needle in service.needles([address.lower()])["cs"]:
        out.setdefault(needle, ("base64", True))
    return out


def search(captured, needles, site):
    """[{domain, owner, categories, how, requests, first_party}] for every domain that got the canary."""
    hits = {}
    for item in captured:
        hay = item["url"] + "\n" + (item.get("body") or "")
        low = hay.lower()
        found = [how for needle, (how, exact) in needles.items() if (needle in hay if exact else needle in low)]
        if not found:
            continue
        domain = psl.registrable_domain(item["url"]) or item["url"][:60]
        entry = hits.setdefault(domain, {"domain": domain, "how": set(), "requests": 0, "kinds": set()})
        entry["how"].update(found)
        entry["requests"] += 1
        entry["kinds"].add(item["kind"])
    out = []
    for domain, entry in hits.items():
        info = entities.resolve(domain, site)
        out.append({"domain": domain, "owner": info.get("owner"), "categories": info.get("categories"),
                    "broker": info.get("broker"), "first_party": info.get("first_party", False),
                    "how": sorted(entry["how"]), "requests": entry["requests"], "kinds": sorted(entry["kinds"])})
    return sorted(out, key=lambda e: (e["first_party"], e["domain"]))


def _find_field(page, selector, field_type):
    """A locator for the field: the extension's selector, else the keyword rules (fields.js)."""
    if selector:
        try:
            locator = page.locator(selector).first
            if locator.count() and locator.is_visible():
                return locator
        except Exception:
            pass
    page.add_script_tag(path=f"{EXTENSION_DIR}/fields.js")
    index = page.evaluate("""(wanted) => {
        const F = globalThis.WDMEG_FIELDS;
        const all = F.find(document).filter(el => { const r = el.getBoundingClientRect(); return r.width > 10 && r.height > 8; });
        const pick = all.find(el => F.classify(el).type === wanted) || all.find(el => F.classify(el).type === 'email');
        if (!pick) return -1;
        pick.setAttribute('data-wdmeg-canary', '1');
        return 1;
    }""", field_type or "email")
    return page.locator("[data-wdmeg-canary]").first if index == 1 else None


def run(canary_id, log=None):
    """Run one queued canary test. Returns its site."""
    from playwright.sync_api import sync_playwright

    cfg = lists.config()
    with store.db() as conn:
        row = dict(conn.execute("SELECT * FROM canaries WHERE id = ?", (canary_id,)).fetchone())
        conn.execute("UPDATE canaries SET status = 'running', started_at = ? WHERE id = ?", (time.time(), canary_id))
    site, url = row["site"], row["url"]
    captured, result = [], {"field_found": False, "consent_banner": False}
    typed_at = None
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        try:
            context = browser.new_context(user_agent=USER_AGENT, locale="en-US", viewport={"width": 1280, "height": 900})

            def on_request(request):
                if typed_at is None:
                    return
                try:
                    body = request.post_data_buffer
                    body = body.decode("utf-8", "replace")[:300000] if body else ""
                except Exception:
                    body = ""
                captured.append({"url": request.url, "body": body, "kind": request.resource_type})

            def on_socket(socket):
                socket.on("framesent", lambda payload: captured.append(
                    {"url": socket.url, "body": payload if isinstance(payload, str) else
                     payload.decode("utf-8", "replace"), "kind": "websocket"}) if typed_at else None)

            context.on("request", on_request)
            page = context.new_page()
            page.on("websocket", on_socket)
            page.goto(url, timeout=cfg["canary_timeout_seconds"] * 1000, wait_until="domcontentloaded")
            page.wait_for_timeout(4000)
            result["consent_banner"] = any(_visible(page, s) for s in CONSENT)
            field = _find_field(page, row["field"], row["field_type"])
            if field is None:
                result["note"] = "couldn't find the field on the page as a fresh visitor sees it"
            else:
                result["field_found"] = True
                address = canary_address()
                with store.db() as conn:
                    conn.execute("UPDATE canaries SET alias = ? WHERE id = ?", (address, canary_id))
                field.scroll_into_view_if_needed(timeout=5000)
                field.focus(timeout=5000)
                typed_at = time.time()
                for char in address:
                    page.keyboard.type(char)
                    page.wait_for_timeout(random.randint(60, 180))
                field.evaluate("el => el.blur()")
                page.wait_for_timeout(cfg["canary_wait_seconds"] * 1000)
                result["requests"] = len(captured)
                found = search(captured, forms(address), site)
                result["leaked_to"] = [f for f in found if not f["first_party"]]
                result["first_party_saved"] = [f for f in found if f["first_party"]]
        finally:
            browser.close()
    with store.db() as conn:
        conn.execute("UPDATE canaries SET status = 'done', ran_at = ?, result = ?, error = NULL WHERE id = ?",
                     (time.time(), json.dumps(result), canary_id))
    if log:
        log.info("canary %s %s: %s", site, row["field_type"], {k: v for k, v in result.items() if k != "first_party_saved"})
    return site


def _visible(page, selector):
    try:
        locator = page.locator(selector).first
        return locator.count() > 0 and locator.is_visible()
    except Exception:
        return False
