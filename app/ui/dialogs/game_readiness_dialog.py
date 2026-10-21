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

from app.ui.dialogs._tool_page import TOOL_ACCENT, ToolPage
from app.ui.theme import COLORS
from app.ui.widgets import Tooltip
from app.ui.widgets.datatable import DataTable

_DOT = {
    "ok": COLORS["accent_green"],
    "warn": COLORS["accent_yellow"],
    "info": COLORS["muted"],
}

_COLUMNS = (
    {"key": "check", "title": "Check", "w": 150, "sortable": False},
    {"key": "state", "title": "Status", "w": 260, "weight": 1, "min": 180,
     "sortable": False},
)


class GameReadinessDialog(ToolPage):
    def __init__(self, parent, app):
        self.app = app
        self._rows: list[dict] = []
        self._busy = False
        super().__init__(
            parent, title="Game Readiness",
            scope="Read-only checks. Every fix is a normal tweak with its own "
                  "undo in Safety & Undo.")

        self.table = DataTable(self.content, _COLUMNS, row_h=46,
                               accent=TOOL_ACCENT, two_line=True,
                               on_select=self._on_select)
        self.table.pack(fill="both", expand=True)

        self._fix_btn = self.add_button("Fix selected", self._fix_selected,
                                        kind="primary")
        self._fix_btn.set_enabled(False)
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
        self._on_select(self.table.selected)

    def _on_select(self, row):
        fix = (row or {}).get("_cells", {}).get("check", {}).get("fix")
        try:
            self._fix_btn.set_enabled(bool(fix) and not self._busy)
            self._fix_btn.config_text(
                (row or {}).get("_cells", {}).get("check", {}).get("fix_label")
                or "Fix selected")
        except Exception:
            pass

    # --------------------------------------------------------------- read
    def _refresh(self):
        from app.game_readiness import check, summary
        try:
            rows = check()
        except Exception as exc:                       # noqa: BLE001
            rows = []
            self.status.stop("Could not run the checks: %s" % exc,
                             COLORS["accent_red"])
        self._rows = rows
        out = []
        for r in rows:
            out.append({
                "_key": r.get("key") or r.get("label"),
                "_cells": {
                    "check": {"text": r.get("label") or "?",
                              "bold": True,
                              "fix": r.get("fix"),
                              "fix_label": r.get("fix_label") or ""},
                    "state": {"text": r.get("detail") or "",
                              "dot": _DOT.get(r.get("state"), COLORS["muted"])},
                }})
        self.table.set_empty("Nothing to check.", "", glyph="")
        self.table.set_rows(out)

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
        self._on_select(self.table.selected)

    # ------------------------------------------------------------ actions
    def _selected_row(self):
        row = self.table.selected
        if not row:
            return None
        key = row.get("_key")
        return next((r for r in self._rows if (r.get("key") or r.get("label")) == key),
                    None)

    def _fix_selected(self):
        row = self._selected_row()
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
