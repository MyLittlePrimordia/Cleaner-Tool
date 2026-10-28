"""The run lifecycle: claim, pre-flight, worker, summary, release.

Extracted from app/ui/app.py (audit 2026-10, R-001). That module was 5,429
lines and owned the window chrome, the tray, the update checker and the whole
run engine, so a bug in the run path had to be found among the UI glue. The
methods here are the ones that only ever run because a user pressed Run:

  * the pre-flight notices and the hazard/data-loss confirmations,
  * the single-slot claim (PREFLIGHT/RUNNING/CANCELLING) and its release,
  * the two workers - one for tab tasks, one for Install-tab items,
  * the closing summary block and the scorecard hop.

They still live on Application through RunFlow: every one of them reaches for
`self.tabs`, `self.root`, `self._dispatch`, `self._query_drives` and the log,
and threading those through explicit constructor arguments would have moved
the coupling rather than removed it. What the split buys is a file that holds
one subsystem, with its four pure copy helpers at the top where a test can
import them without a window. The lifecycle state is still a VIEW over
RunState (see `_busy`), so no call site in the app changed.

Threading contract, unchanged and load-bearing: nothing here may touch Tk from
a worker thread. UI updates go through `self._dispatch.post`, which is a
queue drained by a Tk-thread pump; a cross-thread `after()` can be dropped
without raising, and one dropped completion hop is a result card the user
never sees.
"""
from __future__ import annotations

import threading
from app.config import RISK_DATA_LOSS
from app.config_persist import mark_tweak_applied, mark_tweak_reverted
from app.elevation import is_admin
from app.freed import account as _account_freed
from app.runner import CancellationToken, RunState
from app.runner.task_runner import (
    change_cursor, changes_since, invoke_task, log_block as _log_block,
)
from app.toast import notify_clean_complete
from app.ui.base import (
    _themed_askyesno, _themed_showinfo, _themed_showerror,
    _themed_showwarning,
)
from app.ui.dialogs.install_failed_dialog import InstallFailedDialog
from app.ui.dialogs.scorecard_dialog import ScorecardDialog
from app.ui.hardware.drives import _snapshot_all_drives, _system_drive_root
from app.ui.theme import COLORS
from app.utils import TaskCancelled, TaskContext, format_bytes
from app.warnings import check_dangerous_combos, check_info_notices

def _run_outcome_word(cancelled: bool, crashed: bool) -> str:
    """The status-bar word for a finished run. Pure, so it is testable.

    A crash used to fall into `complete`, which put a clean headline on a
    failed run in the only line the status bar shows.
    """
    if cancelled:
        return "stopped early"
    return "ended early" if crashed else "complete"


#: (noun, past participle) per kind of Install-tab job. "Update Everything"
#: is the one job that installs nothing - it upgrades what is already there -
#: so its card must not call itself an install.
_DONE_COPY = {
    "install": ("Install", "installed"),
    "update": ("Update", "updated"),
}


def _install_start_copy(total: int, kind: str = "install") -> "tuple[str, str]":
    """(status line, transcript banner) the run opens with.

    A `winget upgrade --all` run installs nothing, so it cannot open with
    "Installing 1 item(s)": for an update `total` counts TASKS - one of them -
    which is neither the right word nor a useful number. Both lines come from
    here so the start of a run and its verdict (_install_done_copy) agree.
    """
    if kind == "update":
        return "Updating installed apps...", "===== Updating all apps ====="
    return (f"Installing {total} item(s)...",
            f"===== Installing {total} selected item(s) =====")


def failed_app_rows(apps, app_fail) -> "list[dict]":
    """(name, url) for each catalog app the installer could not install.

    `install_selected_apps` reports failed NAMES; the vendor page lives on the
    catalog dict the tab passed in. This maps one to the other once, so the
    result dialog (U-001) can offer a real download link instead of the log
    line nobody can see.
    """
    rows = []
    for name in app_fail or ():
        meta = next((a for a in (apps or ())
                     if str(a.get("name")) == str(name)), {})
        rows.append({"name": str(name), "url": str(meta.get("url") or "")})
    return rows


def _install_task_status(task) -> str:
    """What the status line says while ONE Install-tab job runs.

    "Running Update Everything..." named the button and told the user nothing
    about what they are waiting on. The update job is `winget upgrade --all`
    walking every installed app, so it uses the task's own line - the very
    string `update_all_apps` sets a moment later, because two sentences there
    would only flicker past each other. Other jobs keep their label, which is
    also the divider they write into the transcript.
    """
    if getattr(task, "key", "") == "update_all":
        from app.tasks.install_tasks import UPDATE_ALL_STATUS
        return UPDATE_ALL_STATUS
    return f"Running {task.label}..."


def _install_done_copy(ok_n: int, fail_n: int, skipped_n: int,
                       stopped: bool,
                       kind: str = "install") -> "tuple[str, str, str]":
    """(status line, notice title, notice body) for a finished Install-tab run.

    Pure, so the split can be asserted without a window. Three surfaces used
    to carry one sentence: the status line and the notice body were the very
    same string, and its first clause ("Install complete:") repeated the
    notice's own title - and a run the user stopped opened a notice titled
    "Install Done" over "Stopped: 3 finished before stopping.".

    Now the strip names the outcome and the body carries only the counts; the
    status line keeps the whole sentence, because it is the one that outlives
    the dialog. `kind` picks the noun: a `winget upgrade --all` run upgrades
    apps, it does not install them, so its card says "Update Done".
    """
    noun, past = _DONE_COPY.get(kind, _DONE_COPY["install"])
    counts = (f"{ok_n} succeeded"
              + (f", {skipped_n} skipped" if skipped_n else "")
              + f", {fail_n} failed")
    if stopped:
        if not ok_n:
            return (f"{noun} stopped before anything was {past}.",
                    f"{noun} Stopped", f"Nothing was {past}.")
        return (f"{noun} stopped \u2014 {ok_n} finished first.",
                f"{noun} Stopped",
                f"{ok_n} succeeded; the rest were not {past}.")
    # The title is the outcome, so it has to be the real one: an all-failed
    # install used to be announced as "Install Done" in green.
    title = (f"{noun} Done" if not fail_n
             else f"{noun} Failed" if not ok_n
             else f"{noun} Finished With Errors")
    return (f"{noun} finished: {counts}.", title, counts + ".")


def _run_header_line(verb: str, task_count: int, tab_name: str,
                     cancelled: bool) -> "str | None":
    """The banner a run opens its transcript with, or None if it never starts.

    A Stop pressed during pre-flight reaches the worker with the cancel token
    already set, so the loop's first iteration breaks and nothing runs. The
    old worker still wrote "Running 5 Clean task(s)" above it - a header for
    a run the next line says was cancelled, and that `_request_cancel` had
    already reported.
    """
    if cancelled:
        return None
    return f"===== {verb} {task_count} {tab_name} task(s) ====="


def _run_summary_lines(tab_name: str, mode: str, cancelled: bool, crashed,
                       completed: int, skipped_n: int, failed: int,
                       attempted: int, reclaimed_bytes: int,
                       moved_bytes: int, needs_reboot: bool,
                       locked_files: int = 0, accounted=None) -> "list[str]":
    """The lines a finished run's transcript block and status line carry.

    Pure, so the copy can be asserted without a window - this block is the
    part of a log export a bug report is read from.

    Two defects it fixes, both visible only in the transcript:
      * the headline was built from the worker's PROGRESS verb, so a clean
        run read "Running complete: 5 succeeded" - a contradiction, and not
        the vocabulary of the scorecard above it ("Clean Complete", "Undo
        Complete");
      * a stop pressed before the first task still produced "0 succeeded,
        0 failed." plus "Remaining tasks were skipped" for a run in which
        nothing was attempted and nothing was skipped.
    A skipped task (nothing to do on this machine) keeps its own count, and a
    crash never reads as a clean completion - the status bar shows line one.
    """
    word = _run_outcome_word(cancelled, crashed is not None)
    run_word = "Undo" if mode == "revert" else tab_name
    if cancelled and not attempted:
        lines = [f"{run_word} {word} \u2014 no task was attempted."]
    else:
        core = f"{run_word} {word}: {completed} succeeded"
        if skipped_n:
            core += f", {skipped_n} skipped (nothing to change)"
        # No "remaining tasks were skipped" line here: the loop already
        # logged that at the moment of the stop, and the headline says the
        # run stopped early. The block is the verdict, not a second report.
        lines = [core + f", {failed} failed."]
    if crashed is not None:
        lines.append(
            f"The run ended early: {crashed!r}. See the log for the full "
            "error; any tasks that had already run were NOT rolled back.")
    # N-010: with a measurement, the block prints the CREDITED figure (what
    # the card and the ledger show), and the check line below explains the
    # difference from what the tasks removed. Without one it stays the removed
    # total, exactly as before - there is nothing to reconcile it against.
    _freed = reclaimed_bytes
    if accounted is not None:
        try:
            _freed = accounted.credited
        except Exception:
            _freed = reclaimed_bytes
    if _freed > 0:
        lines.append(f"Disk space freed: {format_bytes(_freed)}")
    if accounted is not None:
        # Silent for a run that removed nothing and for one whose free space
        # could not be read - there is no second reading to report.
        try:
            _check = accounted.log_line()
        except Exception:
            _check = ""
        if _check:
            lines.append(_check)
    if moved_bytes > 0:
        lines.append(f"{format_bytes(moved_bytes)} moved to Quarantine "
                     "\u2014 empty the bin (Settings) to reclaim it.")
    if locked_files > 0:
        # U-007: the most common "why did it free less than the estimate"
        # answer. The per-folder lines say where; this says how many and why,
        # in the one block a user reads after a run.
        lines.append(
            f"{locked_files:,} file(s) were locked by running apps and left "
            "in place. Close them and run again to reclaim that space; the "
            "log names each folder.")
    if needs_reboot:
        lines.append("Reboot system for changes to take effect.")
    return lines




class RunFlow:
    # ---------------- run flow ---------------- #

    @staticmethod
    def _preflight_check(tasks):
        """Plain-language notices before a long run: pending reboot, low
        disk, admin rights, network (only when a task looks like it needs
        the internet — install/repair-download tasks). Returns a list of
        notice strings; empty = all clear. Non-blocking (ask-to-continue
        in the caller, never a hard stop)."""
        notices = []

        # pending reboot (CBS RebootPending) — repair tasks work better after
        try:
            import winreg as _wr
            with _wr.OpenKey(_wr.HKEY_LOCAL_MACHINE,
                             r"SOFTWARE\Microsoft\Windows\CurrentVersion\Component Based Servicing\RebootPending"):
                notices.append("Windows is waiting for a restart — restarting first makes repairs more reliable.")
        except OSError:
            pass
        except Exception:
            pass

        # free disk on C:
        try:
            import ctypes as _ct
            free = _ct.c_ulonglong(0)
            total = _ct.c_ulonglong(0)
            avail = _ct.c_ulonglong(0)
            if _ct.windll.kernel32.GetDiskFreeSpaceExW(_ct.c_wchar_p("C:\\"),
                                                       _ct.byref(avail),
                                                       _ct.byref(total),
                                                       _ct.byref(free)):
                gb = free.value / (1024 ** 3)
                if gb < 5:
                    notices.append(f"Only {gb:.1f} GB free on C: — cleaning needs breathing room; free up space first.")
        except Exception:
            pass

        # admin rights when selected tasks need them (limited mode would
        # silently skip them — better said up front)
        if not is_admin():
            admin_tasks = [t for t in tasks if t.admin_required]
            if admin_tasks:
                notices.append(f"{len(admin_tasks)} task(s) need Administrator — they'll be skipped in limited mode "
                               "(restart the app as Administrator for the full run).")

        # network when a task is an internet task (Install tab always is;
        # repair DISM RestoreHealth downloads too; time_sync/gaming_dns/
        # update_all/xbox_apps/store re-register also need the network —
        # F10: the old two-pattern check let those fail mid-run offline
        # instead of warning up front. Non-blocking notice only.
        _net_keys = {t.key for t in tasks}
        _needs_net = any(k.startswith("install_") for k in _net_keys) \
                     or bool(_net_keys & {"dism_restorehealth", "time_sync",
                                          "gaming_dns", "update_all",
                                          "xbox_apps", "store_apps_reregister"})
        if _needs_net:
            try:
                from app.downloader import has_network
                if not has_network():
                    notices.append("No internet connection detected — some tasks need to download files.")
            except Exception:
                pass

        return notices

    def _set_buttons_enabled(self, enabled: bool):
        def _do():
            try:
                for tab in self.tabs.values():
                    tab.set_run_enabled(enabled)
                if getattr(self, "cancel_btn", None) is not None:
                    self.cancel_btn.set_enabled(not enabled)
                if not enabled:
                    self._progress_show()
                else:
                    self._progress_hide()
            except Exception:
                pass
        self._dispatch.post(_do)

    def _progress_show(self):
        try:
            # The row (and its height) is already there; only the bar
            # appears inside it, so the page above cannot be shoved down.
            self.progress_row.show()
            self.cancel_btn.pack(side="right")
            self._progress_hidden = False
        except Exception:
            pass

    def _progress_hide(self):
        try:
            self.progress_row.hide()
            self.cancel_btn.pack_forget()
            self._progress_hidden = True
        except Exception:
            pass

    def _begin_task_progress(self, idx, total, label):
        """Remember which task owns the strip, for its progress callback.

        Called on the worker thread just before a task runs. The callback
        itself only marshals to Tk; nothing here touches a widget.
        """
        self._live_task = (int(idx), max(1, int(total)), str(label))
        self._live_pct = -1

    def _task_progress(self, pct, _raw=None):
        """Turn a tool's percentage into an HONEST run-strip reading.

        The bar is the whole run (task N of M), so a task's own percentage is
        folded into its slot: (idx + pct/100) / total. Without that the bar
        would restart at zero for every DISM/SFC step and look like a loop -
        the exact complaint that led here. The status line names the task and
        its own percentage, which is the number a user watching a 17-minute
        DISM run actually wants.
        """
        info = getattr(self, "_live_task", None)
        if info is None:
            return
        idx, total, label = info
        try:
            p = max(0.0, min(100.0, float(pct)))
        except (TypeError, ValueError):
            return
        whole = int(p)
        if whole == getattr(self, "_live_pct", -1):
            return
        self._live_pct = whole
        frac = (idx + p / 100.0) / float(total)

        def _do(_label=label, _pct=whole, _frac=frac, _idx=idx, _n=total):
            try:
                if not getattr(self, "_busy", False):
                    return
                self.progress_bar.set_indeterminate(False)
                self.progress_bar.set_fraction(_frac)
                self.set_status("%s \u2014 %d%% (step %d of %d)"
                                % (_label, _pct, _idx + 1, _n))
            except Exception:
                pass
        self._ui_hop(_do)

    @staticmethod
    def _invoke_single_task(ctx, task, mode: str = "run", strict_ok: bool = True):
        """Run ONE Task object (worker thread) and normalize its outcome
        to (status, task_bytes, exc) with status in ok|skip|stop|fail.

        H8/ARCH-004: the classification now lives in ONE place,
        `app.runner.task_runner.invoke_task`, which `app.scheduler`'s
        headless loop imports too. It used to be duplicated here and
        mirrored (by comment) in run_auto_clean, so a new outcome or a
        changed reading of a task's return value could land in one path and
        be forgotten in the other — which is how six tasks managed to report
        success while doing nothing on one path only.

        This stays a thin wrapper returning the legacy tuple because both
        call sites (the run_tasks worker and the Install Essentials worker)
        unpack three values, and that shape is part of the tested contract.
        See task_runner for the return-value reading and the exception map."""
        return invoke_task(ctx, task, mode=mode, strict_ok=strict_ok).as_tuple()

    def install_selected_mixed(self, apps, tasks):
        """Install-tab runner: checked Essentials tasks FIRST (Store brings
        winget, bundles before individual apps), then catalog apps — one
        worker, one progress bar, fully cancelable via the Stop button
        (install commands are registered with the cancel registry now)."""
        # Module-8: never hold _busy_lock across a modal (ThemedModal.wait
        # pumps a nested event loop — a worker completion or an after()
        # callback taking the same non-reentrant lock would self-deadlock
        # the Tk thread). Check-and-set is split: fast check under lock,
        # modal without lock, re-check under lock before claiming.
        with self._busy_lock:
            _busy_now = self._busy
        if _busy_now:
            _themed_showinfo(self.root, "An Operation Is Already Running",
                             "Please wait for the current operation to finish.",
                             accent=COLORS["accent_yellow"])
            return
        gen = self._claim_run()
        if gen is None:
            return

        # Same guarantee as run_tasks: the slot is claimed but no worker
        # exists yet, so any failure in the admin-gating dialog, the button
        # updates or the progress setup must release it. _abort_unstarted_run
        # explains what happened here before this guard existed.
        try:
            self._start_claimed_install(gen, tasks, apps)
        except Exception as exc:                       # noqa: BLE001
            self._abort_unstarted_run(exc)

    def _start_claimed_install(self, gen, tasks, apps):
        """The claimed-but-not-yet-started half of install_selected_mixed.

        The caller must already hold the claim (PREFLIGHT) for `gen`.
        """

        # Admin gating for Essentials that need it (same rule as other tabs)
        if not is_admin():
            admin_blocked = [t for t in tasks if t.admin_required]
            if admin_blocked:
                names = ", ".join(t.label for t in admin_blocked)
                self.log(f"Limited mode: skipping {len(admin_blocked)} admin-only installer(s): {names}")
                tasks = [t for t in tasks if not t.admin_required]
                if tasks or apps:
                    n_admin = len(admin_blocked)
                    _themed_showinfo(
                        self.root,
                        f"Skipped {n_admin} Admin-Only "
                        f"{'Installer' if n_admin == 1 else 'Installers'}",
                        f"{names}\n\nContinuing with the rest.",
                        accent=COLORS["accent_yellow"])
            if not tasks and not apps:
                _themed_showinfo(self.root, "Nothing to Run",
                                 "All selected installers need Administrator rights.",
                                 accent=COLORS["accent_yellow"])
                self._release_run()
                return

        # H9: a Stop that arrived during pre-flight must survive into the
        # worker. Clearing the token unconditionally here is what used to
        # discard it — the request was recorded, then wiped before any
        # worker existed to observe it.
        if self._state is not RunState.CANCELLING:
            self._cancel_requested = False
        for tab in self.tabs.values():
            tab.set_run_enabled(False)
        # user bug: the bar + Stop button never appeared during install /
        # update runs (only the tab buttons disabled) — a 10-minute
        # `winget upgrade --all` looked dead. Show them like run_tasks does.
        # Determinate % is impossible for winget batch runs, so indeterminate
        # + live per-package status lines (see _live_status_ctx) is the
        # honest signal.
        self._progress_show()
        self.cancel_btn.set_enabled(True)
        self.progress_bar.set_fraction(0.0)
        self.progress_bar.set_indeterminate(True)
        total = len(apps) + len(tasks)
        # A run whose only job is "Update Everything" installs nothing: it
        # upgrades what is already there, so its verdict, card and title must
        # say Update - everything else this worker runs is an install.
        kind = ("update" if not apps and len(tasks) == 1
                and getattr(tasks[0], "key", "") == "update_all" else "install")
        noun = _DONE_COPY[kind][0]
        status_text, banner = _install_start_copy(total, kind)
        self.set_status(status_text)
        # Same H9 rule as the run header: a Stop that arrived during pre-flight
        # means nothing will be attempted, so do not announce an install that
        # the loop's first iteration stops.
        if not self._cancel_requested:
            self.log(banner)

        ctx = TaskContext(
            log=self.log, set_status=self.set_status,
            cancelled=lambda: self._cancel_requested,
        )

        # BUG-018: _done is the ONLY thing that releases the run slot, hides
        # the progress bar and re-enables every tab's Run button. It used to be
        # called from exactly two places at the end of the worker, so any
        # exception escaping the worker - including the unguarded
        # `from app.tasks.install_tasks import ...` below - left _busy True,
        # the bar spinning forever, Run disabled on every tab and
        # _worker_thread non-None: an unrecoverable UI needing Task Manager.
        # Making _done idempotent is what lets the finally backstop below call
        # it unconditionally without double-reporting on the normal path.
        _done_called = [False]
        # U-001: which catalog apps failed, and their own download pages.
        # Filled in the worker, read by the _done hop (which runs on the Tk
        # thread and builds the failure dialog from this list).
        failed_apps = []

        def _done(ok, summary, ok_n=0, fail_n=0, title=None, detail=None,
                  level="info"):
            """`summary` is the status line (a full sentence). The notice gets
            its own `title` and `detail`, so a finished install names its
            outcome once in the strip and states only the specifics below."""
            if _done_called[0]:
                return
            _done_called[0] = True
            # The transcript's verdict, in the same shape a Clean/Repair/Tweak
            # run ends with. Written HERE so it lands once, on every path
            # through _done, before the dialog hop below.
            _log_block(self.log, summary)
            # audit fix: this ran on the WORKER thread — set_indeterminate/
            # set_run_enabled touch Tk widgets/Canvases, and Tkinter is not
            # thread-safe (rare non-deterministic crashes at install end).
            # Hop to the Tk thread first, like _set_buttons_enabled does.
            def _ui():
                # H10: after(0) hop scheduled after the slot was released —
                # a newer run may own the app by the time this executes.
                if not self._is_current(gen):
                    return
                self.progress_bar.set_indeterminate(False)
                self._set_progress(1, 1)
                self._progress_hide()
                self.cancel_btn.set_enabled(False)
                self._release_run()
                for tab in self.tabs.values():
                    tab.set_run_enabled(True)
                # installs/updates change what `winget upgrade` reports —
                # re-scan so "Update Apps (N)" never shows a stale number.
                try:
                    install_tab = self.tabs.get("Install")
                    if install_tab is not None and hasattr(install_tab, "refresh_update_count"):
                        install_tab.refresh_update_count()
                except Exception:
                    pass
                self.set_status(summary)
                # Install results now use the same themed card language
                # as everything else (was the last white OS box in the app).
                # The strip names the outcome and the body carries the
                # specifics - the two must not be the same sentence.
                _title = title or (f"{noun} Done" if ok else f"{noun} Failed")
                _detail = summary if detail is None else detail
                # Facts only the task knows (which update results a localized
                # winget could not report, for one) ride with the run and land
                # in the card body: the counts above cannot carry a list, and
                # the transcript is not on screen during an install run.
                for note in ctx.notes:
                    _detail = f"{_detail}\n\n{note}" if _detail else note
                # U-001: when catalog apps failed, the useful next step is
                # the vendor's own page, not a wall of text - the app knows
                # the link and the user cannot see the log it was hidden in.
                # The plain notice stays for everything else (an Essentials
                # task, or a failure with no catalog entry behind it).
                shown = False
                if failed_apps and any(f.get("url") for f in failed_apps):
                    try:
                        InstallFailedDialog(self.root, failed_apps,
                                            summary=_detail).wait()
                        shown = True
                    except Exception:
                        shown = False
                if not shown:
                    if level == "warning":
                        _themed_showwarning(self.root, _title, _detail)
                    elif level == "error":
                        _themed_showerror(self.root, _title, _detail)
                    else:
                        _themed_showinfo(self.root, _title, _detail,
                                         accent=COLORS["accent_green"])
            self._dispatch.post(_ui)

        def _worker_inner():
            ok_n, fail_n, skipped_n, stopped = 0, 0, 0, False

            def _record(task, status, exc):
                # F2-2: same outcome contract as the run_tasks worker (via
                # _invoke_single_task); the mapping to the install counters
                # lives here only. A skip (nothing to do on this machine) is
                # completed-with-skip — logged, never a failure, never wedging
                # the summary count (the old code had no skip branch at all,
                # silently dropping the outcome).
                nonlocal ok_n, fail_n, skipped_n, stopped
                if status == "ok":
                    ok_n += 1
                elif status == "skip":
                    skipped_n += 1
                    self.log(f"  (skipped) {task.label}: {exc}")
                elif status == "fail":
                    fail_n += 1
                    self.log(f"  ! {task.label} failed: {exc or 'no run available'}")
                elif status == "stop":
                    stopped = True
                    self.log(f"  {task.label} was cancelled: {exc}")

            # 1) Essentials tasks (sequential, honest per-task errors).
            # strict_ok=False: Essentials install tasks read boolean results
            # as their own outcome (a False here is NOT an invented failure).
            for task in tasks:
                if ctx.cancelled():
                    stopped = True
                    break
                self.set_status(_install_task_status(task))
                self.log(f"--- {task.label} ---")
                status, _bytes, exc = self._invoke_single_task(ctx, task, "run", strict_ok=False)
                _record(task, status, exc)
                if stopped:
                    break
            # 2) Catalog apps — per-app honesty (audit fix: the old code did
            # ok_n += len(apps) whenever install_selected_apps didn't raise,
            # counting failed apps as installed; the runner now reports the
            # real per-app outcome)
            if not stopped and apps:
                try:
                    # BUG-018: this import was directly above the try, so an
                    # ImportError/AttributeError/circular-import from it
                    # escaped the thread target and _done never ran. Inside
                    # the try, the existing handler turns it into an honest
                    # "all apps failed".
                    from app.tasks.install_tasks import install_selected_apps as _runner
                    app_ok, app_fail = _runner(ctx, apps)
                except TaskCancelled as exc:
                    stopped = True
                    self.log(f"  app installs were cancelled: {exc}")
                    app_ok, app_fail = [], []
                except Exception as exc:
                    # _runner raises only when EVERY app failed — count them
                    # all as failed instead of dying with _done never called
                    # (which wedged the UI busy with a frozen progress bar).
                    self.log(f"  ! app installs failed: {exc}")
                    app_ok, app_fail = [], [a["name"] for a in apps]
                else:
                    # A mid-batch Stop breaks the runner without raising —
                    # report Stopped, not Complete.
                    if ctx.cancelled():
                        stopped = True
                ok_n += len(app_ok)
                fail_n += len(app_fail)
                # Map each failed name back to its catalog entry so the
                # result dialog can offer the vendor's page (U-001). The
                # runner returns names, not dicts, so nothing upstream has
                # to change shape.
                failed_apps.extend(failed_app_rows(apps, app_fail))
            if stopped:
                # The block carries the verdict; this line only records who
                # ended the run and what they cut off. A stop before the first
                # item has nothing to cut off, and the block says so alone.
                if ok_n or fail_n:
                    self.log("Cancelled by user — remaining items were skipped.")
                status, title, detail = _install_done_copy(
                    ok_n, fail_n, skipped_n, stopped=True, kind=kind)
                _done(False, status, ok_n, fail_n, title=title, detail=detail,
                      level="warning")
            else:
                status, title, detail = _install_done_copy(
                    ok_n, fail_n, skipped_n, stopped=False, kind=kind)
                # A run where some (or all) items failed is not a plain
                # success - the card is tinted for the outcome it names.
                level = ("info" if not fail_n
                         else "error" if not ok_n else "warning")
                _done(True, status, ok_n, fail_n, title=title, detail=detail,
                      level=level)

        def _worker():
            # BUG-018 backstop. The work lives in _worker_inner so this wrapper
            # can guarantee the run is always released. Anything escaping
            # _worker_inner - an import error, a refactor-induced AttributeError,
            # a circular import, or even SystemExit - is caught here instead of
            # killing the thread and wedging the UI busy forever.
            try:
                _worker_inner()
            except TaskCancelled as exc:
                self.log(f"  install run was cancelled: {exc}")
                status, title, detail = _install_done_copy(
                    0, 0, 0, stopped=True, kind=kind)
                _done(False, status, title=title, detail=detail,
                      level="warning")
            except BaseException as exc:      # noqa: BLE001 - see above
                # Deliberately BaseException: on a worker thread the only two
                # sane options are report-it or wedge-the-UI, and a base
                # exception escaping here is exactly how the wedge happened.
                self.log(f"  ! install worker crashed: {exc!r}")
                _done(False, f"{noun} failed unexpectedly: {exc}",
                      title=f"{noun} Failed",
                      detail=f"{exc}\n\nSee the log for details.",
                      level="error")
            finally:
                # Unconditional release. _done is idempotent, so on the normal
                # path this is a no-op; if the body died without reporting,
                # the UI is still reset and the user can try again.
                _done(False, f"{noun} ended without reporting a result.",
                      title=f"{noun} Failed",
                      detail="It ended without reporting a result. "
                             "See the log for details.", level="error")

        thread = threading.Thread(target=_worker, daemon=True)
        self._mark_running(thread)
        thread.start()

    def run_tasks(self, tab_name, tasks, mode="run", quiet=False,
                  toast_kind=None, toast_extra=None):
        """quiet=True: pilot/headless mode — no interactive popups of any
        kind (preflight askyesno, warnings, admin gates) and no modal
        Scorecard at completion; the run reports via status line + toast
        only. A modal grab_set window must never appear over a running
        fullscreen game."""
        all_selected = list(tasks)  # H1: capture real selection before filtering
        if not is_admin():
            admin_tasks = [t for t in tasks if t.admin_required]
            if admin_tasks:
                names = ", ".join(t.label for t in admin_tasks)
                self.log(f"Limited mode: skipping {len(admin_tasks)} admin-only task(s): {names}")
                tasks = [t for t in tasks if not t.admin_required]
                if not quiet:
                    n_admin = len(admin_tasks)
                    _themed_showinfo(
                        self.root,
                        f"Skipped {n_admin} Admin-Only "
                        f"{'Task' if n_admin == 1 else 'Tasks'}",
                        f"{names}\n\nRunning the rest in limited mode.",
                        accent=COLORS["accent_yellow"],
                    )
            if not tasks:
                if toast_kind:
                    self._toast_async("Needs Administrator",
                                      "Restart as Administrator to run this.")
                if not quiet:
                    _themed_showinfo(
                        self.root,
                        "Nothing to Run",
                        "All selected tasks require Administrator rights. Restart as Administrator to run them.",
                        accent=COLORS["accent_yellow"],
                    )
                return

        # Module-8: same no-lock-across-modal rule as install_selected_mixed
        # — check under lock, modal without lock, claim under lock.
        with self._busy_lock:
            _busy_now = self._busy
        if _busy_now:
            if not quiet:
                _themed_showinfo(self.root, "An Operation Is Already Running",
                                 "Please wait for the current operation to finish.",
                                 accent=COLORS["accent_yellow"])
            return
        gen = self._claim_run()
        if gen is None:
            return

        # Everything from here to `thread.start()` happens with the run slot
        # claimed but NO worker alive yet. A raise anywhere in that window
        # used to unwind past _release_run() and strand the app in PREFLIGHT
        # permanently (busy forever, close refused) — see
        # _abort_unstarted_run. Nothing after thread.start() can raise, so
        # this handler can never pull the slot out from under a live worker.
        try:
            self._start_claimed_run(gen, tab_name, tasks, all_selected, mode,
                                    quiet, toast_kind, toast_extra)
        except Exception as exc:                       # noqa: BLE001
            self._abort_unstarted_run(exc)

    def _start_claimed_run(self, gen, tab_name, tasks, all_selected, mode,
                           quiet, toast_kind, toast_extra):
        """The claimed-but-not-yet-started half of run_tasks.

        Split out so the try/except that guarantees the run slot is released
        wraps exactly the pre-worker window and nothing else. The caller must
        already hold the claim (PREFLIGHT) for `gen`.
        """
        # ---- Pre-flight checks (user request): fail fast with plain
        # language instead of dying mid-DISM. Long-run = 5+ tasks or any
        # task flagged long-running by the Repair tab's nature. ----
        try:
            preflight = self._preflight_check(tasks)
        except Exception:
            preflight = []
        if preflight:
            if quiet:
                self.log("Pre-flight notices (quiet run): " + " | ".join(preflight))
            else:
                msg = "Before we start:\n\n" + "\n".join(f"  • {p}" for p in preflight)
                if not _themed_askyesno(self.root, "Start Anyway?", msg,
                                        accent=COLORS["accent_yellow"]):
                    # ok=False: a run that never executed must not be
                    # recorded as DONE in last_run_outcome.
                    self._release_run()
                    return
                self.log("Pre-flight notices: " + " | ".join(preflight))

        # Blocking hazards only — FYI notes are logged, never a popup
        # (curated presets like Deep Clean / Recommended are safe by
        # design, so they run silent; Custom combos that truly conflict
        # still ask here — except quiet runs, which log and continue:
        # the pilot only ever runs its own curated preset).
        try:
            info_notes = check_info_notices(tasks)
        except Exception:
            info_notes = []
        if info_notes:
            for _note in dict.fromkeys(info_notes):
                self.log(_note)
        warnings = check_dangerous_combos(tasks)
        if warnings:
            warnings = list(dict.fromkeys(warnings))
            if quiet:
                self.log("Hazard combo (quiet run, continuing): " + " | ".join(warnings))
            else:
                msg = "Potentially problematic task combinations detected:\n\n" + "\n\n".join(warnings)
                if not _themed_askyesno(self.root, "Run These Conflicting Tasks?",
                                        msg, accent=COLORS["accent_red"]):
                    self._release_run()
                    return

        # Data-loss confirmation (audit 2026-10, H-002). A red badge is not a
        # confirmation: Deep Clean is one click and includes emptying the
        # Recycle Bin and uninstalling preinstalled apps. Ask once, naming
        # exactly what will be destroyed. Quiet (Auto-Pilot) runs only ever
        # execute curated presets and must never put a modal over a game.
        risky = [t for t in tasks
                 if getattr(t, "risk", "") == RISK_DATA_LOSS]
        if risky and not quiet:
            names = "\n".join(
                f"  \u2022 {t.label} \u2014 {t.description}" for t in risky)
            if not _themed_askyesno(
                    self.root, "Run Tasks That Delete Data?",
                    "These selected tasks permanently remove data that the "
                    "app cannot restore:\n\n" + names,
                    accent=COLORS["accent_red"],
                    yes_text="Run anyway", no_text="Cancel"):
                self.log("Run cancelled at the data-loss confirmation.")
                self._release_run()
                return

        # Save selection for scheduler (H1: run mode only, full pre-filter list)
        # C-3 audit fix: quiet (background pilot) runs must NOT overwrite the
        # saved selection — a game launch would otherwise silently replace the
        # user's last manual Tweak pick with the Game Session preset, and the
        # next scheduled --auto-clean would apply session tweaks unattended.
        if mode == "run" and not quiet:
            # F08: single-lock RMW.
            try:
                from app.config_persist import update_config as _update
                def _mut(cfg, tab_name=tab_name, all_selected=all_selected):
                    cfg.setdefault("selected_tasks", {})[tab_name] = [t.key for t in all_selected]
                _update(_mut)
            except Exception:
                pass

        # H9: a Stop that arrived during pre-flight must survive into the
        # worker. Clearing the token unconditionally here is what used to
        # discard it — the request was recorded, then wiped before any
        # worker existed to observe it.
        if self._state is not RunState.CANCELLING:
            self._cancel_requested = False
        self._set_buttons_enabled(False)
        self.progress_bar.set_fraction(0.0)
        self.progress_bar.set_indeterminate(True)
        self.set_status(f"Starting {len(tasks)} task(s)...")

        # H10: the run owns its results list. `self._run_results` still points
        # at it (a completed run's results stay readable there), but
        # rebinding it on the next run no longer corrupts this run's data: the
        # worker and the deferred result card both close over the local.
        run_results = []
        self._run_results = run_results

        thread = threading.Thread(target=self._run_tasks_worker,
                                  args=(tab_name, tasks, mode, quiet,
                                        toast_kind, toast_extra, gen,
                                        run_results), daemon=True)
        self._mark_running(thread)
        thread.start()


    # ------------------------------------------------------------------ #
    # H9: run lifecycle state.
    #
    # RunState is now the single source of truth for "is a run active".
    # `_busy` is a VIEW over it — the same move H1 made for
    # `_cancel_requested` over CancellationToken — so all 37 existing
    # read/write sites and the smoke suite keep working untouched, while the
    # combinations the boolean could express but the engine could not act on
    # are now either impossible or explicitly modelled:
    #
    #   * "busy with no worker thread" -> PREFLIGHT, a real state. That window
    #     spans the admin gate and two modal askyesno prompts. A Stop pressed
    #     in it used to set a cancel token no worker existed to observe, and
    #     the run then started anyway.
    #   * "not busy but a worker is live" -> impossible: _claim_run,
    #     _mark_running and _release_run are the only writers, and each moves
    #     the state and _worker_thread under one lock.
    #
    # Both accessors tolerate missing attributes so __init__'s own
    # assignments cannot AttributeError regardless of statement order.
    # ------------------------------------------------------------------ #
    @property
    def _busy(self) -> bool:
        return getattr(self, "_state", RunState.IDLE).busy

    @_busy.setter
    def _busy(self, value) -> None:
        st = getattr(self, "_state", None)
        if st is None:
            self._state = st = RunState.IDLE
        if value:
            # Never downgrade a pending cancel. `_busy = True` from a
            # re-entrant call while a stop is in flight means "still busy",
            # not "start over" — clearing CANCELLING here would let a run
            # that the user already stopped keep going.
            self._state = st if st is RunState.CANCELLING else RunState.RUNNING
        else:
            self._state = RunState.IDLE

    def _abort_unstarted_run(self, exc):
        """Release a claimed run slot whose worker never started.

        run_tasks and install_selected_mixed both claim the single run slot
        (PREFLIGHT) and then do a fair amount of work before a worker exists:
        pre-flight notices, hazard-combo prompts, an admin-gating dialog, the
        config write, the button/progress updates and the scorecard's
        before-snapshot. Any of those can raise — a TclError on a root being
        destroyed, a grab_set failure, a bad drive query.

        Before this helper, such an exception unwound straight out of the Tk
        callback, so _release_run() was never reached and the app stayed in
        PREFLIGHT for the rest of the session: _busy stayed True forever and
        _on_close refused to close the window. The user had to kill the
        process. Both call sites now funnel failures here.

        Safe to call only BEFORE _mark_running() + thread.start(): once a
        worker owns the slot it releases it itself, and releasing underneath
        it would report a bogus outcome.
        """
        try:
            self._release_run()
        except Exception:
            pass
        try:
            # B-0xx: if the failure happened after _start_claimed_run disabled
            # the Run buttons and showed the progress bar (its _set_buttons_
            # enabled(False) / progress lines run before the worker exists),
            # nothing else re-enabled them — the app looked stuck despite the
            # "still usable" message. _set_buttons_enabled(True) also hides the
            # progress bar.
            self._set_buttons_enabled(True)
        except Exception:
            pass
        try:
            self.log(f"! Could not start the run: {exc}")
            import traceback
            self.log(traceback.format_exc().strip())
        except Exception:
            pass
        try:
            _themed_showinfo(
                self.root, "Could Not Start",
                f"{exc}\n\nThe app is still usable — see the log for details.",
                accent=COLORS["accent_red"])
        except Exception:
            pass

    def _claim_run(self):
        """Take the single run slot and return this run's generation number.

        Call under _busy_lock, or rely on this taking it — it never holds the
        lock across a caller modal, because ThemedModal.wait pumps a nested
        event loop and a worker completion taking the same non-reentrant lock
        would self-deadlock the Tk thread.

        Returns the new generation on success, or None if a run is already
        active. The generation is what lets a late completion tell whether it
        still owns the app (H10): a run releases the slot before scheduling
        its result card, so a newer run can start before the older run's
        deferred UI hop runs.
        """
        with self._busy_lock:
            if self._state.busy:
                return None
            self._state = RunState.PREFLIGHT
            self._run_gen += 1
            self._current_gen = self._run_gen
            return self._current_gen

    def _is_current(self, gen) -> bool:
        """True while `gen` is still the run that owns the app.

        A worker finishing hands the slot back with _release_run BEFORE it
        schedules its result card, toast and status hop with root.after(0).
        Those hops are therefore not ordered against a subsequent run: by the
        time they execute, the next run may have started, rebound
        _run_results, and taken the progress bar. Anything deferred must ask
        this first, or it will paint the previous run's outcome over the
        current one's.
        """
        if gen is None:
            return True          # caller opted out; behave as before
        return gen == self._current_gen

    def _mark_running(self, thread):
        """PREFLIGHT -> RUNNING, adopting the worker thread in the same
        critical section so the two can never disagree."""
        with self._busy_lock:
            self._worker_thread = thread
            if self._state is RunState.CANCELLING:
                return  # Stop arrived during pre-flight: stay cancelling
            self._state = RunState.RUNNING

    def _release_run(self):
        """End of a run: clear the worker thread and the cancel token and
        return to IDLE, all under one lock. Replaces the three separate
        statements that used to be able to interleave with a second run's
        claim.

        I-005: this used to take an `ok` flag and store it in a
        `last_run_outcome` property that NOTHING read (and that classified a
        user's Stop as ERROR). The run's outcome is already reported - by the
        summary block, the status line and the scorecard - so the flag and the
        property are gone rather than left as a second, unused source of truth.
        """
        with self._busy_lock:
            self._worker_thread = None
            self._state = RunState.IDLE
            # B-0xx: reset the cancel token INSIDE the critical section. It
            # used to run one statement after the lock released, so a second
            # run could claim the slot and have its Stop request wiped by this
            # run's late reset — a Stop on the new run was silently ignored.
            # Safe under the lock: the _cancel_requested setter does not take
            # _busy_lock itself.
            self._cancel_requested = False

    def _run_in_preflight(self) -> bool:
        """True while a run is claimed but not yet executing. The only window
        where a Stop request cannot be honoured by a worker, because there
        isn't one yet."""
        return self._state is RunState.PREFLIGHT

    # ARCH-006 / H1: `_cancel_requested` is now a VIEW over the
    # CancellationToken created in __init__, not a second piece of state.
    # The getter/setter pair is what lets all eight existing call sites (and
    # the smoke suite) keep reading and writing the familiar name while the
    # token stays the single source of truth — a bare bool can no longer be
    # flipped from a worker thread without taking the lock.
    #
    # Both accessors tolerate `self._cancel` being absent so that
    # __init__'s own `self._cancel_requested = False` cannot deadlock or
    # AttributeError regardless of statement order.
    @property
    def _cancel_requested(self) -> bool:
        tok = getattr(self, "_cancel", None)
        return bool(tok is not None and tok.is_cancelled())

    @_cancel_requested.setter
    def _cancel_requested(self, value) -> None:
        tok = getattr(self, "_cancel", None)
        if tok is None:
            tok = self._cancel = CancellationToken()
        if value:
            tok.cancel()
        else:
            tok.reset()

    def _request_cancel(self):
        if not self._busy:
            return
        # H9: record the intent in the state, not just the token. Previously a
        # Stop pressed during the pre-flight window set `_cancel_requested`
        # with no worker alive to observe it, the run then started anyway, and
        # `_cancel_requested = False` further down wiped the request before
        # the worker could see it. CANCELLING survives that reset: the setter
        # for `_busy` refuses to downgrade it, and _mark_running checks for
        # it so the run never silently proceeds as if un-cancelled.
        # read BEFORE the transition, or it is always False
        _was_preflight = self._run_in_preflight()
        with self._busy_lock:
            if self._state is not RunState.CANCELLING:
                self._state = RunState.CANCELLING
        self._cancel_requested = True
        if _was_preflight:
            # No worker exists yet, so "finishing the current task" would be a
            # lie — and logging BOTH lines is worse than either. Say the one
            # true thing and return: the cancel token is now set, the
            # conditional reset in run_tasks will not clear it, and
            # _mark_running keeps the state at CANCELLING, so the run stops
            # as soon as the worker starts.
            self.log("Stop requested before the run started — "
                     "nothing to interrupt.")
            try:
                from app.utils import cancel_current_command
                cancel_current_command()
            except Exception:
                pass
            return
        try:
            from app.utils import cancel_current_command
            cancel_current_command()
        except Exception:
            pass
        self.log("Stop requested — finishing the current task, then stopping.")

    def _run_tasks_worker(self, tab_name, tasks, mode, quiet=False,
                          toast_kind=None, toast_extra=None, gen=None,
                          run_results=None):
        verb = "Undoing" if mode == "revert" else "Running"

        # Scorecard 'before' snapshot -- taken HERE, on the worker thread,
        # not on the Tk thread before the thread was even started.
        #
        # This used to run in _start_claimed_run, which put up to 26
        # GetDiskFreeSpaceExW calls on the Tk thread before and after every
        # run. That is the exact hazard the disk monitor was moved off-thread
        # to fix (see _start_disk_monitor): one unreachable mapped network
        # drive stalls the UI for the length of its SMB timeout, so the app
        # froze before the run appeared to start and again before the
        # scorecard appeared. Measuring here is also more honest -- the
        # reading now brackets the actual work rather than the UI setup.
        #
        # A failed read yields None/{} and the scorecard renders without the
        # before/after bar. It never invents numbers.
        try:
            run_before_bytes = self._query_drive_free_total(_system_drive_root())
        except Exception:
            run_before_bytes = None
        try:
            run_before_drives = _snapshot_all_drives(
                self._query_drives, self._query_drive_free_total)
        except Exception:
            run_before_drives = {}
        # Still published for anything that wants to inspect the last run's
        # baseline; the worker is the only writer and the scorecard reads the
        # closure above, not this.
        self._run_before_bytes = run_before_bytes
        self._run_before_drives = run_before_drives
        # N-002: a cheap memory / restart-flag reading, bracketed around the
        # run exactly like the disk snapshots above (same worker thread, same
        # fail-soft contract: an unreadable value is None and the scorecard
        # simply omits that line rather than inventing a change).
        try:
            from app import benchmark as _bench
            run_before_measure = _bench.snapshot()
        except Exception:
            run_before_measure = None
        self._run_before_measure = run_before_measure

        ctx = TaskContext(log=self.log, set_status=self.set_status,
                          cancelled=lambda: self._cancel_requested,
                          progress=self._task_progress)
        # 2.5: opt-in Quarantine bin. Attached only for a real apply run; a
        # revert restores registry tweaks and a dry_run/Storage-Insight ctx
        # never deletes, so neither needs (or should get) a bin.
        try:
            from app import quarantine as _q
            if mode != "revert" and _q.enabled():
                ctx.quarantine = _q.Quarantine()
        except Exception:
            pass

        total_bytes = 0
        completed, failed = 0, 0
        skipped_n = 0
        cancelled = False
        self._set_progress(0, len(tasks))

        # H9: a Stop pressed during pre-flight reaches the worker with the
        # token already set. No banner for a run that never starts.
        _header = _run_header_line(verb, len(tasks), tab_name, ctx.cancelled())
        if _header:
            self.log(_header)

        # Scorecard collectors (worker-thread writes only; the dialog
        # builder runs on the Tk thread AFTER the worker joins its UI
        # hop, so no lock is needed — same single-writer pattern as the
        # existing counters above).
        # H10: prefer the list handed in by run_tasks. Falling back to the
        # attribute keeps the pre-H10 calling convention working, but then the
        # list is shared with any later run.
        if run_results is None:
            run_results = getattr(self, "_run_results", None)
            if run_results is None:
                run_results = self._run_results = []
        results = run_results

        def _record(idx, task, status, task_bytes, exc, task_changes=()):
            # F2-1: the single place mapping the shared helper's outcome to
            # the scorecard rows + counters + applied-tweak bookkeeping
            # (worker-thread locals only; one result row per attempted task,
            # same as the old inline branches).
            #
            # R-005: the row carries this task's OWN typed changes as a 5th
            # element, so the card can reveal them per row. The run-wide list
            # still comes from ctx.changes.
            nonlocal total_bytes, completed, failed, skipped_n, cancelled
            task_changes = tuple(task_changes or ())
            if status == "ok":
                completed += 1
                total_bytes += task_bytes
                results.append((task.label, "ok", task_bytes, None,
                                task_changes))
                # Phase 2 (#14): keep the applied-tweak registry truthful
                if mode == "revert":
                    mark_tweak_reverted(task.key)
                else:
                    if task.revert is not None:  # it's a tweak
                        mark_tweak_applied(task.key)
            elif status == "skip":
                # B5 audit fix: a skip means nothing changed on this
                # machine — count it as completed-with-skip, log the
                # reason, but do NOT record the tweak as applied (the
                # '✓ Active' badge must show what is actually active).
                skipped_n += 1
                results.append((task.label, "skip", 0,
                                str(exc) if exc else None, task_changes))
                self.log(f"  (skipped) {task.label}: {exc}")
            elif status == "stop":
                # F-2 audit fix: utils.run_cmd_checked deliberately raises
                # TaskCancelled for a user Stop (its H6 contract) — the run
                # was stopped, not failed; no tweak is marked applied/
                # reverted, remaining tasks are skipped by the next
                # iteration's cancelled() check, summary reports 'stopped'.
                cancelled = True
                results.append((task.label, "stop", 0, None, task_changes))
                self.log(f"  (stopped) {task.label}: {exc}")
            else:  # "fail"
                failed += 1
                if exc is None:
                    # helper reports no run/revert function on the Task
                    reason = f"No {'revert' if mode == 'revert' else 'run'} step defined for this task"
                    self.log(f"  ! No {'revert' if mode == 'revert' else 'run'} for '{task.label}'")
                else:
                    reason = str(exc)
                    self.log(f"  ! ERROR in '{task.label}': {exc}")
                results.append((task.label, "fail", 0, reason, task_changes))
            self._set_progress(idx + 1, len(tasks))

        # BUG-018 backstop. The release of the run slot lives at the END of this
        # function, with no top-level try/finally guarding it, so anything that
        # escaped here left _busy True, the bar spinning, every Run button
        # disabled and _worker_thread set forever - an unrecoverable UI needing
        # Task Manager. Anything unexpected is caught, logged, and turned into
        # an honest failed row so the normal summary/release path still runs.
        crashed = None
        try:
            for idx, task in enumerate(tasks):
                if ctx.cancelled():
                    # idx 0 means nothing was attempted: the header was never
                    # written and _request_cancel already said where the stop
                    # landed, so "remaining tasks were skipped" would explain
                    # a run the transcript never saw start.
                    if idx:
                        self.log("Cancelled by user — remaining tasks were "
                                 "skipped.")
                    cancelled = True
                    break
                self.set_status(f"{verb}: {task.label}...")
                self._begin_task_progress(idx, len(tasks), task.label)
                _start = change_cursor(ctx)
                status, task_bytes, exc = self._invoke_single_task(ctx, task, mode)
                self._live_task = None
                _record(idx, task, status, task_bytes, exc,
                        changes_since(ctx, _start))
        except TaskCancelled as exc:
            cancelled = True
            self.log(f"Run cancelled: {exc}")
        except BaseException as exc:          # noqa: BLE001 - see above
            # Deliberately BaseException. On a worker thread the alternatives are
            # report-it or wedge-the-UI, and wedging the UI is the bug.
            crashed = exc
            self.log(f"  ! run worker crashed: {exc!r}")
            # Every task without a row gets a 'fail' row, so the scorecard
            # matches the task list and the counts add up instead of showing
            # phantom successes.
            for t in tasks[len(results):]:
                results.append((t.label, "fail", 0, str(exc), ()))
                failed += 1

        # How many tasks were actually attempted, read BEFORE the padding
        # below turns never-started tasks into 'stopped' rows: the summary has
        # to tell a stop after two tasks from a stop before the first one.
        attempted = len(results)

        # tasks never attempted (user Stop mid-run) show as 'stopped' rows
        # so the scorecard tells the whole story. Every attempted task
        # appended exactly one result, so len(results) == attempted count;
        # the remainder are the never-started ones. (Only after a real
        # stop — a completed loop leaves nothing remaining.)
        if cancelled and len(results) < len(tasks):
            for t in tasks[len(results):]:
                results.append((t.label, "stop", 0, None, ()))

        # B-006: files moved into the opt-in Quarantine bin still occupy disk.
        # Task totals include them (they did leave the target folder), so the
        # run-level figure must subtract what was only relocated or the
        # scorecard/toast/history claim space that was never freed.
        moved_bytes = 0
        try:
            if ctx.quarantine is not None:
                moved_bytes = int(getattr(ctx.quarantine, "moved_bytes", 0) or 0)
        except Exception:
            moved_bytes = 0
        try:
            from app.quarantine import split_reclaimed
            reclaimed_bytes, moved_bytes = split_reclaimed(total_bytes, moved_bytes)
        except Exception:
            reclaimed_bytes, moved_bytes = total_bytes, 0

        # Scorecard 'after' snapshot -- measured HERE, on the worker thread,
        # immediately after the last task finished. It used to be taken after
        # the completion hop was queued, which put up to 26 blocking
        # GetDiskFreeSpaceExW calls between the run finishing and the result
        # card appearing. One unreachable mapped network drive stalled the UI
        # for its whole SMB timeout. Nothing between here and `_done_popup`
        # changes the disks, so moving it up the function changes no reading.
        #
        # A failed read degrades to "no before/after bar"; it never invents
        # numbers.
        try:
            after = self._query_drive_free_total(_system_drive_root())
        except Exception:
            after = None
        try:
            after_drives = _snapshot_all_drives(
                self._query_drives, self._query_drive_free_total)
        except Exception:
            after_drives = {}
        # N-002: the matching end-of-run reading for the scorecard's
        # before/after block. Read HERE (worker thread) so it never touches
        # the Tk thread.
        try:
            from app import benchmark as _bench
            after_measure = _bench.snapshot()
        except Exception:
            after_measure = None

        # N-010: reconcile "what the tasks removed" with "what the disk
        # gained" ONCE, here, and let every surface read the same object. The
        # transcript line, the status line, the toast, the ledger entry and
        # the scorecard used to each render `reclaimed_bytes` as if it were a
        # measurement; on a run whose caches apps recreated within seconds
        # that number was simply not true.
        _before_map = dict(run_before_drives or {})
        _after_map = dict(after_drives or {})
        _system_root = _system_drive_root()
        # A failed all-drives query still leaves the system-drive pair above:
        # one honest reading is enough to verify the number.
        if _system_root not in _before_map and run_before_bytes:
            _before_map[_system_root] = run_before_bytes
        if _system_root not in _after_map and after:
            _after_map[_system_root] = after
        accounted = _account_freed(
            reclaimed_bytes, _before_map, _after_map,
            moved_bytes=moved_bytes,
            locked_files=int(getattr(ctx, "skipped_locked", 0) or 0))

        # H11: applied-badge state needs no refresh — the view reads live.
        # audit fix: cached Undo bodies hold stale "✓ Active" badges built
        # from the PRE-run tweak_state. _clear_body_cache existed for exactly
        # this but had zero call sites — _enter_undo only popped the 'undo'
        # key, leaving preset/custom bodies stale after a revert. Drop all
        # cached bodies now that the machine state has actually changed.
        # H11: _clear_body_cache destroys widgets — marshal to the Tk
        # thread like every other worker→UI hop (direct call mutated the
        # cache dicts from the worker and leaked the widget handles).
        try:
            tweak_tab = self.tabs.get("Tweak")
            if tweak_tab is not None:
                self._dispatch.post(tweak_tab._clear_body_cache)
        except Exception:
            pass

        needs_reboot = any(getattr(t, "risk", "") == "REBOOT REQUIRED" for t in tasks)
        summary_lines = _run_summary_lines(
            tab_name, mode, cancelled, crashed, completed, skipped_n, failed,
            attempted, reclaimed_bytes, moved_bytes, needs_reboot,
            locked_files=int(getattr(ctx, "skipped_locked", 0) or 0),
            accounted=accounted)
        summary = "\n".join(summary_lines)
        # One statement of the reboot requirement, one of each count: the
        # block used to print "Reboot system for changes to take effect."
        # again on its own line right under a summary that already said it.
        _log_block(self.log, summary)

        # F-003 fix: marshal through the Tk thread — was a direct
        # cross-thread Canvas call (see _stop_indeterminate docstring).
        # H10: if a newer run already owns the app, this completion is stale.
        # Releasing the slot here would hand it back while the new run is
        # mid-flight, and re-enabling the buttons/status would paint the old
        # run's outcome over the new one. Drop it on the floor.
        if not self._is_current(gen):
            self.log("  ! stale run completion ignored (superseded by a newer run).")
            return

        self._stop_indeterminate()
        self._set_progress(1, 1)
        self.set_status(summary_lines[0] + (" — Reboot needed" if needs_reboot else ""))

        self._release_run()
        self._set_buttons_enabled(True)

        # Native completion toast — scheduled BEFORE the scorecard hop:
        # _done_popup blocks in the scorecard's modal wait_window(), so
        # any hop queued after it would only run once the user closes the
        # dialog — useless when the app is minimized (the exact case the
        # toast exists for: transient signal while the scorecard waits
        # unseen behind the taskbar). Tk fires same-delay after()s in
        # schedule order, so the toast decision always runs first. Both
        # the state() read and the toast fire on the Tk thread; the
        # worker only schedules. Headless scheduled runs never reach this
        # code at all (scheduler.py runs its own path, gui not imported).
        if toast_kind:
            # tray-started run: always report the real outcome
            try:
                _tt, _tm = self._result_toast(
                    toast_kind, toast_extra, accounted.credited, completed,
                    failed, skipped_n, cancelled)
                self._toast_async(_tt, _tm)
            except Exception:
                pass
        if not cancelled and tab_name == "Clean" and mode == "run" and not toast_kind:
            def _toast_if_minimized():
                # H10: scheduled with after(0) AFTER the slot was released, so
                # a newer run may own the app by now.
                if not self._is_current(gen):
                    return
                try:
                    if self.root.state() != "iconic":
                        return
                    # audit minor 3: pass the failure count so the toast
                    # copy is truthful (only claims a clean completion
                    # when nothing failed)
                    threading.Thread(target=notify_clean_complete,
                                     args=(accounted.credited, completed,
                                           failed, skipped_n),
                                     daemon=True).start()
                except Exception:
                    pass
            try:
                self._dispatch.post(_toast_if_minimized)
            except Exception:
                pass

        def _done_popup():
            # H10: this hop is where a superseded run does its damage. The
            # slot was released before it was scheduled, so by now another run
            # may have rebound _run_results and taken the progress bar. Show
            # nothing rather than a card built from someone else's data.
            if not self._is_current(gen):
                return
            # Scorecard (user-approved 2026-09): the themed results card
            # replaces the generic Done/Stopped messagebox. Built on the
            # Tk thread with the worker's collected per-task rows; the
            # freed-space 'after' snapshot is read HERE (the run's
            # deletions have settled by the time this hop executes).
            # quiet (pilot) runs skip the modal entirely — a grab_set
            # window over a fullscreen game is hostile; toast + status
            # line carry the outcome instead.
            # Feature 10: remember this run BEFORE the card is built, so
            # the 30-day sentence on the card includes it. Recorded for
            # quiet (Auto-Pilot) runs too — they free real space — but
            # never for Undo runs, which give space back rather than
            # freeing it.
            # R-005: a run earns a history entry when it freed space OR
            # changed something. Tweak/Repair/Undo runs free nothing, and
            # they are exactly where typed changes matter; `kind` says which
            # surface produced the entry.
            _run_changes = tuple(ctx.changes)
            _run_kind = ("undo" if mode == "revert"
                         else {"Clean": "clean", "Tweak": "tweak",
                               "Repair": "repair"}.get(
                                   tab_name, (tab_name or "run").lower()))
            # N-010: the ledger takes the CREDITED number - removed bytes,
            # capped at what the disk measurably gained - so the 30-day total
            # is a figure the user could have checked in Explorer rather than
            # a sum of file sizes that may never have left the drive. With no
            # measurement it is the removed total, as it always was.
            if accounted.credited > 0 or _run_changes:
                try:
                    from app.config_persist import record_run
                    record_run(accounted.credited, kind=_run_kind,
                               changes=_run_changes)
                except Exception:
                    pass
            # A clean spends part of the junk estimate it was planned from:
            # the finding on the bus and the cached walk both quoted what the
            # measured cleaners WOULD free, and some of that is now gone. Left
            # up, they would have the Health row and the hub card ask the user
            # to fix what this run just fixed. storage_scan owns the rule (a
            # run that removed nothing contradicts nothing) and the next walk
            # republishes a number that is true again.
            if tab_name == "Clean" and mode == "run" and not cancelled:
                try:
                    from app.storage_scan import invalidate_junk_reading
                    invalidate_junk_reading(
                        [getattr(t, "key", "") for t in tasks],
                        accounted.counted)
                except Exception:
                    pass
            if quiet:
                try:
                    if tab_name == "Clean":
                        self.set_status(
                            f"Freed {format_bytes(accounted.credited)}."
                            if accounted.credited else "Nothing to clean.")
                    else:
                        verb = "restored" if mode == "revert" else "applied"
                        self.set_status(f"Auto-Pilot: session tweaks {verb}.")
                except Exception:
                    pass
                return
            # `after` / `after_drives` are closed over from the WORKER thread,
            # which measured them after the last task finished. They used to be
            # read here, on the Tk thread, which put another 26 blocking
            # GetDiskFreeSpaceExW calls in front of the scorecard -- the same
            # UI-freeze hazard the disk monitor was moved off-thread to avoid.
            run_again = None
            if (not cancelled and tab_name == "Clean" and mode == "run"):
                # 'Clean Again' — re-run the same selection. The task list
                # is captured by closure; in preset mode the page still
                # holds the same preset, in custom the same toggles.
                same_tasks = list(tasks)
                def _run_again():
                    self.run_tasks(tab_name, same_tasks, mode="run")
                run_again = _run_again
            try:
                card = ScorecardDialog(
                    self.root,
                    tab_name=tab_name, mode=mode, cancelled=cancelled,
                    # H10: `results` is this run's own list (closed over),
                    # not self._run_results, which a newer run may have
                    # rebound by the time this hop runs.
                    results=list(results),
                    # N-010: the card's hero number is the VERIFIED figure,
                    # not the sum of file sizes. `accounted` carries both, so
                    # the explanation under it can name the difference.
                    total_bytes=accounted.credited,
                    accounted=accounted,
                    before=run_before_bytes,
                    after=after,
                    needs_reboot=needs_reboot,
                    on_run_again=run_again,
                    before_drives=run_before_drives,
                    after_drives=after_drives,
                    measurements=(run_before_measure, after_measure),
                    # R-005: the shared write layer's ledger for this run.
                    # Copied because the worker's context is done, but the
                    # dialog must own an immutable snapshot of it.
                    changes=tuple(ctx.changes),
                )
                card.wait()
                # Audit fix (Auto-Maintenance discoverability): the
                # scheduler only lives behind a small corner wrench icon —
                # easy to never notice it exists. A one-time tip (never
                # shown again after this) after the first successful Clean
                # run points new users at it, without adding a permanent
                # banner or nagging every run.
                if (tab_name == "Clean" and mode == "run" and not cancelled
                        and results
                        and any(r[1] == "ok" for r in results)):
                    self._maybe_show_automaint_tip()
            except Exception:
                # the scorecard must never mask a run's outcome — fall
                # back to the old messagebox if building it fails
                try:
                    if mode == "revert":
                        _done_title = ("Undo Stopped" if cancelled
                                       else "Undo Complete")
                    else:
                        _done_title = (f"{tab_name} Run Stopped" if cancelled
                                       else f"{tab_name} Complete")
                    _themed_showinfo(
                        self.root, _done_title, summary,
                        accent=COLORS["accent_yellow" if cancelled
                                      else "accent_green"])
                except Exception:
                    pass
        # Routed through the dispatcher rather than root.after(0) called from
        # this worker thread: a cross-thread .after() can be dropped outright,
        # and this is the one hop whose loss means the user never sees the
        # result card. The dispatcher also stops cleanly on a destroyed root,
        # which is what the old H6 TclError guard was there for.
        self._dispatch.post(_done_popup)
