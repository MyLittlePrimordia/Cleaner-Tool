"""Install profiles ("My Setups"): the named package lists a user can replay.

Split out of `app.config_persist` (R-006, continued) for the same reason as
`config_history` and `config_tweaks`: this is one config region
(`install_profiles`) with its own rules — a 60-character name, a per-profile
item cap, an overall cap that refuses rather than silently evicting — and
those rules belong next to each other instead of in the middle of the loader.

`app.config_persist` re-exports all three names, so existing importers are
unchanged. The config doors are fetched at call time to avoid the cycle
described in `config_tweaks`.
"""
from __future__ import annotations

from app.config_schema import PROFILE_ITEMS_MAX, PROFILE_MAX


def _cp():
    from app import config_persist
    return config_persist


def get_install_profiles() -> dict:
    """{name: [package ids]} — always a plain dict, never None."""
    try:
        profiles = _cp().load_config().get("install_profiles")
        return dict(profiles) if isinstance(profiles, dict) else {}
    except Exception:
        return {}


def save_install_profile(name: str, ids: "list[str]") -> bool:
    """Create or overwrite one profile. True when it was stored.

    Single-lock read-modify-write (H9 contract) so a save from the dialog
    can't clobber a concurrent one."""
    try:
        name = str(name or "").strip()[:60]
        if not name:
            return False
        clean = [str(i) for i in (ids or []) if str(i).strip()][:PROFILE_ITEMS_MAX]

        def _mut(cfg):
            profiles = cfg.get("install_profiles")
            if not isinstance(profiles, dict):
                profiles = {}
            if name not in profiles and len(profiles) >= PROFILE_MAX:
                # full: drop nothing silently — refuse instead (the dialog
                # tells the user to delete one first)
                raise ValueError("profile limit reached")
            profiles[name] = clean
            cfg["install_profiles"] = profiles

        _cp().update_config(_mut)
        return True
    except Exception:
        return False


def delete_install_profile(name: str) -> bool:
    """Remove one profile. True when something was removed."""
    try:
        name = str(name or "").strip()
        if not name:
            return False
        removed = {"hit": False}

        def _mut(cfg):
            profiles = cfg.get("install_profiles")
            if isinstance(profiles, dict) and name in profiles:
                profiles.pop(name, None)
                cfg["install_profiles"] = profiles
                removed["hit"] = True

        _cp().update_config(_mut)
        return removed["hit"]
    except Exception:
        return False
