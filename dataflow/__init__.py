"""Where does my email go: an Edge extension that shows, under every field you can type into,
which scripts and companies listen to it, read it or send it somewhere, which of them are data
brokers, and what the site's privacy policy says about that kind of data. It blocks what can be
blocked and runs canary tests on risky fields. No language model anywhere: keyword rules, small
local transformers, and public lists.

    extension/      the Edge extension (MV3): inject.js wraps the page's functions (MAIN world),
                    fields.js + content.js find fields and draw the overlay, background.js keeps
                    the blocking rules and talks to the helper below
    host.py         the native messaging host Edge starts (HOST_NAME below)
    entities.py     domain -> owner company, categories, data broker (Tracker Radar, Disconnect,
                    data/platforms.yaml, and the broker registry when the broker agent is installed)
    lists.py        the weekly downloads of those lists
    policy.py       finds and analyzes privacy policies (NLI, NER, and PrivBERT on OPP-115 when
                    that optional model is there), cached per site for 30 days or until the text changes
    canary.py       canary tests in headless Chromium, never submitting
    fields.py       the fallback field classifier, for fields the keyword rules can't place
    store.py        the SQLite cache (dataflow.db in the state folder)
    ops.py          the commands, at the assistant's prompt and in a terminal; compact JSON, every list paged
    install.py      registers the host with Edge and gives this install's extension its own id

The `dataflow` launchd agent (agents/dataflow_agent.py) refreshes the lists weekly and runs what
the extension queued while the browser was closed. `.venv/bin/python -m dataflow help`.
"""
import os

from lib.identity import APP_ID

ROOT = os.path.dirname(os.path.abspath(__file__))
EXTENSION_DIR = os.path.join(ROOT, "extension")
DATA_DIR = os.path.join(ROOT, "data")
MODELS_DIR = os.path.join(os.path.dirname(ROOT), "models")
HOST_NAME = f"{APP_ID}.dataflow"         # background.js names it too, as a literal
MODES = ("observe", "block", "strict")
DEFAULT_MODE = "block"
