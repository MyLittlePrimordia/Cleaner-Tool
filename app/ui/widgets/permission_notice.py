"""PermissionNotice: the one way a tool says "this needs administrator rights".

AUDIT TV-010 (2026-10). Elevation was handled three different ways: File Lock
put a warning button in its footer, Restore Points wrote a line of status
text, and several tools opened a relaunch prompt. A user could not tell
whether "Restart as administrator" was advice, a requirement, or a button
they had already missed.

This notice is the shared pattern: a lock glyph, the fixed title "Needs
administrator", one sentence saying what is hidden or blocked, and at most
one action that asks for the rights. The title is fixed on purpose — a
recognisable heading is what makes the same situation read the same way in
every tool. The reason is the tool's own, because that is the part that is
actually different.

It is a tonal surface, not a dialog and not a card in the content flow: it
appears only in the state that needs it and disappears once the elevated
result lands. `set_enabled(False)` while the UAC prompt is open, so the
button cannot be pressed twice.

Depends only on app.ui.theme.
"""
from __future__ import annotations

import tkinter as tk

from app.ui.metrics import CHIP_PAD, CONTROL_GAP
from app.ui.theme import (
    COLORS, EMOJI, RADIUS_CONTROL, SPACE_2, TYPE, _hex_lerp,
)
from app.ui.widgets.button import AnimatedButton
from app.ui.widgets.rounded_card import RoundedCard

_TITLE = "Needs administrator"
_REASON = "Windows is hiding part of this result from a normal user."

#: A-008. A refused request used to leave this notice exactly as it was, so
#: the same button that had just failed looked like it had never been tried.
#: The refused state says what happened and turns the control into a retry.
_REFUSED_TITLE = "Administrator rights were refused"
_REFUSED_REASON = ("Windows did not grant the request, so this can only show "
                   "what a normal scan sees.")


class PermissionNotice(RoundedCard):
    """Lock glyph, fixed title, one reason, one elevation action."""

    def __init__(self, parent, reason=_REASON, action_text="Restart as admin",
                 on_action=None, bg=None, accent=None, **kw):
        page_bg = bg or (parent["bg"] if isinstance(
            parent, (tk.Frame, tk.Canvas)) else COLORS["bg"])
        self._accent = accent or COLORS["accent_yellow"]
        fill = _hex_lerp(page_bg, self._accent, 0.14)
        super().__init__(parent, fill=fill, bg=page_bg,
                         radius=RADIUS_CONTROL, **kw)
        self._fill = fill
        inner = tk.Frame(self.body, bg=fill, bd=0)
        inner.pack(fill="x", padx=CHIP_PAD, pady=CONTROL_GAP)

        glyph = tk.Label(inner, text="\U0001F512", font=EMOJI["badge"],
                         bg=fill, fg=self._accent, bd=0)
        glyph.pack(side="left", anchor="n", padx=(0, CONTROL_GAP))

        self._col = tk.Frame(inner, bg=fill, bd=0)
        self._col.pack(side="left", fill="x", expand=True)
        self.title_lbl = tk.Label(
            self._col, text=_TITLE, font=TYPE["label_large"], bg=fill,
            fg=self._accent, anchor="w", bd=0)
        self.title_lbl.pack(fill="x")
        self.reason_lbl = tk.Label(
            self._col, text=str(reason or _REASON), font=TYPE["caption"],
            bg=fill, fg=COLORS["subtext"], anchor="w", justify="left",
            wraplength=420, bd=0)
        self.reason_lbl.pack(fill="x", pady=(SPACE_2, 0))

        # The disclosure is created hidden; it appears only after a refusal,
        # because before that there is nothing that failed to explain.
        self._why_link = tk.Label(
            self._col, text="Why?", font=TYPE["caption"], bg=fill,
            fg=COLORS["subtext"], anchor="w", cursor="hand2", bd=0)
        self._why_text = tk.Label(
            self._col, text="", font=TYPE["caption"], bg=fill,
            fg=COLORS["muted"], anchor="w", justify="left",
            wraplength=420, bd=0)
        self._why_link.bind("<Button-1>", lambda _e: self._toggle_why())

        self._action = None
        self._on_action = on_action
        self._action_text = str(action_text or "")
        if on_action is not None:
            self._action = AnimatedButton(
                inner, text=action_text, command=on_action, role="warn",
                size="sm")
            self._action.pack(side="right", anchor="n",
                              padx=(SPACE_2, 0))
        self._col.bind("<Configure>", self._fit, add="+")

    def _fit(self, event=None):
        try:
            width = int(event.width) if event is not None else int(
                self._col.winfo_width())
            if width > 40 and int(self.reason_lbl.cget("wraplength")) != width:
                self.reason_lbl.config(wraplength=width)
        except Exception:
            pass

    # ---- api ----------------------------------------------------------- #

    def set_refused(self, why=""):
        """A-008: record a refusal instead of repeating the invitation.

        `why` is the exact command the tool wanted to run, shown behind the
        disclosure. The action becomes a visible retry: a control that has
        already failed must never look identical to one that has not.
        """
        try:
            self.title_lbl.config(text=_REFUSED_TITLE,
                                  fg=COLORS["accent_red"])
            self.reason_lbl.config(text=_REFUSED_REASON)
        except Exception:
            pass
        why = str(why or "").strip()
        self._why = why
        try:
            if why:
                if not self._why_link.winfo_manager():
                    self._why_link.pack(fill="x", pady=(SPACE_2, 0))
            else:
                self._why_link.pack_forget()
                self._why_text.pack_forget()
        except Exception:
            pass
        if self._action is not None and self._on_action is not None:
            try:
                self._action.config_text("Try again")
            except Exception:
                pass
        self.set_enabled(True)

    def _toggle_why(self):
        try:
            if self._why_text.winfo_manager():
                self._why_text.pack_forget()
                return
            shown = self._why or "No command was recorded for this attempt."
            self._why_text.config(text="It asked Windows to run:\n%s" % shown)
            self._why_text.pack(fill="x", pady=(SPACE_2, 0))
        except Exception:
            pass

    def set_enabled(self, enabled):
        if self._action is not None:
            try:
                self._action.set_enabled(bool(enabled))
            except Exception:
                pass

    def action(self):
        return self._action
