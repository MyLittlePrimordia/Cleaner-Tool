"""UpdatePickerDialog - choose exactly which Windows updates to install.

"Fix everything" installs Windows and security updates unattended and only
LISTS driver updates. People who want control - or want a driver Windows
Update is offering - get it here: every pending update from Windows Update,
a checkbox on each, and an Install button that installs only what is ticked
(app/updates.install_selected). Drivers are flagged, because Windows Update
sometimes offers an older or buggy graphics driver and the user should know
which rows those are before ticking them.

Feature upgrades (a new Windows version) are never listed as installable, and
updates that need a licence agreement are shown but not selectable - they are
left for Settings > Windows Update, where the licence is shown.

Installing needs administrator rights; without them the list still works
(it is read-only) and the button explains why it is off.

Threading: one daemon worker for the scan, one for the install; results come
back through the dispatcher.
"""
from __future__ import annotations

import threading

from app.ui.dialogs._tool_page import TOOL_ACCENT, ToolPage
from app.ui.theme import COLORS
from app.ui.widgets.datatable import DataTable

_COLUMNS = (
    {"key": "name", "title": "Update", "w": 380, "weight": 1, "min": 220,
     "sortable": False},
    {"key": "kind", "title": "", "w": 96, "min": 84, "sortable": False},
    {"key": "size", "title": "Size", "w": 80, "min": 70, "anchor": "e",
     "sortable": False},
)

_KIND = {
    "driver": ("Driver", (COLORS["surface"], COLORS["accent_orange"])),
    "security": ("Security", (COLORS["surface"], COLORS["accent_green"])),
    "definition": ("Definitions", (COLORS["surface"],
                                   COLORS["accent_green"])),
    "software": ("Update", (COLORS["surface"], COLORS["subtext"])),
    "upgrade": ("New Windows", (COLORS["surface"],
                                COLORS["accent_yellow"])),
}


def _fmt(n):
    try:
        from app.utils import format_bytes
        return format_bytes(int(n or 0), compact=True)
    except Exception:
        return ""


class UpdatePickerDialog(ToolPage):
    def __init__(self, parent, app):
        self.app = app
        self._updates = []
        self._checked = set()
        self._busy = False
        self._stop_token = [False]
        self._seq = 0

        super().__init__(
            parent, title="Choose Updates",
            scope="Installs only what you tick. A restart may be needed.")

        self.table = DataTable(self.content, _COLUMNS, row_h=50,
                               accent=TOOL_ACCENT, two_line=True,
                               show_header=False, on_check=self._on_check)
        self.table.pack(fill="both", expand=True)

        self._install = self.add_button("Install selected", self._do_install,
                                        kind="primary")
        self._install.set_enabled(False)
        self._all = self.add_button("Select all", self._toggle_all)
        self._all.set_enabled(False)
        self.refresh_btn = self.add_button("Check again", self._start)
        self.banner.set_state("…", COLORS["muted"])
        self.banner.set_title("Asking Windows Update...")
        self.on_close(self._stop)
        self._start()

    # ------------------------------------------------------------ helpers
    def _stop(self):
        self._stop_token[0] = True

    def _selectable(self, u):
        return u["kind"] != "upgrade" and u.get("eula", True)

    def _sync(self):
        n = len(self._checked)
        try:
            self._install.config_text("Install selected (%d)" % n if n
                                      else "Install selected")
            self._install.set_enabled(bool(n) and not self._busy)
            self._all.set_enabled(not self._busy and any(
                self._selectable(u) for u in self._updates))
            self.refresh_btn.set_enabled(not self._busy)
        except Exception:
            pass

    # --------------------------------------------------------------- scan
    def _start(self):
        if self._busy:
            return
        from app import updates
        self._stop_token[0] = False
        self._seq += 1
        seq = self._seq
        self._busy = True
        self._checked = set()
        self.banner.set_state("…", COLORS["muted"])
        self.banner.set_title("Asking Windows Update...")
        self.banner.set_sub("This can take a minute.")
        self.status.start("Searching for updates...")
        self.table.set_rows([])
        self.table.set_empty("Searching...", "", glyph="")
        self._sync()

        self.run_worker(updates.scan, lambda res: self._land(seq, res))

    def _land(self, seq, res):
        if seq != self._seq:
            return
        self._busy = False
        self.status.stop("")
        self._updates = res.get("updates") or []
        if not res.get("ok"):
            self.banner.set_state("!", COLORS["accent_yellow"])
            self.banner.set_title("Windows Update did not answer",
                                  COLORS["accent_yellow"])
            self.banner.set_sub(res.get("error") or "")
            self.table.set_empty("Could not read the update list",
                                 "Try Check again in a minute.", glyph="!")
        elif not self._updates:
            self.banner.set_state("✓", COLORS["accent_green"])
            self.banner.set_title("Windows is up to date.")
            self.banner.set_sub("")
            self.table.set_empty("Nothing to install", "", glyph="✓")
        else:
            s = res["summary"]
            self.banner.set_state(str(s["total"]), COLORS["accent_yellow"])
            self.banner.set_title("%d update%s available." % (
                s["total"], "" if s["total"] == 1 else "s"))
            bits = []
            if s["drivers"]:
                bits.append("%d driver%s (tick these only if you want "
                            "them)" % (s["drivers"],
                                       "" if s["drivers"] == 1 else "s"))
            if s["needs_eula"]:
                bits.append("%d need a licence you accept in Settings" %
                            s["needs_eula"])
            self.banner.set_sub("; ".join(bits) or "Tick the ones you want.")
        self._render()

    def _render(self):
        rows = []
        for u in self._updates:
            word, pill = _KIND.get(u["kind"], _KIND["software"])
            sel = self._selectable(u)
            sub = u["maker"] + " " + u["model"] if u["kind"] == "driver" \
                else ""
            if u["kind"] == "upgrade":
                sub = "A new version of Windows - install it from Settings"
            elif not u.get("eula", True):
                sub = "Needs you to accept a licence in Settings"
            elif u["reboot"]:
                sub = (sub + "  ·  needs a restart").strip(" ·")
            row = {"_key": u["id"], "_dim": not sel, "_cells": {
                "name": {"text": u["title"], "bold": True, "sub": sub.strip()},
                "kind": {"text": word, "pill": pill},
                "size": {"text": _fmt(u["size"]), "fg": COLORS["subtext"]}}}
            if sel:
                row["_check"] = u["id"] in self._checked
            rows.append(row)
        self.table.set_rows(rows)
        self._sync()

    def _on_check(self, row):
        uid = row["_key"]
        (self._checked.discard if uid in self._checked
         else self._checked.add)(uid)
        self._render()

    def _toggle_all(self):
        ids = {u["id"] for u in self._updates if self._selectable(u)}
        self._checked = set() if self._checked == ids else ids
        self._render()

    # ------------------------------------------------------------ install
    def _do_install(self):
        from app.ui.base import _themed_askyesno
        chosen = [u for u in self._updates if u["id"] in self._checked]
        if not chosen or self._busy:
            return
        has_driver = any(u["kind"] == "driver" for u in chosen)
        body = "\n".join("- " + u["title"][:70] for u in chosen[:8])
        if len(chosen) > 8:
            body += "\n- and %d more" % (len(chosen) - 8)
        if has_driver:
            body += ("\n\nThis includes a driver. If the screen or a device "
                     "misbehaves afterwards, use Restore Points to go back.")
        if not _themed_askyesno(
                self._dlg, "Install %d update%s?" % (
                    len(chosen), "" if len(chosen) == 1 else "s"), body,
                accent=TOOL_ACCENT, yes_text="Install", no_text="Go Back"):
            return
        from app import updates
        from app.utils import TaskContext, TaskSkipped
        ids = [u["id"] for u in chosen]
        self._busy = True
        self._sync()
        self.status.start("Installing %d update(s)... this can take a while."
                          % len(ids))
        token = self._stop_token

        def worker():
            ctx = TaskContext(
                log=lambda _m: None,
                set_status=lambda m: self._ui(self.status.set_text, str(m)),
                cancelled=lambda: bool(token[0]))
            try:
                res = updates.install_selected(ctx, ids)
                self._ui(self._land_install, res, "")
            except TaskSkipped as exc:
                self._ui(self._land_install, None, str(exc))
            except Exception as exc:                       # noqa: BLE001
                self._ui(self._land_install, None, str(exc))

        threading.Thread(target=worker, daemon=True).start()

    def _land_install(self, res, error):
        self._busy = False
        if res is None:
            self.status.stop(error, COLORS["accent_red"])
            self._sync()
            return
        done, bad = int(res.get("installed") or 0), int(res.get("failed") or 0)
        text = "Installed %d update%s" % (done, "" if done == 1 else "s")
        if bad:
            text += "; %d did not install" % bad
        if res.get("reboot"):
            text += ". Restart your PC to finish."
        self.status.stop(text, COLORS["accent_yellow"] if bad
                         else COLORS["accent_green"])
        self.banner.set_state("✓" if not bad else "!",
                              COLORS["accent_green"] if not bad
                              else COLORS["accent_yellow"])
        self.banner.set_title(text)
        self.banner.set_sub("Press Check again to refresh the list.")
        done_ids = set(self._checked)
        self._updates = [u for u in self._updates if u["id"] not in done_ids] \
            if not bad else self._updates
        self._checked = set()
        self._render()
