"""A-003: the schedule's last/next run, read without a locale trap.

`schtasks /V` prints dates in the OS language, which is exactly the class of
bug the schedule drift check already avoids by reading XML. These fields come
from Get-ScheduledTaskInfo as FILETIME integers, so nothing here depends on
the machine's date format.

The point of the whole feature: a switch that says "On" must not hide a job
that has not actually run for weeks.
"""
from __future__ import annotations

import json
import time

import pytest

from app import scheduler as s

_OFFSET = 11_644_473_600


def _filetime(epoch):
    return int((float(epoch) + _OFFSET) * 10_000_000)


def _payload(last=None, nxt=None, result=0, state="Ready"):
    return json.dumps({
        "last": _filetime(last) if last is not None else 0,
        "next": _filetime(nxt) if nxt is not None else 0,
        "result": result,
        "state": state,
    })


# --------------------------------------------------------------------------- #
# Parsing
# --------------------------------------------------------------------------- #
def test_filetime_becomes_epoch_seconds():
    now = 1_800_000_000
    info = s.parse_schedule_info(_payload(last=now - 60, nxt=now + 60))
    assert info["last_run"] == pytest.approx(now - 60, abs=1)
    assert info["next_run"] == pytest.approx(now + 60, abs=1)
    assert info["last_result"] == 0
    assert info["state"] == "Ready"


def test_zero_is_never_ran_not_1970():
    """FILETIME 0 means "no run", and rendering it as 1601-tinted history is
    the kind of fake data this pass exists to remove."""
    info = s.parse_schedule_info(_payload(last=None, nxt=None))
    assert "last_run" not in info
    assert "next_run" not in info


def test_unreadable_output_yields_no_claim():
    for raw in ("", "not json", "[]", None, "{}"):
        assert s.parse_schedule_info(raw) == {} or \
            set(s.parse_schedule_info(raw)) <= {"last_result"}


def test_a_nonzero_last_result_survives():
    info = s.parse_schedule_info(_payload(last=1000, result=0x80070005))
    assert info["last_result"] == 0x80070005


# --------------------------------------------------------------------------- #
# The sentence Settings shows
# --------------------------------------------------------------------------- #
def test_a_fresh_daily_run_is_not_stale():
    now = time.time()
    out = s.describe_schedule({"last_run": now - 3600,
                               "next_run": now + 7200}, "daily", now=now)
    assert out["stale"] is False
    assert out["failed"] is False
    assert "Last ran 1 hour ago" in out["text"]
    assert "next " in out["text"]


def test_a_missed_weekly_run_is_named_stale():
    """The audit's case: the laptop was off at 03:00, so nothing ran for
    three weeks while the switch still read 'On'."""
    now = time.time()
    out = s.describe_schedule({"last_run": now - 21 * 86400}, "weekly", now=now)
    assert out["stale"] is True
    assert "later than its weekly schedule" in out["text"]


def test_never_run_says_so_instead_of_inventing_a_date():
    out = s.describe_schedule({}, "daily", now=time.time())
    assert out["text"].startswith("Has not run yet")
    assert out["stale"] is False


def test_a_failed_run_reports_the_code_and_is_not_silent():
    now = time.time()
    out = s.describe_schedule({"last_run": now - 60, "last_result": 1},
                              "daily", now=now)
    assert out["failed"] is True
    assert "failed" in out["text"]
    assert "0x00000001" in out["text"]


# --------------------------------------------------------------------------- #
# Wiring
# --------------------------------------------------------------------------- #
def test_start_when_available_is_requested_after_a_successful_create(
        monkeypatch):
    """schtasks cannot set it, so enable_schedule must ask PowerShell once.

    Without it a run missed while the machine was off is skipped forever - the
    reason A-003 calls the current behaviour dishonest rather than merely
    quiet.
    """
    calls = []

    monkeypatch.setattr(s, "get_schedule_status", lambda extra=None: (False, ""))
    monkeypatch.setattr(s, "_build_schtasks_cmd", lambda *a, **k: ["schtasks"])
    monkeypatch.setattr(s, "run_capture",
                        lambda cmd, timeout=30: (calls.append(cmd), (0, "OK"))[1])
    monkeypatch.setattr(s, "update_config", lambda _m: None)
    monkeypatch.setattr(s, "_set_start_when_available",
                        lambda extra=None: calls.append("swa") or True)

    ok, _out = s.enable_schedule("weekly", "03:00")
    assert ok is True
    assert "swa" in calls, "a created task must be given StartWhenAvailable"


def test_a_failed_create_does_not_touch_task_settings(monkeypatch):
    calls = []
    monkeypatch.setattr(s, "get_schedule_status", lambda extra=None: (False, ""))
    monkeypatch.setattr(s, "_build_schtasks_cmd", lambda *a, **k: ["schtasks"])
    monkeypatch.setattr(s, "run_capture", lambda cmd, timeout=30: (1, "denied"))
    monkeypatch.setattr(s, "_set_start_when_available",
                        lambda extra=None: calls.append("swa"))
    assert s.enable_schedule("daily", "03:00")[0] is False
    assert calls == []


def test_the_info_probe_names_the_task_and_asks_powershell():
    argv = s._schedule_info_argv()
    joined = " ".join(argv).lower()
    assert "get-scheduledtaskinfo" in joined
    assert s.TASK_NAME.lower() in joined
    assert "tofiletimeutc" in joined
