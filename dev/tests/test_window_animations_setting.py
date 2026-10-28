"""The in-app "Window animations" setting (2026-10).

One value, three layers: config.json declares it (with a coercer, or a
hand-edited file could put anything there), `settings_apply` writes it, and the
chrome caches the decision so a hover never loads the config. The layers fail
differently, so they are pinned separately — and the schema test guards the
contract app/config_schema.py states for itself: every key has BOTH a default
and a coercer, or it is defaulted but never validated.
"""
from __future__ import annotations

import pytest


def test_the_default_follows_windows_and_the_schema_agrees():
    from app.config_persist import DEFAULT_CONFIG
    from app.config_schema import SCHEMA

    # "system" is the default on purpose: the Accessibility switch is the
    # user's standing decision, and a fresh install must not overrule it.
    # Both declarations, because dev/tests/test_config_schema.py requires it of
    # every key (a default with no coercer is never validated).
    assert DEFAULT_CONFIG["window_animations"] == "system"
    assert "window_animations" in SCHEMA


@pytest.mark.parametrize("stored,want", [
    ("off", "off"),
    ("on", "on"),
    ("system", "system"),
    ("  OFF  ", "off"),          # whitespace and case are not user errors
    ("On", "on"),
    ("sometimes", "system"),     # a value from a newer/edited file
    (7, "system"),
    (None, "system"),
])
def test_junk_falls_back_to_following_windows(stored, want):
    from app.config_persist import DEFAULT_CONFIG
    from app.config_schema import apply_schema

    data = apply_schema({"window_animations": stored}, DEFAULT_CONFIG)
    assert data["window_animations"] == want


def test_saving_writes_the_choice_and_ignores_nonsense(monkeypatch):
    from app import settings_apply

    saved = {}

    def _update(mut):
        data = {}
        mut(data)                  # what would have been written to disk
        saved.update(data)

    monkeypatch.setattr("app.config_persist.update_config", _update)

    results, errors = settings_apply.apply_settings(
        {"window_animations": "off"}, config={})
    assert saved == {"window_animations": "off"}
    assert errors == []
    assert any("animations" in r.lower() for r in results)

    # an unknown value is dropped, not written: the coercer would only turn it
    # back into "system" on the next load, so writing it hides a bug in the UI
    saved.clear()
    results, errors = settings_apply.apply_settings(
        {"window_animations": "sometimes"}, config={})
    assert saved == {}
    assert errors == []

    # a form that does not carry the key must leave the stored value alone
    saved.clear()
    settings_apply.apply_settings({"close_to_tray": True}, config={})
    assert "window_animations" not in saved
