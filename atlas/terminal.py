"""Terminal commands at the prompt, and the ready-made ones ("shortcuts").

    $ <command>        run a shell command as you (never as admin) and show what it printed
    shortcuts          list the ready-made commands below

A `$` command runs in zsh from your home folder with nothing to type into, 60 seconds at
most. sudo and the like are refused, and could not ask for a password anyway. It runs as you,
so it can still change or delete your own files.

Add your own to SHORTCUTS: a name, the wordings that trigger it (a regex matched against the
whole lowercased request), what it does, and either a shell command or a Python function.
"""
import os
import re
import signal
import subprocess
from dataclasses import dataclass
from typing import Callable, Optional

TIMEOUT_SECONDS = 60
MAX_COMMAND_CHARS = 400
MAX_OUTPUT_CHARS = 4000
PATH = "/opt/homebrew/bin:/opt/homebrew/sbin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"

_ADMIN = re.compile(r"(?:^|[\s;&|(`{])(?:sudo|su|doas|login|dscl|visudo)(?=\s|$)|administrator privileges",
                    re.IGNORECASE)


# The two agents that may only be asked about (status, logs), never stopped or edited from here.
_PROTECTED = re.compile(r"site[_-]?(?:monitor|keeper)|nsfw_blocklist", re.IGNORECASE)


def refusal(command):
    """Why a command won't be run, or None if it may."""
    if not command.strip():
        return "there was no command"
    if len(command) > MAX_COMMAND_CHARS:
        return f"that command is too long (over {MAX_COMMAND_CHARS} characters); nothing was run"
    if _ADMIN.search(command):
        return "not allowed: nothing runs as admin"
    if _PROTECTED.search(command):
        return "not allowed: the site monitor and its keeper are not to be touched from here"
    return None


def run_shell(command, timeout=TIMEOUT_SECONDS):
    """Run a command line in zsh as the user. Returns what it printed, plus the exit status if it failed."""
    reason = refusal(command)
    if reason:
        return reason
    env = dict(os.environ, PATH=PATH, SUDO_ASKPASS="/usr/bin/false", TERM="dumb", NO_COLOR="1")
    try:
        process = subprocess.Popen(["/bin/zsh", "-c", command], cwd=os.path.expanduser("~"), env=env,
                                   stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                   text=True, errors="replace", start_new_session=True)
    except OSError as error:
        return f"could not run it: {error}"
    try:
        output, _ = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except OSError:
            pass
        output, _ = process.communicate()
        return (output or "")[-MAX_OUTPUT_CHARS:].rstrip() + f"\n(stopped: still running after {timeout} seconds)"
    output = (output or "").rstrip()
    if len(output) > MAX_OUTPUT_CHARS:
        output = output[:MAX_OUTPUT_CHARS] + "\n… (cut short)"
    if process.returncode != 0:
        return f"{output}\n(exit status {process.returncode})".strip()
    return output or "done. it printed nothing."


# ------------------------------------------------------------ shortcuts

@dataclass
class Shortcut:
    name: str                      # the canonical wording, shown in `shortcuts` and `tools`
    pattern: str                   # regex for the whole lowercased request
    what: str                      # shown when asking, and in `shortcuts`
    shell: Optional[str] = None
    action: Optional[Callable[[], str]] = None
    looks_only: bool = False       # only reports, changes nothing

    def run(self):
        return self.action() if self.action else run_shell(self.shell)


SHORTCUTS = [
    Shortcut("disk space", r"disk(?:\s+space)?|free\s+space|storage|how\s+much\s+(?:disk\s+)?(?:space|storage)(?:\s+(?:is\s+|do\s+i\s+have\s+)?(?:left|free))?\??",
             "show free disk space", shell="df -h ~ | awk 'NR==2 {print $4 \" free of \" $2 \" (\" $5 \" used)\"}'",
             looks_only=True),
    Shortcut("battery", r"battery(?:\s+(?:status|level))?\??", "show the battery level",
             shell="pmset -g batt | tail -1 | cut -f2", looks_only=True),
    Shortcut("ip", r"(?:my\s+)?ip(?:\s+address)?\??|what(?:'s| is)\s+my\s+ip\??", "show this mac's address on the network",
             shell="ipconfig getifaddr en0 || echo 'not on wi-fi'", looks_only=True),
    Shortcut("uptime", r"uptime|how\s+long\s+(?:has\s+it|have\s+you)\s+been\s+(?:up|on)\??", "show how long the mac has been on",
             shell="uptime", looks_only=True),
]

_SHELL = re.compile(r"^(?:\$|sh|shell|terminal|zsh|bash)[:\s]\s*(.+)$", re.IGNORECASE | re.DOTALL)


def listing():
    width = max(len(s.name) for s in SHORTCUTS)
    return "\n".join(f"{s.name.ljust(width)}   {s.what}" for s in SHORTCUTS) + "\n$ <command>   run it in the terminal"


def interpret(raw, low, core):
    """A Plan for a `$` command or a shortcut, or None."""
    Plan, Outcome = core.Plan, core.Outcome
    if low in ("shortcuts", "shortcut", "presets", "terminal", "shell"):
        return Plan("help", "list the ready-made commands", lambda: Outcome(listing()), looks_only=True)
    match = _SHELL.match(raw)
    if match:
        command = match.group(1).strip()
        reason = refusal(command)
        if reason:
            return core._error(reason, final=True)
        return Plan("shell", f"run in the terminal: {command}", lambda: Outcome(run_shell(command)))
    for shortcut in SHORTCUTS:
        if re.fullmatch(shortcut.pattern, low):
            return Plan("shortcut", shortcut.what, lambda s=shortcut: Outcome(s.run()),
                        looks_only=shortcut.looks_only)
    return None
