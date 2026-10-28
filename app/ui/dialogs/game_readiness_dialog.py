"""GameReadinessDialog — "is this PC set up to play?"

Backed by app/game_readiness.py (read-only). Seven checks, one line each,
and exactly one thing to do per row. Every fix is the EXISTING tweak task,
run through the normal engine, so it snaps a prior value first and can be
undone from Safety & Undo like any other tweak — this dialog never writes a
registry value itself.

Two rows deliberately do not fix in place: the graphics driver (the Drivers
tab owns installs/verification) and low disk space (Storage Insight shows
what is safe to delete). Both close this dialog and open the right one.
"""
from __future__ import annotations

import tkinter as tk

from app.ui.dialogs._tool_page import ToolPage
from app.ui.theme import COLORS, SPACE_1
from app.ui.widgets import EmptyState, FindingRow


def _gather():
    """Worker body: the engine call, with no Tk access at all."""
    from app.game_readiness import check, publish_findings
    rows = check()
    # The bus write belongs here, off the Tk thread: it persists to config and
    # must never delay the list painting.
    try:
        publish_findings(rows)
    except Exception:
        pass
    return rows


#: Engine state -> FindingRow severity. One mapping, so the dot in this list
#: and every other finding list mean the same thing.
_SEVERITY = {
    "ok": "ok",
    "warn": "warn",
    "bad": "bad",
    "info": "info",
}


class GameReadinessDialog(ToolPage):
    def __init__(self, parent, app):
        self.app = app
        self._rows: list[dict] = []
        self._busy = False
        super().__init__(
            parent, title="Game Readiness",
            scope="Read-only checks. Every fix is a normal tweak with its own "
                  "undo in Safety & Undo.", scroll=True)

        # AUDIT TV-004/TV-010: the seven checks are FINDINGS, not table rows.
        # Each carries its own severity dot, its evidence and exactly one
        # action, so fixing one no longer means selecting it and then finding
        # a footer button that renames itself (the old select-then-fix flow).
        self._list = tk.Frame(self.content, bg=COLORS["bg"], bd=0)
        self._list.pack(fill="x")
        self._row_widgets = []

        self._recheck_btn = self.add_button("Check again", self._refresh)

        self.banner.set_state("…", COLORS["muted"])
        self.banner.set_title("Checking your setup…")
        self._refresh()

    # ------------------------------------------------------------ helpers
    def set_busy(self, busy):
        self._busy = bool(busy)
        try:
            self._recheck_btn.set_enabled(not self._busy)
        except Exception:
            pass
        for row in getattr(self, "_row_widgets", ()):
            row.set_action_enabled(not self._busy)

    # --------------------------------------------------------------- read
    def _refresh(self):
        """Read the seven checks OFF the Tk thread.

        AUDIT TL-001: this used to call `check()` inline from __init__ - a
        `powercfg` call with a 15 s timeout plus a PowerShell CIM query with a
        45 s timeout - so the window painted late and the whole app looked
        hung while PowerShell started. Every other read-only tool already
        uses run_worker; this was the outlier.
        """
        if self._busy:
            return
        self.set_busy(True)
        self.banner.set_state("…", COLORS["muted"])
        self.banner.set_title("Checking your setup…")
        self.status.start("Reading the power plan, driver, display and disk…")
        self.run_worker(_gather, self._land)

    def _land(self, rows):
        from app.game_readiness import summary
        self._rows = rows
        self._render()

        warns, total = summary(rows)
        if warns:
            self.banner.set_state("!", COLORS["accent_yellow"])
            self.banner.set_title("%d of %d checks want attention." % (warns,
                                                                      total))
            self.banner.set_sub("Select a row, then fix it here or open the "
                                "screen that owns it.")
        else:
            self.banner.set_state("✓", COLORS["accent_green"])
            self.banner.set_title("All %d checks look good." % total)
            self.banner.set_sub("Nothing needs changing to play.")
        self.status.stop("Checked %d settings." % total,
                         COLORS["accent_green"])
        self.set_busy(False)

    def _worker_failed(self, exc):
        """A failed read must leave an honest banner, not "Checking…" forever."""
        super()._worker_failed(exc)
        try:
            self.banner.set_state("!", COLORS["accent_yellow"])
            self.banner.set_title("The checks could not run.")
            self.banner.set_sub(str(exc) or "Windows did not answer.")
        except Exception:
            pass

    # ------------------------------------------------------------ list
    def _render(self):
        """Paint the findings list; an empty answer says so."""
        for w in list(self._list.winfo_children()):
            try:
                w.destroy()
            except Exception:
                pass
        self._row_widgets = []
        if not self._rows:
            EmptyState(self._list, glyph="✓", text="Nothing to check.",
                       detail="Windows reported no readiness checks.",
                       wrap=self.body_width()).pack(fill="x")
            return
        for r in self._rows:
            fix = r.get("fix")
            label = self._action_label(r)
            row = FindingRow(
                self._list, title=r.get("label") or "?",
                evidence=r.get("detail") or "",
                severity=_SEVERITY.get(r.get("state"), "info"),
                action_label=label,
                on_action=(lambda r=r: self._run_fix(r)) if fix else None,
                wrap=self.body_width(82))
            row.pack(fill="x", pady=(SPACE_1, SPACE_1))
            self._row_widgets.append(row)
        self.set_busy(self._busy)

    @staticmethod
    def _action_label(row):
        if not row.get("fix"):
            return ""
        if row.get("fix") == "drivers_tab":
            return "Open Drivers"
        if row.get("fix") == "storage_insight":
            return "Open Storage"
        return row.get("fix_label") or "Fix"

    # ------------------------------------------------------------ actions
    def _run_fix(self, row):
        if not row or not row.get("fix") or self._busy:
            return
        fix = row["fix"]
        if fix == "drivers_tab":
            self.close()
            try:
                self.app._show_tab("Drivers")
            except Exception:
                pass
            return
        if fix == "storage_insight":
            self.close()
            try:
                self.app._open_storage_insight()
            except Exception:
                pass
            return
        try:
            from app.tab_presets import TABS
            task = next(t for t in TABS.get("Tweak", []) if t.key == fix)
        except Exception:
            self.status.stop("That fix is not available in this build.",
                             COLORS["accent_yellow"])
            return
        self.close()
        try:
            self.app.run_tasks("Tweak", [task], mode="run")
        except Exception:
            pass
