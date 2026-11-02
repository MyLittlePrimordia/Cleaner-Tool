"""N-002: portable setup bundle -- your configuration as one file.

A gamer rebuilds PCs: fresh Windows, new drive, LTSC experiment. Re-ticking
40 boxes by hand is the tax. This module exports the *choices* (which Clean /
Repair / Tweak / Install items are ticked, saved app sets, Pilot watch
lists, Game Mode close lists) to one versioned JSON file, and imports such
a file back through validation.

Deliberately NOT portable: applied-tweak snapshots and restore points are
machine state (registry values from THIS Windows), and the tweak backup
(Settings Tweaks and backup) already covers carrying those. Importing
someone else's registry values onto your Windows would violate the safety
contract, so this bundle carries intentions, never machine state.

Honesty rules:

* unknown task keys / tabs in an imported file are dropped AND counted;
* install profiles are re-validated with the schema's own bounds;
* pilot/game lists are stored as plain text and sanitised on use;
* export never includes tokens or machine identifiers -- ids only.
"""
from __future__ import annotations

import json
import time

VERSION = 1

KEYS = (
    "selected_tasks",
    "install_profiles",
    "session_pilot_enabled",
    "session_pilot_preset",
    "session_pilot_games",
    "session_pilot_paths",
    "session_pilot_names",
    "session_pilot_watch_all",
    "game_night_close_apps",
    "game_night_custom_apps",
)

PROFILE_MAX = 20
PROFILE_ITEMS_MAX = 400
PROFILE_NAME_MAX = 60


def _str_list(value, cap=400):
    out = []
    if isinstance(value, (list, tuple)):
        for item in value:
            if isinstance(item, str) and item.strip():
                out.append(item.strip())
                if len(out) >= cap:
                    break
    return out


def export_setup():
    """Current portable choices as a plain dict. Never raises."""
    data = {"version": VERSION, "exported_at": int(time.time()),
            "app": "Cleaner Tool setup"}
    try:
        from app.config_persist import load_config
        cfg = load_config() or {}
    except Exception:
        cfg = {}
    try:
        from app.config_profiles import get_install_profiles
        profiles = get_install_profiles()
    except Exception:
        profiles = {}
    payload = {}
    for key in KEYS:
        if key == "install_profiles":
            payload[key] = profiles if isinstance(profiles, dict) else {}
        else:
            payload[key] = cfg.get(key)
    data["setup"] = payload
    return data


def _filter_selected_tasks(raw):
    kept, dropped = {}, 0
    try:
        from app.tab_presets import TABS
        valid = {tab: {t.key for t in (tasks or [])}
                 for tab, tasks in (TABS or {}).items()}
    except Exception:
        return {}, 0
    if not isinstance(raw, dict):
        return {}, 0
    for tab, keys in raw.items():
        if tab not in valid or not isinstance(keys, (list, tuple)):
            try:
                dropped += len(keys or [])
            except Exception:
                dropped += 1
            continue
        good = [k for k in keys
                if isinstance(k, str) and k in valid[tab]]
        dropped += sum(1 for k in (keys or []) if k not in good)
        if good:
            kept[tab] = good
    return kept, dropped


def _filter_profiles(raw):
    kept, dropped = {}, 0
    if not isinstance(raw, dict):
        return {}, 0
    items = list(raw.items())
    for name, ids in items:
        if len(kept) >= PROFILE_MAX:
            dropped += 1
            continue
        if not isinstance(name, str) or not name.strip():
            dropped += 1
            continue
        name = name.strip()[:PROFILE_NAME_MAX]
        if not isinstance(ids, (list, tuple)):
            dropped += 1
            continue
        clean = [i for i in ids
                 if isinstance(i, str) and i.strip()][:PROFILE_ITEMS_MAX]
        dropped += max(0, len([i for i in ids if isinstance(i, str)]) -
                       len(clean))
        if not clean:
            dropped += 1
            continue
        kept[name] = clean
    return kept, dropped


def import_setup(data):
    """Apply an exported bundle. Returns a report dict.

    Report: applied {key: count}, dropped int, errors [...].
    Unknown keys/tabs/profiles are dropped and counted, never applied.
    Never raises: a corrupt file reports, it does not break config.
    """
    report = {"applied": {}, "dropped": 0, "errors": []}
    try:
        setup = (data or {}).get("setup")
        if not isinstance(setup, dict):
            report["errors"].append("Not a setup file (no setup section).")
            return report
    except Exception:
        report["errors"].append("Could not read the setup file.")
        return report
    try:
        from app.config_persist import update_config
    except Exception as exc:
        report["errors"].append("Could not open settings: " + str(exc))
        return report
    mutations = {}
    selected, dropped = _filter_selected_tasks(setup.get("selected_tasks"))
    report["dropped"] += dropped
    if selected:
        mutations["selected_tasks"] = selected
    profiles, dropped = _filter_profiles(setup.get("install_profiles"))
    report["dropped"] += dropped
    if profiles:
        mutations["install_profiles"] = profiles
    for key in ("session_pilot_games", "session_pilot_paths"):
        items = _str_list(setup.get(key), cap=400)
        if items:
            mutations[key] = items
    names = setup.get("session_pilot_names")
    if isinstance(names, dict):
        clean_names = {str(k): str(v) for k, v in names.items()
                       if isinstance(k, str) and isinstance(v, str)}
        if clean_names:
            mutations["session_pilot_names"] = clean_names
    for key in ("session_pilot_enabled", "session_pilot_watch_all"):
        value = setup.get(key)
        if isinstance(value, bool):
            mutations[key] = value
    preset = setup.get("session_pilot_preset")
    if isinstance(preset, str) and preset.strip():
        try:
            from app.tab_presets import PRESETS
            tweak_presets = PRESETS.get("Tweak", {})
            if preset.strip() in tweak_presets:
                mutations["session_pilot_preset"] = preset.strip()
            else:
                report["dropped"] += 1
        except Exception:
            report["dropped"] += 1
    for key in ("game_night_close_apps", "game_night_custom_apps"):
        items = _str_list(setup.get(key), cap=100)
        if items:
            mutations[key] = items
    if not mutations:
        if not report["errors"] and not report["dropped"]:
            report["errors"].append("Nothing usable in this file.")
        return report
    patch = dict(mutations)
    try:
        def _mut(cfg, _m=patch):
            for k, v in _m.items():
                cfg[k] = v
        update_config(_mut)
        report["applied"] = {
            k: (len(v) if isinstance(v, (list, dict)) else 1)
            for k, v in mutations.items()}
    except Exception as exc:
        report["errors"].append("Could not save settings: " + str(exc))
    return report


def default_name():
    try:
        return "cleaner-tool-setup-" + time.strftime("%Y%m%d") + ".json"
    except Exception:
        return "cleaner-tool-setup.json"


def write(data, path):
    text = json.dumps(data, indent=2, sort_keys=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)


def read(path):
    with open(path, "r", encoding="utf-8") as fh:
        data = json.load(fh)
    if not isinstance(data, dict):
        raise ValueError("Not a setup file.")
    return data
