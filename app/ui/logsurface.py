"""The in-memory log, the status line and the progress bar (audit 2026-10, R-007).

Third slice carved out of `Application`, next to `RunFlow` (the run engine) and
`TrayControls` (the tray menu), using the same base-class composition so every
call site kept its name: `self.log(...)`, `self.set_status(...)`,
`self._set_progress(...)` and `self.export_logs()` all still live on the window.

Why these ten methods belong together: they are the only things that write the
three pieces of run feedback the user sees — the log list, the status label and
the progress bar — and all three share the same thread rule. Tk calls are only
safe on the Tk thread, so each of them funnels through `_ui_hop`, which is the
whole reason the group has one owner instead of being scattered through a
4000-line shell.

The mixin expects exactly what `Application.__init__` already sets before
anything can call these: `self.root`, `self._dispatch`, `self.active_tab`,
`self._busy`, and (after `_build_main_ui`) `self.status_lbl` /
`self.progress_bar`.
"""
from __future__ import annotations

import os
import threading
import time

from app.ui.base import _themed_showerror
from app.ui.theme import COLORS, TAB_ACCENTS


class LogSurface:
    """Log buffer, status line and progress bar for the main window."""

    # ---------------- log (in-memory only, #16 simplified per user) ------- #
    # No log window in the UI anymore — the log accumulates in memory and
    # "Export Logs" (Settings → Data & logs) saves it to a .txt for debugging.

    def _build_log(self, parent):
        self._log_lines = []
        self._log_lines.append("Welcome to Cleaner Tool.")

    def log(self, text):
        def _do():
            self._log_lines.append(f"[{time.strftime('%H:%M:%S')}] {text}")
            if len(self._log_lines) > 8000:
                del self._log_lines[:2000]
        try:
            if threading.current_thread() is threading.main_thread():
                _do()
            else:
                self._dispatch.post(_do)
        except Exception:
            pass

    # keep old internal name working for close-guard message
    def _log_full(self, text):
        self.log(text)

    def export_logs(self):
        """Save the full in-memory log to a .txt the user picks (user
        request: replaces the old View Details window — keeps debugging
        possible without console clutter in the UI)."""
        from tkinter import filedialog
        try:
            default_name = f"CleanerTool_Log_{time.strftime('%Y%m%d_%H%M%S')}.txt"
            path = filedialog.asksaveasfilename(
                parent=self.root,
                title="Export Logs",
                defaultextension=".txt",
                initialfile=default_name,
                filetypes=[("Text files", "*.txt"), ("All files", "*.*")],
            )
            if not path:
                return
            header = (
                f"Cleaner Tool log export\n"
                f"Exported: {time.strftime('%Y-%m-%d %H:%M:%S')}\n"
                f"Edition: {self._windows_edition_str()}\n"
                f"{'=' * 60}\n"
            )
            # Audit 2026-10: a support bundle must include the environment
            # and recent unhandled exceptions, not only the in-memory run
            # log (which is empty after a crash).
            try:
                from app.diagnostics import diagnostics_text
                header += "\n" + diagnostics_text() + "\n"
            except Exception:
                pass
            # U-001: the app writes a diagnostics trail to events.log the
            # whole time (elevation cookie failures, swallowed ops, ...),
            # but it was never read back. A bug report should carry it, so
            # append the tail. Best-effort: an unreadable log is skipped.
            try:
                from app.config_events import read_events
                events = read_events(500)
            except Exception:
                events = []
            payload = header + "\n".join(self._log_lines) + "\n"
            if events:
                payload += ("\n" + ("-" * 60) + "\n"
                            + "Diagnostics \u2014 swallowed failures / event log "
                            "(last %d)\n" % len(events)
                            + ("-" * 60) + "\n"
                            + "\n".join(events) + "\n")
            # H-003: this file is what users attach to public issues. The
            # elevation token and account name are redacted once, here,
            # rather than trusting every writer upstream to have done it.
            from app.logsafe import redact
            with open(path, "w", encoding="utf-8") as f:
                f.write(redact(payload))
            # user bug: exporting mid-run overwrote the live "Running X…"
            # status with "Log saved to …" and it never came back. When a
            # run is active the status belongs to the run — log only.
            if not getattr(self, "_busy", False):
                self.set_status("Log saved: " + os.path.basename(path))
            self.log(f"Log exported to {path}")
        except Exception as exc:
            _themed_showerror(self.root, "Export Failed",
                              f"Windows returned:\n{exc}")

    def _windows_edition_str(self) -> str:
        try:
            import winreg
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                                r"SOFTWARE\Microsoft\Windows NT\CurrentVersion") as k:
                product = winreg.QueryValueEx(k, "ProductName")[0]
                display = winreg.QueryValueEx(k, "DisplayVersion")[0]
            return f"{product} ({display})"
        except Exception:
            return "Windows"

    # ---------------- status line (fading, #16) ---------------- #

    def set_status(self, text):
        def _do():
            try:
                if hasattr(self, "status_lbl") and self.status_lbl.winfo_exists():
                    # cancel pending fades
                    for aid in getattr(self, "_status_fade_ids", []):
                        try:
                            self.root.after_cancel(aid)
                        except Exception:
                            pass
                    self._status_fade_ids = []
                    self.status_lbl.config(text=text, fg=TAB_ACCENTS.get(self.active_tab, COLORS["accent_green"]))
                    # fade back to muted after 4s (unless running)
                    if not self._busy:
                        aid = self.root.after(4000, lambda: self._fade_status())
                        self._status_fade_ids.append(aid)
            except Exception:
                pass
        try:
            if threading.current_thread() is threading.main_thread():
                _do()
            else:
                self._dispatch.post(_do)
        except Exception:
            pass

    def _fade_status(self):
        try:
            self.status_lbl.config(fg=COLORS["subtext"])
        except Exception:
            pass

    # ---------------- progress ---------------- #

    def _ui_hop(self, fn) -> None:
        """Marshal a callable onto the Tk thread, from either thread.

        MED-014, and the reason this is not just _dispatch.post() everywhere:
        a 0 ms `after()` timer is only ever at risk when it is scheduled from
        a WORKER thread, because whether it fires depends on the Tk thread
        happening to be inside Tcl's event loop when the cross-thread
        notifier arrives. From the Tk thread it is reliable by construction.

        So the worker path goes through TkDispatcher, whose delivery is
        guaranteed, while the main-thread path keeps the historical
        `after(0, ...)` deferral. That deferral is load-bearing: the
        pre-flight countdown relies on that one tick, and making it inline
        advanced the countdown enough to outrun the stop.
        """
        try:
            if threading.current_thread() is threading.main_thread():
                self.root.after(0, fn)
            else:
                self._dispatch.post(fn)
        except Exception:
            pass

    def _set_progress(self, value, maximum=None):
        def _do():
            try:
                # M10: `if maximum:` misrouted maximum=0 into the fraction
                # branch (and a real value/0 would raise); be explicit.
                if maximum is not None:
                    self.progress_bar.set_fraction(value / maximum if maximum else 0.0)
                else:
                    self.progress_bar.set_fraction(value)
            except Exception:
                pass
        self._ui_hop(_do)

    def _stop_indeterminate(self):
        """Turn off the shimmer and paint the full bar — ON THE TK THREAD.

        F-003 fix: _run_tasks_worker used to call
        progress_bar.set_indeterminate(False) directly from the worker
        thread. set_indeterminate(False) does after_cancel + Canvas _draw
        (delete/all + item creation) — real Tcl calls. Tkinter is not
        thread-safe; the identical Install-tab path (install_selected_mixed
        _done) was already fixed to marshal via root.after for exactly this
        reason, but this call site never got the fix. Same shape as
        _set_progress above: try/except only guards the shutdown race."""
        def _do():
            try:
                self.progress_bar.set_indeterminate(False)
            except Exception:
                pass
        self._ui_hop(_do)
