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

Depends only on app.ui.theme.
"""
from __future__ import annotations

import tkinter as tk

from app.ui.theme import COLORS, SPACE_5, TYPE, SPACE_1
from app.ui.metrics import CARD_BODY_INSET, CONTROL_GAP

#: The empty state's geometry, in one place.
#:
#: `gap_*` values are the space BETWEEN elements (glyph -> text -> detail ->
#: action); `pad_top` is the breathing room above the glyph. DataTable offsets
#: its canvas items by exactly these, so a table's empty state and a packed
#: one are the same block at the same rhythm.
METRICS = {
    "glyph_font": TYPE["glyph_large"],
    "text_font": TYPE["label_medium"],
    "detail_font": TYPE["micro_regular"],
    "pad_top": 28,
    "glyph_gap": 34,
    "text_gap": 22,
    "detail_gap": 36,
    "pad_x": SPACE_5,
    "action_h": 32,
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

    #: Fallback wrap width when the caller cannot supply one.
    #:
    #: REDESIGN 2026-10 (audit D3): both labels used to hardcode
    #: wraplength=460. These six dialogs are PROPORTIONAL (720-1120px), and
    #: ToolPage exists precisely to replace hardcoded wraplengths with
    #: body_width(). So every empty state in every Tools popup wrapped to a
    #: width unrelated to the window it was in: on a 1120px window the text
    #: occupied 41% of the width and looked stranded; on a 720px window 460px
    #: overflowed the body. A caller that knows its width should pass
    #: wrap=self.body_width(...); this constant is only the last resort.
    DEFAULT_WRAP = 460

    def __init__(self, parent, glyph="", text="", detail="", bg=None,
                 accent=None, wrap=None):
        self._page_bg = bg or (parent["bg"] if isinstance(
            parent, (tk.Frame, tk.Canvas)) else COLORS["bg"])
        super().__init__(parent, bg=self._page_bg, bd=0)

        # Tk ignores a wraplength below ~2px, and `wrap=0` means "never
        # wrap" - so a not-yet-laid-out caller measuring 1px must fall back to
        # the constant rather than silently disabling wrapping.
        try:
            _wrap = int(wrap or 0)
        except Exception:
            _wrap = 0
        if _wrap < 40:
            _wrap = self.DEFAULT_WRAP
        self._wrap = _wrap
        m = METRICS

        if glyph:
            # "muted", not "hairline": this glyph is a FOREGROUND, and
            # hairline is now reserved for borders. On the true-black canvas a
            # #242424 glyph on a #0D0D0D page is invisible, which would delete
            # the one cue that says "there is genuinely nothing here".
            tk.Label(self, text=str(glyph), font=m["glyph_font"],
                     bg=self._page_bg, fg=accent or COLORS["muted"],
                     bd=0).pack(pady=(m["pad_top"], CONTROL_GAP))

        tk.Label(self, text=str(text or ""), font=m["text_font"],
                 bg=self._page_bg, fg=COLORS["text"], anchor="center",
                 justify="center", wraplength=self._wrap, bd=0).pack(
                     fill="x", padx=m["pad_x"])

        if detail:
            tk.Label(self, text=str(detail), font=m["detail_font"],
                     bg=self._page_bg, fg=COLORS["subtext"], anchor="center",
                     justify="center", wraplength=self._wrap, bd=0).pack(
                         fill="x", padx=m["pad_x"], pady=(SPACE_1, 0))

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
