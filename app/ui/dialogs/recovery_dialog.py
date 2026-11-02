"""RecoveryDialog — the per-item half of Safety & Undo (U-002).

The Safety overview answers "what kinds of recovery exist, and how many?".
This dialog answers the next question: "which one, exactly?".

Every row is one recoverable thing this app recorded:

    Applied tweaks     one revertable tweak, undone through the Tweak runner
    Service changes    one recorded start type, put back with `svcs.undo_change`
    Startup items      one item this app disabled, re-enabled through Startup Manager
    Quarantined files  one moved file, restored with `quarantine.restore_entry`

The dialog owns no recovery logic. It reads the same records the overview
reads and calls the same single-item helpers, so a row here cannot promise
something the owning module cannot do. Refresh re-reads; every action asks
before it writes and reports what the engine actually said.
"""
from __future__ import annotations

import os
import tkinter as tk

from app.ui.base import _themed_askyesno
from app.ui.dialogs._tool_page import ToolPage
from app.ui.metrics import COMPACT_INSET, GROUP_GAP, ROW_PAD, TEXT_NUDGE
from app.ui.theme import COLORS, RADIUS_SURFACE, SPACE_3, TYPE
from app.ui.widgets.wrap import fit_wrap
from app.ui.widgets import FindingRow, RoundedCard

_ELIGIBLE_KINDS = ("tweaks", "services", "startup", "quarantine")


def _short(path, limit=88):
    path = str(path or "")
    return path if len(path) <= limit else "…" + path[-(limit - 1):]


class RecoveryDialog(ToolPage):
    def __init__(self, parent, app):
        self.app = app
        self._busy = False
        self._stop_token = [False]
        super().__init__(
            parent, title="Recovery list",
            scope="One row per recorded recovery. Each button puts exactly "
                  "that item back; nothing else is touched.",
            scroll=True)
        self.refresh_btn = self.add_button("Refresh", self._start)
        self.add_button("Close", self.close)
        self.on_close(self._stop)
        self._start()

    # ------------------------------------------------------------- state
    def _stop(self):
        self._stop_token[0] = True

    def set_busy(self, busy):
        self._busy = bool(busy)
        try:
            self.refresh_btn.set_enabled(not self._busy)
        except Exception:
            pass

    def _start(self):
        self._stop_token[0] = False
        self.set_busy(True)
        self.status.start("Reading recovery records…")
        token = self._stop_token
        self.run_worker(self._gather,
                        lambda data: None if token[0] else self._land(data))

    @staticmethod
    def _gather():
        out = {"tweaks": [], "services": [], "startup": [], "quarantine": [],
               "quarantine_bytes": 0}
        try:
            from app import tweak_center
            for task in tweak_center.applied_tweak_tasks():
                out["tweaks"].append({
                    "key": getattr(task, "key", ""),
                    "label": getattr(task, "label", None) or getattr(
                        task, "key", "Tweak"),
                })
        except Exception:
            pass
        try:
            from app import svcs
            for rec in reversed(svcs.undo_record()):
                if isinstance(rec, dict) and rec.get("name") and rec.get("start"):
                    out["services"].append({
                        "name": str(rec.get("name")),
                        "start": str(rec.get("start")),
                    })
        except Exception:
            pass
        try:
            from app.config_persist import get_startup_disabled
            for rec in get_startup_disabled():
                if isinstance(rec, dict) and rec.get("name"):
                    out["startup"].append({
                        "source": str(rec.get("source") or ""),
                        "name": str(rec.get("name")),
                        "command": str(rec.get("command") or ""),
                    })
        except Exception:
            pass
        try:
            from app import quarantine
            root = quarantine.default_root()
            for rec in reversed(quarantine.list_entries(root)):
                if not isinstance(rec, dict) or not rec.get("src"):
                    continue
                name = str(rec.get("dst") or "")
                try:
                    size = os.path.getsize(os.path.join(
                        quarantine._files_dir(root), name))
                except OSError:
                    size = 0
                out["quarantine"].append({
                    "src": str(rec.get("src")),
                    "dst": name,
                    "size": size,
                })
                out["quarantine_bytes"] += size
        except Exception:
            pass
        return out

    # ------------------------------------------------------------- paint
    def _land(self, data):
        self.set_busy(False)
        for child in list(self.content.winfo_children()):
            try:
                child.destroy()
            except Exception:
                pass
        total = sum(len(data.get(k) or []) for k in _ELIGIBLE_KINDS)
        self.banner.set_state("↩", COLORS["accent_green"])
        self.banner.set_title(
            "%d recoverable item%s" % (total, "" if total == 1 else "s"))
        self.banner.set_sub(
            "Pick one to put just that item back. The overview stays a "
            "summary; this list is the detail.")
        self.status.stop(
            "Empty list is a real answer: nothing this app changed is "
            "waiting to be put back." if total == 0 else
            "Read %d record%s." % (total, "" if total == 1 else "s"),
            COLORS["subtext"])

        self._section(
            "Applied tweaks", data.get("tweaks") or [],
            "No revertable tweaks are recorded as applied.",
            self._tweak_row)
        self._section(
            "Service changes", data.get("services") or [],
            "No service startup-type change is waiting to be undone.",
            self._service_row)
        self._section(
            "Startup items", data.get("startup") or [],
            "This app has not disabled a startup item.",
            self._startup_row)
        q = data.get("quarantine") or []
        q_note = ("No files are held in the quarantine bin." if not q else
                  "Files moved aside instead of deleted. "
                  "%s in the bin; emptying it is what reclaims the space."
                  % _size_text(data.get("quarantine_bytes") or 0))
        self._section("Quarantined files", q, q_note, self._quarantine_row)

    def _section(self, title, items, empty_text, row_builder):
        card = RoundedCard(self.content, fill=COLORS["surface"],
                           bg=COLORS["bg"], radius=RADIUS_SURFACE)
        card.pack(fill="x", padx=TEXT_NUDGE, pady=COMPACT_INSET)
        body = card.body
        head = tk.Frame(body, bg=COLORS["surface"])
        head.pack(fill="x", padx=SPACE_3, pady=(ROW_PAD, 0))
        tk.Label(head, text=title, font=TYPE["label_large"],
                 bg=COLORS["surface"], fg=COLORS["text"],
                 anchor="w").pack(side="left")
        tk.Label(head, text=str(len(items)), font=TYPE["caption"],
                 bg=COLORS["surface"], fg=COLORS["muted"],
                 anchor="e").pack(side="right")
        if not items:
            _lbl = tk.Label(body, text=empty_text, font=TYPE["caption"],
                            bg=COLORS["surface"], fg=COLORS["subtext"],
                            anchor="w", justify="left", wraplength=0)
            _lbl.pack(fill="x", padx=SPACE_3,
                      pady=(TEXT_NUDGE, GROUP_GAP))
            fit_wrap(_lbl, body, reserve=2 * SPACE_3)
            return
        rows = tk.Frame(body, bg=COLORS["surface"])
        rows.pack(fill="x", padx=SPACE_3, pady=(TEXT_NUDGE, GROUP_GAP))
        for item in items:
            try:
                row_builder(rows, item)
            except Exception:
                continue

    def _row(self, parent, title, evidence, label, command, severity="info"):
        row = FindingRow(parent, title=title, evidence=evidence,
                         severity=severity, action_label=label,
                         on_action=command, bg=COLORS["surface"], wrap=540)
        row.pack(fill="x", pady=(0, TEXT_NUDGE))
        return row

    def _tweak_row(self, parent, item):
        self._row(parent, item.get("label") or item.get("key") or "Tweak",
                  "Applied and revertable. Undo puts the recorded values back.",
                  "Undo", lambda k=item.get("key"): self._undo_tweak(k))

    def _service_row(self, parent, item):
        name = item.get("name") or "Service"
        self._row(parent, name,
                  "Startup type will return to %s." % (item.get("start") or "?"),
                  "Undo", lambda n=name, s=item.get("start"):
                  self._undo_service(n, s))

    def _startup_row(self, parent, item):
        name = item.get("name") or "Startup item"
        source = item.get("source") or ""
        self._row(
            parent, name,
            "Held by this app (%s). Re-enable restores the exact startup "
            "entry." % source.replace("_", " "),
            "Re-enable",
            lambda s=source, n=name: self._reenable_startup(s, n))

    def _quarantine_row(self, parent, item):
        src = item.get("src") or ""
        self._row(parent, os.path.basename(src) or "File",
                  "%s · %s" % (_size_text(item.get("size") or 0),
                               _short(src)),
                  "Restore", lambda e=dict(item): self._restore_file(e))

    # ------------------------------------------------------------ actions
    def _run_action(self, label, fn, land):
        if self._busy:
            return
        self.set_busy(True)
        self.status.start(label)
        self.run_worker(fn, land)

    def _undo_tweak(self, key):
        try:
            from app import tweak_center
            task = tweak_center.task_for_key(key)
        except Exception:
            task = None
        if task is None or getattr(task, "revert", None) is None:
            self.status.stop("That tweak can no longer be undone here.",
                             COLORS["accent_yellow"])
            return
        label = getattr(task, "label", key)
        if not _themed_askyesno(self._dlg, "Undo this tweak?",
                                "Put back only “%s”? Every other applied "
                                "tweak stays as it is." % label,
                                accent=COLORS["accent_green"],
                                yes_text="Undo it", no_text="Keep it"):
            return
        try:
            self.close()
        except Exception:
            pass
        try:
            self.app.run_tasks("Tweak", [task], mode="revert")
        except Exception as exc:
            try:
                self.app.log("! Could not undo tweak: %r" % (exc,))
            except Exception:
                pass

    def _undo_service(self, name, start):
        if not _themed_askyesno(
                self._dlg, "Undo this service change?",
                "Set %s back to %s? Windows may ask for Administrator "
                "rights." % (name, start),
                accent=COLORS["accent_green"], yes_text="Undo it",
                no_text="Cancel"):
            return

        def work():
            from app import svcs
            return svcs.undo_change({"name": name, "start": start})

        def land(res):
            self.set_busy(False)
            if not isinstance(res, dict):
                self.status.stop("The service change could not be undone.",
                                 COLORS["accent_red"])
            elif res.get("ok"):
                self.status.stop("Restored %s to %s." % (name, start),
                                 COLORS["accent_green"])
                self._start()
            elif res.get("cancelled"):
                self.status.stop("Elevation was cancelled; the record is kept.",
                                 COLORS["accent_yellow"])
            else:
                self.status.stop(res.get("error") or res.get("message") or
                                 "The service change could not be undone.",
                                 COLORS["accent_red"])

        self._run_action("Restoring %s…" % name, work, land)

    def _reenable_startup(self, source, name):
        if not _themed_askyesno(
                self._dlg, "Re-enable this startup item?",
                "%s will run at sign-in again. The exact entry this app "
                "saved is restored." % name,
                accent=COLORS["accent_green"], yes_text="Re-enable",
                no_text="Leave off"):
            return

        def work():
            from app.startup_manager import set_item_enabled
            return set_item_enabled("%s:%s" % (source, name), True)

        def land(res):
            self.set_busy(False)
            ok = bool(res and res[0])
            msg = (res[1] if res and len(res) > 1 else "")
            self.status.stop(msg or ("Re-enabled." if ok else
                                     "Could not re-enable it."),
                             COLORS["accent_green"] if ok
                             else COLORS["accent_red"])
            if ok:
                self._start()

        self._run_action("Re-enabling %s…" % name, work, land)

    def _restore_file(self, entry):
        src = entry.get("src") or "its original location"
        if not _themed_askyesno(
                self._dlg, "Restore this file?",
                "Move it back to %s? Anything already at that path is left "
                "untouched." % _short(src, 240),
                accent=COLORS["accent_green"], yes_text="Restore",
                no_text="Leave it"):
            return

        def work():
            from app import quarantine
            return quarantine.restore_entry(entry)

        def land(res):
            self.set_busy(False)
            ok = bool(res and res[0])
            msg = (res[1] if res and len(res) > 1 else "")
            self.status.stop(msg or ("Restored." if ok else
                                     "Could not restore the file."),
                             COLORS["accent_green"] if ok
                             else COLORS["accent_red"])
            if ok:
                self._start()

        self._run_action("Restoring the file…", work, land)


def _size_text(n):
    try:
        n = float(n or 0)
    except (TypeError, ValueError):
        n = 0.0
    if n < 1024:
        return "%d B" % int(n)
    for unit in ("KB", "MB", "GB", "TB"):
        n /= 1024.0
        if n < 1024 or unit == "TB":
            return ("%.1f %s" % (n, unit)) if n < 10 else ("%.0f %s" % (n, unit))
    return "0 B"
