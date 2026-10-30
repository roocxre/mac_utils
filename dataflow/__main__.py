"""`python -m dataflow <command>`: where-does-my-email-go from a terminal.

    .venv/bin/python -m dataflow status
    .venv/bin/python -m dataflow site example.com
    .venv/bin/python -m dataflow recent since=7d
    .venv/bin/python -m dataflow mode example.com strict
    .venv/bin/python -m dataflow canary url=https://example.com/signup
    .venv/bin/python -m dataflow policy example.com type=email
    .venv/bin/python -m dataflow install        (register the helper with Edge; then load the extension once)
    .venv/bin/python -m dataflow refresh        (download the public lists now)
    .venv/bin/python -m dataflow work           (run the queued policy analyses and canary tests)
    .venv/bin/python -m dataflow train-fields   (retrain the fallback field classifier's head)
    .venv/bin/python -m dataflow help
"""
import sys

from lib.common import get_logger
from dataflow import ops


def work(log, limit=20):
    """Run queued jobs while nobody else is (the host or the agent). Returns how many ran."""
    from dataflow import service
    from mail import store as mail_store

    done = 0
    with mail_store.file_lock("dataflow-work", wait=False) as held:
        if not held:
            return 0
        while done < limit:
            job = service.next_job()
            if not job:
                break
            service.run_job(job, log)
            done += 1
    return done


def main(argv):
    words = argv[1:]
    if not words or words[0] in ("help", "-h", "--help"):
        print(__doc__.strip() + "\n\n" + ops.usage())
        return 0
    log = get_logger("dataflow")
    if words[0] == "install":
        from dataflow import install
        print(ops.as_json(install.install()))
        return 0
    if words[0] == "refresh":
        from dataflow import lists
        print(ops.as_json(lists.refresh(force="--force" in words, log=log)))
        return 0
    if words[0] == "work":
        print(ops.as_json({"jobs_run": work(log)}))
        return 0
    if words[0] == "train-fields":
        from dataflow import fields
        print(ops.as_json(fields.train()))
        return 0
    line = " ".join(f'{w.split("=", 1)[0]}="{w.split("=", 1)[1]}"' if "=" in w and " " in w else w for w in words)
    print(ops.call("dataflow " + line))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
