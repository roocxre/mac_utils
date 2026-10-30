"""What one email is, from what it says: transactional, marketing, or other.

A sender is not one thing: a business that sends ads also sends receipts and appointment
reminders, often from the same address and through the same sending platform (so the headers
look alike). The words decide. Weighted patterns from config.json ("content") are counted over the
subject (x1.5) and the body text, each pattern once, plus a few header signals.

    transactional  reaches transactional_at and is not beaten by marketing by more than
                   transactional_margin: a receipt with a "you might also like" footer is a receipt
    marketing      reaches marketing_at
    other          neither (a person writing, a notice with nothing to go on)

Failing authentication or a lookalike domain is never transactional: phishing dresses up as
receipts and security alerts.

The kind decides which category of the sender a message belongs to (rules.sender_key): its ads,
its receipts and notices, or its other mail. Every rule is about one category, and receipts and
notices are never proposed, unsubscribed from or blocked.
"""
import re

from mail import headers as hdr

_COMPILED = {}


def _patterns(cfg, kind):
    key = (kind, tuple(tuple(p) for p in cfg["content"][kind]))
    if key not in _COMPILED:
        _COMPILED[key] = [(re.compile(pattern, re.IGNORECASE), weight) for pattern, weight in cfg["content"][kind]]
    return _COMPILED[key]


def _score(patterns, subject, text):
    total, hits = 0.0, []
    for pattern, weight in patterns:
        in_subject = pattern.search(subject)
        found = in_subject or pattern.search(text)
        if found:
            total += weight * (1.5 if in_subject else 1)
            hits.append(found.group(0).lower().strip())
    return total, hits


def classify(subject, text, headers, labels, cfg, trusted=True):
    """{"kind": transactional|marketing|other, "transactional": score, "marketing": score, "why": [words]}."""
    c = cfg["content"]
    subject = subject or ""
    text = (text or "")[:c["body_chars"]]
    tx, tx_hits = _score(_patterns(cfg, "transactional"), subject, text)
    mk, mk_hits = _score(_patterns(cfg, "marketing"), subject, text)
    has_unsubscribe = bool(hdr.values(headers, "List-Unsubscribe"))
    if has_unsubscribe:
        mk += c["header_marketing"]["list_unsubscribe"]
    if hdr.first(headers, "Precedence").strip().lower() in ("bulk", "list", "junk"):
        mk += c["header_marketing"]["precedence_bulk"]
    if "CATEGORY_PROMOTIONS" in labels:
        mk += c["header_marketing"]["promotions_tab"]
        mk_hits.append("promotions tab")
    if "CATEGORY_UPDATES" in labels:
        tx += c["header_transactional"]["updates_tab"]
    sender = hdr.sender(headers)
    if not has_unsubscribe and re.search(cfg["transactional_from"], sender or "", re.IGNORECASE):
        tx += c["header_transactional"]["noreply_without_unsubscribe"]
        tx_hits.append(f"{sender.split('@')[0]}@ with no unsubscribe")
    if trusted and tx >= c["transactional_at"] and tx >= mk - c["transactional_margin"]:
        kind, why = "transactional", tx_hits
    elif mk >= c["marketing_at"]:
        kind, why = "marketing", mk_hits
    else:
        kind, why = "other", []
    return {"kind": kind, "transactional": round(tx, 1), "marketing": round(mk, 1), "why": why[:4]}
