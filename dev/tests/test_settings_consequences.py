"""U-009: every Settings control says what changing it does.

A switch or dropdown that states only its own name ("Window animations") makes
the user guess the consequence, and the guessing is where a setting gets
switched on and forgotten. Toggles have carried a supporting line since the
2026-10 pass; the choice rows and the time field did not, so this guards both.

Source-level on purpose: the property is "no call site may be added without a
consequence", which is true of the code that will exist next month, not only of
the widgets a test can reach at runtime.
"""
from __future__ import annotations

import ast
from pathlib import Path

SETTINGS = Path(__file__).resolve().parents[2] / "app" / "ui" / "tabs" / "settingstab.py"

#: Row builders that render a consequence line, and the POSITION that line
#: takes when it is passed positionally (`self._toggle(card, key, label,
#: value, tip)`). `_entry` only ever takes it by keyword.
GUARDED = {"_toggle": 4, "_choice": 5, "_entry": None}


def _rows():
    tree = ast.parse(SETTINGS.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not isinstance(func, ast.Attribute):
            continue
        if not isinstance(func.value, ast.Name) or func.value.id != "self":
            continue
        if func.attr not in GUARDED:
            continue
        yield node, func.attr


def _tip_of(call, position):
    for keyword in call.keywords:
        if keyword.arg == "tip":
            return keyword.value
    if position is not None and len(call.args) > position:
        return call.args[position]
    return None


def test_every_settings_row_states_its_consequence():
    missing = []
    for call, name in _rows():
        tip = _tip_of(call, GUARDED[name])
        if tip is None:
            missing.append((name, call.lineno))
            continue
        try:
            text = ast.literal_eval(tip)
        except Exception:
            continue
        if not isinstance(text, str) or not text.strip():
            missing.append((name, call.lineno))
    assert not missing, (
        "these Settings rows do not say what changing them does: %r" % missing)


def test_the_guard_actually_walks_the_file():
    """A guard that matched nothing would pass forever."""
    assert len(list(_rows())) >= 10


def test_the_time_field_explains_a_missed_run():
    """U-009: 'Time' alone does not say what happens to a run that could not
    fire at 03:00, which is the one fact a scheduler must state."""
    text = SETTINGS.read_text(encoding="utf-8")
    assert "StartWhenAvailable" in text
