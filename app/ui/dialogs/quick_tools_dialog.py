"""QuickToolsDialog — extracted verbatim from app/gui.py (H12, batch 2).

One dialog per module, mirroring app/ui/widgets. Depends only on
app.ui below it, plus the app.utils helpers it already used; it
knows nothing
about any other dialog except where noted.
"""
from __future__ import annotations

import tkinter as tk

from app.ui.widgets.rounded_card import RoundedCard
from tkinter import ttk, messagebox
from app.tools_shortcuts import TOOLS_SHORTCUTS
from app.ui.dialogs._tool_page import ToolPage
from app.ui.theme import COLORS, F, TAB_ACCENTS


class QuickToolsDialog(ToolPage):
    """Quick Tools popup (corner toolbox icon, not a Tools card): the
    exact shortcut box that used to sit inline below the 4 cards —
    same 2 columns (Windows tools / Windows Settings), same one-click
    rows. Same popup dimensions as every other feature dialog
    (ThemedModal proportional size — no custom size). Read-only
    launcher, no busy guard (mirrors card clicks, never touches the
    run engine).

    REDESIGN 2026-10: on the shared ToolPage shell — one Header banner
    (answering "what is this?") instead of a bare title, and the two
    columns live in the content zone.

    DENSITY 2026-10 (popup-fit pass): the rows are one-line menu items
    now (10pt label, no label padding, 10px corners). As 46px tiles with
    a 12pt label and 10px of padding per side they pitched at 72px, so
    the 10-item settings column alone asked for 720px — the whole reason
    this popup had a scrollbar. It now asks for 504px and fits."""

    def __init__(self, parent, app, open_target):
        self.app = app
        self._open_target = open_target
        super().__init__(parent, title="Quick Tools",
                         accent=TAB_ACCENTS["Tools"],
                         scope="One-click Windows tools and settings.",
                         scroll=True)
        self.banner.set_state("\u26A1", TAB_ACCENTS["Tools"])
        self.banner.set_title("Jump straight to a tool")
        self.banner.set_sub("Windows tools and settings, one click away.")
        body = self.content
        try:
            cols = tk.Frame(body, bg=COLORS["bg"])
            cols.pack(fill="x")
            for ci in (0, 1):
                cols.grid_columnconfigure(ci, weight=1, uniform="toolcols")
            for ci, (group, items) in enumerate(TOOLS_SHORTCUTS):
                self._build_column(cols, ci, group, items)
        except Exception:
            pass

    def _build_column(self, parent, col, title, items):
        cell = tk.Frame(parent, bg=COLORS["bg"])
        cell.grid(row=0, column=col, sticky="new", padx=10)
        tk.Label(cell, text=title, font=(F, 12, "bold"),
                 bg=COLORS["bg"], fg=COLORS["primary"],
                 anchor="w").pack(fill="x", padx=4, pady=(0, 6))
        for label, target in items:
            self._make_bullet(cell, label, target)

    def _make_bullet(self, parent, label, target):
        """One launcher tile: a rounded row with a big label and a chevron.

        Tile metrics (2026-10 popup-fit pass): 46px minimum, 12pt label and
        10px of vertical padding per side made a 72px row pitch, so the
        10-item "Windows Settings" column alone came to 720px - the popup's
        content was 770px against a 535px content zone at 150% DPI, and that
        scrollbar is what the user saw. These are one-line menu rows: 11pt
        labels with 4px of padding read the same and pitch at ~43px.
        """
        rest, hover = COLORS["bg_alt"], COLORS["surface_hover"]
        tile = RoundedCard(parent, fill=rest, bg=COLORS["bg"], radius=12,
                           min_height=32)
        tile.pack(fill="x", pady=2)
        lbl = tk.Label(tile.body, text=label, font=(F, 10), bg=rest,
                       fg=COLORS["text"], cursor="hand2", anchor="w",
                       bd=0, highlightthickness=0)
        # No vertical padding on the label: the card's own rounded inset
        # (radius * 0.29 each side) already holds the text off the edges.
        lbl.pack(side="left", fill="x", expand=True, padx=(14, 0))
        chev = tk.Label(tile.body, text="\u203A", font=(F, 12), bg=rest,
                        fg=COLORS["muted"], cursor="hand2", bd=0,
                        highlightthickness=0)
        chev.pack(side="right", padx=(0, 14))

        def _paint(on):
            col = hover if on else rest
            tile.set_fill(col)
            for w in (lbl, chev):
                w.config(bg=col)
            chev.config(fg=COLORS["primary"] if on else COLORS["muted"])

        for w in (tile, tile.body, lbl, chev):
            w.bind("<Enter>", lambda e: _paint(True), add="+")
            w.bind("<Leave>", lambda e: _paint(False), add="+")
            w.bind("<Button-1>",
                   lambda e, t=target: self._launch(t), add="+")

    def _launch(self, target):
        try:
            self._open_target(target)
        except Exception:
            pass
