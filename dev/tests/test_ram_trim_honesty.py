"""A-002: the RAM row must not promise a speed-up the app does not stand behind.

"Force Memory Trim" calls EmptyWorkingSet on every process, which pages
everything out and makes the next click slower; Windows already reclaims
memory under pressure. The task stays (it is opt-in, harmless and sometimes
wanted), but it must never be part of a preset the user did not tick - the
Quick Clean preset that the tray and the one-click paths run above all.

These tests pin the two halves that could drift apart: the copy, and the
selection policy. The policy paragraph that used to live in
`app/smartcare.py` is gone with that module (it had no caller); what survives
is the rule it stated, enforced here against the presets that actually run.
"""
from __future__ import annotations

from app.tab_presets import PRESETS
from app.tasks import clean_tasks


def _ram_task():
    return next(t for t in clean_tasks.TASKS if t.key == "ram_purge")


def test_the_row_does_not_advertise_a_speed_up():
    task = _ram_task()
    assert "Free Up RAM" not in task.label
    # The description is the row's tooltip, so it is where the tradeoff has
    # to live: this is the one place a user reads what the task does.
    text = (task.label + " " + task.description).lower()
    assert "windows already" in text
    assert "rarely" in text


def test_no_preset_selects_it():
    """A preset is one click for a whole tier, and two of them run without a
    user watching (`Quick Clean` from the tray, and the schedule's own run).
    Choosing to page out every working set has to be an explicit Custom tick,
    so no tier may name this key."""
    for tier, keys in PRESETS.get("Clean", {}).items():
        assert "ram_purge" not in keys, tier


def test_it_is_still_reachable_by_hand():
    """The policy is "opt-in", not "removed": a user who wants it must still
    find it in the Custom grid, which is the only place it is allowed."""
    from app.tab_presets import CUSTOM_GROUPS

    listed = {key for _group, keys in CUSTOM_GROUPS["Clean"] for key in keys}
    assert "ram_purge" in listed
