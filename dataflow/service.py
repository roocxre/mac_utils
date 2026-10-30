"""What the native host and the commands share: site settings, the extension's reports, site
information, the job queue (policy analyses and canary tests) and the exposures reported to the
broker agent.
"""
import base64
import hashlib
import json
import time
from urllib.parse import quote, urlsplit

from dataflow import DEFAULT_MODE, MODES, entities, lists, store
from lib import components, psl

RISKS = ("low", "medium", "high")


def config():
    return lists.config()


def site_of(url_or_domain):
    return psl.registrable_domain(url_or_domain or "")


# ------------------------------------------------------------ your addresses, as the extension looks for them

def real_emails():
    """Your real addresses: the Gmail account (mail), the broker vault's (brokers), config
    extra_emails. Each component counts only when it is installed. Cached a day."""
    cached = store.get("real_emails")
    if cached and time.time() - cached["at"] < 86400:
        return cached["emails"]
    found = set()
    if components.installed("mail"):
        try:
            from mail import store as mail_store
            if mail_store.get("address"):
                found.add(mail_store.get("address"))
        except Exception:
            pass
    if components.installed("brokers"):
        try:
            from brokers import vault
            found.update(vault.identity().get("emails") or [])
        except Exception:
            pass
    found.update(config().get("extra_emails") or [])
    emails = sorted({e.strip().lower() for e in found if e and "@" in e})
    store.put("real_emails", {"at": time.time(), "emails": emails})
    return emails


def _b64_stable(data, offset):
    """The part of base64(prefix + data) that doesn't depend on the bytes around it."""
    encoded = base64.b64encode(b"\0" * offset + data).decode()
    start = -(-8 * offset // 6)
    end = (8 * (offset + len(data)) - 6) // 6 + 1
    return encoded[start:end]


def needles(emails=None):
    """What the outgoing filter looks for: ci (case-insensitive) and cs (exact) strings per address."""
    ci, cs = set(), set()
    for email in emails if emails is not None else real_emails():
        email = email.strip().lower()
        raw = email.encode()
        ci.update({email, quote(email, safe=""), quote(email, safe="@"), hashlib.md5(raw).hexdigest(),
                   hashlib.sha1(raw).hexdigest(), hashlib.sha256(raw).hexdigest()})
        for offset in range(3):
            stable = _b64_stable(raw, offset)
            if len(stable) >= 8:
                cs.add(stable)
                cs.add(stable.replace("+", "-").replace("/", "_"))
        cs.add(base64.b64encode(hashlib.sha256(raw).digest()).decode().rstrip("="))
    return {"ci": sorted(ci), "cs": sorted(cs)}


def canary_needles(address):
    return needles([address.lower()])


# ------------------------------------------------------------ what the extension starts with

def hello():
    """Everything the extension needs at start: settings, block lists, your addresses' forms, fingerprints."""
    hand = lists.platforms()
    modes, overlay_off, version = store.modes()
    cats = hand.get("categories") or {}
    normal, wildcard, exception = psl._load()
    return {"version": version, "modes": modes, "overlay_off": overlay_off, "default_mode": DEFAULT_MODE,
            "psl": {"normal": sorted(normal), "wildcard": sorted(wildcard), "exception": sorted(exception)},
            "needles": needles(), "blocks": entities.block_lists(),
            "after_submit": sorted(set(cats.get("email-platform", [])) | set(cats.get("payment", []))),
            "login_hosts": sorted(hand.get("login_hosts") or []),
            "payment_frames": hand.get("payment_frames") or {}, "widgets": hand.get("widgets") or {},
            "consent_tools": hand.get("consent_tools") or {}, "lists_age_days": entities.age()}


def settings():
    modes, overlay_off, version = store.modes()
    return {"version": version, "modes": modes, "overlay_off": overlay_off, "default_mode": DEFAULT_MODE}


def set_mode(site, mode):
    domain = site_of(site)
    if not domain:
        raise ValueError("which site? give a domain like example.com")
    mode = (mode or "").strip().lower()
    if mode not in MODES:
        raise ValueError(f"mode is one of {', '.join(MODES)}")
    version = store.set_site(domain, mode=mode)
    return {"site": domain, "mode": mode, "version": version}


def set_overlay(site, on):
    domain = site_of(site)
    version = store.set_site(domain, overlay=bool(on))
    return {"site": domain, "overlay": bool(on), "version": version}


# ------------------------------------------------------------ resolving what the hooks saw

def resolve(site, urls):
    """{url: entity info} for the script and request URLs the hooks saw (by host, so per URL cheap)."""
    out = {}
    for url in urls[:500]:
        out[url] = entities.resolve(url, site)
    return out


# ------------------------------------------------------------ one site's information

def site_info(site, page_url=None, policy_links=(), signals=None, queue=True):
    """Mode, the site's own broker status, policy summary, latest canary. Queues a policy analysis
    when there is none or it is 30 days old."""
    domain = site_of(site)
    own = entities.resolve(domain)
    out = {"site": domain, "mode": store.site_mode(domain),
           "site_broker": {"id": own["broker"], "name": own.get("broker_name")} if own["broker"] else None}
    with store.db() as conn:
        policy = conn.execute("SELECT * FROM policies WHERE site = ?", (domain,)).fetchone()
        if queue and (policy_links or signals):
            policy = _queue_policy(conn, domain, page_url, list(policy_links), signals or {}, policy)
        canary = conn.execute("SELECT * FROM canaries WHERE site = ? ORDER BY id DESC LIMIT 1", (domain,)).fetchone()
        blocked = conn.execute("SELECT COUNT(*) FROM blocked WHERE site = ? AND at > ?",
                               (domain, time.time() - 30 * 86400)).fetchone()[0]
    out["policy"] = policy_summary(policy)
    out["canary"] = canary_summary(canary)
    out["blocked_30d"] = blocked
    return out


def _queue_policy(conn, domain, page_url, links, signals, row):
    now = time.time()
    days = config()["policy_days"]
    merged_signals = {**(store.loads(row["signals"], {}) if row else {}), **signals}
    candidates = links or (store.loads(row["candidates"], []) if row else [])
    stale = row is None or row["status"] in (None, "error") and now - (row["queued_at"] or 0) > 86400 or \
        (row["analyzed_at"] and now - row["analyzed_at"] > days * 86400)
    if row is None:
        conn.execute("INSERT INTO policies (site, candidates, signals, status, queued_at) VALUES (?, ?, ?, 'queued', ?)",
                     (domain, json.dumps(candidates), json.dumps(merged_signals), now))
    else:
        conn.execute("UPDATE policies SET candidates = ?, signals = ? WHERE site = ?",
                     (json.dumps(candidates), json.dumps(merged_signals), domain))
        if stale and row["status"] != "queued":
            conn.execute("UPDATE policies SET status = 'queued', queued_at = ? WHERE site = ?", (now, domain))
    if page_url and not candidates:
        conn.execute("UPDATE policies SET candidates = ? WHERE site = ?",
                     (json.dumps([f"{urlsplit(page_url).scheme}://{urlsplit(page_url).netloc}/privacy"]), domain))
    return conn.execute("SELECT * FROM policies WHERE site = ?", (domain,)).fetchone()


def policy_summary(row):
    if row is None:
        return {"status": "none"}
    signals = store.loads(row["signals"], {})
    out = {"status": row["status"], "url": row["url"], "signals": signals}
    result = store.loads(row["result"], {})
    if result:
        out["findings"] = result.get("findings", {})
        out["passages"] = len(result.get("passages", []))
        out["analyzed"] = time.strftime("%Y-%m-%d", time.localtime(row["analyzed_at"])) if row["analyzed_at"] else None
        out["recipients"] = result.get("recipients", [])[:12]
    if row["error"]:
        out["error"] = row["error"]
    return out


def policy_passages(site, data_type=None, page=1, per=10):
    from dataflow import policy
    domain = site_of(site)
    with store.db() as conn:
        row = conn.execute("SELECT * FROM policies WHERE site = ?", (domain,)).fetchone()
    if row is None or not row["result"]:
        return {"site": domain, "status": row["status"] if row else "none", "items": [], "total": 0, "page": 1,
                "pages": 1}
    result = store.loads(row["result"], {})
    items = policy.for_data_type(result.get("passages", []), data_type)
    shown = [{"text": p["text"][:600], "labels": p["labels"], "types": p.get("types", []), "link": p.get("link"),
              **({"nli": p["nli"]} if p.get("nli") else {})} for p in items]
    return {"site": domain, "url": row["url"], "data_type": data_type or "all",
            "items": shown[(page - 1) * per:page * per], "page": page, "pages": max(1, -(-len(shown) // per)),
            "total": len(shown)}


def canary_summary(row):
    if row is None:
        return None
    out = {"id": row["id"], "status": row["status"], "url": row["url"], "field_type": row["field_type"],
           "queued": time.strftime("%Y-%m-%d", time.localtime(row["queued_at"])) if row["queued_at"] else None}
    if row["ran_at"]:
        out["ran"] = time.strftime("%Y-%m-%d", time.localtime(row["ran_at"]))
    result = store.loads(row["result"], {})
    if result:
        out["leaked_to"] = result.get("leaked_to", [])
        out["consent_banner"] = result.get("consent_banner")
        out["requests"] = result.get("requests")
        out["field_found"] = result.get("field_found")
    if row["alias"]:
        out["address"] = row["alias"]               # the throwaway address the test typed
    if row["error"]:
        out["error"] = row["error"]
    return out


# ------------------------------------------------------------ what the extension saw on a page

def record_page(site, url, risk, fields, recipients):
    """Keep one page's findings (fields, their risk, recipients) and merge its recipients per site.
    Queues an automatic canary when a field is medium or high and the site has none in 30 days."""
    domain = site_of(site)
    clean = url.split("#")[0].split("?")[0][:500]
    now = time.time()
    risk = risk if risk in RISKS else "low"
    with store.db() as conn:
        conn.execute("INSERT OR REPLACE INTO pages (url, site, seen_at, risk, fields, recipients) VALUES (?, ?, ?, ?, ?, ?)",
                     (clean, domain, now, risk, json.dumps(fields[:60]), json.dumps(recipients[:80])))
        for item in recipients[:80]:
            if item.get("first_party") or not item.get("domain"):
                continue
            row = conn.execute("SELECT * FROM recipients WHERE site = ? AND domain = ?", (domain, item["domain"])).fetchone()
            actions = set(store.loads(row["actions"], []) if row else []) | set(item.get("actions", []))
            types = set(store.loads(row["field_types"], []) if row else []) | set(item.get("field_types", []))
            conn.execute("INSERT OR REPLACE INTO recipients VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                         (domain, item["domain"], item.get("owner"), json.dumps(item.get("categories", [])),
                          item.get("broker"), json.dumps(sorted(actions)), json.dumps(sorted(types)), now))
    queued = None
    risky = [f for f in fields if f.get("risk") in ("medium", "high") and f.get("type") not in ("password", "payment")]
    if risky:
        best = sorted(risky, key=lambda f: (f.get("risk") != "high", f.get("type") != "email"))[0]
        queued = queue_canary(domain, url, best.get("selector"), best.get("type"), why="auto")
    return {"stored": True, "canary": queued}


def queue_canary(site, url, selector, field_type, why="auto"):
    """Queue a canary test. Automatic ones: one per site per 30 days. On demand: not twice in 10 minutes."""
    domain = site_of(site or url)
    now = time.time()
    with store.db() as conn:
        last = conn.execute("SELECT * FROM canaries WHERE site = ? ORDER BY id DESC LIMIT 1", (domain,)).fetchone()
        if last and last["status"] in ("queued", "running"):
            return {"id": last["id"], "status": last["status"]}
        if last and why == "auto" and now - (last["queued_at"] or 0) < config()["canary_days"] * 86400:
            return None
        if last and why != "auto" and now - (last["queued_at"] or 0) < 600:
            return {"id": last["id"], "status": last["status"], "note": "tested in the last 10 minutes"}
        cursor = conn.execute("INSERT INTO canaries (site, url, field, field_type, status, why, queued_at) VALUES "
                              "(?, ?, ?, ?, 'queued', ?, ?)", (domain, url, selector, field_type, why, now))
        return {"id": cursor.lastrowid, "status": "queued"}


def record_blocked(site, domain, kind, detail=""):
    with store.db() as conn:
        conn.execute("INSERT INTO blocked (site, domain, kind, detail, at) VALUES (?, ?, ?, ?, ?)",
                     (site_of(site), domain, kind, str(detail)[:300], time.time()))
    return {"logged": True}


def submitted(site, url, real_email, domains):
    """A form went out. With your real email in it, broker-owned scripts on the page become top
    removal targets for the broker agent (when it is installed)."""
    out = {}
    if real_email and components.installed("brokers"):
        reported = []
        domain = site_of(site)
        for party in sorted(set(domains))[:60]:
            found = entities.resolve(party)
            if not found["broker"]:
                continue
            with store.db() as conn:
                if conn.execute("SELECT 1 FROM exposures WHERE site = ? AND broker = ?",
                                (domain, found["broker"])).fetchone():
                    continue
                conn.execute("INSERT INTO exposures VALUES (?, ?, ?, ?)", (domain, found["broker"], found["domain"],
                                                                         time.time()))
            try:
                from brokers import work
                work.record_exposure(found["domain"], "extension", f"its script was on {domain} when you typed "
                                                                   "your email there")
                reported.append(found["broker"])
            except Exception as error:
                out["error"] = f"{type(error).__name__}: {error}"
        out["reported_to_brokers"] = reported
    return out


# ------------------------------------------------------------ the queue

STALE_RUNNING = 900          # a job "running" this long belonged to a helper that died (Edge closed mid-test)


def next_job():
    """('policy', site) or ('canary', id) or None: the oldest queued job."""
    with store.db() as conn:
        stale = time.time() - STALE_RUNNING
        conn.execute("UPDATE canaries SET status = 'queued', queued_at = ? WHERE status = 'running' AND "
                     "COALESCE(started_at, queued_at) < ?", (time.time(), stale))
        conn.execute("UPDATE policies SET status = 'queued', queued_at = ? WHERE status = 'running' AND "
                     "COALESCE(started_at, queued_at) < ?", (time.time(), stale))
        policy = conn.execute("SELECT site, queued_at FROM policies WHERE status = 'queued' ORDER BY queued_at LIMIT 1"
                              ).fetchone()
        canary = conn.execute("SELECT id, queued_at FROM canaries WHERE status = 'queued' ORDER BY queued_at LIMIT 1"
                              ).fetchone()
    if policy and (not canary or policy["queued_at"] <= canary["queued_at"]):
        return ("policy", policy["site"])
    if canary:
        return ("canary", canary["id"])
    return None


def run_job(job, log=None):
    """Run one job. Returns the site it was for. A job that fails is marked so it isn't retried at once."""
    kind, ref = job
    try:
        if kind == "policy":
            from dataflow import policy
            policy.analyze_site(ref, log)
            return ref
        from dataflow import canary
        return canary.run(ref, log)
    except Exception as error:
        text = f"{type(error).__name__}: {str(error)[:200]}"
        with store.db() as conn:
            if kind == "policy":
                conn.execute("UPDATE policies SET status = 'error', error = ? WHERE site = ?", (text, ref))
                return ref
            conn.execute("UPDATE canaries SET status = 'error', error = ? WHERE id = ?", (text, ref))
            row = conn.execute("SELECT site FROM canaries WHERE id = ?", (ref,)).fetchone()
        if log:
            log.exception("job %s failed", job)
        return row["site"] if row else None


def queue_depth():
    with store.db() as conn:
        return {"policies": conn.execute("SELECT COUNT(*) FROM policies WHERE status = 'queued'").fetchone()[0],
                "canaries": conn.execute("SELECT COUNT(*) FROM canaries WHERE status = 'queued'").fetchone()[0]}
