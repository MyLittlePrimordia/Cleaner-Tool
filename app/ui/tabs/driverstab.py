"""DriversTab - find, download and install drivers for this PC.

A main-window tab (not a dialog): it is a workflow with state - scan, choose,
download, install - and people come back to it.

    Header    the answer ("3 drivers can be updated", "1 device has no
              driver") in the same Header every Tools dialog uses
    Toolbar   Scan this PC, a "check every device" option, and the folder
              downloads go to (open / change)
    List      one row per driver package: checkbox, the device(s), installed
              version -> available version, size, and a Missing / Update tag
    Footer    progress bar + live status, Select all, Download selected, and
              Download & install

Where the drivers come from, and what this is NOT, is documented in
app/drivers/__init__.py: Microsoft-signed packages from the Microsoft Update
Catalog, looked up by hardware id. Coverage is whatever vendors published to
Windows Update. For a device with nothing there, or the newest graphics
driver, the row says so and offers the manufacturer's page.

Installing needs administrator rights, makes a restore point first, and backs
up the driver it replaces (app/drivers/service.py). Download-only never
changes the PC.

Threading: one daemon worker per operation; it reaches Tk only through the
dispatcher (BUG-001).
"""
from __future__ import annotations

import os
import threading
import tkinter as tk

from app.ui.widgets.checkbox import M3Checkbutton
from tkinter import filedialog

from app.ui.base import _themed_askyesno, _themed_showinfo
from app.ui.theme import COLORS, F, TAB_ACCENTS
from app.ui.widgets import AnimatedButton, Tooltip
from app.ui.widgets.datatable import DataTable
from app.ui.widgets.header import Header

ACCENT = TAB_ACCENTS["Drivers"]

_COLUMNS = (
    {"key": "name", "title": "Driver", "w": 380, "weight": 1, "min": 220,
     "sortable": False},
    {"key": "status", "title": "", "w": 90, "min": 80, "sortable": False},
    {"key": "size", "title": "Size", "w": 80, "min": 70, "anchor": "e",
     "sortable": False},
)


def _fmt_size(n) -> str:
    try:
        from app.utils import format_bytes
        return format_bytes(int(n or 0), compact=True)
    except Exception:
        return ""


class DriversTab(tk.Frame):
    def __init__(self, parent, app):
        super().__init__(parent, bg=COLORS["bg"])
        self.app = app
        self._token = [False]
        self._items = []          # candidates from the last scan
        self._checked = set()     # update_ids ticked
        self._busy = False
        self._seq = 0
        self._downloaded = {}     # update_id -> result dict
        self._run_locked = False  # a Clean/Repair/Tweak run is in progress
        self._devices = []        # inventory from the last scan
        self._sel = None          # selected row key (shows its extra action)
        self._dispatch = getattr(app, "_dispatch", None)

        pad = tk.Frame(self, bg=COLORS["bg"])
        pad.pack(fill="both", expand=True, padx=24, pady=(6, 10))

        self.header = Header(pad, accent=ACCENT)
        self.header.pack(fill="x")
        self.header.set_state("?", COLORS["muted"])
        self.header.set_title("Find drivers for this PC.")
        self.header.set_sub("Scan checks your hardware and looks for missing "
                            "or newer drivers online. Nothing is installed "
                            "unless you choose it.")

        bar = tk.Frame(pad, bg=COLORS["bg"])
        bar.pack(fill="x", pady=(10, 8))
        self.scan_btn = AnimatedButton(
            bar, text="Scan this PC", command=self._scan, bg=ACCENT,
            fg=COLORS["black"], font=(F, 9, "bold"), padx=16, pady=6)
        self.scan_btn.pack(side="left")
        self._full = tk.BooleanVar(value=False)
        full = M3Checkbutton(
            bar, text="Check every device (slower)", variable=self._full,
            bg=COLORS["bg"], fg=COLORS["subtext"], selectcolor=COLORS["bg"],
            activebackground=COLORS["bg"], activeforeground=COLORS["text"],
            font=(F, 9), bd=0, highlightthickness=0)
        full.pack(side="left", padx=(14, 0))
        Tooltip(full, "By default only devices that are missing a driver or "
                      "have an older third-party driver are checked.")
        self.folder_lbl = tk.Label(bar, text="", font=(F, 9),
                                   bg=COLORS["bg"], fg=COLORS["muted"],
                                   anchor="e")
        self.change_btn = AnimatedButton(
            bar, text="Change folder\u2026", command=self._change_folder,
            bg=COLORS["bg"], fg=COLORS["primary"], variant="outlined",
            font=(F, 10, "bold"), padx=16, pady=6)
        self.change_btn.pack(side="right")
        self.open_btn = AnimatedButton(
            bar, text="Open folder", command=self._open_folder,
            bg=COLORS["bg"], fg=COLORS["primary"], variant="outlined",
            font=(F, 10, "bold"), padx=16, pady=6)
        self.open_btn.pack(side="right", padx=(0, 6))
        self.folder_lbl.pack(side="right", padx=(0, 10))

        # footer first so its space is reserved (house rule)
        foot = tk.Frame(pad, bg=COLORS["bg"])
        foot.pack(side="bottom", fill="x", pady=(10, 0))
        self.rail = tk.Canvas(foot, height=6, bg=COLORS["bg_alt"],
                              highlightthickness=0, bd=0)
        self.rail.pack(fill="x")
        self.rail.bind("<Configure>", lambda e: self._paint_rail())
        self._frac = 0.0
        row = tk.Frame(foot, bg=COLORS["bg"])
        row.pack(fill="x", pady=(8, 0))
        self.status = tk.Label(row, text="", font=(F, 9), bg=COLORS["bg"],
                               fg=COLORS["subtext"], anchor="w")
        self.status.pack(side="left", fill="x", expand=True)
        self.install_btn = AnimatedButton(
            row, text="Download & install", command=self._download_install,
            bg=ACCENT, fg=COLORS["black"], font=(F, 9, "bold"), padx=14,
            pady=6)
        self.install_btn.pack(side="right")
        self.download_btn = AnimatedButton(
            row, text="Download only", command=self._download_only,
            bg=COLORS["surface"], fg=COLORS["text"], font=(F, 9), padx=14,
            pady=6)
        self.download_btn.pack(side="right", padx=(0, 8))
        self.all_btn = AnimatedButton(
            row, text="Select all", command=self._toggle_all,
            bg=COLORS["surface"], fg=COLORS["text"], font=(F, 9), padx=12,
            pady=6)
        self.all_btn.pack(side="right", padx=(0, 8))
        Tooltip(self.install_btn, "Makes a restore point, backs up the driver "
                                  "being replaced, then installs. Needs "
                                  "administrator rights.")
        Tooltip(self.download_btn, "Saves the files to the Drivers folder "
                                   "and changes nothing on this PC.")

        self.table = DataTable(pad, _COLUMNS, row_h=52, accent=ACCENT,
                               two_line=True, on_check=self._on_check,
                               on_action=self._on_action,
                               on_select=self._on_select,
                               show_header=False)
        self.table.pack(fill="both", expand=True)
        self.table.set_empty("No scan yet",
                             "Press Scan this PC to look for drivers.",
                             glyph="🔄")
        self._refresh_folder()
        self._sync_buttons()

    # ------------------------------------------------------------ helpers
    def _post(self, fn, *a):
        d = self._dispatch
        if d is not None:
            d.post(lambda: fn(*a))
        else:                                   # tests / no dispatcher
            self.after(1, lambda: fn(*a))

    def _drivers_root(self) -> str:
        from app.drivers import folder
        return folder.configured()

    def _refresh_folder(self):
        try:
            import os as _os
            root = str(self._drivers_root())
            # just the folder name: the full path used to run into the
            # checkbox and clip. The full path stays available on hover.
            self.folder_lbl.config(
                text="\U0001F4C1  " + (_os.path.basename(root.rstrip("\\/")) or root))
            if not getattr(self, "_folder_tip", False):
                Tooltip(self.folder_lbl, root)
                self._folder_tip = True
        except Exception:
            pass

    def _say(self, text, color=None):
        self.status.config(text=text, fg=color or COLORS["subtext"])

    def _paint_rail(self):
        c = self.rail
        c.delete("all")
        w = c.winfo_width()
        if w > 4 and self._frac > 0:
            c.create_rectangle(0, 0, int(w * min(1.0, self._frac)), 6,
                               fill=ACCENT, outline="")

    def _progress(self, frac):
        self._frac = frac
        self._paint_rail()

    def _set_busy(self, busy):
        self._busy = bool(busy)
        self._sync_buttons()

    def set_run_enabled(self, enabled: bool):
        """Run-engine tab contract: Application calls this on EVERY tab when
        a run starts and ends (a tab without it raises AttributeError inside
        the loop, which silently hides the Stop button and progress bar of
        every run - the A-1/A-2 audit bug). Driver downloads and installs
        wait for a running Clean/Repair/Tweak run instead of competing with
        it for the disk, the network and the installer."""
        self._run_locked = not enabled
        self._sync_buttons()

    def _sync_buttons(self):
        have = (bool(self._checked) and not self._busy
                and not self._run_locked)
        idle = not self._busy and not self._run_locked
        for b, on in ((self.install_btn, have), (self.download_btn, have),
                      (self.scan_btn, idle),
                      (self.all_btn, bool(self._items) and idle)):
            try:
                b.set_enabled(on)
            except Exception:
                pass
        n = len(self._checked)
        try:
            self.install_btn.config_text(
                "Download & install (%d)" % n if n else "Download & install")
            self.download_btn.config_text(
                "Download only (%d)" % n if n else "Download only")
        except Exception:
            pass

    # --------------------------------------------------------------- scan
    def _scan(self):
        if self._busy:
            return
        from app.drivers import service
        self._token[0] = False
        self._seq += 1
        seq = self._seq
        self._set_busy(True)
        self._items, self._checked = [], set()
        self.table.set_empty("Scanning...", "Reading your hardware, then "
                             "asking the catalog about each device.", glyph="")
        self.table.set_rows([])
        self.header.set_state("…", COLORS["muted"])
        self.header.set_title("Scanning...")
        self.header.set_sub("")
        self._say("Reading your devices...")
        self._progress(0.0)
        full = bool(self._full.get())
        token = self._token

        def prog(i, n, name):
            self._post(self._on_scan_progress, seq, i, n, name)

        def worker():
            res = service.scan_all(progress=prog,
                                   should_stop=lambda: token[0], full=full)
            self._post(self._land_scan, seq, res)

        threading.Thread(target=worker, daemon=True).start()

    def _on_scan_progress(self, seq, i, n, name):
        if seq != self._seq:
            return
        self._progress(i / n if n else 0)
        if name:
            self._say("Checking %s  (%d of %d)" % (name, i + 1, n))

    def _land_scan(self, seq, res):
        if seq != self._seq:
            return
        self._set_busy(False)
        self._progress(0.0)
        self._items = res.get("items") or []
        self._devices = res.get("devices") or []
        err = res.get("error") or ""
        missing = sum(1 for i in self._items if i["state"] == "missing")
        updates = len(self._items) - missing
        if err and not self._items:
            self.header.set_state("!", COLORS["accent_yellow"])
            self.header.set_title("Could not finish the scan.",
                                  COLORS["accent_yellow"])
            self.header.set_sub(err)
            self.table.set_empty(
                "The online lookup did not work",
                "You can search the Microsoft Update Catalog in your browser "
                "for any device. Check your internet connection and try "
                "again.", glyph="!")
        elif not self._items:
            self.header.set_state("✓", COLORS["accent_green"])
            self.header.set_title("No missing or newer drivers found.")
            self.header.set_sub("Checked %d device%s. Newer graphics drivers "
                                "usually come from NVIDIA, AMD or Intel "
                                "directly." % (
                                    res.get("checked", 0),
                                    "" if res.get("checked") == 1 else "s"))
            self.table.set_empty("Everything checked is up to date.", "",
                                 glyph="✓")
        else:
            bits = []
            if missing:
                bits.append("%d device%s missing a driver" % (
                    missing, "" if missing == 1 else "s"))
            if updates:
                bits.append("%d driver%s can be updated" % (
                    updates, "" if updates == 1 else "s"))
            self.header.set_state(str(len(self._items)),
                                  COLORS["accent_yellow"])
            self.header.set_title(" and ".join(bits).capitalize() + ".")
            self.header.set_sub("Tick the ones you want. Download only saves "
                                "the files; Download & install also installs "
                                "them.")
        self._say("")
        self._render()

    # ------------------------------------------------------------ render
    def _gpu_rows(self):
        """Graphics drivers are a GUIDED row, not a catalog download: the
        catalog only carries what vendors published to Windows Update, which
        is usually behind NVIDIA/AMD/Intel's own current release, so for the
        GPU the honest answer is a link to the maker's page."""
        from app.tasks.repair_tasks import get_gpu_manufacturer_download_url
        rows, seen = [], set()
        for d in self._devices:
            if d.get("cls", "").lower() != "display" or d["id"] in seen:
                continue
            url = get_gpu_manufacturer_download_url(d["name"])
            if not url or d["state"] == "missing":
                continue
            seen.add(d["id"])
            vendor = ("NVIDIA" if "nvidia" in url else "Intel"
                      if "intel" in url else "AMD")
            have = "installed %s%s" % (d["version"] or "?", (
                " (%s)" % d["date"][:4]) if d["date"] else "")
            rows.append({
                "_key": "gpu:" + d["id"], "_url": url,
                "_actions": [{"id": "vendor",
                              "text": "Open %s page" % vendor}],
                "_cells": {
                    "name": {"text": d["name"], "bold": True,
                             "sub": "%s  ·  get the newest from %s" % (
                                 have, vendor)},
                    "status": {"text": "Graphics",
                               "pill": (COLORS["surface"], ACCENT)},
                    "size": {"text": ""}}})
        return rows

    def _render(self):
        rows = []
        for it in self._items:
            have = ("installed %s (%s)" % (it["installed"], (it.get("installed_date") or "")[:4] or "?")
                    if it["installed"] and it["state"] != "missing"
                    else "no driver installed")
            sub = "%s  ->  %s  ·  %s" % (
                have, it["version"], it["title"][:60])
            rows.append({
                "_key": it["update_id"], "_it": it,
                "_check": it["update_id"] in self._checked,
                "_cells": {
                    "name": {"text": it["name"], "bold": True, "sub": sub},
                    "status": {
                        "text": "Missing" if it["state"] == "missing"
                        else "Update",
                        "pill": ((COLORS["accent_red"], COLORS["text"])
                                 if it["state"] == "missing"
                                 else (COLORS["surface"], ACCENT))},
                    "size": {"text": _fmt_size(it["size"]),
                             "fg": COLORS["subtext"]},
                }})
            # The safe fallback if the in-app lookup is wrong or stale: the
            # same search in the user's browser. Shown on the selected row
            # only, so the list is not a column of identical buttons.
            if self._sel == it["update_id"]:
                rows[-1]["_actions"] = [{"id": "browser",
                                         "text": "Open in browser"}]
        rows.extend(self._gpu_rows())
        self.table.set_rows(rows)
        self._sync_buttons()

    def _on_check(self, row):
        uid = row["_key"]
        if uid in self._checked:
            self._checked.discard(uid)
        else:
            self._checked.add(uid)
        self._render()

    def _on_select(self, row):
        key = row["_key"]
        if key != self._sel and not str(key).startswith("gpu:"):
            self._sel = key
            self._render()

    def _on_action(self, row, aid):
        import webbrowser
        try:
            if aid == "vendor":
                webbrowser.open(row["_url"])
            elif aid == "browser":
                from app.drivers import catalog
                webbrowser.open(catalog.browser_url(row["_it"]["query"]))
        except Exception:
            self._say("Could not open the browser.", COLORS["accent_yellow"])

    def _toggle_all(self):
        ids = {i["update_id"] for i in self._items}
        self._checked = set() if self._checked == ids else ids
        self.all_btn.config_text("Select none" if self._checked == ids
                                 else "Select all")
        self._render()

    # --------------------------------------------------------- folder ops
    def _open_folder(self):
        try:
            os.makedirs(self._drivers_root(), exist_ok=True)
            os.startfile(self._drivers_root())
        except Exception:
            self._say("Could not open the folder.", COLORS["accent_yellow"])

    def _change_folder(self):
        path = filedialog.askdirectory(parent=self.winfo_toplevel(),
                                       title="Choose where drivers are saved")
        if not path:
            return
        from app.drivers import folder
        if not folder.writable(path):
            _themed_showinfo(self.winfo_toplevel(), "Cannot use that folder",
                             "Cleaner Tool cannot write to that folder. "
                             "Choose another.", accent=ACCENT)
            return
        try:
            from app.settings_apply import apply_settings
            apply_settings({"drivers_folder": path})
        except Exception:
            pass
        self._refresh_folder()
        self._say("Drivers will be saved to %s" % path)

    # ----------------------------------------------------- download/install
    def _chosen(self):
        return [i for i in self._items if i["update_id"] in self._checked]

    def _download_only(self):
        self._run(install=False)

    def _download_install(self):
        chosen = self._chosen()
        if not chosen:
            return
        names = "\n".join("- " + i["name"] for i in chosen[:8])
        if len(chosen) > 8:
            names += "\n- and %d more" % (len(chosen) - 8)
        if not _themed_askyesno(
                self.winfo_toplevel(), "Install %d driver%s?" % (
                    len(chosen), "" if len(chosen) == 1 else "s"),
                "%s\n\nFirst a restore point is saved and the drivers being "
                "replaced are backed up to your Drivers folder. The screen "
                "may flicker; some drivers ask for a restart." % names,
                accent=ACCENT, yes_text="Install", no_text="Go Back"):
            return
        self._run(install=True)

    def _run(self, install: bool):
        chosen = self._chosen()
        if not chosen or self._busy:
            return
        from app.drivers import service
        self._token[0] = False
        self._set_busy(True)
        token = self._token
        root = self._drivers_root()
        self._progress(0.0)
        self._say("Starting download...")
        try:
            self.scan_btn.set_enabled(False)
        except Exception:
            pass

        def dprog(i, n, name, done, total, speed):
            frac = (i + (done / total if total else 0)) / n
            self._post(self._on_dl_progress, frac, name, done, total, speed)

        def worker():
            dls = service.download_items(chosen, root, progress=dprog,
                                         should_stop=lambda: token[0])
            ok = [d for d in dls if d["ok"]]
            result = None
            if install and ok and not token[0]:
                self._post(self._say, "Saving a restore point and backing up "
                                      "the current drivers...")
                result = service.install_items(
                    ok, root, progress=lambda i, n, name: self._post(
                        self._say, "Installing %s  (%d of %d)" % (
                            name, i + 1, n)),
                    should_stop=lambda: token[0])
            self._post(self._land_run, install, dls, result)

        threading.Thread(target=worker, daemon=True).start()

    def _on_dl_progress(self, frac, name, done, total, speed):
        self._progress(frac)
        mb = done / 1048576.0
        if total:
            self._say("Downloading %s  -  %.1f of %.1f MB  (%.1f MB/s)" % (
                name, mb, total / 1048576.0, speed / 1048576.0))
        else:
            self._say("Downloading %s  -  %.1f MB" % (name, mb))

    def _land_run(self, install, dls, result):
        self._set_busy(False)
        self._progress(0.0)
        ok = [d for d in dls if d["ok"]]
        bad = [d for d in dls if not d["ok"]]
        lines = []
        if bad:
            lines.append("%d download%s failed: %s" % (
                len(bad), "" if len(bad) == 1 else "s", bad[0]["error"]))
        if not install:
            msg = "Saved %d file%s to %s" % (len(ok), "" if len(ok) == 1
                                             else "s", self._drivers_root())
            self._say(msg + ("  -  " + lines[0] if lines else ""),
                      COLORS["accent_yellow"] if bad else COLORS["accent_green"])
            return
        if result is None:
            self._say(lines[0] if lines else "Nothing was installed.",
                      COLORS["accent_yellow"])
            return
        if result.get("error"):
            self._say(result["error"], COLORS["accent_red"])
            return
        done_ok = [r for r in result["results"] if r["ok"]]
        fail = [r for r in result["results"] if not r["ok"]]
        text = "Installed %d driver%s" % (len(done_ok), "" if len(done_ok)
                                          == 1 else "s")
        if fail:
            text += "; %d failed (%s)" % (len(fail), fail[0]["detail"])
        if result.get("reboot"):
            text += ". Restart your PC to finish."
        self._say(text, COLORS["accent_yellow"] if fail
                  else COLORS["accent_green"])
        self.header.set_state("✓" if not fail else "!",
                              COLORS["accent_green"] if not fail
                              else COLORS["accent_yellow"])
        self.header.set_title(text)
        self.header.set_sub("Old drivers were backed up to %s" %
                            result.get("backup", ""))
        installed = {d["item"]["update_id"] for d, r in zip(
            ok, result["results"]) if r["ok"]}
        self._items = [i for i in self._items
                       if i["update_id"] not in installed]
        self._checked -= installed
        self._render()

    def stop(self):
        """Cancel whatever is running (called when the window closes)."""
        self._token[0] = True
