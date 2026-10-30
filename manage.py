#!/usr/bin/env python3
"""Installs, removes and reports on the components in components.json and their launchd agents.

Layout: agents/ holds one script per launchd agent, atlas/ the assistant (parser, chat, tools), lib/
the helpers they share, and mail/, unsub/, brokers/ and dataflow/ the email components. They import
each other as packages (`from lib import common`), which works because `add` and `install` put this
folder on the virtualenv's path (macutils.pth). ./install.sh makes the virtualenv, then runs `setup`
and `add`.

    ./manage.py components [--json]                   every component, and whether it's installed
    ./manage.py setup <id ...> [--only step,...] [--json]   download its models, run its setup steps
    ./manage.py add <id ...> [--no-agents] [--json]   load its agents and record it as installed
    ./manage.py remove <id ...> [--json]              unload its agents and drop it from the record
    ./manage.py install [agent ...] [--json]          write plists to ~/Library/LaunchAgents and (re)load them
    ./manage.py uninstall [agent ...] [--json]        unload them and delete their plists
    ./manage.py uninstall-all [--keep-state] [--json] every agent, the Edge helper, the record, state and logs
    ./manage.py status [agent ...] [--json]           whether each agent is loaded and how its last run went
    ./manage.py doctor [agent ...] [--json]           check for problems: not loaded, running old code,
                                                      errors, missing models, permissions, sign-ins
    ./manage.py plan [id ...]                         for install.sh: what to sync and set up

Ids may be separated by spaces or commas; `all` means every component. Without agent names, install,
uninstall, status and doctor take the agents of the installed components. MACUTILS_NO_LAUNCHD=1 writes
and removes plists without calling launchctl (tests and sandboxes).
"""
import json
import os
import plistlib
import shutil
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.abspath(__file__))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)   # lib/ is here; install.sh runs this before the virtualenv has the .pth

from lib.components import RECORD  # noqa: E402
from lib.identity import INSTALL_DIR, LOG_DIR, STATE_DIR, keychain_service, label  # noqa: E402

PYTHON = os.path.join(ROOT, ".venv", "bin", "python")
LAUNCH_AGENTS = os.path.expanduser("~/Library/LaunchAgents")
DOMAIN = f"gui/{os.getuid()}"
STAND_DOWN = os.path.join(STATE_DIR, "buddy-stand-down")   # while it exists, lib/buddy.py revives nothing
PAIR = ("sitemonitor", "sitekeeper")                         # the two that revive each other (lib/buddy.py)
SECRETS = {"mail": "gmail", "brokers": "brokers"}            # component -> its Keychain service (keychain_service)

# name -> script plus exactly one trigger:
#   daemon   runs all the time and is restarted if it exits
#   watch    runs when anything in these folders changes
#   calendar runs on a schedule (launchd StartCalendarInterval; Weekday 0 is Sunday)
#   interval runs every N seconds
AGENTS = {
    "audioguard": {"script": "audio_guard.py", "daemon": True},
    "appblocker": {"script": "app_blocker.py", "daemon": True},
    "clipboard": {"script": "clipboard_tools.py", "daemon": True},
    # These two reload each other if either is unloaded (lib/buddy.py); uninstalling them through
    # this script stands them down first, so it sticks.
    "sitemonitor": {"script": "site_monitor.py", "daemon": True, "throttle": 10},
    "sitekeeper": {"script": "site_keeper.py", "daemon": True, "throttle": 10},
    "autounpack": {"script": "auto_unpack.py", "watch": ["~/Downloads"]},
    "screenshotcopy": {"script": "screenshot_copy.py", "watch": ["~/Desktop"]},
    "screentext": {"script": "screen_text.py", "daemon": True},
    "cmdnotify": {"script": "command_notifier.py", "daemon": True},
    "messagepopup": {"script": "message_popup.py", "daemon": True, "throttle": 10},
    "assistant": {"script": "assistant.py", "daemon": True, "throttle": 10},
    "stalearchiver": {"script": "stale_archiver.py", "calendar": {"Hour": 0, "Minute": 0}},
    "devclean": {"script": "dev_cleaner.py", "calendar": {"Weekday": 0, "Hour": 2, "Minute": 0}},
    "logtrimmer": {"script": "log_trimmer.py", "calendar": {"Weekday": 6, "Hour": 1, "Minute": 0}},
    "packagemanagergc": {"script": "package_gc.py", "calendar": {"Day": 1, "Hour": 4, "Minute": 0}},
    "pockkeeper": {"script": "pock_keeper.py", "interval": 1800},
    "mailpoller": {"script": "mail_poller.py", "interval": 300},
    "unsubscriber": {"script": "unsubscriber.py", "calendar": {"Weekday": 0, "Hour": 9, "Minute": 0}},
    "brokeragent": {"script": "broker_agent.py", "interval": 600},
    "dataflow": {"script": "dataflow_agent.py", "interval": 3600},
}


def no_launchd():
    return os.environ.get("MACUTILS_NO_LAUNCHD", "") not in ("", "0")


def plist_path(name):
    return os.path.join(LAUNCH_AGENTS, label(name) + ".plist")


def build_plist(name, spec):
    # Anything a script prints, including an uncaught traceback, lands here.
    output = os.path.join(LOG_DIR, f"{name}.stderr.log")
    plist = {
        "Label": label(name),
        "ProgramArguments": [PYTHON, os.path.join(ROOT, "agents", spec["script"])],
        "StandardOutPath": output,
        "StandardErrorPath": output,
    }
    if spec.get("daemon"):
        # ThrottleInterval keeps a crashing daemon from respawning (and logging) every 10 seconds.
        plist.update(RunAtLoad=True, KeepAlive=True, ThrottleInterval=spec.get("throttle", 60))
    if "watch" in spec:
        plist["WatchPaths"] = [os.path.expanduser(p) for p in spec["watch"]]
    if "calendar" in spec:
        plist["StartCalendarInterval"] = spec["calendar"]
    if "interval" in spec:
        plist.update(StartInterval=spec["interval"], RunAtLoad=True)
    return plist


def launchctl(*args):
    if no_launchd():
        # Nothing reaches launchd: `print` answers "not loaded", everything else "worked".
        return subprocess.CompletedProcess(["launchctl", *args], 1 if args[:1] == ("print",) else 0, "",
                                           "launchctl skipped (MACUTILS_NO_LAUNCHD=1)")
    return subprocess.run(["launchctl", *args], capture_output=True, text=True, check=False)


def is_loaded(name):
    return launchctl("print", f"{DOMAIN}/{label(name)}").returncode == 0


def unload(name):
    launchctl("bootout", f"{DOMAIN}/{label(name)}")
    # bootout returns before the job is fully gone; loading again too early fails.
    for _ in range(50):
        if not is_loaded(name):
            return
        time.sleep(0.1)


def path_file():
    """The .pth file that lets the virtualenv's Python import agents/, atlas/ and lib/ from anywhere."""
    import glob
    site = glob.glob(os.path.join(ROOT, ".venv", "lib", "python*", "site-packages"))
    return os.path.join(site[0], "macutils.pth") if site else None


def ensure_path():
    target = path_file()
    if target is None:
        sys.exit("no virtualenv at .venv: run ./install.sh first (see README)")
    try:
        with open(target, encoding="utf-8") as f:
            if f.read().strip() == ROOT:
                return
    except OSError:
        pass
    with open(target, "w", encoding="utf-8") as f:
        f.write(ROOT + "\n")


# ---------------------------------------------------------------- components and the record

def version():
    try:
        with open(os.path.join(ROOT, "VERSION"), encoding="utf-8") as f:
            return f.read().strip() or "0.0.0"
    except OSError:
        return "0.0.0"


_catalogue = {}


def catalogue():
    """components.json: groups and components, read once."""
    if not _catalogue:
        with open(os.path.join(ROOT, "components.json"), encoding="utf-8") as f:
            _catalogue.update(json.load(f))
    return _catalogue


def components():
    """id -> component, in components.json order."""
    return {c["id"]: c for c in catalogue()["components"]}


def component_of(agent):
    return next((c["id"] for c in catalogue()["components"] if agent in c["agents"]), None)


def read_record():
    """The install record, or None when there is none (a checkout set up by hand)."""
    try:
        with open(RECORD, encoding="utf-8") as f:
            record = json.load(f)
        return record if isinstance(record.get("installed"), dict) else None
    except (OSError, ValueError, AttributeError):
        return None


def installed_ids():
    """The installed component ids in components.json order. Without a record, all of them."""
    record = read_record()
    return [i for i in components() if record is None or i in record["installed"]]


def write_record(installed):
    order = list(components())
    installed = dict(sorted(installed.items(), key=lambda item: order.index(item[0]) if item[0] in order else len(order)))
    os.makedirs(STATE_DIR, exist_ok=True)
    temporary = RECORD + ".tmp"
    with open(temporary, "w", encoding="utf-8") as f:
        json.dump({"version": version(), "installed": installed}, f, indent=2)
        f.write("\n")
    os.replace(temporary, RECORD)


def now():
    from datetime import datetime
    return datetime.now().astimezone().isoformat(timespec="seconds")


def parse_ids(words):
    ids = [i for word in words for i in word.split(",") if i]
    return list(components()) if ids == ["all"] else ids


def closure(ids):
    """The ids plus every component they require, each after what it requires."""
    table = components()
    unknown = [i for i in ids if i not in table]
    if unknown:
        sys.exit(f"unknown component: {', '.join(unknown)} (./manage.py components lists them)")
    order, seen = [], set()

    def visit(i):
        if i in seen:
            return
        seen.add(i)
        for need in table[i].get("requires", []):
            visit(need)
        order.append(i)

    for i in ids:
        visit(i)
    return order


def agents_of(ids):
    table = components()
    return [a for i in ids for a in table[i]["agents"]]


# ---------------------------------------------------------------- output

class Events:
    """Progress as plain lines for people, or (--json) one JSON object per line for the setup app:
    {"event": "step", "id", "title", "status": start|done|skipped|failed, "detail"} (a skipped step
    with "needs_you": true is the user's to do), {"event": "progress", "id", "done", "total"} and
    {"event": "finished", "ok", "message"}."""

    def __init__(self, as_json=False):
        self.json = as_json
        self.tty = sys.stdout.isatty()

    def emit(self, **event):
        print(json.dumps(event, ensure_ascii=False), flush=True)

    def step(self, id, title, status, detail="", **extra):
        if self.json:
            self.emit(event="step", id=id, title=title, status=status, detail=detail, **extra)
        elif status == "start":
            print(f"==> {title}", flush=True)
        elif detail or status != "done":
            prefix = "needs you: " if extra.get("needs_you") else {"done": "", "skipped": "skipped: ",
                                                                    "failed": "FAILED: "}.get(status, "")
            print(f"    {prefix}{detail}".rstrip(), file=sys.stderr if status == "failed" else sys.stdout, flush=True)

    def progress(self, id, done, total, **extra):
        if self.json:
            self.emit(event="progress", id=id, done=done, total=total, **extra)
        elif self.tty and total:
            print(f"\r    {100 * done // total:3d}%  {extra.get('detail', '')}"[:100].ljust(100), end="", flush=True)
            if done >= total:
                print(flush=True)

    def agent(self, name, verb, ok, text):
        """One line per agent: `clipboard: loaded` (the assistant shows these as they are)."""
        if self.json:
            self.emit(event="step", id=f"agent:{name}", title=f"{verb} {name}", status="done" if ok else "failed",
                      detail=text)
        else:
            print(f"{name}: {text}", flush=True)

    def note(self, text):
        if self.json:
            self.emit(event="note", text=text)
        else:
            print(text, flush=True)

    def finished(self, ok, message):
        if self.json:
            self.emit(event="finished", ok=ok, message=message)
        elif message:
            print(message, flush=True)


# ---------------------------------------------------------------- agents

def stand_down(on):
    """Create (on) or delete the flag that keeps the sitemonitor pair from reviving each other."""
    if on:
        os.makedirs(STATE_DIR, exist_ok=True)
        open(STAND_DOWN, "a").close()
    elif os.path.exists(STAND_DOWN):
        os.remove(STAND_DOWN)


def install(names, events=None):
    """Write each agent's plist and (re)load it. Returns the names that failed to load."""
    events = events or Events()
    os.makedirs(LOG_DIR, exist_ok=True)
    os.makedirs(LAUNCH_AGENTS, exist_ok=True)
    ensure_path()
    pair = any(n in PAIR for n in names)
    if pair:
        stand_down(True)      # so neither revives the other while it's being replaced
    for name in names:
        unload(name)
    if pair:
        stand_down(False)     # before loading, or they would come up standing down
    failed = []
    for name in names:
        with open(plist_path(name), "wb") as f:
            plistlib.dump(build_plist(name, AGENTS[name]), f)
        if no_launchd():
            events.agent(name, "Load", True, "plist written (launchd skipped)")
            continue
        result = launchctl("bootstrap", DOMAIN, plist_path(name))
        # Its partner may have reloaded it from the same plist in the meantime.
        ok = result.returncode == 0 or (name in PAIR and is_loaded(name))
        events.agent(name, "Load", ok, "loaded" if ok else "FAILED: " + result.stderr.strip())
        if not ok:
            failed.append(name)
    return failed


def uninstall(names, events=None):
    events = events or Events()
    if any(n in PAIR for n in names):
        stand_down(True)      # first: otherwise the partner reloads it
    for name in names:
        unload(name)
        if os.path.exists(plist_path(name)):
            os.remove(plist_path(name))
        events.agent(name, "Unload", True, "removed")


def native_host_files():
    """The Edge helper's registration: its manifest in each Edge channel, and its launcher."""
    from dataflow import install as host
    found = [os.path.join(host.SUPPORT, b, "NativeMessagingHosts", f"{host.HOST_NAME}.json") for b in host.BROWSERS]
    return [p for p in found + [host.LAUNCHER] if os.path.exists(p)]


def unregister_host(events):
    files = native_host_files()
    for path in files:
        os.remove(path)
    events.step("native-host", "Unregister the Edge helper", "done" if files else "skipped",
                f"removed its registration ({len(files)} file(s))" if files else "it wasn't registered")


def secrets_hint(ids):
    """How to delete the Keychain items of these components; this script never touches the Keychain."""
    services = [keychain_service(SECRETS[i]) for i in ids if i in SECRETS]
    return [f"while security delete-generic-password -s {s} >/dev/null 2>&1; do :; done" for s in services]


# ---------------------------------------------------------------- commands

def list_components(as_json):
    record = read_record()
    table = components()
    rows = []
    for c in table.values():
        entry = (record or {}).get("installed", {}).get(c["id"], {})
        rows.append({**c, "installed": record is None or c["id"] in record["installed"],
                     "version": entry.get("version"), "installed_at": entry.get("installed_at")})
    if as_json:
        print(json.dumps({"version": version(), "recorded": record is not None, "root": ROOT,
                          "groups": catalogue()["groups"], "components": rows}, ensure_ascii=False, indent=2))
        return 0
    for group in catalogue()["groups"]:
        print(group["title"])
        for row in rows:
            if row["group"] == group["id"]:
                print(f"  [{'x' if row['installed'] else ' '}] {row['id']:17} {row['title']}"
                      + (f"  ({row['version']})" if row["version"] else ""))
    if record is None:
        print("\nNo install record: a checkout run by hand counts every component as installed.")
    return 0


def plan(ids):
    """For install.sh, as key=value lines: the requested components with what they require, which of
    them are new, the union with what's installed (its extras are what uv syncs), and steps to re-run
    for installed components when their code is replaced."""
    table = components()
    record = read_record()
    recorded = [i for i in table if record and i in record["installed"]]
    requested = closure(ids if ids else recorded)
    union = [i for i in table if i in recorded or i in requested]
    extras = []
    for i in union:
        extras += [e for e in table[i].get("extras", []) if e not in extras]
    refresh = [f"{i}:native-host" for i in recorded if i not in requested and "native-host" in table[i].get("setup", [])]
    lines = {"requested": requested, "new": [i for i in requested if i not in recorded],
             "reinstall": [i for i in requested if i in recorded], "sync": union, "extras": extras,
             "refresh": refresh}
    for key, values in lines.items():
        print(f"{key}={' '.join(values)}")
    return 0


def setup(ids, events, only=None):
    from lib import setup_steps
    table = components()
    chosen = [table[i] for i in closure(ids)]
    runner = setup_steps.Setup(ROOT, PYTHON, events, chosen)
    if not runner.steps(only):
        events.finished(True, "")
        return 0
    ok = runner.run(only)
    events.finished(ok, "setup done" if ok else "setup failed")
    return 0 if ok else 1


def add(ids, events, agents=True):
    ids = closure(ids)
    record = read_record()
    installed = dict(record["installed"]) if record else {}
    ensure_path()
    failed = install(agents_of(ids), events) if agents else []
    stamp = now()
    for i in ids:
        installed[i] = {"version": version(), "installed_at": stamp}
    write_record(installed)
    events.step("record", "Record the installed components", "done", "recorded " + ", ".join(ids))
    if "dataflow" in ids:
        events.note("Edge, once: edge://extensions -> Developer mode -> Load unpacked -> "
                    + os.path.join(ROOT, "dataflow", "extension"))
    ok = not failed
    events.finished(ok, f"added {', '.join(ids)}" if ok else f"could not load {', '.join(failed)}")
    return 0 if ok else 1


def remove(ids, events):
    table = components()
    closure(ids)              # exits on an unknown id
    current = installed_ids()
    blocked = {i: [c for c in current if c not in ids and i in table[c].get("requires", [])] for i in ids}
    blocked = {i: needers for i, needers in blocked.items() if needers}
    if blocked:
        needers = sorted({c for cs in blocked.values() for c in cs})
        sys.exit("; ".join(f"{', '.join(cs)} need{'s' if len(cs) == 1 else ''} {i}" for i, cs in blocked.items())
                 + f": remove {'it' if len(needers) == 1 else 'them'} too (./manage.py remove {' '.join(needers + ids)})")
    uninstall(agents_of(ids), events)
    if "dataflow" in ids:
        unregister_host(events)
    record = read_record()
    installed = record["installed"] if record else {i: {"version": version(), "installed_at": now()} for i in current}
    write_record({i: v for i, v in installed.items() if i not in ids})
    events.step("record", "Record the installed components", "done", "removed " + ", ".join(ids))
    if "dataflow" in ids:
        events.note("Remove the extension in Edge too: edge://extensions")
    for line in secrets_hint(ids):
        events.note(f"Its Keychain item stays; to delete it: {line}")
    if any(table[i].get("extras") for i in ids):
        events.note("Its Python packages leave the virtualenv the next time ./install.sh runs.")
    if any(table[i].get("models") for i in ids):
        events.note("Its models stay in models/, where other components may use them.")
    events.finished(True, f"removed {', '.join(ids)}")
    return 0


def uninstall_all(events, keep_state=False):
    stand_down(True)
    present = [n for n in AGENTS if os.path.exists(plist_path(n)) or is_loaded(n)]
    uninstall(present, events)
    try:
        unregister_host(events)
    except Exception as error:   # the dataflow code missing from a damaged install
        events.step("native-host", "Unregister the Edge helper", "failed", f"{type(error).__name__}: {error}")
    if os.path.exists(RECORD):
        os.remove(RECORD)
    events.step("record", "Delete the install record", "done", f"deleted {RECORD}")
    if not keep_state:
        for kind, folder in (("state", STATE_DIR), ("logs", LOG_DIR)):
            if os.path.isdir(folder):
                shutil.rmtree(folder)
            events.step(kind, f"Delete the {kind}", "done", f"deleted {folder}")
    for line in secrets_hint(list(SECRETS)):
        events.note(f"Not removed: Keychain items. To delete them: {line}")
    events.note("Not removed: Playwright's Chromium, in ~/Library/Caches/ms-playwright")
    events.note("Not removed: the Edge extension, if you loaded it (edge://extensions)")
    if ROOT != INSTALL_DIR:
        events.note(f"Not removed: this checkout and its virtualenv and models, in {ROOT}")
    events.finished(True, f"removed {len(present)} agent(s)" + ("" if keep_state else ", the state and the logs"))
    return 0


def status(names, as_json=False):
    rows = []
    for name in names:
        result = launchctl("print", f"{DOMAIN}/{label(name)}")
        row = {"agent": name, "component": component_of(name), "loaded": result.returncode == 0,
               "plist": os.path.exists(plist_path(name))}
        if row["loaded"]:
            fields = {}
            for line in result.stdout.splitlines():
                key, _, value = line.strip().partition(" = ")
                if key in ("state", "runs", "last exit code", "pid") and key not in fields:
                    fields[key] = value
            row.update(state=fields.get("state"), runs=fields.get("runs"), last_exit=fields.get("last exit code"),
                       pid=fields.get("pid"))
        stderr_log = os.path.join(LOG_DIR, f"{name}.stderr.log")
        row["stderr_bytes"] = os.path.getsize(stderr_log) if os.path.exists(stderr_log) else 0
        rows.append(row)
    if as_json:
        print(json.dumps({"launchd": not no_launchd(), "agents": rows}, indent=2))
        return 0
    for row in rows:
        name = row["agent"]
        if not row["loaded"]:
            print(f"{name:18} " + ("plist written, launchd skipped" if no_launchd() and row["plist"] else "not loaded"))
            continue
        print(f"{name:18} {row['state'] or '?':16} runs={row['runs'] or '?':5} "
              f"last exit={row['last_exit'] or '?':16} stderr={row['stderr_bytes']}B")
    return 0


def _agent_files(script):
    """The agent's script plus the project modules it imports (one level), for staleness checks."""
    import re
    files = [os.path.join(ROOT, "agents", script)]
    with open(files[0], encoding="utf-8") as f:
        source = f.read()
    imports = re.findall(r"^\s*from (agents|atlas|lib|mail|unsub|brokers|dataflow)(?:\.(\w+))? import ([\w, ]+)",
                         source, re.MULTILINE)
    for package, module, imported in imports:
        # `from lib.common import run` names the module; `from lib import common, ocr` lists them.
        names = [module] if module else [n.split(" as ")[0].strip() for n in imported.split(",")]
        for name in names:
            candidate = os.path.join(ROOT, package, name + ".py")
            if os.path.exists(candidate) and candidate not in files:
                files.append(candidate)
    return files


def _process_start(pid):
    out = subprocess.run(["ps", "-o", "lstart=", "-p", str(pid)], capture_output=True, text=True).stdout.strip()
    try:
        return time.mktime(time.strptime(" ".join(out.split()), "%a %b %d %H:%M:%S %Y"))
    except ValueError:
        return None


def _venv_json(*args):
    """Run `python -m <args>` in the virtualenv (it has packages this Python may lack) and parse its JSON."""
    answer = subprocess.run([PYTHON, "-m", *args], capture_output=True, text=True, timeout=60, cwd=ROOT)
    return json.loads(answer.stdout)


def _missing_models(ids):
    """Model files of these components that are absent or the wrong size (sizes only: hashing is setup's job)."""
    try:
        with open(os.path.join(ROOT, "models.lock.json"), encoding="utf-8") as f:
            lock = json.load(f)["models"]
    except (OSError, ValueError, KeyError):
        return ["models.lock.json"]
    table = components()
    names = []
    for i in ids:
        names += [m for m in table[i].get("models", []) if m not in names]
    missing = []
    for name in names:
        for entry in lock.get(name, {}).get("files", []):
            path = os.path.join(ROOT, "models", name, entry["path"])
            if not os.path.exists(path) or os.path.getsize(path) != entry["size"]:
                missing.append(name)
                break
    return missing


def doctor(names, as_json=False):
    checks = []

    def report(level, text, component=None):
        checks.append({"level": level, "text": text, "component": component})
        if not as_json:
            tag = {"ok": "ok  ", "warn": "WARN", "fail": "FAIL", "info": "    "}[level]
            print(f"{tag}  {text}")

    installed = set(installed_ids())
    table = components()
    for name in names:
        spec = AGENTS[name]
        owner = component_of(name)
        if no_launchd():
            report("ok" if os.path.exists(plist_path(name)) else "fail",
                   f"{name}: plist {'written' if os.path.exists(plist_path(name)) else 'missing'} (launchd skipped)",
                   owner)
            continue
        result = launchctl("print", f"{DOMAIN}/{label(name)}")
        if result.returncode != 0:
            report("fail", f"{name}: not loaded (./manage.py install {name})", owner)
            continue
        fields = {}
        for line in result.stdout.splitlines():
            key, _, value = line.strip().partition(" = ")
            if key in ("state", "pid", "last exit code") and key not in fields:
                fields[key] = value
        if spec.get("daemon") and fields.get("state") != "running":
            report("fail", f"{name}: should be running but is {fields.get('state', '?')}", owner)
            continue
        if fields.get("last exit code", "0") not in ("0", "(never exited)"):
            report("warn", f"{name}: last run exited with {fields['last exit code']}", owner)
        stderr_log = os.path.join(LOG_DIR, f"{name}.stderr.log")
        if os.path.exists(stderr_log) and os.path.getsize(stderr_log) > 0:
            report("warn", f"{name}: has output on stderr ({os.path.getsize(stderr_log)} bytes, see {stderr_log})", owner)
        if spec.get("daemon") and fields.get("pid", "").isdigit():
            started = _process_start(int(fields["pid"]))
            newest = max(os.path.getmtime(f) for f in _agent_files(spec["script"]))
            if started and newest > started + 1:
                report("warn", f"{name}: running code older than its files (./manage.py install {name})", owner)
                continue
        report("ok", name, owner)

    if not as_json:
        print()
    record = read_record()
    if record is None:
        report("info", "no install record: every component counts as installed (./install.sh makes one)")
    else:
        stale = [i for i, v in record["installed"].items() if v.get("version") != version()]
        if stale:
            report("warn", f"{', '.join(stale)} installed from another version than this code ({version()}): "
                           "run the installer again")
    if not os.path.exists(PYTHON):
        report("fail", f"no virtualenv at {os.path.join(ROOT, '.venv')} (./install.sh)")
    target = path_file()
    linked = False
    try:
        with open(target, encoding="utf-8") as f:
            linked = f.read().strip() == ROOT
    except (OSError, TypeError):
        pass
    report("ok" if linked else "warn", "project is on the virtualenv's path" if linked
           else "project is not on the virtualenv's path, so nothing can import (./manage.py install)")
    for name in _missing_models(sorted(installed)):
        users = [i for i in table if i in installed and name in table[i].get("models", [])]
        report("warn", f"model {name} is missing or incomplete (./manage.py setup {' '.join(users)})", users[0])
    if "screentext" in installed:
        prebuilt = os.path.join(ROOT, "bin", "ocr-helper")
        built = os.path.join(STATE_DIR, "ocr-helper")
        ready = os.access(prebuilt, os.X_OK) or os.access(built, os.X_OK)
        report("ok" if ready else "warn", "text recognition helper " + ("present" if ready else
               "missing (./manage.py setup screentext)"), "screentext")

    if "mail" in installed:
        try:
            from mail import auth as mail_auth, store as mail_store
            gmail = mail_auth.health()
            if not gmail["signed_in"]:
                report("warn", "Gmail: " + (gmail["error"] or "not signed in (.venv/bin/python -m mail setup CLIENT_FILE)"),
                       "mail")
            else:
                report("warn" if gmail["missing_scopes"] else "ok", f"Gmail signed in as {gmail['address']}"
                       + (f", but NOT granted: {', '.join(gmail['missing_scopes'])} (run setup again)"
                          if gmail["missing_scopes"] else ""), "mail")
                polled = mail_store.get("last_poll_at")
                if not polled or time.time() - polled > 1800:
                    report("warn", "Gmail: no poll in the last 30 minutes (./manage.py install mailpoller; logs mailpoller)",
                           "mail")
                if "unsub" in installed:
                    from unsub import ops as unsub_ops
                    inbox = unsub_ops.status()
                    if inbox["capture_error"]:
                        report("warn", f"inbox agent: capture failing: {inbox['capture_error'][:120]}", "unsub")
                    if inbox["capture_queue"] > 1000:
                        report("warn", f"inbox agent: {inbox['capture_queue']} polled messages not yet captured", "unsub")
                    analysed = unsub_ops.store.get("last_analysis_at")
                    if analysed and time.time() - analysed > 8 * 86400:
                        report("warn", "inbox agent: no analysis in 8 days (./manage.py install unsubscriber; "
                                       "logs unsubscriber)", "unsub")
                    if inbox["actions"].get("failed_7d"):
                        report("warn", f"inbox agent: {inbox['actions']['failed_7d']} actions failed this week "
                                       "(unsub actions status=failed)", "unsub")
                    report("ok", f"inbox agent: {inbox['mode']} mode, {inbox['pending_decisions']} decisions waiting, "
                                 f"last report {inbox['last_analysis'] or 'not yet'}", "unsub")
        except Exception as error:
            report("warn", f"Gmail: {type(error).__name__}: {error}", "mail")

    if "brokers" in installed:
        import glob
        if not glob.glob(os.path.join(ROOT, "brokers", "registry", "*.yaml")):
            report("warn", "broker agent: the broker list is empty (./manage.py setup brokers)", "brokers")
        try:
            brokers = _venv_json("brokers", "status")
            if not brokers["ready"]:
                report("warn", "broker agent: no details yet (say \"broker report\", then Your details)", "brokers")
            else:
                if brokers["last_pass_ago"] is None or not brokers["last_pass_ago"].endswith("min"):
                    report("warn", "broker agent: no pass in the last 2 hours (./manage.py install brokeragent; "
                                   "logs brokeragent)", "brokers")
                report("ok", f"broker agent: {brokers['mode']} approvals, {brokers['manual_tasks']} manual tasks, "
                             f"emails today {brokers['emails_today']}", "brokers")
            with open(os.path.join(ROOT, "brokers", "config.json"), encoding="utf-8") as f:
                months = json.load(f)["jurisdiction"].get("recheck_months", 6)
            checked = brokers.get("jurisdiction_checked")
            if not checked:
                report("info", "broker agent: every request is voluntary. When you've looked up whether your state's "
                               "law gives you a right to deletion, put the date in brokers/config.json "
                               "(jurisdiction.checked) and doctor reminds you to look again", "brokers")
            elif time.time() - time.mktime(time.strptime(checked, "%Y-%m-%d")) > months * 30.5 * 86400:
                report("warn", f"broker agent: you last checked the legal context on {checked}: look again whether "
                               "your state's law now gives you a right to deletion (brokers/config.json jurisdiction)",
                       "brokers")
        except Exception as error:
            report("warn", f"broker agent: {type(error).__name__}: {error}", "brokers")

    if "dataflow" in installed:
        try:
            flow = _venv_json("dataflow", "status")
            ext = flow["extension"]
            if not ext["registered_with"] or not ext["launcher"]:
                report("warn", "email extension: the helper isn't registered with Edge "
                               "(./manage.py setup dataflow --only native-host)", "dataflow")
            elif not ext["loaded_in_edge"]:
                report("warn", "email extension: not loaded in Edge (edge://extensions → Developer mode → Load unpacked "
                               f"→ {os.path.join(ROOT, 'dataflow', 'extension')})", "dataflow")
            # flow["optional_models"] (PrivBERT) isn't distributed, so it never counts as missing.
            missing = [name for name, ok in flow["models"].items() if not ok]
            if missing:
                report("warn", f"email extension: models missing: {', '.join(missing)} (./manage.py setup dataflow)",
                       "dataflow")
            if flow["lists_age_days"] is None or flow["lists_age_days"] > 9:
                report("warn", "email extension: tracker lists over 9 days old (./manage.py install dataflow; "
                               "logs dataflow)", "dataflow")
            if ext["loaded_in_edge"] and not missing:
                report("ok", f"email extension: {flow['pages_7d']} pages this week, {flow['high_risk_pages_7d']} "
                             f"high risk, {flow['blocked_7d']} blocked", "dataflow")
        except Exception as error:
            report("warn", f"email extension: {type(error).__name__}: {error}", "dataflow")

    if "sitemonitor" in installed:
        site_log = os.path.join(LOG_DIR, "site_monitor.log")
        trusted = None
        if os.path.exists(site_log):
            with open(site_log, encoding="utf-8", errors="replace") as f:
                for line in f:
                    if "accessibility permission:" in line:
                        trusted = "NOT granted" not in line
        if trusted is None:
            report("info", "accessibility: not reported yet (sitemonitor has not logged it)", "sitemonitor")
        else:
            report("ok" if trusted else "warn",
                   "Accessibility for Python " + ("granted" if trusted else
                   "NOT granted: Firefox-family browsers can't be read (System Settings > Privacy & Security)"),
                   "sitemonitor")
    if "messagepopup" in installed:
        inbox = os.path.join(STATE_DIR, "inbox")
        pending = len([n for n in os.listdir(inbox) if n.endswith(".json")]) if os.path.isdir(inbox) else 0
        if pending:
            report("warn", f"{pending} message(s) waiting in the popup inbox", "messagepopup")

    problems = sum(c["level"] in ("warn", "fail") for c in checks)
    if as_json:
        print(json.dumps({"ok": not problems, "problems": problems, "checks": checks}, ensure_ascii=False, indent=2))
    else:
        print(f"\n{'all clear' if not problems else f'{problems} thing(s) need attention'}")
    return 1 if problems else 0


def main(argv):
    args = argv[1:]
    command = args[0] if args else "status"
    flags, words, only = set(), [], None
    rest = iter(args[1:])
    for arg in rest:
        if arg == "--only":
            only = next(rest, "")
        elif arg.startswith("--only="):
            only = arg.split("=", 1)[1]
        elif arg.startswith("--"):
            flags.add(arg)
        else:
            words.append(arg)
    allowed = {"components": {"--json"}, "plan": set(), "setup": {"--json"}, "add": {"--json", "--no-agents"},
               "remove": {"--json"}, "install": {"--json"}, "uninstall": {"--json"},
               "uninstall-all": {"--json", "--keep-state"}, "status": {"--json"}, "doctor": {"--json"}}
    if command in ("help", "-h", "--help"):
        print(__doc__)
        return 0
    if command not in allowed or flags - allowed[command] or (only is not None and command != "setup"):
        sys.exit(__doc__ + f"\nUnknown command or option: {' '.join(args)}")
    as_json = "--json" in flags
    events = Events(as_json)

    if command == "components":
        return list_components(as_json)
    if command == "plan":
        return plan(parse_ids(words))
    if command in ("setup", "add", "remove"):
        ids = parse_ids(words)
        if not ids:
            sys.exit(f"which components? ./manage.py {command} <id ...> (./manage.py components lists them)")
        if command == "setup":
            return setup(ids, events, [s for s in only.split(",") if s] if only else None)
        if command == "add":
            return add(ids, events, agents="--no-agents" not in flags)
        return remove(ids, events)
    if command == "uninstall-all":
        return uninstall_all(events, keep_state="--keep-state" in flags)

    names = words or agents_of(installed_ids())
    unknown = [n for n in names if n not in AGENTS]
    if unknown:
        sys.exit(__doc__ + f"\nUnknown agent: {', '.join(unknown)}\n")
    if command == "install":
        record = read_record()
        missing = sorted({component_of(n) or n for n in names if record and component_of(n) not in record["installed"]})
        if missing:
            sys.exit(f"{', '.join(missing)} isn't installed: add it with the installer, or ./install.sh --components "
                     f"{','.join(missing)}")
        return 1 if install(names, events) else 0
    if command == "uninstall":
        uninstall(names, events)
        return 0
    if command == "status":
        return status(names, as_json)
    return doctor(names, as_json)


if __name__ == "__main__":
    sys.exit(main(sys.argv))
