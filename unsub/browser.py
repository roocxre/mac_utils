"""Phase 4, step 3: open an unsubscribe link in a headless browser and click through it.

Safety:
    - only for senders whose authentication lines up (actions.py checks before calling)
    - a fresh browser context each time: no cookies, nothing from your own browser
    - only the sender's own domains (From and passing DKIM) and known sending platforms are
      visited: HTTP redirects are followed by hand first and a hop elsewhere stops it; in the
      browser, a top-level navigation elsewhere is blocked, and the page's address is checked
      before every click
    - it never types anything; a page with a password or card field stops it
    - every step has a timeout, and the whole run TOTAL_SECONDS

The steps on a page: done if it already says you are unsubscribed; otherwise a dedicated
handler for the big sending platforms (their standard pages), else the generic one: tick
"unsubscribe from all" style boxes, click the most unsubscribe-like button, look again.
Returns (ok, detail).
"""
import re
import time
from urllib.parse import urljoin, urlsplit

from lib import psl
from mail import headers as hdr

STEP_TIMEOUT_MS = 15000
NAVIGATION_TIMEOUT_MS = 20000
TOTAL_SECONDS = 90
MAX_STEPS = 4
MAX_REDIRECTS = 8
USER_AGENT = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) "
              "Chrome/124.0 Safari/537.36")

# Domains the sending platforms serve unsubscribe pages from, on top of mail/headers.PLATFORMS.
PLATFORM_DOMAINS = set(hdr.PLATFORMS) | {
    "list-manage.com", "mailchimp.com", "kmail-lists.com", "klaviyo.com", "sendgrid.net", "sendgrid.com",
    "hubspot.com", "hs-sites.com", "hubspotemail.net", "hsforms.com", "hubspotlinks.com", "exacttarget.com",
    "exct.net", "marketingcloudapps.com", "braze.com", "braze.eu", "constantcontact.com", "ctctcdn.com",
    "createsend.com", "createsend1.com", "mailgun.org", "mailgun.net", "sparkpostmail.com", "postmarkapp.com",
    "customer.io", "customeriomail.com", "mailjet.com", "sendinblue.com", "brevo.com", "iterable.com",
    "marketo.com", "mktoweb.com", "emarsys.net", "substack.com", "beehiiv.com", "convertkit.com", "ck.page",
    "omnisend.com", "attentivemobile.com", "shopify.com", "squarespace.com", "campaign-archive.com"}

CONFIRMED = re.compile(
    r"you(?:'ve|’ve| have| are| were|'re|’re)? (?:now |been |successfully )*(?:been )?(?:unsubscribed|removed from|opted out)"
    r"|successfully (?:unsubscribed|removed|opted out)|unsubscri(?:be|ption) (?:was |is )?(?:successful|complete|confirmed)"
    r"|(?:has|have) been (?:unsubscribed|removed from)|(?:preferences|settings) (?:have been |has been |were |was )?"
    r"(?:updated|saved)|subscription (?:has been |was )?cancell?ed|we(?:'ve|’ve| have) removed|no longer subscribed",
    re.IGNORECASE)
ASKS_FOR_SECRETS = ("input[type=password], input[autocomplete^='cc-'], input[autocomplete='current-password'], "
                    "input[name*='card' i], input[name*='cvv' i], input[name*='cvc' i], input[name*='iban' i]")
ALL_BOXES = re.compile(r"unsubscribe (?:me )?from all|opt[- ]?out (?:of|from) all|all (?:e-?mails|communications|"
                       r"mailings|marketing|lists)|from all (?:lists|e-?mails)|global unsubscribe", re.IGNORECASE)
AVOID = re.compile(r"keep me|stay subscribed|cancel|re-?subscribe|go back|no,? thanks|don.t unsubscribe|"
                   r"log ?in|sign ?in|sign ?up|subscribe to|register|privacy|terms", re.IGNORECASE)
RANKS = (re.compile(r"unsubscribe (?:me )?from all|opt[- ]?out (?:of|from) all", re.IGNORECASE),
         re.compile(r"^\W*(?:yes,? )?(?:unsubscribe(?: me| now)?|confirm unsubscri\w*)\W*$", re.IGNORECASE),
         re.compile(r"\bunsubscribe\b", re.IGNORECASE),
         re.compile(r"\bopt[- ]?out\b", re.IGNORECASE),
         re.compile(r"^\W*(?:confirm|submit|save(?: changes| preferences)?|update(?: preferences)?|continue|yes)\W*$",
                    re.IGNORECASE))

# name, host pattern, selectors tried in order: boxes are ticked, the first other one is clicked.
HANDLERS = [
    ("Mailchimp", r"(?:^|\.)(?:list-manage|mailchimp)\.com$",
     ["#unsub-submit", "input[type=submit][value*='nsubscribe']", "button:has-text('Unsubscribe')"]),
    ("Klaviyo", r"(?:^|\.)(?:klaviyo|kmail-lists)\.com$",
     ["button:has-text('Unsubscribe')", "input[type=submit][value*='nsubscribe']"]),
    ("SendGrid", r"(?:^|\.)sendgrid\.(?:net|com)$",
     ["label:has-text('nsubscribe from all') >> input", "button:has-text('Unsubscribe')",
      "input[type=submit][value*='nsubscribe']", "button:has-text('Confirm')"]),
    ("HubSpot", r"hubspot|(?:^|\.)hs-sites\.com$|hsforms",
     ["#globalunsub", "label:has-text('nsubscribe from all') >> input", "button:has-text('Unsubscribe from all')",
      "input[type=submit][value*='nsubscribe']", "button:has-text('Unsubscribe')", "button:has-text('Update')"]),
    ("Salesforce Marketing Cloud", r"(?:^|\.)(?:exacttarget\.com|exct\.net|marketingcloudapps\.com)$",
     ["label:has-text('nsubscribe from all') >> input", "input[value*='nsubscribe from all']",
      "input[type=submit][value*='nsubscribe']", "button:has-text('Unsubscribe')"]),
    ("Braze", r"(?:^|\.)(?:braze\.com|braze\.eu|appboy\.com)$",
     ["button:has-text('Unsubscribe')", "input[type=submit][value*='nsubscribe']"]),
    ("Constant Contact", r"(?:^|\.)constantcontact\.com$",
     ["input[type=radio][value*='all' i]", "input[value='Unsubscribe']", "button:has-text('Unsubscribe')",
      "input[type=submit]"]),
]

_CANDIDATES = "button, input[type=submit], input[type=button], [role=button], a"
_DESCRIBE = """els => els.map(e => ({
    text: (e.innerText || e.value || e.getAttribute('aria-label') || '').trim().replace(/\\s+/g, ' ').slice(0, 80),
    tag: e.tagName.toLowerCase(),
    visible: !!(e.offsetWidth || e.offsetHeight || e.getClientRects().length) && !e.disabled}))"""


class Refused(Exception):
    """The page or link broke a safety rule; the text says which."""


def site(host):
    host = (host or "").lower().rstrip(".")
    return psl.registrable_domain(host) or host


def host_of(url):
    try:
        return (urlsplit(url).hostname or "").lower()
    except ValueError:
        return ""


def allowed(url, domains):
    return urlsplit(url).scheme in ("https", "http") and site(host_of(url)) in domains


def allowed_domains(sender_domains):
    """The sender's own domains plus every known sending platform."""
    return {site(d) for d in sender_domains if d} | PLATFORM_DOMAINS


def resolve(url, domains):
    """Follow HTTP redirects by hand, stopping at any hop outside the allowed domains. The final URL."""
    import requests

    for _ in range(MAX_REDIRECTS):
        if not url.lower().startswith("https://"):
            raise Refused(f"not an https link: {url[:80]}")
        if not allowed(url, domains):
            raise Refused(f"{host_of(url)} is neither the sender's nor a sending platform's")
        response = requests.get(url, allow_redirects=False, timeout=15, headers={"User-Agent": USER_AGENT})
        location = response.headers.get("Location")
        if response.status_code in (301, 302, 303, 307, 308) and location:
            url = urljoin(url, location)
            continue
        return url
    raise Refused("too many redirects")


def _check(page, domains, blocked):
    if blocked:
        raise Refused(f"the page tried to go to {blocked[-1]}, which is neither the sender's nor a sending platform's")
    if not allowed(page.url, domains):
        raise Refused(f"the page moved to {host_of(page.url)}, which is neither the sender's nor a sending platform's")
    if page.locator(ASKS_FOR_SECRETS).count():
        raise Refused("the page asks for a password or payment details")


def _settle(page):
    try:
        page.wait_for_load_state("networkidle", timeout=8000)
    except Exception:          # a page that keeps polling never goes idle; carry on
        pass


def _confirmed(page):
    try:
        return CONFIRMED.search(page.inner_text("body", timeout=5000) or "") is not None
    except Exception:
        return False


def _tick(locator):
    kind = (locator.get_attribute("type") or "").lower()
    if kind in ("checkbox", "radio"):
        if not locator.is_checked():
            locator.check()
        return True
    return False


def _platform_step(page):
    """(handler name, clicked) for a known platform's page, or (None, False)."""
    host = host_of(page.url)
    for name, pattern, selectors in HANDLERS:
        if not re.search(pattern, host):
            continue
        for selector in selectors:
            found = page.locator(selector)
            for index in range(min(found.count(), 10)):
                element = found.nth(index)
                if not element.is_visible():
                    continue
                if _tick(element):
                    continue
                element.click()
                return name, True
        return name, False
    return None, False


def _generic_step(page, clicked):
    """Tick 'from all' boxes, then click the most unsubscribe-like control not clicked before. True if it clicked."""
    ticked = False
    boxes = page.locator("input[type=checkbox], input[type=radio]")
    for index in range(min(boxes.count(), 40)):
        box = boxes.nth(index)
        try:
            label = box.evaluate("e => ((e.labels && e.labels[0] && e.labels[0].innerText) || e.value || "
                                 "e.getAttribute('aria-label') || '')")
        except Exception:
            continue
        if ALL_BOXES.search(label or "") and box.is_visible():
            ticked = _tick(box) or ticked
    candidates = page.locator(_CANDIDATES)
    described = candidates.evaluate_all(_DESCRIBE)
    best, best_rank = None, len(RANKS)
    for index, item in enumerate(described):
        text = item["text"]
        if not item["visible"] or not text or AVOID.search(text) or (page.url, text) in clicked:
            continue
        for rank, pattern in enumerate(RANKS):
            if rank >= best_rank:
                break
            if item["tag"] == "a" and rank > 2:       # only follow links that say unsubscribe
                break
            if rank == 4 and not ticked and not page.locator("form").count():
                break
            if pattern.search(text):
                best, best_rank = (index, text), rank
                break
    if best is None:
        return False
    clicked.add((page.url, best[1]))
    candidates.nth(best[0]).click()
    return True


def _click_through(page, domains, deadline, blocked):
    clicked, used = set(), []
    for _step in range(MAX_STEPS + 1):
        _settle(page)
        _check(page, domains, blocked)
        if _confirmed(page):
            how = f" by {used[-1]}" if used else ""
            return True, f"confirmed on {host_of(page.url)}{how}"
        if time.time() > deadline or _step == MAX_STEPS:
            break
        name, done = _platform_step(page)
        if done:
            used.append(f"{name} handler")
        elif _generic_step(page, clicked):
            used.append("generic handler")
        else:
            return False, f"no unsubscribe button found on {host_of(page.url)}"
    return False, f"no confirmation on {host_of(page.url)} after {len(used)} click{'s' if len(used) != 1 else ''}"


def unsubscribe(url, sender_domains):
    """Try to unsubscribe through the page at url. (ok, detail)."""
    import requests
    from playwright.sync_api import Error as PlaywrightError, TimeoutError as PlaywrightTimeout, sync_playwright

    domains = allowed_domains(sender_domains)
    deadline = time.time() + TOTAL_SECONDS
    try:
        url = resolve(url, domains)
    except Refused as refused:
        return False, str(refused)
    except requests.RequestException as error:
        return False, f"could not open the link: {type(error).__name__}"
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        try:
            context = browser.new_context(user_agent=USER_AGENT, accept_downloads=False)
            page = context.new_page()
            context.on("page", lambda popup: popup.close())       # no new windows
            page.set_default_timeout(STEP_TIMEOUT_MS)
            page.set_default_navigation_timeout(NAVIGATION_TIMEOUT_MS)

            blocked = []

            def guard(route):
                request = route.request
                try:
                    top = request.is_navigation_request() and request.frame == page.main_frame
                except PlaywrightError:
                    top = False
                if top and not allowed(request.url, domains):
                    blocked.append(host_of(request.url))
                    return route.abort()
                return route.continue_()

            page.route("**/*", guard)
            page.goto(url, wait_until="domcontentloaded")
            return _click_through(page, domains, deadline, blocked)
        except Refused as refused:
            return False, str(refused)
        except PlaywrightTimeout:
            return False, "the page timed out"
        except PlaywrightError as error:
            return False, "browser: " + (str(error).splitlines() or ["error"])[0][:160]
        finally:
            browser.close()
