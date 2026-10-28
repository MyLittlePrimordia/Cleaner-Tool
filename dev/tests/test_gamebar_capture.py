"""Tests for the Game Bar capture fix (Win+G screenshots / clips on LTSC).

The fix is one tweak exposed in the Tweak tab AND applied by the Install tab's
"Install Game Bar" row. These tests are unit-level: every registry read/write,
the snapshot helpers, and the install capabilities are monkeypatched, so nothing
here touches the real machine or the network.

What is pinned:
  * apply sets BOTH capture values to 1;
  * the AllowGameDVR=0 policy is CLEARED when present (it blocks capture even
    after the two HKCU values are fixed) but never CREATED when absent;
  * a denied policy clear does not fail the per-user part, and an all-blocked
    apply raises (so the runner never badges it applied);
  * revert restores the snapshot, or the pre-fix values when there is none;
  * the task is registered, grouped, revertable and has a verify;
  * install_game_bar applies the fix (even when Game Bar is already present)
    and never fails a successful install if the fix itself hiccups.
"""
from __future__ import annotations

from app.utils import TaskContext
from app.tasks import tweak_gaming as t
from app.tasks import tweak_tasks


def _ctx(logs=None) -> TaskContext:
    if logs is None:
        logs = []
    return TaskContext(log=logs.append, set_status=lambda _s: None)


def _stub_snapshot(monkeypatch, snapshot=None):
    """Neutralise the snapshot helpers; return the list of snap calls."""
    snaps = []
    monkeypatch.setattr(t, "get_tweak_snapshot", lambda _tid: snapshot or {})
    monkeypatch.setattr(t, "_snap_reg_values",
                        lambda _ctx, tid, specs: snaps.append((tid, list(specs))))
    monkeypatch.setattr(t, "clear_tweak_snapshot", lambda _tid: None)
    return snaps


# --------------------------------------------------------------------------- #
# apply
# --------------------------------------------------------------------------- #

def test_apply_sets_both_flags_and_clears_a_blocking_policy(monkeypatch):
    snaps = _stub_snapshot(monkeypatch)
    writes, deletes = [], []

    # the per-user writes go through app.utils.reg_set_value (imported inside
    # the function), so patch it at its source.
    import app.utils as utils
    monkeypatch.setattr(
        utils, "reg_set_value",
        lambda _ctx, hive, path, name, value, value_type="REG_DWORD":
            writes.append((hive, name, value)) or True)
    # a present, blocking policy (what disable_game_dvr leaves behind)
    monkeypatch.setattr(t, "reg_get_value", lambda *a, **k: 0)
    monkeypatch.setattr(
        t, "reg_delete_value",
        lambda _ctx, hive, path, name: deletes.append((hive, name)) or True)

    t.apply_gamebar_capture(_ctx())

    assert snaps[0][0] == "gamebar_capture"
    assert {n for _h, n, _v in writes} == {"GameDVR_Enabled", "AppCaptureEnabled"}
    assert all(v == 1 for _h, _n, v in writes)
    assert all(h == "HKCU" for h, _n, _v in writes)
    assert ("HKLM", "AllowGameDVR") in deletes


def test_apply_leaves_an_absent_policy_alone(monkeypatch):
    _stub_snapshot(monkeypatch)
    import app.utils as utils
    monkeypatch.setattr(utils, "reg_set_value", lambda *a, **k: True)
    monkeypatch.setattr(t, "reg_get_value", lambda *a, **k: None)
    deleted = []
    monkeypatch.setattr(t, "reg_delete_value",
                        lambda _ctx, hive, path, name: deleted.append(name) or True)

    t.apply_gamebar_capture(_ctx())

    # no policy value existed -> the fix MUST NOT create one
    assert deleted == []


def test_apply_keeps_an_explicit_enabled_policy(monkeypatch):
    _stub_snapshot(monkeypatch)
    import app.utils as utils
    monkeypatch.setattr(utils, "reg_set_value", lambda *a, **k: True)
    monkeypatch.setattr(t, "reg_get_value", lambda *a, **k: 1)   # already allowed
    deleted = []
    monkeypatch.setattr(t, "reg_delete_value",
                        lambda _ctx, hive, path, name: deleted.append(name) or True)

    t.apply_gamebar_capture(_ctx())

    assert deleted == []


def test_apply_survives_a_denied_policy_clear(monkeypatch):
    _stub_snapshot(monkeypatch)
    import app.utils as utils
    monkeypatch.setattr(utils, "reg_set_value", lambda *a, **k: True)
    monkeypatch.setattr(t, "reg_get_value", lambda *a, **k: 0)
    monkeypatch.setattr(t, "reg_delete_value", lambda *a, **k: False)  # denied
    logs = []

    # per-user part succeeded, so this must NOT raise
    t.apply_gamebar_capture(_ctx(logs))
    assert any("AllowGameDVR" in line for line in logs)


def test_apply_raises_when_everything_is_blocked(monkeypatch):
    cleared = []
    monkeypatch.setattr(t, "get_tweak_snapshot", lambda _tid: {})
    monkeypatch.setattr(t, "_snap_reg_values", lambda *a, **k: None)
    monkeypatch.setattr(t, "clear_tweak_snapshot", lambda tid: cleared.append(tid))
    import app.utils as utils
    monkeypatch.setattr(utils, "reg_set_value", lambda *a, **k: False)
    monkeypatch.setattr(t, "reg_get_value", lambda *a, **k: None)

    try:
        t.apply_gamebar_capture(_ctx())
    except RuntimeError:
        pass
    else:                                                # pragma: no cover
        raise AssertionError("an all-blocked apply must raise")
    assert cleared == ["gamebar_capture"]                # not left half-recorded


# --------------------------------------------------------------------------- #
# revert
# --------------------------------------------------------------------------- #

def test_revert_uses_the_snapshot_when_present(monkeypatch):
    monkeypatch.setattr(t, "get_tweak_snapshot",
                        lambda _tid: {"specs": [["HKCU", "X", "Y"]]})
    restored = []
    monkeypatch.setattr(t, "_restore_reg_values",
                        lambda _ctx, tid, value_type="REG_DWORD":
                            restored.append(tid) or 1)

    t.revert_gamebar_capture(_ctx())
    assert restored == ["gamebar_capture"]


def test_revert_falls_back_to_pre_fix_values(monkeypatch):
    monkeypatch.setattr(t, "get_tweak_snapshot", lambda _tid: {})
    writes, deletes = [], []
    monkeypatch.setattr(
        t, "reg_set_value_checked",
        lambda _ctx, hive, path, name, value, value_type="REG_DWORD":
            writes.append((hive, name, value)))
    monkeypatch.setattr(t, "reg_delete_value",
                        lambda _ctx, hive, path, name: deletes.append(name) or True)

    t.revert_gamebar_capture(_ctx())

    assert {n for _h, n, _v in writes} == {"GameDVR_Enabled", "AppCaptureEnabled"}
    assert all(v == 0 for _h, _n, v in writes)
    assert "AllowGameDVR" in deletes


# --------------------------------------------------------------------------- #
# registration / grouping / wiring
# --------------------------------------------------------------------------- #

def test_task_is_registered_grouped_and_reversible():
    task = next((x for x in tweak_tasks.TASKS if x.key == "gamebar_capture"), None)
    assert task is not None
    assert task.revert is not None
    assert task.verify is not None
    assert task.default is False            # opt-in, Custom-only

    from app.tab_presets import CUSTOM_GROUPS, TABS
    assert "gamebar_capture" in {x.key for x in TABS["Tweak"]}
    groups = {k for _title, keys in
              CUSTOM_GROUPS["Tweak"] for k in keys}
    assert "gamebar_capture" in groups


def test_verify_flags_a_blocking_policy(monkeypatch):
    # both capture flags are on ...
    # (patched at the source: verify reads through two modules since the split)
    import app.utils as utils
    monkeypatch.setattr(utils, "reg_get_value",
                        lambda _ctx, hive, path, name:
                            1 if hive == "HKCU" else 0)
    # ... but AllowGameDVR=0 still disables capture machine-wide -> drift
    assert t.verify_gamebar_capture() is False


def test_verify_ok_without_a_policy(monkeypatch):
    import app.utils as utils
    monkeypatch.setattr(utils, "reg_get_value",
                        lambda _ctx, hive, path, name:
                            1 if hive == "HKCU" else None)
    assert t.verify_gamebar_capture() is True


def test_install_game_bar_applies_the_capture_fix(monkeypatch):
    from app.tasks import install_tasks
    from app.tasks import tweak_tasks
    from app import config_persist

    monkeypatch.setattr(install_tasks.cap, "has_game_bar", lambda: True)
    called = []
    monkeypatch.setattr(tweak_tasks, "apply_gamebar_capture",
                        lambda ctx: called.append("apply"))
    monkeypatch.setattr(config_persist, "mark_tweak_applied",
                        lambda tid: called.append(("mark", tid)))

    install_tasks.install_game_bar(_ctx())

    assert called == ["apply", ("mark", "gamebar_capture")]


def test_install_game_bar_never_fails_a_successful_install(monkeypatch):
    from app.tasks import install_tasks
    from app.tasks import tweak_tasks

    monkeypatch.setattr(install_tasks.cap, "has_game_bar", lambda: True)

    def _boom(_ctx):
        raise RuntimeError("registry locked")

    monkeypatch.setattr(tweak_tasks, "apply_gamebar_capture", _boom)
    logs = []
    install_tasks.install_game_bar(_ctx(logs))          # must not raise
    assert any("capture fix not applied" in line for line in logs)
