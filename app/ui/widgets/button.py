"""AnimatedButton: canvas pill with hover/press easing.

H4. Extracted verbatim from app/gui.py (blueprint B1.2).
"""
from __future__ import annotations

import tkinter as tk
from tkinter import messagebox

from app.ui.theme import (COLORS, _hex_lerp, _lerp, _measure_font, F,
                          STATE_HOVER, STATE_PRESS, DISABLED_CONTAINER,
                          _round_rect_points,
                          DISABLED_CONTENT)

#: Gap between a button's optional leading icon and its text (redesign
#: 2026-10). Shared by _measure (reserves the space) and _draw (places the
#: two items), so the icon and the label cannot drift apart.
ICON_GAP = 7

class AnimatedButton(tk.Canvas):
    """Material 3 button (full-pill container, label-large text).

    Interaction follows the M3 state-layer model instead of motion tricks:
    hover paints an 8% overlay of the content colour over the container,
    press 10%, keyboard focus adds a 2px primary ring, and disabled is a 12%
    on-surface container with 38% content. There is no scale-on-press.

    The class keeps the bg/fg/hover_bg API every caller already uses; `bg`
    is the container colour and `fg` the content colour, so a filled button
    is (accent, on-accent), a tonal button is (surface, text), and so on."""

    def __init__(self, parent, text, command=None,
                 bg=COLORS["secondary_container"],
                 fg=COLORS["on_secondary_container"], hover_bg=None, font=None,
                 padx=24, pady=9,
                 width=None, height=None, image=None, radius=None,
                 variant=None, **kw):
        super().__init__(parent, highlightthickness=0, bd=0,
                         bg=parent["bg"] if isinstance(parent, (tk.Frame, tk.Canvas)) else COLORS["bg"],
                         **kw)
        self._text = text
        # M3 shape/variant. radius=None -> full pill; radius=16 -> the extended
        # FAB shape. variant: None (filled/tonal: bg is the container),
        # "outlined" (page-coloured container + outline-variant stroke,
        # primary-coloured label) or "text" (no container until hovered).
        self._radius = radius
        self._variant = variant
        try:
            self._page_bg = parent["bg"]
        except Exception:
            self._page_bg = COLORS["bg"]
        # Optional leading ICON (redesign 2026-10). Added so a themed PNG can
        # sit inside a pill the same way it sits on a card - otherwise the
        # preset cards carry generated artwork while the one remaining button
        # that names a preset ("Game Session") has to keep an emoji, and a
        # lone system-font glyph beside seven geometric icons is exactly the
        # inconsistency this icon set exists to remove.
        #
        # The image is held on the instance: a canvas item references an image
        # by NAME only, so without this the PhotoImage would be collected and
        # the button would render nothing.
        self._image = image
        self._command = command
        self._bg = bg
        self._fg = fg
        self._hover_bg = hover_bg or _hex_lerp(bg, fg, STATE_HOVER)
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
        # in-app font/text pair). Single-line text only, like every other
        # button label in this UI; multiline would need per-line measuring.
        fnt = _measure_font(self.winfo_toplevel(), self._font)
        self._text_w = fnt.measure(self._text)
        w = self._text_w + 6
        h = fnt.metrics("linespace") + 6
        # Icon column: the image's own size plus a fixed gap. The image is
        # scaled to the text's line height rather than to its natural size,
        # so a 48px asset does not make a 10pt pill 48px tall.
        if self._image is not None:
            try:
                iw = self._image.width()
                ih = self._image.height()
                target = max(10, fnt.metrics("linespace") - 2)
                # Tk cannot SCALE a PhotoImage - only subsample it by an
                # integer factor - so the factor has to be rounded, not
                # floored. Flooring was the bug: 48//27 == 1, so a 48px asset
                # beside 10pt text was never reduced at all and the icon
                # overhung the pill's text line.
                biggest = max(iw, ih)
                if biggest > target:
                    f = max(2, int(round(biggest / float(target))))
                    # Subsample the CALLER's image only if it is ours to
                    # shrink; the caller keeps the original alive (the preset
                    # cards hold theirs in _card_photos), so this cannot
                    # cause a collected PhotoImage.
                    self._image = self._image.subsample(f, f)
                    iw, ih = iw // f, ih // f
                self._icon_box = (iw, ih)
                w += iw + ICON_GAP
            except Exception:
                self._icon_box = None
        else:
            self._icon_box = None
        w += self._padx * 2
        h += self._pady * 2
        # M3 touch target: a text button is never shorter than 32px (36px for
        # 10pt+ labels), whatever padding a legacy call site passed. Tiny
        # inline buttons (8pt) are left alone so list rows keep their density.
        try:
            _sz = abs(int(self._font[1])) if isinstance(self._font, tuple) else 10
        except Exception:
            _sz = 10
        if not self._fixed_h and self._text:
            if _sz >= 10:
                h = max(h, 36)
            elif _sz == 9:
                h = max(h, 32)
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
        w = self._bw
        h = self._bh
        x0 = y0 = 0
        outline = ""
        if self._variant in ("outlined", "text"):
            base = self._page_bg
            if self._enabled:
                color = _hex_lerp(base, self._fg, self._hover * STATE_HOVER)
                color = _hex_lerp(color, self._fg, self._press * STATE_PRESS)
                fg = self._fg
                if self._variant == "outlined":
                    outline = COLORS["hairline"]
            else:
                color = base
                fg = _hex_lerp(COLORS["bg"], COLORS["text"], DISABLED_CONTENT)
                if self._variant == "outlined":
                    outline = _hex_lerp(COLORS["bg"], COLORS["text"],
                                        DISABLED_CONTAINER)
        elif self._enabled:
            # Container -> hover layer -> press layer (M3 state layers).
            color = _hex_lerp(self._current_bg, self._hover_bg, self._hover)
            color = _hex_lerp(color, self._fg, self._press * STATE_PRESS)
            fg = self._fg
        else:
            # Disabled: 12% on-surface container, 38% on-surface content.
            color = _hex_lerp(COLORS["bg"], COLORS["text"], DISABLED_CONTAINER)
            fg = _hex_lerp(COLORS["bg"], COLORS["text"], DISABLED_CONTENT)
        r = self._radius if self._radius is not None else h / 2
        ins = 1 if outline else 0      # keep the 1px stroke inside the canvas
        self._round_rect(x0 + ins, y0 + ins, x0 + w - ins, y0 + h - ins,
                         max(0, r - ins), fill=color, outline=outline)
        if self._focus_ring and self._enabled:
            # M3 focus indicator: 2px primary ring, drawn just inside.
            self._round_rect(x0 + 1.5, y0 + 1.5, x0 + w - 1.5, y0 + h - 1.5,
                             max(1, r - 1.5), outline=COLORS["primary"],
                             width=2)
        # Icon then text, as one optically centred group. Without an icon this
        # reduces to the original single centred text item, unchanged.
        iw = ih = 0
        if self._image is not None and getattr(self, "_icon_box", None):
            iw, ih = self._icon_box
            group_w = iw + ICON_GAP + self._text_w
            self.create_image(self._bw / 2 - group_w / 2 + iw / 2,
                              self._bh / 2, image=self._image)
            self.create_text(
                self._bw / 2 - group_w / 2 + iw + ICON_GAP + self._text_w / 2,
                self._bh / 2, text=self._text,
                font=self._font, fill=fg,
            )
        else:
            self.create_text(
                self._bw / 2, self._bh / 2, text=self._text,
                font=self._font, fill=fg,
            )

    def _round_rect(self, x0, y0, x1, y1, r, **kw):
        # Real sampled arcs: with only corner control points, Tk's smoothing
        # rounds much less than `r`, so a pill (r = h/2) came out as a
        # lightly rounded rectangle.
        r = max(0.0, min(r, (x1 - x0) / 2.0, (y1 - y0) / 2.0))
        return self.create_polygon(
            _round_rect_points(x0, y0, x1, y1, r, steps=8),
            smooth=True, **kw)

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
                    from app.ui.base import _themed_showerror
                    _themed_showerror(
                        self.winfo_toplevel(), "Unexpected Error",
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
        elif bg or fg:
            # keep the 8% state layer in step with the new container/content
            self._hover_bg = _hex_lerp(self._current_bg, self._fg, STATE_HOVER)
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
