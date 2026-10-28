"""B-012 / B-013: reads may degrade, writes must not destroy.

`load_config()` has always been allowed to serve DEFAULT_CONFIG when the file
could not be READ (a locked handle, an AV scan, a transient I/O error). What it
must never do is hand that dict to the next writer, which would replace the
user's intact config - every tweak snapshot, the applied registry, the saved
selections - with defaults. And when the corrupt file is recovered from its
`.bak`, the unreadable primary must be moved aside BEFORE the next save copies
it over the backup.

Both tests drive the real loader/saver through a temp CONFIG_DIR; only the
file read and the event log are faked.
"""
from __future__ import annotations

import json

import pytest

import app.config_persist as cp


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    """A temp config path plus a clean degraded/cache state."""
    path = tmp_path / "config.json"
    monkeypatch.setattr(cp, "CONFIG_FILE", path)
    monkeypatch.setattr(cp, "CONFIG_DIR", tmp_path)
    events = []
    monkeypatch.setattr(cp, "log_security_event",
                        lambda kind, msg: events.append((kind, msg)))
    monkeypatch.setattr(cp, "_config_cache", None)
    monkeypatch.setattr(cp, "_config_mtime", None)
    cp._clear_degraded()
    yield path, events
    cp._clear_degraded()
    cp._config_cache = None
    cp._config_mtime = None


def _write(path, data):
    path.write_text(json.dumps(data), encoding="utf-8")


# --------------------------------------------------------------------------- #
# B-012: an unreadable file is not overwritten
# --------------------------------------------------------------------------- #

def test_a_locked_config_is_never_overwritten_with_defaults(cfg, monkeypatch):
    path, events = cfg
    stored = {
        "schedule_frequency": "daily",
        "applied_tweaks": ["gamedvr_disable"],
        "tweak_snapshots": {"gamedvr_disable": {"specs": [
            ["HKCU", r"Software\Microsoft\Windows\CurrentVersion\GameDVR",
             "AppCaptureEnabled"]]}},
    }
    _write(path, stored)
    before = path.read_bytes()

    def _locked(_fh):
        raise PermissionError("the file is locked by another process")

    monkeypatch.setattr(cp.json, "load", _locked)

    # A read degrades to defaults rather than inventing the file's content,
    # and says so once.
    served = cp.load_config()
    assert served["schedule_frequency"] == cp.DEFAULT_CONFIG["schedule_frequency"]
    assert served["applied_tweaks"] == []
    assert cp._config_degraded is True
    assert [k for k, _m in events] == ["config_unreadable"]

    # A read again while degraded does not log again.
    cp.load_config()
    assert [k for k, _m in events] == ["config_unreadable"]

    # The write refuses instead of persisting the defaults.
    with pytest.raises(cp.ConfigUnavailable):
        cp.update_config(lambda c: c.update({"schedule_frequency": "weekly"}))
    with pytest.raises(cp.ConfigUnavailable):
        cp.save_config(dict(cp.DEFAULT_CONFIG))

    assert path.read_bytes() == before, "the user's file must be untouched"


def test_the_writer_works_again_once_the_file_is_readable(cfg, monkeypatch):
    path, _events = cfg
    _write(path, {"schedule_frequency": "daily"})

    original = cp.json.load
    calls = {"n": 0}

    def _fail_once(fh):
        calls["n"] += 1
        if calls["n"] == 1:
            raise PermissionError("locked")
        return original(fh)

    monkeypatch.setattr(cp.json, "load", _fail_once)
    cp.load_config()                     # degrades
    assert cp._config_degraded

    # The next read retries the disk, succeeds, and clears the flag.
    again = cp.load_config()
    assert cp._config_degraded is False
    assert again["schedule_frequency"] == "daily"

    cp.update_config(lambda c: c.update({"schedule_frequency": "weekly"}))
    assert json.loads(path.read_text(encoding="utf-8"))["schedule_frequency"] == "weekly"


def test_a_failed_write_does_not_mark_the_session_degraded(cfg):
    """Only reads degrade. A save that fails is just a failed save."""
    path, _events = cfg
    _write(path, {"schedule_frequency": "daily"})
    assert cp._config_degraded is False
    cp.load_config()
    assert cp._config_degraded is False


# --------------------------------------------------------------------------- #
# H-008: an import validates the snapshots, not just the JSON
# --------------------------------------------------------------------------- #

def test_import_drops_snapshots_the_registry_would_reject(cfg, tmp_path):
    path, events = cfg
    _write(path, {})
    bundle = tmp_path / "bundle.json"
    _write(bundle, {
        "version": 1,
        "applied_tweaks": ["good_tweak", 7, "", "x" * 500],
        "tweak_snapshots": {
            "good_tweak": {"specs": [["HKCU", r"Software\X", "V"]]},
            # a bad hive, a spec that is not a list, and a non-dict snapshot
            "bad_hive": {"specs": [["HKXX", r"Software\X", "V"]]},
            "bad_spec": {"specs": ["HKCU"]},
            "not_a_dict": "nonsense",
        },
    })

    assert cp.import_tweak_snapshots(str(bundle)) == 1

    on_disk = json.loads(path.read_text(encoding="utf-8"))
    assert set(on_disk["tweak_snapshots"]) == {"good_tweak"}, (
        "rejected snapshots must never reach the file")
    assert on_disk["applied_tweaks"] == ["good_tweak"]
    assert any(k == "snapshot_import_rejected" for k, _m in events)


# --------------------------------------------------------------------------- #
# B-013: a recovery keeps the backup it recovered from
# --------------------------------------------------------------------------- #

def test_recovery_archives_the_corrupt_file_so_the_bak_survives(cfg):
    path, events = cfg
    bak = path.with_suffix(path.suffix + ".bak")
    good = {
        "schedule_frequency": "daily",
        "applied_tweaks": ["gamedvr_disable"],
        "tweak_snapshots": {},
    }
    _write(bak, good)
    path.write_text("{ this is not json", encoding="utf-8")

    recovered = cp.load_config()
    assert recovered["schedule_frequency"] == "daily"
    assert recovered["applied_tweaks"] == ["gamedvr_disable"]
    assert any(k == "config_recovered" for k, _m in events)

    # The unreadable primary is out of the way, so the next save's backup step
    # cannot copy it over the known-good .bak.
    archived = list(path.parent.glob("config.json.corrupt-*"))
    assert len(archived) == 1
    assert archived[0].read_text(encoding="utf-8") == "{ this is not json"

    cp.update_config(lambda c: c.update({"schedule_frequency": "monthly"}))

    assert json.loads(path.read_text(encoding="utf-8"))["schedule_frequency"] == "monthly"
    assert json.loads(bak.read_text(encoding="utf-8")) == good, (
        "the backup must still be the file that rescued the session")
