"""TweakSnapshotsDialog — the per-row "Applied tweaks" browser (roadmap 2.1).

The counterpart to Undo-All: instead of one "put everything back" button, this
lists what is CURRENTLY applied, one card per tweak, with the recorded
prior -> current value for every registry value that tweak touched, and an
**Undo** button on each row so the user can put back just one tweak. A value
Windows has since reset is highlighted (that is the same drift the Tweak tab's
Health view reports as "Reinforce").

This dialog owns no registry logic. It renders the groups from
`app.tweak_center.applied_groups()` (Tk-free, tested) and, when a row's Undo is
clicked, hands the resolved Task list back to the caller via `on_undo` — the
actual revert still runs through the normal task runner, exactly like the
Tweak tab's Undo.

Deliberately failure-tolerant: a browser must never be the thing that stops the
user, so every failure path degrades to a plain messagebox and then to a no-op.
"""
from __future__ import annotations

import tkinter as tk
from tkinter import messagebox

from app.ui.base import ThemedModal
from app.ui.theme import COLORS, RADIUS_SURFACE, TYPE, SPACE_1, SPACE_2, SPACE_3
from app.ui.metrics import CARD_BODY_INSET, GROUP_GAP, TEXT_NUDGE
from app.ui.widgets import AnimatedButton, ScrollableRoundedPanel
from app.ui.widgets.rounded_card import RoundedCard

_SUBTITLE = ("These tweaks are currently applied. On each row, Undo puts that "
             "one tweak back to the exact value recorded when it ran.")


def show_tweak_snapshots(parent, groups, on_undo, *, accent=None) -> bool:
    """Open the applied-tweaks browser.

    groups   : list from tweak_center.applied_groups().
    on_undo  : called with a list of Task objects (one row) or every group
               ("Undo all"); the dialog is closed first.
    Returns True if it was shown."""
    try:
        from app import tweak_center
    except Exception:
        tweak_center = None

    try:
        modal = ThemedModal(parent, title="Applied tweaks", accent=accent,
                            size=(780, 580))
        body = modal.body
        tk.Label(body, text=_SUBTITLE, font=TYPE["caption"], bg=COLORS["bg"],
                 fg=COLORS["subtext"], wraplength=modal.body_width(24),
                 justify="left").pack(fill="x", pady=(TEXT_NUDGE, SPACE_2))

        def _undo_keys(keys):
            tasks = []
            if tweak_center is not None:
                for k in keys:
                    t = tweak_center.task_for_key(k)
                    if t is not None:
                        tasks.append(t)
            try:
                modal.close()
            except Exception:
                pass
            if tasks and on_undo is not None:
                try:
                    on_undo(tasks)
                except Exception:
                    pass

        panel = ScrollableRoundedPanel(body)
        panel.pack(fill="both", expand=True)
        inner = panel.inner

        for g in groups:
            card = RoundedCard(inner, fill=COLORS["bg_alt"],
                               bg=COLORS["bg"], radius=RADIUS_SURFACE)
            card.pack(fill="x", pady=(0, GROUP_GAP))
            hdr = tk.Frame(card.body, bg=COLORS["bg_alt"])
            hdr.pack(fill="x", padx=CARD_BODY_INSET, pady=(GROUP_GAP, TEXT_NUDGE))
            tk.Label(hdr, text=g.get("label", g.get("task", "")),
                     font=TYPE["card_title"], bg=COLORS["bg_alt"],
                     fg=COLORS["text"], anchor="w").pack(side="left")
            AnimatedButton(
                hdr, text="Undo",
                command=lambda k=g.get("task"): _undo_keys([k]),
                role="danger", size="sm").pack(side="right")

            rows = g.get("rows") or []
            if not rows:
                tk.Label(
                    card.body,
                    text="Changes Windows settings (no registry detail "
                         "recorded).",
                    font=TYPE["caption"], bg=COLORS["bg_alt"], fg=COLORS["subtext"],
                    anchor="w", justify="left",
                    wraplength=modal.body_width(80)
                ).pack(fill="x", padx=CARD_BODY_INSET, pady=(TEXT_NUDGE, SPACE_3))
                continue

            for r in rows:
                line = tk.Frame(card.body, bg=COLORS["bg_alt"])
                line.pack(fill="x", padx=CARD_BODY_INSET, pady=(TEXT_NUDGE, 0))
                tk.Label(
                    line, text="%s  \u2192  %s" % (r["prior"], r["current"]),
                    font=TYPE["body_medium"], bg=COLORS["bg_alt"],
                    fg=COLORS["accent_yellow"] if r["changed"]
                    else COLORS["text"], anchor="w"
                ).pack(side="left")
                if r["changed"]:
                    tk.Label(line, text="changed since", font=TYPE["micro_regular"],
                             bg=COLORS["bg_alt"], fg=COLORS["accent_yellow"],
                             anchor="w").pack(side="left", padx=(SPACE_2, 0))
                tk.Label(card.body,
                         text="%s\\%s\\%s" % (r["hive"], r["path"], r["name"]),
                         font=TYPE["mono_small"], bg=COLORS["bg_alt"],
                         fg=COLORS["muted"], anchor="w"
                         ).pack(fill="x", padx=CARD_BODY_INSET, pady=(0, TEXT_NUDGE))
            # a little breathing room under the last row of the card
            tk.Frame(card.body, bg=COLORS["bg_alt"], height=8).pack(fill="x")

        frow = tk.Frame(body, bg=COLORS["bg"])
        frow.pack(fill="x", pady=(SPACE_2, SPACE_1))
        AnimatedButton(
            frow, text="Undo all",
            command=lambda: _undo_keys([g.get("task") for g in groups]),
            role="filled", size="md", accent=COLORS["accent_red"]).pack(
                side="left")
        AnimatedButton(
            frow, text="Close", command=modal.close,
            role="filled", size="md",
            accent=accent or COLORS["primary"]).pack(side="right")
        modal._dlg.bind("<Return>", lambda _e: modal.close())
        try:
            panel.refresh_scroll()
        except Exception:
            pass
        modal.wait()
        return True
    except Exception:
        try:
            lines = []
            for g in groups:
                lines.append(g.get("label", g.get("task", "")))
                for r in g.get("rows") or []:
                    lines.append("    %s -> %s  (%s\\%s\\%s)"
                                 % (r["prior"], r["current"], r["hive"],
                                    r["path"], r["name"]))
            messagebox.showinfo("Applied tweaks", "\n".join(lines))
            return True
        except Exception:
            return False
