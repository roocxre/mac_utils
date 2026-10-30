r"""Where-does-my-email-go commands, at the assistant's prompt (atlas/mail_commands.py) and in a
terminal (`python -m dataflow`). Answers are compact JSON with every list paged.

    dataflow status                          lists' age, models, queue, extension and helper
    dataflow site example.com                what's known about a site: recipients, brokers,
                                             policy, canary tests, mode
    dataflow recent since=7d risk=high       risky fields and sites seen lately
    dataflow mode example.com strict         observe, block (the default) or strict for a site
    dataflow canary url=https://…/signup     run a canary test now (type=email for the field)
    dataflow canaries example.com            a site's canary results
    dataflow policy example.com type=email   quoted policy passages for one kind of data
    dataflow refresh                         download the public lists again now

The long names work too: site_report, recent_findings, set_site_mode, canary_run,
canary_results, policy_findings.
"""
import os
import re
import subprocess
import time

from dataflow import MODES, entities, install, lists, models, service, store
from lib import psl
from lib.identity import LOG_DIR
from mail import ops as mail_ops

PER_PAGE = 10
MAX_PER_PAGE = 50
_ARG = re.compile(r"""(\w+)\s*=\s*(?:"([^"]*)"|'([^']*)'|(\S+))""")
Problem = mail_ops.Problem
as_json = mail_ops.as_json


def _paging(args):
    try:
        page = max(1, int(args.get("page", 1)))
        per = max(1, min(MAX_PER_PAGE, int(args.get("per", PER_PAGE))))
    except ValueError:
        raise Problem("page= and per= are numbers")
    return page, per


def _paged(rows, page, per):
    return {"items": rows[(page - 1) * per:page * per], "page": page, "pages": max(1, -(-len(rows) // per)),
            "total": len(rows)}


def _site(value):
    domain = psl.registrable_domain(value or "")
    if not domain or "." not in domain:
        raise Problem("which site? give a domain like example.com or a page's address")
    return domain


def _date(t):
    return time.strftime("%Y-%m-%d", time.localtime(t)) if t else None


# ------------------------------------------------------------ looking

def status():
    cfg = lists.config()
    with store.db() as conn:
        pages = conn.execute("SELECT COUNT(*) FROM pages WHERE seen_at > ?", (time.time() - 7 * 86400,)).fetchone()[0]
        risky = conn.execute("SELECT COUNT(*) FROM pages WHERE seen_at > ? AND risk = 'high'",
                             (time.time() - 7 * 86400,)).fetchone()[0]
        policies = conn.execute("SELECT status, COUNT(*) AS n FROM policies GROUP BY status").fetchall()
        canaries = conn.execute("SELECT status, COUNT(*) AS n FROM canaries GROUP BY status").fetchall()
        blocked = conn.execute("SELECT COUNT(*) FROM blocked WHERE at > ?", (time.time() - 7 * 86400,)).fetchone()[0]
    return {
        "lists_age_days": lists.age_days() and round(lists.age_days(), 1),
        "lists_due": lists.due(),
        "models": {name: models.available(name) for name in (cfg["nli"]["model"], "bert-base-NER")} |
                  {cfg["fields"]["model"]: os.path.exists(models.path(cfg["fields"]["model"], "model.safetensors")),
                   "field-head": os.path.exists(os.path.join(models.path(cfg["fields"]["head"])))},
        # PrivBERT isn't distributed; without it every policy passage counts (policy.py)
        "optional_models": {cfg["privbert"]["model"]: models.available(cfg["privbert"]["model"])},
        "queue": service.queue_depth(),
        "extension": install.status(),
        "helper_last_seen": _date(store.get("host_seen_at")),
        "pages_7d": pages, "high_risk_pages_7d": risky, "blocked_7d": blocked,
        "policies": {r["status"]: r["n"] for r in policies}, "canaries": {r["status"]: r["n"] for r in canaries},
        "sites_with_modes": len(store.modes()[0]),
    }


def site_report(site):
    domain = _site(site)
    info = service.site_info(domain, queue=False)
    with store.db() as conn:
        rows = conn.execute("SELECT * FROM recipients WHERE site = ? ORDER BY last_seen DESC", (domain,)).fetchall()
        pages = conn.execute("SELECT url, risk, seen_at FROM pages WHERE site = ? ORDER BY seen_at DESC LIMIT 5",
                             (domain,)).fetchall()
        blocked = conn.execute("SELECT domain, kind, COUNT(*) AS n FROM blocked WHERE site = ? GROUP BY domain, kind "
                               "ORDER BY n DESC LIMIT 10", (domain,)).fetchall()
    recipients = [{"domain": r["domain"], "owner": r["entity"], "categories": store.loads(r["categories"], []),
                   "actions": store.loads(r["actions"], []), "fields": store.loads(r["field_types"], []),
                   **({"broker": r["broker"]} if r["broker"] else {})} for r in rows[:25]]
    return {
        "site": domain, "mode": info["mode"], "site_is_broker": info["site_broker"],
        "recipients": {"total": len(rows), "items": recipients},
        "brokers": sorted({r["broker"] for r in rows if r["broker"]}),
        "policy": {k: v for k, v in info["policy"].items() if k in ("status", "url", "analyzed", "signals", "error")} |
                  {"findings": {k: {kk: vv for kk, vv in v.items() if kk in ("verdict", "quote")}
                                for k, v in (info["policy"].get("findings") or {}).items()}},
        "canary": info["canary"],
        "blocked": [{"domain": b["domain"], "how": b["kind"], "times": b["n"]} for b in blocked],
        "pages": [{"url": p["url"], "risk": p["risk"], "seen": _date(p["seen_at"])} for p in pages],
    }


def recent_findings(since=None, risk=None, page=1, per=PER_PAGE):
    cutoff = mail_ops._since(since) if since else time.time() - 7 * 86400
    wanted = ("high",) if risk == "high" else ("high", "medium") if risk in (None, "", "medium") else \
        ("high", "medium", "low")
    with store.db() as conn:
        rows = conn.execute(f"SELECT * FROM pages WHERE seen_at >= ? AND risk IN ({','.join('?' * len(wanted))}) "
                            "ORDER BY seen_at DESC", (cutoff, *wanted)).fetchall()
    items = []
    for r in rows:
        fields = [f for f in store.loads(r["fields"], []) if f.get("risk") in wanted]
        recipients = store.loads(r["recipients"], [])
        items.append({"url": r["url"], "site": r["site"], "risk": r["risk"], "seen": _date(r["seen_at"]),
                      "fields": [{"type": f.get("type"), "risk": f.get("risk"), **({"widget": f["widget"]}
                                                                                    if f.get("widget") else {})}
                                 for f in fields[:6]],
                      "third_parties": [{"domain": x["domain"], "owner": x.get("owner"),
                                         "categories": [c for c in x.get("categories", []) if c in entities.HIGH +
                                                        entities.MEDIUM], **({"broker": x["broker"]} if x.get("broker")
                                                                             else {})}
                                        for x in recipients[:8]]})
    return _paged(items, page, per)


def policy_findings(site, data_type=None, page=1, per=PER_PAGE):
    domain = _site(site)
    out = service.policy_passages(domain, data_type, page, per)
    if out.get("status") in ("none", None) and not out["items"]:
        with store.db() as conn:
            service._queue_policy(conn, domain, f"https://{domain}/", [], {}, None)
        _spawn(["work"])
        out["note"] = "no policy read yet for this site: reading it now; ask again in a minute"
    return out


def canary_results(site, page=1, per=PER_PAGE):
    domain = _site(site)
    with store.db() as conn:
        rows = conn.execute("SELECT * FROM canaries WHERE site = ? ORDER BY id DESC", (domain,)).fetchall()
    return _paged([service.canary_summary(r) for r in rows], page, per)


# ------------------------------------------------------------ doing

def set_site_mode(site, mode=None):
    domain = _site(site)
    if not mode:
        return {"site": domain, "mode": store.site_mode(domain), "modes": list(MODES)}
    try:
        result = service.set_mode(domain, mode)
    except ValueError as error:
        raise Problem(str(error))
    result["note"] = "Edge picks this up within a few seconds (reload the page for blocking rules to apply)"
    return result


def canary_run(url, field_type=None, selector=None):
    if not url or not re.match(r"^https?://", url):
        raise Problem("canary_run needs the page's full address: dataflow canary url=https://example.com/signup")
    queued = service.queue_canary(psl.registrable_domain(url), url, selector, field_type or "email", why="you")
    _spawn(["work"])
    return {"queued": queued, "note": "a hidden browser types a throwaway canary address into the field and watches "
                                      "the network for ~10 s, never submitting. dataflow canaries <site> shows the result"}


def refresh():
    _spawn(["refresh"])
    return {"started": "downloading the public lists (Tracker Radar, Disconnect, EasyPrivacy, PSL) and rebuilding "
                       "the index"}


def _spawn(args):
    import manage
    log = os.path.join(LOG_DIR, "dataflow.log")
    with open(log, "a") as out:
        subprocess.Popen([manage.PYTHON, "-m", "dataflow", *args], stdout=out, stderr=out, stdin=subprocess.DEVNULL,
                         cwd=manage.ROOT, start_new_session=True)


# ------------------------------------------------------------ the text form

OPS = {
    # name: (keys, first key, looks only, summary)
    "dataflow status": ((), None, True, "where-does-my-email-go: lists, models, queue, extension"),
    "dataflow site": (("site",), "site", True, "a site's recipients, brokers, policy, canary tests, mode"),
    "dataflow recent": (("since", "risk", "page", "per"), "since", True, "risky fields and sites seen lately"),
    "dataflow mode": (("site", "mode"), "site", False, "a site's blocking mode: observe, block or strict"),
    "dataflow canary": (("url", "type", "selector"), "url", False, "run a canary test on a page's field"),
    "dataflow canaries": (("site", "page", "per"), "site", True, "a site's canary test results"),
    "dataflow policy": (("site", "type", "page", "per"), "site", True, "quoted policy passages for one data type"),
    "dataflow refresh": ((), None, False, "download the public tracker and broker lists again"),
}
# the long names (and a few loose verbs) -> the verbs
OTHER_NAMES = {"site_report": "site", "recent_findings": "recent", "set_site_mode": "mode", "canary_run": "canary",
                 "canary_results": "canaries", "policy_findings": "policy", "report": "site", "findings": "recent",
                 "results": "canaries", "set_mode": "mode"}
_CALL = re.compile(r"^(dataflow|site_report|recent_findings|set_site_mode|canary_run|canary_results|policy_findings)"
                   r"(?:[ _](\w+))?\b\s*(.*)$", re.IGNORECASE | re.DOTALL)


def parse(text):
    """(op, {key: value}) for a `dataflow` command, or None if the text is not one. Raises Problem."""
    match = _CALL.match(text.strip())
    if not match:
        return None
    head, verb, rest = match.group(1).lower(), (match.group(2) or "").lower(), match.group(3)
    if head != "dataflow":
        verb, rest = OTHER_NAMES[head], text.strip()[len(head):]
    verb = OTHER_NAMES.get(verb, verb or "status")
    op = "dataflow " + verb
    if op not in OPS:
        raise Problem(f"no dataflow {verb}. " + usage())
    keys, first_key, _looks, _summary = OPS[op]
    args = {m.group(1).lower(): next(g for g in m.groups()[1:] if g is not None) for m in _ARG.finditer(rest)}
    loose = _ARG.sub(" ", rest).strip().strip("\"'").split()
    if op == "dataflow mode" and len(loose) == 2 and not args:
        args = {"site": loose[0], "mode": loose[1]}
    elif loose:
        if not first_key or first_key in args or len(loose) > 1:
            raise Problem(f"{op} does not take {' '.join(loose)!r}. it takes: "
                          f"{', '.join(k + '=' for k in keys) or 'nothing'}")
        args[first_key] = loose[0]
    unknown = [k for k in args if k not in keys]
    if unknown:
        raise Problem(f"{op} has no {unknown[0]}=. it takes: {', '.join(k + '=' for k in keys) or 'nothing'}")
    return op, args


def looks_only(op, args=None):
    if op == "dataflow mode" and not (args or {}).get("mode"):
        return True
    return OPS[op][2]


def run(op, args):
    try:
        if op == "dataflow status":
            result = status()
        elif op == "dataflow site":
            result = site_report(args.get("site"))
        elif op == "dataflow recent":
            page, per = _paging(args)
            result = recent_findings(args.get("since"), args.get("risk"), page, per)
        elif op == "dataflow mode":
            result = set_site_mode(args.get("site"), args.get("mode"))
        elif op == "dataflow canary":
            result = canary_run(args.get("url"), args.get("type"), args.get("selector"))
        elif op == "dataflow canaries":
            page, per = _paging(args)
            result = canary_results(args.get("site"), page, per)
        elif op == "dataflow policy":
            page, per = _paging(args)
            result = policy_findings(args.get("site"), args.get("type"), page, per)
        elif op == "dataflow refresh":
            result = refresh()
        else:
            raise Problem(f"no operation {op}")
    except Problem as problem:
        return str(problem)
    return as_json(result)


def call(text):
    try:
        parsed = parse(text)
    except Problem as problem:
        return str(problem)
    if parsed is None:
        return "not a dataflow command. " + usage()
    return run(*parsed)


def usage():
    return "commands: " + " · ".join(f"{op} {' '.join(k + '=' for k in spec[0])}".strip() for op, spec in OPS.items())
