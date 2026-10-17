"""HealthReportDialog - "is my PC OK, and can you just fix it?"

Surfaces app/health_scan.py (thirteen read-only checks) and acts on them
through app/smartcare.py.

Third pass (2026-10). The report used to find problems and then OPEN ANOTHER
TAB - Clean, Repair, Install - so the "fix" buttons fixed nothing in this
window and the driver button just showed the Install tab. Now the window does
the work itself:

    Check       the thirteen read-only checks (unchanged), problems first
    Fix         one primary button, "Fix everything", runs Smart Care INSIDE
                this window: a restore point, junk cleanup, Windows repair
                only if the scan found damage, Windows + driver updates
                through Windows Update, and `winget upgrade --all`. Each
                problem row also has its own button that runs just that fix.
    Progress    a live step list (pending / running / done / skipped /
                failed) with honest time estimates and a Stop button
    Result      what was freed, what was updated, whether a restart is
                needed, and a short "needs you" list for real problems no
                button can fix (a failing drive, a crashing program)

Honesty rules this dialog must not break:
* It never reports success for a step that was skipped or failed, and a
  step that needs administrator rights says so instead of doing nothing.
* A failing drive (SMART "F") gets NO fix button. You back it up.
* Nothing is "boosted". See app/smartcare.py for why there is no RAM step.
* Navigating to another screen is reserved for things that need a human
  decision (which startup programs to disable), never for a fix.

Threading: the scan and the care run each use one daemon worker; they reach
Tk only through the dispatcher (BUG-001).
"""
from __future__ import annotations

import threading
import tkinter as tk

from app.ui.base import _themed_askyesno
from app.ui.dialogs._tool_page import TOOL_ACCENT, ToolPage
from app.ui.theme import COLORS, F
from app.ui.widgets import AnimatedButton, Tooltip
from app.ui.widgets.datatable import DataTable

#: Grade -> (badge background, badge foreground). A pair: accent_red with
#: black text is unreadable, so F gets white, and "?" is never given a badge
#: fill so an unmeasured sensor cannot be mistaken for a scored one.
GRADE_BADGE = {
    "A": (COLORS["accent_green"], COLORS["black"]),
    "B": (COLORS["accent_teal"], COLORS["black"]),
    "C": (COLORS["accent_yellow"], COLORS["black"]),
    "D": (COLORS["accent_orange"], COLORS["black"]),
    "F": (COLORS["accent_red"], COLORS["black"]),
    "?": (COLORS["surface"], COLORS["subtext"]),
}

#: Accent used for the card's grade-tinted hairline when expanded.
GRADE_TINT = {
    "A": COLORS["accent_green"], "B": COLORS["accent_teal"],
    "C": COLORS["accent_yellow"], "D": COLORS["accent_orange"],
    "F": COLORS["accent_red"], "?": COLORS["muted"],
}

#: Fixed row order, matching health_scan.SENSORS, so a card filling in always
#: means the same thing. Never sorted by grade - the list would jump around
#: while it loads.
#:
#: BUG-044: the report outgrew a flat list. Thirteen collapsed cards measured
#: 546px, which overflows the 343px and 448px viewports - so every user on a
#: small screen got a scrollbar for a list that is mostly titles anyway.
#:
#: Grouping into four sections fixes it honestly: collapsed, the report is
#: four rows (~120px) that fit everywhere, and each section carries a rollup
#: grade so the collapsed view still answers "is anything wrong" at a glance.
#: Flattening these keys gives _ORDER, which stays the single source of row
#: order for _make_card.
#:
#: The grouping is by SUBJECT, not by grade: a user opening "Security &
#: Settings" should find the security card there whether it passed or not.
_SECTIONS = (
    ("storage", "Storage & Memory",
     ("disk", "memory", "smart", "disks", "junk")),
    ("problems", "Crashes & Reliability",
     ("crashes", "time", "drivers")),
    ("security", "Security & Settings",
     ("security", "maintenance", "startup")),
    ("windows", "Windows & Graphics",
     ("gpu", "windows")),
)

_ORDER = tuple(k for _sid, _t, keys in _SECTIONS for k in keys)

#: Worst-first, for a section's rollup grade. "?" sorts last on purpose: an
#: unreadable check is not a FAULT, so a section of five unknowns must not
#: report itself as broken.
_SEVERITY = ("F", "D", "C", "B", "A")

#: Plain-language "what am I looking at", shown when a card is expanded.
_WHY = {
    "disk": "How much room is left on your main drive. Windows needs some "
            "spare room to install updates and keep running smoothly.",
    "memory": "How much memory is installed, and how much space Windows has "
              "set aside for it. A pagefile far larger than what you actually "
              "use is disk space being held in reserve for nothing.",
    "crashes": "Programs that hung or crashed in the last 7 days. Repeated "
               "crashes usually mean one program needs updating or "
               "reinstalling - this is usually the answer to 'why is my PC "
               "acting weird'.",
    "security": "Whether Microsoft Defender is watching for threats, how old "
                "its virus definitions are, and whether anything is blocking "
                "it from doing its job.",
    "maintenance": "Three quiet settings that decide whether your drive stays "
                   "healthy over time: TRIM, Storage Sense and Fast Startup.",
    "time": "Whether Windows is correcting your clock, and how long the PC "
            "has been running without a restart. A wrong clock breaks "
            "certificate checks and app signing.",
    "startup": "How many programs start automatically with Windows. Each one "
               "slows down sign-in a little.",
    "gpu": "How old your graphics driver is. Out-of-date drivers are a "
           "common cause of stutter, black screens and lower frame rates.",
    "smart": "The health rating your drives report about themselves. A drive "
             "going bad rarely warns you first, so knowing early matters.",
    "windows": "A read-only check that Windows' own system files are not "
               "corrupt. Needs Administrator, so it shows ? otherwise.",
    "junk": "Roughly how much space temporary files, caches and leftover "
            "installers are taking up. Cleaning is always optional.",
    "disks": "What your drives actually are. Whether a machine has an SSD - "
             "and whether it is a fast NVMe one - predicts its speed better "
             "than almost anything else.",
    "drivers": "How old the drivers on this PC are. Very old drivers are a "
               "real cause of crashes and hardware oddities. This one takes "
               "a few seconds because Windows has to read every device.",
}

#: Grades worth offering an action for.
_ACTION_GRADES = ("B", "C", "D", "F")

#: Collapsed row height target. Five of these plus the hero and the footer fit
#: the dialog at its proportional minimum with no scrolling at all; pinned by

_COLUMNS = (
    {"key": "grade", "title": "", "w": 56, "min": 52, "sortable": False},
    {"key": "check", "title": "", "w": 300, "weight": 1, "min": 160,
     "sortable": False},
)

#: Problem card -> (Smart Care step key, button label). Cards missing here
#: have no in-window fix: they are advice (see smartcare.advice).
_FIX_STEP = {
    "disk": ("clean", "Free up space"),
    "junk": ("clean", "Clean it up"),
    "windows": ("repair", "Repair"),
    "time": ("time", "Fix clock"),
    # Drivers are a CHOICE, not an unattended fix: the row opens the Drivers
    # tab, which scans online, lets the user pick, and backs up first.
    "gpu": ("drivers", "Review drivers"),
    "drivers": ("drivers", "Review drivers"),
    "security": ("windows_updates", "Update Windows"),
}

#: One row and one button per FIX, not per card. Graphics driver, driver age
#: and Defender definitions all resolve to the same step (Windows Update), and
#: free space and junk both resolve to Clean; three rows with the same button
#: read as three different actions and made the report look bloated.
_STEP_TITLE = {
    "clean": "Free up space",
    "repair": "Repair Windows files",
    "time": "Fix the clock",
    "windows_updates": "Update Windows",
    "drivers": "Update drivers",
}
_STEP_BUTTON = {
    "clean": "Clean up",
    "repair": "Repair",
    "time": "Fix clock",
    "windows_updates": "Update now",
    "drivers": "Review drivers",
}

#: Step status -> (glyph, pill background, pill foreground)
_STEP_PILL = {
    "pending": ("·", COLORS["surface"], COLORS["muted"]),
    "running": ("▶", COLORS["accent_pink"], COLORS["black"]),
    "ok": ("✓", COLORS["accent_green"], COLORS["black"]),
    "skip": ("–", COLORS["surface"], COLORS["subtext"]),
    "fail": ("!", COLORS["accent_red"], COLORS["text"]),
    "stop": ("■", COLORS["accent_yellow"], COLORS["black"]),
}

#: Where each "needs you" action goes. These are decisions, not fixes.
_ADVICE_BUTTON = {"drivers": "Open Drivers",
                  "drivetool": "Open Drive Toolkit",
                  "reliability": "See crashes", "startup": "Choose",
                  "procmgr": "See what uses it", "storage": "See big items"}


class HealthReportDialog(ToolPage):
    """Check, then fix - in this window."""

    def __init__(self, parent, app):
        self.app = app
        self._stop_token = [False]
        self._cards = {}
        self._show_ok = False
        self._scan_seq = 0
        self._scanning = False
        self._mode = "scan"            # scan | review | care | done
        self._plan = []
        self._step_state = {}          # step key -> (status, detail)
        self._result = None
        self._care_seq = 0

        super().__init__(
            parent, title="PC Health",
            scope="Checking changes nothing. Fix everything saves a "
                  "restore point first.")

        self.table = DataTable(self.content, _COLUMNS, row_h=46,
                               accent=TOOL_ACCENT, two_line=True,
                               show_header=False,
                               on_select=self._on_select,
                               on_action=self._on_action)
        self.table.pack(fill="both", expand=True)

        self._primary = self.add_button("Fix everything", self._fix_all,
                                        kind="primary")
        self._primary.set_enabled(False)
        self._second = self.add_button("Check again", self._rescan)
        self._second.set_enabled(False)
        # "Fix everything" installs Windows/security updates unattended; this
        # is the way to pick exactly which updates (drivers included).
        self._pick = self.add_button("Choose updates...", self._open_picker)
        Tooltip(self._pick, "See every pending Windows update and install "
                            "only the ones you tick.")
        self.refresh_btn = self._second
        self._admin_btn = AnimatedButton(
            self.foot_btns, text="Restart as administrator",
            command=self._restart_admin, bg=COLORS["surface"],
            fg=COLORS["accent_yellow"], font=(F, 9), padx=14, pady=6)
        self.on_close(self._stop_scan)
        self._start_scan()

    # -------------------------------------------------------------- rows
    def _titles(self):
        from app.health_scan import SENSORS
        return dict((sp[0], sp[1]) for sp in SENSORS)

    def _section_grade(self, keys):
        """Worst real grade among the section's cards; '?' only if all are."""
        for g in _SEVERITY:
            for k in keys:
                if self._cards.get(k, {}).get("grade") == g:
                    return g
        return "?" if any(k in self._cards for k in keys) else " "

    def _all_keys(self):
        return [k for _sid, _t, keys in _SECTIONS for k in keys]

    def _rebuild(self):
        if self._mode in ("care", "done"):
            return self._rebuild_steps()
        titles = self._titles()
        order = {g: i for i, g in enumerate(_SEVERITY)}
        probs, fine, wait = [], [], []
        for k in self._all_keys():
            card = self._cards.get(k)
            if card is None:
                wait.append(k)
            elif card.get("grade") in ("C", "D", "F"):
                probs.append(k)
            else:
                fine.append(k)
        probs.sort(key=lambda k: order.get(self._cards[k]["grade"], 9))
        rows = []

        def check_row(k, with_action):
            card = self._cards.get(k)
            gr = (card or {}).get("grade", "…")
            bg, fg = GRADE_BADGE.get(gr, GRADE_BADGE["?"])
            cell = {"text": gr, "pill": (bg, fg)} if card else \
                {"text": "…", "fg": COLORS["muted"]}
            return {"_key": k, "_actions": [], "_cells": {
                "grade": cell,
                "check": {"text": titles.get(k, k), "bold": True,
                          "sub": (card or {}).get("headline")
                          or "Checking..."}}}

        # Problems that share a fix share a row (and one button).
        by_step, singles = {}, []
        for k in probs:
            fix = _FIX_STEP.get(k)
            (by_step.setdefault(fix[0], []) if fix else singles).append(k)
        entries = []     # (severity rank, row)
        for step, members in by_step.items():
            worst = min(members, key=lambda k: order.get(
                self._cards[k]["grade"], 9))
            gr = self._cards[worst]["grade"]
            bg, fg = GRADE_BADGE.get(gr, GRADE_BADGE["?"])
            if len(members) == 1:
                title = titles.get(members[0], members[0])
                sub = self._cards[members[0]].get("headline") or ""
            else:
                title = _STEP_TITLE.get(step, step)
                sub = "; ".join(self._cards[m].get("headline") or ""
                                for m in members if self._cards[m]
                                .get("headline"))
            acts = ([{"id": "fix:" + step,
                      "text": _STEP_BUTTON.get(step, "Fix")}]
                    if self._mode == "review" and gr in _ACTION_GRADES
                    else [])
            entries.append((order.get(gr, 9), {
                "_key": "step:" + step, "_actions": acts, "_cells": {
                    "grade": {"text": gr, "pill": (bg, fg)},
                    "check": {"text": title, "bold": True, "sub": sub}}}))
        for k in singles:
            entries.append((order.get(self._cards[k]["grade"], 9),
                            check_row(k, False)))
        entries.sort(key=lambda e: e[0])
        rows.extend(r for _rank, r in entries)
        for k in wait:
            rows.append(check_row(k, False))
        if fine:
            rows.append({"_key": "ok", "_chev": "open" if self._show_ok
                         else "closed", "_cells": {
                             "grade": {"text": ""},
                             "check": {"text": "%d other check%s look%s fine"
                                       % (len(fine), "" if len(fine) == 1
                                          else "s", "s" if len(fine) == 1
                                          else ""),
                                       "fg": COLORS["subtext"]}}})
            if self._show_ok:
                for k in fine:
                    rows.append(check_row(k, False))
        self.table.set_rows(rows)

    def _rebuild_steps(self):
        """The live step list (care) or the result list (done)."""
        rows = []
        for st in self._plan:
            status, detail = self._step_state.get(st.key, ("pending", ""))
            if status == "pending" and st.skip_reason:
                status, detail = "skip", st.skip_reason
            glyph, bg, fg = _STEP_PILL[status]
            if status == "pending":
                sub = st.eta and ("Waiting - " + st.eta) or "Waiting"
            elif status == "running":
                sub = detail or ("Working - " + st.eta if st.eta
                                 else "Working")
            else:
                sub = detail
            rows.append({"_key": "step:" + st.key, "_cells": {
                "grade": {"text": glyph, "pill": (bg, fg)},
                "check": {"text": st.label if status in ("pending",
                                                         "running")
                          else st.done_label, "bold": True, "sub": sub}}})
        if self._mode == "done" and self._result and self._result.advice:
            rows.append({"_key": "advice-head", "_head": True, "_cells": {
                "grade": {"text": ""},
                "check": {"text": "Needs you", "bold": True}}})
            for a in self._result.advice:
                acts = []
                if a["action"] in _ADVICE_BUTTON:
                    acts = [{"id": "adv:" + a["action"],
                             "text": _ADVICE_BUTTON[a["action"]]}]
                rows.append({"_key": "adv:" + a["key"], "_actions": acts,
                             "_cells": {"grade": {"text": "!", "pill": (
                                 COLORS["accent_yellow"], COLORS["black"])},
                                 "check": {"text": a["text"]}}})
        self.table.set_rows(rows)

    def _on_select(self, row):
        if row["_key"] == "ok":
            self._show_ok = not self._show_ok
            self._rebuild()

    def _on_action(self, row, aid):
        if aid == "fix:drivers":
            self._route("drivers", None)
        elif aid.startswith("fix:"):
            self._fix_some([aid[4:]])
        elif aid.startswith("adv:"):
            self._route(aid[4:], None)

    # -------------------------------------------------------------- scan
    def _paint_footer(self):
        busy = self._mode == "care"
        scanning = self._scanning
        try:
            if self._mode == "care":
                self._primary.config_text("Stop")
                self._primary.set_enabled(True)
                self._second.pack_forget()
                self._pick.pack_forget()
            elif self._mode == "done":
                # After a run the useful next step is to see the new grade.
                self._primary.config_text("Check again")
                self._primary.set_enabled(True)
                self._second.pack_forget()
                self._pick.pack_forget()
            else:
                if not self._second.winfo_manager():
                    self._second.pack(side="right", padx=(8, 0))
                if not self._pick.winfo_manager():
                    self._pick.pack(side="right", padx=(8, 0))
                self._pick.set_enabled(not scanning)
                self._second.set_enabled(not scanning)
                self._primary.set_enabled(
                    not scanning and bool(self._cards))
                self._primary.config_text(
                    "Fix everything" if self._has_problems()
                    else "Run Smart Care")
            need = bool(self._result and self._result.needs_admin
                        and self._mode == "done")
            if need and not self._admin_btn.winfo_manager():
                self._admin_btn.pack(side="right", padx=(8, 0))
            elif not need and self._admin_btn.winfo_manager():
                self._admin_btn.pack_forget()
        except Exception:
            pass
        _ = busy

    def _has_problems(self):
        return any(c.get("grade") in ("C", "D", "F")
                   for c in self._cards.values())

    def _start_scan(self):
        from app.health_scan import run_health_scan
        from app.utils import TaskContext

        self._stop_token[0] = False
        token = self._stop_token
        self._scan_seq += 1
        seq = self._scan_seq
        self._scanning = True
        self._mode = "scan"
        self._result = None
        self._cards = {}
        self.banner.set_state("…", COLORS["muted"])
        self.banner.set_title("Checking your PC...")
        self.banner.set_sub("This usually takes under a minute.")
        self.status.start("")
        self._paint_footer()
        self._rebuild()

        def on_card(key, card):
            self._ui(self._land_card, seq, key, card)

        def worker():
            try:
                ctx = TaskContext(
                    log=lambda _m: None,
                    set_status=lambda m: self._ui(self._set_status, seq, m),
                    cancelled=lambda: bool(token[0]))
                run_health_scan(ctx, on_card=on_card)
            except Exception:
                pass
            finally:
                self._ui(self._finish, seq)

        threading.Thread(target=worker, daemon=True).start()

    def _set_status(self, seq, text):
        if seq == self._scan_seq and self._mode == "scan":
            self.status.set_text(str(text))

    def _land_card(self, seq, key, card):
        if seq != self._scan_seq:
            return
        self._cards[key] = card
        self._rebuild()

    def _finish(self, seq):
        if seq != self._scan_seq:
            return
        from app.health_scan import count_fixes, overall_grade, verdict_for
        self._scanning = False
        self._mode = "review"
        cards = self._cards
        self.status.stop("")
        if not cards:
            self.banner.set_state("?", COLORS["accent_yellow"])
            self.banner.set_title("We couldn't check this PC",
                                  COLORS["accent_yellow"])
            self.banner.set_sub("No check completed, so there is no score. "
                                "Press 'Check again', or restart as "
                                "Administrator.")
            self._paint_footer()
            return
        grade = overall_grade(cards)
        headline, _detail = verdict_for(grade, count_fixes(cards))
        bg, _fg = GRADE_BADGE.get(grade, GRADE_BADGE["?"])
        # Keep the main window's badge honest: a manual check is as good a
        # data point as the scheduled one.
        try:
            from app import health_badge
            health_badge.record(grade)
            self.app._refresh_health_badge()
        except Exception:
            pass
        self.banner.set_state(grade, bg)
        self.banner.set_title(headline, bg if grade != "?" else None)
        self.banner.set_sub(
            "Fix everything cleans, repairs and updates in one go - it "
            "saves a restore point first." if self._has_problems() else
            "Nothing is wrong. Smart Care can still clean up and check for "
            "updates.")
        self._paint_footer()
        self._rebuild()

    def _rescan(self):
        if not self._scanning and self._mode != "care":
            self._plan = []
            self._start_scan()

    def _refresh(self):
        return self._rescan()

    # --------------------------------------------------------------- fix
    def _plan_for(self, keys):
        """The Smart Care steps for `keys` (None = everything)."""
        from app import smartcare as sc
        full = sc.build_plan(self._cards)
        if keys is None:
            return full
        picked = [s for s in full if s.key in keys]
        risky = {"repair", "time", "windows_updates"}
        if any(s.key in risky for s in picked):
            cp = [s for s in full if s.key == "checkpoint"]
            picked = cp + picked
        return picked

    def _fix_all(self):
        if self._mode == "care":
            return self._request_stop()
        if self._mode == "done":
            return self._rescan()
        self._begin(self._plan_for(None))

    def _fix_some(self, keys):
        if self._mode == "review":
            self._begin(self._plan_for(set(keys)))

    def _confirm_text(self, plan):
        lines = []
        for st in plan:
            if st.skip_reason or not st.tasks:
                continue
            lines.append("- %s%s" % (st.label, (" (%s)" % st.eta)
                                     if st.eta else ""))
        return "\n".join(lines)

    def _begin(self, plan):
        runnable = [s for s in plan if s.tasks and not s.skip_reason]
        if not runnable:
            self.status.stop("Nothing to do.")
            return
        body = ("This will:\n%s\n\nA restore point is saved first when "
                "anything is repaired or updated. Updates can ask for a "
                "restart. You can press Stop at any time."
                % self._confirm_text(plan))
        if not _themed_askyesno(self._dlg, "Run Smart Care?", body,
                                accent=TOOL_ACCENT, yes_text="Start",
                                no_text="Go Back"):
            return
        self._run_care(plan)

    def _run_care(self, plan):
        from app import smartcare as sc
        from app.utils import TaskContext

        self._stop_token[0] = False
        token = self._stop_token
        self._care_seq += 1
        seq = self._care_seq
        self._mode = "care"
        self._plan = plan
        self._step_state = {}
        self._result = None
        self.banner.set_state("▶", TOOL_ACCENT)
        self.banner.set_title("Taking care of your PC...")
        self.banner.set_sub("You can keep using it. Press Stop to end "
                            "after the current step.")
        self.status.start("")
        self._paint_footer()
        self._rebuild()

        def on_step(event, i, payload):
            self._ui(self._land_step, seq, event, payload)

        def worker():
            ctx = TaskContext(
                log=lambda _m: None,
                set_status=lambda m: self._ui(self._set_status_care, seq, m),
                cancelled=lambda: bool(token[0]))
            try:
                res = sc.run_plan(ctx, plan, on_step=on_step,
                                  cards=self._cards)
            except Exception as exc:                      # noqa: BLE001
                res = sc.CareResult()
                res.advice = []
                self._ui(self._care_crashed, seq, str(exc))
                return
            self._ui(self._land_done, seq, res)

        threading.Thread(target=worker, daemon=True).start()

    def _set_status_care(self, seq, text):
        if seq == self._care_seq and self._mode == "care":
            self.status.set_text(str(text))

    def _land_step(self, seq, event, payload):
        if seq != self._care_seq:
            return
        if event == "start":
            self._step_state[payload.key] = ("running", "")
        else:
            self._step_state[payload.key] = (payload.status, _step_detail(
                payload))
        self._rebuild()

    def _care_crashed(self, seq, msg):
        if seq != self._care_seq:
            return
        self._mode = "review"
        self.status.stop("")
        self.banner.set_state("!", COLORS["accent_red"])
        self.banner.set_title("Smart Care hit a problem", COLORS["accent_red"])
        self.banner.set_sub(msg[:160])
        self._paint_footer()
        self._rebuild()

    def _land_done(self, seq, res):
        from app import smartcare as sc
        if seq != self._care_seq:
            return
        self._mode = "done"
        self._result = res
        for sr in res.steps:
            self._step_state[sr.key] = (sr.status, _step_detail(sr))
        self.status.stop("")
        failed = res.failed
        if res.stopped:
            glyph, color = "■", COLORS["accent_yellow"]
        elif failed:
            glyph, color = "!", COLORS["accent_yellow"]
        else:
            glyph, color = "✓", COLORS["accent_green"]
        self.banner.set_state(glyph, color)
        self.banner.set_title(sc.headline(res))
        sub = []
        if res.reboot:
            sub.append("Restart your PC to finish the updates.")
        if res.needs_admin:
            sub.append("Some steps need administrator rights.")
        if failed:
            sub.append("%d step%s did not finish." % (
                failed, "" if failed == 1 else "s"))
        sub.append("Press Check again to see your new grade.")
        self.banner.set_sub(" ".join(sub))
        self._paint_footer()
        self._rebuild()

    def _request_stop(self):
        self._stop_token[0] = True
        self.status.set_text("Stopping after the current step...")

    def _open_picker(self):
        try:
            self.app._open_update_picker()
        except Exception:
            pass

    def _restart_admin(self):
        try:
            self.close()
            self.app._offer_restart_elevated()
        except Exception:
            pass

    # --------------------------------------------------------------- nav
    def _route(self, action, payload):
        """Open the screen where the USER decides (never used to fix)."""
        self.close()
        try:
            if action == "drivers":
                self.app._switch_to("Drivers")
            elif action == "storage":
                self.app._open_storage_insight()
            elif action == "procmgr":
                self.app._open_process_manager()
            elif action == "startup":
                self.app._open_startup_manager()
            elif action == "drivetool":
                self.app._open_drive_toolkit()
            elif action == "reliability":
                from app.health_scan import open_reliability_monitor
                open_reliability_monitor()
        except Exception:
            pass

    def _stop_scan(self):
        self._stop_token[0] = True


def _step_detail(sr) -> str:
    """The one line under a finished step."""
    from app.utils import format_bytes
    bits = []
    if sr.status == "ok":
        if sr.freed:
            bits.append("freed %s" % format_bytes(sr.freed, compact=True))
        bits.append(sr.detail or "Done")
    else:
        bits.append(sr.detail or "")
    return " - ".join(b for b in bits if b)


def count_fixes_safe(cards) -> int:
    """count_fixes without raising - this runs on every card that lands."""
    try:
        from app.health_scan import count_fixes
        return count_fixes(cards)
    except Exception:
        return 0


#: The global button names its destination, so the user knows where they are
#: about to go before they click.
_FIX_LABELS = {
    "storage": "Free up space",
    "repair": "Fix the problem",
    "clean": "Clean it up",
    "install": "See updates",
    "none": "Fix what's found",
}

#: What the primary button is actually about to do, in one sentence, shown in
#: the subline BEFORE the route closes the dialog. "repair" is worded as
#: "starting", not "done", because it hands off to a repair run the user then
#: has to watch, confirm and wait for - the dialog must not imply it finished.
_WHAT_THE_BUTTON_DOES = {
    "storage": "Opening Storage Insight to free up space.",
    "repair": "Starting a Windows file check on the Repair tab.",
    "clean": "Opening the Clean tab with Quick Clean selected.",
    "install": "Opening the Install tab to look for driver updates.",
}