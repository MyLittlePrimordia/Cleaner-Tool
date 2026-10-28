"""The percentage a long repair prints must reach the strip and the sentence.

The 2026-10 user report was "DISM RestoreHealth just shows the text with the
animation going in a loop, no progress or percentage". The parser and the
callback exist; this pins the arithmetic that folds a task's own percentage
into its slot in the whole run, and the status text that names both.
"""
from __future__ import annotations

from app.ui.app import Application


class _Progress:
    def __init__(self):
        self.indeterminate = []
        self.fractions = []

    def set_indeterminate(self, on):
        self.indeterminate.append(on)

    def set_fraction(self, value):
        self.fractions.append(value)


def _app():
    app = object.__new__(Application)
    app._live_task = (1, 4, "DISM RestoreHealth")
    app._live_pct = -1
    app._busy = True
    app.progress_bar = _Progress()
    app._ui_hop = lambda fn: fn()
    statuses = []
    app.set_status = statuses.append
    return app, statuses


def test_a_task_percentage_is_folded_into_the_whole_run():
    app, statuses = _app()
    app._task_progress(61.2)
    assert app.progress_bar.indeterminate[-1] is False
    assert app.progress_bar.fractions[-1] == (1 + 0.612) / 4.0
    assert statuses[-1] == "DISM RestoreHealth — 61% (step 2 of 4)"


def test_sfc_style_zero_percent_still_draws_a_real_bar():
    app, statuses = _app()
    app._task_progress(0.0)
    assert app.progress_bar.fractions[-1] == (1 + 0.0) / 4.0
    assert "0%" in statuses[-1]


def test_the_same_whole_percentage_is_not_repainted():
    app, statuses = _app()
    app._task_progress(61.2)
    app._task_progress(61.8)       # same whole percent: no extra UI hop
    assert len(statuses) == 1
    app._task_progress(62.0)
    assert len(statuses) == 2


def test_progress_without_a_current_task_is_ignored():
    app, statuses = _app()
    app._live_task = None
    app._task_progress(50)
    assert statuses == []
