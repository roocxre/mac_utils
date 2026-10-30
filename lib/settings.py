"""A component's settings: its config.json defaults, with your own changes laid over them.

The shipped config.json is replaced on every update, so your changes live in the state folder as
<component>-config.json, holding only the keys you changed (nested objects merge key by key):

    {"jurisdiction": {"states": ["NY"], "checked": "2026-11-01"}}
"""
import json
import os

from lib.identity import STATE_DIR


def override_path(component):
    return os.path.join(STATE_DIR, f"{component}-config.json")


def _merge(base, mine):
    for key, value in mine.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            _merge(base[key], value)
        else:
            base[key] = value
    return base


def load(component, defaults_path):
    with open(defaults_path, encoding="utf-8") as f:
        settings = json.load(f)
    try:
        with open(override_path(component), encoding="utf-8") as f:
            mine = json.load(f)
    except FileNotFoundError:
        return settings
    return _merge(settings, mine) if isinstance(mine, dict) else settings
