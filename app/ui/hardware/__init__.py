"""Hardware / OS interop. Tk-free, so it can be used off the UI thread.

Each submodule is extracted verbatim from app/gui.py and depends only on the
stdlib (plus its siblings), never on the widget layer.
"""
from app.ui.hardware import capture, drives, mmdev, xinput

__all__ = ["capture", "drives", "mmdev", "xinput"]
