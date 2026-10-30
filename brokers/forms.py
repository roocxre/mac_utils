"""Web forms, in a headless Playwright browser with a profile of its own (brokers-browser in the
state folder): nothing from your own browser, and nothing of this one reaches it.

    selectors     a broker's own definition (registry `selectors`): each field must be found, or the
                  broker is set to `broken` and listed under registry maintenance. It never guesses.
    privacy managers  OneTrust, TrustArc and Osano request forms (privacy_manager)
    generic       for brokers with no definition: the opt-out page's "Do Not Sell" link, then a
                  privacy manager, then a form whose fields it can name; otherwise a manual task

Hard rules: it never solves a CAPTCHA, uploads an ID, pays, or creates an account. The moment a
page asks for one of those, it stops and a manual task says what's needed. It fills only the
details you approved. A required field it can't fill from those, or a box that would declare
residency somewhere, also stops it. Every run returns a Result.
"""
import re
import time
from contextlib import contextmanager
from dataclasses import dataclass, field

from brokers import watch
from lib import psl
from lib.common import state_path

PROFILE = state_path("brokers-browser")
# Dry runs (brokers/formcheck.py): everything up to the submit button, which is only named, never pressed.
DRY = False
# Hand-off (brokers/handoff.py): a check only a person may pass is waited for, in a visible window, not a stop.
HANDOFF = False
HANDOFF_WAIT = 300
# Every page load counts against its site, across all the agent's processes: a site that sees too many from
# one connection may block it, and you with it. A site that blocked us is left alone.
SITES = state_path("broker-sites.json")
PER_HOUR, PER_DAY = 8, 20
PLATFORM_SHARE = 5          # privacy portals (OneTrust…) host thousands of companies: their limit is this many times larger
BACK_OFF_DAYS = 7
STEP_MS = 15000
NAVIGATION_MS = 30000
TOTAL_SECONDS = 150
HTTPS_ONLY = True           # the tests serve local pages over http
USER_AGENT = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) "
              "Chrome/128.0 Safari/537.36")

CAPTCHA = ("iframe[src*='recaptcha'], iframe[src*='hcaptcha'], iframe[src*='challenges.cloudflare.com'], "
           "iframe[title*='captcha' i], .g-recaptcha, .h-captcha, .cf-turnstile, #challenge-form, [data-sitekey], "
           "#px-captcha, iframe[src*='arkoselabs'], iframe[src*='funcaptcha'], "
           "input[name*='captcha' i]:visible, input[id*='captcha' i]:visible, input[placeholder*='captcha' i]:visible, "
           "input[aria-label*='captcha' i]:visible, img[src*='captcha' i]:visible, img[alt*='captcha' i]:visible")
# (the last six: a code box to type into. A hidden token field, like invisible reCAPTCHA's, asks nothing of anyone.)
CAPTCHA_TEXT = re.compile(r"verify (?:that )?you(?:'re| are) (?:a )?human|are you a robot|just a moment\.\.\.|"
                          r"performing security verification|verifying you are human|"
                          r"checking your browser|press (?:&|and) hold|complete the security check|"
                          r"unusual traffic|access denied", re.IGNORECASE)
BLOCKED_TEXT = re.compile(r"access (?:has been )?denied|you (?:have )?lost your access|you have been blocked|"
                          r"request blocked|forbidden", re.IGNORECASE)
ID_UPLOAD = "input[type=file]"
# A page with nothing to fill that says why: the request needs a copy of your ID.
ID_TEXT = re.compile(r"(?:upload|attach|submit|provide|send)\b.{0,40}(?:image|copy|photo|scan) of (?:your |a )?"
                     r"(?:driver.?s licen[cs]e|government|state[- ]issued|photo id|passport|id\b)|"
                     r"government[- ]issued (?:photo )?id(?:entification)? (?:is )?required", re.IGNORECASE)
PAYMENT = ("input[autocomplete^='cc-'], input[name*='card' i], input[name*='cvv' i], input[name*='cvc' i], "
           "input[placeholder*='card number' i], iframe[src*='stripe'], iframe[src*='braintree']")
ACCOUNT = "input[type=password]"
# A sign-in or sign-up page without a password box: one-time codes and register buttons (an OTP portal).
ACCOUNT_BUTTON = re.compile(r"^\W*(?:register|sign ?up|create (?:an |your )?account|log ?in|sign ?in|"
                            r"(?:send|get|request|email) (?:me )?(?:an? |the )?(?:otp|one[- ]time|login|verification )?"
                            r"(?:otp|code|passcode|password)\b)", re.IGNORECASE)
CONFIRMED = re.compile(r"thank you|request (?:has been |was |is )?(?:received|submitted|processed|complete|"
                       r"confirmed|verified|in progress|being processed)|(?:email|address|request) (?:has been |was |is )?"
                       r"(?:verified|confirmed)|successfully (?:verified|confirmed)|\bsuccess(?:ful)?\b|"
                       r"(?:has|have) been (?:removed|suppressed|opted out|submitted|processed)|"
                       r"opt[- ]?out (?:request )?(?:is |has been )?(?:complete|submitted|received|successful)|"
                       r"we(?:'ve|’ve| have) (?:received|removed|processed)|successfully (?:submitted|removed|opted)",
                       re.IGNORECASE)
CHECK_EMAIL = re.compile(r"check your (?:e-?mail|inbox)|(?:sent|emailed) (?:you )?(?:a|an) (?:e-?mail|link|"
                         r"verification|confirmation)|verify your e-?mail|confirmation (?:e-?mail|link) (?:has been )?sent|"
                         r"click (?:on )?the (?:link|button) in (?:the|your|that) e-?mail", re.IGNORECASE)
RESIDENCY = re.compile(r"resident|residency|reside in|california|virginia|colorado|connecticut|texas|oregon|"
                       r"\bccpa\b|\bcpra\b|\bgdpr\b", re.IGNORECASE)
DO_NOT_SELL = re.compile(r"do not sell|don.t sell|your privacy choices|opt[- ]?out|delete (?:my|your) (?:data|"
                         r"information|info)|privacy request|data (?:subject )?request|remove (?:my|your) (?:info|"
                         r"information|listing|record)|suppression", re.IGNORECASE)
DELETE_KIND = re.compile(r"\bdelet|\beras|\bremov", re.IGNORECASE)
REQUEST_KIND = re.compile(r"\bdelet|\bremov|\berase|do not sell|don.t sell|opt[- ]?out|suppress", re.IGNORECASE)
SELF = re.compile(r"myself|\bmy own\b|\bconsumer\b|data subject|\bself\b|for me\b", re.IGNORECASE)
# Choices that make you someone else's representative ("authorized to act on behalf of the data subject"):
# never picked, even though they name the data subject too.
AGENT = re.compile(r"authori[sz]ed|on behalf|\bagent\b|representative|attorney|parent|guardian|power of", re.IGNORECASE)
# Fields about someone or something else (an agent, a company) or a detail that isn't one of yours to send
# (a middle name): left empty, and a required one is something the agent can't fill.
NOT_YOURS = re.compile(r"middle|company|business|organi[sz]ation|requestor|requester|\bagent\b|authori[sz]ed|"
                       r"employer|job ?title|captcha|code above|security code", re.IGNORECASE)
SUBMIT = re.compile(r"^\W*(?:submit|send|continue|next|request|opt[- ]?out|remove|delete|confirm|done)\b",
                    re.IGNORECASE)
# Privacy managers: the page's host -> name. Their forms ask the same few things everywhere.
MANAGERS = {"onetrust.com": "OneTrust", "trustarc.com": "TrustArc", "osano.com": "Osano",
            "datasubjectrequest.com": "Osano", "my.datagrail.io": "DataGrail", "datagrail.io": "DataGrail",
            "transcend.io": "Transcend", "ketch.com": "Ketch", "trustsuperset.com": "TrustSuperset",
            "securiti.ai": "Securiti", "fillout.com": "Fillout", "jotform.com": "Jotform"}
# What a field is, from its name, id, placeholder, label, aria-label and autocomplete together.
FIELD_WORDS = [("email", re.compile(r"e-?mail", re.I)), ("first_name", re.compile(r"first|given|fname|forename", re.I)),
               ("last_name", re.compile(r"last|surname|family|lname", re.I)),
               ("full_name", re.compile(r"full ?name|^name$|your name|\bname\b", re.I)),
               ("phone", re.compile(r"phone|mobile|\btel\b", re.I)), ("zip", re.compile(r"zip|postal", re.I)),
               ("city", re.compile(r"\bcity\b|town", re.I)), ("country", re.compile(r"country", re.I)),
               ("state", re.compile(r"\bstate\b|province|region", re.I)),
               ("address", re.compile(r"street|address(?! ?2)|addr1|line ?1", re.I)),
               ("dob", re.compile(r"birth|\bdob\b", re.I)),
               ("listing_url", re.compile(r"\burl\b|profile link|listing|link to", re.I))]
_DESCRIBE_FIELDS = """els => els.map((e, i) => {
  const lab = (e.labels && e.labels.length) ? Array.from(e.labels).map(l => l.innerText).join(' ') : '';
  const vis = !!(e.offsetWidth || e.offsetHeight || e.getClientRects().length);
  const shown = (lab || e.placeholder || e.getAttribute('aria-label') || e.name || e.id || '').replace(/\\s+/g, ' ').trim();
  return {i, tag: e.tagName.toLowerCase(), type: (e.type || '').toLowerCase(), name: e.name || '', id: e.id || '', shown,
          text: [e.name, e.id, e.placeholder, lab, e.getAttribute('aria-label'), e.autocomplete].filter(Boolean).join(' '),
          required: e.required || e.getAttribute('aria-required') === 'true', visible: vis && !e.disabled,
          checked: !!e.checked, value: e.value || ''};
})"""


@dataclass
class Result:
    ok: bool
    outcome: str                  # submitted, awaiting_confirmation, manual, broken, failed
    detail: str = ""
    manual: str = ""              # captcha, id, payment, account, field, residency, no-form
    sent: dict = field(default_factory=dict)     # {element: value} that went into the form
    url: str = ""
    needs: list = field(default_factory=list)    # field: the details it still needs, all of them yours to approve


class DryRun(Exception):
    """Dry mode reached the submit button: what it would have pressed."""

    def __init__(self, button):
        super().__init__(button)
        self.button = button


class Stop(Exception):
    def __init__(self, manual, detail, needs=()):
        super().__init__(detail)
        self.manual, self.detail, self.needs = manual, detail, list(needs)
        watch.say(f"stopped ({manual}): {detail}", "stop")


@contextmanager
def session(headless=True, profile=PROFILE):
    """A browser context on the agent's own profile (or another: two at once can't share one). Yields the context.
    A visible one (a hand-off) keeps the browser's own user agent: a made-up one draws more checks."""
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        context = p.chromium.launch_persistent_context(profile, headless=headless,
                                                       **({"user_agent": USER_AGENT} if headless else {}),
                                                       viewport={"width": 1280, "height": 1000}, locale="en-US",
                                                       accept_downloads=False)
        context.set_default_timeout(STEP_MS)
        context.set_default_navigation_timeout(NAVIGATION_MS)
        context.on("page", lambda page: _watched(context, page))     # the watch window: each page and its address
        try:
            yield context
        finally:
            context.close()


def _watched(context, page):
    watch.screencast(context, page)

    def navigated(frame):
        if frame == page.main_frame and frame.url.startswith("http"):
            watch.say(f"page: {frame.url}", "form", url=frame.url)
            _note(frame.url)

    page.on("framenavigated", navigated)


# ------------------------------------------------------------ how hard each site is used

def _sites(change=None):
    """The visit record ({site: {"visits": [times], "blocked_until": t}}), changed under a lock if asked."""
    import json
    from mail import store
    with store.file_lock("broker-sites"):
        try:
            with open(SITES, encoding="utf-8") as f:
                sites = json.load(f)
        except (OSError, ValueError):
            sites = {}
        if change:
            change(sites)
            with open(SITES + ".tmp", "w", encoding="utf-8") as f:
                json.dump(sites, f)
            import os
            os.replace(SITES + ".tmp", SITES)
    return sites


def _note(url):
    here, now = site(url), time.time()
    if here:
        def add(sites):
            entry = sites.setdefault(here, {})
            entry["visits"] = [t for t in entry.get("visits", []) if now - t < 86400] + [now]
        _sites(add)


def note_blocked(url, days=BACK_OFF_DAYS):
    here = site(url)
    if here:
        _sites(lambda sites: sites.setdefault(here, {}).update(blocked_until=time.time() + days * 86400))
        watch.say(f"{here} blocked us: the agent leaves it alone for {days} days", "warn")


def site_ok(url):
    """(True, "") if the agent may load a page of this site now; else (False, why)."""
    here, now = site(url), time.time()
    entry = _sites().get(here) or {}
    if entry.get("blocked_until", 0) > now:
        return False, (f"{here} blocked the agent's browser; it's left alone until "
                       f"{time.strftime('%b %-d', time.localtime(entry['blocked_until']))}")
    visits = entry.get("visits", [])
    share = PLATFORM_SHARE if here in MANAGERS else 1
    if sum(1 for t in visits if now - t < 3600) >= PER_HOUR * share or \
            len([t for t in visits if now - t < 86400]) >= PER_DAY * share:
        return False, f"{here} has had as many page loads from us as it should today; trying later"
    return True, ""


def site(url):
    return psl.registrable_domain(url or "") or ""


# The answer a passed check leaves in the page (Turnstile, reCAPTCHA, hCaptcha, a typed code).
PASSED = ("() => Array.from(document.querySelectorAll(\"[name='cf-turnstile-response'], [name='g-recaptcha-response'], "
          "[name='h-captcha-response'], input[name*='captcha' i], input[placeholder*='captcha' i]\"))"
          ".some(e => (e.value || '').trim().length > 0)")


def _texts(page):
    try:
        text = page.inner_text("body", timeout=5000)[:20000]
    except Exception:
        text = ""
    try:
        title = page.title()
    except Exception:
        title = ""
    return text, title


def _person_check(page, text, title):
    """Why the page wants a person (a check page, a CAPTCHA on the form), or None. In a hand-off, a check
    you've passed (its answer is in the page) no longer counts."""
    if (CAPTCHA_TEXT.search(text) or CAPTCHA_TEXT.search(title)) and len(text) < 3000:
        cloudflare = "cloudflare" in text.lower() or "just a moment" in title.lower()
        return "Cloudflare is checking whether you're a person" if cloudflare else \
            "the site is checking whether you're a person"
    if _captchas(page):
        try:
            if HANDOFF and page.evaluate(PASSED):
                return None
        except Exception:
            pass
        return "the page shows a CAPTCHA"
    return None


# Invisible reCAPTCHA (its badge, size=invisible) scores the visit and asks no one anything; if it does
# decide to show a challenge on submit, the check after submitting sees that one.
_ASKS = ("els => els.filter(e => !/size=invisible|grecaptcha-badge|invisible/i.test((e.getAttribute('src') || '') + ' ' "
         "+ (e.className || '') + ' ' + (e.getAttribute('data-size') || '')) && !e.closest('.grecaptcha-badge') "
         "&& !['BUTTON', 'INPUT', 'A'].includes(e.tagName)).length")      # a submit button wearing invisible reCAPTCHA


def _captchas(page):
    """How many person checks the page shows (not counting invisible reCAPTCHA)."""
    found = 0
    for selector in CAPTCHA.split(", "):
        try:
            found += page.locator(selector).evaluate_all(_ASKS)
        except Exception:
            continue
    return found


def _wait_for_person(page, why):
    """Hand-off: you pass the check in the visible window; the agent carries on once it's passed."""
    watch.say(f"your turn: {why}. Pass it in the browser window; the agent waits", "stop")
    try:
        page.bring_to_front()
    except Exception:
        pass
    deadline = time.time() + HANDOFF_WAIT
    while time.time() < deadline:
        page.wait_for_timeout(1000)
        try:
            page.wait_for_load_state("domcontentloaded", timeout=5000)
        except Exception:
            continue
        text, title = _texts(page)
        if text and not _person_check(page, text, title):
            watch.say("check passed: the agent takes it from here", "done")
            page.wait_for_timeout(1500)
            return
    raise Stop("captcha", f"{why}, and it wasn't passed within {HANDOFF_WAIT // 60} minutes")


def hard_stops(page):
    """Raise Stop if the page asks for anything the agent must never do. A check only a person may pass
    is a stop, or in a hand-off, a wait for you to pass it."""
    text, title = _texts(page)
    if BLOCKED_TEXT.search(title + " " + text[:600]) and len(text) < 3000:
        note_blocked(page.url)
        raise Stop("blocked", "the site has blocked the agent's browser (access denied)")
    why = _person_check(page, text, title)
    if why:
        if not HANDOFF:
            raise Stop("captcha", why)
        _wait_for_person(page, why)
        text, title = _texts(page)
    if page.locator(ID_UPLOAD).count():
        raise Stop("id", "the form asks for a document upload (an ID)")
    if page.locator(PAYMENT).count():
        raise Stop("payment", "the page asks for payment details")
    if page.locator(f"{ACCOUNT} >> visible=true").count():      # not a site's hidden header login
        raise Stop("account", "the page wants you to sign in or make an account")
    for button in page.locator("button:visible, input[type=submit]:visible, input[type=button]:visible").all()[:60]:
        try:
            label = (button.inner_text(timeout=1000) or button.get_attribute("value") or "").strip()
        except Exception:
            continue
        if ACCOUNT_BUTTON.search(label):     # buttons only: a "Log in" link in a site's header is no stop
            raise Stop("account", f"the page is a sign-in or sign-up (\"{label[:30]}\")")
    return text


def _tick(box):
    """Check a box, including one a site draws over with its own styling (the click lands on the drawing)."""
    try:
        box.check(timeout=4000)
    except Exception:
        box.check(force=True, timeout=4000)


def _fill(locator, value, tag):
    if tag == "select":
        options = locator.evaluate("e => Array.from(e.options).map(o => [o.value, o.text])")
        wanted = str(value).strip().lower()
        for option_value, text in options:
            if wanted in (option_value.strip().lower(), text.strip().lower()):
                locator.select_option(option_value)
                return
        raise Stop("field", f"the list has no option for {value!r}")
    try:
        locator.fill(str(value), timeout=5000)
    except Exception:
        locator.fill(str(value), force=True, timeout=5000)     # covered by the site's own styling


def _outcome(page, before_url, confirm=None):
    """After a submit: what the page says."""
    page.wait_for_timeout(2500)
    try:
        page.wait_for_load_state("networkidle", timeout=10000)
    except Exception:
        pass
    text = hard_stops(page)
    if CHECK_EMAIL.search(text):
        watch.say("the site says it sent a confirmation email", "done")
        return Result(True, "awaiting_confirmation", "the site sent a confirmation email", url=page.url)
    pattern = re.compile(confirm, re.IGNORECASE) if confirm else CONFIRMED
    if pattern.search(text):
        watch.say("the site confirmed the request", "done")
        return Result(True, "submitted", "the site confirmed the request", url=page.url)
    watch.say("no confirmation appeared after submitting", "warn")
    return Result(False, "broken", "no confirmation appeared after submitting", url=page.url)


# ------------------------------------------------------------ a broker's own selectors

def run_selectors(context, url, selectors, values, domains):
    """Fill the broker's fields from values ({element: value}) and submit. A selector that finds
    nothing makes the broker broken; nothing is guessed."""
    page = context.new_page()
    try:
        page.goto(url, wait_until="domcontentloaded")
        _stay(page, domains)
        hard_stops(page)
        sent = {}
        listing = selectors.get("listing")
        if listing and "listing_url" in values and not str(listing).startswith("TODO"):
            box = page.locator(f"{listing} >> visible=true").first
            if not box.count():
                return Result(False, "broken", f"the listing field {listing} is not on the page", url=page.url)
            box.fill(values["listing_url"].split("\n")[0])
            sent["listing_url"] = values["listing_url"].split("\n")[0]
            watch.say("filled the listing's address", "form")
        for css, element in (selectors.get("fields") or {}).items():
            if element not in values:
                raise Stop("field", f"the form needs your {element}, which you didn't approve")
            box = page.locator(f"{css} >> visible=true").first
            if not box.count():
                return Result(False, "broken", f"the field {css} is not on the page", url=page.url)
            _fill(box, values[element], box.evaluate("e => e.tagName.toLowerCase()"))
            sent[element] = values[element]
            watch.say(f"filled {element.replace('_', ' ')} into {css[:40]}", "form")
        submit = page.locator(f"{selectors['submit']} >> visible=true").first
        if not submit.count():
            return Result(False, "broken", f"the submit button {selectors['submit']} is not on the page", url=page.url)
        hard_stops(page)
        before = page.url
        if DRY:
            watch.say(f"would press {selectors['submit'][:40]} (dry run: not pressed)", "done")
            return Result(True, "dry", f"would press {selectors['submit'][:60]}", sent=sent, url=page.url)
        watch.say(f"pressed {selectors['submit'][:40]}", "form")
        submit.click()
        result = _outcome(page, before, selectors.get("confirm") if not str(selectors.get("confirm", "")).startswith("TODO")
                          else None)
        result.sent = sent
        return result
    except Stop as stop:
        return Result(False, "manual", stop.detail, manual=stop.manual, url=page.url)
    finally:
        page.close()


FORM_HOSTS = ("https://docs.google.com/forms/", "https://forms.gle/")      # brokers' own forms, hosted elsewhere


def _stay(page, domains):
    here = site(page.url)
    if page.url.startswith(FORM_HOSTS):
        return
    if domains and here not in domains and here not in MANAGERS and not any(here.endswith(m) for m in MANAGERS):
        raise Stop("no-form", f"the page went to {here}, which isn't the broker's")


# ------------------------------------------------------------ forms it has no definition for

def _kind(item):
    if NOT_YOURS.search(re.sub(r"\S+@\S+", "", item["text"])):     # "you@company.com" is a placeholder, not a company
        return "not_yours"
    if item["type"] == "email":
        return "email"
    if item["type"] == "tel":
        return "phone"
    for element, pattern in FIELD_WORDS:
        if pattern.search(item["text"]):
            return element
    return None


def fill_known_form(page, values):
    """Fill a form by what its fields say they are. Returns {element: value} sent. Stops on a required
    field it can't fill from the approved values, or a required box declaring residency."""
    hard_stops(page)
    boxes = page.locator("input, select, textarea")
    described = boxes.evaluate_all(_DESCRIBE_FIELDS)
    sent, filled_any, missing, unknown, chosen = {}, False, [], [], set()
    # One answer per radio group. Where the request type is one choice, deletion is the request.
    radios = boxes.evaluate_all("els => els.map(e => e.type === 'radio' ? [e.name || e.id, "
                                "((e.closest('label') || e.parentElement || {}).innerText || '') + ' ' + (e.id || '')] : null)")
    deletes = {r[0] for r in radios if r and DELETE_KIND.search(r[1])}
    shown = [item for item in described if item["visible"]]
    split_address = any(_kind(item) in ("city", "zip") for item in shown)
    for item in described:
        if not item["visible"] or item["type"] in ("hidden", "submit", "button", "image", "reset", "search"):
            continue
        box = boxes.nth(item["i"])
        if item["type"] in ("checkbox", "radio"):
            label = item["text"] + " " + (box.evaluate("e => (e.closest('label') || e.parentElement || {}).innerText || ''")
                                          or "")
            group = item["name"] or item["id"]
            if AGENT.search(label) or (item["type"] == "radio" and group in chosen):
                continue                    # someone acting for you, or this group already has its answer
            if item["type"] == "radio" and item["checked"]:
                chosen.add(group)
                continue
            if item["type"] == "radio" and REQUEST_KIND.search(label) and group in deletes \
                    and not DELETE_KIND.search(label):
                continue                    # the group has a deletion choice: that one
            if REQUEST_KIND.search(label) and not item["checked"]:
                _tick(box)
                if item["type"] == "radio":
                    chosen.add(group)
                watch.say(f"ticked “{' '.join(label.split())[:50]}”", "form")
            elif item["required"] and not item["checked"]:
                if RESIDENCY.search(label):
                    raise Stop("residency", f"the form wants you to declare: {' '.join(label.split())[:120]}")
                if item["type"] == "checkbox":
                    _tick(box)             # "I am the person named above" and the like
                    watch.say(f"ticked required “{' '.join(label.split())[:50]}”", "form")
                elif SELF.search(label):
                    _tick(box)             # "I am: the consumer / an authorized agent" -> the consumer
                    chosen.add(group)
                    watch.say(f"chose “{' '.join(label.split())[:60]}”", "form")
            continue
        element = _kind(item)
        if element == "country" and US_STATE & set(values):
            if _pick(box, item, COUNTRY_NAMES):         # the country your approved address or state is in
                watch.say("chose United States as the country", "form")
                continue
        if element == "address" and split_address and values.get("street"):
            _fill(box, values["street"], item["tag"])   # city, state and zip have boxes of their own
            sent["address"] = values["street"]
            watch.say(f"filled street address into “{item['shown'][:40]}”", "form")
            filled_any = True
            continue
        if element and element in values:
            if element == "state" and item["tag"] == "select":
                _fill(box, values["state"], "select")
            else:
                _fill(box, values[element], item["tag"])
            sent[element] = values[element]
            filled_any = True
            watch.say(f"filled {element.replace('_', ' ')} into “{item['shown'][:40]}”", "form")
        elif item["required"] and not item["value"]:
            if element == "state" and item["tag"] == "select" and values.get("state"):
                _fill(box, values["state"], "select")
                continue
            if item["tag"] == "select" and RESIDENCY.search(item["text"]):
                raise Stop("residency", "the form asks which state you live in, and you didn't approve sending it")
            if element and element not in ("country", "not_yours"):
                missing.append(element)
            else:
                unknown.append(item["shown"][:60] or "a field")
            continue
        if item["tag"] == "select" and REQUEST_KIND.search(" ".join(box.evaluate(
                "e => Array.from(e.options).map(o => o.text)"))):
            options = box.evaluate("e => Array.from(e.options).map(o => [o.value, o.text])")
            for option_value, text in options:
                if REQUEST_KIND.search(text):
                    box.select_option(option_value)
                    break
    if unknown:
        raise Stop("field", f"the form requires {', '.join(dict.fromkeys(unknown + missing))}, which it can't tell "
                            "what to put in")
    if missing:
        missing = list(dict.fromkeys(missing))
        raise Stop("field", f"the form also needs your {', '.join(missing)}, which you didn't approve for it",
                   needs=missing)
    if not filled_any:
        try:
            if ID_TEXT.search(page.inner_text("body", timeout=5000)[:30000]):
                raise Stop("id", "no form to fill here, and the page says the request needs a copy of your ID")
        except Stop:
            raise
        except Exception:
            pass
        raise Stop("no-form", "no form on the page it could fill")
    return sent


US_STATE = {"state", "city_state", "address"}
COUNTRY_NAMES = ("united states", "united states of america", "usa", "us", "u.s.", "u.s.a.")


def _pick(box, item, names):
    """Choose the option (or type the text) that is one of names. False if there's no such option."""
    if item["tag"] != "select":
        box.fill("United States")
        return True
    for option_value, text in box.evaluate("e => Array.from(e.options).map(o => [o.value, o.text])"):
        if text.strip().lower() in names or option_value.strip().lower() in names:
            box.select_option(option_value)
            return True
    return False


def _field_names(page):
    return set(page.evaluate("() => Array.from(document.querySelectorAll('input,select,textarea'))"
                             ".filter(e => e.type != 'hidden' && (e.offsetWidth || e.offsetHeight))"
                             ".map(e => e.name || e.id)"))


def _form_errors(page):
    """What a form says after a refused submit: its error messages and the browser's own checks."""
    try:
        found = page.evaluate("""() => {
          const out = [];
          document.querySelectorAll('[role=alert], .error, .errors, .field-validation-error, .invalid-feedback, '
            + '.validation-message, .error-message, [aria-live=assertive]').forEach(e => {
              const t = (e.innerText || '').trim(); if (t && (e.offsetWidth || e.offsetHeight)) out.push(t); });
          document.querySelectorAll('input:invalid, select:invalid, textarea:invalid').forEach(e => {
              if (e.offsetWidth || e.offsetHeight) out.push((e.name || e.id) + ': ' + e.validationMessage); });
          return out; }""")
    except Exception:
        return ""
    return "; ".join(dict.fromkeys(" ".join(t.split())[:80] for t in found))[:300]


def _submit(page):
    for candidate in page.locator("button, input[type=submit], [role=button]").all():
        try:
            text = (candidate.inner_text(timeout=2000) or candidate.get_attribute("value") or "").strip()
        except Exception:
            continue
        if SUBMIT.search(text) and not ACCOUNT_BUTTON.search(text) and candidate.is_visible() and candidate.is_enabled():
            if DRY:
                watch.say(f"would press “{text[:40]}” (dry run: not pressed)", "done")
                raise DryRun(text)
            watch.say(f"pressed “{text[:40]}”", "form")
            candidate.click()
            return text
    raise Stop("no-form", "no submit button")


def run_known_form(context, url, values, domains, steps=3):
    """A privacy-manager or plain form, filled by its labels, up to `steps` pages."""
    page = context.new_page()
    try:
        page.goto(url, wait_until="domcontentloaded")
        page.wait_for_timeout(1500)
        _stay(page, domains)
        sent = {}
        for _ in range(steps):
            sent.update(fill_known_form(page, values))
            before, fields = page.url, _field_names(page)
            try:
                _submit(page)
            except DryRun as dry:
                return Result(True, "dry", f"would press “{dry.button[:60]}”", sent=sent, url=page.url)
            result = _outcome(page, before)
            if result.ok:
                result.sent = sent
                return result
            if not page.locator("input:visible, select:visible").count():
                break
            if page.url == before and not (_field_names(page) - fields):
                said = _form_errors(page)       # the same form again: it didn't take it
                watch.say("the form didn't accept it" + (f": {said}" if said else ""), "warn")
                result = Result(False, "broken", "the form didn't accept it" + (f": {said}" if said else
                                                                                " (it said nothing)"), url=page.url)
                result.sent = sent
                return result
        result = Result(False, "broken", "no confirmation appeared", url=page.url)
        result.sent = sent
        return result
    except Stop as stop:
        return Result(False, "manual", stop.detail, manual=stop.manual, url=page.url, needs=stop.needs)
    finally:
        page.close()


def manager_of(url):
    host = site(url)
    return MANAGERS.get(host) or next((name for domain, name in MANAGERS.items() if host.endswith(domain)), None)


def generic(context, start_url, values, domains):
    """auto-identity-remove's order: the "Do Not Sell" link → a privacy manager → a form → manual."""
    if manager_of(start_url):
        return run_known_form(context, start_url, values, domains)
    page = context.new_page()
    try:
        page.goto(start_url, wait_until="domcontentloaded")
        page.wait_for_timeout(1500)
        _stay(page, domains)
        hard_stops(page)
        portal = next((f.url for f in page.frames[1:] if f.url.startswith("https") and manager_of(f.url)
                       and f.locator("input:visible, textarea:visible, select:visible").count()), None)
        if page.locator("form input:visible").count() >= 2:
            target = page.url
        elif portal:
            target = portal               # their privacy portal's form, held in a frame on the page
            watch.say(f"their form is a privacy portal in a frame: opening it ({manager_of(portal)})", "form")
        else:
            target = None
            for link in page.locator("a[href]").all()[:400]:
                try:
                    text = (link.inner_text(timeout=1000) or "").strip()
                    href = link.get_attribute("href") or ""
                except Exception:
                    continue
                if DO_NOT_SELL.search(text):
                    from urllib.parse import urljoin
                    absolute = urljoin(page.url, href)
                    secure = absolute.startswith("https://") or not HTTPS_ONLY
                    if secure and (site(absolute) in domains or manager_of(absolute)):
                        target = absolute
                        break
        if not target:
            return Result(False, "manual", "no opt-out form or link on the page", manual="no-form", url=page.url)
    except Stop as stop:
        return Result(False, "manual", stop.detail, manual=stop.manual, url=page.url)
    finally:
        page.close()
    return run_known_form(context, target, values, domains)


# ------------------------------------------------------------ confirmation links

def confirm_link(context, url, domains):
    """Open a confirmation link from the broker's email (replies.py has checked it is the broker's)
    and press its confirm button if the page has one. Never types anything."""
    fine, why = site_ok(url)
    if not fine:
        return Result(False, "failed", why, url=url)
    page = context.new_page()
    try:
        page.goto(url, wait_until="domcontentloaded")
        page.wait_for_timeout(2000)
        _stay(page, domains)
        text = hard_stops(page)
        if CONFIRMED.search(text) and not page.locator("form button:visible, form input[type=submit]:visible").count():
            return Result(True, "submitted", "confirmed by opening the link", url=page.url)
        for candidate in page.locator("button, input[type=submit], a[role=button], [role=button]").all():
            try:
                label = (candidate.inner_text(timeout=2000) or candidate.get_attribute("value") or "").strip()
            except Exception:
                continue
            if re.search(r"confirm|verify|remove|opt[- ]?out|yes", label, re.IGNORECASE) and candidate.is_visible():
                watch.say(f"pressed “{label[:40]}”", "form")
                candidate.click()
                return _outcome(page, page.url)
        if CONFIRMED.search(text):
            return Result(True, "submitted", "confirmed by opening the link", url=page.url)
        return Result(False, "broken", "the confirmation page said nothing it understood", url=page.url)
    except Stop as stop:
        return Result(False, "manual", stop.detail, manual=stop.manual, url=page.url)
    finally:
        page.close()


def run(entry, how, values, listing_url=None, context=None):
    """Carry out one approved web request. values: {element: value} limited to what you approved.
    context: a session already open (the dry runs reuse one); otherwise one is opened here."""
    start = entry.get("opt_out_url") or entry.get("website")
    fine, why = site_ok(start)
    if not fine:
        watch.say(why, "warn")
        return Result(False, "failed" if "as many page loads" in why else "manual", why,
                      manual="" if "as many page loads" in why else "blocked")
    if context is None:
        with session() as context:
            return run(entry, how, values, listing_url, context)
    domains = set(entry.get("domains") or []) | {site(entry.get("website"))}
    started = time.time()
    if how in ("direct-form", "search-form"):
        url = entry.get("opt_out_url") or entry.get("website")
        result = run_selectors(context, url, entry["selectors"], values, domains)
        if result.outcome == "broken" and "not on the page" in result.detail:
            watch.say("its saved form definition is out of date: reading the page itself", "warn")
            stale = result.detail
            result = generic(context, url, values, domains)
            result.detail += f" (its saved definition is out of date: {stale})"
    elif how == "privacy-manager":
        result = run_known_form(context, entry.get("opt_out_url"), values, domains)
    else:
        result = generic(context, entry.get("opt_out_url") or entry.get("website"), values, domains)
    result.detail += f" ({round(time.time() - started)} s)"
    return result
