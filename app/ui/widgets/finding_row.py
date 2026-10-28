"""FindingRow: one problem, its evidence, and the one action that answers it.

AUDIT TV-004 (2026-10). Tools presented a result in whatever shape their
author reached for: a coloured table cell, a red status line, a paragraph
with a button underneath. The audit's finding was not that any one of those
was ugly — it was that the same fact ("this check wants attention") read as a
different kind of thing depending on the window.

A FindingRow is the shared answer for a *finding*: a severity dot, a title
that names the problem, one line of evidence the tool actually measured, and
at most one action. It is deliberately not a card (no fill, no radius); it is
a list row, so a list of findings reads as one block instead of a stack of
nested surfaces.

`severity` is limited to the four states the app already uses:
    "ok"    a check that passed (green)
    "info"  context that is neither good nor bad (muted)
    "warn"  something wants attention (amber)
    "bad"   something is broken or unsafe (red)
A tone is never chosen by a call site; it is the severity.

Depends only on app.ui.theme.
"""
from __future__ import annotations

import tkinter as tk

from app.ui.metrics import CONTROL_GAP, TEXT_NUDGE
from app.ui.theme import COLORS, SPACE_1, SPACE_2, TYPE
from app.ui.widgets.button import AnimatedButton

#: Severity -> the dot's colour. One table, so two tools cannot disagree
#: about what "warn" looks like.
SEVERITY = {
    "ok": COLORS["accent_green"],
    "good": COLORS["accent_green"],
    "info": COLORS["muted"],
    "warn": COLORS["accent_yellow"],
    "bad": COLORS["accent_red"],
}

_DOT = 8
_DOT_BOX = 14


class FindingRow(tk.Frame):
    """A quiet row: severity dot, title + evidence, one inline action."""

    def __init__(self, parent, title="", evidence="", severity="info",
                 action_label="", on_action=None, bg=None, wrap=0, **kw):
        page_bg = bg or COLORS["bg"]
        super().__init__(parent, bg=page_bg, bd=0, **kw)
        self._bg = page_bg
        self._wrap = max(120, int(wrap or 0))
        self._action = None

        dot = tk.Canvas(self, width=_DOT_BOX, height=_DOT_BOX, bg=page_bg,
                        highlightthickness=0, bd=0)
        dot.pack(side="left", anchor="n", pady=TEXT_NUDGE)
        self._dot = dot
        self._dot_color = SEVERITY.get(str(severity).lower(), SEVERITY["info"])
        self._draw_dot()

        col = tk.Frame(self, bg=page_bg, bd=0)
        col.pack(side="left", fill="x", expand=True, padx=(CONTROL_GAP, 0))
        self.title_lbl = tk.Label(
            col, text=str(title or ""), font=TYPE["card_title"], bg=page_bg,
            fg=COLORS["text"], anchor="w", justify="left", bd=0)
        self.title_lbl.pack(fill="x")
        self.evidence_lbl = tk.Label(
            col, text=str(evidence or ""), font=TYPE["caption"], bg=page_bg,
            fg=COLORS["subtext"], anchor="w", justify="left", bd=0,
            wraplength=self._wrap)
        if evidence:
            self.evidence_lbl.pack(fill="x", pady=(TEXT_NUDGE, 0))

        if action_label and on_action is not None:
            self._action = AnimatedButton(
                self, text=str(action_label), command=on_action,
                role="secondary", size="sm")
            self._action.pack(side="right", anchor="n",
                              padx=(SPACE_2, 0), pady=TEXT_NUDGE)

        self.bind("<Configure>", self._fit, add="+")

    # ---- paint --------------------------------------------------------- #
    def _draw_dot(self):
        try:
            self._dot.delete("all")
            self._dot.create_oval(2, 2, 2 + _DOT, 2 + _DOT,
                                  fill=self._dot_color, outline="")
        except Exception:
            pass

    def _fit(self, event=None):
        try:
            width = int(event.width) if event is not None else int(
                self.winfo_width())
            if width <= 1:
                return
            reserve = (_DOT_BOX + CONTROL_GAP + SPACE_1
                       + (self._action.winfo_reqwidth() if self._action else 0)
                       + SPACE_2)
            room = max(120, width - reserve)
            if int(self.evidence_lbl.cget("wraplength")) != room:
                self.evidence_lbl.config(wraplength=room)
        except Exception:
            pass

    # ---- api ----------------------------------------------------------- #

    def set_title(self, title):
        try:
            self.title_lbl.config(text=str(title or ""))
        except Exception:
            pass


    def set_action_enabled(self, enabled):
        if self._action is not None:
            try:
                self._action.set_enabled(bool(enabled))
            except Exception:
                pass


    def action(self):
        return self._action
