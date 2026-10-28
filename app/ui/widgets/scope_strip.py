"""ScopeStrip: what a tool reads, changes and can undo — visible, not hidden.

AUDIT TV-003 (2026-10). The Tools popups opened with a good headline and then
left the one fact a cautious user needs — *does this read, change, or delete?*
— in an ⓘ tooltip in the footer. A tester and an uninstaller therefore looked
identical until you hovered. The strip puts three short chips under the
headline instead, and the tooltip keeps the long explanation.

Chips, not an extra paragraph
-----------------------------
The audit's M3 reference is the assist chip: 32dp tall, small shape, tonal
fill, one line. Each chip carries a verb ("Reads") and a value ("Disk usage +
caches"), so the three read as one sentence across the strip. Tone is a
function of the fact (see app/ui/tool_scope.py); this widget only paints it.

Reflow, because a dialog can be narrow
--------------------------------------
The chips are placed on the same grid with measured widths: they sit on one
row when the content column is wide enough and wrap to a second row when it
is not. A fixed row would shear the third chip off at the minimum window —
the same clipping this app has already fixed in labels and card grids.

Depends only on app.ui.theme and app.ui.tool_scope.
"""
from __future__ import annotations

import tkinter as tk

from app.ui.metrics import CHIP_PAD, CONTROL_GAP
from app.ui.theme import (
    COLORS, RADIUS_PILL, SPACE_1, SPACE_2, SPACE_6, TYPE, _hex_lerp,
    _measure_font, _round_rect_points, ellipsize,
)

#: Chip height. SPACE_6 (32px) is the M3 assist-chip height, and it is on the
#: layout scale rather than a private measurement.
CHIP_H = SPACE_6

#: The tone vocabulary the table emits. Red and amber are reserved for risk
#: and outcome everywhere else in the app; here they mean exactly that.
TONES = {
    "neutral": COLORS["subtext"],
    "good": COLORS["accent_green"],
    "warn": COLORS["accent_yellow"],
    "bad": COLORS["accent_red"],
}

_DOT = 6


class _Chip(tk.Canvas):
    """One painted soft-cornered chip: dot, verb, value."""

    def __init__(self, parent, verb, value, tone="neutral", bg=None):
        page_bg = bg or COLORS["bg"]
        super().__init__(parent, height=CHIP_H, bg=page_bg,
                         highlightthickness=0, bd=0)
        self._page_bg = page_bg
        self._verb = str(verb or "")
        self._full = str(value or "")
        self._tone_name = tone if tone in TONES else "neutral"
        self._tone = TONES[self._tone_name]
        self._vfont = _measure_font(self, TYPE["micro"])
        self._tfont = _measure_font(self, TYPE["body_small"])
        self._shown = self._full
        self._width = 0
        self._restrict_to = None
        self._apply()

    # ---- measurement -------------------------------------------------- #
    def _overhead(self):
        return (CHIP_PAD * 2 + _DOT + CONTROL_GAP
                + int(self._vfont.measure(self._verb)) + SPACE_1)

    def _width_for(self, text):
        return self._overhead() + int(self._tfont.measure(text))

    def natural_width(self):
        return self._width_for(self._full)

    def restrict(self, max_w):
        """Fit inside `max_w`, ellipsizing the value if it must."""
        max_w = max(1, int(max_w))
        self._restrict_to = max_w
        if self._width_for(self._full) <= max_w:
            self._shown = self._full
        else:
            room = max_w - self._overhead()
            self._shown = ellipsize(self._full, self._tfont, room)
        self._apply()
        return self._width

    def unrestrict(self):
        """Return to the full value after the strip gets wide again."""
        if self._restrict_to is None and self._shown == self._full:
            return
        self._restrict_to = None
        self._shown = self._full
        self._apply()

    def value_text(self):
        return self._shown

    # ---- paint --------------------------------------------------------- #
    def _apply(self):
        w = self._width_for(self._shown)
        if self._restrict_to is not None:
            w = min(w, self._restrict_to)
        self._width = max(1, w)
        try:
            self.configure(width=self._width)
        except Exception:
            pass
        c = self
        c.delete("all")
        fill = (COLORS["surface"] if self._tone_name == "neutral"
                else _hex_lerp(self._page_bg, self._tone, 0.18))
        try:
            c.create_polygon(
                _round_rect_points(0, 0, self._width, CHIP_H, RADIUS_PILL),
                smooth=True, fill=fill, outline="")
        except Exception:
            pass
        cy = CHIP_H / 2.0
        x = CHIP_PAD
        c.create_oval(x, cy - _DOT / 2, x + _DOT, cy + _DOT / 2,
                      fill=self._tone, outline="")
        x += _DOT + CONTROL_GAP
        c.create_text(x, cy, text=self._verb, font=TYPE["micro"],
                      fill=self._tone, anchor="w")
        x += int(self._vfont.measure(self._verb)) + SPACE_1
        c.create_text(x, cy, text=self._shown, font=TYPE["body_small"],
                      fill=COLORS["text"], anchor="w")


class ScopeStrip(tk.Frame):
    """Three (or two) scope chips, wrapped by measured width."""

    GAP = SPACE_2

    def __init__(self, parent, chips, bg=None, **kw):
        page_bg = bg or COLORS["bg"]
        super().__init__(parent, bg=page_bg, bd=0, **kw)
        self._page_bg = page_bg
        self._chips = [_Chip(self, verb, value, tone, bg=page_bg)
                       for verb, value, tone in chips]
        self._rows = 0
        self._last_w = -1
        self.bind("<Configure>", self._on_configure, add="+")
        # Establish a sane one-row layout before the first real <Configure>.
        # Without it the frame asks for 1px until the window is mapped, and a
        # dialog that measures itself during construction would reserve no
        # room for the strip.
        self._reflow(720)

    # ---- layout -------------------------------------------------------- #
    def _on_configure(self, event):
        width = int(getattr(event, "width", 0) or 0)
        # A <Configure> with no width also fires while the window tears down;
        # gridding into a dying container raises TclError, which is not a
        # layout problem worth answering.
        if width <= 1:
            return
        self._reflow(width)

    def _reflow(self, width):
        width = int(width or 0)
        if width <= 1 or not self.winfo_exists():
            return self._rows
        if width == self._last_w:
            return self._rows
        self._last_w = width
        for chip in self._chips:
            try:
                chip.grid_forget()
            except Exception:
                pass
        row = col = 0
        used = 0
        # Leave room for the trailing gap so the last chip cannot push the
        # row one pixel past the content column.
        avail = max(120, width - self.GAP)
        for chip in self._chips:
            natural = chip.natural_width()
            if natural > avail:
                natural = chip.restrict(avail)
            else:
                # A wider window must bring the full value back; without
                # this the chip keeps the ellipsis it needed at 720px even
                # after the dialog is maximised.
                chip.unrestrict()
            need = natural if used == 0 else natural + self.GAP
            if used + need > avail and col > 0:
                row += 1
                col = 0
                used = 0
            try:
                chip.grid(row=row, column=col, sticky="w",
                          padx=(0, self.GAP), pady=(0, SPACE_1))
            except Exception:
                # A destroyed dialog is not a layout state to recover from.
                return self._rows
            used += (natural if col == 0 else need)
            col += 1
        self._rows = row + 1
        return self._rows

    # ---- api ----------------------------------------------------------- #
    def rows(self):
        """The number of rows the last reflow used (test/geometry handle)."""
        return self._rows

    def texts(self):
        """[(verb, visible value)] — lets a test assert what a user can read."""
        return [(chip._verb, chip.value_text()) for chip in self._chips]

    def chips(self):
        return list(self._chips)
