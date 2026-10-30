"""The inbox unsubscribe agent. No LLM anywhere in it: every decision is a rule.

    capture.py   takes the `inbox` events the mail poller queues (every 5 minutes) and keeps each new
                 inbox message (headers, body) and what you do with it:
                 opened, deleted (unread or not), archived, starred, replied, marked as spam
    analyze.py   weekly (the `unsubscriber` launchd agent): sums the week up per sender, sorts
                 senders into keep / unsubscribe / block / unsure (rules.py, config.json), applies
                 saved rules in permanent mode, writes the report and shows a banner
    actions.py   carries out what you approve: unsubscribe (one-click, mailto, then the browser:
                 browser.py), block (that category of their mail goes to Trash as it arrives)
    ops.py       the commands: `unsub report`, `unsub decide approve=s3,s5` …, at the assistant's
                 prompt and in a terminal (`python -m unsub`)

You use it through the inbox dashboard (atlas/inbox_dashboard.py, in the assistant: type "inbox
report" at the prompt, or click the weekly banner).

Senders are `s<number>`. The database is unsub.db in the state folder; the email layer's own
tables are only reached through mail/ops.py.
"""
from lib.identity import notification

# Posted (by a banner click, or `python -m unsub open`) to make the assistant open the dashboard.
OPEN_DASHBOARD = notification("open-inbox-report")
