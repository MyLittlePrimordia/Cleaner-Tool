"""M3Dropdown: a rounded Material 3 "exposed dropdown" field.

Stands in for ttk.Combobox (and tk.OptionMenu) so every picker in the app is
a rounded, filled field with a drawn chevron instead of the square native
control. It implements the small Combobox surface the app uses: textvariable,
values, state, width, font, bind("<<ComboboxSelected>>"), current(), get(),
set(), config(values=..., state=...) and widget["values"].

Clicking (or Space / Enter / Down) opens a styled popup menu under the field.
"""
from __future__ import annotations

import tkinter as tk

from app.ui.theme import (COLORS, F, _hex_lerp, _measure_font,
                          _round_rect_points, ellipsize)

#: Horizontal room a field reserves beyond its text: the 18px left inset plus
#: the chevron column and the gap before it. Shared by the width estimate here
#: and the text room in `_draw`, so the two cannot disagree.
_FIELD_RESERVE = 64


def _field_px(parent, font, width, values):
    """Pixel width for a dropdown field.

    The formula used to be `chars * 7.4 + 64`. 7.4px is a fair average for one
    character at 10pt on a 100% display, but Tk sizes text in POINTS, so a
    character is ~15px at tk scaling 2.0 (a DPI-aware 200% display) and the
    field came out about half the width it needed — "At startup" in the PC
    Health picker rendered as "At start…". This measures the real font, and
    also widens the field to fit its longest value, so a field whose content is
    wider than its `width` is never ellipsized."""
    try:
        fnt = _measure_font(parent.winfo_toplevel(), font)
        char = max(4, fnt.measure("0"))
        longest = max([fnt.measure(v) for v in values] or [0])
    except Exception:
        char, longest = 7, 0
    px = int(max(8, int(width)) * char) + _FIELD_RESERVE
    px = max(px, longest + _FIELD_RESERVE)
    return max(150, min(px, 560))


class M3Dropdown(tk.Canvas):
    H = 42
    RADIUS = 14

    def __init__(self, parent, textvariable=None, values=(), state="readonly",
                 width=20, font=None, bg=None, command=None, **_ignored):
        try:
            page = bg or parent["bg"]
        except Exception:
            page = COLORS["bg"]
        self._font = font or (F, 10)
        self._values = [str(v) for v in values]
        self._enabled = state != "disabled"
        px = _field_px(parent, self._font, width, self._values)
        super().__init__(parent, width=px, height=self.H, bg=page,
                         highlightthickness=0, bd=0, cursor="hand2",
                         takefocus=1)
        self._page = page
        self._var = textvariable if textvariable is not None else tk.StringVar()
        self._command = command
        self._hover = False
        self._focus = False
        self._open = False
        self.bind("<Configure>", lambda _e: self._draw())
        self.bind("<Button-1>", self._on_click)
        self.bind("<Enter>", lambda _e: self._set_hover(True))
        self.bind("<Leave>", lambda _e: self._set_hover(False))
        self.bind("<FocusIn>", lambda _e: self._set_focus(True))
        self.bind("<FocusOut>", lambda _e: self._set_focus(False))
        for k in ("<space>", "<Return>", "<Down>"):
            self.bind(k, self._on_click)
        try:
            self._var.trace_add("write", lambda *_a: self._draw())
        except Exception:
            pass
        self._draw()

    # ---- Combobox-compatible API ------------------------------------------ #

    def get(self):
        return self._var.get()

    def set(self, value):
        self._var.set(value)

    def current(self, index=None):
        if index is None:
            try:
                return self._values.index(self._var.get())
            except ValueError:
                return -1
        if 0 <= index < len(self._values):
            self._var.set(self._values[index])

    def __getitem__(self, key):
        if key == "values":
            return tuple(self._values)
        return super().__getitem__(key)

    def __setitem__(self, key, value):
        if key == "values":
            self._values = [str(v) for v in value]
        else:
            super().__setitem__(key, value)

    def configure(self, cnf=None, **kw):
        if cnf:
            kw = {**cnf, **kw}
        if "values" in kw:
            self._values = [str(v) for v in kw.pop("values")]
        if "state" in kw:
            self._enabled = kw.pop("state") != "disabled"
        if "textvariable" in kw:
            self._var = kw.pop("textvariable")
        for k in ("font", "width", "justify", "postcommand", "exportselection"):
            kw.pop(k, None)
        if kw:
            try:
                super().configure(**kw)
            except Exception:
                pass
        self._draw()

    config = configure

    # ---- interaction -------------------------------------------------------- #

    def _set_hover(self, on):
        self._hover = on
        self._draw()

    def _set_focus(self, on):
        self._focus = on
        self._draw()

    def _on_click(self, _e=None):
        if not self._enabled or not self._values:
            return
        try:
            self.focus_set()
        except Exception:
            pass
        menu = tk.Menu(
            self, tearoff=0, bd=0, relief="flat", font=self._font,
            bg=COLORS["surface"], fg=COLORS["text"],
            activebackground=COLORS["secondary_container"],
            activeforeground=COLORS["on_secondary_container"],
            activeborderwidth=0)
        cur = self._var.get()
        for v in self._values:
            menu.add_command(
                label=("\u2713  " if v == cur else "     ") + v,
                command=lambda x=v: self._choose(x))
        self._open = True
        self._draw()
        try:
            menu.tk_popup(self.winfo_rootx(),
                          self.winfo_rooty() + self.winfo_height() + 2)
        finally:
            try:
                menu.grab_release()
            except Exception:
                pass
            self._open = False
            self._draw()

    def _choose(self, value):
        self._var.set(value)
        try:
            self.event_generate("<<ComboboxSelected>>")
        except Exception:
            pass
        if self._command:
            try:
                self._command(value)
            except Exception:
                pass

    # ---- rendering ------------------------------------------------------------ #

    def _draw(self):
        self.delete("all")
        w = self.winfo_width()
        h = self.winfo_height()
        if w < 20:
            w = int(self["width"])
        if h < 10:
            h = self.H
        if self._enabled:
            fill = _hex_lerp(COLORS["surface"], COLORS["text"],
                             0.06 if self._hover else 0.0)
            ink = COLORS["text"]
            chev = COLORS["subtext"]
        else:
            fill = _hex_lerp(self._page, COLORS["text"], 0.08)
            ink = _hex_lerp(self._page, COLORS["text"], 0.38)
            chev = ink
        ring = COLORS["primary"] if (self._open or self._focus) else ""
        self.create_polygon(
            _round_rect_points(1, 1, w - 2, h - 2, self.RADIUS),
            smooth=True, fill=fill, outline=ring, width=2 if ring else 1)
        text = ellipsize(self._var.get() or "", self._font, w - _FIELD_RESERVE)
        self.create_text(18, h / 2, text=text, anchor="w", font=self._font,
                         fill=ink)
        cx, cy = w - 24, h / 2
        s = 5
        if self._open:
            pts = (cx - s, cy + s / 2, cx, cy - s / 2, cx + s, cy + s / 2)
        else:
            pts = (cx - s, cy - s / 2, cx, cy + s / 2, cx + s, cy - s / 2)
        self.create_line(*pts, fill=chev, width=2, capstyle="round",
                         joinstyle="round")
