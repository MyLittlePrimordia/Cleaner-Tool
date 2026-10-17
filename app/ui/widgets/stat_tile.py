"""StatTile: one equal-width number tile for a tool dialog's headline row.

The six Tools popups had six different ways of showing a headline - a bare
label, a 20pt hero number, a grade pill, a sentence - which is most of why
they did not look like a family. This is the one shape a number takes
everywhere: a rounded tile with the VALUE large and the LABEL small beneath.

Fixed width is the point. A row of tiles is only symmetrical when every tile
is the same size regardless of how long its value is, so `width` is a real
constructor argument and the value label is clipped rather than allowed to
widen the tile. Callers lay them out with grid_columnconfigure(..., weight=1)
or pack them side by side with equal gaps; either way they line up.

Depends only on app.ui.theme, like every other widget.
"""
from __future__ import annotations

import tkinter as tk

from app.ui.theme import COLORS, F, _measure_font, _round_rect_points

#: Tall enough for a 13pt value plus an 8pt label with breathing room, short
#: enough that four tiles never push a tool dialog's list out of view.
#:
#: Named TILE_H rather than HEIGHT on purpose: module-level `WIDTH =` /
#: `HEIGHT =` names are how a dialog pins its own footprint (see
#: app/ui/base.py), so a widget uses a distinct name to keep the two
#: concepts apart. A widget is not a dialog.
TILE_H = 56
RADIUS = 12      # M3 medium shape


class StatTile(tk.Canvas):
    """A single fixed-width tile: big value, small label, optional accent.

    .set(value, accent=None) updates in place. An accent of None keeps the
    current one, so a caller can colour a value once (green for "Protected")
    and then keep updating the text without restating the colour.
    """

    VALUE_FONT = (F, 13, "bold")
    LABEL_FONT = (F, 8)

    def __init__(self, parent, label, value="—", accent=None, width=118,
                 bg=None):
        self._page_bg = bg or (parent["bg"] if isinstance(
            parent, (tk.Frame, tk.Canvas)) else COLORS["bg"])
        super().__init__(parent, width=int(width), height=TILE_H,
                         highlightthickness=0, bd=0, bg=self._page_bg)
        self._label = str(label or "")
        self._value = str(value if value is not None else "—")
        self._accent = accent
        self._fill = COLORS["bg_alt"]
        try:
            self.bind("<Configure>", lambda _e: self._draw())
        except Exception:
            pass
        self.after_idle(self._draw)

    # ---- api ---------------------------------------------------------- #

    def set(self, value, accent=None):
        """New value, and optionally a new accent colour for it."""
        self._value = str(value if value is not None else "—")
        if accent is not None:
            self._accent = accent
        self._draw()

    def set_label(self, label):
        self._label = str(label or "")
        self._draw()

    def set_accent(self, accent):
        self._accent = accent
        self._draw()

    # ---- paint -------------------------------------------------------- #

    def _draw(self):
        try:
            self.delete("tile")
            w, h = self.winfo_width(), self.winfo_height()
            if w < 8 or h < 8:
                return
            self.create_polygon(
                _round_rect_points(1, 1, w - 2, h - 2, RADIUS),
                smooth=True, fill=self._fill, outline="",
                tags="tile")
            fg = self._accent or COLORS["text"]
            # Clip the value to the tile so a long number cannot push the
            # label out of alignment - equal widths beat always-readable.
            fnt = _measure_font(self.winfo_toplevel(), self.VALUE_FONT)
            room = max(10, w - 16)
            text = self._value
            if fnt.measure(text) > room:
                while text and fnt.measure(text + "…") > room:
                    text = text[:-1]
                text = (text + "…") if text else "…"
            self.create_text(w / 2, h * 0.38, text=text, font=self.VALUE_FONT,
                             fill=fg, tags="tile")
            self.create_text(w / 2, h * 0.74, text=self._label,
                             font=self.LABEL_FONT, fill=COLORS["subtext"],
                             tags="tile")
        except Exception:
            pass