"""ToolCard: the tile the Tools page is built from.

The Tools page is a catalogue, not a list: 19 utilities of the same rank, and
the user's question is "which one", not "which row". A card can carry the
icon, the name and one line saying what the tool does at the same time. The
one-line-row revision could not, so it pushed every description into a
tooltip - a page that cannot explain itself without hovering.

The anatomy is `feature_card_body(compact=True)`, the same helper the Tweak
preset cards use, so a tool tile cannot invent its own icon size, inset,
ellipsis rule or second type size:

    (icon in a tonal circle)  Title
                              one line saying what it does

Two rules the grid depends on:

  * every card is the SAME height by construction: the 40px icon fixes the
    head's height and the title + supporting line together are shorter than
    it at every density, so no card ends where its text happens to end;
  * the title and the supporting line are ELLIPSIZED to the measured width,
    never wrapped. A wrapped blurb would make one card taller than its
    neighbours, which is exactly the "the cards are not symmetrical" report
    this exists to fix.

The card is one click target (W3C disclosure-card behaviour): the face, the
icon, both labels and the badge all open the tool, and Return / Space do the
same from the keyboard, because a card that only answers a mouse is a card
half the users cannot open.

Depends only on app.ui.theme and two sibling widgets.
"""
from __future__ import annotations

from app.ui.theme import (
    COLORS,
    ICON_CARD_TOOL,
    RADIUS_SURFACE,
    SPACE_1,
    SPACE_3,
    SURFACE_CARD,
    TYPE,
    icon_target_px,
)
from app.ui.widgets.feature_card import feature_card_body
from app.ui.widgets.rounded_card import RoundedCard

#: The card height at 100% density. The mark plus its vertical padding is the
#: natural height of the anatomy at that scale (the two text lines are taller
#: from 125% up, and then the text decides). The real floor is computed per
#: instance from the DPI-scaled mark; this constant is the 100% value the
#: tests compare against.
CARD_HEIGHT = ICON_CARD_TOOL + 2 * SPACE_1

#: The emoji fallback is drawn inside the icon circle, not beside it: the
#: shared default (`readout_large`) is sized for a 52px preset card and
#: overflows a 40px tool chip, so the three art-less tools would render a
#: clipped glyph. 18pt is the largest size that sits inside the circle.
GLYPH_FONT = TYPE["headline_small"]


class ToolCard(RoundedCard):
    """One tool tile: icon, name, one supporting line. Equal size, always."""

    def __init__(self, parent, *, title, blurb="", glyph="", photo=None,
                 accent=None, on_click=None, bg=None, min_height=None):
        self._face = SURFACE_CARD
        page_bg = bg or COLORS["bg"]
        # The mark SCALES with the display, exactly like the type beside it.
        # A fixed 40px chip next to 18pt text was the "icon too small at high
        # DPI" bug the rest of the app already fixed; it also clipped the
        # photo, which the tab sizes through icon_target_px and which used to
        # land 8px larger than the canvas that drew it at 125%.
        box = icon_target_px(ICON_CARD_TOOL, parent)
        super().__init__(parent, fill=self._face, bg=page_bg,
                         radius=RADIUS_SURFACE,
                         min_height=int(min_height or (box + 2 * SPACE_1)))
        self._accent = accent or COLORS["accent_sky"]
        self._on_click = on_click
        self._hover = False
        self.head, self.title_lbl, self.blurb_lbl, self.icon_lbl, self.badge = \
            feature_card_body(
                self, title, blurb, self._accent, photo=photo,
                glyph=glyph or "\u2022", box=box,
                glyph_font=GLYPH_FONT, compact=True, face=self._face,
                pad_x=SPACE_3, pad_y=SPACE_1,
                click=self._activate, hover=self._hover_at)
        # Collected once: the tint walk runs on every hover and this card is
        # one of nineteen on the page.
        self._tintables = self._descendants()
        self._bind_surface()

    # ---- activation --------------------------------------------------- #

    def _activate(self):
        """Mouse entry point (the shared body helper calls it with no args)."""
        try:
            self.focus_set()
        except Exception:
            pass
        return self.activate()

    def activate(self):
        """Open this tool, as a click would. The one shared entry point."""
        if self._on_click is None:
            return None
        try:
            self.focus_set()
        except Exception:
            pass
        try:
            return self._on_click(None)
        except Exception:
            return None

    def _on_key(self, _event=None):
        """Keyboard activation.

        Returns "break" so Space / Return do not also fall through to the
        enclosing page (a bare `return` does not suppress Tk's class binding,
        so the page would scroll under the card that was just activated).
        """
        try:
            self.activate()
        except Exception:
            pass
        return "break"

    def _bind_surface(self):
        """Bind the parts of the card the body helper does not own.

        `feature_card_body` binds its own six widgets, which leaves the card's
        padding and its rounded face unclickable - a card whose edge does
        nothing reads as a broken button. These bindings cover the rest.
        """
        try:
            self.configure(takefocus=1)
        except Exception:
            pass
        for seq in ("<Return>", "<space>"):
            try:
                self.bind(seq, self._on_key, add="+")
            except Exception:
                pass
        try:
            self.bind("<FocusIn>", lambda _e: self._show_focus(True), add="+")
            self.bind("<FocusOut>", lambda _e: self._show_focus(False), add="+")
        except Exception:
            pass
        for w in (self, self.body):
            try:
                w.bind("<Button-1>", lambda _e: self._activate(), add="+")
                w.bind("<Enter>", lambda _e: self._hover_at(True), add="+")
                w.bind("<Leave>", lambda _e: self._hover_at(False), add="+")
                w.configure(cursor="hand2" if self._on_click else "")
            except Exception:
                pass

    # ---- state -------------------------------------------------------- #

    def _show_focus(self, on):
        """A visible focus ring - focus you cannot see is not keyboard access."""
        try:
            if on:
                self.configure(highlightthickness=2,
                               highlightbackground=self._accent,
                               highlightcolor=self._accent)
            else:
                self.configure(highlightthickness=0)
        except Exception:
            pass

    def _hover_at(self, on):
        if self._hover == bool(on):
            return
        self._hover = bool(on)
        self._tint(COLORS["surface_hover"] if self._hover else self._face)

    def _tint(self, color):
        """Repaint the card face and every child that carries it.

        Matches on both the resting and the hover colour: matching only the
        resting colour makes the un-hover a no-op (the child's bg is already
        the hover tone), and a mouse parked over the grid then leaves every
        card it touched looking selected.
        """
        base = (self._face.lower(), COLORS["surface_hover"].lower())
        try:
            self.set_fill(color)
        except Exception:
            pass
        for w in self._tintables:
            try:
                if str(w.cget("bg")).lower() in base:
                    w.config(bg=color)
                    # The status badge paints its own background, so it has
                    # to be told too or a hover leaves a pale pill behind.
                    if w is self.badge:
                        w.set_face(color)
            except Exception:
                pass
