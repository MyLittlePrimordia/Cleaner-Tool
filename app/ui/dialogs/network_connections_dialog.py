"""NetworkConnectionsDialog - "what is my PC talking to, and which program
is doing it?"

Backed by app/netconn.py, which reads Windows' own TCP/UDP tables.

Rebuilt from scratch (2026-10, second pass). Before: four stat tiles, three
toggle switches, a search box, a four-column table, a detail panel with three
sections and two buttons. Now the question gets one list:

    Header   "11 programs are online."
    Search   one box
    List     program name + how many connections, and who it is talking to
    Select   the selected row shows where it connects (one line) and grows
             ONE button: End program (never for Windows' own programs)
    Footer   Show Windows' own / Hide Windows' own, and Check again

Gone from the screen: Auto-refresh, Hide idle (idle programs are simply not
listed), Copy list, PIDs, ports, file paths, stat counters.

Rules carried over: grouped by program not PID; Windows' own processes
hidden by default; "and N more" is said out loud when the sampler capped the
list; nothing is ever labelled "suspicious" - the connection tables cannot
support that judgement and a tool that cries wolf trains people to ignore
it. End program goes through the same refusal guards File Lock Finder uses.
"""
from __future__ import annotations

import threading
import tkinter as tk

from app.ui.base import _themed_askyesno
from app.ui.dialogs._tool_page import TOOL_ACCENT, ToolPage
from app.ui.theme import COLORS, TYPE, SPACE_2
from app.ui.widgets import SearchField
from app.ui.widgets.datatable import DataTable

_COLUMNS = (
    {"key": "name", "title": "", "w": 260, "weight": 1, "min": 150,
     "sortable": False},
    {"key": "state", "title": "", "w": 120, "min": 100, "sortable": False},
)


def verdict_for(summary: dict) -> str:
    """The headline sentence."""
    if summary.get("error"):
        return summary["error"]
    n = int(summary.get("user_apps") or 0)
    if n <= 0:
        return "Nothing is connected right now."
    return "1 program is online." if n == 1 else "%d programs are online." % n


class NetworkConnectionsDialog(ToolPage):
    def __init__(self, parent, app):
        self.app = app
        self._stop_token = [False]
        self._scan_seq = 0
        self._entries = {}
        self._data = {}
        self._summary = {}
        self._selected_key = None
        self._show_system = False
        self._query = tk.StringVar()
        self._search_after = None
        # Per-program icons (user request: Network Connections showed no
        # program icons). Extracted off the Tk thread, cached by exe path.
        self._icon_cache = {}
        self._icon_missed = set()
        self._icon_loading = set()

        super().__init__(
            parent, title="Network Connections",
            scope="Lists what is connected. It cannot tell you whether that "
                  "is expected.", with_toolbar=True)

        self._entry = SearchField(
            self.toolbar, textvariable=self._query,
            placeholder="Search programs", accent=TOOL_ACCENT,
            on_change=self._on_query)
        self._entry.pack(fill="x")

        self._detail = tk.Label(self.content, text="", font=TYPE["caption"],
                                bg=COLORS["bg"], fg=COLORS["subtext"],
                                anchor="w", justify="left", bd=0)
        self._detail.pack(side="bottom", fill="x", pady=(SPACE_2, 0))
        self.table = DataTable(self.content, _COLUMNS, row_h=46,
                               accent=TOOL_ACCENT, two_line=True,
                               show_header=False, icon_col="name",
                               on_select=self._on_select,
                               on_action=self._on_action)
        self.table.pack(fill="both", expand=True)

        self.refresh_btn = self.add_button("Check again", self._refresh)
        self.refresh_btn.set_enabled(False)
        self._sys_btn = self.add_button("Show Windows' own", self._toggle_sys)

        self.banner.set_state("…", COLORS["muted"])
        self.banner.set_title("Checking connections...")
        self.on_close(self._stop)
        self._start()

    # ------------------------------------------------------------ helpers
    def _stop(self):
        self._stop_token[0] = True

    def set_busy(self, busy):
        try:
            self.refresh_btn.set_enabled(not busy)
        except Exception:
            pass

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

    def _toggle_sys(self):
        self._show_system = not self._show_system
        self._sys_btn.config_text("Hide Windows' own" if self._show_system
                                  else "Show Windows' own")
        self._render()

    # --------------------------------------------------------------- read
    def _refresh(self):
        if not self._stop_token[0]:
            self._start()

    def _start(self):
        from app.netconn import connections
        token = self._stop_token
        token[0] = False
        self._scan_seq += 1
        seq = self._scan_seq
        self.set_busy(True)
        self.status.start("Reading connections...")

        def worker():
            try:
                data = connections()
            except Exception as exc:            # noqa: BLE001
                data = {"error": "Could not read connections: %s" % exc}
            if token[0]:
                return
            self._ui(self._land, seq, data)

        threading.Thread(target=worker, daemon=True).start()

    def _land(self, seq, data):
        if seq != self._scan_seq:
            return
        from app.netconn import META, summarize
        self._data = data if isinstance(data, dict) else {}
        self._summary = s = summarize(self._data)
        self.set_busy(False)
        self.status.stop("")
        if s.get("error"):
            self.banner.set_state("!", COLORS["accent_yellow"])
            self.banner.set_title(s["error"], COLORS["accent_yellow"])
            self.banner.set_sub("")
        else:
            n = int(s.get("user_apps") or 0)
            self.banner.set_state("✓" if n else "–",
                                  COLORS["accent_green"] if n
                                  else COLORS["muted"])
            self.banner.set_title(verdict_for(s))
            meta = self._data.get(META) or {}
            bits = []
            hidden = int(s.get("hidden") or 0)
            if hidden:
                bits.append("%d Windows program%s hidden." % (
                    hidden, "" if hidden == 1 else "s"))
            if meta.get("ipv6_unreadable"):
                bits.append("IPv6 could not be read, so only IPv4 is "
                            "shown.")
            self.banner.set_sub(" ".join(bits))
        self._render()

    # ------------------------------------------------------------ render
    def _visible(self):
        from app.netconn import _rows_only
        q = (self._query.get() or "").strip().lower()
        entries = [s for s in _rows_only(self._data).values()
                   if (self._show_system or not s["system"])
                   and (s["active"] or s["listening"])]
        if q:
            entries = [s for s in entries
                       if q in (s["name"] or "").lower()
                       or q in (s.get("exe") or "").lower()
                       or any(q in ep.lower() for ep in s["endpoints"])]
        entries.sort(key=lambda s: (-s["active"], -s["listening"],
                                    (s["name"] or "").lower()))
        return entries

    def _render(self):
        self._search_after = None
        if self._data.get("error"):
            self.table.set_empty("Nothing to show.",
                                 "Windows would not return the connection "
                                 "table.", glyph="!")
            self.table.set_rows([])
            return
        entries = self._visible()
        self._entries = {}
        rows = []
        want = []          # (row key, exe path) still needing an icon
        for s in entries:
            key = s.get("exe") or str(id(s))
            self._entries[key] = s
            if s["active"]:
                doing = "%d connected" % s["active"]
                dot = COLORS["accent_green"]
            else:
                doing, dot = "Listening", COLORS["accent_sky"]
            eps = list(s.get("endpoints") or [])
            first = eps[0].split(" ")[0] if eps else "waiting for a connection"
            sub = first + ("  and %d more" % (len(eps) - 1 + int(
                s.get("omitted") or 0)) if len(eps) > 1 else "")
            acts = []
            if key == self._selected_key and not s["system"] and s.get("pids"):
                acts = [{"id": "end", "text": "End program",
                         "fg": COLORS["accent_red"]}]
            # Only treat the record's exe as a real path when it is one -
            # netconn falls back to "PID 1234" when it cannot resolve it.
            path = s.get("exe") or ""
            if path and not path.lower().endswith(".exe") and "\\" not in path:
                path = ""
            name_cell = {"text": s["name"], "bold": True, "sub": sub}
            cached = self._icon_cache.get(path)
            if cached is not None:
                name_cell["icon"] = cached
            elif path:
                want.append((key, path))
            rows.append({
                "_key": key, "_s": s, "_actions": acts,
                "_cells": {
                    "name": name_cell,
                    "state": {"text": doing, "dot": dot},
                }})
        q = (self._query.get() or "").strip()
        self.table.set_empty(
            "Nothing matches." if q else "No connections.",
            "Try a different search." if q else
            "Nothing is using the network right now.", glyph="?")
        self.table.set_rows(rows)
        if want:
            self._load_icons(want)
        if self._selected_key:
            self.table.select(self._selected_key, reveal=False)
        self._paint_detail()

    # ------------------------------------------------------------- icons
    def _load_icons(self, pairs):
        """Extract program icons on a worker, one extraction per unique
        exe path, then hand finished images to the Tk thread."""
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

        threading.Thread(target=work, daemon=True, name="NetIcons").start()

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
        imgs = {}
        try:
            for row in self.table.rows:
                p = (row.get("_s") or {}).get("exe") or ""
                img = self._icon_cache.get(p)
                if img is not None:
                    imgs[row["_key"]] = img
            if imgs:
                self.table.apply_icons(imgs)
        except Exception:
            pass

    def _paint_detail(self):
        s = self._entries.get(self._selected_key)
        if not s:
            self._detail.config(text="")
            return
        eps = [e.replace(" ", "  ") for e in (s.get("endpoints") or [])]
        text = "Connected to: " + ", ".join(eps[:4]) if eps else \
            "Not connected to anything right now."
        more = len(eps) - 4 + int(s.get("omitted") or 0)
        if more > 0:
            text += "  and %d more" % more
        ports = s.get("listen_ports") or []
        if ports:
            text += "\nWaiting for connections on port %s." % ", ".join(
                str(p) for p in ports[:4])
        self._detail.config(text=text, wraplength=max(
            200, self.content.winfo_width() - 8))

    def _on_select(self, row):
        if row["_key"] != self._selected_key:
            self._selected_key = row["_key"]
            self._render()

    # ------------------------------------------------------------ actions
    def _on_action(self, row, aid):
        if aid == "end":
            self._end(row["_s"])

    def _end(self, s):
        from app.filelocks import close_program
        pids = [int(p) for p in (s.get("pids") or [])]
        if not pids or s["system"]:
            return
        name = s["name"] or "this program"
        if not _themed_askyesno(
                self._dlg, "End %s?" % name,
                "%s will be ended immediately.\n\nAnything it has not saved "
                "is lost." % name,
                accent=TOOL_ACCENT, yes_text="End it", no_text="Go Back"):
            return
        self.status.start("Ending %s..." % name)

        self.run_worker(lambda: [close_program(p, True) for p in pids],
                        lambda results: self._land_end(name, results))

    def _land_end(self, name, results):
        bad = [r for r in results if not r.get("ok")]
        if not bad:
            self.status.stop("Ended %s." % name, COLORS["accent_green"])
        else:
            self.status.stop(bad[0].get("message") or "Could not end it.",
                             COLORS["accent_red"])
        self._start()
