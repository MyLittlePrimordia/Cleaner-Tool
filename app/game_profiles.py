"""Per-game tweak profiles (N-003): one exe, one small set of tweak keys.

Game Night already applies a fixed preset. A profile lets the same session
start with the settings a particular game wants — "cs2.exe" with the GPU
scheduler and pointer precision off, "bg3.exe" with nothing but the power
plan — and Auto-Pilot picks the match from the process it already watches.

The store is bounded by the schema, and the module refuses an empty name or
an empty key list rather than saving a profile that can never do anything.
Matching is case-insensitive and by basename only: a full path is reduced, so
moving the game does not silently detach its profile.
"""
from __future__ import annotations

from app.config_schema import (
    GAME_PROFILE_EXE_MAX,
    GAME_PROFILE_KEY_MAX,
    GAME_PROFILE_KEYS_MAX,
    GAME_PROFILE_NAME_MAX,
    GAME_PROFILES_MAX,
)


def _clean_name(name) -> str:
    return " ".join(str(name or "").split())[:GAME_PROFILE_NAME_MAX]


def _clean_exe(exe) -> str:
    text = str(exe or "").strip().replace("\\", "/")
    return text.rsplit("/", 1)[-1].strip()[:GAME_PROFILE_EXE_MAX].lower()


def _clean_keys(keys) -> list:
    out = []
    for key in keys or []:
        if not isinstance(key, str):
            continue
        text = key.strip()[:GAME_PROFILE_KEY_MAX]
        if text and text not in out:
            out.append(text)
        if len(out) >= GAME_PROFILE_KEYS_MAX:
            break
    return out


def profiles() -> dict:
    """The stored table, or {} when the file has nothing readable."""
    try:
        from app.config_persist import load_config
        table = load_config().get("game_profiles")
    except Exception:  # noqa: BLE001 — a bad config must not break a start
        return {}
    return table if isinstance(table, dict) else {}


def save(name, exe, keys) -> bool:
    """Create or replace one profile. False when it would be a no-op entry."""
    label = _clean_name(name)
    clean = _clean_keys(keys)
    if not label or not clean:
        return False
    try:
        from app.config_persist import update_config

        def _mut(cfg):
            table = cfg.get("game_profiles")
            if not isinstance(table, dict):
                table = {}
            # Same name in different case is the same profile, not a twin.
            for existing in [k for k in table if k.lower() == label.lower()]:
                table.pop(existing, None)
            table[label] = {"exe": _clean_exe(exe), "keys": clean}
            # Oldest insertion goes first; dict order is the store order.
            while len(table) > GAME_PROFILES_MAX:
                table.pop(next(iter(table)))
            cfg["game_profiles"] = table

        update_config(_mut)
        return True
    except Exception:  # noqa: BLE001 — the caller gets a plain False
        return False


def delete(name) -> bool:
    """Remove one profile by name (case-insensitive). False if none matched."""
    label = _clean_name(name)
    if not label:
        return False
    removed = [False]
    try:
        from app.config_persist import update_config

        def _mut(cfg):
            table = cfg.get("game_profiles")
            if not isinstance(table, dict):
                return
            for existing in [k for k in table if k.lower() == label.lower()]:
                table.pop(existing, None)
                removed[0] = True
            cfg["game_profiles"] = table

        update_config(_mut)
    except Exception:  # noqa: BLE001 — the caller gets a plain False
        return False
    return removed[0]


def keys_for(name) -> list:
    """Tweak keys stored under `name`, newest copy wins. [] when absent."""
    label = _clean_name(name)
    if not label:
        return []
    for existing, row in profiles().items():
        if existing.lower() == label.lower():
            keys = row.get("keys") if isinstance(row, dict) else None
            return list(keys) if isinstance(keys, list) else []
    return []


def for_exe(exe) -> list:
    """Keys for the profile bound to this process name. [] when none match."""
    wanted = _clean_exe(exe)
    if not wanted:
        return []
    for row in profiles().values():
        if isinstance(row, dict) and _clean_exe(row.get("exe")) == wanted:
            keys = row.get("keys")
            return list(keys) if isinstance(keys, list) else []
    return []


__all__ = ["delete", "for_exe", "keys_for", "profiles", "save"]
