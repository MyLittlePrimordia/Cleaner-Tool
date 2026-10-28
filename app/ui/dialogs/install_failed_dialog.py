"""What to do when a catalog app will not install (U-001).

The install engine has always known the answer — every catalog entry carries
the vendor's own download page, and a failed install logs
`-> Manual download available at: <url>` — but that line goes to the in-memory
log, which has no window in this app. So the one moment the user needs the
link, it is invisible: the run ends in a themed box saying "2 failed", and
recovering means knowing the catalog, finding the app again, and spotting the
🔗 mirror.

This dialog is the missing half of that failure path: one row per app that did
not install, each with an "Open page" action that uses the same browser-raising
opener as the Install tab (app/openlink.py). It claims nothing about why the
install failed — winget refuses for a lot of reasons, including no internet —
so the header says what happened and the run's own summary rides along.
"""
from __future__ import annotations

from app.ui.base import Tooltip
from app.ui.dialogs._tool_page import ToolPage
from app.ui.theme import COLORS
from app.ui.widgets.datatable import DataTable

ACCENT = COLORS["accent_yellow"]

_COLUMNS = (
    {"key": "app", "title": "App", "w": 300, "weight": 1, "min": 160,
     "sortable": False},
)


class InstallFailedDialog(ToolPage):
    """Failed installs, each with the vendor page that fixes it."""

    def __init__(self, parent, failed, summary=""):
        self._failed = [dict(f) for f in (failed or [])
                        if str((f or {}).get("name") or "").strip()]
        super().__init__(parent, title="Install Failed", accent=ACCENT,
                         scope="Nothing on your PC was changed for these "
                               "apps; the run's other items were unaffected.",
                         scroll=False, fit=False)

        n = len(self._failed)
        self.banner.set_state("!", ACCENT)
        self.banner.set_title(
            "1 app didn't install" if n == 1 else "%d apps didn't install" % n)
        # Honest about the cause: the dialog knows THAT winget failed, not why.
        # A package delisted upstream, a blocked download and no internet all
        # land here, and the run's own summary (which does know) is below.
        self.banner.set_sub(
            "Windows' package manager (winget) could not install "
            + ("this one" if n == 1 else "these")
            + ". That can mean the package is no longer published, the "
              "download was blocked, or the PC was offline. Each row links "
              "the vendor's own page.")

        if str(summary or "").strip():
            try:
                self.status.set_text(str(summary).strip().splitlines()[0])
            except Exception:
                pass

        self.table = DataTable(self.content, _COLUMNS, row_h=46,
                               accent=ACCENT, two_line=True,
                               show_header=False,
                               on_action=self._on_action)
        self.table.pack(fill="both", expand=True)
        self.table.set_rows(self._rows())

        close = self.add_button("Close", self.close, kind="primary")
        Tooltip(close, "The download pages stay available on the Install tab.")

    # ------------------------------------------------------------------ rows
    def _rows(self):
        rows = []
        for item in self._failed:
            url = str(item.get("url") or "")
            actions = [{"id": "open", "text": "Open page"}] if url else []
            rows.append({
                "_key": str(item.get("name")),
                "_actions": actions,
                "_cells": {"app": {
                    "text": str(item.get("name")),
                    "bold": True,
                    "sub": ("Review the end of the log for winget's own "
                            "message." if not url else
                            "Install it from the official page; the app will "
                            "leave it unticked."),
                }},
            })
        return rows

    def _on_action(self, row, aid):
        if aid != "open":
            return
        item = next((f for f in self._failed
                     if str(f.get("name")) == row.get("_key")), {})
        url = str(item.get("url") or "")
        if not url:
            return
        from app.openlink import open_web_url
        open_web_url(url, show_error=lambda message: self._say(message))

    def _say(self, message):
        try:
            self.status.set_text(message)
        except Exception:
            pass
