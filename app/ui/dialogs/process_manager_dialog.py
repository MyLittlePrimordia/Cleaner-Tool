"""ProcessManagerDialog — extracted verbatim from app/gui.py (H12, batch 4).

One dialog per module, mirroring app/ui/widgets. Depends only on
app.ui below it, plus the app.utils helpers it already used; it
knows nothing
about any other dialog except where noted.
"""
from __future__ import annotations

import os

import tkinter as tk
from tkinter import ttk, messagebox
from app.ui.base import ThemedModal, _themed_askyesno
from app.ui.theme import (COLORS, F, ICON_ROW_PX, TAB_ACCENTS,
                          icon_bg_rgb)
from app.ui.widgets import AnimatedButton, ScrollableRoundedPanel


class ProcessManagerDialog(ThemedModal):
    """Process Manager (Tools tab card): gamer-friendly task manager.

    One row per app — processes from the same program are summed into a
    single row (CPU + RAM totals, "· N processes" when N > 1), ordered
    heaviest-first. One Close per row closes the whole group. Core
    Windows processes are never listed at all (nothing shown that you
    can't close). No filter pill, no search, no Refresh/Close buttons —
    the list refreshes itself quietly and the X closes the popup.

    Flicker fix: rows are built once and updated IN PLACE on every
    tick (labels + button commands only). A full rebuild happens only
    when the grouped set actually changes (app opened/closed). Close
    uses taskkill /PID /F (same path Game Night already trusts).
    """

    #: Reserved icon slot, in Tk text units at the default font. Sized
    #: from ICON_ROW_PX so the text-unit size lands close to the real
    #: image box; reserving it is what keeps rows from reflowing when
    #: an icon arrives late (or never does).
    _ICON_SLOT_W = 32

    _TICK_MS = 3000
    _TOP_N = 80
    _MAX_GROUPS = 60

    def __init__(self, parent, app):
        self.app = app
        self._tick_after = None
        self._sampling = False
        self._sampler = None
        self._rows = {}  # group key -> row frame (harness compat)
        self._row_widgets = {}  # group key -> {"name","detail","btn","icon","pids"}
        # key -> PhotoImage. Icons are extracted per image PATH, so this is
        # what stops the 3-second refresh re-extracting them forever.
        self._icon_cache = {}
        # Group keys known to have NO icon. A negative cache matters as
        # much as the positive one: without it every app that legitimately
        # has no icon is re-queued, re-resolved and re-extracted on
        # every tick, forever.
        self._icon_missed = set()
        self._icon_done = set()
        self._displayed_keys = []  # key order currently on screen
        self._last_items = []
        self._shown_groups = []
        super().__init__(parent, title="Process Manager",
                         accent=TAB_ACCENTS["Tools"])
        body = self.body

        self._panel = ScrollableRoundedPanel(body)
        self._panel.config(height=460)
        self._panel.pack(fill="both", expand=True)

        # Bottom-center stats (single clean line, replaces the old
        # "Top 10 · heaviest:" footer and the Refresh/Close buttons —
        # the dialog auto-refreshes and the title-bar X closes it).
        self._status = tk.Label(body, text="Scanning…", font=(F, 9),
                                bg=COLORS["bg"], fg=COLORS["subtext"],
                                anchor="center", justify="center")
        self._status.pack(fill="x", pady=(10, 0))

        try:
            from app.process_manager import ProcessSampler
            # include_system=False: system rows are never listed — no
            # point showing what can't be closed.
            self._sampler = ProcessSampler(top_n=self._TOP_N,
                                           include_system=False)
        except Exception:
            self._sampler = None

        try:
            self.on_close(self._cancel_tick)
        except Exception:
            pass
        # Prime + first paint, then tick
        self._refresh_list(first=True)
        self._schedule_tick()

    @staticmethod
    def _detail_text(group):
        try:
            cpu = float(group.get("total_cpu") or 0)
            ram = float(group.get("total_ram") or 0)
            count = int(group.get("count") or 1)
        except Exception:
            return ""
        text = f"{cpu:.0f}% CPU  ·  {ram:.0f} MB RAM"
        if count > 1:
            text += f"  ·  {count} processes"
        return text

    @staticmethod
    def _stats_text(groups):
        try:
            n_apps = len(groups)
            n_proc = sum(int(g.get("count") or 0) for g in groups)
            gb = sum(float(g.get("total_ram") or 0)
                     for g in groups) / 1024.0
            return (f"{n_apps} apps  ·  {n_proc} processes  ·  "
                    f"{gb:.1f} GB RAM")
        except Exception:
            return ""

    def _say(self, text):
        try:
            self._status.config(text=text)
        except Exception:
            pass

    def _cancel_tick(self):
        try:
            if self._tick_after is not None and self._dlg is not None \
                    and self._dlg.winfo_exists():
                self._dlg.after_cancel(self._tick_after)
        except Exception:
            pass
        self._tick_after = None

    def _schedule_tick(self):
        self._cancel_tick()
        try:
            if self._dlg is not None and self._dlg.winfo_exists():
                self._tick_after = self._dlg.after(self._TICK_MS,
                                                   self._on_tick)
        except Exception:
            self._tick_after = None

    def _on_tick(self):
        self._tick_after = None
        try:
            if self._dlg is None or not self._dlg.winfo_exists():
                return
        except Exception:
            return
        self._refresh_list()
        self._schedule_tick()

    def _refresh_list(self, first=False):
        """Sample off the Tk thread, then apply the result on it.

        PERF. `ProcessSampler.sample()` spawns tasklist and walks every
        process, so it costs a few hundred ms. It used to run inline here, on
        the Tk thread, which meant the whole app - not just this dialog - was
        frozen for that long every _TICK_MS, and for the entire first paint
        when the dialog opened. The user reported the app locking up for
        seconds on opening this card.

        The split is clean because the expensive half is pure data: sample()
        and group_processes() touch no widget. Only the row update needs the Tk
        thread, so that is the only part marshalled back.
        """
        import threading as _th

        if getattr(self, "_sampling", False):
            return                      # never overlap samples
        self._sampling = True

        def _work():
            items, groups = [], []
            try:
                if self._sampler is not None:
                    items = self._sampler.sample()
            except Exception:
                items = []
            # _last_items feeds the End-task action, so it must be
            # updated even though only `groups` reaches the UI.
            self._last_items = list(items or [])
            try:
                from app.process_manager import group_processes
                groups = group_processes(list(items or []))[: self._MAX_GROUPS]
            except Exception:
                groups = []
            try:
                # BUG-001: this used to reach for the APP's dispatcher via
                # getattr and fall back to `self._dlg.after(0, ...)` when that
                # came back None. Two problems: the fallback is a Tk call from
                # this worker thread (a hard RuntimeError on Python 3.14, which
                # the outer except then turned into a silently stuck
                # process list), and it could never fire anyway --
                # Application.__init__ builds _dispatch unconditionally, so the
                # "None" branch was dead code guarding against nothing.
                #
                # ThemedModal now owns a dispatcher per dialog, so there is one
                # hop and it is always the safe one.
                self._dispatch.post(
                    lambda: self._apply_groups(groups, first))
            except Exception:
                self._sampling = False

        try:
            _th.Thread(target=_work, daemon=True, name="ProcMgrSample").start()
        except Exception:
            self._sampling = False
            self._apply_groups([], first)

    def _sync_row_order(self, groups):
        """Re-pack existing rows into a NEW order, but only when it changed.

        This exists because the rows must follow the CPU/RAM ranking, and
        because the previous version of this was itself the flicker the user
        reported: it called pack_forget() then pack() on every row on every
        tick, unconditionally. pack_forget() unmaps the widget - it genuinely
        leaves the screen - and pack() then renders it back in, so "disappear
        and reappear, and you can see it rendering in" was being caused on
        purpose three times a minute.

        So the guard is the whole point: if the order is unchanged, nothing is
        touched at all. Re-ordering is not free of visual churn, and doing it
        when nothing moved is churn with no purpose.
        """
        order = [g.get("key") for g in (groups or [])]
        if order == list(self._displayed_keys):
            return
        try:
            for g in reversed(groups or []):
                row = self._rows.get(g.get("key"))
                if row is None:
                    continue
                try:
                    row.pack_forget()
                    row.pack(fill="x", padx=4, pady=3)
                except Exception:
                    pass
        except Exception:
            pass
        # Recorded so the next tick can tell "already in this order" from
        # "needs re-packing", which is the difference between zero widget
        # operations and one per row.
        try:
            self._displayed_keys = list(order)
        except Exception:
            pass

    def _apply_groups(self, groups, first=False):
        """Tk-thread half: everything here touches widgets."""
        self._sampling = False
        try:
            if self._dlg is None or not self._dlg.winfo_exists():
                return
        except Exception:
            return
        self._shown_groups = groups
        new_keys = [g.get("key") for g in groups]
        # Steady state (same apps, same order): update values in place —
        # no destroy/rebuild, no flicker. Structure changed (app opened
        # or closed): rebuild once.
        # Membership, NOT order, decides whether to rebuild. Groups are
        # sorted by live CPU+RAM, so the ORDER changes almost every tick -
        # and comparing ordered lists meant any rank change destroyed and
        # recreated every row. That is the flicker the user reported:
        # rows visibly disappear and render back in. Comparing the SET of
        # keys asks the question that actually matters ("are these the same
        # apps?"), and the order is then synced by re-packing, which is
        # cheap and does not destroy a widget.
        _same = set(new_keys) == set(self._displayed_keys)
        if not first and _same and self._row_widgets:
            self._update_rows_in_place(groups)
            # Re-rank ONLY when the roster changed. Groups are sorted by live
            # CPU+RAM, so the order genuinely moves - measured at 100% of ticks
            # on this machine - and re-ordering means unmapping and re-packing
            # every row, which is the visible flicker. Numbers still update
            # every tick, so the ranking stays readable in the columns; it just
            # does not reshuffle under the reader. Adding or removing an app is
            # a real event worth re-ordering for.
            _fresh = [k for k in new_keys if k not in set(self._displayed_keys)]
            if _fresh:
                self._sync_row_order(groups)
        else:
            self._rebuild_rows(groups)
        # Icons are filled in off the Tk thread and only for rows that do
        # not already have one, so a refresh never re-extracts.
        try:
            self._load_icons(new_keys)
        except Exception:
            pass
        # Transient "Closed X." messages are overwritten here by the
        # fresh stats — unless this tick found nothing new to say.
        try:
            if groups:
                self._say(self._stats_text(groups))
            elif not first:
                self._say("Nothing running right now.")
        except Exception:
            pass

    def _rebuild_rows(self, groups):
        try:
            holder = self._panel.inner
        except Exception:
            return
        try:
            for w in list(holder.winfo_children()):
                w.destroy()
        except Exception:
            return
        self._rows = {}
        self._row_widgets = {}
        # The widgets these images were attached to are being destroyed, so the
        # references go with them rather than pinning dead PhotoImages for the
        # rest of the session.
        self._icon_cache = {}
        self._icon_missed = set()
        self._icon_done = set()
        self._displayed_keys = [g.get("key") for g in groups]
        if not groups:
            try:
                tk.Label(holder, text="Nothing running right now.",
                         font=(F, 9), bg=COLORS["bg_alt"],
                         fg=COLORS["subtext"]).pack(pady=20)
                self._panel.refresh_scroll()
            except Exception:
                pass
            return
        for g in groups:
            try:
                self._build_row(holder, g)
            except Exception:
                continue
        try:
            self._panel.refresh_scroll()
        except Exception:
            pass

    def _update_rows_in_place(self, groups):
        """Refresh labels + Close targets without touching the widget
        tree — this is what makes the 3s tick flicker-free."""
        for g in groups:
            try:
                k = g.get("key")
                refs = self._row_widgets.get(k)
                if not refs:
                    continue
                refs["name"].config(text=g.get("display") or "?")
                refs["detail"].config(text=self._detail_text(g))
                try:
                    pids = tuple(int(r.get("pid"))
                                 for r in g.get("items", [])
                                 if r.get("pid")
                                 and int(r.get("pid")) != os.getpid())
                except Exception:
                    pids = ()
                if refs.get("pids") != pids:
                    # The group's process set changed, so a group that was
                    # iconless may now have an image worth trying.
                    try:
                        self._icon_done.discard(k)
                    except Exception:
                        pass
                refs["pids"] = pids
                disp = g.get("display") or "?"
                try:
                    refs["btn"]._command = (
                        lambda ps=pids, d=disp:
                        self._confirm_close_group(ps, d))
                except Exception:
                    pass
            except Exception:
                continue

    def _build_row(self, parent, group):
        key = group.get("key")
        disp = group.get("display") or "?"
        try:
            pids = tuple(int(r.get("pid")) for r in group.get("items", [])
                         if r.get("pid")
                         and int(r.get("pid")) != os.getpid())
        except Exception:
            pids = ()
        if not pids:
            return
        row = tk.Frame(parent, bg=COLORS["bg_alt"])
        row.pack(fill="x", padx=4, pady=3)
        # Icon placeholder. It is created EMPTY so the layout is identical
        # whether or not an icon ever arrives - a row that changes width when
        # its picture lands is far more distracting than no picture at all.
        # The real image is filled in by _load_icons off the Tk thread.
        # A FIXED-SIZE slot, reserved in PIXELS. Without one the row is narrow
        # until a picture lands and then widens, which is a visible reflow
        # mid-scroll. With the slot reserved up front, an app with no icon
        # leaves it empty and every row lines up either way.
        #
        # The size is held by a Frame, not by the Label, and that is not
        # incidental. Tk measures a Label's width in CHARACTERS and its height
        # in LINES, so `width=32, height=32` on a Label means 32 characters
        # across and THIRTY-TWO LINES down - roughly 500 px per row. With a few
        # dozen apps the panel became tens of thousands of pixels tall and the
        # popup rendered completely blank. A Frame's width/height are pixels,
        # so the slot is sized exactly ICON_ROW_PX and the Label fills it.
        icon_box = tk.Frame(row, width=ICON_ROW_PX, height=ICON_ROW_PX,
                            bg=COLORS["bg_alt"])
        icon_box.pack_propagate(False)
        icon_box.pack(side="left", padx=(10, 0), pady=8)
        icon_lbl = tk.Label(icon_box, bg=COLORS["bg_alt"])
        icon_lbl.pack(fill="both", expand=True)
        left = tk.Frame(row, bg=COLORS["bg_alt"])
        left.pack(side="left", fill="x", expand=True, padx=(10, 6), pady=8)
        name = tk.Label(left, text=disp, font=(F, 11, "bold"),
                        bg=COLORS["bg_alt"], fg=COLORS["text"], anchor="w")
        name.pack(fill="x")
        detail = tk.Label(left, text=self._detail_text(group), font=(F, 9),
                          bg=COLORS["bg_alt"], fg=COLORS["subtext"],
                          anchor="w")
        detail.pack(fill="x")
        btn = AnimatedButton(
            row, text="Close",
            command=lambda ps=pids,
            d=disp: self._confirm_close_group(ps, d),
            bg=COLORS["surface"], fg=COLORS["text"],
            font=(F, 9, "bold"), padx=14, pady=5)
        btn.pack(side="right", padx=(0, 10), pady=8)
        self._rows[key] = row
        self._row_widgets[key] = {"frame": row, "name": name,
                                  "detail": detail, "btn": btn,
                                  "icon": icon_lbl, "pids": pids}

    def _load_icons(self, keys):
        """Fill in row icons on a worker, then hand each image to the Tk thread.

        The icon source is the RUNNING IMAGE, not the registry's DisplayIcon:
        for an Electron or store app the registry points at a launcher (often
        Update.exe) while the real icon lives in the .exe actually executing -
        the same reasoning app/icon_resolve documents for the Install tab.

        Three properties this keeps:
          * the plate is the row's own colour, so the opaque square an icon
            carries never shows as a border (the bug fixed in the uninstall
            popup);
          * results are cached by image path, so the 3-second refresh never
            re-extracts anything;
          * the Tk thread only ever sees a finished PhotoImage.
        """
        import threading as _th
        pending = []
        for key in keys:
            w = self._row_widgets.get(key) or {}
            lbl = w.get("icon")
            # Skip only if this group is EXHAUSTED - every image it has is
            # already known to be iconless. Checking the path-keyed cache by
            # group key would re-queue it forever, since the cache stores
            # paths and never the key.
            if lbl is not None and key not in self._icon_done \
                    and key not in self._icon_cache:
                pending.append((key, list(w.get("pids") or [])))
        if not pending:
            return

        def _work():
            out = []
            missed = []
            done = []
            for key, pids in pending:
                # Collect EVERY distinct image for this group, not just the
                # first readable one. A group is one app, and an app often runs
                # several exes: a launcher, an updater, a service, the real
                # binary. Stopping at the first readable path meant the group's
                # icon depended on PID order - if that PID happened to be
                # Update.exe (no icon) the row was blank even though the next
                # PID was the actual application.
                cands = []
                for pid in pids:
                    try:
                        from app.process_manager import _process_image_path
                        _p = _process_image_path(int(pid))
                    except Exception:
                        _p = ""
                    if _p and _p not in cands:
                        cands.append(_p)
                # The negative cache is keyed by PATH, not by group. Keying it
                # by group was the second half of the bug: one unlucky PID
                # marked the whole group permanently iconless, and a group
                # whose processes changed could never recover. Remembering the
                # image that has no icon still stops the repeated work, while
                # letting every other candidate be tried.
                todo = [p for p in cands if p not in self._icon_missed]
                if not cands:
                    # Nothing readable at all - a protected system process.
                    # A sentinel per group keeps this from being re-queued every
                    # tick, without polluting the path-keyed cache.
                    _sent = "\0none:" + str(key)
                    if _sent not in self._icon_missed:
                        missed.append(_sent)
                    continue
                if not todo:
                    continue
                got = False
                for _path in todo:
                    try:
                        from app.icon_extract import icon_ppm_bytes
                        from app.ui.theme import ICON_ROW_PX, icon_bg_rgb
                        ppm = icon_ppm_bytes(_path, size=ICON_ROW_PX,
                                             bg_rgb=icon_bg_rgb(COLORS["bg_alt"]))
                    except Exception:
                        ppm = None
                    if ppm:
                        out.append((key, _path, ppm))
                        got = True
                        break
                    missed.append(_path)
            for _k, _ps in pending:
                if not any(o[0] == _k for o in out):
                    done.append(_k)
            if missed:
                try:
                    self._icon_missed.update(missed)
                except Exception:
                    pass
            if done:
                try:
                    self._icon_done.update(done)
                except Exception:
                    pass
            if not out:
                return
            try:
                # BUG-001: was getattr(app, "_dispatch") with a
                # `self.after(0, ...)` fallback -- another off-thread Tk call
                # hiding behind a branch that could never be taken. Same fix as
                # the group hop above: one hop, and it is the safe one.
                self._dispatch.post(lambda: self._apply_icons(out))
            except Exception:
                pass

        try:
            _th.Thread(target=_work, daemon=True, name="ProcMgrIcons").start()
        except Exception:
            pass

    def _apply_icons(self, found):
        """Tk-thread half: turn finished PPM bytes into PhotoImages."""
        try:
            import tkinter as tk
        except Exception:
            return
        for key, path, ppm in found:
            try:
                img = tk.PhotoImage(data=ppm)
            except Exception:
                continue
            self._icon_cache[key] = img
            w = self._row_widgets.get(key) or {}
            lbl = w.get("icon")
            if lbl is None:
                continue
            try:
                if not lbl.winfo_exists():
                    continue
                lbl.config(image=img)
                lbl.image = img      # keep a reference, or Tk garbage-collects it
            except Exception:
                pass

    def _confirm_close_group(self, pids, display):
        try:
            pids = tuple(int(p) for p in (pids or [])
                         if int(p) != os.getpid())
        except Exception:
            return
        if not pids:
            return
        # Safety net: never touch a protected PID through a stale
        # callback, even though system rows are no longer listed.
        try:
            from app.process_manager import close_process
            protected = set()
            for _it in (self._last_items or []):
                if not _it.get("safe", True):
                    try:
                        protected.add(int(_it.get("pid")))
                    except Exception:
                        pass
            pids = tuple(p for p in pids if p not in protected)
        except Exception:
            from app.process_manager import close_process
        if not pids:
            return
        if len(pids) > 1:
            title, msg = ("Close all?",
                          f"Close all {len(pids)} {display or 'process'} "
                          "processes?\n\nUnsaved work in those windows "
                          "will be lost.")
            yes = "Close all"
        else:
            title, msg = ("Close process?",
                          f"Close {display or 'this process'}?\n\nUnsaved "
                          "work in that app will be lost.")
            yes = "Close"
        try:
            ok = _themed_askyesno(
                self._dlg, title, msg, accent=TAB_ACCENTS["Tools"],
                yes_text=yes, no_text="Keep running")
        except Exception:
            ok = True
        if not ok:
            return
        try:
            done = sum(1 for p in pids if close_process(p, force=True))
        except Exception:
            done = 0
        if done:
            self._say(f"Closed {display}.")
        else:
            self._say(f"Couldn't close {display}.")
        try:
            if self._dlg is not None and self._dlg.winfo_exists():
                self._dlg.after(600, lambda: self._refresh_list())
        except Exception:
            pass

    def close(self):
        self._cancel_tick()
        try:
            super().close()
        except Exception:
            try:
                self.destroy()
            except Exception:
                pass
