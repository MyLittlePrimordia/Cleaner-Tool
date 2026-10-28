"""RoundedEntry: canvas backing plate behind a borderless Entry.

H4. Extracted verbatim from app/gui.py (blueprint B1.2).
"""
from __future__ import annotations

import tkinter as tk

from app.ui.metrics import COMPACT_INSET, TEXT_NUDGE
from app.ui.theme import COLORS, RADIUS_CONTROL, TYPE, _round_rect_points

class RoundedEntry(tk.Frame):
    """Text entry with rounded corners (user request: filter/search boxes).

    Tk Entries are always rectangular, so a canvas behind draws the rounded
    background + focus ring while a borderless Entry sits on top. Drop-in:
    pack/grid the RoundedEntry itself and use `.entry` exactly like the
    Entry it replaces (textvariable, binds, width all live there).
    Focus ring turns the tab accent on focus, hairline otherwise."""

    RADIUS = RADIUS_CONTROL   # the small shape role
    PAD_Y = COMPACT_INSET

    def __init__(self, parent, textvariable=None, width=20, font=None,
                 entry_bg=None, accent=None, **kw):
        super().__init__(parent, bg=parent["bg"] if isinstance(parent, tk.Frame) else COLORS["bg"])
        # M3 text field: the focus ring is always the primary colour, not the
        # tab accent (kept as a param for API compatibility).
        self._accent = COLORS["primary"]
        self._fill = entry_bg or COLORS["surface"]
        self._cv = tk.Canvas(self, highlightthickness=0, bd=0, bg=self["bg"])
        self._cv.place(relx=0, rely=0, relwidth=1, relheight=1)
        self.entry = tk.Entry(self, textvariable=textvariable, width=width,
                              bg=self._fill, fg=COLORS["text"],
                              insertbackground=COLORS["text"],
                              font=font or TYPE["caption"], bd=0, relief="flat",
                              highlightthickness=0, **kw)
        self.entry.pack(fill="x",
                        padx=(self.RADIUS + TEXT_NUDGE,
                              self.RADIUS + TEXT_NUDGE),
                        pady=self.PAD_Y)
        self.bind("<Configure>", lambda e: self._draw(focused=False))
        # add="+" so call-site placeholder binds (FocusIn/Out) keep working
        self.entry.bind("<FocusIn>", lambda e: self._draw(focused=True), add="+")
        self.entry.bind("<FocusOut>", lambda e: self._draw(focused=False), add="+")
        self.after(0, lambda: self._draw(focused=False))

    def _draw(self, focused):
        try:
            w, h = self.winfo_width(), self.winfo_height()
            if w < 4 or h < 4:
                return
            self._cv.delete("all")
            self._cv.create_polygon(
                _round_rect_points(1, 1, w - 1, h - 1, self.RADIUS),
                smooth=True, fill=self._fill,
                outline=self._accent if focused else "",
                width=2 if focused else 1)
        except Exception:
            pass


