"""AUDIT TX-001: one fact store, one reader per key.

These tests are about the contract tools rely on, not about this PC's
hardware: every provider is a fake, and the shared singleton is only checked
for its registrations (importing it must not measure anything).
"""
from __future__ import annotations

import gc
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from app.facts import FAILURE_TTL, Fact, Facts


class Clock:
    """A settable clock, so TTL tests never sleep."""

    def __init__(self, now=1_000.0):
        self.now = float(now)

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += float(seconds)
        return self.now


def test_a_fact_carries_value_source_and_time():
    clock = Clock()
    store = Facts(clock=clock)
    store.register("sample.value", lambda: 42, 30, source="test.reader")

    fact = store.get("sample.value")

    assert isinstance(fact, Fact)
    assert fact.key == "sample.value"
    assert fact.value == 42
    assert fact.source == "test.reader"
    assert fact.as_of == clock.now
    assert fact.ok is True
    assert fact.error == ""


def test_a_reading_is_cached_until_its_ttl_expires():
    clock = Clock()
    store = Facts(clock=clock)
    calls = []
    store.register("sample.value", lambda: calls.append(1) or len(calls), 30)

    assert store.get("sample.value").value == 1
    assert store.get("sample.value").value == 1
    assert calls == [1]

    clock.advance(30.5)
    assert store.get("sample.value").value == 2
    assert calls == [1, 1]


def test_max_age_zero_measures_now_and_clears_the_engine_memo():
    store = Facts(clock=Clock())
    calls, hooks = [], []
    store.register("sample.value", lambda: calls.append(1) or len(calls), 600,
                   on_invalidate=lambda: hooks.append(1))

    store.get("sample.value")
    store.get("sample.value", max_age=0)

    assert len(calls) == 2
    assert hooks == [1]


def test_an_unknown_key_is_an_honest_failure_not_an_exception():
    store = Facts(clock=Clock())

    fact = store.get("nothing.here")

    assert fact.ok is False
    assert fact.value is None
    assert "No reader" in fact.error


def test_a_failed_reader_keeps_its_reason_and_is_retried_soon():
    clock = Clock()
    store = Facts(clock=clock)
    calls = []

    def boom():
        calls.append(1)
        raise PermissionError("access denied")

    store.register("sample.value", boom, 600)

    first = store.get("sample.value")
    second = store.get("sample.value")

    assert first.ok is False
    assert "access denied" in first.error
    assert second.ok is False
    assert len(calls) == 1, "a failure should not be re-read on every open"

    clock.advance(FAILURE_TTL + 0.1)
    store.get("sample.value")
    assert len(calls) == 2, "the retry must not be punished for a full TTL"


def test_a_reader_can_recover_after_failing():
    store = Facts(clock=Clock())
    state = {"ok": False}

    def maybe():
        if not state["ok"]:
            raise OSError("not ready")
        return "ready"

    store.register("sample.value", maybe, 60)
    assert store.get("sample.value").ok is False

    state["ok"] = True
    store.refresh("sample.value")
    assert store.get("sample.value").value == "ready"


def test_invalidate_forgets_the_value_and_asks_the_engine_too():
    store = Facts(clock=Clock())
    state = {"value": 1}
    hooks = []
    store.register("sample.value", lambda: state["value"], 600,
                   on_invalidate=lambda: hooks.append(1))

    assert store.get("sample.value").value == 1
    state["value"] = 2
    assert store.get("sample.value").value == 1, "still cached"

    store.invalidate("sample.value")
    assert hooks == [1]
    assert store.get("sample.value").value == 2


def test_refresh_measures_once_and_tells_subscribers():
    store = Facts(clock=Clock())
    calls = []
    seen = []
    store.register("sample.value", lambda: calls.append(1) or len(calls), 600)
    stop = store.subscribe("sample", seen.append)

    store.get("sample.value")
    store.get("sample.value")
    assert len(seen) == 1, "a cache hit is not news"

    store.refresh("sample.value")
    assert len(calls) == 2
    assert len(seen) == 2
    assert seen[-1].value == 2

    stop()
    store.refresh("sample.value")
    assert len(seen) == 2


def test_unsubscribe_and_a_raising_subscriber_never_break_a_read():
    store = Facts(clock=Clock())
    store.register("sample.value", lambda: 1, 600)

    def explode(_fact):
        raise RuntimeError("subscriber bug")

    stop = store.subscribe("sample", explode)
    assert store.get("sample.value").ok is True
    stop()
    assert store.get("sample.value").ok is True


def test_a_subscriber_added_during_a_notification_survives():
    store = Facts(clock=Clock())
    store.register("sample.value", lambda: 1, 600)
    later = []
    added = []

    def first(_fact):
        if not added:
            added.append(1)
            store.subscribe("sample", lambda f: later.append(f.value))

    store.subscribe("sample", first)
    store.refresh("sample.value")
    store.refresh("sample.value")

    assert later == [1], "the list was rebuilt and lost the new subscriber"


def test_a_bound_subscriber_dies_with_its_owner():
    store = Facts(clock=Clock())
    store.register("sample.value", lambda: 1, 600)

    class Owner:
        def __init__(self):
            self.seen = []

        def on_fact(self, fact):
            self.seen.append(fact)

    owner = Owner()
    store.subscribe("sample", owner.on_fact)
    store.get("sample.value")
    assert len(owner.seen) == 1

    del owner
    gc.collect()
    # Must not raise or keep a dead dialog alive.
    store.refresh("sample.value")


def test_concurrent_reads_of_one_key_measure_once():
    store = Facts(clock=Clock())
    calls = []
    started = threading.Event()
    release = threading.Event()

    def slow():
        calls.append(1)
        started.set()
        assert release.wait(3), "test provider was never released"
        return "value"

    store.register("sample.value", slow, 600)
    out = []

    def read():
        out.append(store.get("sample.value").value)

    first = threading.Thread(target=read)
    second = threading.Thread(target=read)
    first.start()
    assert started.wait(3)
    second.start()
    time.sleep(0.05)          # let the second reader reach the key lock
    release.set()
    first.join(3)
    second.join(3)

    assert calls == [1], "two dialogs must not run the same measurement"
    assert out == ["value", "value"]


def test_keys_are_dotted_and_have_exactly_one_reader():
    store = Facts(clock=Clock())
    store.register("sample.value", lambda: 1, 60)

    with pytest.raises(ValueError):
        store.register("sample.value", lambda: 2, 60)
    with pytest.raises(ValueError):
        store.register("plainkey", lambda: 1, 60)
    with pytest.raises(ValueError):
        store.register("sample.zero", lambda: 1, 0)


def test_the_shared_store_registers_the_first_domains():
    from app import facts as shared

    keys = set(shared.providers())
    assert {"hardware.os", "hardware.cpu", "hardware.ram", "hardware.board",
            "hardware.gpu", "disks.usage", "memory.usage",
            "system.startup"} <= keys


def test_importing_the_store_does_not_import_tk():
    root = Path(__file__).resolve().parents[2]
    code = ("import sys, app.facts; "
            "raise SystemExit(1 if 'tkinter' in sys.modules else 0)")
    done = subprocess.run([sys.executable, "-c", code], cwd=str(root),
                          capture_output=True, timeout=30)
    assert done.returncode == 0, done.stderr.decode("utf-8", "replace")


# --------------------------------------------------------------------------- #
# Adoption: the first tools read the shared fact, not their own copy.
# --------------------------------------------------------------------------- #
tk = pytest.importorskip("tkinter")
from _tkhelpers import make_root

GB = 1024 ** 3


def test_pc_specs_sections_read_shared_facts(monkeypatch):
    from app.ui.dialogs import specs_dialog

    values = {
        "hardware.os": {"caption": "Test OS", "build": "build 7"},
        "hardware.cpu": {"name": "Test CPU", "detail": "3.0GHz"},
        "hardware.ram": {"headline": "32 GB", "detail": "16 GB available"},
        "hardware.board": {"name": "Test Board", "firmware": "BIOS 9"},
        "hardware.gpu": [{"name": "Test GPU"}],
        "disks.usage": [{"drive": "C:", "label": "System", "fs": "NTFS",
                         "total": 100 * GB, "free": 40 * GB}],
    }
    calls = []

    def fake_get(key, **_kw):
        calls.append(key)
        return Fact(key, values[key], "test", time.time())

    root = make_root()
    try:
        root.withdraw()
        dlg = specs_dialog.SpecsDialog(root, object())
        monkeypatch.setattr(specs_dialog.facts, "get", fake_get)

        os_rows = dict(dlg._gather("OS"))
        assert os_rows["Operating System"] == "Test OS"
        assert os_rows["Version"] == "build 7"
        assert calls[-1] == "hardware.os"

        assert dict(dlg._gather("CPU"))["Processor"] == "Test CPU"
        assert dict(dlg._gather("RAM"))["Memory"] == "32 GB"
        assert dict(dlg._gather("Board"))["Motherboard"] == "Test Board"
        assert dict(dlg._gather("Graphics"))["GPU 1"] == "Test GPU"
        storage = dict(dlg._gather("Storage"))
        assert "C: System" in storage
        assert "40.0GB free of 100.0GB" in storage["C: System"]
        dlg.close()
    finally:
        root.destroy()


def test_drive_toolkit_takes_its_picker_from_the_disk_fact(monkeypatch):
    from app.ui.dialogs import drive_toolkit_dialog

    def fake_get(key, **_kw):
        assert key == "disks.usage"
        return Fact(key, [{"drive": "E:", "label": "", "fs": "NTFS",
                           "total": 100 * GB, "free": 50 * GB}],
                    "test", time.time())

    monkeypatch.setattr(drive_toolkit_dialog.facts, "get", fake_get)
    root = make_root()
    try:
        root.withdraw()
        dlg = drive_toolkit_dialog.DriveToolkitDialog(root, object())
        dlg._switch("speed")
        assert dlg._speed_var.get() == "E:"
        dlg.close()
    finally:
        root.destroy()


def test_hardware_monitor_adopts_the_gpu_fact(monkeypatch):
    from app.ui.dialogs import hardware_monitor_dialog

    def fake_get(key, **_kw):
        assert key == "hardware.gpu"
        return Fact(key, [{"name": "Test Adapter", "total_vram_bytes": 8 * GB,
                           "total_vram_approx": False}],
                    "test", time.time())

    monkeypatch.setattr(hardware_monitor_dialog.facts, "get", fake_get)
    root = make_root()
    try:
        root.withdraw()
        dlg = hardware_monitor_dialog.HardwareMonitorDialog(root, object())
        assert dlg._gpu_list[0]["name"] == "Test Adapter"
        assert dlg._gpu_menu_labels()[0].startswith("Test Adapter")
        dlg.close()
    finally:
        root.destroy()


def test_startup_manager_keeps_a_failed_read_distinct_from_empty(monkeypatch):
    from app.ui.dialogs import startup_manager_dialog

    monkeypatch.setattr(
        startup_manager_dialog.facts, "get",
        lambda key, **_kw: Fact(key, None, "test", time.time(), ok=False,
                                error="registry busy"))
    root = make_root()
    try:
        root.withdraw()
        dlg = startup_manager_dialog.StartupManagerDialog(root, object())
        texts = []
        for child in dlg._panel.inner.winfo_children():
            try:
                texts.append(child.cget("text"))
            except Exception:
                pass
        assert any("Couldn't read your startup items." in t for t in texts)
        dlg.close()
    finally:
        root.destroy()


def test_startup_refresh_button_forces_a_new_read(monkeypatch):
    from app.ui.dialogs import startup_manager_dialog

    seen = []

    def fake_get(key, **kw):
        seen.append(dict(kw))
        return Fact(key, [], "test", time.time())

    monkeypatch.setattr(startup_manager_dialog.facts, "get", fake_get)
    root = make_root()
    try:
        root.withdraw()
        dlg = startup_manager_dialog.StartupManagerDialog(root, object())
        dlg._refresh(force=True)
        assert seen[-1].get("max_age") == 0
        dlg.close()
    finally:
        root.destroy()


def test_startup_toggle_invalidates_the_cached_list(monkeypatch):
    from app.ui.dialogs import startup_manager_dialog

    calls = []
    monkeypatch.setattr(
        startup_manager_dialog.facts, "get",
        lambda key, **_kw: Fact(key, [], "test", time.time()))
    monkeypatch.setattr(
        startup_manager_dialog.facts, "invalidate",
        lambda *keys: calls.extend(keys))
    import app.startup_manager as startup_manager
    monkeypatch.setattr(startup_manager, "set_item_enabled",
                        lambda *a, **k: (True, "Changed."))

    root = make_root()
    try:
        root.withdraw()
        dlg = startup_manager_dialog.StartupManagerDialog(root, object())
        var = tk.BooleanVar(value=False)
        dlg._on_toggle({"id": "hkcu_run:Thing", "enabled": True}, var)
        assert "system.startup" in calls
        dlg.close()
    finally:
        root.destroy()
