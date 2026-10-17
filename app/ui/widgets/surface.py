"""Surface: a rounded, hairline-bordered panel that fills whatever it is
given.

RoundedCard sizes ITSELF to its content (it has to - a Canvas asks for
nothing). That is right for a card in a stack and wrong for a pane that must
fill a grid cell: the card keeps re-requesting its content's height and the
two fight. Surface does the opposite - it never requests anything, it paints
a rounded face at whatever size the layout hands it and resizes its content
frame to match. Use it for the regions that fill the window; use RoundedCard
for things that hug their text.

Depends only on app.ui.theme.
"""
from __future__ import annotations

import math
import tkinter as tk

from app.ui.theme import COLORS, SHAPE_M, _round_rect_points


class Surface(tk.Canvas):
    def __init__(self, parent, *, fill=None, radius=SHAPE_M, pad=1, bg=None,
                 outline=None, height=1, width=1):
        page_bg = bg or COLORS["bg"]
        super().__init__(parent, highlightthickness=0, bd=0, bg=page_bg,
                         height=height, width=width)
        self._fill = fill or COLORS["bg_alt"]
        self._radius = int(radius)
        self._pad = int(pad)
        self._outline = outline or ""   # M3 filled surface: no stroke
        # square body must sit inside the rounded face (arc depth ~0.29r)
        self._inset = self._pad + int(math.ceil(self._radius * 0.29))
        self.body = tk.Frame(self, bg=self._fill, bd=0)
        self._win = self.create_window(self._inset, self._inset, window=self.body,
                                       anchor="nw")
        self._size = (0, 0)
        self.bind("<Configure>", self._on_configure)

    def _on_configure(self, e):
        w, h = e.width, e.height
        if (w, h) == self._size or w < 4 or h < 4:
            return
        self._size = (w, h)
        self.delete("face")
        self.create_polygon(
            _round_rect_points(1, 1, w - 2, h - 2, self._radius),
            smooth=True, fill=self._fill, outline=self._outline,
            tags="face")
        self.tag_lower("face")
        inset = self._inset
        self.coords(self._win, inset, inset)
        self.itemconfigure(self._win, width=max(1, w - inset * 2),
                           height=max(1, h - inset * 2))

    def set_fill(self, color):
        self._fill = color
        self.body.config(bg=color)
        self._size = (0, 0)
        self.event_generate("<Configure>", width=self.winfo_width(),
                            height=self.winfo_height())
