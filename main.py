#!/usr/bin/env python3
"""Shim so `python main.py` still works — real entry is app/__main__.py"""
import sys

from app.__main__ import main

if __name__ == "__main__":
    try:
        main()
    except Exception:
        # `python -m app` has this guard; `python main.py` (and every elevated
        # relaunch of it, which runs in a console that vanishes on exit) did
        # not. A crash in the GUI build therefore left no traceback in the
        # startup log — only the app log, which a user reporting "it closed
        # everything" is never asked for. Log both, hold the elevated console
        # long enough to read, then re-raise.
        import traceback

        tb = traceback.format_exc()
        try:
            from app.__main__ import _early_log
            _early_log("Uncaught exception in main:\n" + tb)
        except Exception:
            pass
        try:
            from app.diagnostics import log as _diag_log
            _diag_log("Uncaught exception in main:\n" + tb, level="ERROR")
        except Exception:
            pass
        if "--elevation-token=" in " ".join(sys.argv):
            try:
                import time
                time.sleep(5)
            except Exception:
                pass
        raise

