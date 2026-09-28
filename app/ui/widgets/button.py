"""AnimatedButton: canvas pill with hover/press easing.

H4. Extracted verbatim from app/gui.py (blueprint B1.2).
"""
from __future__ import annotations

import tkinter as tk
from tkinter import messagebox

from app.ui.theme import COLORS, _hex_lerp, _lerp, _measure_font, F

class AnimatedButton(tk.Canvas):
    """Rounded button with press/relax animation and hover glow. Renders
    text inside the canvas so the whole pill animates as one piece."""

    def __init__(self, parent, text, command=None, bg=COLORS["surface"],
                 fg=COLORS["text"], hover_bg=None, font=None, padx=26, pady=10,
                 width=None, height=None, **kw):
        super().__init__(parent, highlightthickness=0, bd=0,
                         bg=parent["bg"] if isinstance(parent, (tk.Frame, tk.Canvas)) else COLORS["bg"],
                         **kw)
        self._text = text
        self._command = command
        self._bg = bg
        self._fg = fg
        self._hover_bg = hover_bg or _hex_lerp(bg, "#FFFFFF", 0.08)
        self._font = font or (F, 10, "bold")
        self._padx = padx
        self._pady = pady
        self._press = 0.0          # 0 relaxed .. 1 pressed
        self._hover = 0.0
        # Must exist BEFORE any <Enter>/<Leave> can fire — Tk can deliver
        # a hover event before a click ever happens (user-reported crash).
        self._pressing = False
        self._hovering = False
        self._current_bg = bg
        self._enabled = True
        self._fixed_w = width
        self._fixed_h = height
        self._focus_ring = False

        # keyboard accessibility (audit a11y finding): canvas widgets are
        # invisible to Tab focus by default — accept focus, activate on
        # Return/Space. Focus indication is drawn IN-CANVAS (user feedback:
        # Tk's native highlightthickness box read as a stray 'framework
        # outline' left behind after clicking) — no native focus rectangle.
        self.configure(takefocus=1, highlightthickness=0)
        self.bind("<ButtonPress-1>", self._on_press)
        self.bind("<ButtonRelease-1>", self._on_release)
        self.bind("<Enter>", self._on_enter)
        self.bind("<Leave>", self._on_leave)
        self.bind("<FocusIn>", lambda e: self._draw_focus(True))
        self.bind("<FocusOut>", lambda e: self._draw_focus(False))
        self.bind("<Return>", self._on_key_activate)
        self.bind("<space>", self._on_key_activate)
        self._measure()
        self._draw()

    def _measure(self):
        # F7: ask the cached shared font instead of a throwaway tk.Label.
        # A text-only Label's request size is font.measure(text)+6 wide by
        # font.metrics("linespace")+6 tall (Tk Label border/padding
        # defaults — verified equal to real Label widgets across every
        # in-app font/text pair). Single-line text only, like every button
        # label in this UI; multiline would need per-line measuring.
        fnt = _measure_font(self.winfo_toplevel(), self._font)
        w = fnt.measure(self._text) + 6
        h = fnt.metrics("linespace") + 6
        w += self._padx * 2
        h += self._pady * 2
        if self._fixed_w:
            w = self._fixed_w
        if self._fixed_h:
            h = self._fixed_h
        self.config(width=w, height=h)
        # NOTE: _w/_h are tkinter's reserved widget-path attribute names —
        # store pixel size under _bw/_bh instead (clobbering _w makes the
        # widget's Tcl path an integer and every later canvas call fails
        # with "invalid command name").
        self._bw, self._bh = w, h

    def _draw(self):
        self.delete("all")
        # Press: shrink 3% and darken slightly; hover: lighten slightly.
        t = self._press
        shrink = t * 0.04
        w = self._bw * (1 - shrink)
        h = self._bh * (1 - shrink)
        x0 = (self._bw - w) / 2
        y0 = (self._bh - h) / 2
        color = _hex_lerp(self._current_bg, self._hover_bg, self._hover)
        color = _hex_lerp(color, "#000000", t * 0.12)
        if not self._enabled:
            # disabled: muted flat fill, dimmed text
            color = _hex_lerp(color, COLORS["bg"], 0.45)
        r = h / 2 if h < 44 else 14
        self._round_rect(x0, y0, x0 + w, y0 + h, r, fill=color, outline="")
        if self._focus_ring and self._enabled:
            # in-canvas keyboard focus: 1px brightened outline just inside
            # the pill — visible only while actually Tab-focused
            self._round_rect(x0 + 1.5, y0 + 1.5, x0 + w - 1.5, y0 + h - 1.5,
                             max(1, r - 1.5), outline=_hex_lerp(color, "#FFFFFF", 0.55),
                             width=1)
        fg = self._fg if self._enabled else _hex_lerp(self._fg, COLORS["bg"], 0.45)
        self.create_text(
            self._bw / 2, self._bh / 2, text=self._text,
            font=self._font, fill=fg,
        )

    def _round_rect(self, x0, y0, x1, y1, r, **kw):
        points = [
            x0 + r, y0, x1 - r, y0, x1, y0, x1, y0 + r,
            x1, y1 - r, x1, y1, x1 - r, y1, x0 + r, y1,
            x0, y1, x0, y1 - r, x0, y0 + r, x0, y0,
        ]
        return self.create_polygon(points, smooth=True, **kw)

    def _draw_focus(self, on: bool):
        """Keyboard focus state — rendered in-canvas by _draw (no native
        Tk focus rectangle; user feedback called that a stray outline)."""
        self._focus_ring = on
        self._draw()

    # animation loop
    def _animate(self):
        target_p = 1.0 if self._pressing else 0.0
        target_h = 1.0 if self._hovering else 0.0
        moved = False
        if abs(self._press - target_p) > 0.02:
            self._press = _lerp(self._press, target_p, 0.35)
            moved = True
        if abs(self._hover - target_h) > 0.02:
            self._hover = _lerp(self._hover, target_h, 0.30)
            moved = True
        if moved:
            self._draw()
            self.after(16, self._animate)
        else:
            self._press, self._hover = target_p, target_h
            self._draw()

    def _on_press(self, _):
        if self._enabled:
            self._pressing = True
            self._animate()

    def _on_release(self, _):
        if self._enabled and getattr(self, "_pressing", False):
            self._pressing = False
            self._animate()
            # small haptic-feel delay then fire
            self.after(60, self._fire)

    def _on_key_activate(self, _):
        """Keyboard activation for a Tab-focused button (<Return>/<space>).

        M2 audit fix: this used to call _on_release directly, but that path
        only fires the command when <ButtonPress-1> has first set
        _pressing — so keyboard focus was advertised (takefocus=1) while
        Enter/Space never fired anything. The mouse path is press (set
        _pressing) -> release (fire); a key press has no separate press
        event, so synthesize the press half and run the exact same release
        sequence (same animation, same 60ms-then-fire). The `not
        self._pressing` guard stops held-key auto-repeat from re-firing the
        command."""
        if self._enabled and not self._pressing:
            self._pressing = True
            self._on_release(None)

    def _fire(self):
        if self._enabled and self._command:
            try:
                self._command()
            except Exception as exc:
                # audit fix: bare except made a buggy command a silent no-op
                # — the click appeared to do nothing with zero feedback.
                import traceback
                try:
                    messagebox.showerror(
                        "Unexpected Error",
                        f"Something went wrong running that button:\n\n{exc}\n\n"
                        f"(Full details are in the exported log: the 📋 corner icon)",
                    )
                except Exception:
                    pass
                # surface to stderr too — captured by Export Logs when a
                # console exists (source runs), invisible in windowed builds
                traceback.print_exc()

    def _on_enter(self, _):
        if self._enabled:
            self._hovering = True
            self._animate()

    def _on_leave(self, _):
        self._hovering = False
        self._pressing = False
        self._animate()

    def set_style(self, bg=None, fg=None, hover_bg=None):
        if bg:
            self._bg = self._current_bg = bg
        if fg:
            self._fg = fg
        if hover_bg:
            self._hover_bg = hover_bg
        self._draw()

    def set_enabled(self, enabled: bool):
        self._enabled = enabled
        self._draw()

    def config_text(self, text: str):
        if text == self._text:
            return  # F7: unchanged label — skip re-measure + full redraw
        self._text = text
        self._measure()
        self._draw()
