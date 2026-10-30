"""Looks a program up in Homebrew and, once you say yes, runs the install in a Terminal window.

Nothing is installed in the background: this only finds the package and describes it. The
install itself is an ordinary `brew install` that you start with one click and can watch.
"""
import json
import os
import re
import subprocess
from collections import namedtuple

from lib.common import state_path

BREW = next((p for p in ("/opt/homebrew/bin/brew", "/usr/local/bin/brew") if os.path.exists(p)), None)
ENV = dict(os.environ, PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin",
           HOMEBREW_NO_AUTO_UPDATE="1", HOMEBREW_NO_ENV_HINTS="1", HOMEBREW_NO_COLOR="1")
# Homebrew package names: lowercase letters, digits and a few separators. Nothing else is
# ever passed to a command line.
TOKEN_RE = re.compile(r"^[a-z0-9][a-z0-9@._+-]{0,80}$")
OFFICIAL_TAPS = ("homebrew/core", "homebrew/cask")

Package = namedtuple("Package", "kind token name version desc tap installed deprecated disabled")


def _brew(*args):
    return subprocess.run([BREW, *args], stdin=subprocess.DEVNULL, capture_output=True, text=True,
                          env=ENV, timeout=60, check=False)


def normalize(name):
    return re.sub(r"\s+", "-", name.strip().lower())


def lookup(token):
    """Packages named exactly this: an app (cask), a command-line tool (formula), both or neither."""
    if BREW is None or not TOKEN_RE.match(token):
        return []
    found = []
    for kind, flag, key in (("cask", "--cask", "casks"), ("formula", "--formula", "formulae")):
        result = _brew("info", "--json=v2", flag, token)
        if result.returncode != 0:
            continue
        try:
            for item in json.loads(result.stdout).get(key, []):
                if kind == "cask":
                    found.append(Package("cask", item["token"], (item.get("name") or [item["token"]])[0],
                                         item.get("version") or "", item.get("desc") or "", item.get("tap") or "",
                                         bool(item.get("installed")), bool(item.get("deprecated")),
                                         bool(item.get("disabled"))))
                else:
                    found.append(Package("formula", item["name"], item["name"],
                                         (item.get("versions") or {}).get("stable") or "", item.get("desc") or "",
                                         item.get("tap") or "", bool(item.get("installed")),
                                         bool(item.get("deprecated")), bool(item.get("disabled"))))
        except (ValueError, KeyError, TypeError):
            continue
    return [p for p in found if p.tap in OFFICIAL_TAPS and TOKEN_RE.match(p.token)]


def search(text, limit=10):
    """Names containing the text, as (kind, token) pairs, apps first."""
    if BREW is None or not re.match(r"^[\w .@+-]{1,60}$", text):
        return []
    results = []
    for kind, flag in (("cask", "--cask"), ("formula", "--formula")):
        out = _brew("search", flag, text)
        tokens = [line.strip() for line in out.stdout.splitlines() if TOKEN_RE.match(line.strip())]
        results.extend((kind, token) for token in tokens[:limit])
    return results


def label(kind, token):
    return f"{token}  ({'app' if kind == 'cask' else 'command-line tool'})"


def describe(pkg):
    what = "app" if pkg.kind == "cask" else "command-line tool"
    lines = [f"{pkg.name} {pkg.version} ({what})"]
    if pkg.desc:
        lines.append(pkg.desc)
    if pkg.deprecated:
        lines.append("Note: Homebrew marks this as deprecated.")
    lines.append(f"\nThis runs in Terminal:\n{' '.join(command(pkg))}")
    return "\n".join(lines)


def command(pkg):
    return [BREW, "install", "--cask" if pkg.kind == "cask" else "--formula", pkg.token]


def run_in_terminal(pkg):
    """Open a Terminal window running the install. Returns an error message, or '' on success."""
    if not TOKEN_RE.match(pkg.token):
        return "that package name isn't one i'll run"
    script = state_path(f"brew-install-{pkg.token}.command")
    with open(script, "w", encoding="utf-8") as f:
        f.write("#!/bin/zsh\n"
                'rm -f -- "$0"\n'
                f"{' '.join(command(pkg))}\n"
                'echo\necho "Finished. You can close this window."\n')
    os.chmod(script, 0o700)
    result = subprocess.run(["open", "-a", "Terminal", script], capture_output=True, text=True, timeout=15)
    return "" if result.returncode == 0 else f"couldn't open Terminal: {result.stderr.strip()[:200]}"
