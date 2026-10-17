"""FileLockDialog - "what is stopping me deleting this?"

Backed by app/filelocks.py (Windows Restart Manager plus a handle scan).

Rebuilt from scratch (2026-10, second pass). The first redesign still had a
path box, File/Folder buttons, recent chips, stat tiles, a PID column, Ask /
Force buttons on every row, and three footer buttons. The real task has
three steps and this has three screens' worth of controls on one:

    1  Choose the file or folder        (two big buttons, nothing else)
    2  See who is holding it            (one row per program)
    3  Press Close                      (one button per program)

A program that will not close politely gets a second, red "Force close" on
its row ONLY after a polite close failed - the user is never asked to choose
between "ask" and "force" up front. Typing a path, recent targets, PIDs and
the "keep checking" loop are gone; "Check again" covers the same need.

Safety is unchanged: graceful close first, force only after a confirmation
that names the program and warns about unsaved work, core Windows processes
and this app are refused in filelocks.close_program, and every outcome is
confirmed by RE-SCANNING instead of trusting taskkill.
"""
from __future__ import annotations

import os
import threading
import tkinter as tk
from tkinter import filedialog as _fd

from app.ui.base import _themed_askyesno
from app.ui.dialogs._tool_page import TOOL_ACCENT, ToolPage
from app.ui.theme import COLORS, F
from app.ui.widgets import AnimatedButton, Tooltip
from app.ui.widgets.datatable import DataTable

_COLUMNS = (
    {"key": "name", "title": "Program holding it open", "w": 300,
     "weight": 1, "min": 160, "sortable": False},
)


class FileLockDialog(ToolPage):
    def __init__(self, parent, app):
        self.app = app
        self._stop_token = [False]
        self._scan_seq = 0
        self._data = {}
        self._target = ""
        self._limits = {}
        self._elevated = False
        self._busy = False
        self._failed = set()       # pids a polite close did not free

        super().__init__(
            parent, title="File Lock Finder",
            scope="Closing a program can lose unsaved work - it is asked to "
                  "close first.")

        self.table = DataTable(self.content, _COLUMNS, row_h=52,
                               accent=TOOL_ACCENT, two_line=True,
                               on_action=self._on_action, show_header=False)
        self._build_picker()

        self._close_all = self.add_button("Close all", self._close_all_safe,
                                          kind="primary")
        self._again = self.add_button("Check again", self._refresh)
        self._other = self.add_button("Choose another", self._show_picker)
        self._admin_btn = AnimatedButton(
            self.foot_btns, text="Scan as admin", command=self._load_elevated,
            bg=COLORS["surface"], fg=COLORS["accent_yellow"], font=(F, 9),
            padx=14, pady=6)
        Tooltip(self._admin_btn,
                "Programs running as another user cannot be seen without "
                "this. Windows will ask first.")
        self.on_close(self._stop)
        self._show_picker()

    # ------------------------------------------------------------ screens
    def _build_picker(self):
        f = tk.Frame(self.content, bg=COLORS["bg"], bd=0)
        self._picker = f
        inner = tk.Frame(f, bg=COLORS["bg"], bd=0)
        inner.place(relx=0.5, rely=0.42, anchor="center")
        tk.Label(inner, text="What can't you delete?", font=(F, 11, "bold"),
                 bg=COLORS["bg"], fg=COLORS["text"]).pack()
        tk.Label(inner, text="Pick it and this will show which program has "
                             "it open.", font=(F, 9), bg=COLORS["bg"],
                 fg=COLORS["subtext"]).pack(pady=(4, 16))
        row = tk.Frame(inner, bg=COLORS["bg"], bd=0)
        row.pack()
        AnimatedButton(row, text="Choose a folder", command=lambda: self._pick(True),
                       bg=TOOL_ACCENT, fg=COLORS["black"], font=(F, 10, "bold"),
                       padx=22, pady=10).pack(side="left", padx=6)
        AnimatedButton(row, text="Choose a file", command=lambda: self._pick(False),
                       bg=COLORS["surface"], fg=COLORS["text"], font=(F, 10),
                       padx=22, pady=10).pack(side="left", padx=6)

    def _show_picker(self):
        self._stop_token[0] = False
        self._target = ""
        self._data = {}
        self.table.pack_forget()
        self._picker.pack(fill="both", expand=True)
        self.banner.set_state("?", COLORS["muted"])
        self.banner.set_title("Find out what is using a file or folder.")
        self.banner.set_sub("")
        self.status.stop("")
        for b in (self._close_all, self._again, self._other):
            b.pack_forget()
        self._admin_btn.pack_forget()

    def _show_results(self):
        self._picker.pack_forget()
        if not self.table.winfo_manager():
            self.table.pack(fill="both", expand=True)
        for b in (self._close_all, self._again, self._other):
            if not b.winfo_manager():
                b.pack(side="right", padx=(8, 0))

    # ------------------------------------------------------------ helpers
    def _stop(self):
        self._stop_token[0] = True

    def set_busy(self, busy):
        self._busy = bool(busy)
        try:
            self._again.set_enabled(not busy)
        except Exception:
            pass

    def _worker_failed(self, exc):
        # B-017: _land_elevated is what normally re-enables this button; on a
        # worker exception it must be restored here or it stays dead.
        try:
            self._admin_btn.set_enabled(True)
        except Exception:
            pass
        super()._worker_failed(exc)

    def _pick(self, use_dir):
        if use_dir:
            path = _fd.askdirectory(parent=self._dlg,
                                    title="Choose the folder that is in use")
        else:
            path = _fd.askopenfilename(
                parent=self._dlg, title="Choose the file that is in use")
        if path:
            self._target = path
            self._elevated = False
            self._failed = set()
            self._show_results()
            self._start()

    # --------------------------------------------------------------- scan
    def _refresh(self):
        if self._target:
            self._start()

    def _start(self):
        from app.filelocks import holders
        token = self._stop_token
        token[0] = False
        self._scan_seq += 1
        seq = self._scan_seq
        target = self._target
        self.set_busy(True)
        self.status.start("Checking...")
        self.banner.set_state("…", COLORS["muted"])
        self.banner.set_title("Checking...")
        self.banner.set_sub(target)

        def worker():
            try:
                data = holders(target, resolve_files=True)
            except Exception as exc:                # noqa: BLE001
                data = {"holders": [], "error": "Could not check: %s" % exc}
            if token[0]:
                return
            self._ui(self._land, seq, data)

        threading.Thread(target=worker, daemon=True).start()

    def _land(self, seq, data, note=""):
        if seq != self._scan_seq:
            return
        self._data = data if isinstance(data, dict) else {}
        self.set_busy(False)
        self.status.stop(note)
        self._apply_limits()
        self._paint()

    def _apply_limits(self):
        """Offer a deeper scan only when the first one could not see
        everything - a scan that silently says "nothing" when something does
        is worse than no scan."""
        from app.filelocks import scan_limits
        try:
            self._limits = scan_limits()
        except Exception:
            self._limits = {}
        try:
            if self._limits.get("incomplete") and not self._elevated:
                if not self._admin_btn.winfo_manager():
                    self._admin_btn.pack(side="right", padx=(8, 0))
            elif self._admin_btn.winfo_manager():
                self._admin_btn.pack_forget()
        except Exception:
            pass

    def _paint(self):
        from app.filelocks import verdict_for
        hs = self._data.get("holders") or []
        closable = [h for h in hs if h.get("can_close")]
        err = self._data.get("error")
        name = os.path.basename(self._target.rstrip("\\/")) or self._target
        if err:
            self.banner.set_state("!", COLORS["accent_yellow"])
            self.banner.set_title(str(err), COLORS["accent_yellow"])
        elif hs:
            n = len(hs)
            self.banner.set_state(str(n), COLORS["accent_yellow"])
            self.banner.set_title(
                "%s is open in %s." % (name, "1 program" if n == 1
                                       else "%d programs" % n))
        else:
            self.banner.set_state("✓", COLORS["accent_green"])
            self.banner.set_title(verdict_for(dict(
                self._data, incomplete=bool(self._limits.get("incomplete"))
                and not self._elevated)))
        sub = "Close the program and you can delete or rename it." if hs \
            else ""
        if self._limits.get("incomplete") and not self._elevated:
            sub = (sub + " Some programs may be hidden - Scan as admin "
                         "checks them too.").strip()
        self.banner.set_sub(sub)
        self._close_all.set_enabled(len(closable) > 1)
        self._render()

    def _load_elevated(self):
        """Re-scan with administrator rights behind a UAC prompt."""
        from app.filelocks import holders_elevated
        if not self._target:
            return
        self.set_busy(True)
        self._admin_btn.set_enabled(False)
        self.status.start("Waiting for the administrator prompt...")
        target = self._target
        self.run_worker(holders_elevated, self._land_elevated, target)

    def _land_elevated(self, data):
        self.set_busy(False)
        self._admin_btn.set_enabled(True)
        if data.get("cancelled"):
            self.status.stop("Administrator access was declined.",
                             COLORS["accent_yellow"])
            return
        if data.get("error"):
            self.status.stop(data["error"], COLORS["accent_red"])
            return
        self._elevated = True
        self._data = data
        self.status.stop("Scanned with administrator rights.")
        self._apply_limits()
        self._paint()

    # ------------------------------------------------------------ closing
    def _on_action(self, row, action_id):
        h = row.get("_h")
        if h is not None:
            self._close_holder(h, action_id == "force")

    def _close_holder(self, holder, force):
        """Close one program, then RE-SCAN and report what actually
        happened. A polite close that did not free the file turns that
        row's button into a red Force close."""
        from app.filelocks import close_program, holders
        pid = int(holder.get("pid") or 0)
        name = holder.get("name") or "this program"
        if not holder.get("graceful"):
            force = True            # no window to ask: only force can work
        if force:
            if not _themed_askyesno(
                    self._dlg, "Force close %s?" % name,
                    "%s will be ended immediately.\n\nUnsaved work is lost."
                    % name,
                    accent=TOOL_ACCENT, yes_text="Force close",
                    no_text="Go Back"):
                return
        self.status.start("Closing %s..." % name)
        self.set_busy(True)
        target = self._target

        def _work():
            res = close_program(pid, force,
                                expected_start=holder.get("start"))
            after = holders(self._target)
            return res, after

        self.run_worker(
            _work,
            lambda pair: self._land_close(holder, pair[0], pair[1], force))

    def _land_close(self, holder, res, after, was_force):
        name = holder.get("name") or "It"
        pid = int(holder.get("pid") or 0)
        self._data = after if isinstance(after, dict) else {}
        self.set_busy(False)
        still = {int(h.get("pid") or 0) for h in
                 (self._data.get("holders") or [])}
        if not res.get("ok"):
            self._failed.add(pid)
            note = res.get("message") or "Could not close it."
            self._apply_limits()
            self._paint()
            self.status.stop(note, COLORS["accent_red"])
            return
        if pid in still and not was_force:
            self._failed.add(pid)
            note = "%s did not close. You can force it." % name
        elif self._data.get("holders"):
            note = "%s closed. Something else still has it open." % name
        else:
            note = "Nothing has this open now."
        self._apply_limits()
        self._paint()
        self.status.stop(note, COLORS["accent_green"]
                         if not self._data.get("holders")
                         else COLORS["accent_yellow"])

    def _close_all_safe(self):
        """Politely close every program that permits it, in one go. Force is
        never used here - that is a per-program decision."""
        from app.filelocks import close_program
        jobs = [h for h in (self._data.get("holders") or [])
                if h.get("can_close") and h.get("graceful")]
        if not jobs:
            self.status.stop("Nothing here can be closed politely. Use "
                             "Force close on a row.")
            return
        if not _themed_askyesno(
                self._dlg, "Close %d program(s)?" % len(jobs),
                "%s will be asked to close.\n\nYou can reopen them from "
                "your taskbar." % ", ".join(h.get("name") or "?"
                                            for h in jobs[:6]),
                accent=TOOL_ACCENT, yes_text="Close them", no_text="Go Back"):
            return
        self.status.start("Closing...")
        self.set_busy(True)

        def _work():
            for h in jobs:
                close_program(int(h.get("pid") or 0), False,
                              expected_start=h.get("start"))
            from app.filelocks import holders
            return holders(self._target, resolve_files=True)

        self.run_worker(_work, lambda data: self._land(self._scan_seq, data))

    # ------------------------------------------------------------ render
    def _render(self):
        from app.filelocks import why_must_force, why_not_closable
        hs = self._data.get("holders") or []
        rows = []
        for h in hs:
            pid = int(h.get("pid") or 0)
            allowed = bool(h.get("can_close"))
            files = h.get("files") or []
            if not allowed:
                sub = why_not_closable(h)
            elif not h.get("graceful") and why_must_force(h):
                sub = why_must_force(h)
            elif files:
                sub = ", ".join(os.path.basename(f) for f in files[:3])
                if len(files) > 3:
                    sub += "  +%d more" % (len(files) - 3)
            else:
                sub = h.get("kind") or "a program"
            acts = []
            if allowed:
                if pid in self._failed or not h.get("graceful"):
                    acts.append({"id": "force", "text": "Force close",
                                 "fg": COLORS["accent_red"]})
                else:
                    acts.append({"id": "close", "text": "Close"})
            rows.append({
                "_key": pid or id(h), "_h": h, "_actions": acts,
                "_cells": {"name": {
                    "text": h.get("name") or "Unknown program", "bold": True,
                    "sub": sub, "dot": COLORS["accent_green"] if allowed
                    else COLORS["muted"]}}})
        if self._data.get("error"):
            self.table.set_empty("Could not check", str(self._data["error"]),
                                 glyph="!")
        else:
            self.table.set_empty(
                "Nothing has it open",
                "You can delete or rename it now.", glyph="✓")
        self.table.set_rows(rows)

    def _resize(self):
        pass
