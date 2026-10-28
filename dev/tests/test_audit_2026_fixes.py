"""Regression tests for the 2026-10 audit fixes.

Each test pins the BEHAVIOUR that was wrong, not the implementation:

  B-001  a matching live task must not be rewritten
  B-003  stopping Auto-Pilot mid-session releases the stay-awake hold
  B-004  a crashed run is not reported as "complete"
  B-005  the drive benchmark writes ON the requested drive
  B-006  bytes moved to Quarantine are not reported as reclaimed
  B-008  a failed lazy page does not change the active tab
  B-010  a malformed driver cache entry is a miss, not an exception
  B-009  the update swap script escapes batch metacharacters
  B-011  task matching reads locale-independent XML, not LIST labels
  H-002  a DATA LOSS task asks before running
"""
from __future__ import annotations

import os

import pytest

from dev.tests._tkhelpers import make_root

tk = pytest.importorskip("tkinter")

_TASK_XML = """<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <Actions Context="Author">
    <Exec>
      <Command>C:\\App\\Cleaner.exe</Command>
      <Arguments>--auto-clean</Arguments>
    </Exec>
  </Actions>
  <Triggers><CalendarTrigger><StartBoundary>2026-01-01T03:00:00</StartBoundary>
    <ScheduleByWeek /></CalendarTrigger></Triggers>
</Task>
"""


# --------------------------------------------------------------------------- #
# B-001 / B-011 — scheduler drift check is reachable and locale-independent
# --------------------------------------------------------------------------- #

def _scheduler_probe(monkeypatch, xml, sched_ok=True, tr_args="--auto-clean"):
    import app.scheduler as s

    calls = []
    monkeypatch.setattr(s, "get_schedule_status",
                        lambda extra=None: (True, "irrelevant LIST output"))
    monkeypatch.setattr(s, "_get_executable_and_args",
                        lambda extra=None: (r"C:\App\Cleaner.exe", [tr_args]))
    monkeypatch.setattr(s, "_live_task_xml", lambda _name: xml)
    monkeypatch.setattr(s, "_xml_schedule_matches", lambda *_a: sched_ok)
    monkeypatch.setattr(s, "_build_schtasks_cmd",
                        lambda *a, **k: calls.append("rewrite") or ["schtasks"])
    monkeypatch.setattr(s, "update_config", lambda _m: calls.append("update_config"))
    # If a rewrite is attempted it would reach subprocess.run; fail loudly.
    class _Result:
        returncode = 0
        stdout = "OK"
        stderr = ""

    def _fake_run(*_a, **_k):
        calls.append("schtasks-run")
        return _Result()

    monkeypatch.setattr(s.subprocess, "run", _fake_run)
    result = s.enable_schedule("weekly", "03:00")
    return result, calls


def test_matching_schedule_is_not_rewritten(monkeypatch):
    result, calls = _scheduler_probe(monkeypatch, _TASK_XML)
    assert result == (True, "Schedule already up to date.")
    assert calls == ["update_config"], "a matching task must not be rewritten"


def test_unreadable_xml_rewrites_instead_of_assuming_a_match(monkeypatch):
    # BUG-010's rule: only a POSITIVE confirmation may skip the rewrite.
    result, calls = _scheduler_probe(monkeypatch, None)
    assert result[0] is True
    assert "rewrite" in calls, "None ('cannot tell') must fall through to a rewrite"


def test_schedule_mismatch_rewrites(monkeypatch):
    _, calls = _scheduler_probe(monkeypatch, _TASK_XML, sched_ok=False)
    assert "rewrite" in calls


def test_command_mismatch_rewrites(monkeypatch):
    _, calls = _scheduler_probe(monkeypatch, _TASK_XML, tr_args="--auto-update")
    assert "rewrite" in calls


def test_task_xml_parsing_is_label_independent():
    import app.elevated_launch as el
    import app.scheduler as sch

    for parser in (el.parse_task_xml, sch._xml_exec):
        got = parser(_TASK_XML)
        assert got == (r"C:\App\Cleaner.exe", "--auto-clean")


def test_elevated_launch_status_matches_via_xml(monkeypatch):
    import app.elevated_launch as el

    monkeypatch.setattr(el, "_run", lambda _cmd: (True, _TASK_XML))
    monkeypatch.setattr(el, "current_task_run",
                        lambda: r"C:\App\Cleaner.exe --auto-clean")
    exists, matches, _out = el.status()
    assert exists is True and matches is True

    # A translated LIST body must be irrelevant to the answer.
    monkeypatch.setattr(el, "_run",
                        lambda _cmd: (True, "Aufgabe: C:\\App\\Cleaner.exe"))
    exists, matches, _out = el.status()
    assert exists is True and matches is False


def test_task_names_are_shared_by_create_query_delete():
    import app.scheduler as s

    assert s._task_name_for(None) == s.TASK_NAME
    assert s._task_name_for(["--auto-update"]).endswith("_auto_update")


# --------------------------------------------------------------------------- #
# B-003 — stopping Auto-Pilot releases the stay-awake hold
# --------------------------------------------------------------------------- #

class _FakeSession:
    def __init__(self, state):
        self.state = state
        self.stopped = False

    def stop(self):
        self.stopped = True


def _pilot_app(state):
    from app.ui.app import Application

    app = object.__new__(Application)
    app._pilot_sess = _FakeSession(state)
    app.log = lambda _msg: None
    calls = []
    app._set_stay_awake = lambda on: calls.append(on)
    return app, calls


def test_pilot_stop_releases_stay_awake_when_a_session_is_active():
    from app.ui.session import SessionState

    app, calls = _pilot_app(SessionState.ACTIVE)
    app._pilot_stop()
    assert app._pilot_sess.stopped is True
    assert calls == [False], "a live session's wake hold must be released on stop"


def test_pilot_stop_does_not_touch_stay_awake_when_idle():
    from app.ui.session import SessionState

    app, calls = _pilot_app(SessionState.WATCHING)
    app._pilot_stop()
    assert calls == []


# --------------------------------------------------------------------------- #
# B-004 — a crashed run is not "complete"
# --------------------------------------------------------------------------- #

def test_run_outcome_word_distinguishes_crash_from_completion():
    from app.ui.app import _run_outcome_word

    assert _run_outcome_word(False, False) == "complete"
    assert _run_outcome_word(False, True) == "ended early"
    assert _run_outcome_word(True, False) == "stopped early"
    assert _run_outcome_word(True, True) == "stopped early"


# --------------------------------------------------------------------------- #
# B-005 — benchmark writes on the drive it was asked about
# --------------------------------------------------------------------------- #

def test_benchmark_uses_the_requested_drive(monkeypatch, tmp_path):
    import tempfile as real_tempfile

    from app import storage_scan

    # Pretend %TEMP% is on another volume: the old code then looked for
    # <drive>:\Windows\Temp and gave up when it did not exist.
    monkeypatch.setattr(real_tempfile, "gettempdir",
                        lambda: "Z:\\definitely-not-here")
    write_mbps, read_mbps = storage_scan.write_benchmark(
        str(tmp_path) + os.sep, size_mb=1)
    assert write_mbps and write_mbps > 0
    assert read_mbps and read_mbps > 0
    leftovers = [p for p in os.listdir(tmp_path) if p.startswith("cleaner_bench_")]
    assert leftovers == [], "the benchmark must remove its temp file"


# --------------------------------------------------------------------------- #
# B-006 — quarantined bytes are not "freed"
# --------------------------------------------------------------------------- #

def test_split_reclaimed_subtracts_moved_bytes():
    from app.quarantine import split_reclaimed

    assert split_reclaimed(1000, 400) == (600, 400)
    assert split_reclaimed(1000, 0) == (1000, 0)
    assert split_reclaimed(0, 500) == (0, 0)
    assert split_reclaimed("bad", None) == (0, 0)


# --------------------------------------------------------------------------- #
# B-008 — a failed lazy page leaves the active tab alone
# --------------------------------------------------------------------------- #

def test_failed_page_does_not_change_active_tab():
    from app.ui.app import Application

    root = make_root()
    try:
        root.withdraw()
        app = object.__new__(Application)
        app.root = root
        page = tk.Frame(root)
        app.tabs = {"Clean": page}
        app.active_tab = "Clean"
        app._tab_factories = {
            "Repair": lambda: (_ for _ in ()).throw(RuntimeError("boom"))}
        app._tab_icons = {}
        app._rail_t = [0.0] * 7
        app._switch_anim = None
        app._switch_anim_after = None
        app._hover_seg = None
        app._hover_t = 0.0
        app.log = lambda _m: None
        app.set_status = lambda _m: None
        app._paint_pills = lambda: None
        app._update_tab_icon = lambda: None
        app._style_switch_labels = lambda: None
        app._tick_switch = lambda: None
        app._wheel_bound = True

        app._switch_to("Repair")
        assert app.active_tab == "Clean"
        assert app.tabs.get("Repair") is None
    finally:
        root.destroy()


# --------------------------------------------------------------------------- #
# B-010 — a corrupt driver cache entry is a miss
# --------------------------------------------------------------------------- #

def test_corrupt_cache_entry_is_a_miss_still_valid_entries_work():
    from app.drivers.scan import Cache

    cache = Cache(None, now=lambda: 1000.0)
    cache.data = {
        "bad-str": "nope",
        "bad-shape": [1],
        "bad-stamp": ["yesterday", []],
        "bad-results": [999.0, "not-a-list"],
        "fresh": [990.0, [{"id": "x"}]],
        # older than CACHE_TTL_S (24h) relative to now=1000.0
        "stale": [-90000.0, [{"id": "y"}]],
    }
    assert cache.get("bad-str") is None
    assert cache.get("bad-shape") is None
    assert cache.get("bad-stamp") is None
    assert cache.get("bad-results") is None
    assert cache.get("stale") is None
    assert cache.get("missing") is None
    assert cache.get("fresh") == [{"id": "x"}]


# --------------------------------------------------------------------------- #
# B-009 — the swap script escapes batch metacharacters
# --------------------------------------------------------------------------- #

def test_swap_script_escapes_percent_and_caret(tmp_path):
    from app.self_update import build_swap_script

    script = build_swap_script(
        r"C:\100% Games\Cleaner^Tool\Cleaner-Tool.exe",
        r"C:\Users\me\AppData\Local\CleanerTool\update\Cleaner-Tool.exe")
    assert "100%% Games" in script
    assert "Cleaner^^Tool" in script
    assert 'set "OLD=C:\\100%% Games\\Cleaner^^Tool\\Cleaner-Tool.exe.old"' in script
    # Every path still lands inside a quoted assignment (`set /a` is arithmetic).
    for line in script.splitlines():
        if line.startswith("set ") and not line.startswith("set /a"):
            assert line.endswith('"') and line.count('"') == 2


# --------------------------------------------------------------------------- #
# H-002 — DATA LOSS asks before running
# --------------------------------------------------------------------------- #

def test_data_loss_task_is_confirmed(monkeypatch):
    import app.ui.runflow as runflowmod
    from app.tasks import Task
    from app.ui.app import Application

    asked = []
    released = []

    monkeypatch.setattr(runflowmod, "is_admin", lambda: True)
    monkeypatch.setattr(runflowmod, "check_info_notices", lambda _t: [])
    monkeypatch.setattr(runflowmod, "check_dangerous_combos", lambda _t: [])
    monkeypatch.setattr(
        runflowmod, "_themed_askyesno",
        lambda _root, title, msg, **k: (asked.append(msg), False)[1])

    risk = Task(key="recycle_bin", label="Empty Bin", description="Removes files",
                run=lambda _ctx: 0, risk="DATA LOSS", admin_required=False)
    app = object.__new__(Application)
    app.root = None
    app.log = lambda _m: None
    app._preflight_check = lambda _t: []
    # I-005 removed the outcome flag: the slot is released with no arguments,
    # and the run summary (not a stored enum) reports the verdict.
    app._release_run = lambda: released.append(True)
    app._set_buttons_enabled = lambda _e: pytest.fail("worker must not start")

    app._start_claimed_run(1, "Clean", [risk], [risk], "run", False, None, None)

    assert asked and "Empty Bin" in asked[0]
    assert released == [True], "the claimed slot must be handed back"
    assert not hasattr(app, "_worker_thread")


def test_safe_task_is_not_confirmed(monkeypatch):
    import app.ui.runflow as runflowmod
    from app.tasks import Task
    from app.ui.app import Application

    asked = []

    monkeypatch.setattr(runflowmod, "is_admin", lambda: True)
    monkeypatch.setattr(runflowmod, "check_info_notices", lambda _t: [])
    monkeypatch.setattr(runflowmod, "check_dangerous_combos", lambda _t: [])
    monkeypatch.setattr(
        runflowmod, "_themed_askyesno",
        lambda *a, **k: (asked.append(a), True)[1])

    # Never touch the real config from a test.
    import app.config_persist as cp
    monkeypatch.setattr(cp, "update_config", lambda _m: {})

    from app.runner import RunState

    safe = Task(key="user_temp_files", label="Temp", description="",
                run=lambda _ctx: 0, risk="SAFE", admin_required=False)
    app = object.__new__(Application)
    app.root = None
    app.log = lambda _m: None
    app._preflight_check = lambda _t: []
    app._release_run = lambda ok=True: None
    app._state = RunState.PREFLIGHT
    app._cancel_requested = False
    app._dispatch = None
    app.tabs = {}
    app._run_results = []

    started = []

    class _Thread:
        def __init__(self, *a, **k):
            started.append(True)

        def start(self):
            pass

    monkeypatch.setattr(runflowmod.threading, "Thread", _Thread)
    app._mark_running = lambda _t: None
    app._set_buttons_enabled = lambda _e: None

    class _Progress:
        def set_fraction(self, _v):
            pass

        def set_indeterminate(self, _v):
            pass

    app.progress_bar = _Progress()
    app.set_status = lambda _m: None
    app._set_progress = lambda *a, **k: None
    app._start_claimed_run(1, "Clean", [safe], [safe], "run", False, None, None)

    assert asked == [], "safe tasks must not prompt"
    assert started == [True]
