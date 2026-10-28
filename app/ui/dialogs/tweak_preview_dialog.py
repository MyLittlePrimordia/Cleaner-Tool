"""TweakPreviewDialog — the Tweak tab's "Preview changes" popup (roadmap 2.3).

Renders the read-only list produced by `app.tweak_introspect.preview_lines()`:
for each selected tweak, the registry values it will touch and their current
settings. Nothing here applies anything — the list is built from a static parse
of the task source plus read-only registry reads.

Kept deliberately small and failure-tolerant: a preview must never be the thing
that stops a run, so every failure path degrades to a plain messagebox and then
to a silent no-op.
"""
from __future__ import annotations

import tkinter as tk
from tkinter import messagebox
from tkinter.scrolledtext import ScrolledText

from app.ui.base import ThemedModal
from app.ui.theme import COLORS, TYPE, SPACE_1, SPACE_2
from app.ui.metrics import TEXT_NUDGE
from app.ui.widgets import AnimatedButton

_SUBTITLE = ("These are the Windows settings the selected tweaks will change. "
             "Nothing has been changed yet.")


def show_tweak_preview(parent, lines, *, title="Preview changes",
                       subtitle=_SUBTITLE, accent=None) -> bool:
    """Open the read-only preview popup. Returns True if it was shown."""
    text = "\n".join(lines) if lines else "Nothing selected to preview."
    try:
        modal = ThemedModal(parent, title=title, accent=accent,
                            size=(760, 560))
        body = modal.body
        if subtitle:
            tk.Label(body, text=subtitle, font=TYPE["caption"], bg=COLORS["bg"],
                     fg=COLORS["subtext"],
                     wraplength=modal.body_width(24),
                     justify="left").pack(fill="x", pady=(TEXT_NUDGE, SPACE_2))
        txt = ScrolledText(body, wrap="none", font=TYPE["mono"],
                           bg=COLORS["surface"], fg=COLORS["text"],
                           relief="flat", height=18,
                           insertbackground=COLORS["text"],
                           highlightthickness=0, bd=0)
        txt.pack(fill="both", expand=True)
        # ScrolledText ships a classic (light) scrollbar; recolour it for the
        # dark theme so the app's one scrolling text surface stays consistent.
        try:
            txt.vbar.config(bg=COLORS["surface_hover"],
                            troughcolor=COLORS["bg"],
                            activebackground=COLORS["primary"],
                            highlightthickness=0, bd=0, relief="flat",
                            width=12)
        except Exception:
            pass
        txt.insert("1.0", text)
        txt.config(state="disabled")
        brow = tk.Frame(body, bg=COLORS["bg"])
        brow.pack(fill="x", pady=(SPACE_2, SPACE_1))
        AnimatedButton(brow, text="Close", command=modal.close,
                       role="filled", size="md",
                       accent=accent or COLORS["primary"]).pack(side="right")
        modal._dlg.bind("<Return>", lambda _e: modal.close())
        modal.wait()
        return True
    except Exception:
        try:
            messagebox.showinfo(title, text)
            return True
        except Exception:
            return False
