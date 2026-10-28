"""ServicesDialog - "what is Windows running in the background?"

Backed by app/svcs.py (one PowerShell read of the Service Control Manager).

Rebuilt from scratch (2026-10, second pass). The first redesign kept every
power-user control (five filter chips, four verbs, a three-way startup
switch, Undo, Windows Services, Copy, Check again) and added a detail panel
beside them. Most people open this to answer "is something from another
company running that I do not want?" - so that is now the default view:

    Header   "12 services from other companies are running"
    Search   one box
    Tabs     Other companies (default) | Running | All
    List     name + what it does, and whether it is running
    Select   the selected row grows TWO buttons, nothing else does:
               [Stop] / [Start]        and        [Startup]
             Startup opens a three-item menu: Automatic / Manual / Disabled
    Footer   Undo last change - only once there is something to undo

Windows' own services are hidden by default (they are ~95% of the list and
none of them are the user's to manage). Restart, Pause, per-service paths
and dependency lists were removed from the screen: a Stop then Start is a
restart, and the safety rules below already protect anything that has
dependants.

Safety rules are unchanged and live in app/svcs.py: never_change() blocks
core Windows services, change_refusal() is consulted again at click time, a
declined UAC prompt is reported as such, stopping asks first, and
startup-type changes are undoable.
"""
from __future__ import annotations

import threading
import tkinter as tk

from app.ui.base import _themed_askyesno
from app.ui.dialogs._tool_page import TOOL_ACCENT, ToolPage
from app.ui.theme import COLORS, TYPE, SPACE_2
from app.ui.widgets import SearchField
from app.ui.widgets.chips import ChipBar
from app.ui.widgets.datatable import DataTable

_TABS = (("third", "Other companies"), ("running", "Running"),
         ("all", "All"))

_COLUMNS = (
    {"key": "display", "title": "Service", "w": 260, "weight": 1, "min": 150,
     "sortable": False},
    {"key": "status", "title": "", "w": 96, "min": 90, "sortable": False},
)

_START_LABEL = {"Automatic": "Automatic", "Manual": "Manual",
                "Disabled": "Disabled"}


def _exe_of(raw):
    """A service's ImagePath -> a bare .exe path for icon extraction.

    Service ImagePaths carry command-line arguments (and are often
    quoted), e.g. '"C:\\...\\svchost.exe" -k netsvcs'. ExtractIconEx needs
    a literal file path, so cut at the .exe; fall back to the first token.
    """
    s = (raw or "").strip().strip('"')
    if not s:
        return ""
    i = s.lower().find(".exe")
    if i != -1:
        return s[:i + 4]
    return s.split(" ")[0]


def undo_record_safe():
    try:
        from app.svcs import undo_record
        return undo_record()
    except Exception:
        return []


class ServicesDialog(ToolPage):
    def __init__(self, parent, app):
        self.app = app
        self._stop_token = [False]
        self._scan_seq = 0
        self._data = {}
        self._selected_name = ""
        self._query = tk.StringVar()
        self._search_after = None
        self._busy = False
        # Per-service icons (user request: Services showed no program
        # icons). Extracted off the Tk thread, cached by .exe path, and
        # only ever for the rows currently on screen.
        self._icon_cache = {}       # exe path -> tk.PhotoImage
        self._icon_missed = set()   # exe paths known to have no icon
        self._icon_loading = set()  # exe paths with a worker in flight

        super().__init__(
            parent, title="Services",
            scope="Changing one needs administrator rights. Startup changes "
                  "can be undone. Stop and Start are not recorded - Start "
                  "puts it back.", with_toolbar=True)

        self._entry = SearchField(
            self.toolbar, textvariable=self._query,
            placeholder="Search services", accent=TOOL_ACCENT,
            on_change=self._on_query)
        self._entry.pack(fill="x")

        c = self.content
        self._tabs = ChipBar(c, _TABS, initial="third",
                             command=lambda _k: self._render(),
                             accent=TOOL_ACCENT)
        self._tabs.pack(anchor="w", pady=(0, SPACE_2))
        self.table = DataTable(c, _COLUMNS, row_h=46, accent=TOOL_ACCENT,
                               two_line=True, show_header=False,
                               icon_col="display",
                               on_select=self._on_select,
                               on_action=self._on_action)
        self.table.pack(fill="both", expand=True)

        self._undo_btn = self.add_button("Undo last change", self._undo)
        self._undo_btn.pack_forget()

        self.banner.set_state("…", COLORS["muted"])
        self.banner.set_title("Reading services...")
        self.on_close(self._stop)
        self._start()

    # ------------------------------------------------------------ helpers
    def _stop(self):
        self._stop_token[0] = True

    def set_busy(self, busy):
        self._busy = bool(busy)

    def _on_query(self, *_a):
        if self._search_after is not None:
            try:
                self._dlg.after_cancel(self._search_after)
            except Exception:
                pass
        try:
            self._search_after = self._dlg.after(140, self._render)
        except Exception:
            self._search_after = None

    # --------------------------------------------------------------- read
    def _start(self):
        from app.svcs import services, publish_service_findings
        token = self._stop_token
        token[0] = False
        self._scan_seq += 1
        seq = self._scan_seq
        self.set_busy(True)
        self.status.start("Reading services...")

        def worker():
            try:
                data = services()
            except Exception as exc:                 # noqa: BLE001
                data = {"services": [],
                        "error": "Could not read services: %s" % exc}
            # Off the Tk thread: the bus writes config, and a service running
            # from a user-writable folder is exactly what Health and the hub
            # should be able to surface after this window closes.
            try:
                publish_service_findings(data)
            except Exception:
                pass
            if token[0]:
                return
            self._ui(self._land, seq, data)

        threading.Thread(target=worker, daemon=True).start()

    def _land(self, seq, data):
        if seq != self._scan_seq:
            return
        from app.svcs import summarize
        self._data = data if isinstance(data, dict) else {}
        rows = self._data.get("services") or []
        fans = {}
        for r in rows:
            for dep in (r.get("deps") or []):
                key = dep.strip().lower()
                if key:
                    fans.setdefault(key, []).append(r["display"])
        for r in rows:
            r["dependents"] = fans.get(r["name"].strip().lower(), [])
        s = summarize(self._data)
        self.set_busy(False)
        self.status.stop("")
        if s.get("error"):
            self.banner.set_state("!", COLORS["accent_yellow"])
            self.banner.set_title(s["error"], COLORS["accent_yellow"])
            self.banner.set_sub("")
        else:
            third_run = len([r for r in rows
                             if r.get("running") and not r.get("microsoft")])
            self.banner.set_state("✓", COLORS["accent_green"])
            self.banner.set_title(
                "%d service%s from other companies %s running." % (
                    third_run, "" if third_run == 1 else "s",
                    "is" if third_run == 1 else "are"))
            self.banner.set_sub(
                "Windows' own services are hidden. Select one to stop it or "
                "change when it starts.")
        self._sync_undo()
        self._render()

    # ------------------------------------------------------------ render
    def _filtered(self):
        from app.svcs import filter_services
        tab = self._tabs.selected or "third"
        return filter_services(self._data.get("services") or [],
                               query=self._query.get() or "",
                               only="" if tab == "all" else tab)

    def _render(self):
        self._search_after = None
        if self._data.get("error"):
            self.table.set_empty("Nothing to show.",
                                 "Windows would not return the service list.",
                                 glyph="!")
            self.table.set_rows([])
            return
        from app.svcs import never_change
        found = self._filtered()
        rows = []
        want = []          # (row key, exe path) still needing an icon
        for r in found:
            running = bool(r.get("running"))
            desc = (r.get("description") or "").strip()
            pub = (r.get("publisher") or "").strip()
            sub = desc or pub or r["name"]
            acts = []
            if r["name"] == self._selected_name and not never_change(r["name"]):
                acts = [{"id": "toggle",
                         "text": "Stop" if running else "Start",
                         "fg": COLORS["accent_red"] if running
                         else COLORS["accent_green"]},
                        {"id": "startup",
                         "text": "Starts: %s ▾" % _START_LABEL.get(
                             r.get("start_mode"), r.get("start_mode") or "?")}]
            path = _exe_of(r.get("path"))
            disp = {"text": r.get("display") or r["name"],
                    "bold": True, "sub": sub}
            cached = self._icon_cache.get(path)
            if cached is not None:
                disp["icon"] = cached
            elif path:
                want.append((r["name"], path))
            rows.append({
                "_key": r["name"], "_svc": r, "_actions": acts,
                "_cells": {
                    "display": disp,
                    "status": {"text": "Running" if running else "Stopped",
                               "dot": (COLORS["accent_green"] if running
                                       else COLORS["muted"]),
                               "fg": COLORS["text"] if running
                               else COLORS["subtext"]},
                }})
        self.table.set_empty("Nothing matches that.",
                             "Try another search, or choose All.", glyph="?")
        self.table.set_rows(rows)
        if want:
            self._load_icons(want)
        if self._selected_name:
            self.table.select(self._selected_name, reveal=False)

    # ------------------------------------------------------------- icons
    def _load_icons(self, pairs):
        """Extract service icons on a worker (one extraction per unique
        exe path), then hand finished images to the Tk thread."""
        todo = []
        seen = set()
        for key, path in pairs:
            if (not path or path in seen or path in self._icon_cache
                    or path in self._icon_missed
                    or path in self._icon_loading):
                continue
            seen.add(path)
            self._icon_loading.add(path)
            todo.append((key, path))
        if not todo:
            return
        token = self._stop_token

        def work():
            found = []
            missed = []
            for key, path in todo:
                if token[0]:
                    return
                ppm = None
                try:
                    from app.icon_extract import icon_ppm_bytes
                    from app.ui.theme import ICON_ROW_PX, icon_bg_rgb
                    ppm = icon_ppm_bytes(path, size=ICON_ROW_PX,
                                         bg_rgb=icon_bg_rgb(COLORS["bg"]))
                except Exception:
                    ppm = None
                if ppm:
                    found.append((key, path, ppm))
                else:
                    missed.append(path)
            self._ui(self._land_icons, found, missed)

        threading.Thread(target=work, daemon=True, name="SvcIcons").start()

    def _land_icons(self, found, missed):
        for p in missed:
            self._icon_missed.add(p)
            self._icon_loading.discard(p)
        for _key, path, ppm in found:
            self._icon_loading.discard(path)
            try:
                self._icon_cache[path] = tk.PhotoImage(data=ppm)
            except Exception:
                self._icon_missed.add(path)
        if not found:
            return
        # Attach to every visible row sharing a freshly-cached path.
        imgs = {}
        try:
            for row in self.table.rows:
                p = _exe_of((row.get("_svc") or {}).get("path"))
                img = self._icon_cache.get(p)
                if img is not None:
                    imgs[row["_key"]] = img
            if imgs:
                self.table.apply_icons(imgs)
        except Exception:
            pass

    def _on_select(self, row):
        if row["_key"] != self._selected_name:
            self._selected_name = row["_key"]
            self._render()

    def _selected(self):
        for r in (self._data.get("services") or []):
            if r.get("name") == self._selected_name:
                return r
        return None

    # ------------------------------------------------------------ actions
    def _on_action(self, row, aid):
        if aid == "toggle":
            self._toggle()
        elif aid == "startup":
            self._startup_menu()

    def _toggle(self):
        from app.svcs import change_refusal
        svc = self._selected()
        if not svc or self._busy:
            return
        running = bool(svc.get("running"))
        ok, why = change_refusal(svc, self._data.get("services") or [])
        if not ok:
            self.status.stop(why, COLORS["accent_red"])
            return
        if running and not _themed_askyesno(
                self._dlg, "Stop %s?" % svc.get("display"),
                "It stops right away. Anything it has not saved may be "
                "lost. Press Start on it to turn it back on.",
                accent=TOOL_ACCENT, yes_text="Stop it", no_text="Go Back"):
            return
        action = "Stop" if running else "Start"
        self._run_change(svc, action, "%s %s..." % (action, svc.get("display")))

    def _startup_menu(self):
        svc = self._selected()
        if not svc or self._busy:
            return
        menu = tk.Menu(self._dlg, tearoff=0, bg=COLORS["surface"],
                       fg=COLORS["text"], activebackground=TOOL_ACCENT,
                       activeforeground=COLORS["black"], bd=0,
                       font=TYPE["body_medium"])
        cur = svc.get("start_mode") or "Manual"
        for mode, text in (("Automatic", "Automatic - starts with Windows"),
                           ("Manual", "Manual - starts when needed"),
                           ("Disabled", "Disabled - never starts")):
            menu.add_command(label=("✓ " if mode == cur else "    ") + text,
                             command=lambda m=mode: self._set_start(m))
        try:
            menu.tk_popup(self._dlg.winfo_pointerx(),
                          self._dlg.winfo_pointery())
        finally:
            menu.grab_release()

    def _set_start(self, nxt):
        from app.svcs import change_refusal
        svc = self._selected()
        if not svc or self._busy:
            return
        cur = svc.get("start_mode") or "Manual"
        if nxt == cur:
            return
        ok, why = change_refusal(svc, self._data.get("services") or [], nxt)
        if not ok:
            self.status.stop(why, COLORS["accent_red"])
            return
        if not _themed_askyesno(
                self._dlg, "Set %s to %s?" % (svc.get("display"), nxt),
                "It will %s.\n\nYou can undo this with 'Undo last change'."
                % {"Automatic": "start every time Windows starts",
                   "Manual": "start only when something needs it",
                   "Disabled": "never start"}[nxt],
                accent=TOOL_ACCENT, yes_text="Change it", no_text="Go Back"):
            return
        self._run_change(svc, nxt, "Changing %s..." % svc.get("display"),
                         prev_start=cur)

    def _run_change(self, svc, action, caption, prev_start=None):
        from app.svcs import apply_change
        self._busy = True
        self.status.start(caption)
        self.run_worker(apply_change, lambda res: self._land_action(res, svc, prev_start),
                        svc["name"], action)

    def _undo(self):
        from app.svcs import undo_last_change
        if self._busy or not undo_record_safe():
            return
        self._busy = True
        self.status.start("Undoing...")
        self.run_worker(undo_last_change,
                        lambda res: self._land_action(res, None, None))

    def _land_action(self, res, svc, prev_start=None):
        """Report the outcome truthfully, then re-read the real state."""
        self._busy = False
        if res.get("cancelled"):
            self.status.stop(res.get("message") or "Administrator access "
                             "was declined, so nothing changed.",
                             COLORS["accent_yellow"])
            self._sync_undo()
            return
        if not res.get("ok"):
            self.status.stop(res.get("error") or "That did not work.",
                             COLORS["accent_red"])
            self._sync_undo()
            return
        if svc and prev_start:
            try:
                from app.svcs import record_start_type
                record_start_type(svc["name"], prev_start)
            except Exception:
                pass
        self.status.stop(res.get("message") or "Done.", COLORS["accent_green"])
        self._sync_undo()
        self._start()

    def _sync_undo(self):
        try:
            if undo_record_safe():
                if not self._undo_btn.winfo_manager():
                    self._undo_btn.pack(side="right", padx=(SPACE_2, 0))
            elif self._undo_btn.winfo_manager():
                self._undo_btn.pack_forget()
        except Exception:
            pass
