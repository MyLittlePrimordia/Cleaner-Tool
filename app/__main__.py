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

# Ensure repo root is on sys.path when run as `python app/__main__.py`
if __package__ in (None, ""):
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

# Signal elevated startup early (for elevation coordination)
from app.elevation import signal_elevated_startup
signal_elevated_startup()

from app.gui import launch  # noqa: E402
from app.config_persist import load_config  # noqa: E402
from app.scheduler import run_auto_clean  # noqa: E402


def main():
    if not sys.platform.startswith("win"):
        print("This application is designed for Windows only.")
        return

    # Handle --auto-clean flag (called from Windows Task Scheduler)
    if "--auto-clean" in sys.argv:
        config = load_config()
        selected = config.get("selected_tasks", {})
        success, summary = run_auto_clean(selected)
        sys.exit(0 if success else 1)

    launch()


if __name__ == "__main__":
    main()
