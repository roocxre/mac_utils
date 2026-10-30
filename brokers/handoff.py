"""Hand-off: a check only a person may pass (a Cloudflare page, a CAPTCHA on the form), passed by you,
and the rest done by the agent, in one visible window.

"Pass the check" on a CAPTCHA task in the broker dashboard runs `python -m brokers handoff <task>`:

    1. it waits for a pass that's running to end (they share the agent's browser profile)
    2. opens the task's page in a visible window of the agent's own browser
    3. waits up to forms.HANDOFF_WAIT for you to pass the check, then every later one (forms.HANDOFF)
    4. fills the form with the details you approved for this broker, exactly as a real run does, and
       submits it. Every other stop (an ID upload, payment, an account, a detail you didn't approve)
       still stops it.
    5. records the result like any form run (the ledger, the broker's state), and closes the task
       when it went through

You never type your details into it: the agent does, from the vault. It stays one window because a
passed check belongs to that browser; the hidden one would usually be checked again.
"""
import json
import time

from brokers import forms, plan, registry, send, vault, watch


def run(log, task_id):
    from mail import store as mail_store

    with vault.db() as conn:
        task = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        if not task or task["done_at"]:
            return {"error": f"task {task_id} isn't open"}
        broker_id = task["broker_id"]
        row = dict(conn.execute("SELECT * FROM brokers WHERE id = ?", (broker_id,)).fetchone())
    approved = json.loads(row["approved"]) if row["approved"] else None
    if not approved:
        return {"error": "this broker isn't approved: approve it first, so the agent knows which details to send"}
    entry = registry.broker(broker_id) or {}
    url = task["link"] or approved.get("form") or entry.get("opt_out_url") or entry.get("website")
    how = approved["method"] if approved["method"] in ("direct-form", "search-form", "generic") else "generic"
    watch.say(f"hand-off for {entry.get('name', broker_id)}: waiting for any running pass to finish", "pass", broker_id)
    with mail_store.file_lock("broker-pass"):           # no pass alongside: same browser profile, same requests
        with vault.db() as conn:
            kind = "resubmit" if conn.execute("SELECT 1 FROM requests WHERE broker_id = ? AND sent_at IS NOT NULL AND "
                                              "kind IN ('deletion', 'resubmit')", (broker_id,)).fetchone() else "deletion"
            send.queue(conn, broker_id, kind, detail="hand-off: you pass the check, the agent does the rest")
            request = dict(conn.execute("SELECT * FROM requests WHERE broker_id = ? AND status = 'queued' AND kind = ? "
                                        "ORDER BY id DESC LIMIT 1", (broker_id, kind)).fetchone())
            conn.execute("UPDATE requests SET method = ?, to_address = ? WHERE id = ?", (how, url, request["id"]))
            request.update(method=how, to_address=url)
            values = send.send_form(conn, request, entry, approved, how)
        if values is None:
            return {"error": "your details are missing something this form needs (see the task)"}
        watch.say(f"opening {url} in a window you can see", "pass", broker_id, url=url)
        forms.HANDOFF = True
        try:
            with forms.session(headless=False) as context:
                result = forms.run({**entry, "opt_out_url": url}, how, values, context=context)
        except Exception as error:
            result = forms.Result(False, "failed", f"{type(error).__name__}: {str(error)[:160]}")
        finally:
            forms.HANDOFF = False
        with vault.db() as conn:
            send._after_form(conn, request, entry, result, kind)
            if result.ok:
                conn.execute("UPDATE brokers SET follow_up_at = ?, followed_up = 0 WHERE id = ?",
                             (time.time() + plan.config()["follow_up_days"] * 86400, broker_id))
                conn.execute("UPDATE tasks SET done_at = ?, outcome = ? WHERE id = ?",
                             (time.time(), "done together: you passed the check, the agent sent it", task_id))
                vault.history(conn, broker_id, "hand-off", f"you passed the check; {result.detail}")
    watch.say(f"hand-off result: {result.outcome}: {result.detail}", "done" if result.ok else "warn", broker_id)
    return {"broker": broker_id, "outcome": result.outcome, "detail": result.detail}
