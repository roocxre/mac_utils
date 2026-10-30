"""Every command the assistant knows, in one list that can be searched.

    tools stale cleanup        the commands closest to those words, each with what it does
    tools                      all of them, names only

When a request isn't a command at all, the closest commands come back with the miss, so you can
see what to type instead.

The list is put together from the code that carries the commands out (the agents' job table, the
shortcuts, the four tools), so a new one shows up here by itself. Only the always-running agents
need a line of their own, in DAEMONS below. Commands of parts that aren't installed
(lib/components.py) are left out. Ranking is by the same small sentence transformer that
intent.py uses, plus a little for words that appear outright; no language model.
"""
import re
from collections import namedtuple

import numpy as np

from atlas import agent_commands
from atlas import terminal
from atlas import tools
from lib import components

# command: a real example of the command, exactly as it can be written. what: what it does.
# looks: it only reports. words: other things people call it, for the search only.
Entry = namedtuple("Entry", "command what looks words", defaults=(False, ""))

MAX_SHOWN = 4               # commands given back for a search
MAX_SUGGESTED = 3           # commands given back with a miss
MIN_SCORE = 0.25            # below this nothing is close enough to be worth showing
WORD_BONUS = 0.12           # per word of the request that appears outright in an entry

# The always-running agents: what each one is for. (The ones that do a job are in agent_commands.JOBS.)
DAEMONS = {
    "audioguard": "pauses and mutes when headphones drop out to the speakers",
    "appblocker": "quits apple music, tv, podcasts, tips, freeform and image playground whenever they open",
    "clipboard": "dedents copied code and cleans copied links",
    "screentext": "copies the text of a screen region on Cmd+Shift+2",
    "cmdnotify": "alerts when a long terminal command finishes",
    "messagepopup": "shows messages other programs drop in its inbox folder as banners",
}
PROTECTED = {"sitemonitor": "quits the browser on a blocked site", "sitekeeper": "keeps sitemonitor loaded"}

_INFO = {
    "battery": "battery level, charging state and health", "disk": "free disk space",
    "memory": "how much memory is in use and which apps use the most (looks only, quits nothing)",
    "cpu": "the processor and which apps are busiest", "uptime": "how long the mac has been on, last restart",
    "network": "ip address, router and dns", "ports": "which programs are listening on which ports",
    "processes": "how many processes are running and the busiest", "system": "macOS version, model, chip, screen",
    "time": "the date, time, timezone and week number", "apps": "installed apps, or whether one is installed and running",
}

_FIXED = [
    Entry("files where=downloads sort=newest top=5", "list a folder. sort=newest|oldest|biggest|smallest|added, kind=pdf|image|video|code…, "
          "newer=7d, over=10mb", True, "ls what is in folder contents newest biggest largest recent"),
    Entry("files where=code kind=repo", "list the git repositories in a folder and its subfolders, with each one's branch", True,
          "git repos repositories projects which folders are under git"),
    Entry('files name="meeting notes"', "find a file or folder by name and give its real path", True, "where is locate path"),
    Entry("files where=documents show=size", "how big a folder is. show=count|kinds|tree for a count, a breakdown by kind, or a tree",
          True, "size how big how many count space taken"),
    Entry("read file=notes.txt", "show a text file. part=last lines=20 for the end, from=40 for a line number, show=count for its length",
          True, "cat open contents says head tail lines words"),
    Entry("text find=TODO where=code", "find lines mentioning something inside files. show=count|files, regex=yes, around=2",
          True, "grep search mentions contains used"),
    Entry("search inside lease deposit pdfs", "find files anywhere on the mac by what is in them or their kind (Spotlight)", True,
          "spotlight document about mentioning"),
    Entry("open safari", "open an app, a file or a folder", False, "launch start show bring up"),
    Entry("install firefox", "look a program up in Homebrew and offer to install it", False, "brew download get"),
    Entry("free up memory", "quit the apps on the always-quit list and offer the next biggest to quit", False,
          "ram close apps slow"),
    Entry("battery helper", "quit apps unused for a day and open the battery dashboard: charge, time left now and "
          "with power saving, apps ranked by power (you can quit them there)", False,
          "battery drain draining power hogs energy usage how long will it last dashboard"),
    Entry("power saving on", "save battery: quit unused apps, Low Power Mode, short display sleep, Power Nap off, "
          "dimmer screen. also: power saving off (puts every setting back), power saving status", False,
          "low power mode battery saver conserve stretch battery last longer"),
    Entry("status", "say which background agents are running", True, "agents daemons everything alive"),
    Entry("fixpock", "restart Pock and the Control Strip", False, "touch bar touchbar frozen stuck"),
    Entry("audio", "say where sound is going and whether it is muted", True, "speakers headphones output volume"),
    Entry("inbox", "list popup messages that are waiting or failed", True, "messages popups pending did anyone message me missed"),
    Entry("clear inbox", "delete popup messages that failed to show", False, "messages popups"),
    Entry("clean clipboard", "tidy what was just copied: dedent code, clean a link", False, "copied paste"),
    Entry("capture text", "select a region of the screen and copy the text in it", False, "ocr screen words image"),
    Entry("$ du -sh ~/Downloads", "run one line of zsh in the terminal, for anything no command here covers. never sudo",
          False, "shell command terminal git python"),
]
# The commands of the parts that are installed separately (lib/components.py), left out when theirs isn't.
_PARTS = {
    "mail": [
        Entry("mail status", "the email layer: Gmail sign-in, last poll, queue", True, "gmail email inbox poller token"),
    ],
    "unsub": [
        Entry("inbox report", "open the inbox dashboard: the weekly report of senders to unsubscribe from, block, "
              "unsure, manual, to decide on", True, "newsletters junk spam unwanted email marketing unsubscribe dashboard"),
        Entry("unsub report section=unsubscribe", "the inbox report's entries as data, one section, paged: unsubscribe "
              "block unsure manual noncompliant auto. page=2", True, "newsletters junk spam senders evidence list"),
        Entry("unsub decide approve=s3,s5 reject=s7", "approve or reject inbox report items by sender id. or choose: "
              "unsubscribe=s3 block=s4 keep=s6, trash=yes", False, "unsubscribe block spam newsletter yes no"),
        Entry("unsub sender s3", "one email sender's history: how many, opened, deleted unread, auth, status, actions. "
              "also by address or domain", True, "who emails newsletter stats"),
        Entry("unsub status", "the inbox agent: mode, last capture and report, decisions waiting, queued actions", True,
              "unsubscribe agent inbox report"),
        Entry("unsub override s3", "delete a sender's saved rule and undo its block", False, "unblock undo mistake rule"),
        Entry("unsub mode permanent", "inbox agent decisions become rules (temporary: they apply once). then: unsub promote",
              False, "rules allowlist denylist testing"),
        Entry("unsub actions status=failed", "the inbox agent's actions log: unsubscribes, blocks and how they went",
              True, "unsubscribed history log"),
    ],
    "brokers": [
        Entry("broker report", "open the data broker dashboard: removal requests to approve, manual tasks (CAPTCHAs, ID, "
              "possible matches), refusals with complaint drafts, what's removed", True,
              "data brokers people search spokeo whitepages opt out remove my information delete personal data privacy"),
        Entry("brokers report group=approval", "the broker board as data, one group, paged: approval auto manual refused "
              "removed maintenance oneoffs. page=2", True, "data brokers opt out requests list"),
        Entry("brokers decide approve=spokeo,whitepages reject=acxiom", "approve or reject data broker requests by id. "
              "approve=all group=approval filter=method:email,category:people-search", False,
              "data broker opt out remove yes no batch"),
        Entry("brokers info spokeo", "one data broker: what it is, what it has on you, the request, history, what was sent",
              True, "data broker people search listing"),
        Entry("brokers status", "the broker agent: states, queue, emails sent today, manual tasks, approval mode", True,
              "data broker removal agent progress"),
        Entry("brokers done task=12 outcome=done", "mark a broker manual task done (outcome: done listed not_listed yes no "
              "completed refused no_record); task=dmachoice ticks a one-time opt-out", False, "captcha id manual finished"),
        Entry("brokers mode standing", "broker approvals: standing (follow-ups, re-submissions happen by themselves) or "
              "per_request", False, "approval automatic ask"),
        Entry("brokers ledger broker=spokeo", "which of your details were sent to which data broker, when and how",
              True, "disclosure sent personal information"),
        Entry("brokers scan spokeo", "search a people-search site for your listing now", False, "search find listing"),
        Entry("brokers mark spokeo outcome=removed", "say what happened with a data broker opt-out: outcome=submitted "
              "(waiting for them), removed, or rejected", False, "opt out done removed refused confirmed page"),
        Entry("brokers override spokeo", "cancel a broker's automatic actions and withdraw its standing approval", False,
              "stop undo cancel"),
    ],
    "dataflow": [
        Entry("dataflow site amazon.com", "where your email goes on a site (the Edge extension's findings): third parties "
              "that listen to, read or send its fields, data brokers, what its privacy policy says, canary tests, "
              "blocking mode", True, "where does my email go trackers scripts site privacy policy sells shares data broker"),
        Entry("dataflow recent since=7d risk=high", "risky fields and sites the Edge extension saw lately (session replay, "
              "identity resolution, brokers listening to fields)", True, "where does my email go recent risky forms"),
        Entry("dataflow mode amazon.com strict", "a site's blocking mode in the Edge extension: observe (nothing blocked), "
              "block (the default) or strict (third-party scripts can't read fields)", False,
              "block trackers allow site breaks extension"),
        Entry("dataflow canary url=https://example.com/signup", "canary test: a hidden browser types a throwaway address "
              "into a page's email field without submitting and watches where it is sent", False,
              "test leak form field typed data sent before submit"),
        Entry("dataflow canaries amazon.com", "a site's canary test results", True, "canary leak test results"),
        Entry("dataflow policy amazon.com type=email", "quoted privacy-policy passages about one kind of data (email name "
              "phone address birthday payment)", True, "privacy policy sells shares quote what does it say"),
        Entry("dataflow status", "the where-does-my-email-go helper: lists, models, queue, whether the Edge extension is "
              "installed", True, "extension edge helper status"),
    ],
}


def entries():
    """The whole list, built now from the code that carries the commands out. Parts not installed are left out."""
    found = list(_FIXED)
    for part, commands in _PARTS.items():
        if components.installed(part):
            found += commands
    found += [Entry(f"info about={about}", what, True) for about, what in _INFO.items() if about in tools.ABOUT]
    found += [Entry(s.name, s.what, s.looks_only) for s in terminal.SHORTCUTS]
    called = {}
    for word, agent in agent_commands.ALIASES.items():
        called.setdefault(agent, []).append(word)
    for agent, (what, previews, _extra) in agent_commands.JOBS.items():
        if not components.installed(agent_commands.component(agent)):
            continue
        words = " ".join(called.get(agent, []))
        found.append(Entry(f"run {agent} now", what + (" (asks first)" if agent in agent_commands.ASK_FIRST else ""),
                           False, words + " cleanup clean tidy job"))
        if previews:
            found.append(Entry(f"preview {agent}", f"show what {agent} would do, without doing it", True, words + " dry run would"))
    for agent, what in DAEMONS.items():
        if not components.installed(agent_commands.component(agent)):
            continue
        found.append(Entry(f"restart {agent}", f"restart the agent that {what}. also: stop {agent}, start {agent}",
                           False, " ".join(called.get(agent, []))))
    for agent, what in {**DAEMONS, **{a: j[0] for a, j in agent_commands.JOBS.items()}, **PROTECTED}.items():
        if not components.installed(agent_commands.component(agent)):
            continue
        note = " (status and logs only)" if agent in PROTECTED else ""
        found.append(Entry(f"status {agent}", f"say whether {agent} is running{note}. also: logs {agent}", True,
                           " ".join(called.get(agent, [])) + " " + what.split(",")[0]))
    return found


def _line(entry):
    return f"{entry.command} — {entry.what}"


_cache = {}


def _vectors(found):
    """One vector per entry; worked out once, as the list only changes when the code does."""
    from atlas import intent

    texts = tuple(f"{e.command}. {e.what}. {e.words}" for e in found)
    if _cache.get("texts") != texts:
        encoder = intent._load()["encoder"]
        _cache.update(texts=texts, vectors=np.stack([encoder.encode(text) for text in texts]))
    return _cache["vectors"]


def _words(text):
    return {w for w in re.findall(r"[a-z0-9]{3,}", text.lower()) if w not in _DULL}


_DULL = set("the and for can you run now what whats how are that this with from have any tool tools command commands "
            "please could would want need use thing".split())


def nearest(request, limit=MAX_SHOWN):
    """The entries closest to a request, best first. Empty when nothing is close, or the encoder is missing."""
    from atlas import intent

    found = entries()
    try:
        scores = _vectors(found) @ intent._load()["encoder"].encode(request)
    except Exception:
        scores = np.zeros(len(found))
    wanted = _words(request)
    ranked = []
    for entry, score in zip(found, scores):
        text = f"{entry.command} {entry.what} {entry.words}".lower()
        ranked.append((float(score) + WORD_BONUS * sum(word in text for word in wanted), entry))
    ranked.sort(key=lambda pair: -pair[0])
    return [entry for score, entry in ranked[:limit] if score >= MIN_SCORE]


def search(request):
    """The answer to `tools WORDS`."""
    close = nearest(request)
    if not close:
        return f"no command is close to {request}. for anything else there is the terminal: $ the command"
    return "closest commands:\n" + "\n".join(_line(e) for e in close)


def everything():
    """The answer to a bare `tools`: every command, names only."""
    names = [e.command for e in entries() if not re.match(r"status \w", e.command)]
    return "commands: " + " · ".join(names) + " · status NAME · logs NAME\nfor what one does: tools WORDS"


def suggestion(request):
    """What to add to a miss: the closest real commands, or nothing."""
    close = nearest(request, MAX_SUGGESTED)
    return "\nclosest commands:\n" + "\n".join(_line(e) for e in close) if close else ""


def check():
    """Every entry's command, as the parser sees it. Returns the ones it does not take as a command."""
    from atlas import assistant_core as core

    return [e.command for e in entries() if core.interpret(e.command).kind in ("miss", "error", "help")]


def interpret(raw, low, core):
    """A Plan for `tools` or `tools WORDS`, or None."""
    match = re.match(r"^tools?(?:\s+for)?(?:\s+(.+))?$", raw.strip(), re.IGNORECASE | re.DOTALL)
    if not match:
        return None
    words = (match.group(1) or "").strip()
    return core.Plan("catalogue", f"look up the command for {words}" if words else "list the commands",
                     lambda: core.Outcome(search(words) if words else everything()), looks_only=True)
