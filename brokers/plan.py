"""What to do about each broker: its tier, its state, the request to propose, and whether an
approval you gave still covers what the agent is about to do.

States (one per broker):

    discovered → scanned → planned → approved → submitted → awaiting_confirmation → acknowledged
              → verified_removed | still_listed → relisted → resubmitted (→ submitted …)
    side states: needs_manual, refused, broken, no_response

`listed` (listed, not_listed, possible, unknown) is the last scan's answer, kept beside the state.
A broker that can't be searched goes from discovered straight to planned, and stays acknowledged
until an access-request reply says the data is gone.

Re-approval is always needed, whatever the approval mode, when the details to send would grow
beyond what you approved, when the method changes to one that needs new kinds of details, or when
the broker asks for something outside the approved set (covers()). Switching method with the same
details (a form that needs a person, asked by email instead) is covered.
"""
import json
import os
import time

from brokers import ROOT, registry, vault

STATES = ("discovered", "scanned", "planned", "approved", "submitted", "awaiting_confirmation", "acknowledged",
          "verified_removed", "still_listed", "relisted", "resubmitted", "needs_manual", "refused", "broken",
          "no_response")
OPEN = ("submitted", "awaiting_confirmation", "resubmitted")           # a request is out
# An answer from the broker (or a search) settled these: a hand-done task or a late confirmation
# email never moves a broker out of them (advance()). Only a new search or reply does.
SETTLED = ("acknowledged", "refused", "verified_removed", "no_response")
DONE = ("verified_removed",)
SEARCHABLE = ("search-form",)
FORM_METHODS = ("direct-form", "search-form")
PEOPLE = ("people-search", "background-check")


def config():
    from lib import settings
    return settings.load("brokers", os.path.join(ROOT, "config.json"))


def approval_mode():
    return vault.get("approval_mode") or config()["approval_mode"]


def home_states(cfg=None, found=None):
    """Where you live, for the law a request may cite and the attorney general a complaint names: your
    current address's state (Your details), else config.json jurisdiction.states (empty by default)."""
    found = vault.identity() if found is None else found
    state = str((vault.current_address(found) or {}).get("state") or "").strip().upper()
    configured = (cfg or config())["jurisdiction"].get("states") or []
    return [state] if state else [str(s).strip().upper() for s in configured if str(s).strip()]


# ------------------------------------------------------------ tiers

def exposed_domains():
    """Domains with confirmed exposure: the exposure table (work.record_exposure: broker scripts the
    browser extension saw on pages where you typed your email)."""
    with vault.db() as conn:
        return {r["domain"] for r in conn.execute("SELECT domain FROM exposure")}


def tier(entry, exposed):
    if set(registry.get(entry, "domains", [])) & exposed:
        return 1
    category, priority = registry.get(entry, "category"), registry.get(entry, "priority")
    if category in PEOPLE and priority == "crucial":
        return 2
    if category in PEOPLE:
        return 3
    if category == "identity-resolution" or (category in ("marketing", "b2b") and set(entry.get("sources") or [])
                                             & {"aidr", "badbool"}):
        return 4
    return 5


def ensure_rows():
    """A row for every active broker in the registry, with its tier. Returns {id: row}."""
    entries = registry.load()
    exposed = exposed_domains()
    now = time.time()
    with vault.db() as conn:
        have = {r["id"]: dict(r) for r in conn.execute("SELECT * FROM brokers")}
        for broker_id, entry in entries.items():
            if registry.get(entry, "status") == "defunct":
                continue
            level = tier(entry, exposed)
            if broker_id not in have:
                conn.execute("INSERT INTO brokers (id, state, state_at, tier) VALUES (?, 'discovered', ?, ?)",
                             (broker_id, now, level))
                vault.history(conn, broker_id, "discovered", f"tier {level}")
            elif have[broker_id]["tier"] != level:
                conn.execute("UPDATE brokers SET tier = ? WHERE id = ?", (level, broker_id))
        rows = {r["id"]: dict(r) for r in conn.execute("SELECT * FROM brokers")}
    return rows


def set_state(conn, broker_id, state, detail=""):
    if state not in STATES:
        raise ValueError(state)
    old = conn.execute("SELECT state FROM brokers WHERE id = ?", (broker_id,)).fetchone()
    conn.execute("UPDATE brokers SET state = ?, state_at = ?, detail = ? WHERE id = ?",
                 (state, time.time(), detail or None, broker_id))
    vault.history(conn, broker_id, f"{old['state'] if old else '?'} → {state}", detail)


# ------------------------------------------------------------ methods and proposals

def method(entry, row=None):
    """The method the agent can actually use: email, form, search-form, privacy-manager, generic, manual."""
    found = registry.get(entry, "method")
    if row and row.get("broken"):
        return "manual"
    selectors = registry.get(entry, "selectors", {}) or {}
    complete = bool(selectors.get("fields")) and not registry.is_todo(selectors.get("submit", registry.TODO))
    if found in FORM_METHODS:
        if complete:
            return found
        return "generic" if entry.get("opt_out_url") else ("email" if entry.get("privacy_email") else "manual")
    if found == "email":
        return "email" if entry.get("privacy_email") else "manual"
    if found == "privacy-manager":
        return "privacy-manager"
    if registry.is_todo(entry.get("method")):
        return "email" if entry.get("privacy_email") else "generic" if entry.get("opt_out_url") else "manual"
    return "manual"


def manual_reason(entry):
    """Why this broker needs you whatever happens (its registry flags), or None."""
    if registry.get(entry, "id_required") is True:
        return "id"
    if registry.get(entry, "phone_required") is True:
        return "phone"
    if registry.get(entry, "account_required") is True:
        return "account"
    return None


def propose(broker_id, entry, row, found, listing_urls=()):
    """The request to put in front of you. {method, elements, missing, to|url, access, manual, why}. An
    email field always gets your own address, the one the request emails come from."""
    how = method(entry, row)
    elements = registry.required_elements(entry)
    if "listing_url" in elements and not listing_urls:
        # No listing found (yet): it isn't something you can fill in. A form that needs one can't be
        # used, so the request goes by email if the broker has an address, otherwise to you.
        elements.remove("listing_url")
        if how == "search-form":
            how = "email" if entry.get("privacy_email") else "manual"
    if how in ("generic", "privacy-manager") and "email" not in elements:
        elements.append("email")
    values = vault.element_values(found, "x@x", listing_urls)
    missing = [e for e in elements if e not in values and e != "email"]
    proposal = {"broker_id": broker_id, "method": how, "elements": elements, "missing": missing,
                "manual": manual_reason(entry)}
    if how == "email":
        proposal["to"] = entry["privacy_email"]
        proposal["access"] = registry.get(entry, "method") != "search-form" and not listing_urls and \
            config().get("access_request_with_deletion", True)
    elif how != "manual":
        proposal["url"] = entry.get("opt_out_url") or entry.get("website")
    else:
        proposal["url"] = entry.get("opt_out_url") or entry.get("website")
        proposal["manual"] = proposal["manual"] or "no-method"
    return proposal


def covers(approved, method_now, elements_now, asked=()):
    """(True, "") if the standing approval covers sending elements_now by method_now (and the details
    the broker asked for); otherwise (False, why)."""
    if not approved:
        return False, "never approved"
    allowed = set(approved.get("elements") or [])
    new = [e for e in list(elements_now) + list(asked) if e not in allowed]
    if new:
        return False, "would also send " + ", ".join(sorted(set(new)))
    # A different method (a form that needs a person, asked by email instead) is covered as long as it
    # sends nothing beyond the approved details: the check above.
    return True, ""


def advance(conn, broker_id, state, detail=""):
    """set_state, unless the broker is already settled (SETTLED). Returns True if it moved."""
    row = conn.execute("SELECT state FROM brokers WHERE id = ?", (broker_id,)).fetchone()
    if row and row["state"] in SETTLED:
        vault.history(conn, broker_id, f"stays {row['state']}", detail)
        return False
    set_state(conn, broker_id, state, detail)
    return True


def cancel_requests(conn, broker_id, why):
    """Cancel the broker's queued (and retrying) requests: you did what they were going to do."""
    count = conn.execute("UPDATE requests SET status = 'cancelled', detail = ? WHERE broker_id = ? AND status = 'queued'",
                         (why, broker_id)).rowcount
    if count:
        vault.history(conn, broker_id, f"cancelled {count} queued request(s)", why)
    return count


def set_pending(conn, broker_id, pending, why=""):
    """An action waiting for you: {"kind": confirm|more-info|follow-up|resubmit, ...}. None clears it."""
    conn.execute("UPDATE brokers SET pending = ? WHERE id = ?", (json.dumps(pending) if pending else None, broker_id))
    if pending:
        vault.history(conn, broker_id, f"waiting for you: {pending['kind']}", why)


def approve(conn, broker_id, proposal):
    """Record your approval of exactly this proposal."""
    approved = {"method": proposal["method"], "elements": proposal["elements"], "to": proposal.get("to"),
                "url": proposal.get("url"), "access": proposal.get("access", False)}
    conn.execute("UPDATE brokers SET approved = ?, approved_at = ? WHERE id = ?",
                 (json.dumps(approved), time.time(), broker_id))
    set_state(conn, broker_id, "approved", f"{proposal['method']}: {', '.join(proposal['elements'])}")
    return approved
