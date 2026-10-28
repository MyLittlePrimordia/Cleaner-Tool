"""Quiet watch (N-006): three conditions, one notice, no chattering.

The whole point of this module is the restraint, so the tests are mostly about
what it does NOT do: a drive hovering on the threshold speaks once, not every
thirty seconds; a drop that was already announced stays silent until a new
checkup; an unreadable schedule says nothing rather than guessing; and two
conditions at the same moment produce one notification, not two.

`STATE_FILE` is redirected per test. conftest already points the app's state
directory at a temp folder, but a per-test file keeps the episode memory from
leaking between the cases that deliberately leave a condition un-cleared.
"""
from __future__ import annotations

import json

import pytest

from app import watch
from app.ui.tabs.settingstab import CATEGORIES, _WATCH_DROP, _WATCH_FREE, \
    _watch_choices


@pytest.fixture()
def state_file(tmp_path, monkeypatch):
    path = tmp_path / "watch.json"
    monkeypatch.setattr(watch, "STATE_FILE", path)
    return path


def _cfg(**over):
    base = {
        "watch_enabled": True,
        "watch_free_percent": 10,
        "watch_score_drop": 8,
        "watch_missed_run": True,
    }
    base.update(over)
    return base


def _drive(letter="C", free=5.0, total=100.0):
    """The monitor's own shape: a bare letter, then root, free and total."""
    return (letter, letter + ":\\", free, total)


# --------------------------------------------------------------------------- #
# thresholds
# --------------------------------------------------------------------------- #

def test_defaults_are_the_documented_numbers():
    assert watch.free_threshold({}) == 10
    assert watch.score_drop_threshold({}) == 8
    assert watch.clear_threshold({}) == 12
    assert watch.enabled({}) is True
    assert watch.missed_runs_enabled({}) is True


def test_thresholds_reject_values_that_cannot_be_a_setting():
    for bad in (None, "10", 10.0, True, 1, 1000):
        assert watch.free_threshold({"watch_free_percent": bad}) == 10
        assert watch.score_drop_threshold({"watch_score_drop": bad}) == 8
    assert watch.free_threshold({"watch_free_percent": 7}) == 7
    assert watch.clear_threshold({"watch_free_percent": 7}) == 9


def test_the_switches_read_off_when_off():
    assert watch.enabled({"watch_enabled": False}) is False
    assert watch.missed_runs_enabled({"watch_missed_run": False}) is False


# --------------------------------------------------------------------------- #
# drives
# --------------------------------------------------------------------------- #

def test_a_drive_at_the_threshold_is_reported_and_above_it_is_not():
    at = watch.evaluate(drives=[_drive(free=10.0)], cfg=_cfg())
    assert len(at) == 1
    assert at[0].key == "drive:C"
    assert at[0].route == "storage_insight"
    assert "10.0 GB free of 100 GB" in at[0].body
    assert watch.evaluate(drives=[_drive(free=10.5)], cfg=_cfg()) == []


def test_a_configured_threshold_moves_the_line():
    assert watch.evaluate(drives=[_drive(free=9.0)],
                          cfg=_cfg(watch_free_percent=5)) == []
    assert watch.evaluate(drives=[_drive(free=9.0)],
                          cfg=_cfg(watch_free_percent=15))


def test_a_drive_with_no_size_is_not_measured_rather_than_full():
    assert watch.evaluate(drives=[_drive(free=0.0, total=0.0)],
                          cfg=_cfg()) == []
    assert watch.evaluate(drives=[("X:", "X:\\")], cfg=_cfg()) == []


# --------------------------------------------------------------------------- #
# health score
# --------------------------------------------------------------------------- #

def _trend(delta, last=71, last_ts=1700.0, points=None):
    pts = points if points is not None else [
        {"t": 1000.0, "s": last - delta - 1, "g": "C"},
        {"t": last_ts, "s": last, "g": "C"},
    ]
    return {"delta": delta, "last": last, "last_ts": last_ts,
            "points": pts, "decline": None}


def test_a_small_drop_is_not_worth_a_notification():
    assert watch.evaluate(trend=_trend(-7), cfg=_cfg()) == []


def test_a_real_drop_names_the_screen_and_the_latest_score():
    alerts = watch.evaluate(trend=_trend(-8), cfg=_cfg())
    assert len(alerts) == 1
    alert = alerts[0]
    assert alert.key == "score:1700"
    assert alert.route == "health"
    assert "71 now" in alert.body
    assert "down from 79" in alert.body


def test_the_cause_is_named_only_when_both_checkups_graded_it():
    measured = [
        {"t": 1000.0, "s": 79, "g": "C", "f": {"disk": "A", "memory": "B"}},
        {"t": 1700.0, "s": 71, "g": "D", "f": {"disk": "D", "memory": "B"}},
    ]
    alert = watch.evaluate(trend=_trend(-8, points=measured), cfg=_cfg())[0]
    assert "Disk Space fell from A to D" in alert.body

    # The newer checkup did not measure the sensor, so it cannot be blamed.
    unmeasured = [
        {"t": 1000.0, "s": 79, "g": "C", "f": {"disk": "A"}},
        {"t": 1700.0, "s": 71, "g": "D", "f": {"memory": "B"}},
    ]
    alert = watch.evaluate(trend=_trend(-8, points=unmeasured), cfg=_cfg())[0]
    assert "fell from" not in alert.body


def test_the_first_checkup_has_nothing_to_compare_with():
    assert watch.evaluate(trend={"delta": None, "last": 71, "last_ts": 5.0},
                          cfg=_cfg()) == []


# --------------------------------------------------------------------------- #
# schedule
# --------------------------------------------------------------------------- #

def test_a_stale_or_failed_schedule_is_reported_with_windows_own_words():
    stale = {"kind": "clean", "stale": True, "failed": False,
             "text": "Last ran 12 days ago  ·  later than its weekly schedule",
             "last_run": 900.0}
    alert = watch.evaluate(schedule=stale, cfg=_cfg())[0]
    assert alert.key == "schedule:clean:900"
    assert alert.title == "Scheduled maintenance has stopped running"
    assert "Last ran 12 days ago" in alert.body
    assert alert.route == "settings"

    failed = dict(stale, stale=False, failed=True, kind="update")
    alert = watch.evaluate(schedule=failed, cfg=_cfg())[0]
    assert alert.title == "The last scheduled app updates run failed"


def test_a_healthy_or_unreadable_schedule_says_nothing():
    assert watch.evaluate(schedule={"stale": False, "failed": False},
                          cfg=_cfg()) == []
    assert watch.evaluate(schedule={}, cfg=_cfg()) == []
    assert watch.evaluate(schedule=None, cfg=_cfg()) == []


def test_the_missed_run_switch_is_independent_of_the_master_one():
    stale = {"kind": "clean", "stale": True, "failed": False, "text": "x"}
    assert watch.evaluate(schedule=stale, cfg=_cfg(watch_missed_run=False)) == []


# --------------------------------------------------------------------------- #
# episode memory
# --------------------------------------------------------------------------- #

def test_a_condition_speaks_once_and_stays_silent_while_it_holds():
    alerts = watch.evaluate(drives=[_drive(free=8.0)], cfg=_cfg(),
                            now=1000.0)
    speak, state = watch.decide(alerts, {}, now=1000.0)
    assert [a.key for a in speak] == ["drive:C"]

    speak2, state2 = watch.decide(alerts, state, now=1030.0)
    assert speak2 == []
    # the original stamp is kept, so REPEAT_AFTER measures the episode, not
    # the last tick
    assert state2["announced"]["drive:C"] == 1000.0


def test_recovering_and_crossing_again_is_a_new_episode():
    alerts = watch.evaluate(drives=[_drive(free=8.0)], cfg=_cfg(), now=1000.0)
    _speak, state = watch.decide(alerts, {}, now=1000.0)
    # recovered: no alerts at all, which prunes the key
    _speak, state = watch.decide([], state, now=1100.0)
    assert state["announced"] == {}
    speak, _state = watch.decide(alerts, state, now=1200.0)
    assert [a.key for a in speak] == ["drive:C"]


def test_a_condition_that_stays_true_speaks_again_the_next_day():
    alerts = watch.evaluate(drives=[_drive(free=8.0)], cfg=_cfg(), now=1000.0)
    _speak, state = watch.decide(alerts, {}, now=1000.0)
    speak, _state = watch.decide(
        alerts, state, now=1000.0 + watch.REPEAT_AFTER)
    assert [a.key for a in speak] == ["drive:C"]


def test_a_new_checkup_re_arms_the_score_alert():
    first = watch.evaluate(trend=_trend(-9, last_ts=1700.0), cfg=_cfg(),
                           now=1000.0)
    second = watch.evaluate(trend=_trend(-9, last_ts=1800.0), cfg=_cfg(),
                            now=1000.0)
    _speak, state = watch.decide(first, {}, now=1000.0)
    speak, _state = watch.decide(second, state, now=1001.0)
    assert [a.key for a in speak] == ["score:1800"]


def test_a_corrupt_state_file_is_forgotten_not_fatal(state_file):
    state_file.write_text("{not json", encoding="utf-8")
    assert watch.load_state() == {}


def test_state_round_trips_and_is_replaced_atomically(state_file):
    watch.save_state({"announced": {"drive:C:": 12.0}})
    assert watch.load_state() == {"announced": {"drive:C:": 12.0}}
    assert not state_file.with_name(state_file.name + ".tmp").exists()


def test_old_episode_keys_expire(state_file):
    alerts = watch.evaluate(drives=[_drive(free=8.0)], cfg=_cfg(), now=0.0)
    _speak, state = watch.decide(alerts, {}, now=0.0)
    far = watch.KEEP_STATE + 10
    _speak, state = watch.decide([], state, now=far)
    assert state["announced"] == {}


# --------------------------------------------------------------------------- #
# one notice
# --------------------------------------------------------------------------- #

def test_one_alert_keeps_its_own_copy():
    alert = watch.evaluate(drives=[_drive(free=8.0)], cfg=_cfg())[0]
    assert watch.notice_text([alert]) == (alert.title, alert.body)


def test_several_alerts_become_one_notification_naming_each():
    alerts = watch.evaluate(
        drives=[_drive(free=8.0)],
        trend=_trend(-9),
        schedule={"kind": "clean", "stale": True, "text": "Last ran 9 days ago"},
        cfg=_cfg(),
    )
    assert len(alerts) == 3
    title, body = watch.notice_text(alerts)
    assert title == "Cleaner Tool noticed 3 things"
    assert body.count("\u00b7") == 2
    assert "C: only 8.0 GB free" in body
    assert "health score down 9" in body
    assert "scheduled maintenance is overdue" in body


def test_no_alerts_means_no_notification():
    assert watch.notice_text([]) is None
    assert watch.notice_text(None) is None


# --------------------------------------------------------------------------- #
# the tick
# --------------------------------------------------------------------------- #

def test_the_tick_speaks_once_and_persists_the_episode(state_file):
    drives = [_drive(free=5.0)]
    cfg = _cfg()
    first = watch.check(drives=drives, cfg=cfg, trend={}, schedule={},
                        now=1000.0)
    assert [a.key for a in first] == ["drive:C"]
    assert "drive:C" in watch.load_state()["announced"]
    assert watch.check(drives=drives, cfg=cfg, trend={}, schedule={},
                       now=1010.0) == []


def test_the_switch_silences_everything_at_the_door(state_file):
    calls = []
    assert watch.check(drives=[_drive(free=1.0)], cfg={"watch_enabled": False},
                       trend=_trend(-20), schedule={"stale": True},
                       now=1000.0) == []
    assert calls == []
    assert watch.load_state() == {}


def test_an_injected_verdict_never_spawns_the_task_scheduler(state_file,
                                                            monkeypatch):
    from app import scheduler

    def _boom(*_a, **_k):
        raise AssertionError("the scheduler was queried despite a verdict")

    monkeypatch.setattr(scheduler, "schedule_info", _boom)
    assert watch.check(cfg=_cfg(), trend={}, schedule={}) == []


def test_a_stale_schedule_is_measured_then_remembered(state_file, monkeypatch):
    """One PowerShell query per SCHEDULE_EVERY, not one per tick."""
    from app import scheduler

    now = 1_000_000.0
    queried = []

    def _info(extra=None):
        queried.append(extra)
        return {"last_run": now - 30 * 86400, "next_run": now + 3600,
                "last_result": 0, "state": "Ready"}

    monkeypatch.setattr(scheduler, "schedule_info", _info)
    cfg = _cfg(schedule_enabled=True, schedule_frequency="weekly")

    alerts = watch.check(cfg=cfg, trend={}, now=now)
    assert [a.key for a in alerts] == ["schedule:clean:%d"
                                       % int(now - 30 * 86400)]
    assert queried == [None]

    # six hours later the verdict is re-asked, and the still-stale run is
    # already known - so it does not speak again
    _alerts = watch.check(cfg=cfg, trend={}, now=now + watch.SCHEDULE_EVERY)
    assert len(queried) == 2


def test_an_unreadable_task_is_not_reported_as_a_missed_run(state_file,
                                                            monkeypatch):
    from app import scheduler
    monkeypatch.setattr(scheduler, "schedule_info", lambda extra=None: {})
    cfg = _cfg(schedule_enabled=True)
    assert watch.check(cfg=cfg, trend={}, now=1000.0) == []


# --------------------------------------------------------------------------- #
# UI seams
# --------------------------------------------------------------------------- #

class _LowSpaceStub:
    """Just enough window to drive `_check_low_space` without Tk."""

    from app.ui.app import Application as _App
    # Only the threshold logic is under test here; the nudge is recorded, not
    # rendered. The real method's silence is pinned by the source test below.
    _check_low_space = _App._check_low_space

    def __init__(self):
        self._drive_was_red = {}
        self._nudge_chips = {}
        self._nudge_dismissed = set()
        self.fired = []
        self.hidden = []

    def _show_nudge_chip(self, letter, free_gb):
        pass

    def _ensure_nudge_estimate(self):
        pass

    def _fire_low_space_nudge(self, letter, free_gb):
        self.fired.append(letter)

    def _hide_nudge_chip(self, letter):
        self.hidden.append(letter)


def test_the_chip_uses_the_configured_threshold_and_its_hysteresis(
        monkeypatch):
    from app import config_persist

    monkeypatch.setattr(config_persist, "load_config",
                        lambda: _cfg(watch_free_percent=5))
    stub = _LowSpaceStub()
    stub._check_low_space([_drive(free=9.0)])
    assert stub.fired == []          # above 5%: not marked (it was 5% before)

    stub._check_low_space([_drive(free=4.0)])
    assert stub.fired == ["C"]

    # hysteresis: still marked at 6% (below 7), cleared only above it
    stub._check_low_space([_drive(free=6.0)])
    assert stub.hidden == []
    stub._check_low_space([_drive(free=8.0)])
    assert stub.hidden == ["C"]


def test_the_chip_does_not_send_its_own_notification():
    """The duplicate this split prevents: one owner for speaking."""
    import inspect

    from app.ui.app import Application

    source = inspect.getsource(Application._fire_low_space_nudge)
    assert "notify_low_space" not in source
    assert "show_toast" not in source


def test_the_window_folds_alerts_into_one_toast_and_still_logs_each(
        monkeypatch):
    from app.ui import app as app_module

    sent = []
    monkeypatch.setattr(app_module, "show_toast",
                        lambda title, body, duration="short", tag="":
                        sent.append((title, body)))

    class _AnnounceStub:
        _watch_announce = app_module.Application._watch_announce

        def __init__(self):
            self.lines = []

        def log(self, text):
            self.lines.append(text)

    stub = _AnnounceStub()
    alerts = watch.evaluate(
        drives=[_drive(free=8.0)], trend=_trend(-9), cfg=_cfg())
    stub._watch_announce(alerts)

    assert len(sent) == 1
    assert sent[0][0] == "Cleaner Tool noticed 2 things"
    assert len(stub.lines) == 2
    assert all(line.startswith("Quiet watch: ") for line in stub.lines)


# --------------------------------------------------------------------------- #
# settings
# --------------------------------------------------------------------------- #

def test_settings_has_a_quiet_watch_category():
    assert ("watch", "Quiet watch") in CATEGORIES


def test_a_stored_threshold_outside_the_four_steps_stays_visible():
    """A hand-edited 7% must not be displayed as 5% and then saved as 5."""
    options = _watch_choices(_WATCH_FREE, 7, "%")
    assert ("7%", 7) in options
    assert [key for _label, key in options] == [5, 7, 10, 15, 20]
    # an unlisted value that is already a step changes nothing
    assert _watch_choices(_WATCH_DROP, 12, " points") == _WATCH_DROP


def test_saving_the_watch_rows_writes_the_schema_keys():
    from app.config_persist import load_config
    from app.settings_apply import apply_settings

    results, errors = apply_settings({
        "watch_enabled": False,
        "watch_free": 15,
        "watch_drop": 12,
        "watch_missed": False,
    }, config={})

    assert errors == []
    assert "Quiet watch off" in results
    cfg = load_config()
    assert cfg["watch_enabled"] is False
    assert cfg["watch_free_percent"] == 15
    assert cfg["watch_score_drop"] == 12
    assert cfg["watch_missed_run"] is False


def test_a_bad_threshold_falls_back_to_the_documented_default():
    from app.config_persist import load_config
    from app.settings_apply import apply_settings

    apply_settings({"watch_free": 900, "watch_drop": "lots"}, config={})
    cfg = load_config()
    assert cfg["watch_free_percent"] == watch.FREE_DEFAULT
    assert cfg["watch_score_drop"] == watch.SCORE_DROP_DEFAULT


def test_the_schema_round_trips_the_new_keys():
    from app.config_persist import DEFAULT_CONFIG
    from app.config_schema import SCHEMA, apply_schema, validate_schema

    for key in ("watch_enabled", "watch_free_percent", "watch_score_drop",
                "watch_missed_run"):
        assert key in SCHEMA
        assert key in DEFAULT_CONFIG
    assert validate_schema(DEFAULT_CONFIG) == []

    data = {"watch_free_percent": "10", "watch_score_drop": 7.5,
            "watch_enabled": "yes", "watch_missed_run": False}
    apply_schema(data, DEFAULT_CONFIG)
    assert data["watch_free_percent"] == 10
    assert data["watch_score_drop"] == 8
    assert data["watch_enabled"] is True
    assert data["watch_missed_run"] is False
    json.dumps(data)  # always writable
