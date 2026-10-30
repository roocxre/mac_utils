"""Per-daemon commands for the assistant.

Every agent can be asked about (`status X`, `logs X`). The ones that do a job can be run on
demand (`run X`, `X now`) or previewed (`preview X`), and a few have a command of their own:

    fixpock · audio · inbox / clear inbox · clean clipboard · capture text

The protected agents (sitemonitor, sitekeeper) only answer status and logs. Cleanup jobs ask
before running for real.
"""
import os
import re
import subprocess

import manage
from lib import components
from lib.common import state_path
from lib.identity import LOG_DIR

# agent -> (what running it does, supports --dry-run, extra arguments for an on-demand run)
JOBS = {
    "stalearchiver": ("move Desktop screenshots and items untouched for 14 days into ~/Archive, "
                      "and trash old installers", True, ()),
    "devclean": ("remove Python caches and node_modules/target of projects idle for 30 days", True, ()),
    "logtrimmer": ("delete crash reports over 7 days old and empty logs over 50 MB", True, ()),
    "packagemanagergc": ("clean the Homebrew, pip, npm, pnpm, yarn and uv caches", True, ()),
    "autounpack": ("unpack archives in ~/Downloads and trash them", True, ()),
    "screenshotcopy": ("copy the newest screenshot to the clipboard as an image", False, ()),
    "pockkeeper": ("restart Pock and the Control Strip", False, ("--force",)),
    "mailpoller": ("poll Gmail for new mail and hand it to the email agents", False, ()),
    "unsubscriber": ("analyse the week's inbox mail and show the unsubscribe report", False, ()),
}
ASK_FIRST = {"stalearchiver", "devclean", "logtrimmer", "packagemanagergc", "autounpack"}

# What people call them, for the words resolve_agent would not guess.
ALIASES = {
    "screenshots": "stalearchiver", "screenshot": "stalearchiver", "desktop": "stalearchiver",
    "archive": "stalearchiver", "downloads": "autounpack", "unpack": "autounpack",
    "archives": "autounpack", "dev": "devclean", "caches": "packagemanagergc",
    "cache": "packagemanagergc", "packages": "packagemanagergc", "brew": "packagemanagergc",
    "logs": "logtrimmer", "pock": "pockkeeper", "touchbar": "pockkeeper", "touch bar": "pockkeeper",
    "apple music": "appblocker", "music blocker": "appblocker", "app blocker": "appblocker",
    "bloatware": "appblocker",
    "sound": "audioguard", "audio": "audioguard", "headphones": "audioguard",
    "popups": "messagepopup", "messages": "messagepopup", "ocr": "screentext",
    "notifier": "cmdnotify", "commands": "cmdnotify", "clipboard": "clipboard",
    "browser guard": "sitemonitor", "site monitor": "sitemonitor", "site keeper": "sitekeeper",
    "gmail": "mailpoller", "email": "mailpoller", "mail poller": "mailpoller", "poller": "mailpoller",
    "inbox agent": "unsubscriber", "unsubscribe agent": "unsubscriber", "unsub": "unsubscriber",
}
# The installable part (lib/components.py) an agent belongs to, where the names differ.
PARTS = {"sitekeeper": "sitemonitor", "mailpoller": "mail", "unsubscriber": "unsub", "brokeragent": "brokers"}
# Parts with a setup of their own (Python packages, a Gmail sign-in): their jobs don't run without it.
NEEDS_SETUP = {"mail", "unsub", "brokers", "dataflow"}
MAX_LOG_LINES = 12
INBOX = state_path("inbox")


def component(agent):
    """The installable part an agent belongs to."""
    return PARTS.get(agent, agent)


def agent_for(core, word):
    word = re.sub(r"^(?:the|my)\s+", "", word.strip().lower()).strip()
    word = re.sub(r"\s+(?:agent|daemon|job|thing)$", "", word)
    return ALIASES.get(word) or core.resolve_agent(word)


def log_path(agent):
    return os.path.join(LOG_DIR, os.path.splitext(manage.AGENTS[agent]["script"])[0] + ".log")


def log_tail(agent, lines=MAX_LOG_LINES, since=0):
    """Last lines of an agent's log, without the date. since: only bytes after this offset."""
    try:
        with open(log_path(agent), "rb") as f:
            f.seek(since)
            text = f.read().decode("utf-8", "replace")
    except OSError:
        return ""
    rows = [line[11:] if re.match(r"\d{4}-\d\d-\d\d ", line) else line for line in text.splitlines() if line.strip()]
    return "\n".join(rows[-lines:])


def log_size(agent):
    try:
        return os.path.getsize(log_path(agent))
    except OSError:
        return 0


def run_job(agent, dry_run=False):
    """Run an agent's script once, right now. Returns a short report."""
    _what, _dry, extra = JOBS[agent]
    script = os.path.join(manage.ROOT, "agents", manage.AGENTS[agent]["script"])
    args = [manage.PYTHON, script, *extra] + (["--dry-run"] if dry_run else [])
    before = log_size(agent)
    try:
        result = subprocess.run(args, capture_output=True, text=True, timeout=900)
    except subprocess.TimeoutExpired:
        return f"{agent} was still going after 15 minutes; left it"
    output = result.stdout.strip() if dry_run else log_tail(agent, since=before)
    if result.returncode != 0:
        output = (output + "\n" + result.stderr.strip()[-400:]).strip()
        return f"{agent} failed (exit {result.returncode})\n{output}"
    prefix = "would do (nothing changed):\n" if dry_run else "done.\n"
    return prefix + (output or "nothing to report")


def status_line(core, agent):
    for line in core.run_manage("status").splitlines():
        if line.startswith(agent):
            return " ".join(line.split())
    return f"{agent}: not listed"


def audio_report():
    from lib import coreaudio

    device = coreaudio.default_output()
    info = coreaudio.describe(device) if device is not None else None
    if info is None:
        return "no output device"
    muted = coreaudio.get_mute(device)
    state = "muted" if muted else "not muted" if muted is not None else "mute unknown"
    return f"output: {info.name} ({info.transport}), {state}"


def inbox_report():
    from agents.message_popup import parse

    def listing(folder):
        out = []
        try:
            names = sorted(n for n in os.listdir(folder) if n.endswith(".json"))
        except OSError:
            return out
        for name in names:
            parsed = parse(os.path.join(folder, name))
            out.append(f"{parsed[0]}: {parsed[1][:80]}" if parsed else f"{name} (unreadable)")
        return out

    pending, failed = listing(INBOX), listing(os.path.join(INBOX, "failed"))
    if not pending and not failed:
        return "inbox is empty"
    parts = []
    if pending:
        parts.append(f"waiting ({len(pending)}):\n" + "\n".join(pending))
    if failed:
        parts.append(f"failed to show ({len(failed)}):\n" + "\n".join(failed))
    return "\n\n".join(parts)


def clear_failed():
    folder = os.path.join(INBOX, "failed")
    count = 0
    try:
        for name in os.listdir(folder):
            if name.endswith(".json"):
                os.remove(os.path.join(folder, name))
                count += 1
    except OSError:
        pass
    return f"cleared {count} failed message{'s' if count != 1 else ''}"


def clean_clipboard():
    from AppKit import NSPasteboard
    from agents import clipboard_tools
    import logging

    changed = clipboard_tools.process(NSPasteboard.generalPasteboard(), logging.getLogger("assistant"))
    return "clipboard cleaned up" if changed else "nothing on the clipboard needed fixing"


def capture_text():
    import logging
    from agents import screen_text

    text = screen_text.capture_text(logging.getLogger("assistant"))
    if not text:
        return "nothing copied"
    return f"copied {len(text)} characters:\n{text[:200]}"


def interpret(raw, low, core):
    """Return a Plan for a per-daemon command, or None if this isn't one."""
    Plan, Outcome = core.Plan, core.Outcome

    def simple(kind, summary, action, looks_only=False):
        return Plan(kind, summary, lambda: Outcome(action()), looks_only=looks_only)

    def job_plan(agent, dry_run=False):
        if agent not in JOBS:
            return None
        part = component(agent)
        if part in NEEDS_SETUP and not components.installed(part):
            return core._error(components.not_installed_message(part), final=True)
        what = JOBS[agent][0]
        if dry_run:
            if not JOBS[agent][1]:
                return core._error(f"{agent} has no preview; it would {what}", final=True)
            return simple("agents", f"preview what {agent} would do", lambda: run_job(agent, True), looks_only=True)
        if agent in ASK_FIRST:
            def ask():
                return Outcome(question=f"run {agent} now? it will {what}.", yes_label="run it",
                               on_yes=lambda: Outcome(run_job(agent)))
            return Plan("agents", f"run {agent} now: {what}", ask)
        return simple("agents", f"{what} now", lambda: run_job(agent))

    if re.match(r"^(?:fix ?pock|unstick pock|fix (?:the )?touch ?bar|pock|touch ?bar)$", low):
        return job_plan("pockkeeper")
    if low in ("audio", "audio status", "sound", "output", "where is the sound going"):
        return simple("agents", "report the audio output device", audio_report, looks_only=True)
    if low in ("inbox", "message inbox", "messages", "popups", "pending messages"):
        return simple("agents", "list the popup inbox", inbox_report, looks_only=True)
    if low in ("clear inbox", "clear failed messages", "clear failed"):
        return simple("agents", "delete popup messages that failed to show", clear_failed)
    if low in ("clean clipboard", "fix clipboard", "clean up clipboard", "clipboard"):
        return simple("agents", "tidy the clipboard (dedent code, clean a link)", clean_clipboard)
    if low in ("capture text", "ocr", "grab text", "read screen", "copy text from screen"):
        return simple("agents", "select a screen region and copy its text", capture_text)

    m = re.match(r"^(?:show |tail |see )?(?:logs?|log of|logs? (?:for|of))\s+(.+)$", low) \
        or re.match(r"^(?:show |see )?(.+?)\s+logs?$", low)
    if m:
        agent = agent_for(core, m.group(1))
        if agent:
            return simple("agents", f"show the end of {agent}'s log",
                          lambda: f"{agent}\n" + (log_tail(agent) or "log is empty"), looks_only=True)
    m = re.match(r"^(?:status (?:of )?|how(?:'s| is) (?:the )?)(.+?)\??$", low)
    if m:
        agent = agent_for(core, m.group(1))
        if agent:
            return simple("agents", f"report whether {agent} is running",
                          lambda: status_line(core, agent) + ("\n" + log_tail(agent, 4) if log_tail(agent, 4) else ""),
                          looks_only=True)
    m = re.match(r"^(?:preview|dry[- ]?run|what would)\s+(.+?)(?:\s+(?:do|delete|remove))?\??$", low)
    if m:
        agent = agent_for(core, m.group(1))
        if agent in JOBS:
            return job_plan(agent, dry_run=True)
    m = re.match(r"^(?:run|trigger|do)\s+(.+?)(?:\s+now)?$", low) or re.match(r"^(.+?)\s+now$", low) \
        or re.match(r"^(?:clean up|tidy|archive|unpack)\s+(.+)$", low)
    if m:
        agent = agent_for(core, m.group(1))
        if agent in JOBS:
            return job_plan(agent)
        if agent and re.match(r"^(?:run|trigger|do)\b|\bnow$", low):
            return core._error(f"{agent} runs all the time; try status or logs for it", final=True)
    return None
