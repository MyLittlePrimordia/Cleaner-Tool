"""The single-instance pulse is watched on a WORKER thread, not a Tk poll.

`Application` used to run an `after(500, ...)` chain for the life of the
process just to call `WaitForSingleObject(h, 0)`. That woke the UI thread
twice a second forever (audit 2026-10, B12). The wait now blocks on a
daemon thread and delivers the focus through TkDispatcher.

These tests pin the contract of that blocking wait without needing Win32:
the fake kernel counts waits instead of sleeping.
"""
from __future__ import annotations

import threading

import pytest


class _FakeKernel:
    """Returns WAIT_OBJECT_0 for the first `pulses` waits, timeouts after."""

    def __init__(self, pulses: int):
        self.pulses = pulses
        self.waits = 0

    def WaitForSingleObject(self, _handle, _ms):
        self.waits += 1
        if self.waits <= self.pulses:
            return 0            # _WAIT_OBJECT_0
        return 0x102            # WAIT_TIMEOUT


def _install(monkeypatch, kernel):
    import app.single_instance as si
    monkeypatch.setattr(si, "_is_windows", lambda: True)
    monkeypatch.setattr(si, "ensure_show_event", lambda name=None: 4242)
    monkeypatch.setattr(si, "_k", lambda: kernel)
    return si


def test_each_pulse_is_delivered_then_the_watcher_stops(monkeypatch):
    stop = threading.Event()
    kernel = _FakeKernel(pulses=2)
    si = _install(monkeypatch, kernel)

    seen = []

    def on_pulse():
        seen.append(1)
        if len(seen) >= 2:
            stop.set()          # what the pulse consumer does on shutdown

    si.watch_show_event(on_pulse, stop, timeout_ms=10)

    assert len(seen) == 2, "every pulse must reach the consumer exactly once"
    assert kernel.waits == 2, (
        "the watcher must not re-wait after the stop flag is set")


def test_an_already_stopped_watcher_never_waits(monkeypatch):
    stop = threading.Event()
    stop.set()
    kernel = _FakeKernel(pulses=99)
    si = _install(monkeypatch, kernel)

    si.watch_show_event(lambda: pytest.fail("no pulse expected"), stop)

    assert kernel.waits == 0


def test_off_windows_the_watcher_is_a_no_op(monkeypatch):
    import app.single_instance as si

    monkeypatch.setattr(si, "_is_windows", lambda: False)

    def _boom():           # pragma: no cover - must never run
        raise AssertionError("touched Win32 on a non-Windows host")

    monkeypatch.setattr(si, "ensure_show_event", _boom)
    si.watch_show_event(_boom, threading.Event())
