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
import tempfile
import threading
import traceback

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


def startup_log_path() -> str:
    """The pre-GUI trace written by `app/__main__._early_log`.

    Shared here so the elevation-timeout warning and the entry point cannot
    disagree about where a startup crash went. `_early_log` keeps its own
    literal path on purpose: it runs before this module is imported and must
    not depend on anything that could fail first.
    """
    return os.path.join(tempfile.gettempdir(), "cleaner_tool_startup.log")


def open_startup_log(parent=None) -> bool:
    """Open the startup crash trace, or explain why there is none.

    Called by the "Elevated Window Did Not Appear" dialog, where the whole
    point of the warning is that the elevated copy died before its window
    appeared. Opening the file is the difference between a dead end and a
    report the user can act on.

    The Tk import is local: diagnostics is imported by headless runs and must
    not pull in tkinter. Returns True when the file was handed to the shell,
    False when it was missing (or the shell refused). Never raises.
    """
    path = startup_log_path()
    exists = os.path.isfile(path)
    if exists:
        try:
            os.startfile(path)  # noqa: S606 - a log file the user owns
            return True
        except Exception:
            pass
    try:
        from tkinter import messagebox
        if exists:
            detail = ("Could not open the startup log with the default "
                      "viewer.\n\nThe crash trace is at:\n%s\n\n"
                      "The app log may also have it:\n%s"
                      % (path, log_path()))
        else:
            detail = ("No startup log was written for this attempt.\n\n"
                      "Expected at:\n%s\n\n"
                      "The elevated copy may have stopped before its first "
                      "log line, or the log was removed. The app log may "
                      "still have the traceback:\n%s"
                      % (path, log_path()))
        messagebox.showinfo("Startup Log", detail, parent=parent)
    except Exception:
        pass
    return False


def startup_log_tail(max_lines: int = 6, max_chars: int = 4096) -> list[str]:
    """The last meaningful lines of the startup trace, newest last.

    Reads only the tail of the file: `_early_log` appends across every launch,
    so this must not load a log that has grown for months. A partial first
    line (the read started mid-line) is dropped, control characters are
    flattened so Tk cannot choke on them, and blank lines are skipped.
    Returns [] when the file is missing or unreadable. Never raises.
    """
    try:
        path = startup_log_path()
        if not os.path.isfile(path):
            return []
        with open(path, "rb") as fh:
            size = os.fstat(fh.fileno()).st_size
            read = min(size, max(1024, int(max_chars)))
            fh.seek(size - read)
            data = fh.read()
        lines = data.decode("utf-8", errors="replace").splitlines()
        if read < size and lines:
            # The first line may be the tail of a longer line we did not read.
            lines = lines[1:]
        usable = []
        for raw in lines:
            line = "".join(ch if ch == "\t" or ch >= " " else " "
                           for ch in raw).rstrip()
            if line:
                usable.append(line)
        return usable[-max(1, int(max_lines)):]
    except Exception:
        return []


def startup_log_preview(max_lines: int = 6,
                        max_line_chars: int = 180) -> str:
    """Inline preview for the elevation-timeout warning.

    The warning already points at the log; these lines make the failure
    visible without opening anything. Long traceback lines are clipped so a
    single pathological line cannot push the dialog's buttons off-screen.
    """
    try:
        path = startup_log_path()
        lines = startup_log_tail(max_lines=max_lines)
    except Exception:
        lines, path = [], ""
    if not lines:
        try:
            if path and not os.path.isfile(path):
                return "No startup log was written for this attempt."
        except Exception:
            pass
        return "The startup log is empty."
    shown = []
    limit = max(40, int(max_line_chars))
    for line in lines:
        if len(line) > limit:
            line = line[:limit].rstrip() + "\u2026"
        shown.append(line)
    return "Last startup log lines:\n" + "\n".join(shown)


def copy_startup_log_path(parent=None) -> bool:
    """Put the startup-log path on the clipboard.

    The path is useful even when the file was never written: the user can
    paste it into a report or open it manually. Uses the widget's own Tk
    clipboard (the same mechanism the rest of the app uses), so this module
    still does not import tkinter at import time. Never raises; returns False
    when the clipboard refused and the path had to be shown instead.
    """
    path = startup_log_path()
    try:
        if parent is None:
            raise AttributeError("no clipboard owner")
        parent.clipboard_clear()
        parent.clipboard_append(path)
        try:
            parent.update_idletasks()
        except Exception:
            pass
        return True
    except Exception:
        pass
    try:
        from tkinter import messagebox
        messagebox.showinfo(
            "Copy Path",
            "Could not copy the path automatically.\n\n"
            "The startup log is at:\n%s\n\n"
            "The app log may also have the traceback:\n%s"
            % (path, log_path()), parent=parent)
    except Exception:
        pass
    return False


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
    # LTSC note: support sessions kept guessing the edition from the build
    # number. The edition probe names it (and flags the S/SN branch), so a
    # "winget missing" report on IoT LTSC reads as expected, not broken.
    try:
        from app.compat import edition_label, is_ltsc_edition
        _ed = edition_label()
        if _ed:
            lines.append("Windows edition: %s%s"
                         % (_ed, " (LTSC)" if is_ltsc_edition() else ""))
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
