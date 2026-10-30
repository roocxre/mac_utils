"""`python -m unsub <command>`: the inbox agent's commands from a terminal.

    .venv/bin/python -m unsub status
    .venv/bin/python -m unsub report format=text
    .venv/bin/python -m unsub decide approve=s3,s5
    .venv/bin/python -m unsub analyze            (here and now; add --notify for the banner)
    .venv/bin/python -m unsub spamfolder         (read the Spam folder, then analyse, here and now)
    .venv/bin/python -m unsub work               (run the queued actions here and now)
    .venv/bin/python -m unsub open               (open the dashboard in the running assistant)
    .venv/bin/python -m unsub help
"""
import sys

from lib.common import get_logger
from unsub import ops


def main(argv):
    notify = "--notify" in argv
    words = [w for w in argv[1:] if w != "--notify"]
    if not words or words[0] in ("help", "-h", "--help"):
        print(__doc__.strip() + "\n\n" + ops.usage())
        return 0
    if words[0] == "analyze":
        from unsub import analyze
        print(ops.as_json(analyze.analyze(get_logger("unsubscriber"), notify=notify)))
        return 0
    if words[0] == "spamfolder":
        from unsub import analyze
        print(ops.as_json(analyze.spam_pass(get_logger("unsubscriber"), notify=notify)))
        return 0
    if words[0] == "open":
        from lib.components import installed, not_installed_message
        if not installed("assistant"):         # the dashboard is a window in the assistant
            print(not_installed_message("assistant", "The assistant, where the inbox dashboard opens,"))
            return 1
        from atlas import inbox_dashboard
        inbox_dashboard.post_open()
        print("asked the assistant to open the inbox dashboard")
        return 0
    if words[0] == "work":
        from unsub import actions
        result = actions.work(get_logger("unsubscriber"), notify=notify)
        print(result.get("text") or result.get("skipped"))
        return 0
    # Quote values with spaces back, as the shell took the quotes off.
    line = " ".join(f'{w.split("=", 1)[0]}="{w.split("=", 1)[1]}"' if "=" in w and " " in w else w for w in words)
    print(ops.call("unsub " + line))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
