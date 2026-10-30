"""The rules: what a message says about its sender, and which way a sender goes.

Per message (at capture): who sent it, how it can be unsubscribed from, whether its
authentication passed and lines up with the From domain, the sending platform, what its content
is (content.py: transactional, marketing or other), and a lookalike domain.
A sender's transactional mail is its own stream (sender_key), never proposed.

Per sender (at analysis): two scores from config.json.
    unwanted  how little you want it: deleted unread, never opened, archived unread, Promotions,
              left in Gmail's Spam folder (the Spam pass)…
    spam      how spammy it is: authentication fails, lookalike domain, free-mail bulk sender,
              no way to unsubscribe, marked as spam by you

    keep         allowlisted, you have written to it, mostly transactional, or not unwanted enough
    block        spam score at block_at
    unsubscribe  a valid sender (authentication lines up and it has an unsubscribe option) with
                 unwanted at unsubscribe_at, and at least min_volume emails
    unsure       everything in between, and anything with some transactional mail; you decide

A sender seen only in Spam (the Spam pass) is proposed only for unsubscribing, and only when that
is safe (valid, and not spammy enough to block). Anything else there is real spam: Gmail already
keeps it out, and unsubscribing would tell whoever sent it that the address works.

Mail through Apple's Hide My Email relay comes From the relay; the real sender's domain counts as
one of its signers (for following its unsubscribe link) when iCloud verified it (headers.apple_relay).
"""
import json
import os
import re
import statistics
from email.utils import getaddresses

from lib import psl
from mail import headers as hdr

CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")
_HOMOGLYPHS = str.maketrans({"0": "o", "1": "l", "3": "e", "5": "s", "4": "a", "7": "t", "8": "b"})


def config():
    from lib import settings
    return settings.load("unsub", CONFIG_PATH)


# ------------------------------------------------------------ one message

def list_unsubscribe(headers):
    """{"one_click", "mailto", "https"}: the List-Unsubscribe options ('' where absent)."""
    value = " ".join(hdr.values(headers, "List-Unsubscribe"))
    uris = [u.strip() for u in re.findall(r"<([^>]+)>", value)]
    https = next((u for u in uris if u.lower().startswith("https://")), "")
    mailto = next((u for u in uris if u.lower().startswith("mailto:")), "")
    post = " ".join(hdr.values(headers, "List-Unsubscribe-Post"))
    one_click = https if https and re.search(r"List-Unsubscribe\s*=\s*One-Click", post, re.IGNORECASE) else ""
    return {"one_click": one_click, "mailto": mailto, "https": https}


def list_id(headers):
    value = hdr.first(headers, "List-Id")
    match = re.search(r"<([^>]+)>", value)
    return (match.group(1) if match else value).strip().lower()[:200]


def display_name(headers):
    found = getaddresses(hdr.values(headers, "From"))
    return found[0][0].strip() if found else ""


def auth_facts(headers, from_domain):
    """auth: aligned (DMARC passed, or a passing DKIM signature from the From domain), unaligned
    (something passed, but not for the From domain), or fails."""
    auth = hdr.trusted_auth(headers)
    dkim = hdr.passing_dkim(auth)
    aligned = bool(from_domain) and (auth["dmarc"] == "pass" or from_domain in dkim)
    status = "aligned" if aligned else "unaligned" if (dkim or auth["spf"] == "pass") else "fails"
    return auth, {"auth": status, "dkim": dkim, "dmarc": auth["dmarc"] or None, "arc": auth["arc"] or None}


UNSUB_REQUEST_SUBJECT = re.compile(r"\b(?:unsubscribe|unsub|opt[- ]?out|remove me|take me off|stop (?:sending|emailing))\b",
                                   re.IGNORECASE)
UNSUB_REQUEST_ADDRESS = re.compile(r"^(?:[^@]*unsub[^@]*|leave[-+._][^@]*|leave|remove[^@]*|opt-?out[^@]*|"
                                   r"[^@]*-request|bounce[^@]*)@", re.IGNORECASE)


def unsubscribe_mailtos(conn):
    """Every address senders gave as their mailto: unsubscribe (lower-cased)."""
    found = set()
    for row in conn.execute("SELECT latest FROM senders"):
        uri = (json.loads(row["latest"] or "{}").get("unsub") or {}).get("mailto") or ""
        address = uri[len("mailto:"):].split("?")[0].strip().lower()
        if "@" in address:
            found.add(address)
    return found


def unsubscribe_request(message_id, headers, own_sent, mailtos):
    """True if a message you sent is an unsubscribe request (by the agent or by you), not correspondence.
    Those must never count as writing to someone or replying to them."""
    if message_id in own_sent:
        return True
    recipients = hdr.addresses(headers, "To", "Cc", "Bcc")
    if any(a in mailtos or UNSUB_REQUEST_ADDRESS.match(a) for a in recipients):
        return True
    return bool(UNSUB_REQUEST_SUBJECT.search(hdr.first(headers, "Subject")))


def _distance(a, b):
    previous = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        current = [i]
        for j, cb in enumerate(b, 1):
            current.append(min(previous[j] + 1, current[j - 1] + 1, previous[j - 1] + (ca != cb)))
        previous = current
    return previous[-1]


def lookalike(domain, name, aligned, cfg):
    """Why the From domain (or display name) imitates a well-known brand, or ''."""
    if not domain or domain in cfg["brand_lookalike_exceptions"]:
        return ""
    label = domain.split(".")[0]
    normal = label.replace("rn", "m").replace("vv", "w").translate(_HOMOGLYPHS)
    for brand in cfg["brands"]:
        if label == brand:
            continue
        if normal == brand or (len(brand) >= 5 and _distance(label, brand) == 1) \
                or any(form.startswith(brand + "-") or form.endswith("-" + brand) for form in (label, normal)):
            return f"{domain} imitates {brand}"
    if name and not aligned:
        words = re.findall(r"[a-z]+", name.lower())
        joined = {"".join(words[i:i + n]) for n in (1, 2, 3) for i in range(len(words))}
        for brand in cfg["brands"]:
            if brand in joined and label != brand:
                return f"named {brand} but sent from {domain}"
    return ""


def message_facts(headers, labels, text_links, cfg):
    """Everything the analysis needs from one message (its body aside)."""
    from unsub import body, content

    sender = hdr.sender(headers)
    from_domain = psl.registrable_domain(sender)
    auth, facts = auth_facts(headers, from_domain)
    relay = hdr.apple_relay(headers, auth)
    if relay:       # Hide My Email: its links are the real sender's, whose signature iCloud checked
        facts["relay_sender"] = relay[0]
        facts["dkim"] = facts["dkim"] + [relay[1]] if relay[1] not in facts["dkim"] else facts["dkim"]
    unsub = list_unsubscribe(headers)
    text, links = text_links
    unsub["link"] = body.unsubscribe_link(links, text)
    has_unsub = any(unsub.values())
    precedence = hdr.first(headers, "Precedence").strip().lower()
    platform = hdr.platform(headers, auth)
    subject = hdr.first(headers, "Subject")
    name = display_name(headers)
    imitates = lookalike(from_domain, name, facts["auth"] == "aligned", cfg)
    facts.update(
        sender=sender, name=name[:80], from_domain=from_domain, list_id=list_id(headers), unsub=unsub,
        platform=platform,
        bulk=bool(unsub["one_click"] or unsub["mailto"] or unsub["https"] or list_id(headers)
                  or precedence in ("bulk", "list", "junk") or platform),
        promotions="CATEGORY_PROMOTIONS" in labels,
        lookalike=imitates)
    # What the email says decides whether it is a receipt or an ad, not who sent it (content.py).
    # Phishing dresses up as receipts and security alerts, so only authentic mail can be transactional.
    kind = content.classify(subject, text, headers, labels, cfg, trusted=facts["auth"] != "fails" and not imitates)
    facts.update(content=kind["kind"], content_why=kind["why"], content_scores=[kind["transactional"], kind["marketing"]],
                 transactional=("; ".join(kind["why"]) or "transactional") if kind["kind"] == "transactional" else "")
    return facts


NOTICES = "#notices"
ADS = "#ads"
CATEGORY_NAMES = {"ads": "ad mail", "notices": "receipts and notices", "other": "other mail", "list": "list mail"}


def sender_key(facts):
    """A sender is one category of mail from one place, and every rule is about one of these.

    From address: its ads (ADS), its receipts and notices (NOTICES) and the rest are three senders.
    A mailing list (List-Id) is one, apart from any receipts it sends. So nothing decided about a
    business's ads, a keep, an unsubscribe or a block, ever touches its receipts or its other mail."""
    kind = facts.get("content")
    if facts.get("list_id"):
        return "list:" + facts["list_id"] + (NOTICES if kind == "transactional" else "")
    return "addr:" + (facts.get("sender") or "unknown") + {"transactional": NOTICES, "marketing": ADS}.get(kind, "")


def base_key(key):
    for suffix in (NOTICES, ADS):
        if key.endswith(suffix):
            return key[:-len(suffix)]
    return key


def category(key):
    """ads, notices, list or other: the category a sender key stands for."""
    if key.endswith(NOTICES):
        return "notices"
    if key.endswith(ADS):
        return "ads"
    return "list" if key.startswith("list:") else "other"


def label(facts, key):
    name = f"{facts['name']} <{facts['sender']}>" if facts.get("name") else facts.get("sender") or key
    suffix = {"ads": " · ads", "notices": " · receipts and notices"}.get(category(key), "")
    return name + suffix


def has_unsubscribe(latest):
    return any((latest.get("unsub") or {}).values())


# ------------------------------------------------------------ one sender

def features(s):
    n = max(1, s["total"])
    weeks = max(1.0, ((s["last_seen"] or 0) - (s["first_seen"] or 0)) / (7 * 86400))
    seconds = s["delete_seconds"]
    return {
        "n": s["total"], "opened": s["opened"], "deleted": s["deleted"], "deleted_unread": s["deleted_unread"],
        "archived": s["archived"], "starred": s["starred"], "replied": s["replied"], "spam_reports": s["spam_reports"],
        "open_rate": s["opened"] / n, "delete_unread_rate": s["deleted_unread"] / n,
        "archived_unread_rate": s["archived_unread"] / n, "promotions_rate": s["promotions"] / n,
        "bulk_rate": s["bulk"] / n, "transactional_rate": s["transactional"] / n, "aligned_rate": s["aligned"] / n,
        "auth_fail_rate": s["auth_fail"] / n, "per_week": s["total"] / weeks,
        "spam_folder": s.get("spam_folder") or 0, "spam_folder_rate": (s.get("spam_folder") or 0) / n,
        "median_delete_h": round(statistics.median(seconds) / 3600, 1) if seconds else None,
    }


def unwanted_score(f, cfg):
    w = cfg["unwanted"]
    score = (w["delete_unread_rate"] * f["delete_unread_rate"] + w["unopened_rate"] * (1 - f["open_rate"])
             + w["archived_unread_rate"] * f["archived_unread_rate"] + w["promotions_rate"] * f["promotions_rate"]
             + (w["bulk"] if f["bulk_rate"] >= 0.5 else 0) + (w["per_week_3_or_more"] if f["per_week"] >= 3 else 0)
             + w.get("spam_folder_rate", 0) * f["spam_folder_rate"]
             + (w["starred"] if f["starred"] else 0) + (w["replied"] if f["replied"] else 0))
    if f["median_delete_h"] is not None and f["median_delete_h"] <= w["quick_delete_hours"]:
        score += w["quick_delete"]
    return round(score, 2)


def spam_score(f, latest, from_domain, cfg):
    w = cfg["spam"]
    reasons, score = [], 0.0
    if f["auth_fail_rate"] >= 0.5:
        score += w["auth_fails"]
        reasons.append("authentication fails")
    elif f["aligned_rate"] < 0.5:
        score += w["auth_unaligned"]
        reasons.append("authentication not for the From domain")
    if latest.get("lookalike"):
        score += w["lookalike"]
        reasons.append(latest["lookalike"])
    if from_domain in cfg["freemail"] and f["bulk_rate"] >= 0.5:
        score += w["freemail_bulk"]
        reasons.append(f"bulk mail from a {from_domain} address")
    if not has_unsubscribe(latest):
        score += w["no_unsubscribe"]
        reasons.append("no unsubscribe option")
    if f["spam_reports"]:
        score += w["reported_spam"]
        reasons.append(f"you marked {f['spam_reports']} as spam")
    return round(score, 2), reasons


def classify(s, cfg, wrote_to=False, rule=None):
    """(verdict, scores and reasons). verdict: keep, unsubscribe, block or unsure."""
    f = features(s)
    latest = s["latest"]
    unwanted = unwanted_score(f, cfg)
    spam, spam_reasons = spam_score(f, latest, s["from_domain"], cfg)
    valid = latest.get("auth") == "aligned" and has_unsubscribe(latest)
    info = {"unwanted": unwanted, "spam": spam, "valid": valid, "spam_reasons": spam_reasons, "features": f}

    def out(verdict, why):
        info["why"] = why
        return verdict, info

    if rule and rule["kind"] == "allow":
        return out("keep", "on your allowlist")
    if wrote_to:
        return out("keep", "you have written to them")
    if f["transactional_rate"] >= cfg["transactional_keep_rate"]:
        return out("keep", "mostly transactional mail")
    enough = f["n"] >= cfg["min_volume"]
    mixed = f["transactional_rate"] > 0
    if f["spam_folder"] >= f["n"] and (not valid or spam >= cfg["block_at"]):
        return out("keep", "only seen in Spam, and no safe way to unsubscribe: Gmail already keeps it out")
    if spam >= cfg["block_at"]:
        return out("unsure" if mixed else "block", ("some mail looks transactional; " if mixed else "")
                   + "; ".join(spam_reasons))
    if valid:
        if enough and unwanted >= cfg["unsubscribe_at"]:
            return out("unsure" if mixed else "unsubscribe", "some mail looks transactional" if mixed else "unwanted")
        if enough and unwanted > cfg["keep_below"]:
            return out("unsure", "unwanted score between keep and unsubscribe")
        return out("keep", "wanted enough")
    if spam >= cfg["spam_unsure_at"]:
        return out("unsure", "; ".join(spam_reasons))
    if enough and unwanted >= cfg["unsubscribe_at"]:
        return out("unsure", "unwanted, but " + ("no unsubscribe option" if not has_unsubscribe(latest)
                                                 else "authentication does not line up"))
    return out("keep", "wanted enough" if unwanted <= cfg["keep_below"] else "too little mail to judge")


def evidence(s, info, this_week):
    """One line a person can check, e.g. '14 emails (3 this week), 0 opened, 11 deleted unread (median 2h)'."""
    f, latest = info["features"], s["latest"]
    parts = [f"{f['n']} email{'s' if f['n'] != 1 else ''}" + (f" ({this_week} this week)" if this_week != f["n"] else ""),
             f"{f['opened']} opened"]
    if f["deleted_unread"]:
        parts.append(f"{f['deleted_unread']} deleted unread"
                     + (f" (median {f['median_delete_h']}h)" if f["median_delete_h"] is not None else ""))
    elif f["deleted"]:
        parts.append(f"{f['deleted']} deleted")
    if f["archived"]:
        parts.append(f"{f['archived']} archived")
    if f["spam_folder"]:
        parts.append(f"{f['spam_folder']} in Spam")
    if f["starred"]:
        parts.append(f"{f['starred']} starred")
    if f["replied"]:
        parts.append(f"replied {f['replied']}x")
    parts.append({"aligned": "auth ok", "unaligned": "auth not for From domain", "fails": "auth fails"}.get(
        latest.get("auth"), "auth unknown"))
    methods = [name for name, key in (("one-click", "one_click"), ("mailto", "mailto"), ("link", "https"))
               if (latest.get("unsub") or {}).get(key)]
    if not (latest.get("unsub") or {}).get("https") and (latest.get("unsub") or {}).get("link"):
        methods.append("body link")
    parts.append("unsubscribe: " + "/".join(methods) if methods else "no unsubscribe")
    if latest.get("platform"):
        parts.append(f"via {latest['platform']}")
    for reason in info.get("spam_reasons", []):
        if reason not in ("authentication fails", "authentication not for the From domain", "no unsubscribe option"):
            parts.append(reason)
    return ", ".join(parts)

