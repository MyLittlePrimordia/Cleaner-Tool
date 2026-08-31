"""
Config persistence for scheduled runs and user preferences.
"""

import copy
import json
import os
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
    "selected_tasks": {
        "Clean": [],
        "Repair": [],
        "Tweak": [],
        "Games": [],
    },
    "preview_mode": False,
}

# In-memory cache
_config_cache: dict | None = None
_config_dirty = False
_config_lock = threading.RLock()


def _load_config_from_disk() -> dict:
    """Load config from disk (internal use). No side-effect dir creation for read-only queries."""
    if CONFIG_FILE.exists():
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
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
            return data
        except Exception:
            pass
    return copy.deepcopy(DEFAULT_CONFIG)


def load_config() -> dict:
    """Load config, using cached version if available. Returns the shared cache (caller must call save_config after mutating). Thread-safe."""
    global _config_cache
    with _config_lock:
        if _config_cache is None:
            _config_cache = _load_config_from_disk()
        return _config_cache


def save_config(config: dict) -> None:
    """Save config to disk atomically (write to temp then replace). Thread-safe. Raises on failure."""
    global _config_cache, _config_dirty
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
        _config_dirty = False


def flush_config() -> None:
    """Flush any pending changes to disk. Thread-safe."""
    global _config_cache, _config_dirty
    to_save = None
    with _config_lock:
        if _config_dirty and _config_cache is not None:
            # Copy under lock to avoid race
            to_save = copy.deepcopy(_config_cache)
    if to_save is not None:
        # save_config will re-acquire lock
        save_config(to_save)


def get_selected_task_keys(config: dict, tab_name: str) -> list:
    # Return a copy to prevent caller mutation bypassing _config_dirty
    with _config_lock:
        val = config.get("selected_tasks", {}).get(tab_name, [])
        return list(val) if isinstance(val, list) else []


def set_selected_task_keys(config: dict, tab_name: str, keys: list) -> None:
    # Copy input list to avoid aliasing with caller's list
    global _config_dirty
    with _config_lock:
        # Validate tab_name to avoid polluting config
        if tab_name not in DEFAULT_CONFIG["selected_tasks"] and tab_name not in ("Clean", "Repair", "Tweak", "Games"):
            # Allow custom but still copy
            pass
        config.setdefault("selected_tasks", {})[tab_name] = list(keys)
        _config_dirty = True