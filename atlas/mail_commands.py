"""The email commands at the prompt (each package's ops.py does the work).

    mail status    mail setup file=~/Downloads/client_secret_….json    (mail/ops.py)
    inbox report · spam · unsub    (open the inbox dashboard: atlas/inbox_dashboard.py; a banner says the counts)
    unsub report section=unsure    (the entries as JSON; no window)
    unsub decide approve=s3,s5 reject=s7    unsub status    (unsub/ops.py)
    broker report · brokers · data brokers   (open the broker dashboard: atlas/broker_dashboard.py)
    brokers debug · brokers watch   (watch the broker agent live, its steps and its browser: atlas/broker_watch.py)
    brokers formcheck [report]   (dry-run the form filler on 100 opt-out pages, in the background: brokers/formcheck.py)
    brokers report group=manual · brokers decide approve=spokeo · brokers info spokeo   (brokers/ops.py; your
    details are never reachable from here, only from the dashboard)
    dataflow site example.com · dataflow recent · dataflow mode example.com strict
    (dataflow/ops.py: what the Edge extension found; its overlay in the browser is the human interface)

Answers are compact JSON. Each package is a component of its own: when it isn't installed, the
command says so instead of importing it. Signing in (`mail setup`) is kind "yours", the kind only
you start; it runs in a process of its own, as it waits for the browser.
"""
import os
import re
import subprocess

from lib import components
from lib.identity import LOG_DIR

_MAIL_HEAD = re.compile(r"^mail(?:\s+(?:status|setup)\b.*)?$", re.IGNORECASE | re.DOTALL)
_UNSUB_HEAD = re.compile(r"^(?:unsub\w*|spam)\b", re.IGNORECASE)
_DATAFLOW_HEAD = re.compile(r"^(?:dataflow|site_report|recent_findings|set_site_mode|canary_run|canary_results|"
                            r"policy_findings)\b", re.IGNORECASE)
_BROKER_HEAD = re.compile(r"^(?:brokers?|broker_\w+|manual_done|registry_validate|optout)\b", re.IGNORECASE)
# Spoken to open the inbox dashboard. `unsub report`, `spam` and plain `unsub` open it too, through ops.
_DASHBOARD = re.compile(r"^(?:open\s+|show\s+(?:me\s+)?)?(?:the\s+|my\s+)?(?:inbox|e-?mail|mail|spam|junk|unsubscribe)\s+"
                        r"(?:report|dashboard|review)$", re.IGNORECASE)
_BROKER_WATCH = re.compile(r"^(?:open\s+|show\s+(?:me\s+)?)?(?:the\s+)?(?:data\s+)?brokers?(?:\s+agent)?\s+"
                           r"(?:debug|watch|live|monitor)$|^watch\s+(?:the\s+)?(?:data\s+)?brokers?(?:\s+agent)?$",
                           re.IGNORECASE)
_BROKER_DASHBOARD = re.compile(r"^(?:open\s+|show\s+(?:me\s+)?)?(?:the\s+|my\s+)?(?:data\s+)?(?:brokers?|"
                               r"people[\s-]search|opt[\s-]?outs?|data\s+removal)(?:\s+(?:report|dashboard|review|"
                               r"removal|requests))?$", re.IGNORECASE)
# What each component is called when it isn't installed.
_TITLES = {"mail": "The email layer", "unsub": "The inbox unsubscribe agent",
           "brokers": "The data broker removal agent", "dataflow": "The Edge extension"}


def _not_installed(component, core):
    """A plan saying the component isn't installed, or None when it is."""
    if components.installed(component):
        return None
    return core._error(components.not_installed_message(component, _TITLES[component]), final=True)


def _ops(component):
    """The component's ops module (imported only once it is known to be installed)."""
    if component == "dataflow":
        from dataflow import ops
    elif component == "brokers":
        from brokers import ops
    elif component == "unsub":
        from unsub import ops
    else:
        from mail import ops
    return ops


def _setup(file, core):
    import manage

    path = core.expand(file)
    if not os.path.isfile(path):
        return core.Outcome(f"no file at {core.abbr(path)}. it is the Desktop app OAuth client JSON from Google Cloud")
    log = os.path.join(LOG_DIR, "mail_setup.log")
    with open(log, "a") as out:
        subprocess.Popen([manage.PYTHON, "-m", "mail", "setup", path, "--notify"], stdout=out, stderr=out,
                         stdin=subprocess.DEVNULL, cwd=manage.ROOT, start_new_session=True)
    return core.Outcome("signing in to Gmail: finish it in the browser window. a notification says how it went "
                        f"({core.abbr(log)})")


def _dashboard(ops, core):
    """Open the inbox dashboard; the answer is one line of counts (shown as a banner, like the battery helper)."""
    from atlas import inbox_dashboard

    inbox_dashboard.open_dashboard()
    return core.Outcome(ops.summary_line())


def _broker_dashboard(ops, core):
    """Open the broker dashboard; the answer is one line of counts (a banner)."""
    from atlas import broker_dashboard

    broker_dashboard.open_dashboard()
    return core.Outcome(ops.summary_line())


def _broker_watch(core):
    """Open the live watch window."""
    from atlas import broker_watch

    broker_watch.open_watch()
    return core.Outcome("watching the broker agent: its steps on the left, its browser on the right")


_FORMCHECK = re.compile(r"^brokers?\s+form\s?checks?(?:\s+(report|results|status))?$", re.IGNORECASE)


def _formcheck(report, core):
    """Start the dry run in the background and watch it; or say how far it got."""
    from brokers import formcheck
    if report:
        found = formcheck.summary()
        lines = [found["headline"]] + [f"{kind}: {info['count']}" for kind, info in (found.get("kinds") or {}).items()]
        return core.Outcome("\n".join(lines))
    import manage
    log = os.path.join(LOG_DIR, "broker_formcheck.log")
    with open(log, "a") as out:
        subprocess.Popen([manage.PYTHON, "-m", "brokers", "formcheck"], stdout=out, stderr=out, stdin=subprocess.DEVNULL,
                         cwd=manage.ROOT, start_new_session=True)
    from atlas import broker_watch
    broker_watch.open_watch()
    return core.Outcome("form check started: 100 opt-out pages, made-up details, nothing submitted. it's in the watch "
                        "window; \"brokers formcheck report\" says what it found")


def interpret(raw, low, core):
    """A Plan for an email command, or None."""
    text = raw.strip()
    check = _FORMCHECK.match(text)
    if check:
        return _not_installed("brokers", core) or core.Plan(
            "dashboard", "dry-run the broker form filler", lambda: _formcheck(bool(check.group(1)), core))
    if _BROKER_WATCH.match(text):
        return _not_installed("brokers", core) or core.Plan(
            "dashboard", "watch the broker agent live", lambda: _broker_watch(core))
    if _DASHBOARD.match(text):
        text = "unsub report"
    if _BROKER_DASHBOARD.match(text):
        text = "brokers report"
    if _DATAFLOW_HEAD.match(text):
        component = "dataflow"
    elif _BROKER_HEAD.match(text):
        component = "brokers"
    elif _UNSUB_HEAD.match(text):
        component = "unsub"
    elif _MAIL_HEAD.match(text):
        component = "mail"
    else:
        return None
    missing = _not_installed(component, core)
    if missing:
        return missing
    ops = _ops(component)
    try:
        parsed = ops.parse(text)
    except ops.Problem as problem:
        return core._error(str(problem))
    if parsed is None:
        return None
    op, args = parsed
    if op == "mail setup":
        if not args.get("file"):
            return core._error(ops.run(op, args), final=True)
        return core.Plan("yours", "sign in to Gmail in the browser", lambda: _setup(args["file"], core))
    summary = ops.OPS[op][3]
    if op == "unsub report" and not args:
        return core.Plan("dashboard", "open the inbox dashboard", lambda: _dashboard(ops, core))
    if op == "brokers report" and not args:
        return core.Plan("dashboard", "open the broker dashboard", lambda: _broker_dashboard(ops, core))
    return core.Plan("mail", f"{summary}: {text}", lambda: core.Outcome(ops.run(op, args)))
