"""
Central place for app metadata, window sizing, and the color theme.

Phase 2: single dark-navy theme (user asked for dark only). Accent colors
identify tabs: blue = Clean, amber = Repair, purple = Tweak, green = Install,
red = Undo. All keys from the old palette are kept so existing modules
(task dialogs, admin gate, scheduler UI) keep working unchanged.
"""

APP_NAME = "Cleaner Tool"
APP_SHORT_NAME = "Cleaner Tool"
APP_VERSION = "2.0.0"

# --------------------------------------------------------------------------
# Window sizing
# --------------------------------------------------------------------------
# The window used to open at a hardcoded "1040x800". That number is in
# PHYSICAL pixels, so its apparent size depended entirely on the machine:
# on a 2560x1440 display it covered 41% of the width, while the same 1040
# on a 1366x768 laptop filled three quarters of it. Two 1440p users with
# different scaling got visibly different apps from the same build.
#
# Worse, the *test suite* never agreed with either. The DPI opt-in lives in
# app/__main__.py at import time, and the smoke tests import app.gui
# directly -- so every automated run rendered DPI-unaware (tk scaling 1.33)
# while both the source launcher and the frozen .exe render DPI-aware
# (tk scaling 2.0). The tests were validating a layout no user ever saw.
#
# The fix is to stop guessing an absolute size and derive it from the work
# area instead, which is what actually determines how big the window can
# usefully be. compute_window_size() below is PURE -- it takes numbers and
# returns numbers -- so tools/verify_window_size.py can pin the clamping
# behaviour without needing a display.

# Fallback when the work area cannot be read (headless, or a locked-down Tk).
WINDOW_DEFAULT_SIZE = (1280, 900)
# Same value as before, kept as a string because app/gui.py re-exports it
# and the smoke suite imports it by name.
WINDOW_SIZE = "%dx%d" % WINDOW_DEFAULT_SIZE
# The floor the resize grips clamp to (app/ui/widgets/chrome.py). Also the
# floor compute_window_size clamps up to, so a tiny screen still opens at
# something usable rather than something unusable.
WINDOW_MIN_SIZE = (980, 700)
# Ceiling. Without one, a 4K display would open a 3000px-wide window that
# nobody can use. This is also what decides the size on any normal display:
# 0.78 of a 2560px work area is 1996px, well past the ceiling, so 1440p and
# 4K both land on exactly (1280, 900) rather than scaling with the monitor.
WINDOW_MAX_SIZE = (1280, 900)
# Fraction of the work area to occupy. Width is the tighter constraint on
# the Install/Tools tab, so it is allowed to be greedier than the height.
WINDOW_WORKAREA_FRACTION = (0.78, 0.82)


def compute_window_size(work_w, work_h, fraction=WINDOW_WORKAREA_FRACTION,
                        minimum=WINDOW_MIN_SIZE, maximum=WINDOW_MAX_SIZE,
                        fallback=WINDOW_DEFAULT_SIZE):
    """Return the (width, height) the main window should open at.

    Pure function: no Tk, no globals mutated, no I/O. Given a work area it
    returns the size to use, so the clamping rules are unit-testable.

    The work area (screen minus taskbar) is the only input that actually
    predicts how much room the window has. Anything we would hardcode is a
    guess that is wrong on some machine.

    Rules, in order:
      1. a missing or non-positive work area falls back to `fallback`,
         so a headless run never crashes on a geometry call;
      2. scale by `fraction`, then clamp into [minimum, maximum];
      3. never return anything smaller than `minimum` -- a window the user
         cannot resize is a bug, not a small screen.
    """
    try:
        work_w = int(work_w)
        work_h = int(work_h)
    except (TypeError, ValueError):
        return tuple(fallback)
    if work_w <= 0 or work_h <= 0:
        return tuple(fallback)

    frac_w, frac_h = fraction
    width = int(work_w * float(frac_w))
    height = int(work_h * float(frac_h))

    min_w, min_h = minimum
    max_w, max_h = maximum
    width = max(min_w, min(max_w, width))
    height = max(min_h, min(max_h, height))
    return width, height


def resolve_window_size(root=None, default=None):
    """Ask Tk for the usable work area and return a "WxH" geometry string.

    Falls back to `default` (WINDOW_SIZE) whenever the work area cannot be
    determined, so this is always safe to call. `root` is the Tk root; when
    omitted the module-level default root is used.

    The work area is read from SPI_GETWORKAREA rather than screenwidth/
    screenheight because the latter includes the taskbar and the Windows
    taskbar is not always at the bottom -- a side or top taskbar would make
    the window hang off-screen.
    """
    fallback = default or WINDOW_SIZE
    try:
        import ctypes
        import ctypes.wintypes

        target = root
        if target is None:
            import tkinter as _tk
            target = _tk._default_root
        if target is None:
            return fallback

        hwnd = ctypes.windll.user32.GetParent(target.winfo_id())
        if not hwnd:
            hwnd = target.winfo_id()
        rect = ctypes.wintypes.RECT()
        # 48 == SPI_GETWORKAREA
        ok = ctypes.windll.user32.SystemParametersInfoW(
            48, 0, ctypes.byref(rect), 0)
        if not ok:
            return fallback
        work_w = rect.right - rect.left
        work_h = rect.bottom - rect.top
        if work_w <= 0 or work_h <= 0:
            return fallback
        width, height = compute_window_size(work_w, work_h)
        return "%dx%d" % (width, height)
    except Exception:
        # A geometry string is a convenience, never a correctness
        # requirement -- failing to compute one must not stop the app.
        return fallback

COLORS = {
    # Base surfaces (dark navy)
    "bg": "#0D1117",            # window base
    "bg_alt": "#151B24",        # cards / panels
    "bg_widget": "#10141C",     # log well / progress track
    "surface": "#1C2430",       # buttons / controls
    "surface_hover": "#263041",
    "hairline": "#232B38",      # 1px borders
    # Text
    "text": "#EAEFF5",
    "subtext": "#7C8797",
    # Accents — tab identity + statuses
    "accent_green": "#3ED598",  # Clean (mint)
    "accent_teal": "#2EE6A8",   # bright mint (hover variant)
    "accent_yellow": "#F2B84B", # Repair (amber)
    "accent_sky": "#38BDF8",    # Install alt (sky blue — downloads/cloud feel)
    "accent_pink": "#F472B6",   # spare accent (tried for Install, unused)
    "accent_blue": "#7C6CF0",   # Tweak (violet)
    "accent_mauve": "#B48CFF",
    "accent_red": "#F2555F",    # Undo / danger / reboot badge
    "black": "#0B0E13",         # text on bright accent fills
}

FONT_FAMILY = "Segoe UI"
MONO_FONT_FAMILY = "Consolas"

RISK_SAFE = "SAFE"
RISK_ADVANCED = "ADVANCED"
RISK_REBOOT = "REBOOT REQUIRED"
