"""A dry run of the form filler over brokers' opt-out pages, to find the kinds of form it gets wrong
before a real request meets them ("brokers formcheck").

Each page is filled up to its submit button, which is named and never pressed (forms.DRY), with
made-up details, not yours: some sites record what's typed before anything is sent. The email is on
example.com, which is reserved and reaches no one, and the phone is in the 555-01xx range kept for
fiction.

The sample (SAMPLE pages, fixed when a run starts, so a run picks up where it stopped after the Mac
sleeps) is every broker approved for a web form, a share of the registry's saved form definitions,
and the rest from the opt-out pages of brokers we email (where "use our form" replies point). Each
result gets a kind (would submit, CAPTCHA, sign-in, a field it can't place…); the summary says when
each kind was first seen, so you can tell when new kinds stop turning up (no new kind in the last N
pages: an unseen kind is likely on fewer than 3/N of forms).

It runs in its own browser profile, so it never collides with the agent's passes, and everything
it does shows in the watch window ("brokers debug").
"""
import json
import os
import random
import re
import time

from brokers import forms, registry, vault, watch
from lib.common import state_path

RESULTS = state_path("broker-formcheck.json")
PROFILE = state_path("brokers-formcheck-browser")
SAMPLE = 100
DEFINED = 25                # of the sample: the registry's saved form definitions (likely stale)
SEED = 20261028
PAUSE = 5                   # seconds between pages
SESSION_PAGES = 20          # a fresh browser every so many pages
OFFLINE_WAIT = 60
NETWORK = re.compile(r"ERR_INTERNET_DISCONNECTED|ERR_NAME_NOT_RESOLVED|ERR_NETWORK_CHANGED|ERR_ADDRESS_UNREACHABLE|"
                     r"ERR_NETWORK_IO_SUSPENDED")
FAKE = {"first_name": "Jane", "last_name": "Doe", "full_name": "Jane Doe",
        "email": "jane.doe@example.com", "phone": "555-555-0100", "street": "100 Example St",
        "city": "Anytown", "state": "NY", "zip": "12345", "city_state": "Anytown, NY",
        "address": "100 Example St, Anytown, NY 12345", "dob": "1990-01-01", "age": "36"}


def _load():
    try:
        with open(RESULTS, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def _save(state):
    tmp = RESULTS + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=1)
    os.replace(tmp, RESULTS)


def _sample(n):
    """[(broker_id, how, url)]: approved web forms, saved definitions, then opt-out pages of email brokers."""
    entries = registry.load()
    with vault.db() as conn:
        approved = {r["id"]: json.loads(r["approved"]) for r in conn.execute("SELECT id, approved FROM brokers WHERE "
                                                                            "approved IS NOT NULL")}
    rng = random.Random(SEED)
    page = lambda e: e.get("opt_out_url") or e.get("website")
    forms_now = [(b, "generic", a.get("form") or page(entries[b])) for b, a in sorted(approved.items())
                 if a["method"] == "generic" and b in entries and page(entries[b])]
    defined = sorted(b for b, e in entries.items() if registry.get(e, "method", "") in ("direct-form", "search-form")
                     and e.get("selectors") and page(e) and b not in {f[0] for f in forms_now})
    defined = [(b, registry.get(entries[b], "method"), page(entries[b]))
               for b in rng.sample(defined, min(DEFINED, len(defined)))]
    taken = {b for b, _, _ in forms_now + defined}
    pages = sorted(b for b, e in entries.items() if registry.get(e, "method", "") == "email" and e.get("opt_out_url")
                   and b not in taken)
    rest = max(0, n - len(forms_now) - len(defined))
    picked = [(b, "generic", entries[b]["opt_out_url"]) for b in rng.sample(pages, min(rest, len(pages)))]
    return (forms_now + defined + picked)[:n]


def _online():
    """True if this Mac can reach the internet at all (so a page that won't load is that site's problem)."""
    import socket
    for host in ("one.one.one.one", "dns.google"):
        try:
            socket.create_connection((host, 443), timeout=5).close()
            return True
        except OSError:
            continue
    return False


def _iframe_form(context, url, domains):
    """A form the page holds in a frame of the broker's own site (the filler doesn't go into frames)."""
    page = context.new_page()
    try:
        page.goto(url, wait_until="domcontentloaded")
        page.wait_for_timeout(2500)
        for frame in page.frames[1:]:
            if forms.site(frame.url) in domains and frame.locator("input:visible, textarea:visible").count() >= 2:
                return frame.url
    except Exception:
        pass
    finally:
        page.close()
    return None


def _kind(result, iframe):
    """The kind of outcome, the key the summary groups by."""
    detail = result.detail
    if result.outcome == "dry":
        return "would submit"
    if result.manual == "no-form":
        if iframe:
            return "form inside a frame"
        if "went to" in detail:
            return "left the broker's site"
        return "no form found"
    if result.manual == "field":
        return "a required field it can't place" if "can't tell" in detail else "needs more details"
    if result.manual:
        if result.manual == "captcha" and "Cloudflare" in detail:
            return "Cloudflare check"
        return {"captcha": "CAPTCHA", "account": "sign-in or account", "id": "ID upload", "payment": "payment",
                "residency": "residency question", "blocked": "site blocked us"}.get(result.manual, result.manual)
    if result.outcome == "broken":
        if "not on the page" in detail:
            return "saved definition out of date"
        return "form didn't accept it" if "didn't accept" in detail else "no submit button / broken"
    return "page didn't load or failed"


def run(log, n=SAMPLE, fresh=False):
    """Check up to n pages (resuming the saved run unless fresh). Returns the summary."""
    from mail import store as mail_store
    with mail_store.file_lock("broker-formcheck", wait=False) as held:
        if not held:
            return {"skipped": "a form check is already running"}
        state = None if fresh else _load()
        if not state or state.get("n") != n:
            state = {"n": n, "started": time.time(), "sample": _sample(n), "results": {}}
            _save(state)
        todo = [s for s in state["sample"] if s[0] not in state["results"]]
        watch.say(f"form check: {len(todo)} of {len(state['sample'])} pages to go (dry run, made-up details, "
                  "nothing submitted)", "pass")
        forms.DRY = True
        try:
            return _check(log, state, todo)
        finally:
            forms.DRY = False


def _check(log, state, todo):
    entries = registry.load()
    while todo:
        with forms.session(profile=PROFILE) as context:
            for broker_id, how, url in todo[:SESSION_PAGES]:
                entry = dict(entries.get(broker_id, {}), opt_out_url=url)
                number = next(i for i, s in enumerate(state["sample"], 1) if s[0] == broker_id)
                watch.say(f"form check {number}/{len(state['sample'])}: {entry.get('name', broker_id)} ({how})",
                          "pass", broker_id, url=url)
                started, mark = time.time(), os.path.getsize(watch.EVENTS) if os.path.exists(watch.EVENTS) else 0
                if url.lower().split("?")[0].endswith(".pdf"):
                    result = forms.Result(False, "manual", "the opt-out link is a PDF", manual="pdf")
                else:
                    try:
                        result = forms.run(entry, how, FAKE, context=context)
                    except Exception as error:
                        text = f"{type(error).__name__}: {str(error)[:200]}"
                        if NETWORK.search(text) and not _online():
                            watch.say("this Mac is offline: waiting, then trying this page again", "warn")
                            time.sleep(OFFLINE_WAIT)
                            break
                        result = forms.Result(False, "failed", text)   # online, so it's this site
                domains = set(entry.get("domains") or []) | {forms.site(entry.get("website"))}
                iframe = _iframe_form(context, url, domains) if result.manual == "no-form" else None
                kind = "PDF, not a page" if result.manual == "pdf" else _kind(result, iframe)
                steps = _steps(mark)
                state["results"][broker_id] = {
                    "n": number, "name": entry.get("name", broker_id), "how": how, "url": url, "kind": kind,
                    "detail": result.detail, "ended_at": result.url, "fields": sorted(result.sent),
                    "needs": result.needs, "iframe": iframe, "steps": steps,
                    "seconds": round(time.time() - started)}
                _save(state)
                watch.say(f"→ {kind}" + (f": {result.detail}" if kind != "would submit" else
                                         f" ({', '.join(sorted(result.sent))})"),
                          "done" if kind == "would submit" else "warn", broker_id)
                time.sleep(PAUSE)
        todo = [s for s in state["sample"] if s[0] not in state["results"]]
    state["finished"] = time.time()
    _save(state)
    out = summary(state)
    watch.say(f"form check finished: {out['headline']}", "pass")
    return out


def _steps(mark):
    """This process's form lines since mark: what it filled, chose and would press."""
    try:
        with open(watch.EVENTS, encoding="utf-8") as f:
            f.seek(mark)
            lines = [json.loads(line) for line in f if line.strip()]
    except (OSError, ValueError):
        return []
    return [e["text"] for e in lines if e.get("pid") == os.getpid() and e["kind"] in ("form", "stop", "done", "warn")
            and not e["text"].startswith("page: ")][:60]


def summary(state=None):
    """Kinds of outcome, how often, which brokers, and when each kind first turned up."""
    state = state or _load()
    if not state:
        return {"headline": "no form check has run yet"}
    results = sorted(state["results"].values(), key=lambda r: r["n"])
    kinds = {}
    for result in results:
        kind = kinds.setdefault(result["kind"], {"count": 0, "first_seen": result["n"], "brokers": []})
        kind["count"] += 1
        kind["brokers"].append(f"{result['name']}: {result['detail'][:120]}")
    checked = len(results)
    last_new = max((k["first_seen"] for k in kinds.values()), default=0)
    quiet = checked - last_new
    return {"headline": f"{checked} of {len(state['sample'])} pages checked, {len(kinds)} kinds; no new kind in the "
                        f"last {quiet}" + (f" (an unseen kind is likely on under {round(300 / quiet)}% of forms)"
                                           if quiet >= 10 else ""),
            "kinds": dict(sorted(kinds.items(), key=lambda kv: -kv[1]["count"])),
            "would_submit_fields": _field_counts(results)}


def _field_counts(results):
    counts = {}
    for result in results:
        for field in result["fields"]:
            counts[field] = counts.get(field, 0) + 1
    return dict(sorted(counts.items(), key=lambda kv: -kv[1]))
