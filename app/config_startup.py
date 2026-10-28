"""Startup Manager undo log: what this app disabled, with how to put it back.

Split out of `app.config_persist` (R-006, continued). This is one config region
(`startup_disabled`) whose records must be complete enough to restore an item
exactly, so the whitelist that builds each record, the newest-wins truncation
and the remove path belong together.

Two bug fixes live in here and are commented where they are, because both are
about the same hazard: the caller has ALREADY made the live change (deleted a
registry value, moved a shortcut) by the time these functions are called, so a
record that is dropped, truncated away or stripped of a field strands a real
change with no way back.

`app.config_persist` re-exports all three names; the config doors are fetched at
call time to avoid the cycle described in `config_tweaks`.
"""
from __future__ import annotations

import os

from app.config_schema import STARTUP_MAX


def _cp():
    from app import config_persist
    return config_persist


def get_startup_disabled() -> list:
    """[{source, name, command, value_type}] for every item the user has
    disabled — the data needed to put each one back exactly as it was."""
    try:
        items = _cp().load_config().get("startup_disabled")
        return list(items) if isinstance(items, list) else []
    except Exception:
        return []


def add_startup_disabled(record: dict) -> bool:
    """Record one newly-disabled item. True when stored.

    Single-lock RMW (H9 contract): the caller has already made the live
    change (removed the registry value / moved the shortcut) by the time
    this is called, so losing the race here would strand that change
    with no way back — the lock makes that not happen."""
    try:
        source = str(record.get("source", "")).strip()
        name = str(record.get("name", "")).strip()[:200]
        command = str(record.get("command", ""))[:4096]
        value_type = record.get("value_type", "")
        value_type = value_type if isinstance(value_type, str) else ""
        if not source or not name:
            return False
        # BUG-016: held_name was dropped here. This function rebuilds the
        # record from an explicit whitelist and held_name was not on it, so
        # startup_manager's careful os.path.basename(dest) never reached the
        # config -- SEC-002 was silently defeated and EVERY restore fell
        # through to the legacy guess-the-extension path, which can then
        # hard-fail on the tamper check. Carry it through.
        held_name = str(record.get("held_name", "")).strip()
        clean = {"source": source, "name": name, "command": command,
                 "value_type": value_type}
        if held_name:
            clean["held_name"] = os.path.basename(held_name)[:200]

        def _mut(cfg):
            items = cfg.get("startup_disabled")
            if not isinstance(items, list):
                items = []
            items = [r for r in items
                     if not (r.get("source") == source and r.get("name") == name)]
            items.append(clean)
            # BUG-015: `clean` was appended and the list then truncated from
            # the END, so once the log was full the record just written was
            # discarded immediately. The caller has ALREADY removed the
            # registry value / moved the shortcut by this point, so that
            # stranded a live change with no undo -- the exact outcome the
            # single-lock docstring above exists to prevent. This is an undo
            # log, so keep the newest.
            if len(items) > STARTUP_MAX:
                items = items[-STARTUP_MAX:]
            cfg["startup_disabled"] = items

        _cp().update_config(_mut)
        return True
    except Exception:
        return False


def remove_startup_disabled(source: str, name: str) -> bool:
    """Drop one record once its item has been re-enabled. True when
    something was actually removed."""
    try:
        removed = {"hit": False}

        def _mut(cfg):
            items = cfg.get("startup_disabled")
            if not isinstance(items, list):
                return
            kept = [r for r in items
                    if not (r.get("source") == source and r.get("name") == name)]
            if len(kept) != len(items):
                removed["hit"] = True
            cfg["startup_disabled"] = kept

        _cp().update_config(_mut)
        return removed["hit"]
    except Exception:
        return False
