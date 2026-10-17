"""DetailPane: the right-hand column of a two-pane tool dialog.

Network Connections and Services both answer the same shape of question -
"tell me about this one row" - and both were doing it with a different
stack of ad-hoc labels. This is that stack, once:

    Title (10pt bold)
    ────────────────────────────
    SECTION (8pt bold, letterspaced by uppercasing)
    label            value
    label            value
    a free note line
    [ actions ]

Callers own the scroll container and hand this a `wrap` width, so the pane
reflows with the dialog instead of wrapping at a literal.

Depends only on app.ui.theme.
"""
from __future__ import annotations

import tkinter as tk

from app.ui.theme import COLORS, F, _measure_font, ellipsize
from app.ui.widgets.scroll import ScrollableRoundedPanel
from app.ui.widgets.tooltip import Tooltip


class DetailPane(tk.Frame):
    """A titled, sectioned key/value pane.

    The BODY scrolls; the header, its rule and the action row do not. The
    header is the pane's identity and the actions are its whole point -
    scrolling either away is how a user ends up unable to act on the thing
    they just scrolled to read.

    .body is still the frame callers add rows to: it is the scroller's inner
    frame, so add_row/add_note/add_section/clear need no knowledge of any of
    this.
    """

    PAD = 12

    def __init__(self, parent, title="", wrap=320, bg=None, accent=None,
                 on_click=None):
        self._page_bg = bg or (parent["bg"] if isinstance(
            parent, (tk.Frame, tk.Canvas)) else COLORS["bg"])
        super().__init__(parent, bg=self._page_bg, bd=0)
        self._wrap = int(wrap)
        self._accent = accent or COLORS["accent_sky"]
        self._on_click = on_click
        # Value labels whose wraplength must follow their measured width.
        # Cleared by clear() so a pane that is re-rendered 288 times (Services
        # re-renders per selection) does not accumulate dead widget refs.
        self._value_labels = []
        # True once the pane has a real width, after which a newly added row
        # can be wrapped immediately instead of waiting for a <Configure>
        # that may never come.
        self._wrap_applied = False

        self.head = tk.Label(self, text=title, font=(F, 10, "bold"),
                             bg=self._page_bg, fg=COLORS["text"],
                             anchor="w", justify="left", wraplength=self._wrap,
                             bd=0)
        self.head.pack(fill="x", padx=self.PAD, pady=(self.PAD, 0))

        # REDESIGN 2026-10: the header re-fits ITSELF when the pane is
        # resized, and clamps itself to two lines.
        #
        # It used to rely on the caller remembering to call set_wrap() at the
        # right moment. Network Connections did it inside _select(), i.e. only
        # AFTER a row was clicked - so the pane's first paint always used the
        # literal 320px from the constructor, and a long program name wrapped
        # to four lines (measured: "Charleston White on Sauce Walka's Filmer
        # Swing Him for Slapping Him on Stream - YouTube - Mozilla Firefox").
        # Four lines of TITLE is the tallest thing in the right column and it
        # is not data; it also pushed the body under it, so the same selection
        # rendered differently before and after a click.
        #
        # Two lines, then an ellipsis, is the contract. The full string is
        # the pane's own tooltip, so nothing is lost, and the body below
        # starts at a predictable offset on every render.
        self._full_title = str(title or "")
        self.bind("<Configure>", self._on_resize, add="+")
        try:
            Tooltip(self.head, self._full_title)
        except Exception:
            pass

        self.rule = tk.Frame(self, bg=COLORS["hairline"], height=1, bd=0)
        self.rule.pack(fill="x", padx=self.PAD, pady=(8, 0))

        # The scroller keeps its default 10px request: it must NOT report its
        # content's height upward, or a service with a long description
        # grows the whole dialog instead of scrolling its own pane. Measured
        # with propagate_height=True, a 290px detail pane dragged the layout
        # to 756px of content in a 302px viewport. What makes it fill the
        # space it is GIVEN is the outer body panel stretching its inner
        # frame to the viewport (ScrollableRoundedPanel(fill_inner=True)).
        self.scroller = ScrollableRoundedPanel(self)
        self.scroller.pack(fill="both", expand=True, pady=(4, 0))
        self.body = self.scroller.inner

        self.actions = tk.Frame(self, bg=self._page_bg, bd=0)
        self.actions.pack(fill="x", padx=self.PAD, pady=(8, self.PAD))

    # ---- api ---------------------------------------------------------- #

    def set_title(self, text):
        self._full_title = str(text or "")
        self._fit_title()
        try:
            Tooltip(self.head, self._full_title)
        except Exception:
            pass

    #: Hard cap on the header. Past this the title stops being a title and
    #: starts being a paragraph, and it is not data.
    MAX_TITLE_LINES = 2

    def set_wrap(self, wrap):
        """Override the HEADLINE wrap width. STILL SUPPORTED, now optional.

        Network Connections and Services both call this with
        split.column_width("right") - 24. That remains correct and is still
        the better number when the caller can measure the column directly;
        <Configure> simply re-derives it on any resize, so a caller that
        forgets is no longer broken (which was the actual bug: the FIRST
        paint happened before any caller had run, at the literal 320).

        It deliberately does NOT govern the row values - those are wrapped
        from their measured width by _apply_row_wraps, because a wraplength
        wider than the widget clips instead of wrapping.
        """
        try:
            self._wrap = max(80, int(wrap))
            self.head.config(wraplength=self._wrap)
            self._wrap_applied = True
        except Exception:
            pass
        self._apply_row_wraps()
        self._fit_title()

    def _on_resize(self, event=None):
        try:
            self._wrap = max(80, self.winfo_width() - self.PAD * 2)
            self.head.config(wraplength=self._wrap)
            self._wrap_applied = True
        except Exception:
            pass
        self._apply_row_wraps()
        self._fit_title()

    def _fit_title(self):
        """Clamp the title to MAX_TITLE_LINES, ellipsizing past that."""
        try:
            room = self._wrap
            if room < 60:
                return
            if not self._full_title:
                self.head.config(text="")
                return
            f = _measure_font(self.winfo_toplevel(), self.head.cget("font"))
            one_line = f.measure(self._full_title)
            if one_line <= room:
                # Definitely one line - no wrapping, no truncation needed.
                self.head.config(wraplength=room, text=self._full_title)
                return
            # Wraps to more than one line: budget two lines' worth and cut.
            two = room * self.MAX_TITLE_LINES
            self.head.config(wraplength=room,
                             text=ellipsize(self._full_title,
                                             self.head.cget("font"), two))
        except Exception:
            pass

    def clear(self):
        for w in list(self.body.winfo_children()):
            w.destroy()
        for w in list(self.actions.winfo_children()):
            w.destroy()
        # Drop the destroyed labels. Services re-renders this pane on every
        # selection, so leaving them here would pin every label ever shown -
        # and _apply_row_wraps would then config() dead widgets forever.
        self._value_labels = []

    def settle(self, flush=True):
        """Re-measure the body after a rebuild.

        The flush is NOT optional here. winfo_reqheight() on a Frame returns
        a CACHED request, and freshly packed labels do not appear in it until
        a geometry pass has run - so settle(flush=False) measured the pane
        while it was still empty and left the scrollbar reporting height 1.
        Measured: after the rebuild, content_h read 1 with 290px of rows
        sitting in the pane, and the whole Services layout collapsed to 90px
        because every ancestor was sized from that lie.

        flush=True costs one update_idletasks() over this pane's ~15 widgets,
        which is nothing like the 90-220ms the Install catalog's full-panel
        flush measures. It runs once per rebuild, not per row.
        """
        try:
            self.scroller.refresh_scroll(flush=flush)
        except Exception:
            pass
        # Re-derive the row wraps from the real label widths. settle() is where
        # the pane's geometry is guaranteed to be current, so it is the right
        # place for this - and it means a caller that never resizes still gets
        # correctly wrapped values on its FIRST paint.
        self._apply_row_wraps()

    def add_section(self, title):
        tk.Label(self.body, text=str(title or "").upper(), font=(F, 8, "bold"),
                 bg=self._page_bg, fg=COLORS["subtext"], anchor="w",
                 justify="left", bd=0).pack(
                     fill="x", padx=self.PAD, pady=(10, 3))
        return self

    #: The label column is a FIXED character width so every value in the pane
    #: starts at the same x. Measured at (F, 8) it is 101px, not the 96 this
    #: module used to assume - see add_row for why that mattered.
    LABEL_COL_CHARS = 11
    LABEL_COL_PAD = 8

    def add_row(self, label, value, color=None, mono=False):
        """One label/value line. An empty value still draws the label, so a
        field that went missing reads as a gap rather than vanishing."""
        row = tk.Frame(self.body, bg=self._page_bg, bd=0)
        row.pack(fill="x", padx=self.PAD, pady=(0, 2))
        tk.Label(row, text=str(label or ""), font=(F, 8),
                 bg=self._page_bg, fg=COLORS["subtext"], anchor="w",
                 width=self.LABEL_COL_CHARS,
                 justify="left", bd=0).pack(side="left",
                                            padx=(0, self.LABEL_COL_PAD))
        font = (F, 9, "bold") if mono else (F, 9)
        # A provisional wraplength is set HERE rather than left at Tk's
        # default of 0, which means NEVER WRAP - i.e. clip. It is deliberately
        # an UNDER-estimate of the available width: too small only wraps a
        # little early, while too large shears the text, so the safe direction
        # to be wrong in is the short one. _apply_row_wraps replaces it with
        # the measured width as soon as there is one.
        provisional = max(80, self._wrap - self.PAD * 2 - 118)
        val = tk.Label(row, text=str(value or ""), font=font,
                       bg=self._page_bg, fg=color or COLORS["text"],
                       anchor="w", justify="left", wraplength=provisional,
                       bd=0)
        val.pack(side="left", fill="x", expand=True)
        # Tracked so the wrap can be re-applied from the label's REAL width
        # once the pane has been laid out. See _apply_row_wraps.
        self._value_labels.append(val)
        if self._wrap_applied:
            # The pane has already been laid out once, so this row has a real
            # width available immediately and does not have to wait for the
            # next <Configure> - which for a pane whose width has not changed
            # may never arrive.
            try:
                w = val.winfo_width()
                if w > 1:
                    val.config(wraplength=w)
            except Exception:
                pass
        if self._on_click is not None:
            for w in row.winfo_children():
                try:
                    w.bind("<Button-1>", self._on_click, add="+")
                    w.configure(cursor="hand2")
                except Exception:
                    pass
        return self

    def _apply_row_wraps(self):
        """Set each value label's wraplength from its OWN width.

        BUGFIX 2026-10: add_row computed the wrap as
        `self._wrap - PAD * 2 - 96`, subtracting a HARDCODED estimate of the
        label column. That column is `width=11` characters, which measures
        101px at (F, 8) - so the estimate was 5px short on every row, and the
        value's wraplength came out WIDER than the space pack had actually
        given it (measured: wraplength 276 against a real width of 257).

        Tk does not wrap when wraplength exceeds the widget width; it lets the
        text run past the edge and clips it. That is why the Services detail
        pane showed "Advanced Micro Devices, Ir" and "the system itself -
        full cont" with no ellipsis and no second line - the text was being
        sheared exactly as pack would shear it, just inside a label instead
        of a row.

        Measuring the label is the only way to get this right: the width is
        font-dependent, so it cannot be a constant, and it is not knowable
        before layout.
        """
        for val in self._value_labels:
            try:
                w = val.winfo_width()
            except Exception:
                continue
            if w <= 1:
                continue        # not laid out yet; the resize path returns
            try:
                val.config(wraplength=w)
            except Exception:
                pass

    def add_note(self, text, color=None, font=None):
        tk.Label(self.body, text=str(text or ""), font=font or (F, 8),
                 bg=self._page_bg, fg=color or COLORS["subtext"], anchor="w",
                 justify="left", wraplength=max(80, self._wrap - self.PAD * 2),
                 bd=0).pack(fill="x", padx=self.PAD, pady=(3, 0))
        return self

    def add_blank(self, height=6):
        tk.Frame(self.body, bg=self._page_bg, height=height,
                 bd=0).pack(fill="x")
        return self