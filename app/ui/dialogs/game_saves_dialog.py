"""GameSavesDialog — "I lost my save, can I get it back?"

Backed by app/game_saves.py. The Clean tab has always been able to make the
archives; this is the screen that makes them useful:

    Header   how many backups exist, and how old the newest one is
    List     one row per archive: when, how many saves, size
    Button   Restore the selected archive (with a confirm that says, in
             plain words, that current saves are snapshotted first)
    Footer   Back up now / show the folder

Restore is the dangerous verb, so the dialog does three things on purpose:
it never restores on a double-click (select, then press), it names the
pre-restore snapshot in the confirmation, and it reports skipped files
instead of claiming success. Archives written before manifests existed are
listed but marked not restorable — the module refuses to guess destinations.
"""
from __future__ import annotations

import os

from app.ui.base import _themed_askyesno
from app.ui.dialogs._tool_page import TOOL_ACCENT, ToolPage
from app.ui.theme import COLORS
from app.ui.widgets import Tooltip
from app.ui.widgets.datatable import DataTable

_COLUMNS = (
    {"key": "when", "title": "Backup", "w": 190, "sortable": False},
    {"key": "saves", "title": "Saves", "w": 110, "sortable": False},
    {"key": "size", "title": "Size", "w": 90, "weight": 1, "min": 70,
     "sortable": False},
)


def _size(n) -> str:
    from app.utils import format_bytes
    return format_bytes(int(n or 0), compact=True)


def _age(seconds: float) -> str:
    """Plain-English age: '2 days ago', 'just now'."""
    s = max(0.0, seconds)
    if s < 90:
        return "just now"
    if s < 5400:
        return "%d min ago" % max(1, int(s // 60))
    if s < 172800:
        return "%d hours ago" % max(1, int(s // 3600))
    return "%d days ago" % max(1, int(s // 86400))


class GameSavesDialog(ToolPage):
    def __init__(self, parent, app):
        self.app = app
        self._rows: list[dict] = []
        self._busy = False
        super().__init__(
            parent, title="Game Saves",
            scope="Restores are checked against your real save folders, and "
                  "the versions they replace are archived first.")

        self.table = DataTable(self.content, _COLUMNS, row_h=42,
                               accent=TOOL_ACCENT, two_line=True,
                               on_select=self._on_select)
        self.table.pack(fill="both", expand=True)

        self._restore_btn = self.add_button("Restore selected",
                                            self._restore_selected,
                                            kind="primary")
        self._restore_btn.set_enabled(False)
        Tooltip(self._restore_btn,
                "Only the archives made by this app can be restored.")
        self._backup_btn = self.add_button("Back up now", self._backup_now)
        self._folder_btn = self.add_button("Show folder", self._open_folder)

        self.banner.set_state("…", COLORS["muted"])
        self.banner.set_title("Looking for backups…")
        self._refresh()

    # ------------------------------------------------------------ helpers
    def set_busy(self, busy):
        self._busy = bool(busy)
        for b in (self._restore_btn, self._backup_btn):
            try:
                b.set_enabled(not self._busy and self._has_restorable()
                              if b is self._restore_btn
                              else not self._busy)
            except Exception:
                pass

    def _on_select(self, row):
        """Selecting is a read; restoring still takes the explicit button."""
        try:
            self._restore_btn.set_enabled(self._has_restorable() and not self._busy)
        except Exception:
            pass

    def _has_restorable(self) -> bool:
        row = self.table.selected
        return bool(row and (row.get("_cells", {}).get("saves", {})
                             .get("restorable")))

    # --------------------------------------------------------------- read
    def _refresh(self):
        from app.game_saves import list_backups
        import time as _time

        try:
            rows = list_backups()
        except Exception as exc:                      # noqa: BLE001
            rows = []
            self.status.stop("Could not read the backup folder: %s" % exc,
                             COLORS["accent_red"])
        self._rows = rows
        self._render(rows, _time.time())

    def _render(self, rows, now):
        out = []
        for r in rows:
            saves = r.get("files")
            out.append({
                "_key": r["path"],
                "_cells": {
                    "when": {"text": _age(now - r["mtime"]),
                             "sub": r["name"], "bold": True,
                             "dot": (COLORS["accent_green"] if r["restorable"]
                                     else COLORS["accent_yellow"])},
                    "saves": {"text": ("%d file%s" % (saves, "" if saves == 1
                                                     else "s"))
                                     if saves is not None else "?",
                              "sub": "restorable" if r["restorable"]
                                     else "not restorable",
                              "restorable": r["restorable"]},
                    "size": {"text": _size(r["size"])},
                }})
        self.table.set_empty("No save backups yet.",
                             "Use Back up now to make the first one.",
                             glyph="")
        self.table.set_rows(out)

        good = [r for r in rows if r["restorable"]]
        if not rows:
            self.banner.set_state("!", COLORS["accent_yellow"])
            self.banner.set_title("You have no save backups.")
            self.banner.set_sub("Backing up takes a minute and needs no "
                                "settings.")
        elif not good:
            self.banner.set_state("!", COLORS["accent_yellow"])
            self.banner.set_title("Your backups were made before restore "
                                  "support.")
            self.banner.set_sub("Make a new one — it can be restored from here.")
        else:
            newest = min(now - r["mtime"] for r in good)
            self.banner.set_state("✓", COLORS["accent_green"])
            self.banner.set_title(
                "You have %d save backup%s." % (len(good),
                                                "" if len(good) == 1 else "s"))
            self.banner.set_sub("The newest is %s." % _age(newest))
        try:
            self._restore_btn.set_enabled(self._has_restorable() and not self._busy)
        except Exception:
            pass

    # ------------------------------------------------------------ actions
    def _open_folder(self):
        from app.game_saves import backup_folder
        folder = backup_folder()
        try:
            os.startfile(folder)                      # noqa: S606
        except Exception:
            self.status.stop("Could not open the folder — it is at %s" % folder,
                             COLORS["accent_yellow"])

    def _backup_now(self):
        if self._busy:
            return
        from app.game_saves import save_roots
        from app.tasks.game_tasks import backup_game_saves
        from app.utils import TaskContext

        roots = save_roots()
        if not roots:
            self.status.stop("No save folders found on this PC — nothing to "
                             "back up.", COLORS["accent_yellow"])
            return
        self.set_busy(True)
        self.status.start("Backing up your saves…")
        logs: list[str] = []

        def _work():
            ctx = TaskContext(log=logs.append,
                              set_status=lambda _s: None,
                              cancelled=lambda: False)
            try:
                backup_game_saves(ctx)
                return {"ok": True, "log": logs}
            except Exception as exc:                  # noqa: BLE001
                return {"ok": False, "error": str(exc), "log": logs}

        self.run_worker(_work, self._land_backup)

    def _land_backup(self, res):
        self.set_busy(False)
        if not res.get("ok"):
            self.status.stop(res.get("error") or "The backup failed.",
                             COLORS["accent_red"])
            return
        made = [ln for ln in res.get("log") or [] if "Backup complete" in ln]
        self.status.stop(made[-1] if made else "Backup finished.",
                         COLORS["accent_green"])
        self._refresh()

    def _restore_selected(self):
        if self._busy:
            return
        row = self.table.selected
        if not row:
            return
        path = row.get("_key")
        target = next((r for r in self._rows if r["path"] == path), None)
        if target is None or not target["restorable"]:
            self.status.stop("That archive has no manifest, so it cannot be "
                             "restored automatically.",
                             COLORS["accent_yellow"])
            return
        count = target.get("files") or 0
        if not _themed_askyesno(
                self, "Restore these saves?",
                "This puts %d file(s) back where they came from, replacing "
                "whatever is there now.\n\nThe current versions are saved to "
                "a PreRestore archive first, so this can be undone.\n\nClose "
                "the game before restoring." % count,
                accent=COLORS["accent_red"], yes_text="Restore",
                no_text="Cancel"):
            return

        self.set_busy(True)
        self.status.start("Restoring %d file(s)…" % count)
        from app.game_saves import restore_backup

        logs: list[str] = []
        self.run_worker(lambda: restore_backup(path, log=logs.append),
                        lambda res: self._land_restore(res, logs))

    def _land_restore(self, res, logs):
        self.set_busy(False)
        if res.get("cancelled"):
            self.status.stop("Restore stopped — %d file(s) were written."
                             % res.get("restored", 0), COLORS["accent_yellow"])
        elif not res.get("ok"):
            self.status.stop(res.get("error") or "The restore failed.",
                             COLORS["accent_red"])
        elif res.get("skipped"):
            self.status.stop(
                "Restored %d file(s); %d were skipped (locked or not in a save "
                "folder now)." % (res.get("restored", 0), res["skipped"]),
                COLORS["accent_yellow"])
        else:
            self.status.stop("Restored %d file(s)." % res.get("restored", 0),
                             COLORS["accent_green"])
        try:
            self.app.log("\n".join(logs[-40:]))
        except Exception:
            pass
        self._refresh()
