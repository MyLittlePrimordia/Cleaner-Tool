"""EmptyState: the one way the app says "nothing here".

Six dialogs shipped six copies of the same grey paragraph, several of them
three lines long. A newcomer opening a tool and seeing a paragraph has no
idea whether it is a failure, a filter that matched nothing, or genuinely
nothing to report.

EmptyState is the shared answer: a glyph, one sentence naming the cause, at
most one supporting line, and at most ONE button for the next action. If a
message needs two sentences, it is not an empty state.

Two painters, one design
------------------------
Some lists are packed Frames (a scroll panel of rows) and some are a single
virtualised Canvas (DataTable, which paints ~20 visible rows and cannot host a
child widget per state). Rather than let the canvas grow its own typography -
which is exactly how the app ended up with six versions of this - the metrics
live in METRICS here and BOTH painters read them. DataTable imports them in
`_draw_empty`.

Depends only on app.ui.theme and app.ui.metrics.
"""
from __future__ import annotations

import tkinter as tk

from app.ui.theme import COLORS, SPACE_5, TYPE, SPACE_1
from app.ui.metrics import (
    CARD_BODY_INSET, CONTROL_GAP, EMPTY_ACTION_H, EMPTY_DETAIL_GAP,
    EMPTY_GLYPH_GAP, EMPTY_PAD_TOP, EMPTY_TEXT_GAP,
)

#: The empty state's geometry, in one place.
#:
#: `gap_*` values are the space BETWEEN elements (glyph -> text -> detail ->
#: action); `pad_top` is the breathing room above the glyph. DataTable offsets
#: its canvas items by exactly these, so a table's empty state and a packed
#: one are the same block at the same rhythm. The values themselves are named
#: metrics (L-003), not private numbers.
METRICS = {
    "glyph_font": TYPE["glyph_large"],
    "text_font": TYPE["label_medium"],
    "detail_font": TYPE["micro_regular"],
    "pad_top": EMPTY_PAD_TOP,
    "glyph_gap": EMPTY_GLYPH_GAP,
    "text_gap": EMPTY_TEXT_GAP,
    "detail_gap": EMPTY_DETAIL_GAP,
    "pad_x": SPACE_5,
    "action_h": EMPTY_ACTION_H,
}


class EmptyState(tk.Frame):
    """Centred glyph + one line + optional detail + optional action.

    The action is added with .add_button() rather than passed in as a built
    widget. A caller who constructs the AnimatedButton first and hands it
    over gets a subtle and very visible bug: pack() puts a widget into its
    OWN master, not into whichever widget asked for it, so the button lands
    as a SIBLING of the empty state - outside the centred column, in the
    wrong place, at the wrong z-order - and looks like the empty state
    forgot it had a button. Owning the construction makes that impossible.
    """

    METRICS = METRICS

    def __init__(self, parent, glyph="", text="", detail="", bg=None,
                 accent=None, wrap=None):
        """`wrap` is the reading measure in px; pass body_width(...).

        There used to be a `DEFAULT_WRAP = 460` fallback "for when the caller
        cannot supply one". Every real caller supplies one, so the constant
        had no readers - and it was the old bug itself: a 460px column on a
        720-1120px dialog reads as stranded text, or overflows. A caller that
        forgets now gets the frame's REAL width, and keeps it through resizes,
        so the failure mode is gone rather than merely documented (G-002).
        """
        self._page_bg = bg or (parent["bg"] if isinstance(
            parent, (tk.Frame, tk.Canvas)) else COLORS["bg"])
        super().__init__(parent, bg=self._page_bg, bd=0)

        # Tk ignores a wraplength below ~2px, and `wrap=0` means "never
        # wrap" - so a measure that is not laid out yet (or nonsensical) is
        # treated as absent and resolved from the frame below.
        try:
            _wrap = int(wrap or 0)
        except Exception:
            _wrap = 0
        self._fixed_wrap = _wrap if _wrap >= 40 else 0
        self._wrap = self._fixed_wrap
        self._text_lbl = None
        self._detail_lbl = None
        m = METRICS

        if glyph:
            # "muted", not "hairline": this glyph is a FOREGROUND, and
            # hairline is now reserved for borders. On the true-black canvas a
            # #242424 glyph on a #0D0D0D page is invisible, which would delete
            # the one cue that says "there is genuinely nothing here".
            tk.Label(self, text=str(glyph), font=m["glyph_font"],
                     bg=self._page_bg, fg=accent or COLORS["muted"],
                     bd=0).pack(pady=(m["pad_top"], CONTROL_GAP))

        self._text_lbl = tk.Label(
            self, text=str(text or ""), font=m["text_font"],
            bg=self._page_bg, fg=COLORS["text"], anchor="center",
            justify="center", wraplength=self._wrap, bd=0)
        self._text_lbl.pack(fill="x", padx=m["pad_x"])

        if detail:
            self._detail_lbl = tk.Label(
                self, text=str(detail), font=m["detail_font"],
                bg=self._page_bg, fg=COLORS["subtext"], anchor="center",
                justify="center", wraplength=self._wrap, bd=0)
            self._detail_lbl.pack(fill="x", padx=m["pad_x"],
                                  pady=(SPACE_1, 0))

        if not self._fixed_wrap:
            # The frame is packed fill="x" by every caller, so its width IS
            # the column the text has. Configuring a wraplength changes the
            # frame's height, which fires this again; the room check below
            # makes the second pass a no-op.
            self.bind("<Configure>", self._fit_wrap, add="+")
            self.after_idle(self._fit_wrap)

    def _fit_wrap(self, event=None):
        """Give both labels the frame's real width when no measure was passed."""
        if self._fixed_wrap:
            return
        try:
            room = (int(event.width) if event is not None
                    else int(self.winfo_width()))
        except Exception:
            return
        room -= 2 * METRICS["pad_x"]
        if room < 40 or room == self._wrap:
            return
        self._wrap = room
        for lbl in (self._text_lbl, self._detail_lbl):
            if lbl is None:
                continue
            try:
                lbl.config(wraplength=room)
            except Exception:
                pass

    def add_button(self, text, command, accent=None, **btn_kw):
        """The one next action. Returns the button, or None on failure."""
        try:
            from app.ui.widgets.button import AnimatedButton
            btn = AnimatedButton(
                self, text=text, command=command, role="filled",
                accent=accent or COLORS["primary"], size="md", **btn_kw)
            btn.pack(pady=(CARD_BODY_INSET, SPACE_5))
            return btn
        except Exception:
            return None
