"""N-002: ReclaimGuideDialog - the plan, walked one step at a time.

Each step hands off to the tool that owns it (a Clean run or a route). The
dialog closes for that handoff, which is deliberate: the tools are modal and a
second modal grab inside this one would deadlock. What makes it a guide rather
than a list is the state it leaves behind - the steps already addressed live on
the Application, so opening it again resumes at the next open step and the
running total carries over.
"""
from __future__ import annotations

import tkinter as tk

from app import reclaim
from app.reclaim_guide import Guide
from app.ui.dialogs._tool_page import ToolPage
from app.ui.theme import (COLORS, SPACE_1, SPACE_2, SPACE_3, TAB_ACCENTS,
                          TYPE)
from app.ui.widgets import AnimatedButton
from app.ui.widgets.wrap import fit_wrap


class ReclaimGuideDialog(ToolPage):
    """One step per screen, with the running total of what is addressed."""

    def __init__(self, parent, app, plan=None):
        self.app = app
        self._plan = plan or {"steps": []}
        done = getattr(app, "_reclaim_done", None) or set()
        self._guide = Guide(self._plan.get("steps") or [], done=done)
        super().__init__(parent, title="Guided cleanup",
                         accent=TAB_ACCENTS["Clean"],
                         scope="One step at a time. Nothing runs until you "
                               "press its action.",
                         fit=True)
        self.add_button("Finish", self.close, kind="primary")
        self._body = self.content
        self._paint()

    # ---- painting ------------------------------------------------------- #
    def _paint(self):
        body = self._body
        for child in body.winfo_children():
            try:
                child.destroy()
            except Exception:
                pass
        done, total = self._guide.progress()
        self.banner.set_state("\U0001F9F9", TAB_ACCENTS["Clean"])
        if self._guide.finished():
            self.banner.set_title("That's the whole plan.",
                                  COLORS["accent_green"])
            self.banner.set_sub("%d of %d steps addressed · %s measured."
                                % (done, total,
                                   reclaim.format_bytes(
                                       self._guide.addressed_bytes())))
        else:
            self.banner.set_title("Step %d of %d"
                                  % (self._guide.index + 1, total))
            self.banner.set_sub(
                "%d addressed · %s measured so far."
                % (done, reclaim.format_bytes(
                    self._guide.addressed_bytes())))
        step = self._guide.current()
        if step is None:
            tk.Label(body, text="Nothing measured to walk through yet.",
                     font=TYPE["body_large"], bg=COLORS["bg"],
                     fg=COLORS["subtext"], anchor="w").pack(
                         fill="x", pady=(SPACE_3, 0))
            return
        card = tk.Frame(body, bg=COLORS["bg_alt"])
        card.pack(fill="x", pady=(SPACE_3, 0))
        title = tk.Label(card, text=str(step.get("title") or ""),
                         font=TYPE["body_large"], bg=COLORS["bg_alt"],
                         fg=COLORS["text"], anchor="w", justify="left",
                         wraplength=0)
        title.pack(fill="x", padx=SPACE_3, pady=(SPACE_2, 0))
        detail = tk.Label(card, text=str(step.get("detail") or ""),
                          font=TYPE["caption"], bg=COLORS["bg_alt"],
                          fg=COLORS["subtext"], anchor="w", justify="left",
                          wraplength=0)
        detail.pack(fill="x", padx=SPACE_3, pady=(SPACE_1, 0))
        fit_wrap(title, card, reserve=2 * SPACE_3)
        fit_wrap(detail, card, reserve=2 * SPACE_3)

        row = tk.Frame(body, bg=COLORS["bg"])
        row.pack(fill="x", pady=(SPACE_3, 0))
        AnimatedButton(row, text=str(step.get("action") or "Do this step"),
                       command=self._do_step, role="filled",
                       size="md").pack(side="left")
        AnimatedButton(row, text="Done, next", command=self._mark_only,
                       role="tonal", size="md").pack(side="left",
                                                     padx=(SPACE_2, 0))
        if self._guide.index > 0:
            AnimatedButton(row, text="Back", command=self._back,
                           role="tonal", size="md").pack(side="right")

    # ---- actions -------------------------------------------------------- #
    def _remember(self):
        try:
            self.app._reclaim_done = set(self._guide.done)
        except Exception:
            pass

    def _mark_only(self):
        self._guide.mark_done()
        self._remember()
        self._paint()

    def _back(self):
        self._guide.back()
        self._paint()

    def _do_step(self):
        """Mark this step addressed, then hand off to the tool that owns it.

        Closing first is not cosmetic: `routes` opens a modal that calls
        `wait()`, and a nested grab over this dialog would never return.
        """
        step = self._guide.current()
        if step is None:
            return
        keys = reclaim.runnable_keys(step)
        target = str(step.get("target") or "")
        self._guide.mark_done()
        self._remember()
        if not keys and not target:
            self._paint()
            return
        self.close()
        if keys:
            try:
                from app.tab_presets import TABS
                by_key = {t.key: t for t in TABS.get("Clean", [])}
                tasks = [by_key[k] for k in keys if k in by_key]
                if tasks:
                    self.app.run_tasks("Clean", tasks, mode="run")
            except Exception:
                pass
        elif target:
            try:
                from app.routes import open_route
                open_route(self.app, target)
            except Exception:
                pass

    def wait(self):
        try:
            self._dlg.grab_set()
        except Exception:
            pass
        try:
            self._dlg.wait_window()
        except Exception:
            pass


__all__ = ["ReclaimGuideDialog"]
