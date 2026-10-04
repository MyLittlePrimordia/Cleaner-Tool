"""Process DPI awareness, in one place so every entry point can share it.

Extracted from app/__main__.py (which still re-exports it) because the
smoke suite could not reuse it from there: app/__main__.py applies the
opt-in as an import-time side effect, and the tests import app.gui
directly, so every automated run rendered DPI-unaware while both real
launch paths rendered DPI-aware.

That is not cosmetic. Measured on a 2560x1440 display at 150% scaling:

    DPI-unaware -> tk scaling 1.332
    Per-Monitor V2 -> tk scaling 2.000

A 9pt font is therefore 12px in the tests and 18px in the product. The
suite was asserting layout invariants against geometry no user ever sees,
and a change that broke real rendering could still pass CI.

`enable_dpi_awareness()` is idempotent and safe to call from anywhere; it
returns True only when the process actually ended up DPI-aware, so a
caller can tell whether to expect scaled metrics.
"""

from __future__ import annotations

import sys

# DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2
_DPI_AWARENESS_CONTEXT_PER_MONITOR_V2 = -4
# PROCESS_PER_MONITOR_DPI_AWARE
_PROCESS_PER_MONITOR_DPI_AWARE = 2

# -1 == DPI_AWARENESS_INVALID, used only as an "unknown" sentinel.
_DPI_AWARENESS_INVALID = -1


def is_dpi_aware() -> bool:
    """True when Windows reports this process as not DPI-unaware.

    Deliberately does NOT report PerMonitorV2 specifically: the question
    callers actually care about is "will Tk get scaled metrics", and a
    system-DPI-aware process still gets them.
    """
    if not sys.platform.startswith("win"):
        return True
    try:
        import ctypes

        awareness = ctypes.c_int(_DPI_AWARENESS_INVALID)
        # DPI_AWARENESS => 2
        ctypes.windll.shcore.GetProcessDpiAwareness(
            ctypes.byref(awareness))
        return awareness.value != 0  # 0 == DPI_AWARENESS_UNAWARE
    except Exception:
        # Unknown. Assume aware so we never add a second opt-in attempt
        # on a platform that cannot answer the question.
        return True


def enable_dpi_awareness() -> bool:
    """Opt the process into Per-Monitor V2 DPI awareness.

    Must run BEFORE any Tk window exists. Tk 9 does not opt in by itself,
    and calling this after Tk init has no effect, which is why it is an
    import-time side effect on every entry path (app/__main__.py, and now
    the smoke harness via tools/smoke_gui.py).

    Best-effort by design: ask for Per-Monitor V2, fall back to
    system-DPI aware on older Windows, then fall back to nothing at all.
    Every failure is swallowed -- a missing DPI opt-in degrades rendering
    slightly and must never stop the app from starting. Under the frozen
    exe the embedded manifest already set PMv2 before Python started, so
    the first call fails harmlessly (Windows allows setting awareness once
    per process).

    Returns True when the process is DPI-aware afterwards.
    """
    if not sys.platform.startswith("win"):
        return True
    try:
        import ctypes

        ctypes.windll.user32.SetProcessDpiAwarenessContext(
            ctypes.c_void_p(_DPI_AWARENESS_CONTEXT_PER_MONITOR_V2))
        return True
    except Exception:
        pass
    try:
        import ctypes

        ctypes.windll.shcore.SetProcessDpiAwareness(
            _PROCESS_PER_MONITOR_DPI_AWARE)
        return True
    except Exception:
        pass
    return is_dpi_aware()