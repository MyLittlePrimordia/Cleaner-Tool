"""ToggleSwitch: iOS-style animated toggle.

H4. Extracted verbatim from app/gui.py (blueprint B1.2).
"""
from __future__ import annotations

import tkinter as tk

from app.ui.theme import COLORS, _hex_lerp, F

class ToggleSwitch(tk.Canvas):
    """Material 3 switch: 52x32 track, 16px thumb (off) growing to 24px (on).

    Off: hollow pill — the surface behind the switch shows through, ringed
    by a 2px outline with an outline-coloured thumb. On: primary track,
    on-primary thumb carrying a check glyph. Hover and keyboard focus paint
    the M3 state-layer halo around the thumb. Everything is a smooth polygon
    so edges stay antialiased on Windows."""

    W, H = 52, 32
    EASE = 0.32              # per-tick approach @16ms (settles ~200ms)
    #: Fallback only: the OFF track is painted in the surface BEHIND the
    #: switch (see _off_track), so the pill stays hollow on any card or row.
    TRACK_OFF = COLORS["surface_hover"]
    OUTLINE_OFF = COLORS["muted"]
    THUMB_OFF = COLORS["muted"]
    TRACK_ON = COLORS["primary"]
    THUMB_ON = COLORS["on_primary"]
    ICON_ON = COLORS["on_primary_container"]

    def __init__(self, parent, variable: tk.BooleanVar, command=None,
                 accent=None, **kw):  # accent kept for API compat, ignored
        super().__init__(parent, highlightthickness=0, bd=0,
                         bg=parent["bg"] if isinstance(parent, (tk.Frame, tk.Canvas)) else COLORS["bg"],
                         width=self.W, height=self.H, cursor="hand2", **kw)
        self._var = variable
        self._command = command
        self._pos = 1.0 if variable.get() else 0.0   # 0 off .. 1 on
        self._vel = 0.0                              # kept for API compat (no overshoot now)
        self._target = self._pos
        self._enabled = True
        self._anim_after = None
        self._focused = False
        self.bind("<Button-1>", self._toggle)
        self.bind("<Enter>", lambda e: self._draw(hover=True))
        self.bind("<Leave>", lambda e: self._draw())
        # keyboard accessibility (audit a11y): space/Return toggle, Tab focuses
        self.configure(takefocus=1)
        self.bind("<space>", lambda e: self._toggle(e))
        self.bind("<Return>", lambda e: self._toggle(e))
        self.bind("<FocusIn>", self._on_focus_in)
        self.bind("<FocusOut>", self._on_focus_out)
        variable.trace_add("write", lambda *_: self._sync_from_var())
        self._draw()

    def _on_focus_in(self, _e=None):
        self._focused = True
        self._draw()

    def _on_focus_out(self, _e=None):
        self._focused = False
        self._draw()

    # -- interaction -------------------------------------------------- #

    def _toggle(self, _):
        if not self._enabled:
            return
        self._var.set(not self._var.get())
        if self._command:
            self.after(10, self._command)

    def _sync_from_var(self):
        self._target = 1.0 if self._var.get() else 0.0
        self._start_anim()

    def set_enabled(self, enabled: bool):
        self._enabled = enabled
        self._draw()

    # -- ease-out animation -------------------------------------------- #

    def _start_anim(self):
        if self._anim_after is not None:
            try:
                self.after_cancel(self._anim_after)
            except Exception:
                pass
        self._tick_anim()

    def _tick_anim(self):
        # critically damped: monotonic approach, settles exact, no wobble
        self._pos += (self._target - self._pos) * self.EASE
        if abs(self._target - self._pos) < 0.004:
            self._pos = self._target
            self._vel = 0.0
            self._draw()
            self._anim_after = None
            return
        self._draw()
        self._anim_after = self.after(16, self._tick_anim)

    # -- rendering ------------------------------------------------------ #

    def _off_track(self):
        """Colour of the unselected track: the surface behind the switch.

        M3 specifies surfaceContainerHighest here, which on this dark
        palette is #333537 — on a #1E1F20 card that renders as a grey
        FILLED pill and reads as a disabled control (user report 2026-10:
        "the gray fill for the toggle options looks weird"). Painting the
        track in the switch's own background leaves exactly the M3
        outline + thumb pill on every surface the switch is placed on
        (cards, rows, popovers), and still eases cleanly into the primary
        fill as it turns on."""
        try:
            return self.cget("bg")
        except Exception:
            return self.TRACK_OFF

    def _draw(self, hover=False):
        self.delete("all")
        w, h = self.W, self.H
        p = self._pos
        cy = h / 2
        if self._enabled:
            track = _hex_lerp(self._off_track(), self.TRACK_ON, p)
            rim = _hex_lerp(self.OUTLINE_OFF, self.TRACK_ON, p)
            thumb = _hex_lerp(self.THUMB_OFF, self.THUMB_ON, p)
        else:
            track = _hex_lerp(COLORS["bg"], COLORS["text"], 0.12)
            rim = track
            thumb = _hex_lerp(COLORS["bg"], COLORS["text"], 0.38)
        # outline (fades out as the switch turns on), then the track inside it
        self._capsule(1, 1, w - 1, h - 1, (h - 2) / 2, fill=rim)
        if p < 0.999 or not self._enabled:
            self._capsule(3, 3, w - 3, h - 3, (h - 6) / 2, fill=track)
        d = 16 + 8 * p
        cx = 16 + p * (w - 32)
        if self._enabled and (hover or self._focused):
            halo = _hex_lerp(track, thumb, 0.18)
            self._circle(cx, cy, h - 2, fill=halo)
        self._circle(cx, cy, d, fill=thumb)
        if p > 0.85 and self._enabled:
            # check glyph on the selected thumb
            k = d / 24.0
            self.create_line(cx - 4.5 * k, cy + 0.2 * k, cx - 1.5 * k,
                             cy + 3.2 * k, cx + 4.5 * k, cy - 3.2 * k,
                             fill=self.ICON_ON, width=2, capstyle="round",
                             joinstyle="round")

    def _capsule(self, x0, y0, x1, y1, r, **kw):
        """One smooth polygon: rectangle with two TRUE semicircle caps. The
        old version listed only the cap corners and let smooth=True guess
        the curve, which flattened the ends (user feedback: ends weren't
        fully rounded around the knob). Now both ends are sampled along
        real arcs (8 segments each), so the track hugs the circular knob
        with perfect semicircles — still one antialiased shape."""
        import math
        pts = [x0 + r, y0, x1 - r, y0]                      # top edge
        cx, cy = x1 - r, (y0 + y1) / 2                      # right cap
        for i in range(9):
            a = math.radians(-90 + 180 * i / 8)
            pts += [cx + r * math.cos(a), cy + r * math.sin(a)]
        pts += [x1 - r, y1, x0 + r, y1]                      # bottom edge
        cx = x0 + r                                          # left cap
        for i in range(9):
            a = math.radians(90 + 180 * i / 8)
            pts += [cx + r * math.cos(a), cy + r * math.sin(a)]
        return self.create_polygon(pts, smooth=True, **kw)

    def _circle(self, cx, cy, d, **kw):
        """Antialiased circle: 16-point polygon with smooth=True. With few
        points + smooth, Tk renders a clean conic — crisper than
        create_oval's non-antialiased rasterization at small sizes."""
        import math
        pts = []
        for i in range(16):
            a = 2 * math.pi * i / 16
            pts.append(cx + d / 2 * math.cos(a))
            pts.append(cy + d / 2 * math.sin(a))
        return self.create_polygon(pts, smooth=True, **kw)
