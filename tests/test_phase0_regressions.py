"""Phase 0 regression tests.

One test per HIGH-severity fix in the audit handoff, written so it fails on the
OLD code and passes on the fixed code:

  H-1  remove_temp_files_deep   no PowerShell shell-out (empty %TEMP% hazard)
  H-2  apply_clipboard_sync_off  writes 0 (0 = Not allowed), not 1
  H-3  reg_set_value            value_type=None resolves to REG_DWORD (no KeyError)
  H-4  clean_orphan_program_leftovers  fails closed on a short installed list

These are unit tests with the registry / filesystem / subprocess boundary
monkeypatched, so they never touch the real machine.
"""
from __future__ import annotations

import app.utils as utils
from app.utils import TaskContext


def _ctx(logs=None) -> TaskContext:
    if logs is None:
        logs = []
    return TaskContext(log=logs.append, set_status=lambda _s: None)


# --------------------------------------------------------------------------- #
# H-3 — reg_set_value(None type) must resolve to REG_DWORD, not KeyError
# --------------------------------------------------------------------------- #

def test_type_map_has_no_none_key_documents_the_hazard():
    # The bug was _TYPE_MAP[None] -> KeyError; this pins the premise.
    assert None not in utils._TYPE_MAP


def test_reg_set_value_none_type_uses_reg_dword(monkeypatch):
    captured: dict = {}

    class _FakeKey:
        pass

    monkeypatch.setattr(utils.winreg, "CreateKeyEx", lambda *a, **k: _FakeKey())
    monkeypatch.setattr(
        utils.winreg, "SetValueEx",
        lambda key, name, reserved, vtype, value: captured.update(vtype=vtype, value=value),
    )
    monkeypatch.setattr(utils.winreg, "CloseKey", lambda k: None)

    ok = utils.reg_set_value(_ctx(), "HKCU", "Software\\Phase0Test", "X", 1, value_type=None)
    assert ok is True
    assert captured["vtype"] == utils.winreg.REG_DWORD
    assert captured["value"] == 1


def test_reg_set_value_explicit_type_is_honoured(monkeypatch):
    captured: dict = {}
    monkeypatch.setattr(utils.winreg, "CreateKeyEx", lambda *a, **k: object())
    monkeypatch.setattr(
        utils.winreg, "SetValueEx",
        lambda key, name, reserved, vtype, value: captured.update(vtype=vtype),
    )
    monkeypatch.setattr(utils.winreg, "CloseKey", lambda k: None)

    ok = utils.reg_set_value(_ctx(), "HKCU", "Software\\Phase0Test", "Y", "s", value_type="REG_SZ")
    assert ok is True
    assert captured["vtype"] == utils.winreg.REG_SZ


# --------------------------------------------------------------------------- #
# H-2 — clipboard "sync off" must write 0, not 1
# --------------------------------------------------------------------------- #

def test_clipboard_sync_off_writes_zero(monkeypatch):
    import app.tasks.tweak_tasks as t

    captured: dict = {}
    monkeypatch.setattr(t, "get_tweak_snapshot", lambda _tid: {})
    monkeypatch.setattr(t, "_snap_reg_values", lambda _ctx, _tid, _specs: None)
    monkeypatch.setattr(t, "clear_tweak_snapshot", lambda _tid: None)

    def _fake_write(_ctx, hive, path, name, value, value_type="REG_DWORD"):
        captured.update(hive=hive, path=path, name=name, value=value)
        return True

    monkeypatch.setattr(t, "reg_set_value_checked", _fake_write)

    t.apply_clipboard_sync_off(_ctx())

    assert captured["name"] == "AllowCrossDeviceClipboard"
    assert captured["value"] == 0, "0 = Not allowed is the only value that disables cross-device sync"
    assert captured["path"] == "SOFTWARE\\Policies\\Microsoft\\Windows\\System"


# --------------------------------------------------------------------------- #
# H-1 — remove_temp_files_deep must not shell out to PowerShell
# --------------------------------------------------------------------------- #

def test_temp_deep_does_not_shell_out(monkeypatch):
    import app.tasks.clean_tasks as ct

    calls: list = []
    monkeypatch.setattr(
        ct, "_clean_many",
        lambda _ctx, folders, label: calls.append((tuple(folders), label)) or 10,
    )

    def _boom(*a, **k):
        raise AssertionError("remove_temp_files_deep must not shell out (H-1)")

    monkeypatch.setattr(ct, "run_cmd", _boom)

    total = ct.remove_temp_files_deep(_ctx())

    assert total == 20
    assert len(calls) == 2
    labels = {label for _folders, label in calls}
    assert labels == {"user temp folder", "system temp folder"}


# --------------------------------------------------------------------------- #
# H-4 — orphan scan must refuse a short/unreadable installed list
# --------------------------------------------------------------------------- #

def test_orphan_scan_refuses_small_installed_list(monkeypatch, tmp_path):
    import app.game_catalog as gc
    import app.tasks.clean_tasks as ct

    # A folder that would look like a high-confidence leftover candidate if the
    # installed-programs guard were missing.
    (tmp_path / "SomeGameFolder").mkdir()

    monkeypatch.setattr(gc, "installed_display_names", lambda: set())
    monkeypatch.setattr(ct, "_orphan_scan_roots", lambda: [str(tmp_path)])

    logs: list = []
    result = ct.clean_orphan_program_leftovers(_ctx(logs))

    assert result == 0
    assert any("refusing the orphan scan" in line for line in logs)
