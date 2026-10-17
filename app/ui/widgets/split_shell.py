"""SplitShell: the fixed two-column body shared by the list-style tools.

Services and Network Connections both want "list on the left, detail on the
right", and both grew that by hand with different gaps and different
proportions. This is the one version: two grid columns at a fixed ratio with
a hairline between them, so the seam lands in the same place in every popup
at every window size.

The ratio is a weight pair, not a pixel split, so the columns rescale with
the dialog instead of one side eating the space when the window narrows.

Depends only on app.ui.theme.
"""
from __future__ import annotations

import tkinter as tk

from app.ui.theme import COLORS


class SplitShell(tk.Frame):
    """`.left` and `.right` frames on a fixed ratio, with a hairline seam."""

    def __init__(self, parent, left_ratio=0.58, gap=14, bg=None):
        self._page_bg = bg or (parent["bg"] if isinstance(
            parent, (tk.Frame, tk.Canvas)) else COLORS["bg"])
        super().__init__(parent, bg=self._page_bg, bd=0)
        self._gap = int(gap)

        lw = max(1, int(round(left_ratio * 1000)))
        rw = max(1, 1000 - lw)
        self.grid_columnconfigure(0, weight=lw, uniform="split")
        self.grid_columnconfigure(1, weight=0, minsize=self._gap)
        self.grid_columnconfigure(2, weight=rw, uniform="split")
        self.grid_rowconfigure(0, weight=1)

        self.left = tk.Frame(self, bg=self._page_bg, bd=0)
        self.left.grid(row=0, column=0, sticky="nsew")

        seam = tk.Frame(self, bg=COLORS["hairline"], width=1, bd=0)
        seam.grid(row=0, column=1, sticky="ns", padx=self._gap // 2)

        self.right = tk.Frame(self, bg=self._page_bg, bd=0)
        self.right.grid(row=0, column=2, sticky="nsew")

    def set_ratio(self, left_ratio):
        lw = max(1, int(round(left_ratio * 1000)))
        self.grid_columnconfigure(0, weight=lw)
        self.grid_columnconfigure(2, weight=max(1, 1000 - lw))

    def column_width(self, which="right"):
        """Live pixel width of one column, for wraplength. 0 before layout."""
        try:
            w = (self.left if which == "left" else self.right).winfo_width()
            return w if w > 20 else 0
        except Exception:
            return 0