"""`python -m brokers <command>`: the broker agent's commands from a terminal.

    .venv/bin/python -m brokers status
    .venv/bin/python -m brokers report group=manual
    .venv/bin/python -m brokers decide approve=spokeo
    .venv/bin/python -m brokers run [--notify]      (one pass here and now; --notify shows the weekly banner)
    .venv/bin/python -m brokers scan [spokeo]       (search one site now)
    .venv/bin/python -m brokers weekly [--notify]   (make this week's report now)
    .venv/bin/python -m brokers import [state=vt file=~/Downloads/vt.csv]   (rebuild the registry from the lists)
    .venv/bin/python -m brokers validate [--strict]
    .venv/bin/python -m brokers open                (open the broker dashboard in the running assistant)
    .venv/bin/python -m brokers debug               (watch the agent live: its steps and its browser)
    .venv/bin/python -m brokers handoff 12          (task 12's page in a visible window: you pass the check, it does the rest)
    .venv/bin/python -m brokers formcheck [n=100] [fresh]   (dry-run the form filler on opt-out pages: nothing sent)
    .venv/bin/python -m brokers formcheck report    (what kinds of form it met, and how often)
    .venv/bin/python -m brokers help
"""
import sys

from lib.common import get_logger
from brokers import ops


def main(argv):
    notify = "--notify" in argv
    words = [w for w in argv[1:] if w != "--notify"]
    if not words or words[0] in ("help", "-h", "--help"):
        print(__doc__.strip() + "\n\n" + ops.usage())
        return 0
    log = get_logger("broker_agent")
    if words[0] == "handoff" and len(words) > 1:
        from brokers import handoff
        print(ops.as_json(handoff.run(log, int(words[1]))))
        return 0
    if words[0] == "formcheck":
        from brokers import formcheck
        if "report" in words:
            print(ops.as_json(formcheck.summary()))
            return 0
        args = dict(w.split("=", 1) for w in words[1:] if "=" in w)
        print(ops.as_json(formcheck.run(get_logger("broker_formcheck"), int(args.get("n", formcheck.SAMPLE)),
                                        fresh="fresh" in words)))
        return 0
    if words[0] == "run":
        from brokers import work
        print(ops.as_json(work.run_pass(log, notify=notify)))
        return 0
    if words[0] == "scan":
        from brokers import forms, plan, registry, scan, vault, work
        found = vault.identity()
        if not vault.has_identity(found):
            print("add your details in the broker dashboard first")
            return 1
        rows = plan.ensure_rows()
        entries = registry.load()
        if len(words) > 1:
            broker_id, why = ops._broker(words[1]), "discover"
        else:
            with vault.db() as conn:
                due = work._scan_due(conn, plan.config(), rows, entries)
            if not due:
                print("no search is due")
                return 0
            broker_id, why = due
        with forms.session() as context:
            result = scan.scan_one(context, broker_id, entries[broker_id], found, plan.config(), log)
        scan.record(broker_id, result)
        work._after_scan(broker_id, why, result, plan.config(), plan.approval_mode())
        print(ops.as_json({"broker": broker_id, "listed": result["listed"], "listings": len(result["listings"]),
                           **{k: result[k] for k in ("detail", "error") if result.get(k)}}))
        return 0
    if words[0] == "weekly":
        from brokers import report
        print(ops.as_json(report.build(notify=notify)))
        return 0
    if words[0] == "import":
        from brokers import importers
        args = dict(w.split("=", 1) for w in words[1:] if "=" in w)
        print(ops.as_json(importers.run(state=args.get("state"), file=args.get("file"))))
        return 0
    if words[0] == "validate" and "--strict" in argv + words:
        words = ["validate", "strict=yes"]
    if words[0] in ("open", "debug", "watch"):
        from lib.components import installed, not_installed_message
        if not installed("assistant"):         # the broker windows open in the assistant
            print(not_installed_message("assistant", "The assistant, where the broker windows open,"))
            return 1
    if words[0] == "open":
        from atlas import broker_dashboard
        broker_dashboard.post_open()
        print("asked the assistant to open the broker dashboard")
        return 0
    if words[0] in ("debug", "watch"):
        from atlas import broker_watch
        broker_watch.post_open()
        print("asked the assistant to open the broker watch window")
        return 0
    # Quote values with spaces back, as the shell took the quotes off.
    line = " ".join(f'{w.split("=", 1)[0]}="{w.split("=", 1)[1]}"' if "=" in w and " " in w else w for w in words)
    print(ops.call("brokers " + line))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
