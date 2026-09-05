"""
Component capability detection — cached, honest "is X present on this PC".

Used by LTSC/missing-component installer tasks so they can:
  * skip with an honest "already installed" instead of re-running,
  * verify after install that the component actually registered,
  * never guess from the Windows EDITION (LTSC vs Pro) — because OEM
    installs vary wildly, we check the component itself. This covers
    LTSC, N editions, and debloated Pro boxes with one mechanism.

All checks are read-only, cheap (registry/AppxPackage/where), and cached
per-process since the app only installs things once per session.
"""

import functools
import subprocess
import sys

IS_WINDOWS = sys.platform.startswith("win")
if IS_WINDOWS:
    import winreg


def _run_ps(cmd: str, timeout: int = 20) -> str:
    """Run a PowerShell one-liner, return stdout ('' on any failure)."""
    try:
        r = subprocess.run(
            ["powershell", "-NoProfile", "-Command", cmd],
            shell=False, capture_output=True, text=True, timeout=timeout,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        return r.stdout or ""
    except Exception:
        return ""


def _appx_version(package_name: str) -> "str | None":
    """Return the registry package key suffix for an installed Appx
    package (contains the version), or None if absent. Presence is all
    callers need. Uses the per-user Appx repository registry — cheaper
    and more robust than shelling to Get-AppxPackage."""
    if not IS_WINDOWS:
        return None
    path = (r"Software\Classes\Local Settings\Software\Microsoft\Windows"
            r"\CurrentVersion\AppModel\Repository\Packages")
    try:
        root = winreg.OpenKey(winreg.HKEY_CURRENT_USER, path)
    except OSError:
        return None
    suffix = None
    i = 0
    try:
        while True:
            try:
                sub = winreg.EnumKey(root, i)
                i += 1
            except OSError:
                break
            if sub.lower().startswith(package_name.lower() + "_"):
                suffix = sub
                break
    finally:
        try:
            winreg.CloseKey(root)
        except Exception:
            pass
    return suffix


@functools.lru_cache(maxsize=None)
def has_store() -> bool:
    """Microsoft Store installed (per-user Appx)."""
    return _appx_version("Microsoft.WindowsStore") is not None


@functools.lru_cache(maxsize=None)
def has_winget() -> bool:
    """winget CLI present (comes with Store's DesktopAppInstaller)."""
    if not IS_WINDOWS:
        return False
    try:
        r = subprocess.run(
            ["where", "winget"], shell=True, capture_output=True, text=True, timeout=10,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        return r.returncode == 0 and "winget" in (r.stdout or "").lower()
    except Exception:
        return False


@functools.lru_cache(maxsize=None)
def has_game_bar() -> bool:
    """Xbox Game Bar (Win+G overlay) installed."""
    return _appx_version("Microsoft.XboxGamingOverlay") is not None


@functools.lru_cache(maxsize=None)
def has_xbox_app() -> bool:
    """Xbox app + Gaming Services (Game Pass support)."""
    return _appx_version("Microsoft.GamingApp") is not None


@functools.lru_cache(maxsize=None)
def has_gaming_services() -> bool:
    """Microsoft Gaming Services (GamingServices — Game Pass install/auth)."""
    return _appx_version("Microsoft.GamingServices") is not None


@functools.lru_cache(maxsize=None)
def has_xbox_identity_provider() -> bool:
    """Xbox Identity Provider — the sign-in piece Game Pass games need.
    Stripped on LTSC; without it Game Pass logins fail even when the
    Xbox app and Gaming Services are present."""
    return _appx_version("Microsoft.XboxIdentityProvider") is not None


@functools.lru_cache(maxsize=None)
def has_av1_codec() -> bool:
    return _appx_version("Microsoft.AV1VideoExtension") is not None


@functools.lru_cache(maxsize=None)
def has_vp9_codec() -> bool:
    return _appx_version("Microsoft.VP9VideoExtensions") is not None


@functools.lru_cache(maxsize=None)
def has_web_media_extension() -> bool:
    """Web Media Extensions — the free OEM HEVC-compatible media pack
    (what we install instead of the $0.99 HEVC extension)."""
    return _appx_version("Microsoft.WebMediaExtension") is not None


def invalidate_caches() -> None:
    """Clear capability caches — call after an installer ran, so presence
    checks reflect the new state."""
    for fn in (has_store, has_winget, has_game_bar, has_xbox_app,
                has_gaming_services, has_av1_codec, has_vp9_codec,
                has_web_media_extension, has_xbox_identity_provider):
        fn.cache_clear()
