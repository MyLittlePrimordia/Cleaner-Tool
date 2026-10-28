"""R-006 (finished): each config region lives in its own module.

`config_persist` keeps the loader, the atomic writer, the recovery path and the
lock; the tweak-snapshot, install-profile and startup-record writers moved next
door and are re-exported, so every existing import still resolves to the same
function. These tests pin both halves of that: the definition's new home, and
that the writers still work through the REAL config file (not a fake
update_config), which is what the move could plausibly have broken.
"""
from __future__ import annotations

import inspect
import json

import pytest

import app.config_persist as cp


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    """Point the real loader/saver at a temp config, cleaned up afterwards."""
    path = tmp_path / "config.json"
    monkeypatch.setattr(cp, "CONFIG_FILE", path)
    monkeypatch.setattr(cp, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(cp, "_config_cache", None)
    monkeypatch.setattr(cp, "_config_mtime", None)
    cp._clear_degraded()
    yield path
    cp._clear_degraded()
    cp._config_cache = None
    cp._config_mtime = None


def _on_disk(path):
    return json.loads(path.read_text(encoding="utf-8"))


# --------------------------------------------------------------------------- #
# The move itself
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("name,module", [
    ("get_tweak_snapshot", "config_tweaks"),
    ("save_tweak_snapshot", "config_tweaks"),
    ("merge_tweak_snapshot", "config_tweaks"),
    ("clear_tweak_snapshot", "config_tweaks"),
    ("mark_tweak_applied", "config_tweaks"),
    ("mark_tweak_reverted", "config_tweaks"),
    ("get_tweak_state", "config_tweaks"),
    ("export_tweak_snapshots", "config_tweaks"),
    ("import_tweak_snapshots", "config_tweaks"),
    ("get_install_profiles", "config_profiles"),
    ("save_install_profile", "config_profiles"),
    ("delete_install_profile", "config_profiles"),
    ("get_startup_disabled", "config_startup"),
    ("add_startup_disabled", "config_startup"),
    ("remove_startup_disabled", "config_startup"),
])
def test_the_writer_is_defined_in_its_domain_and_re_exported(name, module):
    assert inspect.getsourcefile(getattr(cp, name)).endswith(module + ".py")


def test_the_domain_modules_do_not_import_back_at_module_scope():
    """A top-level import would cycle: config_persist imports these three."""
    for module in ("app.config_tweaks", "app.config_profiles",
                   "app.config_startup"):
        mod = __import__(module, fromlist=["x"])
        assert mod._cp() is cp


# --------------------------------------------------------------------------- #
# Install profiles
# --------------------------------------------------------------------------- #

def test_a_profile_round_trips_through_the_real_file(cfg):
    from app.config_persist import (delete_install_profile, get_install_profiles,
                                    save_install_profile)

    assert save_install_profile("Work", ["7zip.7zip", "Mozilla.Firefox"])
    assert get_install_profiles() == {"Work": ["7zip.7zip", "Mozilla.Firefox"]}
    assert _on_disk(cfg)["install_profiles"]["Work"] == ["7zip.7zip",
                                                        "Mozilla.Firefox"]

    assert save_install_profile("Work", ["Notepad++.Notepad++"])   # overwrite
    assert get_install_profiles()["Work"] == ["Notepad++.Notepad++"]

    assert delete_install_profile("Work") is True
    assert get_install_profiles() == {}
    assert delete_install_profile("Work") is False       # already gone


# --------------------------------------------------------------------------- #
# Startup records
# --------------------------------------------------------------------------- #

def test_a_startup_record_keeps_the_held_name_and_round_trips(cfg):
    """BUG-016: the whitelist must carry `held_name`, or the restore path
    falls back to guessing and can fail its tamper check."""
    from app.config_persist import (add_startup_disabled, get_startup_disabled,
                                    remove_startup_disabled)

    assert add_startup_disabled({
        "source": "HKCU", "name": "Spotify", "command": "spotify.exe",
        "value_type": "REG_SZ", "held_name": r"C:\Users\U\AppData\held\Spotify.lnk",
    })
    items = get_startup_disabled()
    assert len(items) == 1
    assert items[0]["held_name"] == "Spotify.lnk"     # basename only

    # re-adding the same item replaces, never duplicates
    assert add_startup_disabled({"source": "HKCU", "name": "Spotify",
                                 "command": "spotify.exe", "value_type": "REG_SZ"})
    assert len(get_startup_disabled()) == 1

    assert remove_startup_disabled("HKCU", "Spotify") is True
    assert get_startup_disabled() == []
    assert remove_startup_disabled("HKCU", "Spotify") is False


# --------------------------------------------------------------------------- #
# Tweak snapshots and the applied registry
# --------------------------------------------------------------------------- #

def test_a_snapshot_is_taken_once_merged_and_cleared(cfg):
    from app.config_persist import (clear_tweak_snapshot, get_tweak_snapshot,
                                    merge_tweak_snapshot, save_tweak_snapshot)

    save_tweak_snapshot("gamedvr", {"enabled": 1})
    # a second apply must NOT overwrite the original pre-tweak value
    save_tweak_snapshot("gamedvr", {"enabled": 0})
    assert get_tweak_snapshot("gamedvr") == {"enabled": 1}

    merge_tweak_snapshot("gamedvr", {"service": "auto"})
    assert get_tweak_snapshot("gamedvr") == {"enabled": 1, "service": "auto"}

    clear_tweak_snapshot("gamedvr")
    assert get_tweak_snapshot("gamedvr") == {}
    assert "gamedvr" not in _on_disk(cfg).get("tweak_snapshots", {})


def test_the_applied_registry_round_trips(cfg):
    from app.config_persist import (get_tweak_state, mark_tweak_applied,
                                    mark_tweak_reverted)

    mark_tweak_applied("gamedvr")
    mark_tweak_applied("gamedvr")                       # idempotent
    assert _on_disk(cfg)["applied_tweaks"] == ["gamedvr"]
    assert get_tweak_state() == {"gamedvr": True}

    mark_tweak_reverted("gamedvr")
    assert get_tweak_state() == {}


def test_a_snapshot_bundle_round_trips_through_a_file(cfg, tmp_path):
    from app.config_persist import (export_tweak_snapshots, get_tweak_snapshot,
                                    import_tweak_snapshots, mark_tweak_applied,
                                    save_tweak_snapshot)

    save_tweak_snapshot("gamedvr", {"enabled": 1})
    mark_tweak_applied("gamedvr")
    bundle = tmp_path / "undo.json"

    assert export_tweak_snapshots(str(bundle)) == 1
    written = json.loads(bundle.read_text(encoding="utf-8"))
    assert written["version"] == 1
    assert written["tweak_snapshots"]["gamedvr"] == {"enabled": 1}

    # emptying the config and importing the bundle restores the undo history
    cp.update_config(lambda c: c.update({"tweak_snapshots": {},
                                         "applied_tweaks": []}))
    assert get_tweak_snapshot("gamedvr") == {}
    assert import_tweak_snapshots(str(bundle)) == 1
    assert get_tweak_snapshot("gamedvr") == {"enabled": 1}
    assert "gamedvr" in _on_disk(cfg)["applied_tweaks"]


def test_a_malformed_bundle_is_rejected_before_anything_is_written(cfg, tmp_path):
    from app.config_persist import import_tweak_snapshots

    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({"tweak_snapshots": []}), encoding="utf-8")

    with pytest.raises(ValueError):
        import_tweak_snapshots(str(bad))
    # Nothing was written at all - not even a rewritten config file.
    assert not cfg.exists() or "tweak_snapshots" not in _on_disk(cfg)
