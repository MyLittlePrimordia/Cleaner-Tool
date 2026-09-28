"""SpecsDialog — extracted verbatim from app/gui.py (H12, batch 3).

One dialog per module, mirroring app/ui/widgets. Depends only on
app.ui below it, plus the app.utils helpers it already used; it
knows nothing
about any other dialog except where noted.
"""
from __future__ import annotations

import tkinter as tk
from tkinter import ttk, messagebox
from app.ui.base import ThemedModal
from app.ui.theme import COLORS, F, TAB_ACCENTS
from app.ui.widgets import AnimatedButton


class SpecsDialog(ThemedModal):
    """PC Specs: Speccy-style summary in plain words — left nav
    (Summary, OS, CPU, RAM, Board, Graphics, Storage, Audio, Network),
    one-line facts on the right, Copy Specs for support posts.
    Read-only engine (app/pc_specs.py); anything unreadable shows
    'Unknown', never a guess. No temperatures — those need a driver
    stdlib code can't honestly read."""

    _TABS = ("Summary", "OS", "CPU", "RAM", "Board", "Graphics",
             "Storage", "Audio", "Network")

    def __init__(self, parent, app):
        self.app = app
        self._tab = "Summary"
        super().__init__(parent, title="PC Specs", accent=TAB_ACCENTS["Clean"])
        body = self.body
        cols = tk.Frame(body, bg=COLORS["bg"])
        cols.pack(fill="both", expand=True)
        nav = tk.Frame(cols, bg=COLORS["bg_alt"], width=150)
        nav.pack(side="left", fill="y", padx=(0, 8))
        try:
            nav.pack_propagate(False)
        except Exception:
            pass
        self._nav_btns = {}
        for tab in self._TABS:
            b = AnimatedButton(nav, text=tab, command=lambda t=tab: self._show(t),
                               bg=COLORS["bg_alt"], fg=COLORS["text"],
                               font=(F, 9, "bold"), padx=10, pady=5)
            b.pack(fill="x", padx=6, pady=2)
            self._nav_btns[tab] = b
        self._detail = tk.Frame(cols, bg=COLORS["bg"])
        self._detail.pack(side="left", fill="both", expand=True)
        foot = tk.Frame(body, bg=COLORS["bg"])
        foot.pack(fill="x", pady=(8, 0))
        tk.Label(foot, text="Unknown = couldn't be read, never guessed.",
                 font=(F, 8), bg=COLORS["bg"], fg=COLORS["subtext"]).pack(side="left")
        AnimatedButton(foot, text="Copy Specs",
                       command=self._copy_specs,
                       bg=COLORS["surface"], fg=COLORS["text"],
                       font=(F, 9, "bold"), padx=14, pady=6).pack(side="right")
        self._copy_note = tk.Label(foot, text="", font=(F, 8),
                                   bg=COLORS["bg"], fg=COLORS["accent_green"])
        self._copy_note.pack(side="right", padx=(0, 8))
        self.on_close(self._stop)
        self._show("Summary")

    def _rows(self, pairs):
        for child in self._detail.winfo_children():
            try:
                child.destroy()
            except Exception:
                pass
        for title, value in pairs:
            tk.Label(self._detail, text=title, font=(F, 11, "bold"),
                     bg=COLORS["bg"], fg=COLORS["text"],
                     anchor="w").pack(fill="x", pady=(8, 0))
            tk.Label(self._detail, text=value or "Unknown", font=(F, 9),
                     bg=COLORS["bg"], fg=COLORS["subtext"], anchor="w",
                     justify="left", wraplength=560).pack(fill="x")

    def _show(self, tab):
        """Render a section, gathering the facts OFF the Tk thread.

        PERF. Every probe here is a registry or SMBIOS read costing hundreds of
        milliseconds, and this used to run them inline on the Tk thread, so
        opening or switching a section froze the whole app while it worked -
        worst on Summary and on Copy, which builds the full report. The
        gathering half touches no widget, so it is a pure function and belongs
        on a worker; only `self._rows` needs the Tk thread.

        `self.app` is the attribute name - reading "_app" silently yields None
        and would drop the whole path back onto the Tk thread.
        """
        self._tab = tab
        token = getattr(self, "_gather_seq", 0) + 1
        self._gather_seq = token
        self._rows([("Gathering...", "Reading this PC's hardware. One moment.")])

        import threading as _th

        def _work():
            try:
                rows = self._gather(tab)
            except Exception:
                rows = [("Error", "Could not read this section.")]
            try:
                _app = getattr(self, "app", None) or getattr(self, "_app", None)
                _disp = getattr(_app, "_dispatch", None)
                if _disp is not None:
                    _disp.post(lambda: self._render(token, rows))
                else:
                    self.after(0, lambda: self._render(token, rows))
            except Exception:
                pass

        try:
            _th.Thread(target=_work, daemon=True,
                       name="PcSpecsGather").start()
        except Exception:
            self._render(token, self._gather(tab))

    def _render(self, token, rows):
        """Tk-thread half. Ignores a result the user has already navigated past."""
        if token != getattr(self, "_gather_seq", 0):
            return
        try:
            self._rows(rows)
        except Exception:
            pass

    def _gather(self, tab):
        """The facts for one section, as (label, value) pairs. No Tk calls."""
        try:
            from app import pc_specs as _sp
        except Exception:
            return [("Error", "Specs engine unavailable.")]
        if tab == "Summary":
            lines = _sp.summary_lines() or ["Nothing readable."]
            return [("Your PC at a glance", "\n".join(lines))]
        if tab == "OS":
            cap, build = _sp.os_info()
            return [("Operating System", cap), ("Version", build)]
        if tab == "CPU":
            name, detail = _sp.cpu_info()
            return [("Processor", name), ("Speed · Cores", detail)]
        if tab == "RAM":
            head, detail = _sp.ram_info()
            return [("Memory", head), ("Available", detail or "Unknown")]
        if tab == "Board":
            name, detail = _sp.board_info()
            return [("Motherboard", name), ("Firmware", detail or "Unknown")]
        if tab == "Graphics":
            gpus = _sp.gpu_info()
            rows = [(f"GPU {i + 1}", g) for i, g in enumerate(gpus)] or [
                ("Graphics", "Unknown")]
            try:
                from app.tasks.tweak_tasks import _query_video_mode_list
                cur, _mx = _query_video_mode_list()
                # Audit fix: only show a real reading — the DEVMODE
                # struct bug that used to make this show "0x0 @ 0Hz" is
                # fixed at the source now, but keep this guard anyway
                # so a genuinely unreadable display never prints "0x0"
                # instead of just omitting the row.
                if cur and cur[0] and cur[1]:
                    rows.append(("Display now",
                                 f"{cur[0]}x{cur[1]} @ {cur[2]}Hz"))
            except Exception:
                pass
            return rows
        if tab == "Storage":
            disks = _sp.storage_info()
            return [(f"{d['drive']} {d['label']}".strip(),
                     f"{_sp._gb(d['free'])} free of {_sp._gb(d['total'])}"
                     + (f" · {d['fs']}" if d.get("fs") else ""))
                    for d in disks] or [("Drives", "Unknown")]
        if tab == "Audio":
            names = _sp.audio_info()
            return [(f"Output {i + 1}", n) for i, n in enumerate(names)] or [
                ("Audio", "Unknown")]
        if tab == "Network":
            name, dns = _sp.net_info()
            return [("Computer", name), ("Network", dns)]
        return [("", "")]


    def _copy_specs(self):
        """Copy the full specs report (support-ticket use case)."""
        try:
            from app import pc_specs as _sp
            try:
                text = _sp.full_report_text()
            except Exception:
                text = "\n".join(_sp.summary_lines())
            if not text:
                raise ValueError("empty report")
            self._dlg.clipboard_clear()
            self._dlg.clipboard_append(text)
            self._copy_note.config(text="Copied!")
        except Exception:
            try:
                self._copy_note.config(text="Copy failed.")
            except Exception:
                pass

    def _stop(self):
        pass
