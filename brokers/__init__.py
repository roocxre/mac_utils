"""The data broker removal agent. No language model anywhere in it: requests are fixed
templates, web forms run per-broker definitions, and replies are sorted by rules and a small local
NLI model (DeBERTa-v3 xsmall, ONNX, in models/). Anything those can't handle becomes a manual task.

    registry/       one YAML file per broker; `python -m brokers validate [--strict]`
    registry.py     the registry format, loading and the validator
    importers.py    builds the registry from the state registries and the opt-out lists
    vault.py        your details and the disclosure ledger, in an SQLCipher database (brokers.db)
                    whose key is in the login Keychain; also the agent's own tables
    plan.py         tiers, the state machine, what to propose and what an approval covers
    templates.py    the request emails: deletion, access, follow-up, complaint
    send.py         the email pipeline: spread over the days, threads registered with the mail poller
    replies.py      broker mail from the poller: matching, confirmation links, reply classification
    nli.py          the NLI model
    scan.py         searching people-search sites for your listings, with match scoring
    forms.py        web forms: per-broker selectors, privacy managers, the generic runner
    report.py       the weekly report
    work.py         the agent's rolling work: one pass every 10 minutes (agents/broker_agent.py)
    ops.py          the commands, at the assistant's prompt and in a terminal (`python -m brokers`)

You use it through the broker dashboard (atlas/broker_dashboard.py, in the assistant: type "broker
report" at the prompt, or click the weekly banner); your details are entered there too, and only
there. The commands show them masked.

Every request is a voluntary one under the broker's own published opt-out process: no template
claims residency anywhere or a right under any law. Only a law you add for the state you live in
changes that (config.json "jurisdiction").
"""
import os

from lib.identity import notification

ROOT = os.path.dirname(os.path.abspath(__file__))
REGISTRY_DIR = os.path.join(ROOT, "registry")
AGENT = "brokers"           # the mail poller's queue (mail.poller.BROKER_AGENT)

# Posted (by a banner click, or `python -m brokers open`) to make the assistant open the dashboard.
OPEN_DASHBOARD = notification("open-broker-report")
