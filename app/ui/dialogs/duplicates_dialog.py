"""DuplicatesDialog - "where is my disk space going, and what can go?"

Backed by app/dupes.py (size grouping, then content hashing; removals go to
the Recycle Bin).

Rebuilt from scratch (2026-10, second pass). The first redesign still had a
folder button, an "+ Another" button, a search box, a "Never delete" button,
three rows of filter chips, stat tiles, per-set Remove buttons, Copy list,
Scan again and a primary button - eleven decisions before the first result.
The task is: pick a folder, see what is wasted, clear it. So:

    1  Choose a folder                         (one big button)
    2  Scan                                    (progress, Stop)
    3  "12 sets of copies - 3.2 GB can be freed"
       a plain list; click a set to see its copies
    4  ONE button: Clean up N files

The tool picks which copy to keep. The only override is "Keep this one" on a
copy you expand. A second tab, "Largest files", lists the biggest files with
a single "Recycle" button each.

Removed from the screen (not from the product): size and type filters,
search, extra folders, Copy list. The protected-folder list is still read
from disk and still honoured by every removal - it just has no button here.

Unchanged and load-bearing: every removal goes to the Recycle Bin, one copy
of every set is always kept, protected paths are never offered, counters are
recomputed from what is left.
"""
from __future__ import annotations

import os
import threading
from tkinter import filedialog as _fd

from app.dupes import (SIZE_STEPS, is_protected, load_protected,
                       normalise_protected, promote_keeper)
from app.ui.base import _themed_askyesno
from app.ui.dialogs._tool_page import TOOL_ACCENT, ToolPage
from app.ui.theme import COLORS, SPACE_2
from app.ui.metrics import GROUP_GAP
from app.ui.widgets import AnimatedButton
from app.ui.widgets.chips import ChipBar
from app.ui.widgets.datatable import DataTable

_PHASE_TEXT = {"walking": "Looking at files",
               "hashing": "Comparing file contents"}

_COLS_SETS = (
    {"key": "name", "title": "Copies", "w": 300, "weight": 1, "min": 160,
     "sortable": False},
    {"key": "size", "title": "Frees up", "w": 110, "min": 96,
     "anchor": "e", "sortable": False},
)
_COLS_LARGE = (
    {"key": "name", "title": "Largest files", "w": 300, "weight": 1,
     "min": 160, "sort": lambda r: r["_name"].lower()},
    {"key": "size", "title": "Size", "w": 110, "min": 96, "anchor": "e",
     "sort": lambda r: r["_size"], "desc_first": True},
)


def _fmt(n):
    from app.utils import format_bytes
    return format_bytes(int(n or 0), compact=True)


def _plural(n, one, many):
    return "%d %s" % (int(n), one if int(n) == 1 else many)


def _basename(path):
    try:
        return os.path.basename(path) if path else ""
    except Exception:
        return ""


class DuplicatesDialog(ToolPage):
    def __init__(self, parent, app):
        self.app = app
        self._stop_token = [False]
        self._scan_seq = 0
        self._data = {}
        self._roots = []
        self._expanded = set()
        self._freed = 0
        self._view = "dupes"
        self._scanning = False
        self._protected = normalise_protected(load_protected())
        self._min_size = SIZE_STEPS[0][1]
        self._exts = None

        super().__init__(
            parent, title="Duplicate & Large Files",
            scope="Removals go to the Recycle Bin, so they can be undone.",
            with_toolbar=True)

        self._tabs = ChipBar(
            self.toolbar, (("dupes", "Duplicates"), ("large", "Largest files")),
            initial="dupes", command=self._on_view, accent=TOOL_ACCENT)
        self._tabs.pack(side="left")

        self.table = DataTable(self.content, _COLS_SETS, row_h=46,
                               accent=TOOL_ACCENT, two_line=True,
                               on_select=self._on_row,
                               on_action=self._on_action)
        self.table.pack(fill="both", expand=True)

        self.primary_btn = self.add_button("Clean up", self._remove_all_safe,
                                           kind="primary")
        self._folder_btn = self.add_button("Choose another folder",
                                           self._pick)
        self._stop_btn = AnimatedButton(
            self.foot_btns, text="Stop", command=self._stop_scan,
            role="warn", size="sm")

        self.on_close(self._stop)
        self._idle()

    # ------------------------------------------------------------ helpers
    def _stop(self):
        self._stop_token[0] = True

    def set_busy(self, busy):
        self._scanning = bool(busy)
        try:
            if busy and not self._stop_btn.winfo_manager():
                self._stop_btn.pack(side="right", padx=(SPACE_2, 0))
            elif not busy and self._stop_btn.winfo_manager():
                self._stop_btn.pack_forget()
            self._folder_btn.set_enabled(not busy)
        except Exception:
            pass

    def _idle(self):
        self.toolbar.pack_forget()
        self.banner.set_state("?", COLORS["muted"])
        self.banner.set_title("Find files you have more than once.")
        self.banner.set_sub("Choose a folder. Scanning only reads - it "
                            "never deletes anything.")
        self.primary_btn.pack_forget()
        self._folder_btn.pack_forget()
        self.table.set_empty("Choose a folder to start",
                             "Downloads is a good place to begin.",
                             glyph="?",
                             action=("Choose a folder", self._pick))
        self.table.set_rows([])

    def _show_main(self):
        if not self.toolbar.winfo_manager():
            self.toolbar.pack(fill="x", pady=(GROUP_GAP, 0), before=self.content)
        for b in (self.primary_btn, self._folder_btn):
            if not b.winfo_manager():
                b.pack(side="right", padx=(SPACE_2, 0))

    def _on_view(self, key):
        self._view = key
        self.table.set_columns(_COLS_SETS if key == "dupes" else _COLS_LARGE)
        self._render()
        self._paint_banner()

    # ------------------------------------------------------------ folders
    def _pick(self):
        path = _fd.askdirectory(
            parent=self._dlg,
            title="Choose the folder to look for duplicate files")
        if not path:
            return
        self._roots = [path]
        self._expanded = set()
        self._freed = 0
        self._protected = normalise_protected(load_protected())
        self._show_main()
        self._start()

    # --------------------------------------------------------------- scan
    def _stop_scan(self):
        self._stop_token[0] = True
        self.status.stop("Scan stopped.", COLORS["accent_yellow"])
        self.set_busy(False)
        self._sync_primary()

    def _start(self):
        from app.dupes import scan
        token = self._stop_token
        token[0] = False
        self._scan_seq += 1
        seq = self._scan_seq
        roots = list(self._roots)
        self.set_busy(True)
        self.primary_btn.set_enabled(False)
        self.status.start("Looking at files...")
        self.banner.set_state("…", COLORS["muted"])
        self.banner.set_title("Scanning...")
        self.banner.set_sub(roots[0])
        self.table.set_empty("Scanning...", "This can take a minute for a "
                             "big folder.", glyph="")
        self.table.set_rows([])

        def report(phase, done, total):
            if token[0]:
                return
            label = _PHASE_TEXT.get(phase, "Working")
            if total and total > 0:
                # AUDIT TV-006: hashing is the long phase and it already knows
                # how many files it has to do - show that, not a loop.
                self._ui(self.status.progress, done, total, label, "files")
            else:
                self._ui(self.status.set_text,
                         "%s: %s files..." % (label, format(done, ",")))

        def worker():
            try:
                data = scan(roots, min_size=self._min_size,
                            should_stop=lambda: token[0], progress=report,
                            extensions=self._exts)
            except Exception as exc:                  # noqa: BLE001
                data = {"groups": [], "large": [], "all_groups": [],
                        "stats": {}, "error": "Could not scan: %s" % exc}
            if token[0]:
                return
            self._ui(self._land, seq, data)

        threading.Thread(target=worker, daemon=True).start()

    def _land(self, seq, data):
        if seq != self._scan_seq:
            return
        self._data = data if isinstance(data, dict) else {}
        self.set_busy(False)
        self.status.stop("")
        self._publish_finding()
        self._paint_banner()
        self._render()
        self._sync_primary()

    def _publish_finding(self):
        """Carry this scan's result to Health and the Tools hub (AUDIT TX-002).

        An errored scan must not clear a real warning from another day, so it
        is the one path that does not publish. Everything else is a completed
        read and owns the `duplicates.` namespace.
        """
        if self._data.get("error"):
            return
        try:
            from app.dupes import publish_reclaim_finding
            publish_reclaim_finding(self._data.get("stats") or {})
        except Exception:
            pass

    def _paint_banner(self):
        from app.dupes import verdict_for
        st = self._data.get("stats") or {}
        if self._data.get("error"):
            self.banner.set_state("!", COLORS["accent_yellow"])
            self.banner.set_title(self._data["error"], COLORS["accent_yellow"])
            self.banner.set_sub("")
            return
        if self._view == "large":
            n = len(self._data.get("large") or [])
            self.banner.set_state("%d" % n if n else "–",
                                  COLORS["accent_sky"])
            self.banner.set_title("The %d largest files in this folder." % n
                                  if n else "No large files found.")
            self.banner.set_sub("Send one to the Recycle Bin if you do not "
                                "need it.")
            return
        groups = self._data.get("groups") or []
        if not groups:
            self.banner.set_state("✓", COLORS["accent_green"])
            self.banner.set_title(verdict_for(self._data))
            self.banner.set_sub("")
            return
        self.banner.set_state(str(len(groups)), COLORS["accent_yellow"])
        self.banner.set_title("%s of copies. %s can be freed." % (
            _plural(len(groups), "set", "sets"),
            _fmt(int(st.get("wasted") or 0))))
        sub = "One copy of each is always kept."
        if st.get("truncated") or st.get("hash_capped"):
            sub += " The folder is huge, so results are partial."
        self.banner.set_sub(sub)

    # ------------------------------------------------------------ removal
    def _removable(self, group):
        from app.dupes import removable_from
        return removable_from(group, protected=self._protected)

    def _safe_groups(self):
        return [g for g in (self._data.get("groups") or [])
                if self._removable(g)]

    def _sync_primary(self):
        """The batch button's label and state are decided in one place, so it
        can never say 'Clean up 3' with nothing left to clean."""
        n = 0
        try:
            n = sum(len(self._removable(g)) for g in self._safe_groups())
        except Exception:
            pass
        on = self._view == "dupes"
        try:
            self.primary_btn.config_text(
                "Clean up %s" % _plural(n, "file", "files") if n
                else "Nothing to clean up")
            self.primary_btn.set_enabled(bool(n) and on and not self._scanning)
        except Exception:
            pass

    def _recount(self):
        st = self._data.setdefault("stats", {})
        groups = self._data.get("all_groups") or []
        st["groups"] = len(groups)
        st["copies"] = sum(len(g.get("copies") or []) for g in groups)
        st["wasted"] = sum(int(g.get("wasted") or 0) for g in groups)
        return st

    def _drop_groups(self, digests):
        gone = set(digests)
        for k in ("groups", "all_groups"):
            self._data[k] = [g for g in (self._data.get(k) or [])
                             if g.get("digest") not in gone]
        return self._recount()

    def _recycle_job(self, doomed, caption, done_cb):
        from app.dupes import recycle
        self.set_busy(True)
        self.status.start(caption)
        token = self._stop_token
        token[0] = False

        def rep(done, total):
            self._ui(self.status.set_text, "Moved %s of %s..." % (
                format(done, ","), format(total, ",")))

        def worker():
            res = recycle(doomed, should_stop=lambda: token[0], progress=rep,
                          protected=self._protected)
            self._ui(done_cb, res)

        threading.Thread(target=worker, daemon=True).start()

    def _remove_all_safe(self):
        safe = self._safe_groups()
        doomed = [p for g in safe for p in self._removable(g)]
        if not doomed:
            return
        amount = sum(int(g.get("wasted") or 0) for g in safe)
        if not _themed_askyesno(
                self._dlg, "Move %d files to the Recycle Bin?" % len(doomed),
                "This frees about %s.\n\nOne copy of each is kept, and you "
                "can undo all of this from the Recycle Bin." % _fmt(amount),
                accent=TOOL_ACCENT, yes_text="Clean up", no_text="Go Back"):
            return
        keep = {g.get("digest") for g in safe}
        self._recycle_job(doomed, "Moving files to the Recycle Bin...",
                          lambda res, k=keep: self._land_remove(res, k))

    def _land_remove(self, res, keep):
        moved = int(res.get("moved") or 0)
        self._freed += int(res.get("freed") or 0)
        self.set_busy(False)
        self._drop_groups(keep)
        # Cleaning can take the total below the threshold; the bus should stop
        # recommending a tool whose problem was just solved.
        self._publish_finding()
        if res.get("cancelled"):
            self.status.stop("Stopped. %s freed so far." % _fmt(self._freed),
                             COLORS["accent_yellow"])
        elif not moved:
            self.status.stop(res.get("error") or "Nothing was moved.",
                             COLORS["accent_red"])
        else:
            self.status.stop("Freed %s. Everything is in the Recycle Bin."
                             % _fmt(self._freed), COLORS["accent_green"])
        self._paint_banner()
        self._render()
        self._sync_primary()

    def _recycle_large(self, f):
        path = f.get("path") or ""
        if not path:
            return
        if is_protected(path, self._protected):
            self.status.stop("That file is on your never-delete list.",
                             COLORS["accent_yellow"])
            return
        if not _themed_askyesno(
                self._dlg, "Move to the Recycle Bin?",
                "%s\n\n%s - you can put it back from the Recycle Bin."
                % (path, _fmt(f.get("size") or 0)),
                accent=TOOL_ACCENT, yes_text="Recycle", no_text="Go Back"):
            return
        self._recycle_job([path], "Moving to the Recycle Bin...",
                          lambda res, ff=f: self._land_large(res, ff))

    def _land_large(self, res, f):
        self.set_busy(False)
        if int(res.get("moved") or 0):
            self._freed += int(res.get("freed") or f.get("size") or 0)
            self._data["large"] = [x for x in (self._data.get("large") or [])
                                   if x.get("path") != f.get("path")]
            self.status.stop("Freed %s." % _fmt(self._freed),
                             COLORS["accent_green"])
        else:
            self.status.stop(res.get("error") or "Could not move it - it may "
                             "be open in another program.",
                             COLORS["accent_red"])
        self._paint_banner()
        self._render()

    def _keep_one(self, group, path):
        promote_keeper(group, path)
        self._sync_primary()
        self._render()

    # ------------------------------------------------------------ table io
    def _on_row(self, row):
        digest = row.get("_digest")
        if digest is not None and row.get("_group") and row.get("_set"):
            if digest in self._expanded:
                self._expanded.discard(digest)
            else:
                self._expanded.add(digest)
            self._render(select=row["_key"])

    def _on_action(self, row, aid):
        if aid == "keep":
            self._keep_one(row["_group"], row["_path"])
        elif aid == "recycle":
            self._recycle_large(row["_file"])

    # ------------------------------------------------------------ render
    def _render(self, select=None):
        if not self._roots:
            return
        if self._view == "large":
            rows = self._rows_large()
        else:
            rows = self._rows_groups()
        if not rows and not self._scanning:
            self.table.set_empty(
                "Nothing to show",
                "No duplicate files were found here." if self._view == "dupes"
                else "No large files found.", glyph="✓")
        self.table.set_rows(rows)
        if select is not None:
            self.table.select(select, reveal=False)

    def _rows_groups(self):
        rows = []
        for g in (self._data.get("groups") or []):
            copies = g.get("copies") or []
            doomed = self._removable(g)
            digest = g.get("digest")
            opened = digest in self._expanded
            rows.append({
                "_key": "g:%s" % digest, "_digest": digest, "_group": g,
                "_set": True,
                "_chev": "open" if opened else "closed",
                "_cells": {
                    "name": {"text": _basename(g.get("keep")) or "unnamed",
                             "bold": True,
                             "sub": "%d copies - keeping the %s one" % (
                                 len(copies), g.get("reason") or "first")},
                    "size": {"text": _fmt(g.get("wasted") or 0),
                             "fg": COLORS["accent_green"] if doomed
                             else COLORS["subtext"], "bold": True},
                }})
            if not opened:
                continue
            for c in copies:
                is_keep = bool(c.get("keep"))
                path = c.get("path") or ""
                rows.append({
                    "_key": "c:%s" % path, "_depth": 1, "_path": path,
                    "_group": g,
                    "_actions": [] if is_keep else [
                        {"id": "keep", "text": "Keep this one"}],
                    "_cells": {
                        "name": {"text": ("Keeping: " if is_keep
                                          else "Removing: ")
                                 + (_basename(path) or "?"),
                                 "sub": path, "mid": True,
                                 "dot": COLORS["accent_green"] if is_keep
                                 else COLORS["muted"]},
                        "size": {"text": "", "fg": COLORS["subtext"]},
                    }})
        return rows

    def _rows_large(self):
        rows = []
        for f in (self._data.get("large") or []):
            path = f.get("path") or ""
            prot = is_protected(path, self._protected)
            rows.append({
                "_key": "f:%s" % path, "_path": path, "_file": f,
                "_name": _basename(path), "_size": int(f.get("size") or 0),
                "_actions": [] if prot else [
                    {"id": "recycle", "text": "Recycle",
                     "fg": COLORS["accent_red"]}],
                "_cells": {
                    "name": {"text": _basename(path) or "?", "bold": True,
                             "sub": path, "mid": True},
                    "size": {"text": _fmt(f.get("size") or 0), "bold": True},
                }})
        return rows
