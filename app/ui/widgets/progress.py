"""AnimatedProgressBar with an indeterminate shimmer mode, and ProgressRow.

H4. Extracted verbatim from app/gui.py (blueprint B1.2).
"""
from __future__ import annotations

import tkinter as tk

from app.ui.theme import COLORS, _hex_lerp

class AnimatedProgressBar(tk.Canvas):
    """Material 3 linear progress indicator.

    A rounded active indicator (the tab accent) over a tonal track. Real
    fractions fill from the left; indeterminate mode slides a rounded segment
    across the track. Same API as before (set_fraction / set_indeterminate)."""

    THICK = 6

    def __init__(self, parent, accent=COLORS["primary"], height=14, **kw):
        try:
            self._page = parent["bg"]
        except Exception:
            self._page = COLORS["bg"]
        super().__init__(parent, height=height, highlightthickness=0, bd=0,
                         bg=self._page, **kw)
        self._accent = accent
        self._fraction = 0.0
        self._indeterminate = False
        self._shimmer = 0.0
        self._segs = 20          # kept for API compat
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
        bh = self.THICK
        y0 = (h - bh) / 2.0
        y1 = y0 + bh
        track = _hex_lerp(self._page, self._accent, 0.24)
        self._round_rect(0, y0, w, y1, bh / 2.0, fill=track, outline="")
        if self._indeterminate:
            seg = w * 0.35
            x = (self._shimmer / 1.3) * (w + seg) - seg
            x0, x1 = max(0.0, x), min(float(w), x + seg)
            if x1 - x0 > 1:
                self._round_rect(x0, y0, x1, y1, bh / 2.0,
                                 fill=self._accent, outline="")
        elif self._fraction > 0:
            x1 = max(float(bh), self._fraction * w)
            self._round_rect(0, y0, x1, y1, bh / 2.0,
                             fill=self._accent, outline="")

    def _round_rect(self, x0, y0, x1, y1, r, **kw):
        r = max(0.0, min(r, (x1 - x0) / 2.0, (y1 - y0) / 2.0))
        points = [
            x0 + r, y0, x1 - r, y0, x1, y0, x1, y0 + r,
            x1, y1 - r, x1, y1, x1 - r, y1, x0 + r, y1,
            x0, y1, x0, y1 - r, x0, y0 + r, x0, y0,
        ]
        return self.create_polygon(points, smooth=True, **kw)


class ProgressRow(tk.Frame):
    """A strip that keeps the progress bar's height reserved at all times.

    The run bar used to be packed only while something ran and forgotten
    afterwards. `bottom` is the last band of the window, so adding 20px to it
    took 20px away from the page above: every tab's content jumped up when a
    run started, and jumped back when it ended. That is the layout analogue
    of a flicker, and it happened on the one transition the app is named
    after.

    The row is always packed and always the same height; `show()`/`hide()`
    only add or remove the bar INSIDE it. Idle, the strip is blank space
    between the status row and the window edge, which is also where the bar
    will appear, so nothing above it ever moves.

    The bar stays `self.bar` (and the app keeps `self.progress_bar` pointing
    at it), so every existing `set_fraction` / `set_indeterminate` call site
    is unchanged.
    """

    #: 14px bar + 6px of optical breathing room. Fixed, not derived from the
    #: bar's current height, so hiding the bar cannot shrink the strip.
    HEIGHT = 20

    def __init__(self, parent, accent=COLORS["primary"], bg=None, **kw):
        try:
            page_bg = bg or parent["bg"]
        except Exception:
            page_bg = bg or COLORS["bg"]
        super().__init__(parent, bg=page_bg, height=self.HEIGHT, bd=0, **kw)
        # Without this the frame collapses to the height its children ask
        # for, which is zero while the bar is hidden - the entire point.
        self.pack_propagate(False)
        self.grid_propagate(False)
        self.bar = AnimatedProgressBar(self, accent=accent,
                                       height=self.HEIGHT)
        self._shown = False

    @property
    def shown(self) -> bool:
        return self._shown

    def set_accent(self, accent):
        try:
            self.bar._accent = accent
            self.bar._draw()
        except Exception:
            pass

    def show(self):
        if not self._shown:
            self.bar.pack(fill="both", expand=True)
            self._shown = True
        try:
            self.bar._draw()
        except Exception:
            pass

    def hide(self):
        if self._shown:
            self.bar.pack_forget()
            self._shown = False
