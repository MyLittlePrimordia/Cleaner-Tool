"""Windows-build compatibility helpers (N-004).

Some tweaks only apply to a newer Windows build (say, a Windows 11-only
policy). A task can declare ``min_build`` — the minimum Windows build
number it needs, 0 meaning "any". This module reports the host build and
decides whether a task applies.

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
