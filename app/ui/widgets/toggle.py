"""ToggleSwitch: iOS-style animated toggle.

H4. Extracted verbatim from app/gui.py (blueprint B1.2).
"""
from __future__ import annotations

import tkinter as tk

from app.ui.theme import COLORS, _hex_lerp, F

class ToggleSwitch(tk.Canvas):
    """iOS-style toggle, MK3 (user feedback: 'low-res, unpolished, hover
    shows a framework outline').

    What changed vs MK2:
      * SIZE: 44x24 (iOS 'mini' proportions — same shape language, less
        vertical bulk, enables 2-column Custom grids).
      * ANTIALIASING: Tk's create_oval/create_rectangle are NOT antialiased
        on Windows — the capsule caps and knob had visibly jagged edges
        (the 'low-res' look). Everything is now drawn with smooth=True
        polygons (Tk antialiases those), so all edges are crisp.
      * NO HOVER RIM: the old hover affordance drew 3 outline shapes
        slightly OUTSIDE the capsule bounds — at this scale it read as a
        boxy 'framework outline' around the clickable area (user-reported).
        Hover now just brightens the track; keyboard focus brightens it
        slightly more. No outlines anywhere.

    Motion: unchanged critically-damped ease-out (~200ms), no overshoot.
    Track color crossfades muted slate → iOS green (#34C759); knob white
    when ON, gray when OFF so state reads at a glance."""

    # iOS-mini metrics (points ≈ px at 100% scaling)
    W, H = 44, 24
    INSET = 1.5
    KNOB_INSET = 2.5
    GREEN = "#34C759"        # Apple system green
    KNOB_ON = "#FFFFFF"
    KNOB_OFF = "#C3CAD4"     # gray when off — state reads at a glance
    SHADOW = "#000000"
    OFF_TRACK = "#3E4A5A"     # neutral slate, matches dark UI
    EASE = 0.32              # per-tick approach @16ms (settles ~200ms)

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

    @property
    def _knob_d(self):
        return self.H - self.KNOB_INSET * 2

    def _travel(self):
        return self.W - self.KNOB_INSET * 2 - self._knob_d

    def _draw(self, hover=False):
        self.delete("all")
        w, h = self.W, self.H
        ins = self.INSET
        self._focused_now = self._focused
        # track: ONE smooth capsule polygon (rect middle + semicircle ends
        # in a single antialiased shape) — MK2's rect+2-ovals had unantialiased
        # seams and jagged caps.
        r = (h - ins * 2) / 2
        x0, y0, x1, y1 = ins, ins, w - ins, h - ins
        track = _hex_lerp(self.OFF_TRACK, self.GREEN, self._pos)
        if hover and self._enabled:
            track = _hex_lerp(track, "#FFFFFF", 0.12)
        elif self._focused_now and self._enabled:
            track = _hex_lerp(track, "#FFFFFF", 0.08)
        self._capsule(x0, y0, x1, y1, r, fill=track)
        # knob: smooth-polygon circle (antialiased, unlike create_oval)
        kd = self._knob_d
        cx = self.KNOB_INSET + kd / 2 + self._pos * self._travel()
        knob = _hex_lerp(self.KNOB_OFF, self.KNOB_ON, self._pos)
        if self._enabled:
            self._circle(cx + 0.5, self.KNOB_INSET + kd / 2 + 1.5, kd,
                          fill=self.SHADOW, stipple="gray25")
        self._circle(cx, self.KNOB_INSET + kd / 2, kd, fill=knob)
        # NOTE (user feedback): the old hover rim — 3 outline shapes drawn
        # slightly outside the capsule — is GONE. It read as a 'framework
        # outline' around the clickable area at this scale.

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
