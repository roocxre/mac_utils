"""The native messaging host Edge starts for the extension (registered by install.py as
dataflow.HOST_NAME, io.github.roocxre.macutils.dataflow). It lives as long as the extension's port is open.

Messages are JSON with a 4-byte length in front, both ways. The extension sends
{"id": n, "op": …, …} and gets {"id": n, "ok": …} or {"id": n, "error": "…"} back. The host also
pushes {"push": "site", "site": …} when a policy analysis or canary test for that site is done,
and {"push": "settings", …} when a site's mode was changed from a command.

Policy analyses and canary tests run on a worker thread, one at a time, and only while this
process holds the work lock: the `dataflow` launchd agent runs whatever is left when Edge is closed.
"""
import json
import struct
import sys
import threading
import time
import traceback

from lib.common import get_logger

log = get_logger("dataflow_host")
_out_lock = threading.Lock()
_wake = threading.Event()
MAX_OUT = 1000 * 1024                       # Edge drops messages from a host over 1 MB


def read_message(stream):
    header = stream.read(4)
    if len(header) < 4:
        return None
    (length,) = struct.unpack("=I", header)
    body = stream.read(length)
    return json.loads(body.decode("utf-8"))


def write_message(message, stream=None):
    stream = stream or sys.stdout.buffer
    body = json.dumps(message, separators=(",", ":")).encode("utf-8")
    if len(body) > MAX_OUT:
        body = json.dumps({"id": message.get("id"), "error": "answer too large"}).encode("utf-8")
    with _out_lock:
        stream.write(struct.pack("=I", len(body)) + body)
        stream.flush()


def handle(message):
    """The answer to one message from the extension (without its id)."""
    from dataflow import service

    op = message.get("op")
    site = message.get("site") or ""
    if op == "hello":
        from dataflow import store
        store.put("host_seen_at", time.time())
        return service.hello()
    if op == "settings":
        return service.settings()
    if op == "resolve":
        return service.resolve(site, message.get("urls") or [])
    if op == "site":
        info = service.site_info(site, message.get("page_url"), message.get("policy_links") or (),
                                 message.get("signals"))
        _wake.set()
        return info
    if op == "page":
        result = service.record_page(site, message.get("url", ""), message.get("risk"), message.get("fields") or [],
                                     message.get("recipients") or [])
        if result.get("canary"):
            _wake.set()
        return result
    if op == "policy":
        return service.policy_passages(site, message.get("data_type"), int(message.get("page", 1)))
    if op == "mode":
        return service.set_mode(site, message.get("mode"))
    if op == "overlay":
        return service.set_overlay(site, message.get("on", True))
    if op == "canary":
        result = service.queue_canary(site, message.get("url"), message.get("selector"), message.get("field_type"),
                                      why="you")
        _wake.set()
        return result
    if op == "submitted":
        return service.submitted(site, message.get("url", ""), bool(message.get("real_email")),
                                 message.get("domains") or [])
    if op == "blocked":
        return service.record_blocked(site, message.get("domain", ""), message.get("kind", ""),
                                      message.get("detail", ""))
    if op == "classify_fields":
        from dataflow import fields
        return fields.classify(message.get("contexts") or [])
    if op == "ping":
        return {"pong": time.time()}
    raise ValueError(f"unknown op {op!r}")


def _answer(message):
    try:
        write_message({"id": message.get("id"), "ok": handle(message)})
    except Exception as error:
        log.warning("op %s failed: %s\n%s", message.get("op"), error, traceback.format_exc(limit=4))
        write_message({"id": message.get("id"), "error": f"{type(error).__name__}: {error}"})


def worker():
    """Policy analyses and canary tests, one at a time, while the port is open."""
    from dataflow import service
    from mail import store as mail_store

    while True:
        _wake.wait(timeout=60)
        _wake.clear()
        while True:
            job = service.next_job()
            if not job:
                break
            with mail_store.file_lock("dataflow-work", wait=False) as held:
                if not held:
                    break                       # the launchd agent (or another profile's host) is at it
                try:
                    site = service.run_job(job, log)
                except Exception:
                    log.exception("job %s failed", job)
                    site = None
            if site:
                write_message({"push": "site", "site": site})


def settings_watch():
    """Tell the extension when a site's mode changed outside it (a command)."""
    from dataflow import service, store
    seen = store.get("settings_version", 0)
    while True:
        time.sleep(5)
        version = store.get("settings_version", 0)
        if version != seen:
            seen = version
            write_message({"push": "settings", **service.settings()})


def main():
    stdin = sys.stdin.buffer
    threading.Thread(target=worker, daemon=True).start()
    threading.Thread(target=settings_watch, daemon=True).start()
    while True:
        message = read_message(stdin)
        if message is None:
            return 0                            # Edge closed the port
        # Answers that may wait on the database or a model go on their own thread, so pings stay quick.
        threading.Thread(target=_answer, args=(message,), daemon=True).start()


if __name__ == "__main__":
    sys.exit(main())
