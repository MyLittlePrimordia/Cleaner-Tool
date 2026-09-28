"""QuickToolsDialog — extracted verbatim from app/gui.py (H12, batch 2).

One dialog per module, mirroring app/ui/widgets. Depends only on
app.ui below it, plus the app.utils helpers it already used; it
knows nothing
about any other dialog except where noted.
"""
from __future__ import annotations

import tkinter as tk
from tkinter import ttk, messagebox
from app.tools_shortcuts import TOOLS_SHORTCUTS
from app.ui.base import ThemedModal
from app.ui.theme import COLORS, F, TAB_ACCENTS


class QuickToolsDialog(ThemedModal):
    """Quick Tools popup (corner toolbox icon, not a Tools card): the
    exact shortcut box that used to sit inline below the 4 cards —
    same 2 columns (Windows tools / Windows Settings), same bullet
    rows. Same popup dimensions as every other feature dialog
    (ThemedModal 800x600 default — no custom size). Read-only
    launcher, no busy guard (mirrors card clicks, never touches the
    run engine)."""

    def __init__(self, parent, app, open_target):
        self.app = app
        self._open_target = open_target
        # NOTE: no size= passed — ThemedModal WIDTHxHEIGHT default keeps
        # this popup at the exact same dimensions as DNS/Health/etc.
        super().__init__(parent, title="Quick Tools",
                         accent=TAB_ACCENTS["Tools"])
        body = self.body
        try:
            tk.Label(body, text="Windows tools and settings.",
                     font=(F, 9), bg=COLORS["bg"],
                     fg=COLORS["subtext"], wraplength=680,
                     justify="left").pack(anchor="w", pady=(0, 8))
        except Exception:
            pass
        try:
            cols = tk.Frame(body, bg=COLORS["bg"])
            cols.pack(fill="both", expand=True)
            for ci in (0, 1):
                cols.grid_columnconfigure(ci, weight=1, uniform="toolcols")
            for ci, (group, items) in enumerate(TOOLS_SHORTCUTS):
                self._build_column(cols, ci, group, items)
        except Exception:
            pass

    def _build_column(self, parent, col, title, items):
        cell = tk.Frame(parent, bg=COLORS["bg"])
        cell.grid(row=0, column=col, sticky="new", padx=8)
        tk.Label(cell, text=title, font=(F, 10, "bold"),
                 bg=COLORS["bg"], fg=COLORS["text"],
                 anchor="w").pack(fill="x")
        tk.Frame(cell, bg=COLORS["hairline"], height=1).pack(
            fill="x", pady=(4, 6))
        for label, target in items:
            self._make_bullet(cell, label, target)

    def _make_bullet(self, parent, label, target):
        row = tk.Frame(parent, bg=COLORS["bg"])
        row.pack(fill="x", pady=2)
        dot = tk.Label(row, text="•", font=(F, 9, "bold"),
                       bg=COLORS["bg"], fg=TAB_ACCENTS["Tools"],
                       bd=0, highlightthickness=0)
        dot.pack(side="left", padx=(2, 4))
        lbl = tk.Label(row, text=label, font=(F, 9, "bold"),
                       bg=COLORS["bg"], fg=COLORS["text"],
                       cursor="hand2", anchor="w",
                       bd=0, highlightthickness=0)
        lbl.pack(side="left", fill="x", expand=True)
        for w in (row, dot, lbl):
            w.bind("<Enter>", lambda e, l=lbl: l.config(
                fg=TAB_ACCENTS["Tools"]), add="+")
            w.bind("<Leave>", lambda e, l=lbl: l.config(
                fg=COLORS["text"]), add="+")
            w.bind("<Button-1>",
                   lambda e, t=target: self._launch(t), add="+")

    def _launch(self, target):
        try:
            self._open_target(target)
        except Exception:
            pass
