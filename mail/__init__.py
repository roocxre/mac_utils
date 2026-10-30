"""The shared email layer: one Gmail account, used by every email agent. No language model anywhere.

    auth.py         OAuth sign-in (desktop client), tokens in the Keychain, one refresher at a time
    gmail.py        the API client: backoff on 429/5xx, one rate limit shared by every process
    store.py        the SQLite database (mail.db in the state folder) and the file locks
    poller.py       the one history poller; hands events to the agents (events table)
    headers.py      header parsing: addresses, Authentication-Results, ARC
    ops.py          the operations other agents, atlas and the CLI call; compact JSON

The poller runs every 5 minutes as the `mailpoller` launchd agent (agents/mail_poller.py).
From a terminal: `.venv/bin/python -m mail <op> key=value …` (`python -m mail help`).
"""
