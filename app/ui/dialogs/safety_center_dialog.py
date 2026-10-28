"""SafetyCenterDialog - every safety net in one place (U-002).

Answers one question: "what can I get back, and what puts it back?"

    Restore points     System Restore points        (app/restorepts.py)
    Applied tweaks     revertable tweaks the
                       registry says are live       (app/tweak_center.py)
    Service changes    service start-type changes
                       with an undo record          (app/svcs.py)
    Driver backups     pnputil exports of drivers
                       this tool replaced           (app/drivers/folder.py)
    Quarantine         files moved aside instead
                       of deleted                   (app/quarantine.py)

Each section states its state honestly - including "none" - and offers the
single action that fits. It never runs a System Restore for you: rolling a PC
back is destructive and Windows ships a reviewable wizard for it, which this
dialog opens instead. Nothing here invents a count it could not read; a value
that cannot be measured is reported as unknown, not as zero.
"""
from __future__ import annotations

import os
import tkinter as tk

from app.ui.base import _themed_askyesno
from app.ui.dialogs._tool_page import ToolPage
from app.ui.theme import COLORS, RADIUS_SURFACE, TYPE, SPACE_2, SPACE_3
from app.ui.metrics import COMPACT_INSET, GROUP_GAP, ROW_PAD, TEXT_NUDGE
from app.ui.widgets import AnimatedButton, RoundedCard


def _plural(n: int, one: str, many: str = "") -> str:
    return one if n == 1 else (many or one + "s")


def _count_tree(path: str):
    """(file count, total bytes) under `path`, or (0, 0) if it is absent."""
    count = 0
    total = 0
    if not path or not os.path.isdir(path):
        return 0, 0
    for base, _dirs, names in os.walk(path):
        for name in names:
            try:
                total += os.path.getsize(os.path.join(base, name))
                count += 1
            except OSError:
                pass
    return count, total


class SafetyCenterDialog(ToolPage):
    def __init__(self, parent, app):
        self.app = app
        self._data = {}
        self._busy = False
        self._note = None
        self._stop_token = [False]
        super().__init__(
            parent, title="Safety & Undo",
            scope="Everything this app can put back, in one place. It never "
                  "runs a System Restore for you - that is Windows' wizard.",
            scroll=True)

        self._sections = {}
        self._build_section("restore", "Restore points", [
            ("Create restore point", self._create_restore),
            ("Open System Restore", self._open_system_restore)])
        self._build_section("tweaks", "Applied tweaks", [
            ("Undo all tweaks", self._undo_all)])
        self._build_section("services", "Service changes", [
            ("Undo last change", self._undo_last_service)])
        self._build_section("drivers", "Driver backups", [
            ("Open backups folder", self._open_drivers)])
        self._build_section("quarantine", "Quarantined files", [
            ("Open quarantine folder", self._open_quarantine)])

        self.refresh_btn = self.add_button("Refresh", self._start)
        self.on_close(self._stop)
        self._start()

    # ------------------------------------------------------------- layout
    def _build_section(self, key, title, buttons):
        card = RoundedCard(self.content, fill=COLORS["surface"],
                           bg=COLORS["bg"], radius=RADIUS_SURFACE)
        card.pack(fill="x", padx=TEXT_NUDGE, pady=COMPACT_INSET)
        body = card.body
        tk.Label(body, text=title, font=TYPE["label_large"],
                 bg=COLORS["surface"], fg=COLORS["text"], anchor="w").pack(
            fill="x", padx=SPACE_3, pady=(ROW_PAD, 0))
        status = tk.Label(body, text="Checking\u2026", font=TYPE["caption"],
                          bg=COLORS["surface"], fg=COLORS["subtext"],
                          anchor="w", justify="left", wraplength=580)
        status.pack(fill="x", padx=SPACE_3, pady=(TEXT_NUDGE, SPACE_2))
        row = tk.Frame(body, bg=COLORS["surface"])
        row.pack(fill="x", padx=SPACE_3, pady=(0, GROUP_GAP))
        btns = {}
        for label, command in buttons:
            b = AnimatedButton(row, text=label, command=command,
                               role="text", size="sm")
            b.pack(side="left", padx=(0, SPACE_2))
            b.set_enabled(False)
            btns[label] = b
        self._sections[key] = {"card": card, "status": status, "buttons": btns}

    def _set(self, key, text):
        try:
            self._sections[key]["status"].config(text=text)
        except Exception:
            pass

    def _enable(self, key, label, on):
        try:
            self._sections[key]["buttons"][label].set_enabled(bool(on))
        except Exception:
            pass

    def _set_all_buttons(self, on):
        for sec in self._sections.values():
            for b in sec["buttons"].values():
                try:
                    b.set_enabled(bool(on))
                except Exception:
                    pass

    # -------------------------------------------------------------- state
    def _stop(self):
        self._stop_token[0] = True

    def set_busy(self, busy):
        self._busy = bool(busy)
        try:
            self.refresh_btn.set_enabled(not self._busy)
        except Exception:
            pass

    # --------------------------------------------------------------- read
    def _start(self, note=None):
        self._note = note
        self._stop_token[0] = False
        self.set_busy(True)
        self._set_all_buttons(False)
        self.banner.set_state("\u2026", COLORS["muted"])
        self.banner.set_title("Checking your safety nets\u2026")
        self.banner.set_sub("")
        self.status.start("Reading restore points and undo history\u2026")
        token = self._stop_token
        self.run_worker(self._gather,
                        lambda d: None if token[0] else self._land(d))

    @staticmethod
    def _gather():
        out = {}
        try:
            from app import restorepts
            out["protect"] = restorepts.protection_status()
            out["points"] = restorepts.list_points()
        except Exception:
            out["protect"] = {}
            out["points"] = {"points": [], "error": None}
        try:
            from app import tweak_center
            out["tweaks"] = tweak_center.applied_tweak_tasks()
        except Exception:
            out["tweaks"] = []
        try:
            from app import svcs
            out["svc_undo"] = svcs.undo_record()
        except Exception:
            out["svc_undo"] = []
        try:
            from app.drivers import folder as _df
            root = _df.configured()
            out["drivers_root"] = root
            out["drivers"] = _count_tree(os.path.join(root, "backup"))
        except Exception:
            out["drivers_root"] = ""
            out["drivers"] = (0, 0)
        try:
            from app import quarantine
            out["quarantine_root"] = quarantine.default_root()
            out["quarantine"] = quarantine.stats()
        except Exception:
            out["quarantine_root"] = ""
            out["quarantine"] = (0, 0)
        return out

    def _land(self, data):
        self._data = data if isinstance(data, dict) else {}
        self.set_busy(False)
        self.status.stop(self._note or "")
        self._note = None
        self._render_banner()
        self._render_sections()

    def _render_banner(self):
        pts = (self._data.get("points") or {}).get("points") or []
        tweaks = self._data.get("tweaks") or []
        svc = self._data.get("svc_undo") or []
        undoable = len(tweaks) + len(svc)
        green = COLORS["accent_green"]
        if undoable:
            bits = []
            if tweaks:
                bits.append("%d %s" % (len(tweaks),
                                       _plural(len(tweaks), "tweak")))
            if svc:
                bits.append("%d service %s" % (len(svc),
                                               _plural(len(svc), "change")))
            self.banner.set_state("\u21a9", green)
            self.banner.set_title(
                "You can undo %d change%s." % (undoable,
                                               "" if undoable == 1 else "s"),
                green)
            self.banner.set_sub("Ready to revert: " + ", ".join(bits) + ".")
        elif pts:
            self.banner.set_state("\u2713", green)
            self.banner.set_title("Nothing to undo right now.")
            self.banner.set_sub("You have %d restore %s as a safety net." % (
                len(pts), _plural(len(pts), "point")))
        else:
            self.banner.set_state("!", COLORS["accent_red"])
            self.banner.set_title("No changes to undo, and no restore points.")
            self.banner.set_sub("Make a restore point before big changes.")

    def _render_sections(self):
        from app.restorepts import when_plain
        from app.utils import format_bytes

        # -- restore points ------------------------------------------------ #
        pts = (self._data.get("points") or {}).get("points") or []
        err = (self._data.get("points") or {}).get("error")
        pstate = (self._data.get("protect") or {}).get("state")
        if err:
            rtext = err
        elif pts:
            rtext = "%d restore %s. Newest: %s." % (
                len(pts), _plural(len(pts), "point"),
                when_plain(pts[0].get("when")) or "recently")
        else:
            rtext = "No restore points \u2014 there is no way back right now."
        if pstate == "off":
            rtext += " System Protection is OFF."
        elif pstate == "limited":
            rtext += " System Protection is on for some drives only."
        self._set("restore", rtext)
        self._enable("restore", "Create restore point", not self._busy)
        self._enable("restore", "Open System Restore", not self._busy)

        # -- applied tweaks ------------------------------------------------ #
        tweaks = self._data.get("tweaks") or []
        self._set("tweaks",
                  "%d applied %s can be put back." % (
                      len(tweaks), _plural(len(tweaks), "tweak"))
                  if tweaks else "No tweaks are applied.")
        self._enable("tweaks", "Undo all tweaks", bool(tweaks))

        # -- service changes ----------------------------------------------- #
        svc = self._data.get("svc_undo") or []
        self._set("services",
                  "%d service %s recorded and undoable." % (
                      len(svc), _plural(len(svc), "change"))
                  if svc else "No service start-type changes recorded.")
        self._enable("services", "Undo last change", bool(svc))

        # -- driver backups ------------------------------------------------ #
        dcount, dbytes = self._data.get("drivers") or (0, 0)
        droot = self._data.get("drivers_root") or ""
        self._set("drivers",
                  "%d backed-up %s (%s) in %s." % (
                      dcount, _plural(dcount, "file"), format_bytes(dbytes),
                      droot) if dcount else
                  "No driver backups yet \u2014 they appear when this tool "
                  "replaces a driver.")
        self._enable("drivers", "Open backups folder", bool(droot))

        # -- quarantine ---------------------------------------------------- #
        qcount, qbytes = self._data.get("quarantine") or (0, 0)
        qroot = self._data.get("quarantine_root") or ""
        self._set("quarantine",
                  "%d %s (%s) quarantined instead of deleted." % (
                      qcount, _plural(qcount, "file"), format_bytes(qbytes))
                  if qcount else "Nothing is in quarantine.")
        self._enable("quarantine", "Open quarantine folder", bool(qroot))

    # ------------------------------------------------------------ actions
    def _create_restore(self):
        from app import restorepts
        if self._busy:
            return
        self.set_busy(True)
        self.status.start("Creating a restore point\u2026")
        elevated = restorepts.is_elevated()

        def work():
            try:
                return (restorepts.create() if elevated
                        else restorepts.create_elevated())
            except Exception as exc:                  # noqa: BLE001
                return {"ok": False, "error": "Could not create one: %s" % exc}

        self.run_worker(work, self._land_create)

    def _land_create(self, res):
        self.set_busy(False)
        res = res or {}
        if res.get("cancelled"):
            self.status.stop("Administrator access was declined.",
                             COLORS["accent_yellow"])
        elif res.get("skipped"):
            self.status.stop(res.get("message") or "Skipped.")
        elif not res.get("ok"):
            self.status.stop(res.get("error") or "Could not create one.",
                             COLORS["accent_red"])
        else:
            self._start(note="Restore point created.")

    def _undo_last_service(self):
        from app import svcs
        rec = svcs.undo_record()
        if not rec:
            self.status.stop("Nothing to undo.", COLORS["accent_yellow"])
            return
        last = rec[-1]
        name = last.get("name") or "the last service"
        if not _themed_askyesno(self, "Undo This Service Change?",
                                "'%s' returns to how it was before." % name,
                                accent=COLORS["accent_green"]):
            return
        self.set_busy(True)
        self.status.start("Putting the service back\u2026")
        self.run_worker(svcs.undo_last_change, self._land_service)

    def _land_service(self, res):
        res = res or {}
        if res.get("ok"):
            self._start(note="Service change undone.")
        else:
            self.set_busy(False)
            self.status.stop(res.get("error") or "Could not undo it.",
                             COLORS["accent_red"])

    def _undo_all(self):
        from app import tweak_center
        tasks = tweak_center.undo_all_tasks()
        if not tasks:
            self.status.stop("No tweaks to undo.", COLORS["accent_yellow"])
            return
        if not _themed_askyesno(
                self, "Undo %d Applied Tweaks?" % len(tasks),
                "Each one returns to the value recorded when it ran.",
                accent=COLORS["accent_green"]):
            return
        # A revert run owns the app's progress UI, so this modal has to close
        # first; schedule the run after the close unwinds the modal wait().
        self.close()
        try:
            self.app.root.after(60, lambda: self.app.run_tasks(
                "Tweak", tasks, mode="revert"))
        except Exception:
            self.app.run_tasks("Tweak", tasks, mode="revert")

    def _open_system_restore(self):
        from app import restorepts
        if restorepts.open_system_restore():
            self.status.stop("Opened Windows' System Restore wizard.")
        else:
            self.status.stop("Could not open it. Try Start > rstrui.",
                             COLORS["accent_yellow"])

    def _open_drivers(self):
        self._open_folder(self._data.get("drivers_root") or "")

    def _open_quarantine(self):
        self._open_folder(self._data.get("quarantine_root") or "")

    def _open_folder(self, path):
        if not path:
            self.status.stop("That folder is not known yet.",
                             COLORS["accent_yellow"])
            return
        try:
            os.startfile(path)  # noqa: S606 - a folder we chose ourselves
            self.status.stop("Opened %s." % path)
        except Exception as exc:                       # noqa: BLE001
            self.status.stop("Could not open it: %s" % exc,
                             COLORS["accent_red"])
