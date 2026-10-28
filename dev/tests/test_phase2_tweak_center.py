"""Phase 2 regression tests — Tweak Center.

Covers the portable snapshot backup/restore (config_persist) and the Tk-free
capability layer (app/tweak_center.py): applied-tweak listing, undo-all, and the
prior -> current change log.

Config is isolated to an in-memory store, so these tests never touch the user's
real config.json; the only file side effect is under pytest's tmp_path.
"""
from __future__ import annotations

import copy
import json

import pytest

from app.tab_presets import TABS


@pytest.fixture
def mem_config(monkeypatch):
    """Point config_persist at an in-memory store."""
    import app.config_persist as cp

    store = {"tweak_snapshots": {}, "applied_tweaks": []}
    monkeypatch.setattr(cp, "load_config", lambda: copy.deepcopy(store))

    def _update(mutate):
        res = mutate(store)
        return store if res is None else res

    monkeypatch.setattr(cp, "update_config", _update)
    return store


def _revertable_task():
    return next(t for t in TABS["Tweak"] if getattr(t, "revert", None) is not None)


# --------------------------------------------------------------------------- #
# export / import
# --------------------------------------------------------------------------- #

def test_export_then_import_round_trip(mem_config, tmp_path):
    import app.config_persist as cp

    mem_config["applied_tweaks"] = ["file_extensions"]
    mem_config["tweak_snapshots"] = {
        "file_extensions": {
            "specs": [["HKCU", "P", "HideFileExt"]],
            "0:present": True, "0:value": 1, "0:type": "REG_DWORD",
        },
    }
    path = str(tmp_path / "backup.json")

    assert cp.export_tweak_snapshots(path) == 1
    data = json.loads((tmp_path / "backup.json").read_text(encoding="utf-8"))
    assert data["applied_tweaks"] == ["file_extensions"]
    assert "file_extensions" in data["tweak_snapshots"]

    # wipe, then import into the empty store
    mem_config["applied_tweaks"] = []
    mem_config["tweak_snapshots"] = {}
    assert cp.import_tweak_snapshots(path) == 1
    assert mem_config["tweak_snapshots"]["file_extensions"]["specs"]
    assert "file_extensions" in mem_config["applied_tweaks"]


def test_import_keeps_fresher_local_snapshot(mem_config, tmp_path):
    import app.config_persist as cp

    p = tmp_path / "b.json"
    p.write_text(json.dumps({
        "applied_tweaks": [],
        "tweak_snapshots": {"t1": {"specs": [["HKCU", "P", "V"]], "0:value": "OLD"}},
    }), encoding="utf-8")

    mem_config["tweak_snapshots"] = {
        "t1": {"specs": [["HKCU", "P", "V"]], "0:value": "NEW"}}
    assert cp.import_tweak_snapshots(str(p), merge=True) == 0
    assert mem_config["tweak_snapshots"]["t1"]["0:value"] == "NEW"


def test_import_rejects_malformed_file(mem_config, tmp_path):
    import app.config_persist as cp

    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    with pytest.raises(ValueError):
        cp.import_tweak_snapshots(str(bad))

    arr = tmp_path / "arr.json"
    arr.write_text("[1, 2, 3]", encoding="utf-8")
    with pytest.raises(ValueError):
        cp.import_tweak_snapshots(str(arr))


def test_import_skips_junk_entries(mem_config, tmp_path):
    import app.config_persist as cp

    p = tmp_path / "j.json"
    p.write_text(json.dumps({
        "applied_tweaks": ["ok", 5, None],
        "tweak_snapshots": {
            "ok": {"specs": [["HKCU", "P", "V"]]},
            # H-008: an empty spec list is not a snapshot the registry can
            # restore, so it is dropped here rather than imported and then
            # deleted by the next config load.
            "empty": {"specs": []},
            "bad": "not-a-dict",
        },
    }), encoding="utf-8")

    assert cp.import_tweak_snapshots(str(p)) == 1
    assert mem_config["applied_tweaks"] == ["ok"]
    assert set(mem_config["tweak_snapshots"]) == {"ok"}


# --------------------------------------------------------------------------- #
# tweak_center: applied list, undo-all, change log
# --------------------------------------------------------------------------- #

def test_applied_tasks_only_returns_revertable(mem_config):
    from app import tweak_center

    t = _revertable_task()
    mem_config["applied_tweaks"] = [t.key, "not_a_real_task"]

    keys = [x.key for x in tweak_center.applied_tweak_tasks()]
    assert t.key in keys
    assert "not_a_real_task" not in keys
    assert [x.key for x in tweak_center.undo_all_tasks()] == keys


def test_describe_applied_reports_prior_and_current(mem_config, monkeypatch):
    from app import tweak_center

    t = _revertable_task()
    mem_config["applied_tweaks"] = [t.key]
    mem_config["tweak_snapshots"] = {
        t.key: {
            "specs": [["HKCU", "Software\\X", "HideFileExt"]],
            "0:present": True, "0:denied": False,
            "0:value": 0, "0:type": "REG_DWORD",
        },
    }
    # live value is now 1 -> the row must report it as "changed"
    monkeypatch.setattr(tweak_center, "reg_get_value_typed",
                        lambda ctx, hive, path, name: (1, "REG_DWORD"))

    rows = tweak_center.describe_applied()
    assert len(rows) == 1
    r = rows[0]
    assert r["name"] == "HideFileExt"
    assert r["prior"] == "0"
    assert r["current"] == "1"
    assert r["changed"] is True


def test_describe_applied_handles_absent_prior(mem_config, monkeypatch):
    from app import tweak_center

    t = _revertable_task()
    mem_config["applied_tweaks"] = [t.key]
    mem_config["tweak_snapshots"] = {
        t.key: {
            "specs": [["HKCU", "Software\\X", "NewValue"]],
            "0:present": False, "0:denied": False, "0:value": None,
        },
    }
    monkeypatch.setattr(tweak_center, "reg_get_value_typed",
                        lambda ctx, hive, path, name: (1, "REG_DWORD"))

    r = tweak_center.describe_applied()[0]
    assert r["prior"] == "(not set)"
    assert r["current"] == "1"
    assert r["changed"] is True


def test_change_log_text_lists_tweaks(mem_config, monkeypatch):
    from app import tweak_center

    t = _revertable_task()
    mem_config["applied_tweaks"] = [t.key]
    mem_config["tweak_snapshots"] = {
        t.key: {
            "specs": [["HKCU", "Software\\X", "HideFileExt"]],
            "0:present": True, "0:value": 0, "0:type": "REG_DWORD",
        },
    }
    monkeypatch.setattr(tweak_center, "reg_get_value_typed",
                        lambda ctx, hive, path, name: (1, "REG_DWORD"))

    text = tweak_center.change_log_text()
    assert "HideFileExt" in text
    assert "0 -> 1" in text
    assert t.key in text


def test_change_log_empty_is_honest(mem_config):
    from app import tweak_center

    text = tweak_center.change_log_text()
    assert "No applied tweaks" in text


# --------------------------------------------------------------------------- #
# tweak_center: applied_groups (the per-row browser's data — roadmap 2.1)
# --------------------------------------------------------------------------- #

def test_task_for_key():
    from app import tweak_center

    t = _revertable_task()
    assert tweak_center.task_for_key(t.key) is t
    assert tweak_center.task_for_key("not_a_real_task") is None


def test_applied_groups_shape_and_rows(mem_config, monkeypatch):
    from app import tweak_center

    t = _revertable_task()
    mem_config["applied_tweaks"] = [t.key]
    mem_config["tweak_snapshots"] = {
        t.key: {
            "specs": [["HKCU", "Software\\X", "HideFileExt"]],
            "0:present": True, "0:value": 0, "0:type": "REG_DWORD",
        },
    }
    monkeypatch.setattr(tweak_center, "reg_get_value_typed",
                        lambda ctx, hive, path, name: (1, "REG_DWORD"))

    groups = tweak_center.applied_groups()
    assert [g["task"] for g in groups] == [t.key]
    g = groups[0]
    assert g["label"]  # a human label, not just the key
    assert g["rows"][0]["name"] == "HideFileExt"
    assert g["rows"][0]["changed"] is True


def test_applied_groups_keeps_a_revertable_tweak_with_no_registry_rows(mem_config):
    """A powercfg / service tweak has no registry 'specs', so it has no value
    diff — but it must still appear (so it can be undone), with rows == []."""
    from app import tweak_center

    t = _revertable_task()
    mem_config["applied_tweaks"] = [t.key]
    mem_config["tweak_snapshots"] = {t.key: {}}

    groups = tweak_center.applied_groups()
    assert [g["task"] for g in groups] == [t.key]
    assert groups[0]["rows"] == []


def test_applied_groups_empty_when_nothing_applied(mem_config):
    from app import tweak_center

    assert tweak_center.applied_groups() == []
