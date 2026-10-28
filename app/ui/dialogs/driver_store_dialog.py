"""DriverStoreDialog - the old driver packages Windows never shows you.

U-005 (audit 2026-10). Every driver update leaves the previous package in
Windows' driver store; on a gaming PC that is routinely a couple of gigabytes
nobody can find. The Drivers tab could update a driver but never showed what
the updates left behind.

    Header   the answer ("2 old driver versions nothing is using.")
    List     the offered ones first, with a checkbox; then the unused ones
             that are NOT offered, each with the reason in its own row
    Button   ONE primary action: Export & remove (N). Nothing is deleted
             until it has been exported, and that happens inside the same
             elevated script - see app/drivers/store.py.
    Footer   Check again / Open backup folder

Deliberately absent: a "select all" (two versions is the normal case, and a
bulk delete of drivers is exactly the action that should stay manual), sizes
(pnputil does not report them, and inventing one would be a lie), any delete
that is not preceded by its own export, and the packages a device is using
right now (they cannot be touched, so listing a hundred rows of them would
bury the two that matter - the header says how many there are).
"""
from __future__ import annotations

import os

from app.ui.base import _themed_askyesno
from app.ui.dialogs._tool_page import TOOL_ACCENT, ToolPage
from app.ui.theme import COLORS
from app.ui.widgets.datatable import DataTable

_COLUMNS = (
    {"key": "package", "title": "Driver package", "w": 380, "weight": 1,
     "min": 220, "sortable": False},
    {"key": "state", "title": "", "w": 110, "min": 100, "sortable": False},
)

#: (text, (pill background, pill text)) per listed state. A superseded
#: package is the only one with an action, so its pill is the only warm one.
_STATE = {
    "superseded": ("Old version", (COLORS["surface"], COLORS["accent_yellow"])),
    "orphaned": ("Not in use", (COLORS["bg_alt"], COLORS["muted"])),
}

#: Why a row exists but cannot be ticked, in the space of one line.
_WHY_SHORT = {
    "orphaned": "no device is using it \u2014 it may belong to unplugged "
                "hardware",
}


def _details(pkg) -> str:
    bits = [pkg.published]
    if pkg.provider:
        bits.append(pkg.provider)
    if pkg.cls:
        bits.append(pkg.cls)
    if pkg.version:
        bits.append(pkg.version)
    return "  ·  ".join(b for b in bits if b)


class DriverStoreDialog(ToolPage):
    def __init__(self, parent, app):
        self.app = app
        self._stop_token = [False]
        self._data = {}
        self._rows = []
        self._checked = set()          # published names, lowercase
        self._busy = False
        self._note = None              # (text, colour) to show after a re-read

        super().__init__(
            parent, title="Driver Store",
            accent=TOOL_ACCENT,
            scope="Old package versions are copied to your Drivers folder "
                  "before they are removed, so one can be put back.")

        self.table = DataTable(self.content, _COLUMNS, row_h=52,
                               accent=TOOL_ACCENT, two_line=True,
                               on_check=self._on_check, show_header=False)
        self.table.pack(fill="both", expand=True)
        self.table.set_empty("Reading the driver store…", "", glyph="")

        self._primary = self.add_button("Export & remove", self._clean,
                                        kind="primary")
        self._primary.set_enabled(False)
        self._open_btn = self.add_button("Open backup folder",
                                         self._open_backup)
        self._again = self.add_button("Check again", self._start)

        self.banner.set_state("…", COLORS["muted"])
        self.banner.set_title("Reading the driver store…")
        self.banner.set_sub("")
        self.on_close(self._stop)
        self._start()

    # ------------------------------------------------------------ helpers
    def _stop(self):
        self._stop_token[0] = True

    def _drivers_root(self) -> str:
        from app.drivers import folder
        try:
            return folder.configured()
        except Exception:
            return ""

    def set_busy(self, busy):
        self._busy = bool(busy)
        try:
            self._again.set_enabled(not self._busy)
        except Exception:
            pass
        self._sync()

    def _sync(self):
        n = len(self._checked)
        try:
            self._primary.config_text(
                "Export & remove (%d)" % n if n else "Export & remove")
            self._primary.set_enabled(bool(n) and not self._busy)
        except Exception:
            pass

    # --------------------------------------------------------------- read
    def _start(self, note=None):
        """Read the store. `note` survives the re-read, so the outcome of a
        removal is not wiped out by the refresh that follows it."""
        if self._busy:
            return
        self._note = note
        self._stop_token[0] = False
        self._checked = set()
        self.set_busy(True)
        self.status.start("Reading the driver store…")

        def work():
            from app.drivers import store
            return store.report()

        def land(data):
            if self._stop_token[0]:
                return
            self._land(data)

        self.run_worker(work, land)

    def _land(self, data):
        self._data = data if isinstance(data, dict) else {}
        self._rows = list(self._data.get("rows") or [])
        self.set_busy(False)
        if self._note:
            self.status.stop(self._note[0], self._note[1])
        else:
            self.status.stop("")
        self._refresh_banner()
        self._render()

    def _offered(self):
        return [r for r in self._rows if r.state == "superseded"]

    def _listed(self):
        """Rows worth drawing: the offered ones and the explained ones."""
        return [r for r in self._rows if r.state != "in-use"]

    def _refresh_banner(self):
        err = self._data.get("error") or ""
        total = int(self._data.get("packages") or 0)
        offer = self._offered()
        if err:
            self.banner.set_state("!", COLORS["accent_yellow"])
            self.banner.set_title("Could not read the driver store.",
                                  COLORS["accent_yellow"])
            self.banner.set_sub(err)
            return
        if offer:
            n = len(offer)
            self.banner.set_state(str(n), COLORS["accent_yellow"])
            self.banner.set_title(
                "%d old driver version%s nothing is using." % (
                    n, "" if n == 1 else "s"))
            self.banner.set_sub(
                "Windows keeps the previous package every time a driver "
                "updates. Each is exported to your Drivers folder before it "
                "is removed; packages a device is using are not listed.")
        elif total:
            self.banner.set_state("✓", COLORS["accent_green"])
            self.banner.set_title("No leftover driver versions.")
            self.banner.set_sub(
                "%d package%s in the store, and every one of them is in use." % (
                    total, "" if total == 1 else "s"))
        else:
            self.banner.set_state("✓", COLORS["accent_green"])
            self.banner.set_title("The driver store is empty.")
            self.banner.set_sub(
                "No third-party driver package is installed on this PC.")

    def _render(self):
        rows = []
        for row in self._listed():
            pkg = row.package
            key = pkg.published.lower()
            text, pill = _STATE.get(row.state, ("", None))
            cells = {"package": {"text": pkg.label, "bold": True,
                                 "sub": _details(pkg)},
                     "state": {"text": text, "pill": pill} if pill
                     else {"text": text, "fg": COLORS["muted"]}}
            if row.offered:
                rows.append({"_key": pkg.published, "_row": row,
                             "_check": key in self._checked,
                             "_cells": cells})
            else:
                # The reason belongs ON the row: a package that is listed and
                # cannot be ticked has to say why without a hover nobody
                # discovers.
                cells["package"]["sub"] = "%s  —  %s" % (
                    pkg.published, _WHY_SHORT.get(row.state, row.why))
                rows.append({"_key": pkg.published, "_row": row,
                             "_dim": True, "_cells": cells})
        self.table.set_empty(
            "Nothing left to clean up",
            "Every package in the driver store is in use by a device.",
            glyph="")
        self.table.set_rows(rows)
        self._sync()

    # ------------------------------------------------------------ actions
    def _on_check(self, row):
        pkg = row.get("_row").package
        key = pkg.published.lower()
        if key in self._checked:
            self._checked.discard(key)
        else:
            self._checked.add(key)
        self._render()

    def _chosen(self):
        return [r.package for r in self._offered()
                if r.package.published.lower() in self._checked]

    def _clean(self):
        chosen = self._chosen()
        if not chosen or self._busy:
            return
        names = "\n".join("- %s (%s)" % (p.label, p.published)
                          for p in chosen[:8])
        if len(chosen) > 8:
            names += "\n- and %d more" % (len(chosen) - 8)
        root = self._drivers_root()
        if not root:
            self.status.stop("Choose a Drivers folder first (on the Drivers "
                             "tab).", COLORS["accent_yellow"])
            return
        if not _themed_askyesno(
                self._dlg, "Remove %d old driver package%s?" % (
                    len(chosen), "" if len(chosen) == 1 else "s"),
                "%s\n\nEach package is copied to your Drivers folder first, "
                "then removed from the store. Windows will ask for "
                "administrator rights once.\n\nNothing a device is using is "
                "touched." % names,
                accent=TOOL_ACCENT, yes_text="Export & remove",
                no_text="Go Back"):
            return

        self.set_busy(True)
        self.status.start("Waiting for the administrator prompt…")

        def work():
            from app.drivers import store
            return store.clean_elevated(chosen,
                                        os.path.join(root, "backup"))

        def land(res):
            if self._stop_token[0]:
                return
            self._land_clean(res)

        self.run_worker(work, land)

    def _land_clean(self, res):
        self.set_busy(False)
        res = res if isinstance(res, dict) else {}
        if res.get("cancelled"):
            self.status.stop("Administrator access was declined. Nothing was "
                             "removed.", COLORS["accent_yellow"])
            return
        if res.get("error"):
            self.status.stop(res["error"], COLORS["accent_red"])
            return
        removed = list(res.get("removed") or [])
        failed = list(res.get("failed") or [])
        self._checked = set()
        if removed:
            text = "Removed %d package%s." % (len(removed),
                                              "" if len(removed) == 1 else "s")
            if failed:
                text += " %d could not be removed." % len(failed)
            note = (text + "  Backed up to %s" % res.get("folder", ""),
                    COLORS["accent_green"] if not failed
                    else COLORS["accent_yellow"])
        elif failed:
            note = ("%s: %s" % (failed[0][0], failed[0][1]),
                    COLORS["accent_yellow"])
        else:
            note = ("Nothing was removed.", COLORS["accent_yellow"])
        self._start(note=note)

    def _open_backup(self):
        root = self._drivers_root()
        path = os.path.join(root, "backup") if root else ""
        try:
            os.makedirs(path, exist_ok=True)
            os.startfile(path)
        except Exception:
            self.status.stop("Could not open the backup folder.",
                             COLORS["accent_yellow"])


__all__ = ["DriverStoreDialog"]
