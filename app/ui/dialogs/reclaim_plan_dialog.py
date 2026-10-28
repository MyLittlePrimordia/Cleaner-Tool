"""ReclaimPlanDialog — N-002: one ordered list across the tools.

Storage Insight already answers "how big is the junk", but the user's actual
question is "what should I do, in what order". This window is that answer: the
categories that walk measured, biggest first, each with the one action that
frees it (Clean runs those tasks through the normal engine), followed by the
decisions no disk walk can measure - duplicates, unused programs, the driver
store - each opening the tool that owns it.

Deliberately not a second scanner. It is handed a plan built by
`app.reclaim.build_plan` from numbers already measured, so opening it costs
nothing and cannot disagree with the page that launched it. A step with no
measurement says so; a step with no action is not drawn.
"""
from __future__ import annotations

from app.ui.dialogs._tool_page import ToolPage
from app.ui.metrics import PAGE_INSET
from app.ui.theme import COLORS, SPACE_2, TAB_ACCENTS
from app.ui.widgets import EmptyState, FindingRow

_SEV = {"ok": "info", "warn": "warn", "bad": "bad", "info": "info"}


class ReclaimPlanDialog(ToolPage):
    """The ordered plan: measured wins first, then the tools that need a look."""

    def __init__(self, parent, app, plan=None):
        from app import reclaim

        self.app = app
        self._plan = plan or {"steps": [], "measured_total": 0, "partial": False}
        self._rows = []
        accent = TAB_ACCENTS["Clean"]
        # fit=True: the step count is not fixed (five measured categories plus
        # three decisions, more on a fuller machine), so the list has to scroll
        # rather than be cut at the panel edge on a small window or a 200% DPI
        # display.
        super().__init__(parent, title="Reclaim plan", accent=accent,
                         scope="Nothing runs until you pick a step.",
                         fit=True)
        body = self.content
        self.banner.set_state("\U0001F9F9", accent)
        self.banner.set_title(reclaim.headline(self._plan),
                              COLORS["accent_green"])
        steps = self._plan.get("steps") or []
        if self._plan.get("partial"):
            self.banner.set_sub("Measured so far, biggest first. A '?' means "
                                "one cleaner declined to look.")
        else:
            self.banner.set_sub("%d step%s, biggest first."
                                % (len(steps), "" if len(steps) == 1 else "s"))

        self.add_button("Close", self.close, kind="primary")

        if not steps:
            EmptyState(body, glyph="\U0001F31E",
                       text="Nothing measured to plan around.",
                       wrap=520).pack(fill="x", pady=(PAGE_INSET, 0))
            return
        for step in steps:
            self._rows.append(self._build_step(body, step))

    def _build_step(self, parent, step):
        keys = _runnable(step)
        target = str(step.get("target") or "")
        label = str(step.get("action") or "")
        action = None
        if keys:
            action = lambda s=step: self._run_step(s)          # noqa: E731
        elif target:
            action = lambda t=target: self._open_target(t)     # noqa: E731
        row = FindingRow(
            parent, title=str(step.get("title") or ""),
            evidence=str(step.get("detail") or ""),
            severity=_SEV.get(str(step.get("severity") or ""), "info"),
            action_label=label, on_action=action,
            wrap=max(240, self.body_width(60)))
        row.pack(fill="x", pady=(SPACE_2, 0))
        row._step = step
        return row

    # ---- actions --------------------------------------------------------- #
    def _run_step(self, step):
        """Run this step's Clean tasks through the ordinary engine."""
        keys = _runnable(step)
        try:
            from app.tab_presets import TABS
            by_key = {t.key: t for t in TABS.get("Clean", [])}
            tasks = [by_key[k] for k in keys if k in by_key]
        except Exception:
            tasks = []
        if not tasks:
            try:
                self.status.stop("Those cleaners are not on the Clean tab.",
                                 COLORS["accent_yellow"])
            except Exception:
                pass
            return
        self.close()
        try:
            self.app.run_tasks("Clean", tasks, mode="run")
        except Exception:
            pass

    def _open_target(self, target):
        try:
            from app.routes import open_route
        except Exception:
            return
        self.close()
        try:
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


def _runnable(step) -> list:
    """Clean task keys this step can run directly ([] when it only opens)."""
    if not isinstance(step, dict) or step.get("target"):
        return []
    return [str(k) for k in (step.get("keys") or ()) if str(k)]


__all__ = ["ReclaimPlanDialog"]
