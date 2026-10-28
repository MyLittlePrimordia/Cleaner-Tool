"""N-002 GUI: the Scorecard's before/after block renders the honest verdict.

Builds a real ScorecardDialog with canned readings (no run, no registry) and
checks the three outcomes the block must distinguish: a real move, an explicit
"no measurable change", and silence when nothing could be read.
"""
from __future__ import annotations

import pytest

tk = pytest.importorskip("tkinter")

from app.ui.dialogs.scorecard_dialog import ScorecardDialog

from _tkhelpers import make_root


def _snap(mem_avail, restart=False, total=16_000_000_000):
    return {"mem_total": total, "mem_avail": mem_avail,
            "restart_pending": restart}


def _build(measurements):
    root = make_root()
    root.withdraw()
    card = ScorecardDialog(
        root, tab_name="Clean", mode="run", cancelled=False,
        results=[("Temp files", "ok", 1024, None)],
        total_bytes=0, measurements=measurements)
    return root, card


def test_no_measurable_change_is_stated_plainly():
    before = _snap(1_000_000_000)
    root, card = _build((before, _snap(1_000_000_000)))
    try:
        assert card._measure_rows is None
        assert card._measure_lbl is not None
        assert "No measurable change" in card._measure_lbl.cget("text")
        assert "No measurable change" in card._summary_text()
    finally:
        card.close()
        root.destroy()


def test_a_real_change_renders_a_row_and_reaches_the_summary():
    root, card = _build((_snap(1_000_000_000), _snap(3_000_000_000)))
    try:
        assert card._measure_rows and len(card._measure_rows) == 1
        assert card._measure_lbl is None
        text = card._summary_text()
        assert "Before \u2192 after:" in text
        assert "Available memory" in text
    finally:
        card.close()
        root.destroy()


def test_unreadable_readings_stay_silent():
    root, card = _build((None, None))
    try:
        assert card._measure_rows is None
        assert card._measure_lbl is None
        assert "measurable" not in card._summary_text()
    finally:
        card.close()
        root.destroy()


def test_no_measurements_kwarg_still_builds():
    root = make_root()
    root.withdraw()
    card = ScorecardDialog(root, tab_name="Tweak", mode="run", cancelled=False,
                           results=[], total_bytes=0)
    try:
        assert card._measure_rows is None
        assert card._measure_lbl is None
    finally:
        card.close()
        root.destroy()
