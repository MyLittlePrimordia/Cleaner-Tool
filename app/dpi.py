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

# One list, two consumers: the config schema validates against it and Settings
# renders it. config_schema is stdlib-only by contract, so this imports in the
# direction the module documents (nothing in it imports `app`).
from app.config_schema import UI_SCALES

# DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2
_DPI_AWARENESS_CONTEXT_PER_MONITOR_V2 = -4
# PROCESS_PER_MONITOR_DPI_AWARE
_PROCESS_PER_MONITOR_DPI_AWARE = 2

#: Minimum UI density, in Tk's points-per-pixel scale. Tk's 96-DPI default is
#: 1.333; 1.667 is 120 DPI, the 125% step Windows itself offers. The earlier
#: 108-DPI (1.5) floor was a half-step between Windows' own rungs: it still
#: left 9pt captions and the 24px control glyphs reading small on a 1080p or
#: 1440p panel left at 100%, which is the density most desktops actually run.
#: 125% is the first rung that matches what the neighbouring apps in the task
#: bar render at, and it is still a FLOOR - on a 125% or 200% display Tk
#: already reports a larger scale, so high-DPI users keep exactly the density
#: their OS chose.
MIN_TK_SCALING = 120.0 / 72.0

#: The values the "Interface scale" setting can hold. "system" keeps the
#: floor above (follow the display, never below 125%); a number forces that
#: Windows density exactly, including 100% on a display whose owner prefers
#: the smaller text. The labels in Settings are the same percentages Windows
#: shows under Display > Scale, so a user can match the two windows up.
#: (Aliased rather than re-declared: a second literal list is exactly how the
#: schema and the UI drift apart.)
UI_SCALE_CHOICES = UI_SCALES

#: Windows percentage -> Tk `tk scaling`. Tk measures points per PIXEL, so
#: 96 DPI is 96/72. One table, one conversion; the setting stores the
#: percentage rather than the raw float so a config file stays readable and
#: a future Tk change cannot silently reinterpret a stored scale.
_TK_SCALING_BY_PERCENT = {
    "100": 96.0 / 72.0,
    "125": MIN_TK_SCALING,
    "150": 144.0 / 72.0,
    "175": 168.0 / 72.0,
    "200": 192.0 / 72.0,
}


def target_tk_scaling(choice, current):
    """Return the Tk scaling `choice` asks for, given the display's `current`.

    Pure, so the mapping is testable without a display:

      * "system" (or an absent/unknown value, e.g. from a newer build's config)
        follows the display but never drops below MIN_TK_SCALING.
      * "100".."200" force that Windows percentage EXACTLY. That is the point
        of the setting: 100% is allowed even though the default floor is 125%.

    A nonsense `current` still resolves to a usable number rather than raising.
    """
    try:
        current = float(current)
    except (TypeError, ValueError):
        current = 0.0
    key = str(choice or "system").strip().lower()
    if key.endswith("%"):
        key = key[:-1].strip()
    if key in _TK_SCALING_BY_PERCENT:
        return _TK_SCALING_BY_PERCENT[key]
    return max(current, MIN_TK_SCALING)


# -1 == DPI_AWARENESS_INVALID, used only as an "unknown" sentinel.
_DPI_AWARENESS_INVALID = -1


def apply_ui_density(root, choice="system"):
    """Set the window's point scale to the density `choice` asks for.

    This is UI density, not a DPI-awareness call: the process may already be
    Per-Monitor V2 and still render a 100% display at Tk's 1.333 scale, which
    makes 9-11pt type and 24px controls read small on a large monitor.
    Changing it here enlarges every font AND every icon (icons size from
    `winfo_fpixels`, which follows this value), and the window itself grows
    through app.config.device_scale, so text, marks and layout all move
    together.

    `choice` is one of UI_SCALE_CHOICES (default "system" = the historical
    behaviour: follow the display, floor at MIN_TK_SCALING).

    Returns True only when the scale actually changed. Idempotent. The caller
    is the GUI entry point, before the first window is mapped; changing Tk's
    scale after widgets exist would move existing geometry mid-frame, which is
    why the Settings page tells the user to restart.
    """
    try:
        current = float(root.tk.call("tk", "scaling"))
    except Exception:
        return False
    target = target_tk_scaling(choice, current)
    # Tk quantizes scaling to the display's pixel grid, so a repeated call can
    # read back a thousandth off the value it was handed. Treat anything that
    # close as already applied rather than rewriting it every launch.
    if abs(current - target) <= 0.01:
        return False
    try:
        root.tk.call("tk", "scaling", target)
    except Exception:
        return False
    return True


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
    import-time side effect on every entry path (app/__main__.py, before
    the GUI is imported).

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