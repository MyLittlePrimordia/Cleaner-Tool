"""M3Checkbutton: a Material 3 checkbox that stands in for tk.Checkbutton.

The native Windows checkbox ignores the palette. This one is a Frame holding
a 28px canvas (18px rounded box + 28px state-layer halo) and an optional
label, and accepts the subset of tk.Checkbutton options the app actually
passes (text, variable, command, onvalue/offvalue, bg, fg, font, wraplength,
anchor, justify) so each call site only changes its class name. Unknown
Checkbutton options (selectcolor, activebackground, ...) are accepted and
ignored: the box colours come from the M3 tokens, not from the caller.
"""
from __future__ import annotations

import tkinter as tk

from app.ui.theme import COLORS, F, _hex_lerp, _round_rect_points


class M3Checkbutton(tk.Frame):
    CELL = 28            # canvas size (also the hover halo diameter)
    BOX = 18             # the checkbox itself

    def __init__(self, parent, text="", variable=None, command=None,
                 onvalue=True, offvalue=False, bg=None, fg=None, font=None,
                 wraplength=0, anchor="w", justify="left", **_ignored):
        try:
            page = bg or parent["bg"]
        except Exception:
            page = COLORS["bg"]
        super().__init__(parent, bg=page, bd=0, highlightthickness=0)
        self._page = page
        self._var = variable if variable is not None else tk.BooleanVar(
            value=False)
        self._on = onvalue
        self._off = offvalue
        self._command = command
        self._enabled = True
        self._hover = False
        self._focused = False
        self._fg = fg or COLORS["text"]

        self._cv = tk.Canvas(self, width=self.CELL, height=self.CELL, bg=page,
                             highlightthickness=0, bd=0, cursor="hand2",
                             takefocus=1)
        self._cv.pack(side="left")
        self._lbl = None
        if text:
            self._lbl = tk.Label(self, text=text, font=font or (F, 9),
                                 bg=page, fg=self._fg, bd=0,
                                 wraplength=wraplength, anchor=anchor,
                                 justify=justify, cursor="hand2")
            self._lbl.pack(side="left", fill="x", expand=True, padx=(2, 0))

        for w in (self._cv, self._lbl):
            if w is None:
                continue
            w.bind("<Button-1>", self._on_click)
            w.bind("<Enter>", self._on_enter, add="+")
            w.bind("<Leave>", self._on_leave, add="+")
        self._cv.bind("<space>", self._on_click)
        self._cv.bind("<Return>", self._on_click)
        self._cv.bind("<FocusIn>", self._on_focus_in)
        self._cv.bind("<FocusOut>", self._on_focus_out)
        try:
            self._var.trace_add("write", lambda *_: self._draw())
        except Exception:
            pass
        self._draw()

    # ---- state ---------------------------------------------------------- #

    def _is_on(self):
        try:
            return self._var.get() == self._on
        except Exception:
            return False

    def _on_click(self, _e=None):
        if not self._enabled:
            return
        try:
            self._cv.focus_set()
        except Exception:
            pass
        self._var.set(self._off if self._is_on() else self._on)
        if self._command:
            try:
                self._command()
            except Exception:
                pass

    def _on_enter(self, _e=None):
        self._hover = True
        self._draw()

    def _on_leave(self, _e=None):
        self._hover = False
        self._draw()

    def _on_focus_in(self, _e=None):
        self._focused = True
        self._draw()

    def _on_focus_out(self, _e=None):
        self._focused = False
        self._draw()

    # ---- tk.Checkbutton-compatible API ----------------------------------- #

    def invoke(self):
        self._on_click()

    def select(self):
        self._var.set(self._on)

    def deselect(self):
        self._var.set(self._off)

    def toggle(self):
        self._var.set(self._off if self._is_on() else self._on)

    def configure(self, cnf=None, **kw):
        if cnf:
            kw = {**cnf, **kw}
        state = kw.pop("state", None)
        if state is not None:
            self._enabled = state != "disabled"
            self._draw()
        text = kw.pop("text", None)
        if text is not None and self._lbl is not None:
            self._lbl.config(text=text)
        if "command" in kw:
            self._command = kw.pop("command")
        for k in ("variable", "onvalue", "offvalue", "selectcolor",
                  "activebackground", "activeforeground", "fg", "font",
                  "wraplength", "anchor", "justify", "image"):
            kw.pop(k, None)
        if kw:
            try:
                super().configure(**kw)
            except Exception:
                pass

    config = configure

    # ---- rendering -------------------------------------------------------- #

    def _draw(self):
        cv = self._cv
        cv.delete("all")
        c = self.CELL / 2.0
        h = self.BOX / 2.0
        on = self._is_on()
        if self._enabled:
            accent = COLORS["primary"] if on else COLORS["text"]
            edge = COLORS["primary"] if on else COLORS["muted"]
        else:
            accent = COLORS["muted"]
            edge = _hex_lerp(self._page, COLORS["text"], 0.38)
        if self._enabled and (self._hover or self._focused):
            cv.create_oval(0, 0, self.CELL, self.CELL,
                           fill=_hex_lerp(self._page, accent, 0.14),
                           outline="")
        cv.create_polygon(
            _round_rect_points(c - h, c - h, c + h, c + h, 3, steps=4),
            smooth=True, fill=edge, outline="")
        if on:
            cv.create_line(c - 4.5, c + 0.5, c - 1.5, c + 3.5, c + 5, c - 3.5,
                           fill=COLORS["on_primary"] if self._enabled
                           else self._page,
                           width=2, capstyle="round", joinstyle="round")
        else:
            cv.create_polygon(
                _round_rect_points(c - h + 2, c - h + 2, c + h - 2,
                                   c + h - 2, 1.5, steps=3),
                smooth=True, fill=self._page, outline="")
        if self._lbl is not None:
            try:
                self._lbl.config(
                    fg=self._fg if self._enabled
                    else _hex_lerp(self._page, COLORS["text"], 0.38))
            except Exception:
                pass
