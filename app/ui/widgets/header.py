"""Header: the one block of text every Tools popup opens with.

A coloured disc with one glyph, a headline that answers the popup's question,
and one line of context. Nothing else - no stat tiles, no counters. The old
Banner packed three or four numbers beside the headline; for a normal user
they were noise, and at 125-150% display scaling the fixed 72px strip
clipped its own subtitle.

Sized by its text, never by a pixel constant: both labels wrap to the real
window width, so a long sentence grows the header instead of being cut off.

Depends only on app.ui.theme.
"""
from __future__ import annotations

import tkinter as tk

from app.ui.theme import COLORS, ICON_HERO, TYPE, _hex_lerp, SPACE_3
from app.ui.metrics import TEXT_NUDGE

DISC = ICON_HERO


class Header(tk.Frame):
    def __init__(self, parent, accent=None):
        super().__init__(parent, bg=COLORS["bg"], bd=0)
        self._title_full = ""
        self._sub_full = ""
        self._disc = tk.Canvas(self, width=DISC, height=DISC,
                               bg=COLORS["bg"], highlightthickness=0, bd=0)
        self._disc.pack(side="left", anchor="n", padx=(TEXT_NUDGE, SPACE_3))
        col = tk.Frame(self, bg=COLORS["bg"], bd=0)
        col.pack(side="left", fill="x", expand=True)
        self._title = tk.Label(col, text="", font=TYPE["banner_title"],
                               bg=COLORS["bg"], fg=COLORS["text"],
                               anchor="w", justify="left", bd=0)
        self._title.pack(fill="x")
        self._sub = tk.Label(col, text="", font=TYPE["caption"],
                             bg=COLORS["bg"], fg=COLORS["subtext"],
                             anchor="w", justify="left", bd=0)
        self._sub.pack(fill="x", pady=(TEXT_NUDGE, 0))
        col.bind("<Configure>", self._wrap)
        self.set_state("", COLORS["muted"])

    def _wrap(self, e):
        w = max(120, e.width - 4)
        self._title.config(wraplength=w)
        self._sub.config(wraplength=w)

    def set_state(self, glyph, color):
        c = self._disc
        c.delete("all")
        color = color or COLORS["muted"]
        tint = _hex_lerp(COLORS["bg"], color, 0.20)
        # M3 tonal icon container: tinted disc, no outline
        c.create_oval(3, 3, DISC - 3, DISC - 3, fill=tint, outline="")
        if glyph:
            c.create_text(DISC / 2, DISC / 2, text=str(glyph),
                          font=TYPE["readout_bold"], fill=color)

    def set_title(self, text, color=None):
        self._title_full = str(text or "")
        self._title.config(text=self._title_full,
                           fg=color or COLORS["text"])

    def set_sub(self, text, color=None):
        self._sub_full = str(text or "")
        self._sub.config(text=self._sub_full, fg=color or COLORS["subtext"])
        if self._sub_full:
            self._sub.pack(fill="x", pady=(TEXT_NUDGE, 0))
        else:
            self._sub.pack_forget()
