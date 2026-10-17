"""RestorePointDialog - "can I get my PC back if this goes wrong?"

Backed by app/restorepts.py.

Rebuilt from scratch (2026-10, second pass). The first redesign kept the old
shape - stat tiles, a name box, a search box, four buttons - and a normal
user still could not tell what to do. This tool answers ONE question, so:

    Header   the answer ("You have 5 restore points." / "You are not
             protected.") and one line of why
    List     when, and what it was for. Nothing to configure.
    Button   ONE primary action that changes with the situation:
             Create restore point / Turn on protection / Show all (admin)
    Footer   Open System Restore (Windows' own wizard, the safe way back)

Gone on purpose: the name box (a date-stamped default is always fine), the
search box, stat tiles, Copy list, Check again (it re-reads after you
create one). Nothing here deletes points or runs a restore; rolling back is
destructive and Windows ships a reviewable wizard for it.

Elevation is part of the design, not an error path: a declined UAC prompt is
reported as "cancelled", never as a failure.
"""
from __future__ import annotations

import threading

from app.ui.dialogs._tool_page import TOOL_ACCENT, ToolPage
from app.ui.theme import COLORS
from app.ui.widgets import Tooltip
from app.ui.widgets.datatable import DataTable

DEFAULT_DESC = "Cleaner Tool - safety point"

_DOT = {"CUSTOM": COLORS["accent_green"]}

_COLUMNS = (
    {"key": "when", "title": "When", "w": 170, "sortable": False},
    {"key": "desc", "title": "What it was for", "w": 240, "weight": 1,
     "min": 140, "sortable": False},
)


class RestorePointDialog(ToolPage):
    def __init__(self, parent, app):
        self.app = app
        self._stop_token = [False]
        self._seq = 0
        self._data = {}
        self._needs_admin = False
        self._protect = {}
        self._busy = False

        super().__init__(
            parent, title="Restore Points",
            scope="Creating one only adds protection. To go back, use "
                  "Windows' own System Restore.")

        self.table = DataTable(self.content, _COLUMNS, row_h=40,
                               accent=TOOL_ACCENT, two_line=True)
        self.table.pack(fill="both", expand=True)

        # add_button packs right-to-left: first call is rightmost.
        self._primary = self.add_button("Create restore point", self._do_primary,
                                        kind="primary")
        self._primary.set_enabled(False)
        self._restore_btn = self.add_button("Open System Restore",
                                            self._open_restore)
        Tooltip(self._restore_btn,
                "Windows' own wizard - the safe way to go back.")
        self.refresh_btn = self._restore_btn   # busy hook target

        self.banner.set_state("…", COLORS["muted"])
        self.banner.set_title("Checking...")
        self.on_close(self._stop)
        self._start()

    # ------------------------------------------------------------ helpers
    def _stop(self):
        self._stop_token[0] = True

    def set_busy(self, busy):
        self._busy = bool(busy)

    def _worker_failed(self, exc):
        # B-017: the primary button is re-enabled by the _land handlers; on a
        # worker exception it must be restored here or it stays disabled.
        try:
            self._primary.set_enabled(True)
        except Exception:
            pass
        super()._worker_failed(exc)

    def _mode(self):
        """What the one primary button does right now."""
        state = self._protect.get("state")
        if state == "off":
            return "protect"
        if self._needs_admin:
            return "admin"
        return "create"

    def _do_primary(self):
        {"protect": self._open_protection, "admin": self._load_elevated,
         "create": self._create}[self._mode()]()

    # --------------------------------------------------------------- read
    def _start(self):
        from app.restorepts import list_points, protection_status
        token = self._stop_token
        token[0] = False
        self._seq += 1
        seq = self._seq
        self.set_busy(True)
        self.status.start("Checking...")

        def _work():
            try:
                prot = protection_status()
            except Exception:                          # noqa: BLE001
                prot = {}
            try:
                data = list_points()
            except Exception as exc:                  # noqa: BLE001
                data = {"points": [], "error": "Could not read restore "
                                              "points: %s" % exc}
            return prot, data

        def _land_pair(pair):
            if token[0]:
                return
            self._land(seq, pair[0], pair[1])

        self.run_worker(_work, _land_pair)

    def _land(self, seq, prot, data):
        if seq != self._seq:
            return
        self._protect = prot if isinstance(prot, dict) else {}
        self._data = data if isinstance(data, dict) else {}
        self._needs_admin = "administrator" in (self._data.get("error") or "")
        self.set_busy(False)
        self.status.stop("")
        self._refresh_banner()
        self._render()

    def _refresh_banner(self):
        """Badge, headline and button are decided here, together, so they
        can never disagree."""
        from app.restorepts import is_elevated, when_plain
        pts = self._data.get("points") or []
        state = self._protect.get("state")
        amber, red = COLORS["accent_yellow"], COLORS["accent_red"]
        green = COLORS["accent_green"]
        newest = when_plain(pts[0].get("when")) if pts else ""

        if state == "off":
            glyph, color, title = "!", red, "You are not protected."
            sub = "System Protection is off, so Windows cannot make " \
                  "restore points."
            label = "Turn on protection"
        elif self._needs_admin:
            glyph, color = "!", amber
            title = "Windows will not list restore points to you yet."
            sub = "You can still create one. Showing the list needs " \
                  "administrator permission."
            label = "Show restore points"
        elif self._data.get("error"):
            glyph, color = "!", amber
            title, sub = self._data["error"], ""
            label = "Create restore point"
        elif not pts:
            glyph, color = "!", red
            title = "You have no restore points."
            sub = "There is currently no way to undo a change."
            label = "Create restore point"
        else:
            glyph, color = "✓", green
            title = "You have %d restore point%s." % (
                len(pts), "" if len(pts) == 1 else "s")
            sub = "The newest is from %s." % newest.lower() if newest else ""
            label = "Create restore point"
        if state == "limited" and pts:
            sub = (sub + " Protection is on for some drives only.").strip()
        self.banner.set_state(glyph, color)
        self.banner.set_title(title, color if color != green else None)
        self.banner.set_sub(sub)
        self._primary.config_text(label)
        self._primary.set_enabled(not self._busy)
        _ = is_elevated

    def _render(self):
        from app.restorepts import when_plain
        rows = []
        for p in (self._data.get("points") or []):
            rows.append({
                "_key": p.get("seq", id(p)),
                "_cells": {
                    "when": {"text": when_plain(p.get("when")) or "?",
                             "sub": p.get("when") or "", "bold": True,
                             "dot": _DOT.get(str(p.get("type") or "").upper(),
                                             COLORS["muted"])},
                    "desc": {"text": p.get("desc") or "",
                             "sub": p.get("plain") or ""},
                }})
        self.table.set_empty("Nothing to show yet.",
                             "Restore points you create will appear here.",
                             glyph="")
        self.table.set_rows(rows)

    # ------------------------------------------------------------ actions
    def _load_elevated(self):
        from app.restorepts import list_points_elevated
        self.set_busy(True)
        self._primary.set_enabled(False)
        self.status.start("Waiting for the administrator prompt...")

        self.run_worker(list_points_elevated, self._land_elev)

    def _land_elev(self, data):
        self.set_busy(False)
        if data.get("cancelled"):
            self.status.stop("Administrator access was declined.",
                             COLORS["accent_yellow"])
            self._primary.set_enabled(True)
            return
        self.status.stop("")
        self._data = data if isinstance(data, dict) else {}
        self._needs_admin = False
        self._refresh_banner()
        self._render()

    def _create(self):
        from app.restorepts import create, create_elevated, is_elevated
        if self._busy:
            return
        elevated = is_elevated()
        self.set_busy(True)
        self._primary.set_enabled(False)
        self.status.start("Creating a restore point..." if elevated else
                          "Waiting for the administrator prompt...")

        def worker():
            try:
                res = (create(DEFAULT_DESC) if elevated
                       else create_elevated(DEFAULT_DESC))
            except Exception as exc:                  # noqa: BLE001
                res = {"ok": False, "error": "Could not create one: %s" % exc}
            self._ui(self._land_create, res)

        threading.Thread(target=worker, daemon=True).start()

    def _land_create(self, res):
        self.set_busy(False)
        self._primary.set_enabled(True)
        if res.get("cancelled"):
            self.status.stop("Administrator access was declined.",
                             COLORS["accent_yellow"])
        elif res.get("skipped"):
            # The 24-hour rule is a skip, not a fault (BUG-013).
            self.status.stop(res.get("message") or "Skipped.")
        elif not res.get("ok"):
            self.status.stop(res.get("error") or "Could not create one.",
                             COLORS["accent_red"])
        else:
            self.status.stop("Restore point created.",
                             COLORS["accent_green"])
            if not self._needs_admin:
                self._start()

    def _open_protection(self):
        from app.restorepts import open_system_protection
        ok, reason = open_system_protection()
        if ok:
            self.status.stop("Opened Windows' System Protection. Come back "
                             "after turning it on.")
        elif reason == "cancelled":
            self.status.stop("Administrator access was declined.",
                             COLORS["accent_yellow"])
        else:
            self.status.stop("Could not open it. Turn System Protection on "
                             "in Settings > System > About.",
                             COLORS["accent_yellow"])

    def _open_restore(self):
        from app.restorepts import open_system_restore
        if open_system_restore():
            self.status.stop("Opened Windows' System Restore wizard.")
        else:
            self.status.stop("Could not open it. Try Start > rstrui.",
                             COLORS["accent_yellow"])
