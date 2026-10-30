"""Broker mail. The mail poller queues it for this agent (mail.poller BROKER_AGENT): replies in the
threads of requests it sent, and mail from any domain a broker uses.

capture() runs inside the mail poller (every 5 minutes): it reads each message once, works out
which broker it is from, and keeps it in the vault. handle() runs in the agent's pass: it sorts
each reply and acts on it.

Matching, best evidence first: the request's own thread, then a DKIM signature or From on one of a
broker's domains.

Sorting: rules first (bounces, auto-replies, "we received your request", confirmation links, each
outcome's usual phrases), then the NLI model's fixed hypotheses (nli.py). Below the threshold, or
when a reply asks for something the rules can't pin down, it is a manual task.

A confirmation link is opened only when the email passes DKIM for one of the broker's domains, the
link is on one of the broker's domains, and a request to that broker is open.

A broker asking for more: in standing mode, when everything it asks for is already in what you
approved, the follow-up template answers with just those details. Otherwise it waits for your
approval. A request for ID is always a manual task (with the reminder to redact it).
"""
import json
import re
import time

from brokers import AGENT, forms, held, plan, registry, scan, vault, watch
from lib import psl
from mail import headers as hdr

BOUNCE_FROM = re.compile(r"^(?:mailer-daemon|postmaster)@", re.IGNORECASE)
BOUNCE_SUBJECT = re.compile(r"delivery status notification|undeliverable|undelivered|mail delivery (?:failed|"
                            r"subsystem)|returned mail|delivery (?:has )?failed|address not found", re.IGNORECASE)
DELAYED = re.compile(r"\(delay\)|delivery (?:is )?delayed|delivery incomplete|will (?:retry|keep trying)|"
                     r"hasn.t been delivered .{0,40} yet", re.IGNORECASE)
AUTO_SUBJECT = re.compile(r"automatic reply|auto[- ]?reply|out of (?:the )?office|autoreply", re.IGNORECASE)
RECEIVED = re.compile(r"(?:we(?:'ve|’ve| have)|has been|was) (?:successfully )?received|thank you for (?:your|contacting|"
                      r"submitting)|confirmation of receipt|ticket (?:number|#|id)|case (?:number|#)|request (?:number|id)|"
                      r"will (?:respond|get back|be processed) within|within \d+ (?:business )?days|we will (?:process|"
                      r"review) your (?:request|email)|will be reviewed by", re.IGNORECASE)
# Words a confirmation email uses. "Verify your identity" and "confirmation of receipt" are not.
CONFIRM_WORDS = re.compile(r"confirm (?:your|the|this) (?:e-?mail|request|opt[- ]?out|address|removal|submission|privacy)|"
                           r"verify (?:your |the )?(?:e-?mail|opt[- ]?out|request|address|privacy|removal)|"
                           r"click (?:on )?(?:the |this )?(?:link|button) below|by clicking|follow the link below|"
                           r"verify that you (?:are|own)|"
                           r"complete your (?:\S+ )?(?:opt[- ]?out|request|removal)", re.IGNORECASE)
# "This mailbox doesn't take requests: use our form / this other address."
REDIRECT = re.compile(r"(?:is|are) not (?:actively )?monitored for (?:data subject|privacy|dsr|deletion|opt|requests)|"
                      r"no longer (?:actively )?monitored|(?:please )?submit (?:your|a|the) (?:\w+ )?request (?:through|"
                      r"via|using|at|on|by)|make your request at|fill out the (?:provided|online|web|following) form|"
                      r"(?:please )?visit our (?:web ?form|online form|privacy (?:center|portal)|opt[- ]?out page)",
                      re.IGNORECASE)
REPLY_WITH = re.compile(r"(?:respond|reply) (?:to this e-?mail )?with", re.IGNORECASE)
ADDRESS = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
CONFIRM_LINK = re.compile(r"confirm|verif|optout|opt-out|remov|suppress|token=|validate", re.IGNORECASE)
# Each outcome's usual wording; matched before the model is asked.
PHRASES = {
    "needs_id": re.compile(r"(?:government|photo|state)[- ]?issued (?:photo )?id|driver.?s? licen[cs]e|passport|"
                           r"copy of (?:your|an?) (?:id|identification)|proof of identity", re.IGNORECASE),
    "refused_residency": re.compile(r"(?:only|solely) (?:available|applies?|apply|extended) to (?:residents|consumers)|"
                                    r"(?:not|aren.t) a (?:\w+ )?resident|residents of (?:the state of )?\w+ only|"
                                    r"unable to verify (?:your )?residency|not (?:a )?(?:california|virginia|colorado) |"
                                    r"(?:live|lives|reside|resides) in a state (?:that|which) (?:does not|doesn.t) have|"
                                    r"only (?:offer|honou?r|extend|provide) (?:these |privacy )?(?:rights|requests) to "
                                    r"(?:residents|consumers)",
                                    re.IGNORECASE),
    "no_record": re.compile(r"(?:could not|couldn.t|were unable to|did not|didn.t|unable to) (?:find|locate|identify|"
                            r"match)|no (?:records?|information|data|profile|listing)s? (?:was |were )?(?:found|"
                            r"associated|on file|matching)|do(?:es)? not (?:have|hold|maintain) any|(?:is|are) not in our "
                            r"(?:system|database|records|files)", re.IGNORECASE),
    "completed": re.compile(r"(?:has|have) been (?:removed|deleted|suppressed|opted out|processed|honou?red|completed)|"
                            r"we(?:'ve|’ve| have) (?:removed|deleted|suppressed|processed|completed|honou?red)|"
                            r"opt[- ]?out (?:request )?(?:is|has been|was) (?:complete|processed|successful)|"
                            r"(?:is|was) (?:now )?(?:complete|completed)\b|request has been approved|"
                            r"we(?:'ve|’ve| have) accepted your (?:opt[- ]?out|request)|"
                            r"(?:are|is|been) removed from our (?:system|database|records|site|search)", re.IGNORECASE),
    "refused_other": re.compile(r"(?:unable|cannot|can.t|not able|decline) to (?:honou?r|process|fulfill|comply|"
                                r"complete)|(?:is|are) exempt|we (?:must|will) (?:retain|keep)", re.IGNORECASE),
}
ASKS = re.compile(r"please (?:provide|send|reply with|include|confirm|respond (?:to this email )?with)|we (?:need|require)|"
                  r"in order to (?:process|locate|verify)|(?:need|require) (?:your|the following|additional|more)|"
                  r"you must provide", re.IGNORECASE)
# What a broker can ask for, by the words it uses.
ASKED = [("dob", r"date of birth|birth ?date|\bdob\b"), ("phone", r"phone|telephone|mobile"),
         ("address", r"(?:mailing|postal|home|street|current|full) address"), ("zip", r"zip|postal code"),
         ("listing_url", r"(?:profile|listing|record)(?:'s)? (?:url|link|address)|url of|link to (?:your|the)"),
         ("full_name", r"full (?:legal )?name"), ("email", r"e-?mail address"), ("age", r"\bage\b"),
         ("city_state", r"city and state|city/state|state of residence|state (?:that |which |where |in which )?you "
                        r"(?:live|reside)")]
# Help desks and privacy portals that send mail for many companies: the broker is told apart by its
# name in From or the platform subdomain (examplebroker.zendesk.com).
PLATFORM_MAIL = ("onetrust.com", "trustarc.com", "osano.com", "zendesk.com", "zohodesk.com", "freshdesk.com",
                 "helpscout.net", "datagrail.io", "trustsuperset.com", "transcend.io", "ketch.com", "securiti.ai")


# ------------------------------------------------------------ capture (inside the mail poller)

def _match(conn, meta, dkim):
    """(broker_id, request_id, how) or (None, None, None)."""
    row = conn.execute("SELECT id, broker_id FROM requests WHERE thread_id = ? ORDER BY id DESC LIMIT 1",
                       (meta.get("thread_id") or meta.get("threadId") or "",)).fetchone()
    if row:
        return row["broker_id"], row["id"], "thread"
    domains = registry.by_domain()
    for domain in dkim:
        if domain in domains and not registry.shared(domain):
            return domains[domain], None, f"dkim {domain}"
    sender = psl.registrable_domain(hdr.sender(meta["headers"]))
    if sender in domains:
        return domains[sender], None, f"from {sender}"
    if sender in PLATFORM_MAIL:
        found = _platform_broker(conn, meta["headers"], sender)
        if found:
            return found, None, f"name on {sender}"
    return None, None, None


def _squash(text):
    return re.sub(r"[^a-z0-9]", "", (text or "").lower())


def _platform_broker(conn, headers, platform):
    """The one broker with a request out whose name or domain is in From's display name ("Privacy Team at
    Example Data"), or whose domain is part of the platform subdomain (privacy@examplebroker.zendesk.com,
    privacy@examplebroker.mail.datagrail.io). None if no broker, or more than one, fits."""
    from email.utils import parseaddr
    display, address = parseaddr(hdr.first(headers, "From"))
    display = _squash(display)
    host = address.rpartition("@")[2].lower()
    labels = set(host[:-len(platform)].rstrip(".").split(".")) - {""} if host.endswith("." + platform) else set()
    entries = registry.load()
    fits = set()
    for row in conn.execute("SELECT id FROM brokers WHERE state IN ('submitted', 'resubmitted', 'awaiting_confirmation', "
                            "'needs_manual', 'acknowledged')"):
        entry = entries.get(row["id"])
        if not entry:
            continue
        name = _squash(entry.get("name"))
        firsts = {d.split(".")[0] for d in registry.get(entry, "domains", []) if not registry.shared(d)}
        if (len(name) >= 5 and name in display) or labels & firsts or any(len(f) >= 5 and f in display for f in firsts):
            fits.add(row["id"])
    return fits.pop() if len(fits) == 1 else None


def capture(log, api=None):
    """Take the poller's broker events and keep the mail that belongs to a broker."""
    from mail import store as mail_store
    from unsub import body      # a standard-library parser, there with or without the inbox agent

    events = mail_store.take(AGENT)
    if not events:
        return {"captured": 0}
    from mail import gmail
    api = api or gmail.client()
    kept, failed = 0, []
    for event in events:
        if event["kind"] != "added":
            continue
        try:
            raw = api.message(event["message_id"], "full")
        except gmail.NotFound:
            continue
        except gmail.GmailError as error:
            log.warning("broker mail %s: %s", event["message_id"], error)
            failed.append(event["id"])
            continue
        headers = [[h["name"], h["value"]] for h in raw.get("payload", {}).get("headers", [])]
        meta = {"id": raw["id"], "thread_id": raw.get("threadId"), "headers": headers}
        dkim = hdr.passing_dkim(hdr.trusted_auth(headers))
        text, links = body.extract(raw.get("payload", {}))
        with vault.db() as conn:
            broker_id, request_id, how = _match(conn, meta, dkim)
            if not broker_id:
                watch.say(f"mail from {hdr.sender(headers)}: no broker of ours, left alone", "mail")
                continue
            watch.say(f"mail from {hdr.sender(headers)}: {hdr.first(headers, 'Subject')[:80]} ({how})", "mail",
                      broker_id)
            conn.execute("INSERT OR IGNORE INTO replies (broker_id, request_id, message_id, thread_id, received_at, "
                         "sender, subject, text, signed_by, matched_by, links, asks) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                         (broker_id, request_id, raw["id"], raw.get("threadId"), int(raw.get("internalDate", 0)) / 1000,
                          hdr.sender(headers), hdr.first(headers, "Subject")[:300], text[:60000], json.dumps(dkim), how,
                          json.dumps(links[:200]), json.dumps({"auto_submitted": hdr.first(headers, "Auto-Submitted"),
                                                              "precedence": hdr.first(headers, "Precedence")})))
            vault.history(conn, broker_id, "mail", f"{hdr.first(headers, 'Subject')[:120]} ({how})")
            reply_id = conn.execute("SELECT id FROM replies WHERE message_id = ?", (raw["id"],)).fetchone()["id"]
            kept += 1
        try:
            held.save(api, reply_id, broker_id, raw, log)         # a data file they sent: what they had
        except gmail.GmailError as error:
            log.warning("could not keep the files of %s: %s", raw["id"], error)
        try:
            file_away(api, raw["id"], raw.get("labelIds", []))
        except gmail.GmailError as error:
            log.warning("could not file %s: %s", raw["id"], error)
    mail_store.release(failed)
    return {"captured": kept, "retry": len(failed)}


# ------------------------------------------------------------ filing in Gmail

_label_ids = {}


def label_name():
    return plan.config().get("gmail_label") or ""


def file_away(api, message_id, labels, reply=True):
    """A broker's reply goes under the Data brokers label, out of the inbox and marked read: the
    dashboard has what needs you. A request you sent just gets the label. Either comes back out of
    Trash: the vault refers to it, and a complaint may need it."""
    name = label_name()
    if not name:
        return False
    if name not in _label_ids:
        _label_ids[name] = api.label_id(name)
    if "TRASH" in labels:
        api.untrash(message_id)
    api.modify(message_id, add=[_label_ids[name]], remove=["INBOX", "UNREAD"] if reply else [])
    return True


def file_all(log, api=None):
    """Once: every broker email and request already in the vault, filed the same way; and taken out of
    the inbox agent's records (when it is installed), where unread, trashed broker mail would count
    against the sender."""
    from lib import components
    from mail import gmail, store as mail_store

    api = api or gmail.client()
    with vault.db() as conn:
        replies = [r[0] for r in conn.execute("SELECT message_id FROM replies")]
        sent = [r[0] for r in conn.execute("SELECT gmail_id FROM requests WHERE gmail_id IS NOT NULL")]
    done = {"replies": 0, "sent": 0, "gone": 0}
    for ids, reply in ((replies, True), (sent, False)):
        for message_id in ids:
            try:
                labels = api.message(message_id, "minimal").get("labelIds", [])
                file_away(api, message_id, labels, reply)
                done["replies" if reply else "sent"] += 1
            except gmail.NotFound:
                done["gone"] += 1
    with mail_store.db() as conn:
        marks = ",".join("?" * len(replies))
        conn.execute(f"DELETE FROM routes WHERE agent = 'inbox' AND message_id IN ({marks})", replies)
        conn.execute(f"DELETE FROM events WHERE agent = 'inbox' AND message_id IN ({marks})", replies)
    if not components.installed("unsub"):
        log.info("filed broker mail: %s", done)
        return done
    from unsub import store as unsub_store
    with unsub_store.db() as conn:
        senders = {r[0] for r in conn.execute(f"SELECT sender_id FROM messages WHERE id IN ({marks})", replies)}
        done["unsub_messages"] = conn.execute(f"DELETE FROM messages WHERE id IN ({marks})", replies).rowcount
        for sender_id in senders:
            if not conn.execute("SELECT 1 FROM messages WHERE sender_id = ?", (sender_id,)).fetchone():
                conn.execute("DELETE FROM senders WHERE id = ? AND status IS NULL", (sender_id,))
    log.info("filed broker mail: %s", done)
    return done


# ------------------------------------------------------------ sorting

# Where their own words end and the quote of ours begins: "On … wrote:", an Outlook header block, a
# ">" line, or the opening of one of our templates quoted without markers.
QUOTE_START = re.compile(r"^\s*(?:On .{4,200}wrote:|-+ ?Original Message ?-+|From: .+\n\s*Sent: |>)|"
                         r"\bOn [A-Z][a-z]{2}, .{4,120}wrote:|I'm requesting that you delete all personal information|"
                         r"I asked you to delete the personal information|Here are the details you asked for",
                         re.MULTILINE)


def own_words(text):
    """The reply without the quoted request under it: the template asks them to "confirm by email when
    this is complete", which would otherwise read as their answer."""
    text = text or ""
    for match in QUOTE_START.finditer(text):
        if text[:match.start()].strip():
            return text[:match.start()]
    return text


def asked_for(text):
    found = []
    for element, pattern in ASKED:
        if re.search(pattern, text, re.IGNORECASE):
            found.append(element)
    return found


# "…once your request is complete", "…if we need more information": still to come, not said.
FUTURE = re.compile(r"\b(?:once|when|after|until|as soon as|if)\b[^.!?<]*$", re.IGNORECASE)


def _says(pattern, text):
    """The pattern said as a fact, not inside an "once …" / "if …" clause."""
    return any(not FUTURE.search(text[max(0, match.start() - 80):match.start()]) for match in pattern.finditer(text))


def classify(reply, cfg):
    """(label, confidence, how). Labels: bounced, auto_reply, received, confirm_link, redirected,
    completed, needs_info, needs_id, refused_residency, refused_other, no_record, unclear."""
    text, subject, sender = own_words(reply["text"]), reply["subject"] or "", reply["sender"] or ""
    flags = json.loads(reply["asks"] or "{}") if reply["asks"] and reply["asks"].startswith("{") else {}
    if BOUNCE_FROM.search(sender) or BOUNCE_SUBJECT.search(subject):
        if DELAYED.search(subject) or DELAYED.search(text[:600]):
            return "auto_reply", 1.0, "rule: delivery delayed, the mail server is still trying"
        return "bounced", 1.0, "rule"
    links = json.loads(reply["links"] or "[]")
    if CONFIRM_WORDS.search(text) and not REPLY_WITH.search(text) and \
            any(CONFIRM_LINK.search(f"{href} {anchor}") for href, anchor in links):
        return "confirm_link", 1.0, "rule"
    # "Reply with your state…": the ask, even when it says what happens without it ("…or we deny it").
    if REPLY_WITH.search(text) and asked_for(text):
        return "needs_info", 1.0, "rule"
    # An outcome first, then "use our form / other address" (a refusal may also mention the form), then ID.
    for label in ("refused_residency", "no_record", "completed", "refused_other"):
        if PHRASES[label].search(text) if label != "completed" else _says(PHRASES[label], text):
            return label, 1.0, "rule"
    if REDIRECT.search(text):
        return "redirected", 1.0, "rule"
    if PHRASES["needs_id"].search(text):
        return "needs_id", 1.0, "rule"
    if AUTO_SUBJECT.search(subject) or flags.get("auto_submitted", "").lower().startswith("auto-replied"):
        return "auto_reply", 1.0, "rule"
    if _says(ASKS, text):
        return "needs_info", 1.0, "rule"
    if RECEIVED.search(text):
        return "received", 1.0, "rule"
    from brokers import nli
    if not nli.available():
        return "unclear", 0.0, "no model"
    label, probability, _scores = nli.classify(text, cfg["nli"]["max_chars"])
    if probability < cfg["nli"]["threshold"]:
        return "unclear", probability, f"model: {label} at {probability:.2f}"
    return label, probability, "model"


MAX_HOPS = 6


def unwrap(url, domains):
    """A click-tracker's link (Postmark, SES, SendGrid…) followed by hand, one redirect at a time and
    without loading any page, until it reaches one of the broker's domains. That URL is returned
    unopened (opening it is the confirmation); None if it goes anywhere else or nowhere."""
    import requests
    from urllib.parse import urljoin

    for _ in range(MAX_HOPS):
        if forms.site(url) in domains:
            return url
        if forms.HTTPS_ONLY and not url.startswith("https://"):
            return None
        try:
            response = requests.get(url, allow_redirects=False, timeout=15, stream=True,
                                    headers={"User-Agent": forms.USER_AGENT})
            response.close()
        except requests.RequestException:
            return None
        location = response.headers.get("Location")
        if response.status_code not in (301, 302, 303, 307, 308) or not location:
            return None
        url = urljoin(url, location)
    return None


def _confirm_target(reply, entry):
    """The confirmation link, if every condition for opening it holds. (url, None) or (None, why).
    A tracked link counts when its redirects lead to the broker's own domain."""
    domains = set(registry.get(entry, "domains", []))
    signed = set(json.loads(reply["signed_by"] or "[]"))
    if not signed & domains:
        return None, "the email isn't DKIM-signed by the broker"
    secure = lambda href: href.startswith("https://") or not forms.HTTPS_ONLY
    candidates = [href for href, anchor in json.loads(reply["links"] or "[]")
                  if CONFIRM_LINK.search(f"{href} {anchor}") and secure(href)]
    for href in candidates:
        if forms.site(href) in domains:
            return href, None
    for href in candidates:
        target = unwrap(href, domains)
        if target:
            return target, None
    return None, "the confirmation link doesn't lead to the broker's own domain"


def _open_request(conn, broker_id):
    return conn.execute("SELECT * FROM requests WHERE broker_id = ? AND status IN ('sent', 'awaiting') "
                        "ORDER BY id DESC LIMIT 1", (broker_id,)).fetchone()


REPEAT_DAYS = 3         # a confirmation link this soon after you confirmed one by hand is a repeat
OUTCOMES = ("completed", "no_record", "refused_residency", "refused_other")


def _close_confirms(conn, broker_id, why):
    """An answer from the broker means the confirmation happened: its open confirm tasks are done."""
    count = conn.execute("UPDATE tasks SET done_at = ?, outcome = ? WHERE broker_id = ? AND kind = 'confirm' AND "
                         "done_at IS NULL", (time.time(), why, broker_id)).rowcount
    if count:
        vault.history(conn, broker_id, "confirm task closed", why)


def _later_outcome(reply, batch, cfg):
    """True if a later email in this batch, from the same broker, already gives the outcome."""
    for other in batch:
        if other["broker_id"] == reply["broker_id"] and (other["received_at"] or 0) >= (reply["received_at"] or 0) \
                and other["id"] != reply["id"] and classify(other, cfg)[0] in OUTCOMES:
            return True
    return False


def handle(log, cfg=None, click=True):
    """Sort every new reply and act on it. Returns counts by label."""
    cfg = cfg or plan.config()
    mode = plan.approval_mode()
    counts = {}
    with vault.db() as conn:
        replies = [dict(r) for r in conn.execute("SELECT * FROM replies WHERE label IS NULL ORDER BY received_at")]
    for reply in replies:
        entry = registry.broker(reply["broker_id"]) or {}
        label, confidence, how = classify(reply, cfg)
        counts[label] = counts.get(label, 0) + 1
        if label == "confirm_link" and _later_outcome(reply, replies, cfg):
            action = "a later email from them gives the outcome: nothing to confirm"
        else:
            action = _act(reply, entry, label, how, mode, cfg, click, log)
        if label in OUTCOMES:
            with vault.db() as conn:
                _close_confirms(conn, reply["broker_id"], f"they answered: {label}")
                if label.startswith("refused"):
                    plan.cancel_requests(conn, reply["broker_id"], "they refused")
        with vault.db() as conn:
            conn.execute("UPDATE replies SET label = ?, confidence = ?, handled = ?, handled_at = ? WHERE id = ?",
                         (label, confidence, action, time.time(), reply["id"]))
            vault.history(conn, reply["broker_id"], f"reply: {label}", f"{action} ({how})")
        watch.say(f"{entry.get('name', reply['broker_id'])}: their email reads as {label} → {action}", "mail",
                  reply["broker_id"])
    return counts


def _act(reply, entry, label, how, mode, cfg, click, log):
    """What to do with one sorted reply. Returns what was done, in words."""
    broker_id = reply["broker_id"]
    name = entry.get("name", broker_id)
    with vault.db() as conn:
        row = dict(conn.execute("SELECT * FROM brokers WHERE id = ?", (broker_id,)).fetchone() or {})
        request = _open_request(conn, broker_id)
        approved = json.loads(row["approved"]) if row.get("approved") else None
        # Open: a request the agent sent, or one you approved or did by hand (no request row then).
        is_open = request is not None or row.get("state") in ("approved", "submitted", "awaiting_confirmation",
                                                             "resubmitted", "needs_manual")
        if label == "auto_reply":
            return "noted"
        if label == "received":
            if row.get("state") in plan.OPEN:
                when = time.strftime("%b %-d", time.localtime(reply["received_at"] or time.time()))
                conn.execute("UPDATE brokers SET detail = ? WHERE id = ?",
                             (f"they confirmed they got it on {when}; waiting for the outcome", broker_id))
            return "noted: they have it"
        if label == "redirected":
            return _redirected(conn, reply, entry, row, approved, mode)
        if label == "bounced":
            conn.execute("UPDATE brokers SET broken = ? WHERE id = ?", (f"email bounced: {reply['subject'][:80]}", broker_id))
            plan.set_state(conn, broker_id, "needs_manual", "the request email bounced")
            scan.open_task(conn, broker_id, "bounced", f"{name}'s privacy address bounced",
                           [f"{entry.get('privacy_email')} doesn't take mail.",
                            "Their opt-out page may have another way; the registry entry needs a new address."],
                           entry.get("opt_out_url") or entry.get("website"))
            return "registry maintenance: bounced"
        if label == "confirm_link":
            if not is_open or approved is None:
                return "confirmation link, but no open request of yours: left alone"
            if row.get("state") in plan.SETTLED:
                return f"confirmation link, but they already answered ({row['state']}): left alone"
            recent = conn.execute("SELECT 1 FROM tasks WHERE broker_id = ? AND kind = 'confirm' AND done_at > ?",
                                  (broker_id, time.time() - REPEAT_DAYS * 86400)).fetchone()
            if recent:
                return "another confirmation link soon after you confirmed one: a repeat, left alone"
            by_hand = request is None or request["method"] == "hand"     # your own opt-out
            url, why = _confirm_target(reply, entry)
            if not url and by_hand:
                return f"confirmation for the opt-out you did by hand ({why}): click it in their email"
            if not url:
                scan.open_task(conn, broker_id, "confirm", f"Confirm your request to {name}",
                               [why + ", so the agent won't open it.", "If the email is genuinely theirs, open its "
                                "link yourself."], entry.get("opt_out_url"), reply["id"])
                return f"manual: {why}"
            plan.advance(conn, broker_id, "awaiting_confirmation", "the broker sent a confirmation link")
            if mode != "standing" or not click:
                plan.set_pending(conn, broker_id, {"kind": "confirm", "url": url, "reply": reply["id"]},
                                 "open the confirmation link")
                return "needs approval: open the confirmation link"
    if label == "confirm_link":
        return confirm(broker_id, entry, url, reply["id"], cfg, by_hand)
    with vault.db() as conn:
        if label == "completed":
            _acknowledge(conn, broker_id, entry, cfg, "the broker says it's done")
            if registry.get(entry, "honors_nonresidents") != "yes":
                registry.update(broker_id, honors_nonresidents="yes")
            return "acknowledged"
        if label == "no_record":
            if scan.searchable(entry):
                _acknowledge(conn, broker_id, entry, cfg, "no record found (will check by searching)")
            else:
                plan.set_state(conn, broker_id, "verified_removed", "the broker found no record of you")
            return "no record"
        if label == "refused_residency":
            plan.set_state(conn, broker_id, "refused", "refused: you're not a resident of a state whose law it follows")
            registry.update(broker_id, honors_nonresidents="no")
            return "refused (residency)"
        if label == "refused_other":
            plan.set_state(conn, broker_id, "refused", "refused")
            return "refused"
        if label == "needs_id":
            plan.set_state(conn, broker_id, "needs_manual", "asks for ID")
            scan.open_task(conn, broker_id, "id", f"{name} asks for ID",
                           ["The agent never sends ID. If you choose to, reply to their email yourself.",
                            "Redact everything except your name and photo: the number, address, date of birth, "
                            "barcode and signature."], entry.get("opt_out_url"), reply["id"])
            return "manual: ID"
        if label == "needs_info":
            asked = asked_for(own_words(reply["text"]))
            if not asked:
                scan.open_task(conn, broker_id, "reply", f"{name} wants something", ["Their reply asks for something the "
                               "agent can't pin down. Read it and answer them yourself, or mark it done."],
                               entry.get("opt_out_url"), reply["id"])
                return "manual: unclear ask"
            fine, why = plan.covers(approved, (approved or {}).get("method", "email"), [], asked)
            pending = {"kind": "more-info", "asked": asked, "reply": reply["id"], "to": reply["sender"],
                       "thread": reply["thread_id"]}
            if fine and mode == "standing" and request is not None:
                queue_more_info(conn, broker_id, pending, request, auto=True)
                return f"auto: answering with {', '.join(asked)}"
            pending["grows"] = not fine
            plan.set_pending(conn, broker_id, pending, why or "approval mode is per request")
            return f"needs approval: {why or 'per-request mode'}"
        scan.open_task(conn, broker_id, "reply", f"Reply from {name} the agent couldn't read",
                       ["Read it and mark the task done with what it said (done, refused, needs ID, no record)."],
                       entry.get("opt_out_url"), reply["id"])
        return "manual: unclear"


def queue_more_info(conn, broker_id, pending, request, auto):
    conn.execute("INSERT INTO requests (broker_id, kind, method, elements, status, auto, queued_at, to_address, "
                 "thread_id, message_id_header, detail) VALUES (?, 'more-info', 'email', ?, 'queued', ?, ?, ?, ?, ?, ?)",
                 (broker_id, json.dumps(pending["asked"]), int(auto), time.time(), pending["to"], pending["thread"],
                  request["message_id_header"] if request else None,
                  f"answering their request for {', '.join(pending['asked'])}"))
    plan.set_pending(conn, broker_id, None)


REQUEST_BOX = re.compile(r"request|dsr|dsar|subject|delet|opt-?out|ccpa|rights")
FORM_LINK = re.compile(r"form|privacy|opt-?out|request|ccpa|dsr|dsar|do-?not-?sell|donosell|rights", re.IGNORECASE)


# Re-routing can't loop: each place (address or form page) is tried once, at most MAX_REROUTES times
# per broker; and once a broker says "only our form", nothing goes back to its mailbox by itself
# (no email instead of a failed form, no follow-ups there).
MAX_REROUTES = 2


def place(target):
    """An address or a page, the same however it was written (case, www., fragment, query, last slash)."""
    target = (target or "").strip()
    if not target.startswith("http"):
        return target.lower()
    from urllib.parse import urlsplit
    parts = urlsplit(target)
    return parts.netloc.lower().removeprefix("www.") + parts.path.rstrip("/").lower()


def _reroute(approved, old, new):
    """Record a move from old to new in the approval. False if it was tried already or the limit is reached."""
    tried = approved.setdefault("tried", [place(old)] if old else [])
    if place(new) in tried or approved.get("reroutes", 0) >= MAX_REROUTES:
        return False
    tried.append(place(new))
    approved["reroutes"] = approved.get("reroutes", 0) + 1
    return True


def _redirected(conn, reply, entry, row, approved, mode):
    """They want the request somewhere else. Another address on their own domain, or their web form,
    gets the same request (the same approved details) again: by itself in standing mode, else once you
    approve. A place already tried, or a third re-route, is a manual task instead (see MAX_REROUTES)."""
    from brokers import send

    broker_id, name = reply["broker_id"], entry.get("name", reply["broker_id"])
    if row.get("state") in plan.SETTLED:
        return f"pointed elsewhere, but they already answered ({row['state']}): left alone"
    domains = set(registry.get(entry, "domains", []))
    skip = {((approved or {}).get("to") or "").lower(), (entry.get("privacy_email") or "").lower(),
            (reply["sender"] or "").lower()}
    found = []
    for address in ADDRESS.findall(reply["text"] or ""):
        address = address.lower().rstrip(".")
        local, _, host = address.partition("@")
        if address in skip or address in found or psl.registrable_domain(host) not in domains \
                or registry.shared(psl.registrable_domain(host)) or not (REQUEST_BOX.search(local) or "privacy" in local):
            continue
        found.append(address)
    found.sort(key=lambda a: not REQUEST_BOX.search(a.partition("@")[0]))   # a requests box before a general one
    old = (approved or {}).get("form") or (approved or {}).get("to") or entry.get("privacy_email")

    def go(why):
        conn.execute("UPDATE brokers SET approved = ? WHERE id = ?", (json.dumps(approved), broker_id))
        if mode == "standing":
            send.queue(conn, broker_id, "resubmit", auto=True, detail=why)
            conn.execute("UPDATE brokers SET detail = ? WHERE id = ?", (why, broker_id))
            return "auto"
        plan.set_pending(conn, broker_id, {"kind": "resubmit", "why": why}, why)
        return "needs approval"

    if found and approved and approved.get("method") == "email" and not approved.get("form_only") \
            and _reroute(approved, old, found[0]):
        address = found[0]
        approved["to"] = address
        registry.update(broker_id, privacy_email=address)
        how = go(f"{old} doesn't take requests; they said to use {address}")
        return f"{how}: send the request again to {address}"
    links = [href for href, anchor in json.loads(reply["links"] or "[]")
             if href.startswith("http") and forms.site(href) in domains and FORM_LINK.search(f"{href} {anchor}")]
    link = links[0] if links else entry.get("opt_out_url") or entry.get("website")
    if links and not entry.get("opt_out_url"):
        registry.update(broker_id, opt_out_url=links[0])
    if approved:
        approved["form_only"] = True                # their mailbox won't act: never fall back to it
        if link and _reroute(approved, old, link):
            approved.update(method="generic", form=link)
            how = go(f"they only take requests through their form: {link}")
            if mode == "standing":
                plan.set_state(conn, broker_id, "approved", "they only take requests through their form; "
                                                             "the agent submits it with the same details")
            return f"{how}: submit their form ({link})"
        conn.execute("UPDATE brokers SET approved = ? WHERE id = ?", (json.dumps(approved), broker_id))
    again = bool(approved and link and place(link) in (approved.get("tried") or []))
    capped = bool(approved and link and not again and approved.get("reroutes", 0) >= MAX_REROUTES)
    plan.set_state(conn, broker_id, "needs_manual", "they only take requests through their own form")
    scan.open_task(conn, broker_id, "form", f"{name} only takes requests through its form",
                   ["Their privacy mailbox won't act on an email request.",
                    "The agent already tried that page, so it stopped rather than go round again." if again else
                    f"The agent has re-routed this request {MAX_REROUTES} times, so it stopped." if capped else "",
                    "Submit it on their form yourself, then mark this done (Submitted, waiting for them)."],
                   link, reply["id"])
    return "manual: they want their web form"


def confirm(broker_id, entry, url, reply_id, cfg, by_hand=False):
    """Open a broker's confirmation link (the checks in _confirm_target have passed). by_hand: the
    opt-out was yours, so a page it can't read is left to you without a task."""
    name = entry.get("name", broker_id)
    try:
        with forms.session() as context:
            result = forms.confirm_link(context, url, set(registry.get(entry, "domains", [])))
    except Exception as error:
        result = forms.Result(False, "failed", f"{type(error).__name__}: {str(error)[:120]}")
    with vault.db() as conn:
        plan.set_pending(conn, broker_id, None)
        if result.ok:
            _acknowledge(conn, broker_id, entry, cfg, "confirmed by its link")
            return f"auto: opened the confirmation link ({result.detail})"
        if by_hand:
            return f"opened the link for your own opt-out, but {result.detail}: check their email if it matters"
        scan.open_task(conn, broker_id, "confirm", f"Confirm your request to {name}",
                       [f"The agent opened the link but: {result.detail}.", "Open it yourself and confirm."],
                       url, reply_id)
        return f"manual: {result.detail}"


def _acknowledge(conn, broker_id, entry, cfg, detail):
    now = time.time()
    low, high = cfg["verify_after_days"]
    verify = now + ((low + high) / 2) * 86400 if scan.searchable(entry) else None
    conn.execute("UPDATE brokers SET acknowledged_at = ?, verify_at = ? WHERE id = ?", (now, verify, broker_id))
    conn.execute("UPDATE requests SET status = 'acknowledged' WHERE broker_id = ? AND status IN ('sent', 'awaiting')",
                 (broker_id,))
    plan.set_state(conn, broker_id, "acknowledged", detail)

