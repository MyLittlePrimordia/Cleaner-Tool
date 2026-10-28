"""What is inside one Storage Insight category (U-003).

Storage Insight answered "how much" per category and never "what". A user
looking at "Game files & caches — 16.4 GB + ?" had no way to tell which of the
ten cleaners in that group held the bytes, which one refused to look, or which
Clean-tab row to untick - they had to leave the dialog and guess.

This modal is that answer, and it is strictly a view: every number comes from
the walk the parent dialog just ran (`storage_scan.category_members`), labels
and one-line descriptions come from the live task table, and a member that
declined to look says so with its own reason instead of a 0 it never measured.
Nothing here can delete, measure again, or change a tick.
"""
from __future__ import annotations

from app.ui.dialogs._tool_page import ToolPage
from app.ui.theme import COLORS
from app.ui.widgets.datatable import DataTable
from app.utils import format_bytes

ACCENT = COLORS["accent_green"]

_COLUMNS = (
    {"key": "member", "title": "Cleaner", "w": 380, "weight": 1, "min": 200,
     "sortable": False},
    {"key": "size", "title": "Measured", "w": 100, "min": 90, "anchor": "e",
     "sortable": False},
)


class StorageCategoryDialog(ToolPage):
    """One row per cleaner in a category, with the size that walk measured."""

    def __init__(self, parent, title, members, total=None):
        self._members = [dict(m) for m in (members or ())]
        super().__init__(parent, title="What's in this category",
                         accent=ACCENT,
                         scope="Measured by the same read-only walk the "
                               "Storage Insight total came from.",
                         scroll=False, fit=False)

        self.banner.set_state("\u25A4", ACCENT)
        self.banner.set_title(str(title or "This category"))
        self.banner.set_sub(self._subtitle(total))

        self.table = DataTable(self.content, _COLUMNS, row_h=46,
                               accent=ACCENT, two_line=True,
                               show_header=False)
        self.table.pack(fill="both", expand=True)
        rows = self._rows()
        if rows:
            self.table.set_rows(rows)
        else:
            self.table.set_empty(
                text="Nothing to break down",
                detail="This category has no measured cleaners on this PC.")
        self.add_button("Close", self.close, kind="primary")

    # ------------------------------------------------------------------ text
    @staticmethod
    def _subtitle(total):
        if isinstance(total, int) and total > 0:
            return ("%s measured in here. A '?' is a cleaner that declined to "
                    "look (its app was open), not an empty folder."
                    % format_bytes(total))
        return ("Nothing measurable in here right now. A '?' is a cleaner that "
                "declined to look, not an empty folder.")

    # ------------------------------------------------------------------ rows
    def _rows(self):
        rows = []
        for member in self._members:
            size = member.get("size")
            reason = str(member.get("reason") or "").strip()
            if isinstance(size, int):
                cell = {"text": format_bytes(size) if size else "0 B",
                        "mono": True}
                sub = member.get("description") or ""
            else:
                cell = {"text": "?", "fg": COLORS["accent_yellow"]}
                sub = reason or "Not measured by this walk."
            row = {
                "_key": member.get("key") or member.get("label"),
                "_cells": {
                    "member": {"text": member.get("label") or "?",
                               "bold": True, "sub": sub},
                    "size": cell,
                },
            }
            rows.append(row)
        # Unknowns last: the members that hold bytes are the ones to act on.
        rows.sort(key=lambda r: 1 if r["_cells"]["size"].get("text") == "?"
                  else 0)
        return rows
