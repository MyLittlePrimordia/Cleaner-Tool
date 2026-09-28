"""_CatalogPickerDialog — extracted verbatim from app/gui.py (H12, batch 1).

One dialog per module, mirroring app/ui/widgets. Depends only on
app.ui below it, plus the app.utils helpers it already used; it
knows nothing
about any other dialog except where noted.
"""
from __future__ import annotations

import tkinter as tk
from tkinter import ttk, messagebox
from app.ui.base import ThemedModal, Tooltip
from app.ui.theme import COLORS, F, TAB_ACCENTS, _hex_lerp
from app.ui.widgets import AnimatedButton, RoundedEntry, ScrollableRoundedPanel


class _CatalogPickerDialog(ThemedModal):
    """Searchable installed-games/apps picker (Phase-5 pilot overhaul).

    One shared ThemedModal (fixed footprint, X, Escape, scrim) showing a
    scrollable, filterable list of catalog entries. Live keyboard filter
    (RoundedEntry), launcher chip on each row, click to pick.

    Enumeration NEVER blocks the open: pass scan_fn (zero-arg callable)
    and the dialog maps instantly with an honest "Scanning…" row while a
    daemon thread enumerates; results land via a Tk-thread hop. A big
    Steam library or registry on a cold disk must never freeze the UI
    (user bug report: first click froze, then the picker jerked in).
    Precomputed `entries` still works (fast sources, tests).
    """

    def __init__(self, parent, title, entries=None, accent=None,
                 on_pick=None, empty_text="Nothing found on this PC.",
                 scan_fn=None, scanning_text="Scanning…"):
        # entries: precomputed iterable of {name, launcher, path}, or
        # None when scan_fn supplies them asynchronously
        self._all = list(entries or [])
        self._shown = []
        self._on_pick = on_pick
        self._empty_text = empty_text
        self._scanning_text = scanning_text
        self._scanning = scan_fn is not None
        self._scan_token = [False]   # True = stopped/obsolete
        super().__init__(parent, title=title,
                         accent=accent or TAB_ACCENTS["Tweak"])
        body = self.body

        self._search_var = tk.StringVar()
        self._search_var.trace_add("write", lambda *_: self._apply_filter())
        entry = RoundedEntry(body, textvariable=self._search_var)
        entry.pack(fill="x", pady=(2, 6))
        # placeholder text (RoundedEntry has no -placeholder option — Tk's
        # Entry doesn't support one; same convention as the Install tab's
        # search box: insert literal text, clear/restore on focus, and
        # _apply_filter treats it as an empty query)
        self._search_entry = entry.entry
        self._search_placeholder = "Type to filter…"
        self._search_entry.insert(0, self._search_placeholder)
        self._search_focused = False
        self._search_entry.bind("<FocusIn>", self._filter_focus_in)
        self._search_entry.bind("<FocusOut>", self._filter_focus_out)

        self._list_panel = panel = ScrollableRoundedPanel(body)
        panel.pack(fill="both", expand=True, pady=(0, 4))
        self._list_body = panel.inner
        self._apply_filter()

        brow = tk.Frame(body, bg=COLORS["bg"])
        brow.pack(fill="x", pady=(4, 2))
        AnimatedButton(brow, text="Cancel", command=self.close,
                       bg=COLORS["surface"], fg=COLORS["text"],
                       font=(F, 9, "bold"), padx=18, pady=6).pack(side="right")

        if scan_fn is not None:
            # closing a dialog with a sweep in flight is safe: the poll
            # chain checks _modal_closed first (see _check_sweep), the
            # worker only ever writes the holder dict, and the thread is
            # a daemon (dies with the process)
            self._sweep_holder = {"done": False, "found": []}
            try:
                import threading as _th
                _th.Thread(target=lambda: self._sweep_worker(scan_fn),
                           daemon=True,
                           name="CatalogPickerScan").start()
            except Exception:
                self._scanning = False
                self._apply_filter()
            else:
                try:
                    self._dlg.after(150, lambda: self._check_sweep())
                except Exception:
                    pass

    def _sweep_worker(self, scan_fn):
        """Worker thread: enumerate into the holder dict. Pure Python —
        NEVER a Tk call (foreign Tk calls burn ~1s and raise unless the
        main thread happens to be inside event dispatch). The Tk-thread
        poller below publishes."""
        try:
            found = scan_fn() or []
        except Exception:
            found = []
        try:
            self._sweep_holder["found"] = list(found)
        except Exception:
            pass
        try:
            self._sweep_holder["done"] = True
        except Exception:
            pass

    def _check_sweep(self):
        """Tk-thread poller: publish swept entries once ready. Stops on
        close; re-arms while the sweep flies."""
        try:
            if self._modal_closed:
                return
            if not self._dlg.winfo_exists():
                return
        except Exception:
            return
        try:
            holder = self._sweep_holder
        except Exception:
            return
        if not holder.get("done"):
            try:
                self._dlg.after(150, lambda: self._check_sweep())
            except Exception:
                pass
            return
        self._all = list(holder.get("found") or [])
        self._scanning = False
        self._apply_filter()

    def _cancel_scan(self):
        # retained for API compat (older callers may invoke it); the
        # holder/closed checks in _check_sweep make it a no-op path
        try:
            self._scan_token[0] = True
        except Exception:
            pass

    def _filter_focus_in(self, _e):
        if not self._search_focused:
            self._search_focused = True
            if self._search_var.get() == self._search_placeholder:
                self._search_var.set("")

    def _filter_focus_out(self, _e):
        self._search_focused = False
        if not self._search_var.get().strip():
            self._search_var.set(self._search_placeholder)

    def _apply_filter(self):
        if not hasattr(self, "_list_body"):
            return   # trace can fire while inserting the placeholder,
                     # before the list panel exists yet — nothing to do
        q = (self._search_var.get() or "").strip().lower()
        if q == self._search_placeholder.lower():
            q = ""
        for w in list(self._list_body.winfo_children()):
            try:
                w.destroy()
            except Exception:
                pass
        shown = 0
        for g in self._all:
            if q and q not in g.get("name", "").lower() \
                    and q not in g.get("launcher", "").lower():
                continue
            self._build_row(g)
            shown += 1
            if shown >= 300:      # hard cap: pathological registries
                break
        if not shown:
            # scanning state gets its own honest placeholder (not the
            # empty state — "nothing found" while still searching lies)
            text = (self._scanning_text if getattr(self, "_scanning", False)
                    else self._empty_text)
            tk.Label(self._list_body, text=text,
                     font=(F, 9), bg=COLORS["bg_alt"],
                     fg=COLORS["subtext"]).pack(pady=14)
        try:
            self._list_panel.refresh_scroll()
        except Exception:
            pass

    def _build_row(self, g):
        row = tk.Frame(self._list_body, bg=COLORS["bg_alt"])
        inner = tk.Frame(row, bg=COLORS["bg_alt"])
        inner.pack(fill="both", expand=True)
        chip = tk.Label(inner, text=g.get("launcher", "?"), font=(F, 8, "bold"),
                        bg=_hex_lerp(COLORS["bg_alt"], COLORS["accent_sky"], 0.18),
                        fg=COLORS["accent_sky"], padx=6, pady=2)
        chip.pack(side="left", padx=(8, 6), pady=6)
        name = g.get("name", "?")
        tip = (f"{g.get('launcher', '')} — {g.get('folder', '')}"
               if g.get("folder") else name)
        lbl = tk.Label(inner, text=name, font=(F, 9, "bold"),
                       bg=COLORS["bg_alt"], fg=COLORS["text"],
                       anchor="w", cursor="hand2")
        lbl.pack(side="left", anchor="center")
        try:
            Tooltip(lbl, tip)
        except Exception:
            pass
        for w in (row, inner, chip, lbl):
            try:
                w.bind("<Button-1>",
                       lambda e, _g=g: self._pick(_g), add="+")
            except Exception:
                pass
        row.pack(fill="x", pady=1)

    def _pick(self, g):
        cb = self._on_pick
        self.close()
        if cb is not None:
            try:
                cb(g)
            except Exception:
                pass
