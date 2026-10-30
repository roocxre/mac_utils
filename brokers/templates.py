"""The request emails. Fixed text; only the details you approved are filled in.

Every request is voluntary, under the broker's own published opt-out process: no template claims
residency in, or rights under, any state's law. When config.json's jurisdiction lists a law of the
state you live in (plan.home_states) that gives you a right to deletion and covers the broker's
category, `deletion` cites that law and nothing else.
"""
from datetime import date

LABELS = {"full_name": "Name", "first_name": "First name", "last_name": "Last name", "email": "Email",
          "city_state": "City and state", "city": "City", "state": "State", "zip": "ZIP code",
          "address": "Address", "phone": "Phone", "dob": "Date of birth", "age": "Age",
          "listing_url": "Listing(s) on your site"}

DELETION = """I'm requesting that you delete all personal information you hold about me, and stop selling or \
sharing it, under your published privacy policy and opt-out process. The identifying details below are provided \
only so you can find my records. Please don't use them for any other purpose.

{details}

Please confirm by email when this is complete."""

DELETION_RIGHTS = """I'm requesting that you delete all personal information you hold about me, and stop selling \
or sharing it, as {law} provides. The identifying details below are provided only so you can find my records. \
Please don't use them for any other purpose.

{details}

Please confirm by email when this is complete."""

ACCESS_FIRST = """Before you delete it, please tell me what personal information you hold about me (the \
categories, and where you got it), and to whom you have sold or disclosed it. If you can't do both, please \
delete it anyway."""

ACCESS = """Please tell me what personal information you hold about me (the categories, and where you got it), \
and to whom you have sold or disclosed it, under your published privacy policy. The identifying details below \
are provided only so you can find my records. Please don't use them for any other purpose.

{details}"""

FOLLOW_UP = """On {sent} I asked you to delete the personal information you hold about me, and to stop selling \
or sharing it (my request is quoted below). I haven't had a confirmation yet. Please complete the request and \
confirm by email.

{details}"""

MORE_INFO = """Here are the details you asked for, to find my records for my deletion request. Please use them \
only for that.

{details}

Please confirm by email when the deletion is complete."""

COMPLAINT = """To: {regulator}
Subject: {broker} did not follow its own published opt-out process

I am a resident of {home}. {broker} ({website}) publishes a process for consumers to opt out of the sale of \
their personal information and have it deleted ({policy}). I used that process, and {broker} {failure}.

What happened:
{timeline}

The details I gave {broker} to find my records, and nothing else:
{ledger}

A business that publishes an opt-out process and then does not follow it may be engaging in a deceptive \
practice. I'm asking you to look into {broker}'s handling of opt-out requests.

{name}"""

FAILURES = {"refused": "refused my request", "no_response": "has not responded in {days} days",
            "relisted": "removed my listing and then published it again",
            "still_listed": "said it removed my listing, but it is still published"}


def details_block(values):
    """values: {element: value} in the order they should appear."""
    lines = []
    for element, value in values.items():
        if element == "listing_url":
            lines.append(f"{LABELS[element]}:\n" + "\n".join(f"  {url}" for url in str(value).split("\n")))
        else:
            lines.append(f"{LABELS.get(element, element)}: {value}")
    return "\n".join(lines)


def law_for(cfg, category, states=()):
    """The configured law of a state you live in (states: plan.home_states) that covers this kind of
    broker, or None (see config.json jurisdiction.laws)."""
    today = date.today().isoformat()
    for law in cfg["jurisdiction"].get("laws", []):
        if str(law.get("state", "")).upper() in states and law.get("effective", "9999") <= today and \
                category in law.get("covers", []):
            return law
    return None


def sign(values):
    return values.get("full_name") or " ".join(x for x in (values.get("first_name"), values.get("last_name")) if x)


def deletion(values, cfg, category, access=False, states=()):
    """(subject, body). states: where you live (plan.home_states), for a law that gives you the right."""
    law = law_for(cfg, category, states)
    body = (DELETION_RIGHTS.format(law=law["name"], details=details_block(values)) if law
            else DELETION.format(details=details_block(values)))
    if access:
        body = body.replace("\n\nPlease confirm by email", "\n\n" + ACCESS_FIRST + "\n\nPlease confirm by email")
    name = sign(values)
    return "Request to delete my personal information", body + (f"\n\n{name}" if name else "")


def access(values):
    name = sign(values)
    return ("Request for the personal information you hold about me",
            ACCESS.format(details=details_block(values)) + (f"\n\n{name}" if name else ""))


def follow_up(values, sent_on, original):
    name = sign(values)
    quoted = "\n".join("> " + line for line in original.splitlines())
    return ("Re: Request to delete my personal information",
            FOLLOW_UP.format(sent=sent_on, details=details_block(values)) + (f"\n\n{name}" if name else "")
            + "\n\n" + quoted)


def more_info(values):
    name = sign(values)
    return MORE_INFO.format(details=details_block(values)) + (f"\n\n{name}" if name else "")


def complaint(regulator, broker, website, policy, failure, timeline, ledger_lines, home, name, days=60):
    return COMPLAINT.format(regulator=regulator, broker=broker, website=website, policy=policy or website,
                            failure=FAILURES.get(failure, failure).format(days=days),
                            timeline="\n".join(f"- {line}" for line in timeline),
                            ledger="\n".join(f"- {line}" for line in ledger_lines) or "- (none)",
                            home=home, name=name)
