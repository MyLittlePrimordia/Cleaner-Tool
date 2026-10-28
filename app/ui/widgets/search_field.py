"""SearchField: the one search box, with a placeholder and a clear button.

Three dialogs had a bare RoundedEntry and nothing else, so the user had no
affordance beyond the caret. SearchField puts the glyph, the placeholder and
the clear affordance INSIDE the same rounded pill, which a Frame-plus-Entry
cannot do - hence the canvas backing.

It is a RoundedEntry SUBCLASS rather than a second rounded entry. That is not
tidiness, it is two concrete things:

  * RoundedEntry's constructor defers its first paint with `after(0)` so the
    widget has real geometry before drawing. verify_no_offthread_tk.py fails
    the build on any zero-delay `after()` outside its allow-list, and
    `after_idle` is flagged by the same check - so a from-scratch copy would
    have needed a SECOND allow-list entry for the same one-line reason.
    Inheriting it keeps exactly one such call in the codebase.
  * The rounded plate, the focus ring and the Entry setup are the fiddly
    parts. There is one of them now, not two that can drift.

Like RoundedEntry it is a drop-in: pack/grid the SearchField itself and use
`.entry` exactly like the Entry it replaces. Bind `on_change` and it reports
every keystroke, so the caller stays in control of its own debounce.

Depends only on app.ui.theme (and its sibling RoundedEntry, same layer).
"""
from __future__ import annotations


from app.ui.theme import COLORS, RADIUS_PILL, TYPE
from app.ui.metrics import FIELD_PAD
from app.ui.widgets.entry import RoundedEntry

#: The magnifier is DRAWN, not typed. Segoe UI's U+2315 TELEPHONE RECORDER
#: is the wrong shape for "search" and renders as a smudge at 11pt -
#: measured on this machine, it read as a lowercase "o" with a tail. Two
#: canvas primitives are the same three lines of code and are pixel-exact
#: at any DPI.
_GLYPH_CX = 24.0
_GLYPH_R = 7.0
_CLEAR = "✕"


class SearchField(RoundedEntry):
    """Rounded search pill: drawn magnifier, placeholder, Entry, clear."""

    GLYPH_W = 48
    CLEAR_W = 30
    RADIUS = RADIUS_PILL   # the search bar is the pill role
    PAD_Y = FIELD_PAD

    def __init__(self, parent, textvariable=None, placeholder="Search",
                 width=20, font=None, accent=None, on_change=None, **kw):
        # M3 search bar: a wide centred pill (width in chars is a floor)
        super().__init__(parent, textvariable=textvariable,
                         width=max(int(width), 52), font=font or TYPE["body_large"],
                         accent=accent)
        self._ph = str(placeholder or "Search")
        self._on_change = on_change
        self._var = textvariable
        self._clear_hover = False
        self._last = None

        # The inherited Entry is inset by RADIUS+2 per side; a search pill
        # needs room for the glyph and the clear glyph inside that instead.
        self.entry.pack_configure(padx=(self.GLYPH_W, self.CLEAR_W))

        self._cv.bind("<Button-1>", self._on_canvas_click)
        self._cv.bind("<Motion>", self._on_motion)
        self._cv.bind("<Leave>", lambda _e: self._set_clear_hover(False))

        try:
            if textvariable is not None:
                textvariable.trace_add("write", self._on_text)
        except Exception:
            pass
        self._draw(focused=False)

    # ---- api ---------------------------------------------------------- #

    def pack(self, **kw):
        # Centre the pill instead of stretching it edge to edge.
        if kw.get("fill") in ("x", "both"):
            kw["fill"] = "none"
        kw.setdefault("anchor", "center")
        return self.pack_configure(**kw)

    def clear(self):
        try:
            self.entry.delete(0, "end")
        except Exception:
            pass

    def focus(self):
        try:
            self.entry.focus_set()
        except Exception:
            pass

    def text(self):
        """Current text.

        Read from the VARIABLE when one is attached, not from the Entry.
        Inside a StringVar trace callback the Entry has not been updated yet
        - measured: with a trace on the variable, `entry.get()` still
        returned the previous value while `var.get()` was already the new
        one. Reading the Entry there makes every repaint one keystroke
        behind, so the clear glyph never appears until you type the NEXT
        character, and the placeholder comes back while there is text on
        screen. The Entry is only the fallback for a field built without a
        variable.
        """
        if self._var is not None:
            try:
                return self._var.get()
            except Exception:
                pass
        try:
            return self.entry.get()
        except Exception:
            return ""

    # ---- placeholder / clear ------------------------------------------ #

    def _on_text(self, *_a):
        text = self.text()
        if text == self._last:
            return
        self._last = text
        self._draw()
        if self._on_change is not None:
            try:
                self._on_change(text)
            except Exception:
                pass

    def _focused(self):
        try:
            return self.entry.focus_get() is self.entry
        except Exception:
            return False

    # ---- painting ----------------------------------------------------- #

    def _draw(self, focused=False):
        """The inherited rounded plate, plus the three search affordances."""
        try:
            w, h = self.winfo_width(), self.winfo_height()
            if w < 12 or h < 12:
                return
            super()._draw(focused)
            cv = self._cv
            mid = h / 2.0
            cx, cy, r = _GLYPH_CX, mid - 3.0, _GLYPH_R
            cy = mid - 2.0
            cv.create_oval(cx - r, cy - r, cx + r, cy + r,
                           outline=COLORS["subtext"], width=2)
            cv.create_line(cx + r * 0.72, cy + r * 0.72, cx + r + 5,
                           cy + r + 5, fill=COLORS["subtext"], width=2,
                           capstyle="round")
            text = self.text()
            if text:
                cv.create_text(self._clear_cx(), mid, text=_CLEAR,
                               font=TYPE["label_medium"],
                               fill=(COLORS["text"] if self._clear_hover
                                     else COLORS["subtext"]),
                               anchor="center", tags="clr")
            elif not self._focused():
                cv.create_text(self.GLYPH_W + 2, mid, text=self._ph,
                               font=TYPE["body_large"], fill=COLORS["subtext"],
                               anchor="w", tags="ph")
        except Exception:
            pass

    def _clear_cx(self):
        try:
            return self.winfo_width() - 6 - self.CLEAR_W / 2.0 + 4
        except Exception:
            return 0

    def _set_clear_hover(self, on):
        if self._clear_hover == bool(on):
            return
        self._clear_hover = bool(on)
        self._draw()

    # ---- hit testing -------------------------------------------------- #

    def _clear_hit(self, x, y):
        try:
            h = self.winfo_height()
            return (abs(x - self._clear_cx()) <= self.CLEAR_W / 2.0
                    and abs(y - h / 2.0) <= h / 2.0)
        except Exception:
            return False

    def _on_motion(self, e):
        self._set_clear_hover(self._clear_hit(e.x, e.y))

    def _on_canvas_click(self, e):
        if not self._clear_hit(e.x, e.y):
            return
        if self.text():
            self.clear()
        self.focus()