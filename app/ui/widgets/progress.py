"""AnimatedProgressBar with an indeterminate shimmer mode.

H4. Extracted verbatim from app/gui.py (blueprint B1.2).
"""
from __future__ import annotations

import tkinter as tk

from app.ui.theme import COLORS, _hex_lerp, F

class AnimatedProgressBar(tk.Canvas):
    """Segmented progress bar with a sliding glow. Real fraction fills green
    segments; while a task is active an indeterminate shimmer runs on top."""

    def __init__(self, parent, accent=COLORS["accent_green"], height=14, **kw):
        super().__init__(parent, height=height, highlightthickness=0, bd=0,
                         bg=COLORS["bg_widget"], **kw)
        self._accent = accent
        self._fraction = 0.0
        self._indeterminate = False
        self._shimmer = 0.0
        self._segs = 20
        self.bind("<Configure>", lambda e: self._draw())

    def set_fraction(self, fraction: float):
        self._fraction = max(0.0, min(1.0, fraction))
        self._draw()

    def set_indeterminate(self, on: bool):
        # M10: a second set_indeterminate(True) without an intervening
        # False used to start a second _shimmer_loop, orphaning the first
        # (double speed + one un-cancellable timer). Re-entry is a no-op;
        # a fresh start cancels any in-flight tick first.
        if on and self._indeterminate and getattr(self, "_shimmer_after", None) is not None:
            return
        self._indeterminate = on
        if on:
            try:
                if getattr(self, "_shimmer_after", None) is not None:
                    self.after_cancel(self._shimmer_after)
                    self._shimmer_after = None
            except Exception:
                pass
            self._shimmer_loop()
        # audit fix (minor): turning it off left the last scheduled
        # _shimmer_loop after() running one more frame (and redrawing the
        # shimmer highlight on top of a finished bar). Cancel any in-flight
        # tick and redraw once from the real fraction.
        else:
            try:
                if getattr(self, "_shimmer_after", None) is not None:
                    self.after_cancel(self._shimmer_after)
                    self._shimmer_after = None
            except Exception:
                pass
            self._draw()

    def _shimmer_loop(self):
        if not self._indeterminate:
            return
        self._shimmer = (self._shimmer + 0.04) % (1.0 + 0.3)
        self._draw()
        self._shimmer_after = self.after(30, self._shimmer_loop)

    def _draw(self):
        self.delete("all")
        w = self.winfo_width()
        h = self.winfo_height()
        if w < 10:
            return
        gap = 3
        seg_w = (w - gap * (self._segs - 1)) / self._segs
        lit = self._fraction * self._segs
        for i in range(self._segs):
            x = i * (seg_w + gap)
            # partial segment alpha via color blend
            seg_t = max(0.0, min(1.0, lit - i))
            if self._indeterminate:
                dist = abs(((i / self._segs) + 0.5) - self._shimmer) % 1.0
                dist = min(dist, 1.0 - dist)
                seg_t = max(seg_t, max(0.0, 0.65 - dist * 2.2))
            color = _hex_lerp(COLORS["surface"], self._accent, seg_t)
            self._round_rect(x, 2, x + seg_w, h - 2, 3, fill=color, outline="")

    def _round_rect(self, x0, y0, x1, y1, r, **kw):
        points = [
            x0 + r, y0, x1 - r, y0, x1, y0, x1, y0 + r,
            x1, y1 - r, x1, y1, x1 - r, y1, x0 + r, y1,
            x0, y1, x0, y1 - r, x0, y0 + r, x0, y0,
        ]
        return self.create_polygon(points, smooth=True, **kw)
