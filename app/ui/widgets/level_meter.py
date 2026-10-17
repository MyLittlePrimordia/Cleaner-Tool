"""LevelMeter: a horizontal audio level meter in dBFS.

The Mic Check dialog drew its level as a half-circle dial (the same visual
as the Speed Test) plus a thin 14px bar underneath. A dial is a good shape
for "how fast" and a poor one for "how loud": the needle sweeps a 180-degree
arc that is mostly dead zone, there are no dB marks, and the one number that
matters when setting up a mic - the PEAK - was only mentioned in a sentence.

REDESIGN 2026-10: the bar is ONE solid rounded fill, not a row of little
segments with gaps. The segmented version lit like a hardware LED ladder;
as a single continuous shape (the way Material's own progress/level shapes
read) it is a cleaner, calmer bar that still colours by zone - green: fine,
amber: hot, red: about to clip. Under it, a dB ruler, a held peak marker
that falls slowly so a short shout is still visible a second later, and a
CLIP lamp that stays lit until the peak decays. No hairline outlines
anywhere (they read as stray thin white lines against the dark page).

Depends only on app.ui.theme.
"""
from __future__ import annotations

import tkinter as tk

from app.ui.theme import COLORS, F, _round_rect_points

FLOOR_DB = -60.0
TICKS = (-60, -48, -36, -24, -12, -6, 0)
BAR_H = 26
RULER_H = 18


class LevelMeter(tk.Canvas):
    def __init__(self, parent, bg=None):
        super().__init__(parent, height=BAR_H + RULER_H,
                         bg=bg or COLORS["bg"], highlightthickness=0, bd=0)
        self._db = FLOOR_DB
        self._hold = FLOOR_DB
        self._clip = False
        self.bind("<Configure>", lambda e: self._draw())

    def set_level(self, db, hold_db, clip=False):
        """db: current peak in dBFS (-60 = silence). hold_db: held peak."""
        self._db = max(FLOOR_DB, min(0.0, float(db)))
        self._hold = max(FLOOR_DB, min(0.0, float(hold_db)))
        self._clip = bool(clip)
        self._draw()

    def _x_of(self, db, x0, span):
        return x0 + span * (db - FLOOR_DB) / (0.0 - FLOOR_DB)

    def _draw(self):
        self.delete("all")
        w = self.winfo_width()
        if w < 80:
            return
        lamp_w = 52
        x0, x1 = 2, w - lamp_w - 12
        span = x1 - x0
        inner0, inner1 = x0 + 3, x1 - 3
        ispan = max(1, inner1 - inner0)
        r = (BAR_H - 4) / 2
        # Track: a plain tonal pill. No outline (the old hairline outline
        # read as a stray thin white line - user report).
        self.create_polygon(
            _round_rect_points(x0, 2, x1, BAR_H - 2, r),
            smooth=True, fill=COLORS["bg_alt"], outline="")
        # Level: ONE solid rounded fill from the left, coloured by the
        # current zone. No segment gaps, so it reads as a single bar.
        frac = max(0.0, min(1.0, (self._db - FLOOR_DB) / (0.0 - FLOOR_DB)))
        if frac > 0.01:
            fw = ispan * frac
            colour = (COLORS["accent_red"] if self._db > -3 else
                      COLORS["accent_yellow"] if self._db > -12 else
                      COLORS["accent_green"])
            fr = min(r, max(1.0, fw / 2))
            self.create_polygon(
                _round_rect_points(inner0, 2, min(inner1, inner0 + fw),
                                   BAR_H - 2, fr),
                smooth=True, fill=colour, outline="")
        # Held peak marker.
        if self._hold > FLOOR_DB + 1:
            px = self._x_of(self._hold, inner0, ispan)
            self.create_rectangle(px - 1, 4, px + 1, BAR_H - 4,
                                  fill=COLORS["text"], outline="")
        for t in TICKS:
            tx = self._x_of(t, inner0, ispan)
            self.create_line(tx, BAR_H + 1, tx, BAR_H + 5,
                             fill=COLORS["muted"])
            self.create_text(min(max(tx, 10), x1 - 8), BAR_H + 6 + 6,
                             text=str(t) if t else "0 dB",
                             font=(F, 8), fill=COLORS["muted"])
        lx = x1 + 10
        on = self._clip
        self.create_polygon(
            _round_rect_points(lx, 2, lx + lamp_w, BAR_H - 2, r), smooth=True,
            fill=COLORS["accent_red"] if on else COLORS["bg_alt"],
            outline="")
        self.create_text(lx + lamp_w / 2, BAR_H / 2, text="CLIP",
                         font=(F, 8, "bold"),
                         fill=COLORS["black"] if on else COLORS["muted"])
