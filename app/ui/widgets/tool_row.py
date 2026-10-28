"""ToolRow: the ONE row shape every Tools popup draws.

Six dialogs, six hand-rolled row painters, six slightly different column
orders - which is exactly what "my UI is not symmetrical" looks like when
you line the popups up side by side. Every list row in the app now comes
from here:

    [dot] Title in bold 9pt ............. meta 9pt  [actions]  [chevron]
         secondary line 8pt

Rules that make rows line up across dialogs:

  * two text sizes only - 9pt primary, 8pt secondary. No dialog gets to
    invent a third.
  * the meta label is right-aligned against a fixed right inset, so the
    right-hand column reads as a straight line down the list instead of a
    ragged one that follows the length of each value.
  * the status dot is a fixed 10px and is either present or absent, never
    a variable-width glyph, so a row with a dot and a row without one still
    share the same right-hand text edge.
  * hover tints every descendant carrying the row background, and the whole
    row is the click target (W3C disclosure-card / listbox behaviour), not
    a small chevron.

Depends only on app.ui.theme.
"""
from __future__ import annotations

import tkinter as tk

from app.ui.metrics import CARD_BODY_INSET, GROUP_GAP, HAIRLINE_NUDGE, TEXT_NUDGE
from app.ui.theme import COLORS, RADIUS_SURFACE, SPACE_2, TYPE, ellipsize
from app.ui.widgets.rounded_card import RoundedCard

#: Chevron glyphs. U+203A closed, U+2304 open - the pair the health report
#: already used, now the app's single disclosure language instead of one
#: invented per file.
CHEVRON_CLOSED = "›"
CHEVRON_OPEN = "⌄"

DOT_PX = 14
PAD_X = CARD_BODY_INSET

#: The row ladder. A list row is one of three sizes - there is no fourth.
#:
#: name -> (title font, second-line font, meta font, vertical pad, min height)
#:
#: `lg` is the Tools popup row, unchanged since the 2026-09 redesign: 11pt
#: bold title, 9pt second line. `md` and `sm` read the semantic type roles,
#: so a general list row cannot drift from a card title or a caption.
#:
#: The height is a FLOOR, not a fixed size: measured on a real root a
#: single-line row is 37 / 45 / 52 px, and a row carrying a second line is
#: 55 / 63 / 70. The ladder is what keeps the three rungs apart; a caller
#: adds or omits the second line, and the row follows.
SIZES = {
    "lg": (TYPE["card_title"], TYPE["caption"], TYPE["body_medium"], 9, 52),
    "md": (TYPE["row_title"], TYPE["caption"], TYPE["caption"], 7, 44),
    "sm": (TYPE["label_medium"], TYPE["caption"], TYPE["caption"], 4, 36),
}
DEFAULT_SIZE = "lg"


class ToolRow(RoundedCard):
    """One list row: status dot, title, secondary line, meta, actions.

    `wrap` is the pixel width the secondary line wraps at. Pass
    ToolShell.body_width() rather than a literal - a hardcoded wraplength is
    what made six popups reflow differently at different window sizes.
    """

    def __init__(self, parent, title="", sub="", meta="", dot=None,
                 chevron=False, open_now=False, on_click=None, wrap=None,
                 bg=None, accent=None, size=DEFAULT_SIZE, leading_px=0,
                 show_dot=True, fill=None):
        self._page_bg = bg or (parent["bg"] if isinstance(
            parent, (tk.Frame, tk.Canvas)) else COLORS["bg"])
        # One ladder, resolved before anything is built: a row that sized its
        # own labels would be the fifth row height the audit counted.
        t_font, s_font, m_font, pad_y, min_h = SIZES.get(
            size, SIZES[DEFAULT_SIZE])
        self.size = size if size in SIZES else DEFAULT_SIZE
        self._pad_y = pad_y
        self._show_dot = bool(show_dot)
        self._leading_px = int(leading_px or 0)
        # REDESIGN 2026-10: the row face is a RoundedCard now, not a square
        # Frame with a 1px hairline parent. Every row in every Tools popup
        # goes through this class, so this one swap rounds the entire list
        # surface of the app at once - and, more importantly, makes a row the
        # same shape as the cards above it. Rounded surfaces over square rows
        # is what read as "Tkinter app with dark colours".
        # `fill` lets a row inside a card sit FLAT on that card's surface
        # instead of drawing a second card inside it. It defaults to the
        # raised row face every Tools list uses.
        self._bg = fill or COLORS["bg_alt"]
        super().__init__(parent, fill=self._bg, bg=self._page_bg,
                         radius=RADIUS_SURFACE, min_height=min_h)
        self._accent = accent or COLORS["accent_sky"]
        self._hover = False
        self._selected = False
        self._on_click = on_click
        self._wants_chev = bool(chevron)

        inner = self._inner = self.body
        # Public alias, matching ScrollableRoundedPanel.inner: a caller
        # reaching for the row's content frame should not need to know it
        # was stored under a private name.
        self.inner = inner

        # Selection marker, place()d rather than packed so showing it never
        # reflows the row - a bar that shifts the text when you click is
        # worse than no bar.
        self._bar = tk.Frame(inner, bg=self._accent, width=3, bd=0)

        # Packed right-to-left in reverse: the first pack(side="right") ends
        # up rightmost, so chev -> actions -> meta is the reading order.
        self._chev = tk.Label(inner, text="", font=TYPE["chevron"],
                              bg=self._bg, fg=COLORS["subtext"], width=1,
                              anchor="center", bd=0)
        self.actions = tk.Frame(inner, bg=self._bg, bd=0)
        self.meta = tk.Label(inner, text=meta, font=m_font, bg=self._bg,
                             fg=COLORS["subtext"], anchor="e", bd=0)

        if self._wants_chev:
            self._chev.pack(side="right", padx=(0, GROUP_GAP))
        self.actions.pack(side="right")
        self.meta.pack(side="right", padx=(SPACE_2, TEXT_NUDGE))

        # LEADING slot: a caller-fillable frame for an app icon, a chip or a
        # checkbox. Width is requested up front so the text column starts at
        # the same x on every row of a list, whether or not an icon has
        # loaded yet - the same rule DataTable uses for its icon column.
        self.leading = tk.Frame(inner, bg=self._bg, bd=0)
        if self._leading_px:
            # A leading slot with only a width and pack_propagate(False) has
            # a requested height of ONE pixel, so Tk gives it one pixel and
            # every app icon inside is clipped to a horizontal sliver. Give
            # it a real square (or the row's content height, whichever is
            # larger) and let the row grow to hold it.
            leading_h = max(self._leading_px, min_h - 2 * pad_y)
            self.leading.configure(width=self._leading_px,
                                   height=leading_h)
            self.leading.pack_propagate(False)
            self.leading.pack(side="left", padx=(PAD_X, 0), pady=pad_y)

        self.dot = tk.Canvas(inner, width=DOT_PX, height=DOT_PX,
                             highlightthickness=0, bd=0, bg=self._bg)
        if self._show_dot:
            self.dot.pack(side="left", padx=(PAD_X, 0))

        col = self._col = tk.Frame(inner, bg=self._bg, bd=0)
        col.pack(
            side="left", fill="x", expand=True,
            padx=(GROUP_GAP if self._show_dot or self._leading_px
                  else PAD_X, 0),
            pady=(pad_y, pad_y))
        self.title = tk.Label(col, text=title, font=t_font,
                              bg=self._bg, fg=COLORS["text"], anchor="w",
                              justify="left", bd=0)
        self.title.pack(fill="x")
        self.sub = tk.Label(col, text=sub, font=s_font, bg=self._bg,
                            fg=COLORS["subtext"], anchor="w", justify="left",
                            wraplength=wrap or 600, bd=0)
        # An EMPTY second line is not packed at all. A 1px-tall empty Label
        # still reserves a full line's worth of gap in a packed column, and
        # Services has up to 288 rows: 288 phantom lines is the difference
        # between seeing a screen of services and seeing four.
        if sub:
            self.sub.pack(fill="x", pady=(HAIRLINE_NUDGE, 0))
        self._full_title = str(title or "")

        self._tintables = [inner, col, self.title, self.sub, self.meta,
                           self._chev, self.actions, self.dot, self.leading]
        self._bind_tree(inner)
        self._bind_tree(self)
        if self._on_click is not None:
            # takefocus must be set on the CARD itself, which is a tk.Canvas
            # (RoundedCard subclasses it). An earlier attempt assigned
            # self.takefocus = 1, which does NOT change the widget option -
            # cget("takefocus") stayed "" and Tab skipped every row.
            try:
                self.configure(takefocus=1)
            except Exception:
                pass
            # Keyboard is bound to the CARD only, never to its children:
            # binding <space> to a child Label would swallow the space key
            # across the whole list, and the card is the widget that can
            # take focus anyway.
            self._bind_keyboard(self)
        # Re-fit the title whenever the row is given a width, so a
        # programmatic geometry change (a resize, a longer filter) does not
        # leave a stale truncation.
        try:
            col.bind("<Configure>", lambda _e: self._refit(), add="+")
        except Exception:
            pass
        if on_click is not None:
            self._set_clickable(True)
        if dot:
            self.set_dot(dot)
        self.set_chevron(open_now)

    def _refit(self):
        try:
            self.title.config(text=self._fit_title(self._full_title))
        except Exception:
            pass

    # ---- construction helpers ---------------------------------------- #

    def _bind_tree(self, w):
        """Bind hover + click on a widget and every descendant.

        Tk reports Enter/Leave per widget, so binding only the row frame
        loses the tint the moment the pointer crosses onto a child label.
        add="+" so a caller's own bindings survive.
        """
        try:
            w.bind("<Enter>", self._on_enter, add="+")
            w.bind("<Leave>", self._on_leave, add="+")
            if self._on_click is not None:
                w.bind("<Button-1>", self._on_click, add="+")
        except Exception:
            pass
        for c in w.winfo_children():
            self._bind_tree(c)

    def bind_content(self, *widgets):
        """Bind hover (and click) on widgets added after construction.

        Tk reports Enter/Leave per widget, so a button packed into `actions`
        or a label dropped into `leading` later never inherits the row's
        tint: the pointer crosses it and that child stays the resting
        colour, which reads as a pale patch inside the row. Call this once
        the slots are filled.
        """
        for w in (widgets or (self.inner,)):
            self._bind_tree(w)

    def _bind_keyboard(self, w):
        """Keyboard equivalents for <Button-1>, so a row is usable without a mouse.

        REDESIGN 2026-10 (audit E1). Every list in Services, Network
        Connections, Duplicates, File Lock and Restore Points is built from this
        class, and it bound <Button-1> only. Tk gives a widget no keyboard
        activation unless it can take focus, so selecting a service - and
        therefore reaching the action bar that acts on it - was impossible
        without a pointing device. Five dialogs were mouse-only as a result.

        The card takes focus (takefocus=1, set by the caller); Return, KP_Enter
        and Space activate it, Up/Down (and Left/Right) walk to the adjacent
        row so a long list can be traversed, and the focus ring is the accent
        highlight so focus is visible. activate() is the single entry point, so
        the keyboard and the mouse run identical code.

        WHY add="+" ON THE CARD, NOT ON A CHILD: the card is a tk.Canvas, and
        Tk delivers a key event to the FOCUSED widget only. Binding the row's
        inner frame instead appeared to work - <FocusIn> proved the inner
        frame had the binding - but focus_set() lands on the card, so the card
        is what must carry every one of these. An earlier version bound them
        inside _bind_tree() where the descendants were walked, and the keys
        were bound to frames that could never be the focus target.
        """
        try:
            self._is_actionable = True
            # No <KP_Enter>. Tk on Windows exposes the numeric keypad's Enter
            # as plain "Return" (verified: synthesising a KP_Enter event
            # raises "no keycode for keysym KP_Enter"), so binding it adds an
            # unreachable second path for the same physical key.
            for seq in ("<Return>", "<space>"):
                w.bind(seq, self._on_activate_key, add="+")
            for seq, step in (("<Up>", -1), ("<Left>", -1),
                              ("<Down>", 1), ("<Right>", 1)):
                w.bind(seq, (lambda s: lambda e: self._focus_sibling(s))(step),
                       add="+")
            w.bind("<FocusIn>", self._on_focus_in, add="+")
            w.bind("<FocusOut>", self._on_focus_out, add="+")
        except Exception:
            pass

    def _on_activate_key(self, _event=None):
        """Keyboard activation.

        Returns "break" so the key does not ALSO reach the scroll panel behind
        the list - otherwise Space/Return fall through to the enclosing
        <KeyPress> handlers and can scroll the list out from under the row the
        user just activated. A bare `return` from the callback does not
        suppress Tk's class binding; "break" is what does.
        """
        try:
            self.activate()
        except Exception:
            pass
        return "break"

    def activate(self):
        """Select this row, as a click would. The one shared entry point."""
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

    def _focus_sibling(self, step):
        """Move focus to the next/previous ToolRow among our siblings.

        Built from the parent's children rather than a dialog-level list so it
        keeps working when a list is rebuilt, filtered or reordered - which
        all five of these dialogs do while a user is typing in the search box.
        Skips rows that are not actionable rather than stopping on them.
        """
        try:
            kids = [c for c in self.master.winfo_children()
                    if isinstance(c, ToolRow)]
            here = kids.index(self)
            # An explicit bounds check, not just a wrapped range. A previous
            # version indexed kids[here + step] directly, so Up on the FIRST
            # row computed index -1 and Python's negative indexing silently
            # wrapped to the LAST row - pressing Up at the top of a list
            # teleported the user to the bottom.
            target = here + step
            if target < 0 or target >= len(kids):
                return None
            nxt = kids[target]
            if not getattr(nxt, "_is_actionable", False):
                return None
            nxt.focus_set()
            try:
                nxt._show_focus(True)
            except Exception:
                pass
            return "break"
        except Exception:
            return None

    def _show_focus(self, on):
        """A visible focus ring - focus you cannot see is not keyboard access.

        The card IS the Canvas (RoundedCard subclasses tk.Canvas), so the
        highlight ring goes on `self`. highlightbackground is used because Tk
        draws the focus ring with highlightcolor on some themes and
        highlightbackground on others; both are set so the ring is the accent
        colour rather than the platform default.
        """
        try:
            if on and not self._selected:
                self.configure(highlightthickness=2,
                               highlightbackground=self._accent,
                               highlightcolor=self._accent)
            else:
                self.configure(highlightthickness=0)
        except Exception:
            pass

    def _on_focus_in(self, _e=None):
        self._show_focus(True)

    def _on_focus_out(self, _e=None):
        self._show_focus(False)

    def _set_clickable(self, on):
        for w in [self, self._inner, self._col, self.title, self.sub,
                  self.meta, self.dot, self._chev, self.actions]:
            try:
                w.configure(cursor="hand2" if on else "")
            except Exception:
                pass

    # ---- api ---------------------------------------------------------- #

    def set_title(self, text):
        self._full_title = str(text or "")
        self.title.config(text=self._fit_title(self._full_title))

    def _fit_title(self, text):
        """Ellipsize to whatever the row can actually show.

        Without this a long name is clipped MID-CHARACTER by pack, which
        reads as a rendering fault rather than as truncation - "Pinhead vs.
        Penny" with the rest of the title sheared off. The full name is
        always in the tooltip and in the detail pane, so nothing is lost.

        Delegates to theme.ellipsize so this, the Tools card titles and the
        preset card titles cannot drift into three different truncation
        behaviours (redesign 2026-10).
        """
        try:
            # The dot and the leading slot are siblings of the text column,
            # so they are already outside its width - the small reserve here
            # is the gap between them and the first glyph.
            reserve = (DOT_PX + 12) if self._show_dot else 8
            room = self._col.winfo_width() - reserve
            if room < 40:
                return text          # not laid out yet; show it all
            return ellipsize(text, self.title.cget("font"), room)
        except Exception:
            return text

    def set_sub(self, text):
        """Set the second line, packing or unpacking it as needed."""
        text = str(text or "")
        self.sub.config(text=text)
        try:
            if text and not self.sub.winfo_manager():
                self.sub.pack(fill="x", pady=(HAIRLINE_NUDGE, 0))
            elif not text and self.sub.winfo_manager():
                self.sub.pack_forget()
        except Exception:
            pass


    def set_dot(self, color):
        """Draw the status dot, or clear it when color is None."""
        try:
            self.dot.delete("d")
            if color:
                c = DOT_PX / 2.0
                r = DOT_PX / 2.0 - 1
                self.dot.create_polygon(c - r, c, c, c - r, c + r, c,
                                        c, c + r, smooth=True, fill=color,
                                        outline="", tags="d")
        except Exception:
            pass

    def set_chevron(self, open_now):
        try:
            self._chev.config(text=(CHEVRON_OPEN if open_now
                                    else CHEVRON_CLOSED)
                              if self._wants_chev else "")
        except Exception:
            pass

    def set_selected(self, on):
        self._selected = bool(on)
        try:
            if self._selected:
                self._bar.place(x=0, y=0, relheight=1.0, width=3)
            else:
                self._bar.place_forget()
        except Exception:
            pass


    # ---- hover -------------------------------------------------------- #

    def _on_enter(self, _e=None):
        if self._hover:
            return
        self._hover = True
        self._tint(COLORS["surface_hover"])

    def _on_leave(self, _e=None):
        if not self._hover:
            return
        self._hover = False
        self._tint(self._bg)

    def _tint(self, color):
        """Repaint every descendant that carries a row background.

        Matches on BOTH the resting and the hover colour. Matching only the
        resting colour looks right until you un-hover, at which point the
        widget's bg is no longer the resting colour, nothing matches, and the
        tint sticks for the rest of the row's life - a mouse that ends up
        parked anywhere over a list leaves every row it touched highlighted.

        The card face goes through set_fill rather than a plain config: the
        rounded face is painted by RoundedCard, so recolouring only the
        children would leave a two-tone card with the old colour showing
        through the corner radius.
        """
        base = self._bg.lower()
        hot = COLORS["surface_hover"].lower()
        try:
            self.set_fill(color)
        except Exception:
            pass
        for w in self._tintables:
            try:
                if str(w.cget("bg")).lower() in (base, hot):
                    w.config(bg=color)
            except Exception:
                pass
