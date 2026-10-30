"""Reading message headers: addresses, Authentication-Results and ARC, sending platforms.

Headers come as [[name, value], …] in the order Gmail gives them (top of the message first).

Only Gmail's own Authentication-Results (authserv-id mx.google.com, the topmost one) is trusted;
any other copy could have been written by the sender. Forwarded mail fails SPF by design, so the
evidence is DKIM, plus the ARC chain when Gmail says it passed: Gmail sums up what the first hop
saw as `arc=pass (i=1 … dkim=pass dkdomain=x …)`.
"""
import re
from email.utils import getaddresses

from lib import psl

TRUSTED_AUTHSERV = "mx.google.com"
# Cloudflare Email Routing signs every message it forwards as itself. That signature passes on all
# forwarded mail and says nothing about who sent it, so it never counts.
FORWARDER_DKIM = {"cloudflare-email.net"}

# Sending platforms: registrable domain -> name. A DKIM signature from one of these vouches for
# its customer's mail, not for the platform itself.
PLATFORMS = {
    "sendgrid.net": "SendGrid", "mailchimpapp.net": "Mailchimp", "mcsv.net": "Mailchimp", "mcdlv.net": "Mailchimp",
    "rsgsv.net": "Mailchimp", "mandrillapp.com": "Mailchimp Transactional", "klaviyomail.com": "Klaviyo",
    "klaviyo.com": "Klaviyo", "hubspotemail.net": "HubSpot", "hubspot.com": "HubSpot", "hs-email.net": "HubSpot",
    "exacttarget.com": "Salesforce Marketing Cloud", "exct.net": "Salesforce Marketing Cloud",
    "salesforce.com": "Salesforce", "braze.com": "Braze", "brazeemail.com": "Braze", "appboy.com": "Braze",
    "constantcontact.com": "Constant Contact", "ctctcdn.com": "Constant Contact", "amazonses.com": "Amazon SES",
    "mailgun.org": "Mailgun", "mailgun.net": "Mailgun", "sparkpostmail.com": "SparkPost", "postmarkapp.com": "Postmark",
    "mtasv.net": "Postmark", "customeriomail.com": "Customer.io", "customer.io": "Customer.io",
    "mailjet.com": "Mailjet", "sendinblue.com": "Brevo", "brevo.com": "Brevo", "sib.email": "Brevo",
    "iterable.com": "Iterable", "cmail19.com": "Campaign Monitor", "cmail20.com": "Campaign Monitor",
    "createsend.com": "Campaign Monitor", "marketo.org": "Marketo", "mktomail.com": "Marketo",
    "emarsys.net": "Emarsys", "responsys.net": "Oracle Responsys", "bronto.com": "Bronto",
    "zendesk.com": "Zendesk", "intercom-mail.com": "Intercom", "intercom.io": "Intercom",
    "shopifyemail.com": "Shopify Email", "shopify.com": "Shopify", "squarespace-mail.com": "Squarespace",
    "convertkit-mail.com": "ConvertKit", "ck.page": "ConvertKit", "substack.com": "Substack",
    "beehiiv.com": "beehiiv", "attentivemobile.com": "Attentive", "omnisend.com": "Omnisend",
    "gappssmtp.com": "Google Workspace", "onmicrosoft.com": "Microsoft 365",
}
# Headers that name a platform even when its domain does not show (X-Mailgun-Sid, X-SG-EID…).
PLATFORM_HEADERS = {"x-mailgun-": "Mailgun", "x-sg-": "SendGrid", "x-mc-user": "Mailchimp", "x-mandrill": "Mailchimp",
                    "x-mailchimp": "Mailchimp", "x-kmail": "Klaviyo", "x-klaviyo": "Klaviyo", "x-hs-": "HubSpot",
                    "x-sfmc-": "Salesforce Marketing Cloud", "x-ses-": "Amazon SES", "x-pm-message-id": "Postmark",
                    "x-msys-api": "SparkPost", "x-mailjet": "Mailjet"}


def values(headers, name):
    name = name.lower()
    return [value for key, value in headers if key.lower() == name]


def first(headers, name, default=""):
    found = values(headers, name)
    return found[0] if found else default


def addresses(headers, *names):
    """Lower-cased addresses from the named headers, in order, without repeats."""
    found = []
    for name in names:
        for _display, address in getaddresses(values(headers, name)):
            address = address.strip().lower()
            if "@" in address and address not in found:
                found.append(address)
    return found


def sender(headers):
    """The From address, lower-cased ('' if there is none)."""
    found = addresses(headers, "From")
    return found[0] if found else ""


def _split_top(value, separator=";"):
    """Split on a separator outside parentheses and quotes."""
    parts, depth, quoted, current = [], 0, False, []
    for char in value:
        if char == '"':
            quoted = not quoted
        elif not quoted and char == "(":
            depth += 1
        elif not quoted and char == ")":
            depth = max(0, depth - 1)
        if char == separator and depth == 0 and not quoted:
            parts.append("".join(current))
            current = []
        else:
            current.append(char)
    parts.append("".join(current))
    return [part.strip() for part in parts if part.strip()]


def parse_auth_results(value):
    """{"authserv": id, "results": [{"method", "result", "props": {...}, "comment"}]}."""
    parts = _split_top(" ".join(value.split()))
    if not parts:
        return {"authserv": "", "results": []}
    authserv = parts[0].split()[0].lower() if parts[0].split() else ""
    results = []
    for part in parts[1:]:
        match = re.match(r"([\w-]+)\s*=\s*([\w-]+)(.*)$", part, re.DOTALL)
        if not match:
            continue
        rest = match.group(3)
        comment = " ".join(re.findall(r"\(([^()]*)\)", rest))
        props = {key.lower(): val.strip('"') for key, val in re.findall(r"([\w.-]+)\s*=\s*(\"[^\"]*\"|[^\s;()]+)",
                                                                         re.sub(r"\([^()]*\)", " ", rest))}
        results.append({"method": match.group(1).lower(), "result": match.group(2).lower(), "props": props,
                        "comment": comment})
    return {"authserv": authserv, "results": results}


def trusted_auth(headers):
    """Gmail's own verdicts: {"dkim": [(result, domain)], "spf", "dmarc", "arc", "arc_dkim": [(result, domain)],
    "from_domain"} with results lower-cased and '' where Gmail said nothing."""
    for value in values(headers, "Authentication-Results"):
        parsed = parse_auth_results(value)
        if parsed["authserv"] == TRUSTED_AUTHSERV:
            break
    else:
        parsed = {"results": []}
    found = {"dkim": [], "spf": "", "dmarc": "", "arc": "", "arc_dkim": [], "from_domain": ""}
    for item in parsed["results"]:
        props = item["props"]
        if item["method"] == "dkim":
            domain = props.get("header.d") or props.get("header.i", "").lstrip("@").split("@")[-1]
            found["dkim"].append((item["result"], domain.lower()))
        elif item["method"] in ("spf", "dmarc") and not found[item["method"]]:
            found[item["method"]] = item["result"]
            if item["method"] == "dmarc":
                found["from_domain"] = props.get("header.from", "").lower()
        elif item["method"] == "arc" and not found["arc"]:
            found["arc"] = item["result"]
            inner = dict(re.findall(r"([\w.]+)=([^\s]+)", item["comment"]))
            if inner.get("dkdomain"):
                found["arc_dkim"].append((inner.get("dkim", ""), inner["dkdomain"].lower()))
    return found


def passing_dkim(auth):
    """Registrable domains whose DKIM signature passed: directly, or upstream of a passing ARC chain.
    The forwarder's own signatures (FORWARDER_DKIM) are left out."""
    domains = [domain for result, domain in auth["dkim"] if result == "pass" and domain]
    if auth["arc"] == "pass":
        domains += [domain for result, domain in auth["arc_dkim"] if result == "pass" and domain]
    out = []
    for domain in domains:
        registrable = psl.registrable_domain(domain)
        if registrable and registrable not in out and registrable not in FORWARDER_DKIM:
            out.append(registrable)
    return out


APPLE_RELAY = "privaterelay.appleid.com"
APPLE_AUTHSERV = "dkim-verifier.icloud.com"
# Hide My Email writes the original From into the relay address: noreply_at_example_com_<10>_<8 hex>
_APPLE_RELAY_LOCAL = re.compile(r"^(.+)_at_([a-z0-9_-]+?)_[a-z0-9]{10}_[0-9a-f]{8}$")


def apple_relay(headers, auth=None):
    """(original address, its registrable domain) for mail that came through Apple's Hide My Email relay,
    or None. Only when it can be trusted: Gmail saw a passing DKIM signature from the relay, and iCloud's
    own verdict (dkim-verifier.icloud.com, added before the relay signed it) has a passing DKIM signature
    from the same domain the relay address names."""
    address = sender(headers)
    if not address.endswith("@" + APPLE_RELAY):
        return None
    auth = auth or trusted_auth(headers)
    if not any(result == "pass" and domain.endswith(APPLE_RELAY) for result, domain in auth["dkim"]):
        return None
    match = _APPLE_RELAY_LOCAL.match(address.split("@")[0])
    if not match:
        return None
    original = f"{match.group(1)}@{match.group(2).replace('_', '.')}"
    domain = psl.registrable_domain(original)
    for value in values(headers, "Authentication-Results"):
        parsed = parse_auth_results(value)
        if parsed["authserv"] != APPLE_AUTHSERV:
            continue
        for item in parsed["results"]:
            signer = item["props"].get("header.d", "").lower()
            if item["method"] == "dkim" and item["result"] == "pass" and signer \
                    and psl.registrable_domain(signer) == domain:
                return original, domain
    return None


def platform(headers, auth=None):
    """The sending platform the message came through, or ''."""
    auth = auth or trusted_auth(headers)
    for domain in passing_dkim(auth):
        if domain in PLATFORMS:
            return PLATFORMS[domain]
    names = {key.lower() for key, _value in headers}
    for prefix, name in PLATFORM_HEADERS.items():
        if any(key.startswith(prefix) for key in names):
            return name
    for value in values(headers, "Return-Path"):
        domain = psl.registrable_domain(value)
        if domain in PLATFORMS:
            return PLATFORMS[domain]
    return ""
