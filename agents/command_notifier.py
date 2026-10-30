"""Announces when a long-running terminal command finishes.

Watches every interactive shell (any terminal app, any shell) and, when a command that ran
for more than 15 seconds exits, shows an alert that stays until dismissed.
Nothing needs adding to ~/.zshrc or any other shell's startup file.
"""
import os
import subprocess
import time
from collections import namedtuple

from lib.common import run, run_main

MIN_SECONDS = 15
POLL_SECONDS = 2

SHELLS = {"zsh", "bash", "fish", "sh", "dash", "tcsh", "ksh", "nu"}
# Programs you sit inside rather than wait for; their exit isn't news.
IGNORED = {"vim", "nvim", "vi", "nano", "emacs", "less", "more", "man", "top", "htop", "btop",
           "ssh", "mosh", "tmux", "screen", "claude", "python", "python3", "ipython", "node",
           "irb", "R", "sqlite3", "psql", "watch", "tail"}

ALERT_SCRIPT = """on run argv
    display alert (item 1 of argv) message (item 2 of argv) as informational buttons {"OK"} default button "OK"
end run"""

Process = namedtuple("Process", "pid ppid pgid tty elapsed name args")
Job = namedtuple("Job", "name start shell_pid")


def parse_elapsed(text):
    """ps prints elapsed time as [[dd-]hh:]mm:ss."""
    days, _, clock = text.rpartition("-")
    seconds = 0
    for part in clock.split(":"):
        seconds = seconds * 60 + int(part)
    return seconds + (int(days) * 86400 if days else 0)


def snapshot():
    result = run(["ps", "-axo", "pid=,ppid=,pgid=,tty=,etime=,command="], timeout=10)
    processes = {}
    if result is None or result.returncode != 0:
        return processes
    for line in result.stdout.splitlines():
        parts = line.split(None, 5)
        if len(parts) < 6:
            continue
        try:
            pid, ppid, pgid = int(parts[0]), int(parts[1]), int(parts[2])
            elapsed = parse_elapsed(parts[4])
        except ValueError:
            continue
        argv = parts[5].split()
        name = os.path.basename(argv[0]).lstrip("-")
        processes[pid] = Process(pid, ppid, pgid, parts[3], elapsed, name, argv[1:])
    return processes


def is_interactive_shell(process):
    """A shell with a terminal and no script argument (so `bash build.sh` still counts as a command)."""
    return (process.name in SHELLS and process.tty != "??"
            and all(arg.startswith("-") for arg in process.args))


def is_bare_interpreter(process):
    """`python3` on its own is a REPL to sit in; `python3 train.py` is a command to wait for."""
    return process.name in IGNORED and (
        process.name not in {"python", "python3", "node"} or not process.args)


def running_commands(processes):
    """Map process-group id -> Job for every command started from an interactive shell."""
    commands = {}
    for process in processes.values():
        shell = processes.get(process.ppid)
        if shell is None or not is_interactive_shell(shell) or process.tty != shell.tty:
            continue
        if is_interactive_shell(process) or is_bare_interpreter(process):
            continue
        # A pipeline shares one process group; name it after the group leader.
        if process.pgid not in commands or process.pid == process.pgid:
            commands[process.pgid] = Job(process.name, time.time() - process.elapsed, shell.pid)
    return commands


def format_duration(seconds):
    minutes, secs = divmod(int(seconds), 60)
    return f"{minutes}m {secs}s" if minutes else f"{secs}s"


def announce(name, seconds, children):
    children.append(subprocess.Popen(
        ["osascript", "-e", ALERT_SCRIPT, f"Command Complete: {name}",
         f"Execution finished in {format_duration(seconds)}."],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))


def main(log):
    jobs = running_commands(snapshot())
    children = []
    log.info("started")
    while True:
        time.sleep(POLL_SECONDS)
        processes = snapshot()
        if not processes:
            continue
        current = running_commands(processes)
        for pgid in set(jobs) - set(current):
            job = jobs.pop(pgid)
            duration = time.time() - job.start
            # If the shell itself is gone the terminal was closed; nobody is waiting on this.
            if duration > MIN_SECONDS and job.shell_pid in processes:
                log.info("%s finished after %s", job.name, format_duration(duration))
                announce(job.name, duration, children)
        for pgid, job in current.items():
            jobs.setdefault(pgid, job)
        children[:] = [child for child in children if child.poll() is None]


if __name__ == "__main__":
    run_main("command_notifier", main)
