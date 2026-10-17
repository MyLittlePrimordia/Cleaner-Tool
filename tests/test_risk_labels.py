"""N-004: risk / Windows-build labels and the build-selection filter.

Two things are pinned here:

  * every shipped task declares a valid ``risk`` and a sane ``min_build``;
  * ``app.compat.task_supported`` treats ``min_build == 0`` as "any build",
    refuses a build older than the task's minimum, and fails OPEN (shows the
    task) when the host build can't be read — hiding a working tweak because
    a probe failed would be the worse failure.
"""
from __future__ import annotations

from app.compat import (task_min_build, task_supported, unsupported_reason,
                        windows_build)
from app.tasks import Task
from app.tab_presets import TABS

_VALID_RISK = {"SAFE", "ADVANCED", "REBOOT REQUIRED"}


class _Stub:
    def __init__(self, min_build=0, risk="SAFE"):
        self.min_build = min_build
        self.risk = risk


def _all_tasks():
    return [t for tasks in TABS.values() for t in tasks]


def test_task_default_is_build_agnostic_and_safe():
    t = Task(key="k", label="L", description="d", run=lambda ctx: None)
    assert t.min_build == 0
    assert t.risk == "SAFE"


def test_every_shipped_task_has_a_valid_risk_and_min_build():
    bad = [(t.key, getattr(t, "risk", None), getattr(t, "min_build", None))
           for t in _all_tasks()
           if getattr(t, "risk", "SAFE") not in _VALID_RISK
           or task_min_build(t) < 0]
    assert not bad, f"tasks with an invalid risk/min_build: {bad}"


def test_no_shipped_task_declares_a_min_build_yet():
    # Not a hard rule — but a task silently declaring a min_build would stop
    # running on older Windows, so it should be a deliberate, reviewed change
    # (and this test is the tripwire that makes it deliberate).
    declared = [(t.key, task_min_build(t)) for t in _all_tasks()
                if task_min_build(t) > 0]
    assert not declared, (
        "a task now declares min_build; confirm it is intended and update "
        f"this test: {declared}")


def test_min_build_zero_applies_to_every_build():
    assert task_supported(_Stub(min_build=0), build=10240)
    assert task_supported(_Stub(min_build=0), build=26100)


def test_task_needing_a_newer_build_is_unsupported_there():
    stub = _Stub(min_build=22000)
    assert task_supported(stub, build=22621)
    assert not task_supported(stub, build=19045)
    assert unsupported_reason(stub, build=19045) == "Needs Windows build 22000 or newer."
    assert unsupported_reason(stub, build=22621) == ""


def test_unknown_host_build_fails_open(monkeypatch):
    import app.compat as compat
    monkeypatch.setattr(compat, "windows_build", lambda: None)
    stub = _Stub(min_build=999999)   # newer than any real Windows build
    assert task_supported(stub, build=None) is True
    assert unsupported_reason(stub, build=None) == ""


def test_windows_build_probe_returns_int_or_none():
    assert isinstance(windows_build(), (int, type(None)))


def test_min_build_is_coerced_safely():
    assert task_min_build(_Stub(min_build="22000")) == 22000
    assert task_min_build(_Stub(min_build=None)) == 0
    assert task_min_build(_Stub(min_build=-5)) == 0
