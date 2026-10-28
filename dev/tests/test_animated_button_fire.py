"""AnimatedButton fires a command once per click, not once per release event.

The button does not run its command on release: it schedules ``after(60,
_fire)`` so the press animation can play. A double click delivers two
press/release pairs inside that window, so the second release used to queue a
second fire. Both fires found ``_enabled`` still True and both ran the command:
one decision ran ``Remove-MpPreference`` twice (two dialogs), and a toggle such
as Game Mode started and then immediately ended the session.

These tests use the real widget and real Tk events, because the bug lives in
the deferral, not in any handler.
"""
from __future__ import annotations

import time

import pytest

tk = pytest.importorskip("tkinter")

from app.ui.widgets import AnimatedButton
from dev.tests._tkhelpers import make_root


def _pump(root, seconds=0.4):
    """Run the Tk event loop for `seconds` so deferred fires actually happen."""
    deadline = time.time() + seconds
    while time.time() < deadline:
        root.update_idletasks()
        root.update()
        time.sleep(0.01)


def _build(hits):
    root = make_root()
    btn = AnimatedButton(root, text="Go", command=lambda: hits.append(1))
    btn.pack()
    root.update()
    return root, btn


def _click(btn):
    x, y = btn.winfo_width() // 2, btn.winfo_height() // 2
    btn.event_generate("<ButtonPress-1>", x=x, y=y)
    btn.event_generate("<ButtonRelease-1>", x=x, y=y)


def test_a_double_click_runs_the_command_once():
    hits = []
    root, btn = _build(hits)
    try:
        _click(btn)
        _click(btn)          # the second release lands inside the 60 ms window
        _pump(root)
        assert hits == [1], "one decision, one run"
    finally:
        root.destroy()


def test_a_single_click_still_runs_the_command():
    """The guard must not swallow the ordinary case."""
    hits = []
    root, btn = _build(hits)
    try:
        _click(btn)
        _pump(root)
        assert hits == [1]
    finally:
        root.destroy()


def test_a_second_click_after_the_first_finished_runs_again():
    """Two deliberate clicks are two commands: the pending-fire slot is free
    again once the first fire has run, so the button cannot wedge."""
    hits = []
    root, btn = _build(hits)
    try:
        _click(btn)
        _pump(root, 0.2)
        _click(btn)
        _pump(root, 0.2)
        assert hits == [1, 1]
    finally:
        root.destroy()


def test_a_disabled_button_schedules_nothing():
    hits = []
    root, btn = _build(hits)
    try:
        btn.set_enabled(False)
        _click(btn)
        _pump(root, 0.2)
        assert hits == []
        assert btn._pending_fire is None, "nothing was queued"
    finally:
        root.destroy()


def test_a_key_repeat_fires_once():
    """Held Enter auto-repeats <Return>; the repeats arrive inside the same
    60 ms window and must not queue more than one run."""
    hits = []
    root, btn = _build(hits)
    try:
        btn.focus_force()
        root.update()
        btn.event_generate("<Return>")
        btn.event_generate("<Return>")
        _pump(root)
        assert hits == [1]
    finally:
        root.destroy()


def test_the_pending_fire_is_cleared_when_it_runs():
    hits = []
    root, btn = _build(hits)
    try:
        _click(btn)
        assert btn._pending_fire is not None, "queued, not yet fired"
        _pump(root, 0.2)
        assert btn._pending_fire is None, "the slot is free again"
    finally:
        root.destroy()
