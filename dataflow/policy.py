"""Privacy policies: find one, read it, and say what it says about each kind of data, quoting it.

1. Finding it: the links content.js saw in the page's footer and header ("Privacy Policy",
   "Datenschutz"…), then the usual paths (/privacy, /privacy-policy, /legal/privacy,
   /policies/privacy-policy…). A page counts if it has 300+ words about personal information.
   A page that only renders with JavaScript is read in headless Chromium instead.
2. The main text (a readability-style pass: no scripts, navigation, headers, footers or forms),
   split into paragraph-level passages under their headings.
3. PrivBERT fine-tuned on OPP-115, when that optional model is installed, labels each passage's
   categories (first-party collection, third-party sharing, retention, choice…) and
   personal-information types (contact, financial, demographic…). Without it every passage
   counts, and the quoted passages are the ones the findings' keywords pick.
4. On the sharing, collection, retention and choice passages, one sentence at a time, the NLI
   model answers four fixed questions: sells personal information, shares it for advertising,
   shares it with data brokers, lets you opt out. Keywords pick the sentences worth asking
   about ("sale" near "information"…); "says no" needs the denial at the verb ("we do not sell").
5. Named recipients: organisation names (NER) in the sharing passages, matched against the
   tracker lists' owners and the broker registry.

Cached per site: re-read after 30 days, re-analysed only when the text's hash changed.
"""
import hashlib
import json
import re
import time
from html.parser import HTMLParser
from urllib.parse import quote

from dataflow import entities, lists, models, store

COMMON_PATHS = ("/privacy", "/privacy-policy", "/privacy-notice", "/legal/privacy", "/legal/privacy-policy",
                "/policies/privacy-policy", "/privacy.html", "/privacypolicy", "/about/privacy", "/help/privacy",
                "/pages/privacy-policy", "/en/privacy", "/us/privacy")
USER_AGENT = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) "
              "Chrome/140.0.0.0 Safari/537.36 Edg/140.0.0.0")
KEEP = ("First Party Collection/Use", "Third Party Sharing/Collection", "Data Retention", "User Choice/Control")
# A field's data type -> the OPP-115 personal-information types that cover it.
TYPE_MAP = {"email": ("Contact",), "name": ("Contact", "Personal identifier"), "phone": ("Contact",),
            "address": ("Contact", "Location"), "birthday": ("Demographic",), "payment": ("Financial",),
            "password": ("User profile",), "other": ()}
FINDINGS = {
    "sells": {"hypotheses": ("We sell your personal information.", "We may sell your personal information."),
              # selling *your* information, or CCPA's quoted "sale": not "the sale of products"
              "keywords": r"\b(sell|sells|sold|selling|sale of)\b\W+(\w+\W+){0,4}?(your|personal|consumer|customer|"
                          r"user)\b|\b(personal|your)\s+(information|data)\b\W+(\w+\W+){0,6}?(sell|sells|sold|sale)\b|"
                          r"[“\"']s(ell|ells|ale|old|elling)[”\"']"},
    "shares_ads": {"hypotheses": ("We share your personal information with third parties for advertising.",
                                  "We may share your personal information with advertising partners."),
                   "keywords": r"advertis|\bads?\b|marketing partners|interest-based|targeted"},
    "shares_brokers": {"hypotheses": ("We share your personal information with data brokers.",
                                      "We may sell or share your personal information with data brokers."),
                       "keywords": r"data broker|information broker|brokers\b|resell|aggregators?\b|list rental|"
                                   r"data cooperative|data co-op|rent (or sell |or share )?your"},
    "opt_out": {"hypotheses": ("You may opt out of the sale of your personal information.",
                               "You can opt out of the sharing of your personal information."),
                "keywords": r"opt[- ]?out|do not sell|do not share|your (privacy )?choices|unsubscribe|object to"},
}
# Boilerplate that mentions selling without being about selling your data: mergers and the like.
NOT_ABOUT_SELLING = re.compile(r"merger|acquisition|bankruptcy|reorgani[sz]ation|receivership|sale of (all|some|"
                               r"substantially|part|the company|company|its|our)|business transfer|asset sale|"
                               r"sale of assets|change of control|divestiture", re.IGNORECASE)
# "Says no" needs the negation right at the verb: "we do not sell", "will never share", "don't rent".
DENIAL = re.compile(r"\b(not|never|no longer)\b(\W+\w+){0,2}?\W+(sell|sold|share|rent|disclose|trade|provide)|"
                    r"n't\W+(\w+\W+){0,2}?(sell|sold|share|rent|disclose|trade|provide)", re.IGNORECASE)
_SENTENCE = re.compile(r"(?<=[.!?;])\s+(?=[A-Z(“\"])")


# ------------------------------------------------------------ reading a page

class _Blocks(HTMLParser):
    """Text blocks in reading order: ("h", heading) or ("p", paragraph). Skips page furniture."""
    SKIP = {"script", "style", "noscript", "nav", "header", "footer", "aside", "form", "button", "select", "svg",
            "template", "iframe", "object", "canvas"}
    BLOCK = {"p", "li", "td", "th", "dd", "dt", "div", "section", "article", "main", "blockquote", "pre", "br", "tr",
             "table", "ul", "ol", "body", "h1", "h2", "h3", "h4", "h5", "h6"}
    HEADINGS = {"h1", "h2", "h3", "h4", "h5", "h6"}
    VOID = {"br", "img", "input", "meta", "link", "hr", "source", "wbr", "area", "col", "embed", "param", "track"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.blocks, self.buffer, self.skip, self.heading = [], [], 0, 0
        self.stack = []

    def _flush(self):
        text = re.sub(r"\s+", " ", "".join(self.buffer)).strip()
        self.buffer = []
        if text:
            self.blocks.append(("h" if self.heading else "p", text))

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        hidden = "hidden" in attrs or attrs.get("aria-hidden") == "true" or \
            re.search(r"cookie|consent|banner|modal|newsletter", (attrs.get("id") or "") + " " +
                      (attrs.get("class") or ""), re.IGNORECASE) and tag in ("div", "section", "aside")
        if tag in self.VOID:
            if tag == "br":
                self.buffer.append(" ")
            return
        self.stack.append((tag, bool(tag in self.SKIP or hidden)))
        if tag in self.SKIP or hidden:
            self.skip += 1
        if not self.skip and tag in self.BLOCK:
            self._flush()
        if tag in self.HEADINGS and not self.skip:
            self.heading += 1

    def handle_endtag(self, tag):
        if tag in self.VOID:
            return
        while self.stack:
            open_tag, skipping = self.stack.pop()
            if skipping:
                self.skip -= 1
            if open_tag in self.HEADINGS and not self.skip:
                self._flush()
                self.heading = max(0, self.heading - 1)
            if open_tag == tag:
                break
        if not self.skip and tag in self.BLOCK:
            self._flush()

    def handle_data(self, data):
        if not self.skip:
            self.buffer.append(data)

    def close(self):
        super().close()
        self._flush()


def blocks_of(html):
    parser = _Blocks()
    try:
        parser.feed(html)
        parser.close()
    except Exception:
        pass
    return parser.blocks


def looks_like_policy(blocks):
    text = " ".join(t for _k, t in blocks).lower()
    words = len(text.split())
    return words >= 300 and "privacy" in text and any(k in text for k in (
        "personal information", "personal data", "information we collect", "third part", "cookies"))


def _fetch(url, log=None):
    import requests
    try:
        response = requests.get(url, timeout=20, headers={"User-Agent": USER_AGENT, "Accept-Language": "en-US,en;q=0.8"},
                                allow_redirects=True)
    except Exception as error:
        if log:
            log.info("policy fetch %s: %s", url, type(error).__name__)
        return None, None
    if response.status_code != 200 or "html" not in response.headers.get("Content-Type", "html"):
        return None, None
    response.encoding = response.encoding or response.apparent_encoding
    return response.url, response.text


def _render(url):
    """The page as headless Chromium shows it (policies that only render with JavaScript)."""
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        try:
            page = browser.new_page(user_agent=USER_AGENT)
            page.goto(url, timeout=30000, wait_until="domcontentloaded")
            page.wait_for_timeout(3000)
            return page.url, page.content()
        finally:
            browser.close()


def find_policy(site, candidates, log=None):
    """(url, blocks) for the site's privacy policy, or (None, None)."""
    tried, rendered = set(), False
    urls = list(candidates or [])
    for host in (site, "www." + site):
        urls += [f"https://{host}{p}" for p in COMMON_PATHS]
    for url in urls[:18]:
        if url in tried:
            continue
        tried.add(url)
        final, html = _fetch(url, log)
        if not html:
            continue
        blocks = blocks_of(html)
        if looks_like_policy(blocks):
            return final, blocks
        # A link we were given but nearly empty: probably drawn by JavaScript. One render.
        if url in (candidates or []) and not rendered and len(" ".join(t for _k, t in blocks).split()) < 300:
            rendered = True
            try:
                final, html = _render(url)
                blocks = blocks_of(html)
                if looks_like_policy(blocks):
                    return final, blocks
            except Exception as error:
                if log:
                    log.info("policy render %s: %s", url, error)
    return None, None


# ------------------------------------------------------------ passages

def passages_of(blocks, max_words=160):
    """Paragraph-level passages, each with its heading."""
    out, heading, current = [], "", []

    def close():
        if current:
            text = " ".join(current).strip()
            if len(text.split()) >= 8:
                out.append({"heading": heading, "text": text})
        current.clear()

    for kind, text in blocks:
        if kind == "h":
            close()
            heading = text[:150]
            continue
        words = text.split()
        if len(words) > max_words * 2:              # one huge block: split by sentences
            close()
            chunk = []
            for sentence in _SENTENCE.split(text):
                chunk.append(sentence)
                if len(" ".join(chunk).split()) >= max_words:
                    current.extend(chunk)
                    close()
                    chunk = []
            current.extend(chunk)
            close()
            continue
        current.append(text)
        if len(" ".join(current).split()) >= max_words * 0.6:
            close()
    close()
    return out


def fragment_link(url, text):
    """A link that scrolls to and highlights the passage (a text fragment)."""
    words = text.split()
    enc = lambda s: quote(s, safe="").replace("-", "%2D")    # noqa: E731
    start = " ".join(words[:6])
    end = " ".join(words[-4:]) if len(words) > 12 else ""
    return f"{url.split('#')[0]}#:~:text={enc(start)}" + (f",{enc(end)}" if end else "")


def sentences_with(text, keywords):
    """The sentences a keyword appears in (a very short one keeps the next sentence for context)."""
    sentences = [x.strip() for x in _SENTENCE.split(text) if x.strip()]
    out = []
    for i, sentence in enumerate(sentences):
        if keywords.search(sentence):
            if len(sentence.split()) < 10 and i + 1 < len(sentences):
                sentence += " " + sentences[i + 1]
            out.append(sentence[:1200])
    return out


# ------------------------------------------------------------ analysis

def analyze(passages, url, cfg=None, site=None):
    """{passages: [...], findings: {...}, recipients: [...]}"""
    from lib import psl
    cfg = cfg or lists.config()
    site = site or psl.registrable_domain(url)
    threshold = cfg["privbert"]["threshold"]
    labelled = []
    if models.available(cfg["privbert"]["model"]):
        scores = models.privbert([(p["heading"] + ". " if p["heading"] else "") + p["text"] for p in passages])
    else:
        scores = [{} for _ in passages]
    for passage, score in zip(passages, scores):
        labels = [k for k, v in score.items() if not k.startswith("type:") and v >= threshold]
        types = [k[5:] for k, v in score.items() if k.startswith("type:") and v >= threshold]
        labelled.append({"heading": passage["heading"], "text": passage["text"], "labels": labels, "types": types,
                         "link": fragment_link(url, passage["text"])})
    kept = [p for p in labelled if any(label in KEEP for label in p["labels"])] or labelled
    findings = {}
    nli_model = cfg["nli"]["model"]
    if models.available(nli_model):
        for key, spec in FINDINGS.items():
            findings[key] = _finding(kept, spec, cfg, url, site)
    recipients = _recipients([p for p in kept if "Third Party Sharing/Collection" in p["labels"]] or kept, site)
    return {"url": url, "words": sum(len(p["text"].split()) for p in passages), "passages": labelled,
            "findings": findings, "recipients": recipients}


def _finding(passages, spec, cfg, url, site=""):
    keywords = re.compile(spec["keywords"], re.IGNORECASE)
    # A "yes" has to be about the site itself, not "other companies track you…".
    name = re.escape(site.split(".")[0]) if site else "(?!x)x"
    about_us = re.compile(rf"\b(we|us|our|{name})\b", re.IGNORECASE)
    best_yes, best_no = (0.0, None), (0.0, None)
    asked = 0
    for passage in passages:
        for sentence in sentences_with(passage["text"], keywords):
            if asked >= 120:
                break
            asked += 1
            scores = models.nli(sentence, {str(i): h for i, h in enumerate(spec["hypotheses"])}, cfg["nli"]["model"])
            entail = max(x[0] for x in scores.values())
            contra = max(x[1] for x in scores.values())
            denied = DENIAL.search(sentence)
            if spec is FINDINGS["sells"] and NOT_ABOUT_SELLING.search(sentence):
                continue
            if entail > best_yes[0] and not denied and about_us.search(sentence):
                best_yes = (entail, sentence)
            if contra > best_no[0] and denied:
                best_no = (contra, sentence)
    if best_yes[0] >= cfg["nli"]["yes"]:
        return {"verdict": "yes", "p": round(best_yes[0], 2), "quote": best_yes[1][:500],
                "link": fragment_link(url, best_yes[1])}
    if best_no[0] >= cfg["nli"]["no"]:
        return {"verdict": "no", "p": round(best_no[0], 2), "quote": best_no[1][:500],
                "link": fragment_link(url, best_no[1])}
    return {"verdict": "unclear", "asked": asked}


COMPANY = re.compile(r"\b(inc|llc|ltd|limited|corp|corporation|co|company|gmbh|plc|technologies|labs|media|group|"
                     r"holdings|networks|systems|software|analytics|solutions)\b\.?$", re.IGNORECASE)


def _recipients(passages, site=""):
    """Named organisations in sharing passages that the lists know, or that are named as a company."""
    from lib import psl
    if not models.available("bert-base-NER"):
        return []
    data = entities.index()
    owners = {}
    for domain, (owner, categories, broker) in data["domains"].items():
        if owner:
            owners.setdefault(lists.normalize(owner), (owner, broker, domain))
    brokers = {lists.normalize(name): b for b, name in data["brokers"].items() if name}
    own = {lists.normalize(site.split(".")[0])}
    site_owner = (data["domains"].get(site) or [None])[0]
    if site_owner:
        own.add(lists.normalize(site_owner))
    found = {}
    for passage in passages[:40]:
        for name in models.organisations(passage["text"][:2000]):
            key = lists.normalize(name)
            if not key or len(key) < 3 or "@" in name or key in own or key.split()[0] in own:
                continue
            item = {"name": name}
            if key in owners:
                item["owner"], broker, item["domain"] = owners[key]
                if broker:
                    item["broker"] = broker
                if item["domain"] and psl.registrable_domain(item["domain"]) == site:
                    continue
            if key in brokers:
                item["broker"] = brokers[key]
            if item.get("owner") or item.get("broker") or COMPANY.search(name):
                found.setdefault(key, item)
    return sorted(found.values(), key=lambda i: (not i.get("broker"), not i.get("owner"), i["name"]))[:30]


def for_data_type(passages, data_type):
    """The passages about one data type (or all), sharing first. Unlabelled (no PrivBERT): the
    passages the findings' keywords pick."""
    wanted = TYPE_MAP.get(data_type or "", ())
    labelled = any(p["labels"] for p in passages)
    keywords = re.compile("|".join(f"(?:{spec['keywords']})" for spec in FINDINGS.values()), re.IGNORECASE)
    out = []
    for p in passages:
        keep = any(label in KEEP for label in p["labels"]) if labelled else keywords.search(p["text"])
        if not keep:
            continue
        if wanted and p.get("types") and not (set(wanted) & set(p["types"])) and \
                "Generic personal information" not in p["types"]:
            continue
        out.append(p)
    order = {label: i for i, label in enumerate(("Third Party Sharing/Collection", "First Party Collection/Use",
                                                  "User Choice/Control", "Data Retention"))}
    return sorted(out, key=lambda p: min((order.get(label, 9) for label in p["labels"]), default=9))


# ------------------------------------------------------------ the job

def analyze_site(site, log=None):
    cfg = lists.config()
    now = time.time()
    with store.db() as conn:
        row = conn.execute("SELECT * FROM policies WHERE site = ?", (site,)).fetchone()
        conn.execute("UPDATE policies SET status = 'running', started_at = ? WHERE site = ?", (now, site))
    candidates = store.loads(row["candidates"], []) if row else []
    url, blocks = find_policy(site, candidates, log)
    if not url:
        with store.db() as conn:
            conn.execute("UPDATE policies SET status = 'missing', error = ?, fetched_at = ?, analyzed_at = ? WHERE site = ?",
                         ("no privacy policy found at the links on the page or the usual addresses", now, now, site))
        return site
    passages = passages_of(blocks)
    text_hash = hashlib.sha256("\n".join(p["text"] for p in passages).encode()).hexdigest()
    if row and row["text_hash"] == text_hash and row["result"]:
        with store.db() as conn:
            conn.execute("UPDATE policies SET status = 'ok', url = ?, fetched_at = ?, analyzed_at = ?, error = NULL "
                         "WHERE site = ?", (url, now, now, site))
        return site
    signals = store.loads(row["signals"], {}) if row else {}
    whole = " ".join(p["text"] for p in passages).lower()
    signals["policy_mentions_do_not_sell"] = "do not sell" in whole or "do not share" in whole
    started = time.time()
    result = analyze(passages, url, cfg, site)
    result["seconds"] = round(time.time() - started, 1)
    with store.db() as conn:
        conn.execute("UPDATE policies SET status = 'ok', url = ?, text_hash = ?, signals = ?, result = ?, fetched_at = ?, "
                     "analyzed_at = ?, error = NULL WHERE site = ?",
                     (url, text_hash, json.dumps(signals), json.dumps(result), now, time.time(), site))
    if log:
        log.info("policy %s: %d passages, %s, %.1fs", site, len(passages),
                 {k: v.get("verdict") for k, v in result["findings"].items()}, result["seconds"])
    models.unload()
    return site
