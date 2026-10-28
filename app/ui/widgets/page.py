"""The blocks every page shares: its lead, its action bar, its card grid.

Before these existed each tab invented its own top and bottom:

* Clean/Repair/Tweak started with preset cards, Install with a run row,
  Drivers with a dialog's `Header`, Health with a hand-built hero and
  Settings with a category rail. Two of those also carried a second title
  under the app bar (the "three page-header models" finding, C-003).
* The primary action lived in a centred hero on task pages, top-right in
  Health, bottom-right in Drivers/Settings, and two filled `lg` buttons sat
  side by side on Install (C-002).

The components here are deliberately small: they own geometry, not state.
A page fills the slots and keeps deciding what its controls mean.

    lead = PageLead(page, accent=ACCENT)
    lead.set(overline="PC HEALTH", headline="Good", sub="...", meta="...")
    lead.pack(fill="x", padx=PAGE_GUTTER, pady=(PAGE_TOP, SPACE_1))

    bar = PageActionBar(page)
    bar.pack(fill="x", padx=PAGE_GUTTER, pady=(SPACE_1, PAGE_BOTTOM))
    AnimatedButton(bar.primary_slot, "Run", role="filled",
                   size="lg", accent=ACCENT).pack(side="right")

`PageActionBar` packs the primary first at the far right and the secondary
cluster before it, so a secondary appearing or disappearing never moves the
primary - the one property the old centred grid existed to protect.

Depends only on app.ui.theme and RoundedCard.
"""
from __future__ import annotations

import tkinter as tk

from app.ui.theme import (
    COLORS, RADIUS_SURFACE, SPACE_1, SPACE_2,
    SPACE_3, TYPE
)
from app.ui.widgets.rounded_card import RoundedCard

#: Below this a card earns a row of its own rather than a share of a row.
#: Measured against the longest blurb the grid carries, at the smallest
#: type scale the app ships (tk scaling 1.33): 236px is where a two-line
#: supporting sentence stops ellipsizing to noise.
CARD_MIN_W = 236

#: The most columns any page grid grows to. Four 236px cards plus gaps is
#: ~980px of grid; past that a wider window should not produce a fifth
#: column, or the page turns into a wall of chips instead of a card grid.
CARD_MAX_COLS = 4


def flow_columns(width, min_card=CARD_MIN_W, max_columns=CARD_MAX_COLS,
                 gap=SPACE_1 * 2):
    """How many `min_card`-wide columns fit in `width`.

    One function for every page grid. The Tools grid and the preset rows
    used to hardcode 3 in two places, so the same window could fit four
    healthy cards on one page and still squeeze three on another. A page
    that resizes simply calls this again with its new inner width.

    Returns at least 1 (a grid cannot have zero columns) and never more
    than `max_columns`, so the count is a layout decision rather than a
    side effect of a very wide monitor.
    """
    try:
        w = int(width)
    except (TypeError, ValueError):
        return 1
    try:
        min_card = int(min_card)
    except (TypeError, ValueError):
        min_card = CARD_MIN_W
    if w <= 0 or min_card <= 0:
        return 1
    gap = max(0, int(gap))
    cols = (w + gap) // (min_card + gap)
    return max(1, min(int(max_columns), int(cols)))


def grid_cards(parent, cards, columns, *, gap=SPACE_1, equal=False):
    """Place `cards` in a uniform `columns`-wide grid, in order.

    Shared by the preset rows and the Tools grid so both keep the house
    rules: equal column weights (every card the same width), one 4px gap on
    both axes rather than two near-misses, and a single trailing card
    centred instead of stranded against the left edge.

    `equal=True` keeps the REQUESTED column count even when a group has fewer
    cards, so a hub's three-card section and four-card section share one card
    width (the spare cell is whitespace). Without it a short group stretches
    its cards wider than its neighbours - the asymmetry the 2026-10 design
    audit exists to prevent. Preset rows want the default: their card width
    is a function of how many presets they hold.

    Re-gridding an already-built list is the point of the function: it
    destroys nothing and creates nothing, so a resize changes the layout
    without rebuilding a single card. Columns that stop being used lose
    their weight, otherwise Tk keeps their share of the row empty.
    """
    cards = [c for c in (cards or ()) if c is not None]
    if not cards:
        return 0
    requested = max(1, int(columns))
    columns = requested if equal else max(1, min(requested, len(cards)))
    lone_last = (len(cards) % columns) == 1 and len(cards) > 1
    last_row = (len(cards) - 1) // columns
    for i, card in enumerate(cards):
        try:
            row, col = divmod(i, columns)
            if lone_last and row == last_row:
                col = columns // 2
            card.grid(row=row, column=col, sticky="nsew",
                      padx=gap, pady=gap)
        except Exception:
            pass
    # Configure the columns this grid uses; clear the weight of any column
    # a previous (wider) layout had claimed.
    for c in range(CARD_MAX_COLS + 1):
        used = c < columns
        try:
            parent.grid_columnconfigure(
                c, weight=1 if used else 0, uniform="cards" if used else "")
        except Exception:
            pass
    return columns


class PageActionBar(tk.Frame):
    """The bottom band: hint on the left, primary pinned to the far right."""

    def __init__(self, parent, bg=None, gap=SPACE_3, **kw):
        page_bg = bg or COLORS["bg"]
        super().__init__(parent, bg=page_bg, **kw)
        self._page_bg = page_bg
        self._gap = gap
        self.hint_slot = tk.Frame(self, bg=page_bg)
        self.hint_slot.pack(side="left", fill="x", expand=True)
        # Packed BEFORE the secondary cluster, so it owns the far-right edge.
        self.primary_slot = tk.Frame(self, bg=page_bg)
        self.primary_slot.pack(side="right", padx=(gap, 0))
        self.secondary_slot = tk.Frame(self, bg=page_bg)
        self.secondary_slot.pack(side="right")
        self._hint = None

    def set_hint(self, text, color=None):
        """One quiet line on the left; creates the label on first use."""
        if self._hint is None:
            self._hint = tk.Label(
                self.hint_slot, text="", font=TYPE["caption"],
                bg=self._page_bg, fg=COLORS["subtext"], anchor="w")
            self._hint.pack(side="left", fill="x", expand=True)
        self._hint.config(text=str(text or ""),
                          fg=color or COLORS["subtext"])
        return self._hint


class PageLead(RoundedCard):
    """The optional result/state card between the app bar and the page body.

    One anatomy, so a page cannot grow a second title block:

        [ optional media ]  OVERLINE
                            One sentence that says what the state is
                            Supporting line
                            meta line          [ actions ]

    The headline wraps to the info column's real width, not a fixed 520px
    (the old Health hero's L-004 bug), and the action column is a slot rather
    than three separately packed buttons.
    """

    def __init__(self, parent, accent=None, fill=None, bg=None,
                 min_height=154, with_media=False, **kw):
        self._fill = fill or COLORS["surface"]
        self._page_bg = bg or COLORS["bg"]
        super().__init__(parent, fill=self._fill, bg=self._page_bg,
                         radius=RADIUS_SURFACE, min_height=min_height, **kw)
        self.accent = accent or COLORS["primary"]
        self.row = tk.Frame(self.body, bg=self._fill)
        self.row.pack(fill="both", expand=True, padx=SPACE_3, pady=SPACE_2)
        self.media_slot = tk.Frame(self.row, bg=self._fill)
        if with_media:
            self.media_slot.pack(side="left", padx=(0, SPACE_3))
        self.actions = tk.Frame(self.row, bg=self._fill)
        self.actions.pack(side="right", anchor="n", padx=(SPACE_3, 0))
        self.info = tk.Frame(self.row, bg=self._fill)
        self.info.pack(side="left", fill="both", expand=True)
        self.overline_lbl = tk.Label(
            self.info, text="", font=TYPE["section"], bg=self._fill,
            fg=self.accent, anchor="w")
        self.overline_lbl.pack(fill="x", pady=(SPACE_1, 0))
        self.headline_lbl = tk.Label(
            self.info, text="", font=TYPE["headline_small"], bg=self._fill,
            fg=COLORS["text"], anchor="w", justify="left", wraplength=480)
        self.headline_lbl.pack(fill="x")
        self.sub_lbl = tk.Label(
            self.info, text="", font=TYPE["body_medium"], bg=self._fill,
            fg=COLORS["subtext"], anchor="w", justify="left", wraplength=480)
        self.sub_lbl.pack(fill="x", pady=(SPACE_1, 0))
        self.meta_lbl = tk.Label(
            self.info, text="", font=TYPE["caption"], bg=self._fill,
            fg=COLORS["muted"], anchor="w", justify="left", wraplength=480)
        self.meta_lbl.pack(fill="x", pady=(SPACE_2, 0))
        self._overline_base = ""
        self._glyph = ""
        self.info.bind("<Configure>", self._wrap, add="+")

    def _wrap(self, event):
        # Measure what the info column really has. The action/media slots are
        # already packed, so this width is the text's actual room.
        try:
            room = max(180, int(event.width) - 4)
            for lbl in (self.headline_lbl, self.sub_lbl, self.meta_lbl):
                lbl.config(wraplength=room)
        except Exception:
            pass

    def set(self, overline=None, headline=None, sub=None, meta=None):
        if overline is not None:
            self._overline_base = str(overline)
            self.overline_lbl.config(text=self._overline_base)
        if headline is not None:
            self.headline_lbl.config(text=str(headline))
        if sub is not None:
            self.sub_lbl.config(text=str(sub))
        if meta is not None:
            self.meta_lbl.config(text=str(meta))
        return self

    # ---- compatibility for the old Header call shape ------------------ #
    # Drivers used to mount a dialog Header on the page; these three methods
    # let its state updates land on the lead without a second title. The
    # glyph becomes a quiet state suffix on the overline, not a disc.

    def set_state(self, glyph, color=None):
        self._glyph = str(glyph or "")
        text = self._overline_base
        if self._glyph:
            text = (text + " \u00b7 " + self._glyph).strip()
        self.overline_lbl.config(text=text, fg=color or self.accent)

    def set_title(self, text, color=None):
        self.headline_lbl.config(text=str(text),
                                 fg=color or COLORS["text"])

    def set_sub(self, text, color=None):
        self.sub_lbl.config(text=str(text),
                            fg=color or COLORS["subtext"])
