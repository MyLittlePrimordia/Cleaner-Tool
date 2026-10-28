"""Safety Checkpoint's outcome, especially the one the 2026-10 log showed.

The user's Deep Repair run ended with

    Safety Checkpoint — failed: Task returned False

on a machine where System Protection was off. The PowerShell script already
knew how to name that state; these tests pin that the Python side turns it
into a skip with instructions instead of a red failure.
"""
from __future__ import annotations

import pytest

import app.utils as utils
from app.utils import TaskContext, TaskSkipped


def _ctx():
    logs = []
    return TaskContext(log=logs.append, set_status=lambda _m: None), logs


def _fake_run(output):
    def _run(_ctx, _argv, **kw):
        collect = kw.get("collect")
        if collect is not None:
            collect.extend(output)
        return 1
    return _run


def test_the_script_checks_the_restore_service_before_the_daily_limit():
    script = utils.restore_point_create_ps("before changes")
    assert "RESTORE_DISABLED" in script
    assert "SDRSVC" in script and "VSS" in script
    # The service check must come first: a disabled service otherwise reads as
    # the 24-hour throttle and gets the wrong explanation.
    assert script.index("RESTORE_DISABLED") < script.index("RESTORE_THROTTLED")


def test_a_disabled_restore_service_is_a_skip_with_instructions(monkeypatch):
    ctx, logs = _ctx()
    monkeypatch.setattr(utils, "run_cmd", _fake_run(["RESTORE_DISABLED"]))
    with pytest.raises(TaskSkipped) as excinfo:
        utils.create_restore_point(ctx)
    assert "System Protection is off" in str(excinfo.value)
    text = "\n".join(logs)
    assert "Create a restore point" in text
    assert "failed" not in text.lower()


def test_the_checkpoint_service_error_text_also_skips(monkeypatch):
    ctx, _logs = _ctx()
    monkeypatch.setattr(utils, "run_cmd", _fake_run([
        "Checkpoint-Computer : This command cannot be run due to the "
        "following error: the service cannot be started because it is "
        "disabled"]))
    with pytest.raises(TaskSkipped):
        utils.create_restore_point(ctx)


def test_a_point_made_today_is_a_throttle_skip(monkeypatch):
    ctx, logs = _ctx()
    monkeypatch.setattr(utils, "run_cmd", _fake_run(["RESTORE_THROTTLED"]))
    with pytest.raises(TaskSkipped) as excinfo:
        utils.create_restore_point(ctx)
    assert "last 24h" in str(excinfo.value)
    assert any("already created today" in line for line in logs)
