"""StatusIcon: a drawn Material status glyph (check / warning / error).

Tk renders colour emoji such as the check-mark button or the warning sign as
flat monochrome boxes on Windows, so status rows draw their own: a filled
circle in the status colour with a dark symbol on it.
"""
from __future__ import annotations

import tkinter as tk

from app.ui.theme import COLORS, ICON_CHIP


class StatusIcon(tk.Canvas):
    KINDS = {
        "ok": "accent_green",
        "warn": "accent_yellow",
        "bad": "accent_red",
        "info": "accent_blue",
    }

    def __init__(self, parent, kind="ok", bg=None, size=ICON_CHIP):
        try:
            page = bg or parent["bg"]
        except Exception:
            page = COLORS["bg"]
        super().__init__(parent, width=size, height=size, bg=page,
                         highlightthickness=0, bd=0)
        col = COLORS[self.KINDS.get(kind, "accent_blue")]
        ink = COLORS["black"]
        s = float(size)
        self.create_oval(1, 1, s - 1, s - 1, fill=col, outline="")
        c = s / 2.0
        k = s / 22.0
        if kind == "ok":
            self.create_line(c - 5 * k, c + 0.5 * k, c - 1.5 * k, c + 4 * k,
                             c + 5.5 * k, c - 3.5 * k, fill=ink, width=2,
                             capstyle="round", joinstyle="round")
        elif kind == "bad":
            d = 4 * k
            self.create_line(c - d, c - d, c + d, c + d, fill=ink, width=2,
                             capstyle="round")
            self.create_line(c - d, c + d, c + d, c - d, fill=ink, width=2,
                             capstyle="round")
        else:  # warn / info: exclamation mark
            self.create_line(c, c - 5 * k, c, c + 1.5 * k, fill=ink, width=2,
                             capstyle="round")
            self.create_oval(c - 1.1 * k, c + 4 * k, c + 1.1 * k, c + 6.2 * k,
                             fill=ink, outline="")
