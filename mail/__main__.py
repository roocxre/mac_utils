"""`python -m mail <command>`: the email layer's commands from a terminal.

    .venv/bin/python -m mail setup ~/Downloads/client_secret_….json
    .venv/bin/python -m mail poll
    .venv/bin/python -m mail status
    .venv/bin/python -m mail help
"""
import logging
import sys

from mail import ops


def _setup_and_notify(rest):
    from lib.common import notify
    from mail import auth

    try:
        done = auth.setup(" ".join(rest))
    except Exception as error:
        print(f"setup failed: {error}")
        notify("Gmail sign-in failed", str(error)[:200])
        return 1
    missing = done["missing_scopes"]
    notify("Gmail signed in", f"as {done['address']}" + (f"; NOT granted: {', '.join(missing)}" if missing else ""))
    return 0


def main(argv):
    words = [w for w in argv[1:] if w != "--notify"]
    if "--notify" in argv and words[:1] == ["setup"]:
        return _setup_and_notify(words[1:])
    if not words or words[0] in ("help", "-h", "--help"):
        print(__doc__.strip() + "\n\n" + ops.usage())
        return 0
    if words[0] == "poll":
        logging.basicConfig(level=logging.INFO, format="%(message)s")
        print(ops.as_json(ops.poll(logging.getLogger("mail"))))
        return 0
    if words[0] != "mail":
        words = ["mail"] + words
    # Quote values with spaces back, as the shell took the quotes off.
    line = " ".join(f'{w.split("=", 1)[0]}="{w.split("=", 1)[1]}"' if "=" in w and " " in w else w for w in words)
    print(ops.call(line))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
