"""The one card anatomy: icon, title, visible supporting line.

The 2026-10 design audit found two card species on adjacent pages (C-001):

* preset cards: 168px, a 52px icon above a 14pt title and a visible blurb;
* tool cards: a 40px icon beside an 11pt title and **no blurb at all** - the
  description existed only in a tooltip, so the page could not explain itself
  without hovering.

Both now build their content through `feature_card_body`, so the icon column,
the title, the supporting line, the padding and the ellipsis rule are the
same. The page still owns its RoundedCard so it can keep its selection state
and hover animation; what it cannot do is invent a third anatomy.

The compact density is what the Tools tiles use: a 40px (DPI-scaled) mark with
4px vertical / 12px horizontal padding and a one-line blurb, which is what
keeps five rows of cards inside the 626px page band the shell gives the page at
the 125% floor. The preset density uses a 52px icon, 16/12 padding and a
two-line blurb. Both come out of `pad_x` / `pad_y` / `box` rather than a second
anatomy.
"""
from __future__ import annotations

import tkinter as tk

from app.ui.theme import (
    CARD_INSET, COLORS, ICON_CARD_PRESET, ICON_CARD_TOOL, RADIUS_PILL,
    SPACE_1, SPACE_2, SPACE_3, SPACE_5, SPACE_6, SURFACE_CARD, TYPE,
    _hex_lerp, _measure_font, _round_rect_points, ellipsize
)


class CardStatusBadge(tk.Canvas):
    """The card's trailing status slot: a compact tonal pill.

    AUDIT TV-002 asked for a state on every card, not another paragraph.
    The badge says what it found ("2 issues"), never a bare count: a unmarked
    "2" on Startup Manager could be read as two startup programs rather than
    two findings. It lives in the title row so it cannot overlap the blurb,
    and it is a canvas rather than a Label so the corners stay on the shape
    scale like every other chip in the app.

    The widget is created hidden; `set()` packs it, `clear()` takes it out
    again. That lets the hub update a card in place without rebuilding the
    grid (and without re-decoding its icon).
    """

    def __init__(self, parent, face=SURFACE_CARD):
        super().__init__(parent, width=1, height=SPACE_5, bg=face,
                         highlightthickness=0, bd=0)
        self._face = face
        self._text = ""
        self._tone = COLORS["subtext"]

    @property
    def text(self):
        return self._text

    @property
    def tone(self):
        return self._tone

    def set(self, text, tone):
        """Show `text` in `tone`; an empty string clears the badge."""
        text = str(text or "")
        if not text:
            self.clear()
            return
        self._text = text
        self._tone = tone or COLORS["subtext"]
        try:
            if not self.winfo_manager():
                self.pack(side="right", padx=(SPACE_2, 0))
        except Exception:
            pass
        self._draw()

    def clear(self):
        self._text = ""
        try:
            self.pack_forget()
        except Exception:
            pass

    def set_face(self, color):
        """Re-paint for a new card face (hover, selection).

        The pill's own background is a blend of the FACE it sits on, so a
        caller that re-tints the card has to tell the badge or the hover
        leaves a pale pill floating on the darker card - the one rectangle
        in the grid that does not follow the pointer.
        """
        try:
            color = color or self._face
            if color == self._face:
                return
            self._face = color
            self.configure(bg=color)
            if self._text:
                self._draw()
        except Exception:
            pass

    def _draw(self):
        try:
            font = _measure_font(self, TYPE["label_small"])
            width = max(SPACE_6, int(font.measure(self._text)) + SPACE_2 * 2)
            height = SPACE_5
            self.configure(width=width, height=height)
            self.delete("all")
            self.create_polygon(
                _round_rect_points(0, 0, width, height, RADIUS_PILL),
                smooth=True,
                fill=_hex_lerp(self._face, self._tone, 0.18), outline="")
            self.create_text(width / 2.0, height / 2.0, text=self._text,
                             font=TYPE["label_small"], fill=self._tone)
        except Exception:
            pass


def _plain_glyph(glyph):
    """The glyph without its emoji VARIATION SELECTOR (U+FE0F).

    Tk measures the selector as a glyph of its own, so "\U0001F6E1\uFE0F" is
    twice as wide as the shield it is meant to be (82px against 41 at 18pt)
    and the mark spills out of a fixed-size icon chip. The selector only asks
    for emoji presentation, which the base code point already gets in the UI
    font, so dropping it changes nothing but the measurement.
    """
    return str(glyph or "").replace("\uFE0F", "")


def feature_card_body(card, title, blurb, accent, *, photo=None,
                      glyph="\u2022", box=None, compact=False, face=None,
                      click=None, hover=None, glyph_font=None, pad_y=None,
                      pad_x=None):
    """Fill `card.body` with the shared anatomy and return its handles.

    `click`/`hover` are optional callbacks taking ``(widget)`` and
    ``(widget, on)``; every part of the card is bound so the whole face is
    one target. The caller keeps ownership of the card itself.
    """
    face = face or SURFACE_CARD
    box = int(box or (ICON_CARD_TOOL if compact else ICON_CARD_PRESET))
    # `pad_x`/`pad_y` are the density knobs: the preset cards want the
    # comfortable 16/12 rhythm, a grid of nineteen tiles wants the compact
    # 12/4 one - which is what keeps a tool name on ONE line in a four-across
    # column, and keeps the page inside the band the shell gives it at 125%.
    pad_y = SPACE_3 if pad_y is None else int(pad_y)
    pad_x = CARD_INSET if pad_x is None else int(pad_x)
    head = tk.Frame(card.body, bg=face)
    head.pack(fill="both", expand=True, padx=pad_x,
              pady=(pad_y, pad_y))

    icon = tk.Canvas(head, width=box, height=box, bg=face,
                     highlightthickness=0, bd=0)
    icon.create_oval(0, 0, box - 1, box - 1,
                     fill=_hex_lerp(face, accent, 0.22), outline="")
    if photo is not None:
        icon.create_image(box // 2, box // 2, image=photo)
    elif glyph:
        # `glyph_font` is for the smaller card marks: the default is sized
        # for the 52px preset chip and overflows a 40px tool chip, so a
        # caller with art-less tools can pass a fitting size instead of
        # rendering a clipped emoji.
        icon.create_text(box // 2, box // 2, text=_plain_glyph(glyph),
                         font=glyph_font or TYPE["readout_large"],
                         fill=accent)
    icon.pack(side="left", anchor="n")

    txt = tk.Frame(head, bg=face)
    txt.pack(side="left", fill="both", expand=True, padx=(SPACE_3, 0))

    title_row = tk.Frame(txt, bg=face)
    title_row.pack(fill="x")
    title_lbl = tk.Label(title_row, text=str(title), font=TYPE["card_title"],
                         bg=face, fg=COLORS["text"], anchor="w", bd=0)
    title_lbl.pack(side="left", fill="x", expand=True)
    status = CardStatusBadge(title_row, face=face)

    blurb_lbl = tk.Label(
        txt, text=str(blurb or ""), font=TYPE["caption"], bg=face,
        fg=COLORS["subtext"], anchor="w", justify="left", bd=0)
    if compact:
        # One line, ellipsized to the measured column. The full sentence is
        # still in the caller's tooltip; the card itself must say enough to
        # choose without hovering.
        blurb_lbl.pack(fill="x", pady=(SPACE_1, 0))
    else:
        # Two reserved lines at the comfortable density: a one-line and a
        # two-line blurb then share a card height, so the row cannot jump.
        blurb_lbl.configure(height=2)
        blurb_lbl.pack(fill="x", pady=(SPACE_1, 0))

    def _fit(event=None):
        try:
            body_room = int(txt.winfo_width()) - 2
        except Exception:
            return
        if body_room < 40:
            return
        # The badge is a trailing slot in the title row, so it must come out
        # of the title's measure but not out of the blurb's: the blurb spans
        # the full text column underneath it.
        title_room = body_room
        try:
            if status.winfo_manager():
                title_room -= int(status.winfo_reqwidth()) + SPACE_2
        except Exception:
            pass
        title_lbl.config(
            text=ellipsize(str(title), title_lbl.cget("font"),
                           max(1, title_room)))
        if compact:
            blurb_lbl.config(
                text=ellipsize(str(blurb or ""), blurb_lbl.cget("font"),
                               body_room))
        else:
            blurb_lbl.config(wraplength=max(40, body_room))

    txt.bind("<Configure>", _fit, add="+")
    head.bind("<Configure>", _fit, add="+")
    # A badge appearing or changing width changes the title's room even when
    # the text column itself does not resize; the label's own Configure is
    # the event that catches that reflow.
    title_lbl.bind("<Configure>", _fit, add="+")
    _fit()

    targets = (head, icon, txt, title_lbl, blurb_lbl, status)
    if click is not None:
        for w in targets:
            w.bind("<Button-1>", lambda _e: click(), add="+")
    if hover is not None:
        for w in targets:
            w.bind("<Enter>", lambda _e: hover(True), add="+")
            w.bind("<Leave>", lambda _e: hover(False), add="+")
    return head, title_lbl, blurb_lbl, icon, status
