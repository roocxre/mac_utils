# Third-party models, data and software

The code in this repository is under the MIT licence (`LICENSE`). Nothing below is stored in the
repository or bundled in the installer: each item is downloaded by your own Mac, at install time or
while the agents run, from its original source and under that source's own terms.

## Models (downloaded at install, pinned in `models.lock.json`)

| Model | Source | Licence | Used by |
|---|---|---|---|
| all-MiniLM-L6-v2 | [sentence-transformers/all-MiniLM-L6-v2](https://huggingface.co/sentence-transformers/all-MiniLM-L6-v2) | Apache-2.0 | the assistant, the Edge extension |
| nli-deberta-v3-xsmall (int8 ONNX) | [cross-encoder/nli-deberta-v3-xsmall](https://huggingface.co/cross-encoder/nli-deberta-v3-xsmall) | Apache-2.0 | the broker agent |
| nli-deberta-v3-base (int8 ONNX) | [cross-encoder/nli-deberta-v3-base](https://huggingface.co/cross-encoder/nli-deberta-v3-base) | Apache-2.0 | the Edge extension |
| bert-base-NER (int8 ONNX) | [Xenova/bert-base-NER](https://huggingface.co/Xenova/bert-base-NER), converted from [dslim/bert-base-NER](https://huggingface.co/dslim/bert-base-NER) | MIT | the Edge extension |

The extension's small field classifier (`models/field-head.npz`) is trained on your Mac at install
from the examples in `dataflow/fields.py`.

## The data broker registry (built on your Mac by `python -m brokers import`)

The registry is not redistributed. The importer reads these public sources directly:

- [California Privacy Protection Agency data broker registry](https://cppa.ca.gov/data_broker_registry/)
- [eraser](https://github.com/digisamroc/eraser): broker privacy contacts. No licence stated.
- [auto-identity-remove](https://github.com/stephenlthorn/auto-identity-remove): opt-out form definitions. MIT.
- [Big-Ass Data Broker Opt-Out List](https://github.com/yaelwrites/Big-Ass-Data-Broker-Opt-Out-List):
  tiers and notes. Its README states CC BY-NC-SA, so the registry files the importer writes are for
  personal, non-commercial use.

## Lists the agents fetch while they run

- [Public Suffix List](https://publicsuffix.org/) (MPL-2.0): email and site domains.
- [OISD NSFW list](https://oisd.nl/) (GPL-3.0): the adult-site blocker.
- [DuckDuckGo Tracker Radar](https://github.com/duckduckgo/tracker-radar),
  [Disconnect tracking protection lists](https://github.com/disconnectme/disconnect-tracking-protection)
  and [EasyPrivacy](https://easylist.to/): the Edge extension's tracker data, each under its own terms.

## Software

- Python packages are installed from PyPI at the versions in `uv.lock`, each under its own licence.
- [Playwright](https://playwright.dev/) (Apache-2.0) downloads its own build of Chromium for the
  agents that open web pages.
- [uv](https://github.com/astral-sh/uv) (MIT or Apache-2.0) is used by the installer to set up Python.
- Text recognition uses Apple's Vision framework, which is part of macOS.
