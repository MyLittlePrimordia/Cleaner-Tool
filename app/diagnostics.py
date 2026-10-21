"""Crash logging and the diagnostics report.

Why this exists (audit 2026-10): the app had no `sys.excepthook`, no
`threading.excepthook` and no Tk `report_callback_exception` handler. In the
windowed frozen build stderr goes nowhere, so an unhandled callback exception
simply vanished. That is how a guaranteed `NameError` on the manual-run save
path could ship without a single report.

Design rules:
  * stdlib logging only, one rotating file under the per-user config dir;
  * every hook chains to the previous handler, so Python's normal stderr
    output (and any debugger) still works;
  * logging must never raise into the app — every public function is
    best-effort and returns rather than throws;
  * the log is local only. There is no network telemetry anywhere in this
    module, deliberately: privacy is a product feature.

`install_hooks()` is idempotent, so both the entry point (before Tk) and
`Application.__init__` (to capture Tk callbacks) can call it.
"""
from __future__ import annotations

import logging
import logging.handlers
import os
import platform
import sys
import threading
import traceback
from typing import Optional

from app.config import APP_NAME, APP_VERSION

_LOGGER_NAME = "cleaner_tool"
_MAX_BYTES = 512 * 1024
_BACKUPS = 2
_installed = False
_previous = {}


def log_dir() -> str:
    """Directory for the crash log. CLEANER_TOOL_LOG_DIR overrides it (tests)."""
    override = os.environ.get("CLEANER_TOOL_LOG_DIR")
    if override:
        return override
    try:
        from app.config_paths import CONFIG_DIR
        return str(CONFIG_DIR / "logs")
    except Exception:
        import tempfile
        return os.path.join(tempfile.gettempdir(), "CleanerTool", "logs")


def log_path() -> str:
    return os.path.join(log_dir(), "cleaner-tool.log")


def get_logger() -> logging.Logger:
    """The app logger, configured with one rotating file handler."""
    logger = logging.getLogger(_LOGGER_NAME)
    if not any(isinstance(h, logging.handlers.RotatingFileHandler)
               and getattr(h, "_cleaner_tool", False)
               for h in logger.handlers):
        try:
            os.makedirs(log_dir(), exist_ok=True)
            handler = logging.handlers.RotatingFileHandler(
                log_path(), maxBytes=_MAX_BYTES, backupCount=_BACKUPS,
                encoding="utf-8")
            handler._cleaner_tool = True
            handler.setFormatter(logging.Formatter(
                "%(asctime)s %(levelname)-7s pid=%(process)d "
                "%(threadName)s %(message)s"))
            logger.addHandler(handler)
            logger.setLevel(logging.INFO)
            # Never duplicate into the root logger / stderr in the GUI build.
            logger.propagate = False
        except Exception:
            # A read-only profile must not stop the app from running.
            pass
    return logger


def log(message: str, level: int = logging.INFO) -> None:
    """Write one line to the crash log. Never raises."""
    try:
        get_logger().log(level, "%s", message)
    except Exception:
        pass


def log_exception(exc_type, exc, tb, source: str = "unhandled") -> None:
    """Record a full traceback plus the thread/process context."""
    try:
        text = "".join(traceback.format_exception(exc_type, exc, tb)).rstrip()
        log("EXCEPTION in %s\n%s" % (source, text), logging.ERROR)
    except Exception:
        pass


def _system_lines() -> list[str]:
    """Stable facts a support session needs. No user identifiers."""
    lines = [
        "%s %s" % (APP_NAME, APP_VERSION),
        "Python %s" % sys.version.replace("\n", " "),
        "Platform: %s" % platform.platform(),
        "Architecture: %s" % (platform.machine() or "unknown"),
        "Frozen: %s" % bool(getattr(sys, "frozen", False)),
        "Executable: %s" % (sys.executable or "unknown"),
    ]
    try:
        from app.compat import windows_build
        build = windows_build()
        lines.append("Windows build: %s" % (build if build else "unknown"))
    except Exception:
        pass
    try:
        from app.elevation import is_admin
        lines.append("Elevated: %s" % bool(is_admin()))
    except Exception:
        pass
    try:
        from app.config_paths import CONFIG_FILE
        lines.append("Config: %s" % CONFIG_FILE)
    except Exception:
        pass
    lines.append("Log: %s" % log_path())
    return lines


def tail(max_lines: int = 200) -> str:
    """The last `max_lines` of the crash log (or \"\" when unreadable)."""
    try:
        path = log_path()
        if not os.path.isfile(path):
            return ""
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            lines = fh.readlines()
        return "".join(lines[-max(1, int(max_lines)):])
    except Exception:
        return ""


def diagnostics_text() -> str:
    """Header block for a bug report: versions, environment, recent crashes."""
    parts = ["Diagnostics", "=" * 60] + _system_lines()
    try:
        import tkinter as tk
        parts.append("Tk: %s" % (tk.TkVersion if tk._default_root is None
                                 else tk._default_root.tk.call("info", "patchlevel")))
    except Exception:
        pass
    recent = tail(200)
    if recent:
        parts += ["", "Recent log (last 200 lines)", "-" * 60, recent.rstrip()]
    return "\n".join(parts) + "\n"


# --------------------------------------------------------------------------- #
# Hook installation
# --------------------------------------------------------------------------- #

def install_hooks(root=None) -> None:
    """Install global + Tk exception hooks. Idempotent, never raises.

    `root` is optional; pass the Tk root to capture exceptions raised inside
    widget callbacks (Tk swallows those before they reach sys.excepthook).
    """
    global _installed
    try:
        get_logger()
    except Exception:
        pass
    if not _installed:
        try:
            _previous["sys"] = sys.excepthook

            def _sys_hook(exc_type, exc, tb):
                log_exception(exc_type, exc, tb, source="main thread")
                prev = _previous.get("sys")
                if prev is not None:
                    try:
                        prev(exc_type, exc, tb)
                    except Exception:
                        pass

            sys.excepthook = _sys_hook
        except Exception:
            pass
        try:
            _previous["threading"] = threading.excepthook

            def _thread_hook(args):
                log_exception(args.exc_type, args.exc_value, args.exc_traceback,
                              source="thread %s" % getattr(args.thread, "name", "?"))
                prev = _previous.get("threading")
                if prev is not None:
                    try:
                        prev(args)
                    except Exception:
                        pass

            threading.excepthook = _thread_hook
        except Exception:
            pass
        _installed = True

    if root is not None:
        try:
            previous = getattr(root, "_cleaner_exception_hook", None)
            root._cleaner_exception_hook = previous  # marker only

            def _tk_hook(exc_type, exc, tb):
                log_exception(exc_type, exc, tb, source="Tk callback")
                # Keep Tk's normal stderr report when a console exists.
                try:
                    import traceback as _tb
                    _tb.print_exception(exc_type, exc, tb)
                except Exception:
                    pass

            root.report_callback_exception = _tk_hook
        except Exception:
            pass
