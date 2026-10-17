"""Shared Tk root helper for the GUI tests.

GitHub's ``windows-latest`` runner intermittently fails to initialise Tcl --
``TclError: Can't find a usable init.tcl ... No error`` -- when several
interpreters are created in quick succession (for example while Defender holds
the file for a moment). A couple of immediate retries ride that out.

If Tk is genuinely unavailable we skip, matching the rest of the suite's
"no display" handling so a headless or Tcl-less runner cannot fail the build.
"""
from __future__ import annotations

import gc
import time

import pytest

tk = pytest.importorskip("tkinter")


def make_root():
    """Return a fresh Tk root, or skip the test if Tk/Tcl is unusable."""
    last = None
    for attempt in range(4):
        try:
            return tk.Tk()
        except tk.TclError as exc:      # pragma: no cover - no/broken display
            last = exc
            gc.collect()
            time.sleep(0.25 * (attempt + 1))
    pytest.skip("Tk/Tcl unavailable after retries: %s" % last)  # pragma: no cover
