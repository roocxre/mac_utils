"""Monthly cleanup of package manager caches (Homebrew, pip, npm, pnpm, yarn, uv)."""
import glob
import os
import shutil

from lib.common import DRY_RUN, developer_tools_installed, run, run_main

# (command, timeout in seconds); a tool that isn't installed is skipped.
COMMANDS = [
    (["brew", "cleanup", "--prune=7", "-s"], 1800),
    (["pip3", "cache", "purge"], 120),
    (["npm", "cache", "clean", "--force"], 300),
    (["pnpm", "store", "prune"], 300),
    (["yarn", "cache", "clean"], 300),
    (["uv", "cache", "prune"], 300),
]


def build_path():
    """launchd starts jobs with a bare PATH, so add the default places tools are installed:
    Homebrew (/opt/homebrew) and the newest Node installed with nvm (~/.nvm)."""
    node_bins = sorted(
        glob.glob(os.path.expanduser("~/.nvm/versions/node/v*/bin")),
        key=lambda p: [int(n) for n in p.split("/")[-2].lstrip("v").split(".") if n.isdigit()],
    )
    extra = ["/opt/homebrew/bin", "/opt/homebrew/sbin"] + node_bins[-1:]
    return os.pathsep.join(extra + ["/usr/bin", "/bin", "/usr/sbin", "/sbin"])


def main(log):
    env = dict(os.environ, PATH=build_path(), HOMEBREW_NO_AUTO_UPDATE="1")
    for cmd, timeout in COMMANDS:
        tool = shutil.which(cmd[0], path=env["PATH"])
        # /usr/bin/pip3 is a stand-in that asks to install the developer tools when they're missing.
        if not tool or (tool.startswith("/usr/bin/") and not developer_tools_installed()):
            continue
        if DRY_RUN:
            log.info("would run %s (%s)", " ".join(cmd), tool)
            continue
        result = run([tool] + cmd[1:], timeout=timeout, env=env)
        if result is None:
            log.warning("%s: timed out or could not start", " ".join(cmd))
        elif result.returncode != 0:
            log.warning("%s: exit %d: %s", " ".join(cmd), result.returncode, result.stderr.strip()[-300:])
        else:
            last_line = (result.stdout.strip().splitlines() or ["ok"])[-1]
            log.info("%s: %s", " ".join(cmd), last_line)


if __name__ == "__main__":
    run_main("package_gc", main)
