#!/usr/bin/env python3
"""
Cleaner Tool 🧼
--------------
Entry point.
  python -m app
  python app/__main__.py
  python main.py  (shim at repo root)

Supports running as a script (app/__main__.py) or as a module (-m).
"""

import sys
import pathlib


def _enable_dpi_awareness() -> None:
    """Opt the process into Per-Monitor V2 DPI awareness BEFORE any Tk
    window exists (perf finding F1).

    The frozen exe embeds a PerMonitorV2 manifest (write_manifest.py), but
    running from source (`python main.py`) has no manifest, so the process
    stays DPI-unaware and DWM bitmap-stretches every repaint at >100%
    display scaling — smearing/ghosting while scrolling. Tk 9 does NOT opt
    in by itself, and calling this after Tk init has no effect, so it runs
    at import time on every entry path (this module is imported before
    launch() by main.py / `python app/__main__.py` / `python -m app`).

    Best-effort: ask for Per-Monitor V2, fall back to system-DPI aware on
    older Windows, and swallow failures. Under the frozen exe the manifest
    already set PMv2, so the call fails harmlessly (Windows only allows
    setting awareness once per process)."""
    if not sys.platform.startswith("win"):
        return
    try:
        import ctypes
        ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))  # PMv2
        return
    except Exception:
        pass
    try:
        import ctypes
        ctypes.windll.shcore.SetProcessDpiAwareness(2)  # system-DPI aware
    except Exception:
        pass


_enable_dpi_awareness()

# Ensure repo root is on sys.path when run as `python app/__main__.py`
if __package__ in (None, ""):
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from app.config_persist import load_config  # noqa: E402
from app.scheduler import run_auto_clean, run_auto_update  # noqa: E402
# M16: gui (4785-line Tk import) used to load even for headless
# --auto-clean/--auto-update runs that never create a window. Import it
# lazily inside main() so the scheduler stays light and decoupled from Tk.
def _launch_gui():
    from app.gui import launch as _launch  # noqa: E402
    _launch()

# Signal elevated startup early (for elevation coordination), but only on Windows
# and after imports so --elevation-token is already in sys.argv.
if sys.platform.startswith("win"):
    try:
        from app.elevation import signal_elevated_startup
        signal_elevated_startup()
    except Exception:
        pass


def main():
    if not sys.platform.startswith("win"):
        print("This application is designed for Windows only.")
        return

    # M16: both flags present used to silently drop --auto-update (first
    # `if` won). Run both in order when combined.
    want_clean = "--auto-clean" in sys.argv
    want_update = "--auto-update" in sys.argv
    if want_clean or want_update:
        code = 0
        if want_clean:
            config = load_config()
            selected = config.get("selected_tasks", {})
            success, summary = run_auto_clean(selected)
            print(f"[AUTO] clean: {summary}")
            code |= 0 if success else 1
        if want_update:
            success, summary = run_auto_update()
            print(f"[AUTO] update: {summary}")
            code |= 0 if success else 1
        sys.exit(code)

    _launch_gui()


if __name__ == "__main__":
    main()
