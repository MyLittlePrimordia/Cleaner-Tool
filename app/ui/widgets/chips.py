"""ChipBar: one row of mutually exclusive filter chips.

The old filter control (SegmentedTabs) was built to be a two-or-three-way
mode switch: it forces equal-width segments, slides a solid accent thumb and
sizes itself from its widest label. Used as a FILTER it went wrong in
visible ways - Services stacked two of them (so "Running now" and "Any" were
both lit at once and only one of them counted), Duplicates stacked three,
and each stretched to a different width.

A chip bar is what a filter actually is: content-width chips in a single
flowing row, one lit at a time, optional counts so the user can see what a
filter would show BEFORE choosing it. Flat, no animation, no equal widths.

Depends only on app.ui.theme.
"""
from __future__ import annotations

import tkinter as tk

from app.ui.theme import (COLORS, F, _hex_lerp, _measure_font,
                          _round_rect_points)

CHIP_H = 28
GAP = 6
PAD_X = 13
FONT = (F, 9)
FONT_ON = (F, 9, "bold")
FONT_N = (F, 8)


class ChipBar(tk.Canvas):
    """`options` = [(key, label), ...]; `command(key)` fires on a change."""

    def __init__(self, parent, options, initial=None, command=None,
                 accent=None, label=None, bg=None):
        self._page_bg = bg or COLORS["bg"]
        super().__init__(parent, height=CHIP_H, bg=self._page_bg,
                         highlightthickness=0, bd=0)
        self._opts = [(k, str(t)) for k, t in options]
        self._sel = initial if initial is not None else (
            self._opts[0][0] if self._opts else None)
        self._cmd = command
        self._accent = accent or COLORS["accent_pink"]
        self._label = label
        self._counts = {}
        self._hover = None
        self._boxes = []
        self._enabled = True
        self.bind("<Configure>", lambda e: self._draw())
        self.bind("<Button-1>", self._on_click)
        self.bind("<Motion>", self._on_motion)
        self.bind("<Leave>", lambda e: self._set_hover(None))
        self._draw()

    # api
    @property
    def selected(self):
        return self._sel

    def select(self, key, notify=False):
        if key == self._sel:
            return
        self._sel = key
        self._draw()
        if notify and self._cmd is not None:
            self._cmd(key)

    def set_counts(self, counts):
        self._counts = dict(counts or {})
        self._draw()

    def set_enabled(self, on):
        self._enabled = bool(on)
        self._draw()

    def needed_width(self):
        try:
            return self._layout()[-1]
        except Exception:
            return 0

    # internals
    def _fonts(self):
        top = self.winfo_toplevel()
        return (_measure_font(top, FONT), _measure_font(top, FONT_ON),
                _measure_font(top, FONT_N))

    def _layout(self):
        f, fb, fn = self._fonts()
        x = 0
        boxes = []
        if self._label:
            x += fn.measure(self._label) + 10
        for key, text in self._opts:
            n = self._counts.get(key)
            tw = fb.measure(text) if key == self._sel else f.measure(text)
            nw = (fn.measure(str(n)) + 7) if n is not None else 0
            w = tw + nw + PAD_X * 2
            boxes.append((key, text, x, x + w, n))
            x += w + GAP
        return boxes + [x - GAP]

    def _draw(self):
        self.delete("all")
        try:
            lay = self._layout()
        except Exception:
            return
        boxes = lay[:-1]
        self._boxes = boxes
        try:
            if int(self.cget("width")) != int(lay[-1]) + 2:
                self.configure(width=int(lay[-1]) + 2)
        except Exception:
            pass
        if self._label:
            self.create_text(0, CHIP_H / 2, text=self._label, font=FONT_N,
                             fill=COLORS["muted"], anchor="w")
        tint = _hex_lerp(COLORS["bg"], self._accent, 0.16)
        for key, text, x0, x1, n in boxes:
            on = key == self._sel
            hov = key == self._hover and self._enabled
            if on:
                # M3 selected filter chip: tonal container, no outline
                fill = _hex_lerp(COLORS["bg"], self._accent, 0.26)
                outline, fg = "", COLORS["text"]
            else:
                # M3 unselected filter chip: a filled "surface container"
                # chip, NOT an outlined one. The outline-variant hairline
                # read as a stray thin white line against the page (user
                # report), and M3's tonal surfaces are the app's house
                # style anyway (see the palette's "no hairline tricks"
                # note). Hover lifts the fill one tonal step.
                fill = (COLORS["surface"] if hov else COLORS["bg_alt"])
                outline = ""
                fg = COLORS["text"] if hov else COLORS["subtext"]
            if not self._enabled:
                fg = COLORS["muted"]
            self.create_polygon(
                _round_rect_points(x0 + 1, 2, x1 - 1, CHIP_H - 2,
                                   min(8, (CHIP_H - 4) // 2)),
                smooth=True, fill=fill, outline=outline)
            has_n = n is not None
            nw = 0
            if has_n:
                f, fb, fn = self._fonts()
                nw = fn.measure(str(n)) + 7
            tx = x0 + PAD_X
            self.create_text(tx, CHIP_H / 2, text=text, anchor="w",
                             font=FONT_ON if on else FONT, fill=fg)
            if has_n:
                self.create_text(x1 - PAD_X, CHIP_H / 2, text=str(n),
                                 anchor="e", font=FONT_N,
                                 fill=self._accent if on else COLORS["muted"])

    def _key_at(self, x):
        for key, _t, x0, x1, _n in self._boxes:
            if x0 <= x <= x1:
                return key
        return None

    def _set_hover(self, key):
        if key != self._hover:
            self._hover = key
            self._draw()

    def _on_motion(self, e):
        k = self._key_at(e.x) if self._enabled else None
        self._set_hover(k)
        try:
            self.config(cursor="hand2" if k is not None else "")
        except Exception:
            pass

    def _on_click(self, e):
        if not self._enabled:
            return
        k = self._key_at(e.x)
        if k is not None and k != self._sel:
            self._sel = k
            self._draw()
            if self._cmd is not None:
                self._cmd(k)
