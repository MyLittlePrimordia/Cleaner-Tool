"""Windows-build compatibility helpers (N-004) + edition detection (LTSC).

Some tweaks only apply to a newer Windows build (say, a Windows 11-only
policy). A task can declare ``min_build`` — the minimum Windows build
number it needs, 0 meaning "any". This module reports the host build and
decides whether a task applies.

The edition half answers "is this LTSC / IoT / Server?" for the Install
tab and the winget bootstrapper. Rule, same as capabilities.py: never
guess from one signal — EditionID, CompositionEditionID and ProductName
are all read from the same registry key and any of them naming an S/SN
branch counts. Unknown (probe failed, non-Windows) means "not LTSC",
never a guess.

Honesty rule: when the host build cannot be read, ``task_supported``
returns True. Hiding a working tweak because a probe failed is a worse
failure than showing one the user can simply leave switched off.
"""
from __future__ import annotations

import functools
import sys

_IS_WINDOWS = sys.platform.startswith("win")


@functools.lru_cache(maxsize=1)
def windows_build() -> "int | None":
    """The host Windows build number (e.g. 22631), or None if unknown.

    Reads CurrentBuildNumber from the registry; falls back to the tail of
    platform.version() ("10.0.22631" -> 22631). Cached for the process."""
    if not _IS_WINDOWS:
        return None
    try:
        import winreg
        key = winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE,
            r"SOFTWARE\Microsoft\Windows NT\CurrentVersion")
        try:
            value, _ = winreg.QueryValueEx(key, "CurrentBuildNumber")
        finally:
            winreg.CloseKey(key)
        build = int(str(value).strip())
        if build > 0:
            return build
    except Exception:
        pass
    try:
        import platform
        parts = platform.version().split(".")
        if len(parts) >= 3:
            build = int(parts[2])
            if build > 0:
                return build
    except Exception:
        pass
    return None


def task_min_build(task) -> int:
    """The task's declared minimum build as a non-negative int."""
    try:
        return max(0, int(getattr(task, "min_build", 0) or 0))
    except (TypeError, ValueError):
        return 0


def task_supported(task, build: "int | None" = None) -> bool:
    """True when `task` applies to this Windows build.

    min_build <= 0 -> applies everywhere. An unknown host build is treated
    as "supported" (display-side fail-open; see the module docstring)."""
    need = task_min_build(task)
    if need <= 0:
        return True
    if build is None:
        build = windows_build()
    if build is None:
        return True
    return build >= need


def unsupported_reason(task, build: "int | None" = None) -> str:
    """A short, honest explanation for a task that does not apply here, or
    "" when it does."""
    if task_supported(task, build):
        return ""
    return f"Needs Windows build {task_min_build(task)} or newer."


#: Registry values that mark an S/SN (LTSC) branch. Matched case-insensitively
#: against EditionID and CompositionEditionID; ProductName only needs to
#: CONTAIN "LTSC" (e.g. "Windows 11 Enterprise LTSC 2024" / IoT variants).
_LTSC_EDITION_IDS = frozenset({
    "enterprises", "enterprisesn", "iotenterprises",
    "iotenterprisesn",
})


def _is_ltsc_match(edition_id: str = "", composition_id: str = "",
                   product_name: str = "") -> bool:
    """Pure LTSC predicate over raw registry strings (test seam: no I/O).

    Any one signal is enough — OEM/converted images have been seen with
    EditionID and CompositionEditionID disagreeing, so requiring agreement
    would miss real LTSC boxes. Empty/unknown strings never match."""
    try:
        if str(edition_id or "").strip().lower() in _LTSC_EDITION_IDS:
            return True
        if str(composition_id or "").strip().lower() in _LTSC_EDITION_IDS:
            return True
        if "ltsc" in str(product_name or "").lower():
            return True
    except Exception:
        pass
    return False


@functools.lru_cache(maxsize=1)
def windows_edition() -> dict:
    """Raw edition strings from HKLM ...\\CurrentVersion, or {} if unreadable.

    Keys: edition_id, composition_id, product_name, display_version.
    Cached for the process; never raises."""
    if not _IS_WINDOWS:
        return {}
    out: dict = {}
    try:
        import winreg
        key = winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE,
            r"SOFTWARE\Microsoft\Windows NT\CurrentVersion")
        try:
            for value_name, out_key in (
                    ("EditionID", "edition_id"),
                    ("CompositionEditionID", "composition_id"),
                    ("ProductName", "product_name"),
                    ("DisplayVersion", "display_version")):
                try:
                    value, _ = winreg.QueryValueEx(key, value_name)
                    if value:
                        out[out_key] = str(value).strip()
                except OSError:
                    continue
        finally:
            winreg.CloseKey(key)
    except Exception:
        pass
    return out


def is_ltsc_edition(edition: "dict | None" = None) -> bool:
    """True when the host Windows is an LTSC / IoT-LTSC branch.

    Unknown (empty probe, non-Windows) is False — "cannot tell" must not
    re-route a Pro box into the LTSC flow. Pass an explicit dict in tests."""
    try:
        ed = windows_edition() if edition is None else (edition or {})
        return _is_ltsc_match(ed.get("edition_id", ""),
                              ed.get("composition_id", ""),
                              ed.get("product_name", ""))
    except Exception:
        return False


def edition_label(edition: "dict | None" = None) -> str:
    """Short human label for banners/logs, e.g. 'Windows 11 Enterprise LTSC'.

    '' when the edition cannot be read — callers fall back to generic copy."""
    try:
        ed = windows_edition() if edition is None else (edition or {})
        name = str(ed.get("product_name", "") or "").strip()
        disp = str(ed.get("display_version", "") or "").strip()
        if name and disp:
            return f"{name} {disp}"
        return name
    except Exception:
        return ""
