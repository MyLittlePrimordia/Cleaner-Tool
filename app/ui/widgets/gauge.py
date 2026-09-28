"""GaugeDial: eased arc gauge for the Speed Test dialog.

H4. Extracted verbatim from app/gui.py (blueprint B1.2).
"""
from __future__ import annotations

import math
import time
import tkinter as tk

from app.ui.theme import COLORS, TAB_ACCENTS, _hex_lerp, F

class GaugeDial(tk.Canvas):
    """Semicircular speed gauge — house custom-draw style (smooth
    polylines + _hex_lerp bands, like AnimatedButton/ProgressBar).

    Log scale 0..1000 Mbps so slow links stay readable; the needle
    eases toward set_value() on a 16ms tick. Tk-thread only (the
    dialog hops worker progress through _ui). Best-effort: any draw
    failure leaves the last good frame, never raises."""

    MAX_MBPS = 1000.0
    # Evenly-spaced tick table (Ookla-style): one equal-angle step per
    # tick, so the dial reads symmetrically. fraction_for() lerps
    # piecewise between them — needle, sweep and ticks all share it.
    TICKS = (0, 5, 10, 50, 100, 250, 500, 750, 1000)

    def __init__(self, parent, accent=None, width=360, height=215):
        super().__init__(parent, width=width, height=height,
                         highlightthickness=0, bd=0, bg=COLORS["bg"])
        self._accent = accent or TAB_ACCENTS["Tools"]
        # NOTE: _w/_h are tkinter's reserved widget-path attrs (clobbering
        # _w breaks every later Tk call — same trap as AnimatedButton);
        # pixel size lives under _bw/_bh.
        self._bw, self._bh = width, height
        self._shown = 0.0
        self._target = 0.0
        self._anim_after = None
        try:
            self.bind("<Destroy>", self._on_destroy, add="+")
        except Exception:
            pass
        self._draw()

    @staticmethod
    def fraction_for(mbps, cap=MAX_MBPS):
        """Tick-table 0..1 (pure — unit-tested): equal angle per tick,
        piecewise-linear between tick values."""
        try:
            v = max(0.0, float(mbps))
        except Exception:
            return 0.0
        try:
            ticks = GaugeDial.TICKS
            if v <= ticks[0]:
                return 0.0
            if v >= ticks[-1]:
                return 1.0
            n = len(ticks) - 1
            for i in range(n):
                lo, hi = ticks[i], ticks[i + 1]
                if lo <= v <= hi:
                    t = (v - lo) / (hi - lo) if hi > lo else 0.0
                    return (i + max(0.0, min(1.0, t))) / n
            return 1.0
        except Exception:
            return 0.0

    def set_value(self, mbps):
        try:
            v = max(0.0, float(mbps))
        except Exception:
            v = 0.0
        self._target = v
        self._kick()

    def reset(self):
        self._shown = 0.0
        self._target = 0.0
        try:
            if self._anim_after is not None:
                self.after_cancel(self._anim_after)
                self._anim_after = None
        except Exception:
            pass
        self._draw()

    def _kick(self):
        if self._anim_after is not None:
            return
        self._tick()

    def _tick(self):
        self._anim_after = None
        try:
            if not self.winfo_exists():
                return
        except Exception:
            return
        try:
            diff = self._target - self._shown
            if abs(diff) < 0.15 and abs(diff) < max(0.5, self._target * 0.01):
                self._shown = self._target
                self._draw()
                return
            self._shown = self._shown + diff * 0.22
            self._draw()
        except Exception:
            return
        try:
            self._anim_after = self.after(16, self._tick)
        except Exception:
            self._anim_after = None

    def _on_destroy(self, _e=None):
        try:
            if self._anim_after is not None:
                self.after_cancel(self._anim_after)
        except Exception:
            pass
        self._anim_after = None

    def _point(self, cx, cy, radius, frac):
        import math
        theta = math.pi * (1.0 - max(0.0, min(1.0, frac)))
        return (cx + radius * math.cos(theta), cy - radius * math.sin(theta))

    def _arc_points(self, cx, cy, radius, frac_to, steps=60):
        pts = []
        for i in range(int(steps * max(0.0, min(1.0, frac_to))) + 1):
            f = (i / steps)
            pts.extend(self._point(cx, cy, radius, f))
        return pts

    def _draw(self):
        try:
            self.delete("all")
        except Exception:
            return
        try:
            import math
            w, h = self._bw, self._bh
            cx, cy = w / 2.0, h - 22.0
            radius = min(w / 2.0 - 34.0, h - 58.0)
            # track (full sweep, hairline-bright)
            track = self._arc_points(cx, cy, radius, 1.0)
            if len(track) >= 4:
                self.create_line(*track, fill=COLORS["hairline"], width=10,
                                 capstyle="round", smooth=True)
            # value sweep: solid Tools-pink (user call) with a radiant
            # under-glow (wide dim pass + bright core — Tk has no
            # blur/alpha, so glow is faked the house way with layered
            # smooth polylines, like the Ookla dial).
            # Sweep fix (user bug: grainy bar, unfilled start): the sweep
            # is ONE continuous polyline, never chunked — chunk joints
            # antialias into seams/speckle and caps read as dots. Round
            # caps + a dot on BOTH ends bury the track's own overhang so
            # the fill starts flush at the arc base.
            frac = self.fraction_for(self._shown)
            if frac > 0.005:
                npts = 90
                pts = []
                for i in range(npts + 1):
                    pts.extend(self._point(cx, cy, radius, frac * i / npts))
                if len(pts) >= 4:
                    try:
                        self.create_line(
                            *pts, fill=_hex_lerp(self._accent, COLORS["bg"], 0.62),
                            width=11, capstyle="round", smooth=True)
                    except Exception:
                        pass
                    try:
                        self.create_line(*pts, fill=self._accent, width=6,
                                         capstyle="round", smooth=True)
                    except Exception:
                        pass
                try:
                    sx, sy = self._point(cx, cy, radius, 0.0)
                    self.create_oval(sx - 3, sy - 3, sx + 3, sy + 3,
                                     fill=self._accent, outline="")
                    hx, hy = self._point(cx, cy, radius, frac)
                    hr = 4
                    self.create_oval(hx - hr, hy - hr, hx + hr, hy + hr,
                                     fill=self._accent,
                                     outline="")
                except Exception:
                    pass
            # ticks + labels (evenly spaced — one step per tick; large
            # labels parked well clear, user call). Ticks end a full 10px
            # inside the glow edge and labels sit 22px further in still,
            # so lines never touch numerals at any font scale.
            for t in self.TICKS:
                f = self.fraction_for(float(t))
                x0, y0 = self._point(cx, cy, radius - 20.0, f)
                x1, y1 = self._point(cx, cy, radius - 10.0, f)
                try:
                    self.create_line(x0, y0, x1, y1, fill=COLORS["subtext"], width=2)
                    lx, ly = self._point(cx, cy, radius - 42.0, f)
                    self.create_text(lx, ly, text=f"{t}", font=(F, 9),
                                     fill=COLORS["text"])
                except Exception:
                    pass
            # needle + pink hub (user call: the joint stays Tools-pink
            # while the sweep carries the spectrum)
            try:
                nx, ny = self._point(cx, cy, radius - 16.0, frac)
                self.create_line(cx, cy, nx, ny, fill=COLORS["text"], width=3,
                                 capstyle="round")
                r = 7
                self.create_oval(cx - r, cy - r, cx + r, cy + r,
                                 fill=TAB_ACCENTS["Tools"], outline="")
            except Exception:
                pass
        except Exception:
            pass
