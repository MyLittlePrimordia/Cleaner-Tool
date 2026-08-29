"""
Config persistence for scheduled runs and user preferences.
"""

import json
import os
from pathlib import Path

CONFIG_DIR = Path(os.environ.get("LOCALAPPDATA", "")) / "CleanerTool"
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


def _load_config_from_disk() -> dict:
    """Load config from disk (internal use)."""
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    if CONFIG_FILE.exists():
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            # Merge with defaults for any missing keys
            for k, v in DEFAULT_CONFIG.items():
                data.setdefault(k, v)
            return data
        except Exception:
            pass
    return DEFAULT_CONFIG.copy()


def load_config() -> dict:
    """Load config, using cached version if available."""
    global _config_cache
    if _config_cache is None:
        _config_cache = _load_config_from_disk()
    return _config_cache


def save_config(config: dict) -> None:
    """Save config to disk immediately."""
    global _config_cache, _config_dirty
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(config, f, indent=2)
    _config_cache = config
    _config_dirty = False


def flush_config() -> None:
    """Flush any pending changes to disk."""
    global _config_cache, _config_dirty
    if _config_dirty and _config_cache is not None:
        save_config(_config_cache)


def get_selected_task_keys(config: dict, tab_name: str) -> list:
    return config.get("selected_tasks", {}).get(tab_name, [])


def set_selected_task_keys(config: dict, tab_name: str, keys: list) -> None:
    config.setdefault("selected_tasks", {})[tab_name] = keys
    # Mark as dirty, will be flushed on next explicit save or app exit
    global _config_dirty
    _config_dirty = True