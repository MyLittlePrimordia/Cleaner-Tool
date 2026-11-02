"""M3Checkbutton: a Material 3 checkbox that stands in for tk.Checkbutton.

The native Windows checkbox ignores the palette. This one is a Frame holding
a 28px canvas (18px rounded box + 28px state-layer halo) and an optional
label, and accepts the subset of tk.Checkbutton options the app actually
passes (text, variable, command, onvalue/offvalue, bg, fg, font, wraplength,
anchor, justify, state) so each call site only changes its class name.

Two kinds of option are NOT ours to honour: the ones Tk's checkbox uses to
paint its own tick (selectcolor, activebackground, activeforeground, ...),
because the box colours come from the M3 tokens. Those are listed explicitly
in `_TK_PAINT_OPTIONS` and ignored. Anything else raises TypeError - the
previous version swallowed EVERY unknown option, and then swallowed fg/font/
wraplength/anchor/justify in `configure()` too, so a caller could restyle a
checkbox and get no error and no effect (C-001). A silent drop is how one
control quietly becomes the legacy-looking one in a modern row.
"""
from __future__ import annotations

import tkinter as tk

from app.ui.theme import COLORS, TYPE, _hex_lerp, _round_rect_points
from app.ui.metrics import TEXT_NUDGE

#: Tk checkbox options that describe the NATIVE tick, which this widget
#: replaces. Accepting and ignoring them is the documented compatibility
#: contract; every other unknown keyword is a caller mistake and raises.
_TK_PAINT_OPTIONS = frozenset({
    "selectcolor", "activebackground", "activeforeground", "indicatoron",
    "bd", "relief", "offrelief", "highlightthickness", "takefocus",
    "tristatevalue", "image",
})


class M3Checkbutton(tk.Frame):
    CELL = 28            # canvas size (also the hover halo diameter)
    BOX = 18             # the checkbox itself

    def __init__(self, parent, text="", variable=None, command=None,
                 onvalue=True, offvalue=False, bg=None, fg=None, font=None,
                 wraplength=0, anchor="w", justify="left", state="normal",
                 **_ignored):
        unknown = set(_ignored) - _TK_PAINT_OPTIONS
        if unknown:
            raise TypeError(
                "M3Checkbutton got unsupported option(s): %s"
                % ", ".join(sorted(unknown)))
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
        self._enabled = str(state or "normal") != "disabled"
        self._hover = False
        self._focused = False
        self._fg = fg or COLORS["text"]
        self._font = font or TYPE["caption"]
        try:
            self._wrap = int(wraplength or 0)
        except Exception:
            self._wrap = 0
        self._anchor = anchor
        self._justify = justify

        self._cv = tk.Canvas(self, width=self.CELL, height=self.CELL, bg=page,
                             highlightthickness=0, bd=0, cursor="hand2",
                             takefocus=1)
        self._cv.pack(side="left")
        self._lbl = None
        if text:
            self._make_label(str(text))

        self._bind_interactions(self._cv)
        self._cv.bind("<space>", self._on_click)
        self._cv.bind("<Return>", self._on_click)
        self._cv.bind("<FocusIn>", self._on_focus_in)
        self._cv.bind("<FocusOut>", self._on_focus_out)
        try:
            self._var.trace_add("write", lambda *_: self._draw())
        except Exception:
            pass
        self._draw()

    # ---- construction helpers -------------------------------------------- #

    def _make_label(self, text):
        """The text Label, created on first use so `configure(text=...)` works.

        A checkbox built with no text must still be able to GAIN a label: five
        call sites toggle their caption from outside, and the old widget
        dropped those writes on the floor because no Label existed.
        """
        self._lbl = tk.Label(
            self, text=text, font=self._font, bg=self._page, fg=self._fg,
            bd=0, wraplength=self._wrap, anchor=self._anchor,
            justify=self._justify, cursor="hand2")
        self._lbl.pack(side="left", fill="x", expand=True,
                       padx=(TEXT_NUDGE, 0))
        self._bind_interactions(self._lbl)
        return self._lbl

    def _bind_interactions(self, w):
        w.bind("<Button-1>", self._on_click)
        w.bind("<Enter>", self._on_enter, add="+")
        w.bind("<Leave>", self._on_leave, add="+")

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

    # ---- tk.Checkbutton-compatible API ---------------------------------- #

    def invoke(self):
        self._on_click()

    def select(self):
        self._var.set(self._on)

    def toggle(self):
        self._var.set(self._off if self._is_on() else self._on)

    def configure(self, cnf=None, **kw):
        """Apply the options this widget claims to support (C-001).

        `fg`, `font`, `wraplength`, `anchor` and `justify` are real state,
        not compatibility noise: a caller that passes them in the constructor
        and then re-configures one expects the label to change.
        """
        if cnf:
            kw = {**cnf, **kw}
        state = kw.pop("state", None)
        if state is not None:
            self._enabled = str(state) != "disabled"
            self._draw()
        text = kw.pop("text", None)
        if text is not None:
            text = str(text)
            if self._lbl is None:
                if text:
                    self._make_label(text)
            else:
                self._lbl.config(text=text)
        if "command" in kw:
            self._command = kw.pop("command")
        if "fg" in kw:
            self._fg = kw.pop("fg") or COLORS["text"]
            self._draw()
        for key in ("font", "anchor", "justify"):
            if key not in kw:
                continue
            value = kw.pop(key)
            setattr(self, "_" + key, value)
            if self._lbl is not None:
                try:
                    self._lbl.config(**{key: value})
                except Exception:
                    pass
        if "wraplength" in kw:
            try:
                self._wrap = int(kw.pop("wraplength") or 0)
            except (TypeError, ValueError):
                self._wrap = 0
            if self._lbl is not None:
                try:
                    self._lbl.config(wraplength=self._wrap)
                except Exception:
                    pass
        for key in ("variable", "onvalue", "offvalue"):
            kw.pop(key, None)
        for key in _TK_PAINT_OPTIONS:
            kw.pop(key, None)
        if kw:
            try:
                super().configure(**kw)
            except Exception:
                pass

    config = configure

    def cget(self, key):
        """Report the options this widget owns, so callers can read them.

        A Frame's cget raises for `wraplength`, which is exactly the call a
        fitting helper makes to avoid redundant work (C-001).
        """
        local = {"fg": self._fg, "font": self._font,
                 "wraplength": self._wrap, "anchor": self._anchor,
                 "justify": self._justify,
                 "state": "normal" if self._enabled else "disabled"}
        if key in local:
            return local[key]
        if key == "text":
            return self._lbl.cget("text") if self._lbl is not None else ""
        return super().cget(key)

    # ---- rendering ------------------------------------------------------- #

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
