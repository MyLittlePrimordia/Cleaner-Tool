"""EmptyState: the one way a Tools popup says "nothing here".

Six dialogs shipped six copies of the same grey paragraph, several of them
three lines long. A newcomer opening a tool and seeing a paragraph has no
idea whether it is a failure, a filter that matched nothing, or genuinely
nothing to report.

EmptyState is the shared answer: a glyph, ONE sentence naming the cause,
and at most ONE button for the next action. If a message needs two
sentences, it is not an empty state.

Depends only on app.ui.theme.
"""
from __future__ import annotations

import tkinter as tk

from app.ui.theme import COLORS, F


class EmptyState(tk.Frame):
    """Centred glyph + one line + one optional action.

    The action is added with .add_button() rather than passed in as a built
    widget. A caller who constructs the AnimatedButton first and hands it
    over gets a subtle and very visible bug: pack() puts a widget into its
    OWN master, not into whichever widget asked for it, so the button lands
    as a SIBLING of the empty state - outside the centred column, in the
    wrong place, at the wrong z-order - and looks like the empty state
    forgot it had a button. Owning the construction makes that impossible.
    """

    GLYPH_FONT = (F, 22)

    #: Fallback wrap width when the caller cannot supply one.
    #:
    #: REDESIGN 2026-10 (audit D3): both labels used to hardcode
    #: wraplength=460. These six dialogs are PROPORTIONAL (720-1120px), and
    #: ToolShell exists precisely to replace hardcoded wraplengths with
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

        if glyph:
            # "muted", not "hairline": this glyph is a FOREGROUND, and
            # hairline is now reserved for borders. On the true-black canvas a
            # #242424 glyph on a #0D0D0D page is invisible, which would delete
            # the one cue that says "there is genuinely nothing here".
            tk.Label(self, text=str(glyph), font=self.GLYPH_FONT,
                     bg=self._page_bg, fg=accent or COLORS["muted"],
                     bd=0).pack(pady=(28, 6))

        tk.Label(self, text=str(text or ""), font=(F, 9, "bold"),
                 bg=self._page_bg, fg=COLORS["text"], anchor="center",
                 justify="center", wraplength=self._wrap, bd=0).pack(
                     fill="x", padx=24)

        if detail:
            tk.Label(self, text=str(detail), font=(F, 8),
                     bg=self._page_bg, fg=COLORS["subtext"], anchor="center",
                     justify="center", wraplength=self._wrap, bd=0).pack(
                         fill="x", padx=24, pady=(4, 0))

    def add_button(self, text, command, accent=None, **btn_kw):
        """The one next action. Returns the button, or None on failure."""
        try:
            from app.ui.widgets.button import AnimatedButton
            btn = AnimatedButton(
                self, text=text, command=command,
                bg=accent or COLORS["surface"],
                fg=COLORS["black"] if (accent
                                       or COLORS["surface_hover"]) else
                COLORS["text"],
                font=(F, 9, "bold"), **btn_kw)
            btn.pack(pady=(14, 24))
            return btn
        except Exception:
            return None