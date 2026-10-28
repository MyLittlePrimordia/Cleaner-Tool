"""Hardware / OS interop. Tk-free, so it can be used off the UI thread.

`drives.py` is the one module left here: it samples the fixed drives and is
read by the run engine (`app/ui/runflow.py`), the scorecard and Storage
Insight. The COM audio stack that used to live beside it - `mmdev.py`
(_InputMeter, _PadReportStats) and `capture.py` (endpoint enumeration) - went
with the Mic Check and Speaker Test windows in the 2026-10 sweep, and
`xinput.py` with the Gamepad tester: all three were kept alive only by the
re-export shims in `app/ui/__init__.py` and `app/gui.py`, which is to say by
nothing. Output-only audio is `app/audio_out.py` (registry-based, COM-free).

Each module here is extracted verbatim from app/gui.py and depends only on the
stdlib, never on the widget layer.
"""
from app.ui.hardware import drives

__all__ = ["drives"]
