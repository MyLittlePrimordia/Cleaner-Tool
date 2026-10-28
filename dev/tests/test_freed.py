"""N-010: the number a run reports is one the user can check.

The audit's complaint was narrow and fair: every surface reported the sum of
the file sizes the tasks deleted and called it "space freed". On a run whose
caches a game rebuilt in the same minute, that number is not what Explorer
shows. These tests pin the reconciliation and, more importantly, the SILENCE:
a card gains no sentence when the two readings agree, and a run that removed
nothing gains no byte story at all.
"""
from __future__ import annotations

import pytest

from app import freed

MB = 1024 ** 2
GB = 1024 ** 3
TOTAL = 500 * GB
ROOT = "C:\\"


def _maps(before_free, after_free):
    return ({ROOT: (before_free, TOTAL)}, {ROOT: (after_free, TOTAL)})


# --------------------------------------------------------------------------- #
# gains
# --------------------------------------------------------------------------- #

def test_gains_are_the_drives_that_actually_grew_biggest_first():
    before = {"C:\\": (10 * GB, TOTAL), "D:\\": (20 * GB, TOTAL),
              "E:\\": (5 * GB, TOTAL)}
    after = {"C:\\": (12 * GB, TOTAL), "D:\\": (21 * GB, TOTAL),
             "E:\\": (1 * GB, TOTAL)}     # E: lost space
    assert freed.gains(before, after) == [("C:\\", 2 * GB), ("D:\\", 1 * GB)]


def test_a_drive_missing_from_either_snapshot_is_not_invented():
    before = {"C:\\": (10 * GB, TOTAL), "Z:\\": (1 * GB, TOTAL)}
    after = {"C:\\": (11 * GB, TOTAL)}
    assert freed.gains(before, after) == [("C:\\", 1 * GB)]


def test_a_drive_that_lost_space_is_left_out_rather_than_subtracted():
    """Someone else filling a drive mid-run must not make an honest clean
    look like it filled the disk."""
    before = {"C:\\": (10 * GB, TOTAL), "D:\\": (30 * GB, TOTAL)}
    after = {"C:\\": (12 * GB, TOTAL), "D:\\": (2 * GB, TOTAL)}
    assert freed.gains(before, after) == [("C:\\", 2 * GB)]


@pytest.mark.parametrize("junk", [None, {}, "nonsense", 5, [1, 2]])
def test_gains_survive_junk(junk):
    assert freed.gains(junk, junk) == []
    assert freed.gains({ROOT: (1, 2)}, junk) == []


def test_the_scorecards_gain_helper_delegates_to_one_implementation():
    """Two definitions of "a drive gained space" would drift; there is one."""
    from app.ui.hardware.drives import _drive_gains

    before, after = _maps(10 * GB, 11 * GB)
    assert _drive_gains(before, after) == freed.gains(before, after)


# --------------------------------------------------------------------------- #
# unmeasured: the old behaviour, labelled honestly
# --------------------------------------------------------------------------- #

def test_no_snapshots_means_unmeasured_not_zero():
    acc = freed.account(4 * GB)
    assert acc.measured is None
    assert acc.verified is False
    assert acc.verdict == "unmeasured"
    # the run is still credited with what it removed - those bytes did leave
    assert acc.credited == 4 * GB
    assert "not a verified figure" in acc.note()


def test_empty_snapshots_are_not_a_measurement_of_zero():
    """The worker hands over {} when the drive query failed, not "nothing
    gained" - and a zero would read as a failed clean."""
    acc = freed.account(4 * GB, {}, {})
    assert acc.measured is None
    assert acc.verdict == "unmeasured"


def test_unmeasured_runs_add_no_log_line():
    assert freed.account(4 * GB).log_line() == ""


# --------------------------------------------------------------------------- #
# agreement
# --------------------------------------------------------------------------- #

def test_matching_readings_need_no_explanation():
    before, after = _maps(10 * GB, 13 * GB)
    acc = freed.account(3 * GB, before, after)
    assert acc.verdict == "verified"
    assert acc.credited == 3 * GB
    assert acc.note() == ""
    assert "matching" in acc.log_line()


def test_ordinary_noise_is_not_called_a_shortfall():
    """Free space moves by megabytes on an idle PC, and a walk's byte total
    is logical size (compressed and deduped files hold less). Neither is a
    finding."""
    before, after = _maps(10 * GB, 10 * GB + 3 * GB - 40 * MB)
    acc = freed.account(3 * GB, before, after)
    assert acc.verdict == "verified"
    assert acc.note() == ""


def test_a_small_run_is_not_called_short_over_a_couple_of_megabytes():
    before, after = _maps(10 * GB, 10 * GB + 100 * MB - (6 * MB))
    acc = freed.account(100 * MB, before, after)
    assert acc.verdict == "verified"


# --------------------------------------------------------------------------- #
# shortfall
# --------------------------------------------------------------------------- #

def test_a_material_shortfall_is_credited_at_what_the_disk_gained():
    before, after = _maps(10 * GB, 10 * GB + 400 * MB)
    acc = freed.account(3 * GB, before, after)
    assert acc.verdict == "short"
    assert acc.credited == 400 * MB
    assert acc.shortfall == 3 * GB - 400 * MB
    note = acc.note()
    assert "less than the" in note
    assert "recreated by running apps" in note


def test_the_shortfall_names_quarantine_and_locked_files_as_causes():
    before, after = _maps(10 * GB, 10 * GB + 400 * MB)
    acc = freed.account(3 * GB, before, after,
                        moved_bytes=600 * MB, locked_files=17)
    note = acc.note()
    assert "Quarantine" in note
    assert "17 file(s) were locked" in note


def test_a_shortfall_with_no_known_cause_still_says_something_true():
    before, after = _maps(10 * GB, 10 * GB + 400 * MB)
    note = freed.account(3 * GB, before, after).note()
    assert "Quarantine" not in note and "locked" not in note
    assert "recreated" in note or "held open" in note


def test_quarantine_and_locked_files_add_no_copy_when_the_run_verified():
    before, after = _maps(10 * GB, 13 * GB)
    acc = freed.account(3 * GB, before, after, moved_bytes=10 * MB,
                        locked_files=3)
    assert acc.note() == ""


def test_nothing_gained_is_a_zero_hero_not_a_negative_one():
    before, after = _maps(10 * GB, 10 * GB)
    acc = freed.account(2 * GB, before, after)
    assert acc.measured == 0
    assert acc.credited == 0
    assert acc.verdict == "short"
    assert acc.note()


# --------------------------------------------------------------------------- #
# over
# --------------------------------------------------------------------------- #

def test_a_run_is_never_credited_with_someone_elses_gain():
    """A game updating itself while the clean runs must not be counted."""
    before, after = _maps(10 * GB, 10 * GB + 6 * GB)
    acc = freed.account(1 * GB, before, after)
    assert acc.verdict == "over"
    assert acc.credited == 1 * GB
    assert acc.excess == 5 * GB
    assert "Other programs" in acc.note()
    assert "other programs" in acc.log_line()


# --------------------------------------------------------------------------- #
# runs with no byte story
# --------------------------------------------------------------------------- #

def test_a_tweak_run_with_no_bytes_says_nothing_at_all():
    before, after = _maps(10 * GB, 10 * GB + 2 * GB)
    acc = freed.account(0, before, after)
    assert acc.note() == ""
    assert acc.log_line() == ""
    assert acc.credited == 0


@pytest.mark.parametrize("counted", ["junk", None, -5, float("inf")])
def test_junk_totals_never_raise(counted):
    acc = freed.account(counted)
    assert acc.counted >= 0
    assert acc.credited >= 0


def test_material_gap_has_an_absolute_floor_and_a_share():
    assert freed.material_gap(0) == freed.MIN_GAP
    assert freed.material_gap(100 * MB) == freed.MIN_GAP
    assert freed.material_gap(10 * GB) == int(10 * GB * freed.GAP_SHARE)


# --------------------------------------------------------------------------- #
# the run worker: one reconciled number for every surface
# --------------------------------------------------------------------------- #

def _fake_app(monkeypatch, *, removed, before_free, after_free, logs, cards,
              records, snapshots=None, key="junk", tab="Clean"):
    """Drive a real `_run_tasks_worker` with fake drive readings.

    `snapshots` overrides the two before/after maps the worker takes; the
    default is one drive that gained `after_free - before_free`. `key` and
    `tab` name the one task and the page it was run from.
    """
    from types import SimpleNamespace

    import app.config_persist as cp
    import app.ui.runflow as runflowmod
    from app.ui.app import Application

    snaps = iter(snapshots or [{ROOT: (before_free, TOTAL)},
                               {ROOT: (after_free, TOTAL)}])
    monkeypatch.setattr(runflowmod, "_snapshot_all_drives",
                        lambda *_a, **_k: next(snaps), raising=True)
    monkeypatch.setattr(runflowmod, "_themed_showinfo",
                        lambda *_a, **_k: None, raising=True)
    monkeypatch.setattr(runflowmod, "_system_drive_root",
                        lambda: ROOT, raising=True)

    class _Card:
        def __init__(self, _parent, **kwargs):
            cards.append(kwargs)

        def wait(self):
            pass

    monkeypatch.setattr(runflowmod, "ScorecardDialog", _Card, raising=True)
    monkeypatch.setattr(cp, "record_run",
                        lambda b, **kw: records.append((b, kw)), raising=True)

    app = object.__new__(Application)
    app.root = None
    app.log = logs.append
    app.set_status = lambda _s: None
    app._cancel_requested = False
    app._query_drive_free_total = lambda _root: None
    app._query_drives = lambda: []
    app._set_progress = lambda *_a: None
    app._stop_indeterminate = lambda: None
    app._progress_hide = lambda: None
    app.cancel_btn = SimpleNamespace(set_enabled=lambda _v: None)
    app.progress_bar = SimpleNamespace(set_indeterminate=lambda _v: None,
                                       set_fraction=lambda _v: None)
    app._release_run = lambda *_a, **_k: None
    app._set_buttons_enabled = lambda _v: None
    app._is_current = lambda _gen: True
    app._dispatch = SimpleNamespace(post=lambda fn: fn())
    app._maybe_show_automaint_tip = lambda: None
    app.tabs = {}

    def _invoke(_ctx, _task, _mode):
        return "ok", removed, None

    app._invoke_single_task = _invoke
    app._run_tasks_worker(
        tab, [SimpleNamespace(label="Junk", risk="", key=key,
                              revert=None)],
        "run", gen=1, run_results=[])
    return app


def test_the_ledger_and_the_card_get_the_measured_number(monkeypatch):
    logs, cards, records = [], [], []
    _fake_app(monkeypatch, removed=3 * GB, before_free=10 * GB,
              after_free=10 * GB + 400 * MB, logs=logs, cards=cards,
              records=records)

    assert records, "the run was recorded"
    assert records[0][0] == 400 * MB, "credited, not the 3 GB removed"
    assert cards[0]["total_bytes"] == 400 * MB
    assert cards[0]["accounted"].credited == 400 * MB
    assert cards[0]["accounted"].verdict == "short"
    # and the transcript says why, in the block a bug report is read from
    assert any("Free space check" in line and "less than" in line
               for line in logs), logs


def test_a_matching_run_records_what_it_removed(monkeypatch):
    logs, cards, records = [], [], []
    _fake_app(monkeypatch, removed=3 * GB, before_free=10 * GB,
              after_free=13 * GB, logs=logs, cards=cards, records=records)

    assert records[0][0] == 3 * GB
    assert cards[0]["accounted"].verdict == "verified"
    assert any("matching the" in line for line in logs), logs


def test_a_clean_that_removed_bytes_takes_the_junk_reading_down(monkeypatch):
    """A run that spent a measured estimate is the one that invalidates it.

    The published junk finding quotes what the cleaners WOULD free, so once
    a Clean run has removed bytes from one of them the board is holding a
    pre-run reading: Health and the hub card would be pointing at a fix that
    just happened. This rule used to live in `app/smartcare.py`, which nothing
    in the app called; it belongs on the run that removed the bytes.
    """
    from app import insights

    retracted = []
    monkeypatch.setattr(insights, "retract",
                        lambda *ids: retracted.extend(ids) or 1)
    logs, cards, records = [], [], []
    _fake_app(monkeypatch, removed=3 * GB, before_free=10 * GB,
              after_free=13 * GB, logs=logs, cards=cards, records=records,
              key="user_temp_files")

    assert retracted == ["storage.junk"]


def test_a_clean_that_removed_nothing_leaves_the_estimate_standing(monkeypatch):
    """Everything may have been locked: a run that removed nothing has not
    contradicted the estimate, so clearing it would throw away a live reading
    (Storage republishes on its next walk either way)."""
    from app import insights

    retracted = []
    monkeypatch.setattr(insights, "retract",
                        lambda *ids: retracted.extend(ids) or 1)
    logs, cards, records = [], [], []
    _fake_app(monkeypatch, removed=0, before_free=10 * GB, after_free=10 * GB,
              logs=logs, cards=cards, records=records, key="user_temp_files")

    assert retracted == []


def test_another_pages_run_never_touches_the_junk_estimate(monkeypatch):
    """A Tweak or Repair run frees space as a side effect at most; the disk
    reading belongs to the Clean tasks the estimate was built from."""
    from app import insights

    retracted = []
    monkeypatch.setattr(insights, "retract",
                        lambda *ids: retracted.extend(ids) or 1)
    logs, cards, records = [], [], []
    _fake_app(monkeypatch, removed=3 * GB, before_free=10 * GB,
              after_free=13 * GB, logs=logs, cards=cards, records=records,
              key="user_temp_files", tab="Tweak")

    assert retracted == []


def test_an_unreadable_run_still_records_the_removed_total(monkeypatch):
    """A failed drive query must not turn a real clean into a 0-byte run."""
    logs, cards, records = [], [], []
    _fake_app(monkeypatch, removed=3 * GB, before_free=0, after_free=0,
              logs=logs, cards=cards, records=records,
              snapshots=[{}, {}])
    assert records[0][0] == 3 * GB
    assert cards[0]["accounted"].verdict == "unmeasured"
    assert "not a verified figure" in cards[0]["accounted"].note()
    assert not any("Free space check" in line for line in logs), logs


# --------------------------------------------------------------------------- #
# the transcript block
# --------------------------------------------------------------------------- #

def _summary(accounted, reclaimed):
    from app.ui.app import _run_summary_lines

    return _run_summary_lines(
        "Clean", "run", False, None, 2, 0, 0, 2,
        reclaimed, 0, False, accounted=accounted)


def test_the_transcript_states_the_measured_number_next_to_the_removed_one():
    before, after = _maps(10 * GB, 10 * GB + 400 * MB)
    acc = freed.account(3 * GB, before, after)
    lines = _summary(acc, 3 * GB)
    # the same credited number the card and the ledger carry
    assert any(line == "Disk space freed: 400.00 MB" for line in lines), lines
    check = next(line for line in lines if line.startswith("Free space check"))
    assert "400.00 MB" in check and "less than" in check


def test_an_unmeasured_run_adds_no_check_line():
    acc = freed.account(3 * GB)
    assert not any("Free space check" in line for line in _summary(acc, 3 * GB))


def test_a_tweak_run_adds_no_byte_story():
    before, after = _maps(10 * GB, 12 * GB)
    acc = freed.account(0, before, after)
    assert not any("Free space check" in line for line in _summary(acc, 0))


# --------------------------------------------------------------------------- #
# the card
# --------------------------------------------------------------------------- #

def _card(accounted=None, total_bytes=3 * GB, **kwargs):
    from _tkhelpers import make_root
    from app.ui.dialogs.scorecard_dialog import ScorecardDialog

    root = make_root()
    root.withdraw()
    card = ScorecardDialog(
        root, tab_name="Clean", mode="run", cancelled=False,
        results=[("Temp files", "ok", 1024, None)],
        total_bytes=total_bytes, accounted=accounted, **kwargs)
    return root, card


def test_the_card_states_the_shortfall_and_puts_it_in_the_copy():
    before, after = _maps(10 * GB, 10 * GB + 400 * MB)
    acc = freed.account(3 * GB, before, after)
    root, card = _card(acc, total_bytes=acc.credited)
    try:
        assert card._verified_lbl is not None
        shown = card._verified_lbl.cget("text")
        assert "less than the" in shown
        assert shown in card._summary_text()
    finally:
        card.close()
        root.destroy()


def test_a_verified_run_gets_no_extra_sentence():
    before, after = _maps(10 * GB, 13 * GB)
    acc = freed.account(3 * GB, before, after)
    root, card = _card(acc, total_bytes=acc.credited)
    try:
        assert card._verified_lbl is None
        assert "Free space" not in card._summary_text()
    finally:
        card.close()
        root.destroy()


def test_a_caller_without_accounting_builds_and_says_nothing_extra():
    """Smoke fixtures and older call sites pass no `accounted`."""
    root, card = _card(None)
    try:
        assert card._verified_lbl is None
    finally:
        card.close()
        root.destroy()


def test_the_card_can_explain_a_run_that_gained_nothing():
    """Credited is 0, so there is no hero - the sentence is the only thing
    telling the user what happened to the 2 GB the run removed."""
    before, after = _maps(10 * GB, 10 * GB)
    acc = freed.account(2 * GB, before, after)
    root, card = _card(acc, total_bytes=acc.credited)
    try:
        assert card._hero_lbl is None
        assert card._verified_lbl is not None
        assert "less than the" in card._verified_lbl.cget("text")
    finally:
        card.close()
        root.destroy()
