"""
Config persistence for scheduled runs and user preferences.
"""

import copy
import json
import os
import sys
import threading
from pathlib import Path

def _get_config_dir() -> Path:
    base = os.environ.get("LOCALAPPDATA", "")
    if not base or not os.path.isdir(base):
        # Fallback to home or temp if LOCALAPPDATA missing (service / test context)
        base = os.path.expanduser("~") or os.environ.get("TEMP", "") or "."
    return Path(base) / "CleanerTool"

CONFIG_DIR = _get_config_dir()
CONFIG_FILE = CONFIG_DIR / "config.json"

DEFAULT_CONFIG = {
    "schedule_enabled": False,
    "schedule_frequency": "weekly",  # daily, weekly, monthly
    "schedule_time": "03:00",
    # Phase 2 (#12): 5 tabs merged to 3 — Games folded into Clean, Advanced
    # into Tweak. Kept as an explicit dict so load_config can migrate old
    # saved configs (see _migrate_selected_tasks).
    "selected_tasks": {
        "Clean": [],
        "Repair": [],
        "Tweak": [],
    },
    # Real pre-tweak values captured at apply-time, keyed by task id, so
    # revert can restore the machine's actual prior setting instead of a
    # hardcoded guess. Each entry is {"subgroup:setting": index, ...}.
    "tweak_snapshots": {},
    # Phase 2 (#14): tweaks the app has applied and not yet reverted —
    # the source for the UI's "Applied" badges, kept distinct from the
    # on/off state of any toggle. Snapshots (above) also imply "applied".
    "applied_tweaks": [],
}

# Phase 2 (#12): mapping used to migrate configs saved by the old 5-tab UI.
# Games-tab twins -> the Clean-tab task that cleans the identical paths
# (see tab_presets._GAMES_TO_CLEAN_DEDUPE); unique Games tasks move to
# Clean as-is. Advanced tasks move to Tweak; the three cut tasks
# (punch-list #13) are dropped, not migrated. Duplicated here instead of
# importing from tab_presets to avoid a circular import
# (tab_presets -> tweak_tasks -> config_persist).
_LEGACY_GAMES_TO_CLEAN = {
    "gamer_launchers": "launcher_cache",
    "gpu_shader_caches": "shader_cache",
}
_LEGACY_CUT_KEYS = {"adv_memory_integrity", "adv_vmp", "wpbt_disable"}


def _migrate_selected_tasks(data: dict) -> dict:
    """Fold legacy 5-tab selected_tasks into the 3-tab layout, in place."""
    st = data.get("selected_tasks")
    if not isinstance(st, dict):
        return data
    clean = st.setdefault("Clean", [])
    tweak = st.setdefault("Tweak", [])
    # audit fix (probe-confirmed): a hand-edited config could store a task
    # list as a plain string ("gamer_launchers") — iterating it scattered
    # per-character keys ['g','a','m','e','r',...] into the list. Coerce
    # every tab's list to an actual list of strings first.
    for tab_name in ("Clean", "Tweak"):
        if isinstance(st.get(tab_name), str):
            st[tab_name] = [st[tab_name]]
    for legacy_tab in ("Games", "Advanced"):
        keys = st.pop(legacy_tab, []) or []
        if isinstance(keys, str):
            keys = [keys]
        if legacy_tab == "Games":
            for key in keys:
                mapped = _LEGACY_GAMES_TO_CLEAN.get(key, key)
                if mapped not in clean:
                    clean.append(mapped)
        else:
            for key in keys:
                if key in _LEGACY_CUT_KEYS:
                    continue
                if key not in tweak:
                    tweak.append(key)
    return data

# In-memory cache
_config_cache: dict | None = None
_config_lock = threading.RLock()


def _quarantine_corrupt_config(exc: Exception) -> None:
    """Move an unreadable/corrupt config.json aside so its contents are
    not lost and the next save doesn't silently overwrite the only copy.
    Best-effort: a failed rename must never crash startup. A visible
    warning goes to stderr (config_persist is imported by every entry
    point including the headless scheduler; GUI users see the effects as
    a reset to defaults, hence the console note)."""
    target = None
    try:
        from datetime import datetime
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        target = CONFIG_FILE.with_name(f"config.json.corrupt-{stamp}")
        os.replace(CONFIG_FILE, target)
    except Exception:
        target = None
    where = f"quarantined to {target}" if target else "could NOT be moved aside (keeping it in place)"
    print(f"[CleanerTool] config_persist: {CONFIG_FILE} is corrupt or unreadable "
          f"({type(exc).__name__}: {exc}) — {where}. Starting with defaults.", file=sys.stderr)


def _load_config_from_disk() -> dict:
    """Load config from disk (internal use). No side-effect dir creation for read-only queries."""
    if CONFIG_FILE.exists():
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, dict):
                raise ValueError("config.json root is not a JSON object")
            # Merge with defaults for any missing keys (deep merge for selected_tasks)
            for k, v in DEFAULT_CONFIG.items():
                if k not in data:
                    data[k] = copy.deepcopy(v)
                elif isinstance(v, dict) and isinstance(data[k], dict):
                    for sub_k, sub_v in v.items():
                        data[k].setdefault(sub_k, copy.deepcopy(sub_v))
            # Ensure selected_tasks has all tabs
            if "selected_tasks" in data and isinstance(data["selected_tasks"], dict):
                for tab in DEFAULT_CONFIG["selected_tasks"]:
                    data["selected_tasks"].setdefault(tab, [])
                _migrate_selected_tasks(data)
            return data
        except FileNotFoundError:
            pass  # file vanished between exists() and open() — treat as missing
        except Exception as exc:
            # M5 audit fix: this swallowed EVERY exception (incl. a corrupt
            # JSON body) and silently returned defaults, wiping the user's
            # applied-tweak registry / snapshots / selections in memory.
            # Only a genuinely missing file returns defaults silently; a
            # corrupt file is quarantined (rename aside) with a warning.
            _quarantine_corrupt_config(exc)
    return copy.deepcopy(DEFAULT_CONFIG)


def load_config() -> dict:
    """Load config, using cached version if available. Returns a fresh copy so caller mutations never touch the cache without save_config. Thread-safe."""
    global _config_cache
    with _config_lock:
        if _config_cache is None:
            _config_cache = _load_config_from_disk()
        return copy.deepcopy(_config_cache)


def save_config(config: dict) -> None:
    """Save config to disk atomically (write to temp then replace). Thread-safe. Raises on failure."""
    global _config_cache
    # (audit fix: the old `global` statement also named _config_dirty, a
    # variable that never existed anywhere else — removed.)
    with _config_lock:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        # Use .json.tmp to avoid with_suffix clobbering (config.json -> config.tmp)
        tmp = CONFIG_FILE.with_name(CONFIG_FILE.name + ".tmp")
        # Write to temp
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(config, f, indent=2)
            try:
                f.flush()
                os.fsync(f.fileno())
            except Exception:
                pass
        # Atomic replace
        try:
            os.replace(tmp, CONFIG_FILE)
        except Exception as e:
            # Cleanup temp on failure and propagate
            try:
                if tmp.exists():
                    tmp.unlink()
            except Exception:
                pass
            raise
        # Directory fsync for durability (Windows: ensure directory entry flushed)
        try:
            # On Windows, opening directory for fsync is not supported; ignore
            dir_fd = os.open(str(CONFIG_DIR), os.O_RDONLY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
        except Exception:
            pass
        # Store a deepcopy to avoid aliasing with caller's mutable dict
        _config_cache = copy.deepcopy(config)


# --------------------------------------------------------------------------- #
# Tweak snapshot helpers — real pre-tweak values for accurate revert
# --------------------------------------------------------------------------- #

def get_tweak_snapshot(task_id: str) -> dict:
    """Return the saved pre-apply values for a tweak task, or {} if none."""
    cfg = load_config()
    return cfg.get("tweak_snapshots", {}).get(task_id, {})


def save_tweak_snapshot(task_id: str, values: dict) -> None:
    """Save pre-apply values for a tweak task, but only if none exist yet —
    if the tweak is applied again while an unreverted snapshot is already
    on file, keep the ORIGINAL pre-tweak value, not the value from the last
    apply (which would just re-snapshot the tweak's own output)."""
    if not values:
        return
    cfg = load_config()
    snapshots = cfg.setdefault("tweak_snapshots", {})
    if task_id in snapshots and snapshots[task_id]:
        return
    snapshots[task_id] = values
    save_config(cfg)


def clear_tweak_snapshot(task_id: str) -> None:
    """Drop a tweak's saved snapshot after a successful revert, so the next
    apply starts capturing fresh again."""
    cfg = load_config()
    snapshots = cfg.get("tweak_snapshots", {})
    if task_id in snapshots:
        del snapshots[task_id]
        save_config(cfg)


# --------------------------------------------------------------------------- #
# Applied-tweak registry — Phase 2 (#14)
# --------------------------------------------------------------------------- #
# The "Applied" badge must show what is ACTUALLY ACTIVE on this PC, not the
# toggle's on/off state. Two sources imply "applied":
#   * tweak_snapshots — powercfg tweaks snapshot their prior value on apply
#     (only max_performance_gpu / max_cpu_power use this today), and the
#     snapshot is cleared on successful revert;
#   * applied_tweaks — the explicit registry the GUI marks after a tweak
#     run succeeds and clears after its revert succeeds.
# get_tweak_state() merges both in a single config load.

def mark_tweak_applied(task_id: str) -> None:
    cfg = load_config()
    applied = cfg.setdefault("applied_tweaks", [])
    if task_id not in applied:
        applied.append(task_id)
        save_config(cfg)


def mark_tweak_reverted(task_id: str) -> None:
    cfg = load_config()
    applied = cfg.get("applied_tweaks", [])
    if task_id in applied:
        applied.remove(task_id)
        save_config(cfg)


def get_tweak_state() -> dict:
    """One-shot read of which tweak ids are currently applied. Returns
    {task_id: True}. Kept as a plain dict (not a set) so callers can cheaply
    re-read after a run; loads config once, not per tweak."""
    cfg = load_config()
    state = {tid: True for tid in cfg.get("tweak_snapshots", {})}
    state.update({tid: True for tid in cfg.get("applied_tweaks", [])})
    return state