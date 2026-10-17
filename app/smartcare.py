"""Smart Care: one button that checks the PC, fixes what it can, and says
what is left.

Why this exists
---------------
PC Health Report found problems and then handed the user to five other tabs.
A report that cannot act is a to-do list. Smart Care turns it into the thing
people actually want from "PC health": press one button, and the machine is
cleaned, repaired where needed, and brought up to date - with an honest list
of what finished, what was skipped, and what still needs a human.

It is an ORCHESTRATOR, not a new set of tools. Every step is built from tasks
the app already ships and already tests (the Clean and Repair tab tasks,
`winget upgrade --all`, and the Windows Update module). Nothing here invents a
new way to delete a file or change a setting; it chooses which existing,
reversible tasks to run, in what order, and why.

The plan, in order
------------------
1. Safety checkpoint   a System Restore point first, so every step after it
                       has a way back. (Windows allows one per 24 hours;
                       "already made today" is a skip, not a failure.)
2. Clean               temp files, browser/inet cache, error reports, GPU
                       watchdog dumps, update-sharing cache. Reports space
                       freed from the tasks' own byte counts, never a guess.
3. Repair - ONLY IF    the health scan graded Windows files C or worse, run
   NEEDED              DISM RestoreHealth then SFC. These take 10-30 minutes,
                       so they are never run "just in case". A skewed clock
                       is fixed with a time resync.
4. Updates             Windows Update (drivers, security, other software) and
                       `winget upgrade --all` for installed apps.
5. Needs you           things that are real but cannot be fixed by a button -
                       a failing drive, a crashing program, a slow-boot
                       startup list - are named with the next step, not hidden
                       and not faked.

What it deliberately does NOT do
--------------------------------
* "Boost RAM". Tools that advertise this call EmptyWorkingSet on every
  process. Windows already reclaims memory when it is needed; forcing it just
  pages everything out, and the next click is slower. A feature that makes the
  number on a meter look better while the PC gets slower is not shipped.
  Memory problems are reported (the health scan's memory card) with the cause
  that actually helps: close what is using it, or add RAM.
* Empty the Recycle Bin, clear shader caches, or touch startup entries.
  Each of those loses something the user may want; they stay a choice.
* Install a Windows feature upgrade, or a driver Windows Update did not
  publish for this machine.

Tk-free: the dialog drives this through callbacks and a TaskContext.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional

from app.runner.task_runner import FAIL, OK, SKIP, STOP, invoke_task
from app.utils import IS_WINDOWS, TaskContext

# Clean tasks that are safe to run unattended: each only removes files
# Windows or an app regenerates, and none touches user documents.
CLEAN_KEYS = ("user_temp_files", "system_temp_files", "inet_cache",
              "error_reports", "gpu_watchdog_dumps", "delivery_optimization")

REPAIR_KEYS = ("dism_restorehealth", "sfc_scan")


@dataclass
class Step:
    key: str
    label: str            # shown while it runs: "Cleaning junk files"
    done_label: str       # shown after: "Junk files"
    tasks: List[object]
    needs_admin: bool = False
    eta: str = ""         # "about a minute" - honest ranges, not countdowns
    skip_reason: str = ""  # non-empty: planned but not applicable


@dataclass
class StepResult:
    key: str
    label: str
    status: str = SKIP        # ok | skip | fail | stop
    detail: str = ""
    freed: int = 0


@dataclass
class CareResult:
    steps: List[StepResult] = field(default_factory=list)
    freed: int = 0
    reboot: bool = False
    stopped: bool = False
    needs_admin: bool = False
    advice: List[dict] = field(default_factory=list)

    @property
    def ok(self) -> int:
        return sum(1 for s in self.steps if s.status == OK)

    @property
    def failed(self) -> int:
        return sum(1 for s in self.steps if s.status == FAIL)


# --------------------------------------------------------------------------
# Task lookup
# --------------------------------------------------------------------------

def _registry() -> Dict[str, object]:
    """key -> Task for every task a step may use. Imported lazily: the task
    modules are heavy and a badge refresh must not pay for them."""
    from app.tasks import clean_tasks, install_tasks, repair_tasks
    from app.tasks import Task
    from app import updates
    reg: Dict[str, object] = {}
    for mod in (clean_tasks, repair_tasks):
        for t in mod.TASKS:
            reg[t.key] = t
    reg["update_all"] = install_tasks.UPDATE_ALL_TASK
    reg["windows_updates"] = Task(
        "windows_updates", "Windows & Driver Updates",
        "Installs pending Windows, security and driver updates",
        updates.run_windows_updates, default=False, admin_required=True)
    return reg


def _grade(cards: dict, key: str) -> str:
    try:
        return (cards.get(key) or {}).get("grade", "?")
    except Exception:
        return "?"


# --------------------------------------------------------------------------
# Planning (pure given the registry and the scan - unit-tested)
# --------------------------------------------------------------------------

def build_plan(cards: Optional[dict], registry: Optional[dict] = None,
               clean: bool = True, repair: bool = True, updates: bool = True,
               apps: bool = True) -> List[Step]:
    """The ordered steps for these scan results.

    `cards` is the health scan's {key: card}. With no scan (None) repairs are
    skipped - they are only ever run on evidence.
    """
    reg = registry if registry is not None else _registry()
    cards = cards or {}
    plan: List[Step] = []

    def pick(keys):
        return [reg[k] for k in keys if k in reg]

    cp = pick(["restore_point"])
    if cp:
        plan.append(Step("checkpoint", "Saving a restore point",
                         "Restore point", cp, needs_admin=True,
                         eta="under a minute"))
    if clean:
        plan.append(Step("clean", "Cleaning junk files", "Junk files",
                         pick(CLEAN_KEYS), needs_admin=True,
                         eta="about a minute"))
    if repair:
        win = _grade(cards, "windows")
        if win in ("C", "D", "F"):
            plan.append(Step("repair", "Repairing Windows files",
                             "Windows files", pick(REPAIR_KEYS),
                             needs_admin=True, eta="10 to 30 minutes"))
        else:
            plan.append(Step("repair", "Repairing Windows files",
                             "Windows files", [],
                             skip_reason="Windows files look healthy"
                             if win in ("A", "B") else
                             "Not checked - run the scan first"))
        if _grade(cards, "time") in ("C", "D", "F"):
            plan.append(Step("time", "Fixing the clock", "Clock",
                             pick(["time_sync"]), needs_admin=True,
                             eta="a few seconds"))
    if updates:
        # Windows and security updates only. Driver updates are listed by the
        # task but never installed unattended - see updates.run_windows_updates
        # and the Drivers tab.
        plan.append(Step("windows_updates", "Updating Windows",
                         "Windows", pick(["windows_updates"]),
                         needs_admin=True, eta="2 to 15 minutes"))
    if apps:
        plan.append(Step("apps", "Updating your apps", "Apps",
                         pick(["update_all"]), eta="2 to 15 minutes"))
    return plan


def advice(cards: Optional[dict]) -> List[dict]:
    """Real findings no button fixes: {key, text, action}. `action` is the
    app screen that helps ("startup", "reliability", ...) or "".

    Honest on purpose: a failing drive or a crashing program is reported,
    with the next step, instead of being "fixed" by something that cannot.
    """
    cards = cards or {}
    out: List[dict] = []

    def add(key, text, action=""):
        out.append({"key": key, "text": text, "action": action})

    if _grade(cards, "smart") in ("D", "F"):
        add("smart", "A drive is reporting problems. Back up your files now; "
                     "no software fixes a failing drive.", "drivetool")
    if _grade(cards, "crashes") in ("D", "F"):
        add("crashes", "Programs keep crashing. Reliability Monitor shows "
                       "which ones - updating or reinstalling them usually "
                       "helps.", "reliability")
    if _grade(cards, "drivers") in ("C", "D", "F") or _grade(
            cards, "gpu") in ("C", "D", "F"):
        add("drivers", "Drivers need a look. The Drivers tab scans online for "
                       "missing and newer ones and lets you choose.",
            "drivers")
    if _grade(cards, "startup") in ("C", "D", "F"):
        add("startup", "Many programs start with Windows. Turning off the "
                       "ones you do not need makes boot faster.", "startup")
    if _grade(cards, "memory") in ("C", "D", "F"):
        add("memory", "Memory is running tight. Closing background programs "
                      "helps; if it is always full, more RAM is the fix.",
            "procmgr")
    if _grade(cards, "disk") in ("D", "F"):
        add("disk", "Your system drive is nearly full. Cleaning helps a "
                    "little; Storage Insight shows the big items.",
            "storage")
    return out


# --------------------------------------------------------------------------
# Running
# --------------------------------------------------------------------------

class _Tap:
    """A TaskContext wrapper that records the log lines it passes on, so the
    care run can read "[REBOOT]" without every task knowing about it."""

    def __init__(self, ctx: TaskContext, on_log=None):
        self.lines: List[str] = []
        self._on_log = on_log
        self.ctx = TaskContext(
            log=self._log, set_status=ctx.set_status,
            cancelled=ctx.cancelled, dry_run=ctx.dry_run)

    def _log(self, msg):
        self.lines.append(str(msg))
        if self._on_log:
            try:
                self._on_log(str(msg))
            except Exception:
                pass


def run_plan(ctx: TaskContext, plan: List[Step],
             is_admin: Optional[bool] = None,
             on_step: Optional[Callable[[str, int, object], None]] = None,
             on_log: Optional[Callable[[str], None]] = None,
             cards: Optional[dict] = None) -> CareResult:
    """Run the steps in order. Never raises.

    on_step(event, index, payload): ("start", i, Step) before each step and
    ("done", i, StepResult) after, so a dialog can tick a list live. A stop
    request ends the run after the current task and is reported as stopped,
    not failed. Steps that need administrator rights are SKIPPED, with the
    reason, when the app is not elevated - and `needs_admin` is set so the
    UI can offer the one action that fixes it.
    """
    if is_admin is None:
        try:
            from app.elevation import is_admin as _is_admin
            is_admin = bool(_is_admin()) if IS_WINDOWS else True
        except Exception:
            is_admin = False
    result = CareResult(advice=advice(cards))

    for i, step in enumerate(plan):
        if ctx.cancelled():
            result.stopped = True
            break
        sr = StepResult(step.key, step.done_label)
        if on_step:
            on_step("start", i, step)
        tap = _Tap(ctx, on_log)

        if step.skip_reason:
            sr.status, sr.detail = SKIP, step.skip_reason
        elif not step.tasks:
            sr.status, sr.detail = SKIP, "Nothing to do on this PC"
        elif step.needs_admin and not is_admin:
            sr.status, sr.detail = SKIP, "Needs administrator rights"
            result.needs_admin = True
        else:
            ok = fail = skip = 0
            first_err = ""
            first_skip = ""
            for task in step.tasks:
                if ctx.cancelled():
                    result.stopped = True
                    break
                try:
                    tap.ctx.set_status(task.label)
                except Exception:
                    pass
                out = invoke_task(tap.ctx, task)
                if out.ok:
                    ok += 1
                    sr.freed += int(out.bytes_freed or 0)
                elif out.skipped:
                    skip += 1
                    first_skip = first_skip or str(out.error or "")
                elif out.stopped:
                    result.stopped = True
                    break
                else:
                    fail += 1
                    first_err = first_err or str(out.error or "failed")
            if result.stopped and not (ok or fail):
                sr.status, sr.detail = STOP, "Stopped"
            elif fail and not ok:
                sr.status, sr.detail = FAIL, first_err
            elif ok:
                sr.status = OK
                sr.detail = ("Done" if not fail else
                             "Done, but %d part(s) failed" % fail)
            else:
                sr.status, sr.detail = SKIP, first_skip or "Nothing to do"
            if any("[REBOOT]" in ln for ln in tap.lines):
                result.reboot = True

        result.freed += sr.freed
        result.steps.append(sr)
        if on_step:
            on_step("done", i, sr)
        if result.stopped:
            break
    return result


def headline(result: CareResult) -> str:
    """One plain sentence for the end of a run."""
    from app.utils import format_bytes
    if result.stopped:
        return "Stopped. Nothing after this point was changed."
    bits = []
    if result.freed:
        bits.append("freed %s" % format_bytes(result.freed, compact=True))
    verbs = {"repair": "repaired Windows files", "time": "fixed the clock",
             "windows_updates": "updated Windows",
             "apps": "updated your apps"}
    done = [verbs[s.key] for s in result.steps
            if s.status == OK and s.key in verbs]
    if done:
        bits.append(", ".join(done[:-1]) + (" and " if len(done) > 1 else "")
                    + done[-1])
    if not bits:
        return ("Everything that could run was already in good shape."
                if not result.failed else "Some steps did not finish.")
    return "Done: " + " and ".join(bits) + "."
