"""ModalFooter: the one bottom bar a plain popup may have.

The five dialogs that do not build on `ToolPage` (the catalog picker, My
Setups, Past Runs, Checkup History and the Scorecard) each hand-built the
same strip: a `tk.Frame`, `side="bottom"`, 18px side padding, `(12, 10)`
vertical padding, a primary button packed right, and whatever secondary
actions the dialog had packed wherever the author happened to think of it.
Every popup now derives its content insets from `theme.DIALOG_PAD_X` /
`DIALOG_PAD_TOP` / `DIALOG_PAD_BOTTOM`, and this footer adds none of its own.

That reads as five designs until you put two next to each other:

* padding: three used `(12, 10)`, one used `(4, 2)`, one put its buttons in
  the content column and let the list scroll underneath them;
* order: the destructive action sat far left in two dialogs and immediately
  beside Close in the third, so the same "Clear History" control moved
  between windows;
* missing buttons: a secondary action that had to disappear (Clear History
  with no history) changed the primary's position in two of them.

`ModalFooter` owns that geometry, not the dialog's meaning:

    footer = ModalFooter(body, accent=ACCENT)
    footer.pack_footer()
    footer.button("Clear History", self._confirm_clear, kind="quiet")
    footer.button("Close", self.close, kind="primary")

Packing happens in the constructor in this order - quiet (far left), hint
(expanding), primary (far right), then secondary - so a secondary appearing
or disappearing never moves the primary. That is the same rule
`PageActionBar` follows for the pages, applied to the popups.

It reserves its own space with `side="bottom"` BEFORE the dialog's growing
content is built, so a long list shrinks and scrolls instead of pushing the
buttons off the card.

Depends only on app.ui.theme and AnimatedButton.
"""
from __future__ import annotations

import tkinter as tk

from app.ui.theme import COLORS, DIALOG_PAD_TOP, SPACE_2, TYPE
from app.ui.widgets.button import AnimatedButton

#: The footer adds NO side padding of its own: `ThemedModal.body` already
#: pads by `theme.DIALOG_PAD_X` inside the card and ends
#: `theme.DIALOG_PAD_BOTTOM` above its bottom edge. A footer that padded
#: itself again would sit 32px from the card edge while the banner above it
#: sat at 16 - exactly the double-inset the five hand-built strips had.
MODAL_INSET = 0

#: One stack gap above the controls, none below: the body's bottom inset is
#: the card's bottom margin. Same rhythm as every page footer.
MODAL_FOOT_PAD = (DIALOG_PAD_TOP, 0)

#: `kind` -> the shared button role. The same vocabulary ToolPage uses, so
#: a footer button and a page button at the same rung look identical.
KIND_ROLE = {
    "primary": "filled",
    "secondary": "secondary",
    "danger": "danger",
    "warn": "warn",
    "text": "text",
    "quiet": "secondary",
}


class ModalFooter(tk.Frame):
    """Status/hint on the left, primary pinned right, secondaries beside it."""

    def __init__(self, parent, accent=None, inset=MODAL_INSET,
                 pad=MODAL_FOOT_PAD, bg=None):
        page_bg = bg or COLORS["bg"]
        super().__init__(parent, bg=page_bg, bd=0)
        self._page_bg = page_bg
        self.accent = accent or COLORS["primary"]
        self._inset = int(inset)
        self._pad = pad
        self._hint = None

        # Packed in reading order; see the module docstring for why the
        # primary is packed before the secondary cluster.
        self.quiet_slot = tk.Frame(self, bg=page_bg)
        self.quiet_slot.pack(side="left")
        self.hint_slot = tk.Frame(self, bg=page_bg)
        self.hint_slot.pack(side="left", fill="x", expand=True,
                            padx=(SPACE_2, 0))
        self.primary_slot = tk.Frame(self, bg=page_bg)
        self.primary_slot.pack(side="right", padx=(SPACE_2, 0))
        self.secondary_slot = tk.Frame(self, bg=page_bg)
        self.secondary_slot.pack(side="right")

    # ---- placement --------------------------------------------------- #
    def pack_footer(self, **kw):
        """Reserve the strip at the dialog's bottom with the shared insets."""
        kw.setdefault("side", "bottom")
        kw.setdefault("fill", "x")
        kw.setdefault("padx", self._inset)
        kw.setdefault("pady", self._pad)
        self.pack(**kw)
        return self

    def set_hint(self, text, color=None):
        """One quiet caption on the left; created on first use.

        A footer that carries a status line (ToolPage's does) supplies its
        own widget instead; this is for the plain dialogs that only need a
        sentence.
        """
        if self._hint is None:
            self._hint = tk.Label(
                self.hint_slot, text="", font=TYPE["caption"],
                bg=self._page_bg, fg=COLORS["subtext"], anchor="w")
            self._hint.pack(side="left", fill="x", expand=True)
        self._hint.config(text=str(text or ""),
                          fg=color or COLORS["subtext"])
        return self._hint

    # ---- buttons ----------------------------------------------------- #
    def button(self, text, command, kind="secondary", side=None, **kw):
        """Add a footer button and return it.

        `side` overrides the cluster: "quiet" puts it on the far left (the
        position every destructive action in this app uses), "secondary"
        or "primary" force the right-hand clusters. Left to itself, a
        `primary`-kind button takes the right-most slot and everything else
        sits immediately to its left.
        """
        key = str(kind or "secondary").lower()
        role = KIND_ROLE.get(key, "secondary")
        if role == "filled":
            kw.setdefault("accent", self.accent)
        slot_name = side or ("primary" if key == "primary" else
                             "quiet" if key == "quiet" else "secondary")
        slot = getattr(self, slot_name + "_slot", self.secondary_slot)
        btn = AnimatedButton(slot, text=text, command=command, role=role,
                             size="md", **kw)
        # Right-cluster buttons stack outward from the primary: first packed
        # stays nearest the edge, so each new secondary lands to its left.
        btn.pack(side="right" if slot is not self.quiet_slot else "left",
                 padx=((SPACE_2, 0) if slot is not self.quiet_slot
                       else (0, SPACE_2)))
        return btn
