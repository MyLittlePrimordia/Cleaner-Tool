"""Application: the window shell, run lifecycle, tray, and tab routing.

Extracted from app/gui.py (H14). This is the composition root — it is the
one module allowed to know about every tab and every dialog, because it
is what opens them. Everything below it in app/ui knows only about the
layer beneath itself.

app/gui.py remains a re-export facade over this and the other layers, so
nothing outside the app package had to change.
"""
from __future__ import annotations

import os
import pathlib
import subprocess
import sys
import threading
import time

from app.utils import resolve_asset_path
import tkinter as tk
from tkinter import ttk, messagebox
from app.config import APP_NAME, WINDOW_MIN_SIZE, WINDOW_SIZE
from app.config_persist import get_tweak_state, mark_tweak_applied, mark_tweak_reverted
from app.elevation import is_admin, relaunch_as_admin
from app.runner import CancellationToken, RunState
from app.ui.session import SessionOrchestrator, SessionState
from app.ui.tkdispatch import TkDispatcher
from app.runner.task_runner import invoke_task
from app.tab_presets import PRESETS, TABS, TAB_NAMES
from app.toast import notify_clean_complete, notify_low_space, show_toast
from app.ui.base import ThemedModal, Tooltip, _themed_askyesno, _themed_showinfo
from app.ui.dialogs.dns_tester_dialog import DnsTesterDialog
from app.ui.dialogs.drive_toolkit_dialog import DriveToolkitDialog
from app.ui.dialogs.game_server_ping_dialog import GameServerPingDialog
from app.ui.dialogs.gamepad_dialog import GamepadDialog
from app.ui.dialogs.hardware_monitor_dialog import HardwareMonitorDialog
from app.ui.dialogs.keyboard_tester_dialog import KeyboardTesterDialog
from app.ui.dialogs.mic_check_dialog import MicCheckDialog
from app.ui.dialogs.monitor_test_dialog import MonitorTestDialog
from app.ui.dialogs.mouse_tester_dialog import MouseTesterDialog
from app.ui.dialogs.pilot_dialog import PilotDialog
from app.ui.dialogs.process_manager_dialog import ProcessManagerDialog
from app.ui.dialogs.quick_tools_dialog import QuickToolsDialog
from app.ui.dialogs.scorecard_dialog import ScorecardDialog
from app.ui.dialogs.speaker_test_dialog import SpeakerTestDialog
from app.ui.dialogs.specs_dialog import SpecsDialog
from app.ui.dialogs.speed_test_dialog import SpeedTestDialog
from app.ui.dialogs.startup_manager_dialog import StartupManagerDialog
from app.ui.dialogs.storage_insight_dialog import StorageInsightDialog
from app.ui.dialogs.uninstall_programs_dialog import UninstallProgramsDialog
from app.ui.dialogs.webcam_dialog import WebcamDialog
from app.ui.hardware.drives import _snapshot_all_drives, _system_drive_root
from app.ui.theme import COLORS, F, TAB_ACCENTS, _hex_lerp
from app.ui.widgets import AnimatedButton, AnimatedProgressBar, ScrollableRoundedPanel
from app.ui.widgets.chrome import BorderlessChrome, _enable_dark_titlebar, _set_window_icon
from app.utils import IS_WINDOWS, TaskCancelled, TaskContext, format_bytes
from app.warnings import check_dangerous_combos, check_info_notices
from app.ui.tabs.admingateframe import AdminGateFrame
from app.ui.tabs.tasktab import TaskTab
from app.ui.tabs.installtab import InstallTab
from app.ui.tabs.toolstab import ToolsTab

#: The one project this app checks for updates. Used both to query the
#: release API and to validate the download link that comes back from it
#: (MED-012) - if those ever disagree, the link check stops matching.
UPDATE_REPO = "MyLittlePrimordia/Cleaner-Tool"


# this file, so the table lives beside the code that consumes it.)
_TAB_STATUS = {
    "Clean": "Pick a preset to begin.",
    "Repair": "Pick a preset to begin.",
    "Tweak": "Pick a preset to begin.",
    "Install": "Select apps to install.",
    "Tools": "Choose a tool.",
}



class Application:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title(APP_NAME)
        self.root.geometry(WINDOW_SIZE)
        self.root.minsize(*WINDOW_MIN_SIZE)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self.root.configure(bg=COLORS["bg"])
        _set_window_icon(self.root)
        _enable_dark_titlebar(self.root)
        # custom borderless chrome (engages lazily on first <Map> so the
        # headless harness never flashes; falls back to native on failure)
        self._chrome = BorderlessChrome(root, self)

        # H9: RunState is the single source of truth for "is a run active".
        # `_busy` below is a read/write VIEW over it (same trick H1 used for
        # `_cancel_requested` over CancellationToken), so all 37 existing
        # call sites — and the smoke suite — keep working while the
        # unrepresentable combinations go away. Starts IDLE; `_claim_run`
        # moves it through PREFLIGHT -> RUNNING -> CANCELLING -> IDLE.
        self._state = RunState.IDLE
        # H10: run generation. Monotonic, bumped by _claim_run. A worker
        # hands the slot back with _release_run BEFORE it schedules its
        # result card / toast / status hop via root.after(0), so those
        # hops are not ordered against a following run. The generation is
        # how a late completion knows it no longer owns the app.
        self._run_gen = 0
        self._current_gen = 0
        # One-way bridge onto the Tk thread for WORKER -> UI hops. Calling
        # root.after(0, fn) from a worker thread is the documented cross-thread
        # Tcl race: the callback is dropped outright when the main thread is
        # not inside the event loop at that instant. It is intermittent, which
        # is why it reads as "sometimes the results card doesn't appear" rather
        # than as a bug — confirmed by driving a real run headlessly, where
        # the card was never built. Only the run-completion path is routed
        # through this for now (scorecard, completion toast, button
        # re-enable, the install runner's UI hop) because those are the ones
        # where a dropped call leaves the user with no result at all and a
        # stuck interface. The remaining cross-thread .after() calls in this
        # file are lower-stakes and deliberately left alone for now.
        self._dispatch = TkDispatcher(self.root, on_error=self._log_full)
        # INFO-005: these two tray-action busy flags were never initialised
        # here. Their first read was a nested `getattr(self, "_tray_ping_busy",
        # False)`, so the whole thing worked only because of a defensive
        # default - a flag that exists only via getattr is a latent
        # AttributeError for any future caller that reads it directly, and
        # anything that assigns it (rather than clearing it) would silently
        # create it.
        #
        # This matters more after MED-014: a dropped cross-thread `after` used
        # to leave these True and kill "Check My Ping" / "Speed Test" for the
        # session. A flag that is guaranteed to exist is a flag a guard can
        # actually rely on.
        self._tray_ping_busy = False
        self._tray_speedtest_busy = False
        # BUG-023: reference-counted stay-awake hold, initialised here rather
        # than conjured by getattr on first use, so the count is always a real
        # int. _set_stay_awake mutates it under _busy_lock, because the
        # Auto-Pilot watcher thread also calls in from off the Tk thread.
        self._stay_awake_count = 0
        # Guards the two flags above and serialises tray actions against
        # each other, so two tray clicks cannot both pass the busy check.
        self._tray_action_lock = threading.Lock()
        self._dispatch.start()
        self._busy = False
        self._busy_lock = threading.Lock()
        # ARCH-006 / H1: adopt the previously-orphaned CancellationToken from
        # the old app/services package as the SINGLE source of truth for the
        # cancel flag. `_cancel_requested` is kept below as a thin property
        # over this token, so all eight existing read/write sites (and the
        # smoke suite's `getattr(app, "_cancel_requested")` assertion) keep
        # working unchanged while the bool can no longer be mutated without
        # going through the lock.
        self._cancel = CancellationToken()
        self._cancel_requested = False
        self._worker_thread = None
        # H11: tweak_state is no longer a snapshot cached here. It is a
        # read-only property that re-reads the live registry on every access
        # (see the property next to _is_current). The four hand-placed cache
        # refreshes it replaced all lagged runs that finished through a path
        # nobody remembered to update from — the tasktab comment at
        # _context_applied_count said so out loud.
        # H15: the Auto-Pilot lifecycle (watcher object, the watcher->Tk
        # event queue and its pump, the applied/fresh key bookkeeping the
        # revert reconciles against) lives in one object now. The
        # _pilot* names below are thin forwarders, kept because
        # tools/smoke_async.py drives the watcher through them.
        self._pilot_sess = SessionOrchestrator(
            root=self.root,
            on_apply=self._pilot_apply,
            on_revert=self._pilot_revert,
            log=self.log)
        self._pilot_dialog = None

        self._build_style()
        if is_admin():
            self._build_main_ui()
        else:
            # Gate triage (auto-elevate project): the Gate only earns its
            # screen when it can change something. remember_limited (and
            # not auto_elevate, which wins) or a selection with no admin
            # tasks -> straight to limited mode, no interrogation. An
            # opted-in auto_elevate whose task is broken still shows the
            # Gate (with its notice line) — approving once more repairs it.
            _show_gate = True
            _elevate_pending = False
            try:
                from app import elevated_launch as _el
                from app.config_persist import load_config as _load_cfg2
                _cfg2 = _load_cfg2()
                if bool(_cfg2.get("remember_limited", False)) \
                        and not bool(_cfg2.get("auto_elevate", False)):
                    _show_gate = False
                elif not _el.saved_selection_needs_admin():
                    _show_gate = False
                    try:
                        _elevate_pending = bool(_cfg2.get("auto_elevate", False)) \
                            and _el.notice_for_gate() is not None
                    except Exception:
                        pass
            except Exception:
                pass
            if _show_gate:
                AdminGateFrame(self.root, on_continue_limited=self._build_main_ui)
            else:
                self._build_main_ui()
                if _elevate_pending:
                    try:
                        self.set_status("Auto-elevate is on but needs one more approval "
                                        "(click the 🔓 badge when ready).")
                    except Exception:
                        pass
        # single-instance focus handoff (tray/auto-elevate foundation): a
        # second launch pulses our show-event instead of opening a twin —
        # surface here when it arrives. Best-effort, self-rescheduling.
        self._start_single_instance_poll()

    def _start_single_instance_poll(self):
        """Begin the show-event poll (see app/single_instance). A second
        launch focuses this window instead of opening a twin. Cheap
        (~one kernel wait per 500ms), runs for app life, never raises."""
        try:
            from app import single_instance as _si
            if not _si.poll_enabled():
                return
        except Exception:
            return
        self._si_poll()

    def _si_poll(self):
        try:
            from app import single_instance as _si
            if _si.check_signalled():
                for _fn in (self.root.deiconify, self.root.lift,
                            self.root.focus_force):
                    try:
                        _fn()
                    except Exception:
                        pass
        except Exception:
            pass
        try:
            if self.root.winfo_exists():
                self.root.after(500, self._si_poll)
        except Exception:
            pass

    # ---------------- system tray (resident watchdog UI) ---------------- #

    def _ensure_tray(self) -> bool:
        """Start the tray icon on first need (lazy: no icon until the
        window hides once or --tray boots hidden — the suites never hide,
        so they never sprout icons). True while the icon lives."""
        try:
            tray = getattr(self, "_tray", None)
            if tray is not None and tray.running:
                return True
        except Exception:
            pass
        try:
            from app import tray as _traymod
            icon = _traymod.TrayIcon(
                tooltip="Cleaner Tool",
                items=[(101, "Open Cleaner Tool", self.tray_open),
                       (102, self._tray_game_label, self.tray_toggle_game_session),
                       (103, "\U0001f9f9 Quick Clean", self.tray_quick_clean),
                       (105, "\U0001f4f6 Check My Ping", self.tray_check_ping),
                       (106, "\U0001f680 Speed Test", self.tray_speed_test),
                       (None, None, None),
                       (104, "Quit", self.tray_quit)],
                on_ui_thread=lambda fn: self._tray_ui_hop(fn))
            if icon.start():
                self._tray = icon
                return True
            self._tray = None
        except Exception:
            try:
                self._tray = None
            except Exception:
                pass
        return False

    def _tray_ui_hop(self, fn) -> None:
        """Marshal a tray-thread callback onto the Tk thread. Best-effort:
        post-quit calls land nowhere instead of raising."""
        try:
            self._dispatch.post(fn)
        except Exception:
            pass

    def _stop_tray(self) -> None:
        try:
            tray = getattr(self, "_tray", None)
            self._tray = None
            if tray is not None:
                tray.stop()
        except Exception:
            pass

    def _hide_to_tray(self) -> bool:
        """Withdraw the window into the resident tray icon. False when no
        tray can exist (caller quits for real instead)."""
        try:
            if not self._ensure_tray():
                # Do NOT withdraw if there's no way back — leaving the
                # window open beats trapping the user with no tray icon
                # and no visible window (previously: silent no-op).
                try:
                    from app.config_persist import log_security_event
                    log_security_event("tray", "tray icon creation failed; "
                                       "keeping window open instead of hiding")
                except Exception:
                    pass
                try:
                    _themed_showinfo(
                        self.root,
                        "System Tray Unavailable",
                        "Could not create the system-tray icon.\n\n"
                        "The window will stay open so you can still use the app.\n"
                        "If this keeps happening, try restarting Explorer or the PC.",
                        accent=COLORS["accent_yellow"],
                    )
                except Exception:
                    pass
                return False
        except Exception:
            return False
        try:
            self.root.withdraw()
        except Exception:
            return False
        # first-hide explainer (once ever): X hides, Quit quits — never
        # trap a user who expected Close to mean close.
        try:
            from app.config_persist import has_seen_tip, mark_tip_seen
            if not has_seen_tip("tray_hide_once"):
                mark_tip_seen("tray_hide_once")
                try:
                    from app.toast import show_toast as _toast
                    _toast("Cleaner Tool keeps running",
                           "Minimized to the tray — monitors and Auto-Pilot "
                           "stay on. Quit from the tray icon menu.")
                except Exception:
                    pass
        except Exception:
            pass
        return True

    def _maybe_hide_for_tray(self) -> None:
        """--tray boot: withdraw right after the main UI builds (pre-
        mainloop, so nothing flashes). The Gate path never reaches here
        hidden — it builds the UI through its own callback first."""
        try:
            import sys as _sys
            if "--tray" in _sys.argv:
                self._hide_to_tray()
        except Exception:
            pass

    def tray_open(self) -> None:
        """Tray menu / single-click / second-launch focus target."""
        try:
            self.root.deiconify()
        except Exception:
            return
        try:
            self.root.lift()
        except Exception:
            pass
        try:
            self.root.focus_force()
        except Exception:
            pass

    def tray_quit(self) -> None:
        """The one true exit while resident (goes through the busy guard)."""
        try:
            self._on_close(force_quit=True)
        except Exception:
            pass

    def _tray_game_label(self) -> str:
        """Live tray label: shows the current state so the item reads as a
        plain On/Off switch. Evaluated each time the menu opens."""
        try:
            from app.config_persist import get_game_night
            on = bool(get_game_night().get("active"))
        except Exception:
            on = False
        return "\U0001f3ae Game Mode: " + ("On" if on else "Off")

    def tray_toggle_game_session(self) -> None:
        """Tray Game Mode: one click flips it. On applies the Game Session
        FPS tweaks, Off puts back exactly what it changed. Runs quietly (a
        tray click may come from inside a game — no modal over it) and
        reports the REAL outcome in one toast when the work is done."""
        try:
            if getattr(self, "_busy", False):
                self._toast_async("Cleaner Tool is busy",
                                  "A run is in progress — open the app to watch it.")
                self.tray_open()
                return
            from app.config_persist import get_game_night
            if bool(get_game_night().get("active")):
                self.end_game_night(quiet=True)
            else:
                self.start_game_night(quiet=True)
        except Exception:
            pass

    def _toast_async(self, title, msg) -> None:
        """Fire a toast without ever blocking the caller."""
        try:
            threading.Thread(target=show_toast, args=(title, msg),
                             daemon=True).start()
        except Exception:
            pass

    @staticmethod
    def _result_toast(kind, extra, total_bytes, completed, failed,
                      skipped_n, cancelled):
        """(title, body) for a finished tray-started run. Numbers come from
        the run itself — never a canned "done"."""
        extra = extra or {}
        if cancelled:
            return ("Stopped", "The run was stopped early.")
        if kind == "clean":
            if total_bytes and total_bytes > 0:
                body = "Quick Clean finished"
                if failed:
                    body += f" ({failed} failed)"
                return (f"{format_bytes(total_bytes)} freed", body)
            if failed and not completed:
                return ("Clean failed", "Open the app to see why.")
            return ("Nothing to clean", "Your PC is already tidy.")
        if kind == "gm_on":
            if completed > 0:
                body = f"{completed} settings boosted"
                if extra.get("admin_missing"):
                    body += " · run as admin for the rest"
                return ("\U0001f3ae Game Mode on", body)
            if failed:
                return ("Game Mode failed", "Open the app to see why.")
            return ("\U0001f3ae Game Mode on", "Already set up for gaming.")
        if kind == "gm_off":
            if failed and not completed:
                return ("Game Mode off failed", "Open the app to see why.")
            return ("Game Mode off", "Normal settings are back.")
        return ("Done", "")

    def tray_quick_clean(self) -> None:
        """Tray Quick Clean: the curated preset through the standard run
        engine (scorecard waits in the app for later — never a modal over
        a game). Bounces busily with a toast + open like its sibling."""
        try:
            if getattr(self, "_busy", False):
                try:
                    from app.toast import show_toast as _toast
                    _toast("Cleaner Tool is busy",
                           "A run is in progress — open the app to watch it.")
                except Exception:
                    pass
                self.tray_open()
                return
            from app.tab_presets import PRESETS as _PR
            by_key = {t.key: t for t in TABS.get("Clean", [])}
            keys = _PR.get("Clean", {}).get("Quick Clean", [])
            tasks = [by_key[k] for k in keys if k in by_key]
            if not tasks:
                try:
                    from app.toast import show_toast as _toast
                    _toast("Quick Clean", "No clean tasks available.")
                except Exception:
                    pass
                return
            self._toast_async("Quick Clean started", "Working on it\u2026")
            # quiet: no modal scorecard over a game, and the tray run never
            # overwrites the user's saved Clean selection. The result
            # toast reports how much space was actually freed.
            self.run_tasks("Clean", tasks, mode="run", quiet=True,
                           toast_kind="clean")
        except Exception:
            pass

    def tray_check_ping(self) -> None:
        """Tray quick action: one honest ICMP ping to a fast, reliable
        anycast host (Cloudflare 1.1.1.1) — a "how's my connection right
        now" glance for before you queue up, not a per-game/region test
        (that's the full Game Ping tool on the Tools tab, which needs a
        window to show its results table). Reuses gameping.probe_icmp and
        its verdict() bands so "Excellent"/"Playable"/"Poor" mean the same
        thing here as they do there. Runs off the UI thread — a stalled
        or unreachable network must never freeze the tray or the window —
        and answers with a toast, no window required. A second click
        while one is in flight is a no-op rather than stacking probes."""
        with self._tray_action_lock:
            if getattr(self, "_tray_ping_busy", False):
                return
            self._tray_ping_busy = True
        def _work():
            try:
                from app.gameping import probe_icmp, verdict
                _sent, _recv, avg = probe_icmp("1.1.1.1", count=4)
                word, good = verdict(avg)
                if avg is None:
                    title, msg = "No reply", "Couldn't reach the internet just now."
                else:
                    title = f"Ping: {avg:.0f} ms \u2014 {word}"
                    msg = ("Good time to queue up." if good
                           else "Might be a rough match right now.")
                from app.toast import show_toast as _toast
                _toast(title, msg)
            except Exception:
                pass
            finally:
                def _clear():
                    with self._tray_action_lock:
                        self._tray_ping_busy = False
                # MED-014: this was `self.root.after(0, _clear)`. A
                # cross-thread after() is not merely slow, it can be DROPPED
                # outright, and a dropped callback does not raise - so the
                # except branch that used to follow never ran,
                # _tray_ping_busy stayed True, and "Check My Ping" was dead
                # for the rest of the session. The dispatcher is the same fix
                # the run-completion hops use: a queue drained by a Tk-thread
                # pump, so a delivery cannot be silently lost.
                self._dispatch.post(_clear)
        import threading as _th
        _th.Thread(target=_work, daemon=True, name="TrayPingCheck").start()

    def tray_speed_test(self) -> None:
        """Tray quick action: one real download + upload round through the
        same Cloudflare-edge engine (app/speed_test.py) the Tools tab's
        Speed Test card uses — not a separate/fake number. Uses that
        engine's smallest round size (the same one the full dialog opens
        with before escalating on a fast link) so a tray click answers in
        a few seconds instead of running the full multi-round climb,
        which needs the dialog's live progress bar to make sense of.
        Same off-UI-thread + toast pattern as Check My Ping: a starting
        toast first since this takes longer than a ping, then the result;
        never freezes the tray or the window either way."""
        with self._tray_action_lock:
            if getattr(self, "_tray_speedtest_busy", False):
                return
            self._tray_speedtest_busy = True
        def _start_toast():
            # own thread: show_toast blocks ~1-2s on PowerShell startup and
            # neither the UI thread nor the test itself should wait on it.
            try:
                from app.toast import show_toast as _toast
                _toast("Speed Test started",
                       "Checking your download and upload speed\u2026")
            except Exception:
                pass
        def _work():
            title, msg = "Speed Test", "Couldn't finish the test. Try again."
            try:
                from app import speed_test as _st
                down = _st.download_parallel(_st.DOWNLOAD_ROUNDS[0], streams=_st.PARALLEL_STREAMS)
                if down is None:
                    down = _st.download_parallel(_st.DOWNLOAD_ROUNDS[0],
                                                 streams=_st.PARALLEL_STREAMS,
                                                 url=_st.FALLBACK_DOWNLOAD_URL)
                up = _st.upload_parallel(_st.UPLOAD_ROUNDS[0], streams=_st.PARALLEL_STREAMS)
                if down is None and up is None:
                    msg = "Couldn't reach the test server just now."
                else:
                    # numbers go in the bold title line (same pattern as the
                    # ping toast) so the result is the first thing you read
                    title = (f"{_st.format_mbps(down)} down \u00b7 "
                             f"{_st.format_mbps(up)} up")
                    msg = "Your internet speed just now."
            except Exception:
                pass
            try:
                from app.toast import show_toast as _toast2
                _toast2(title, msg)
            except Exception:
                pass
            def _clear():
                with self._tray_action_lock:
                    self._tray_speedtest_busy = False
            # MED-014: was `self.root.after(0, _clear)` from the worker.
            # A dropped cross-thread after() does not raise, so the fallback
            # never fired and the flag stayed True - "Speed Test" dead for
            # the session. Routed through the dispatcher instead.
            self._dispatch.post(_clear)
        import threading as _th2
        _th2.Thread(target=_start_toast, daemon=True,
                    name="TraySpeedStartToast").start()
        _th2.Thread(target=_work, daemon=True, name="TraySpeedTest").start()

    # ---------------- close guard (Phase 1 M3, kept) ---------------- #

    def _on_close(self, force_quit=False):
        """Window X (or tray Quit with force_quit=True).

        The tray owns this app's lifetime: X hides to the tray (monitors
        and pilot keep running — the point of the tray), Quit from the
        tray menu really exits. A run in flight always confirms first;
        confirming from X stops the run then hides (never kills).
        Tray unavailable (no shell) -> old behavior: X quits for real.
        "Close to Tray" (Auto Maintenance dialog, default on) can turn
        this off per-user: when it's off, X quits for real too.
        """
        if self._busy:
            # BUG-020: a run can be claimed but not yet executing — the
            # pre-flight window, which spans the admin gate and up to two
            # modal prompts. There is no worker to join, so "stop the task and
            # close" is not something we could honour, and withdrawing to the
            # tray here would leave the prompt's grab_set modal on a hidden
            # root: no visible window, no reachable dialog, and the app looks
            # hung until someone opens Task Manager. Refuse and stay visible;
            # the user is already looking at a prompt that owns the answer.
            if self._run_in_preflight():
                self.log("Close declined: a confirmation prompt is still "
                         "open. Answer it first.")
                try:
                    self.set_status("Answer the confirmation prompt first — "
                                    "nothing is running yet.")
                except Exception:
                    pass
                return
            proceed = _themed_askyesno(
                self.root,
                "Task Still Running",
                "A task is still running. Closing now may leave a Windows "
                "service stopped until you restart it manually.\n\n"
                "Stop the task and close anyway?",
                accent=COLORS["accent_red"],
                yes_text="Stop & Close",
                no_text="Keep Running",
            )
            if not proceed:
                return
            self._request_cancel()
            if self._worker_thread is not None:
                self._worker_thread.join(timeout=8.0)
                if self._worker_thread.is_alive():
                    self._log_full("  ! Task did not stop in time — closing anyway.")
        _close_to_tray = True
        try:
            from app.config_persist import load_config as _load_ctr
            _close_to_tray = bool(_load_ctr().get("close_to_tray_enabled", True))
        except Exception:
            pass
        if not force_quit and _close_to_tray and self._hide_to_tray():
            return
        try:
            self._stop_tray()
        except Exception:
            pass
        self._stop_disk_monitor()
        # cancel any in-flight nudge estimate walk (phase-6: the scan
        # coordinator's cancelled() consults this token between tasks)
        try:
            self._nudge_scan_token[0] = True
        except Exception:
            pass
        # pilot watcher must die with the app — and stopping never
        # reverts (close is side-effect free by design). H15: shutdown() also
        # cancels the pump's pending after(), which _pilot_stop now does too;
        # this call is the belt-and-braces one for a torn-down root.
        try:
            self._pilot_sess.shutdown()
        except Exception:
            pass
        # stop the worker->UI bridge before the root goes away, so its pump
        # cannot reschedule onto a destroyed interpreter
        try:
            self._dispatch.stop()
        except Exception:
            pass
        self.root.destroy()

    def _build_style(self):
        style = ttk.Style()
        style.theme_use("default")
        style.configure(".", background=COLORS["bg"], foreground=COLORS["text"])
        style.configure("TProgressbar", thickness=10, troughcolor=COLORS["surface"],
                        background=COLORS["accent_green"])
        # dark combobox + listbox (Auto Maintenance dialog)
        style.configure("TCombobox",
                        fieldbackground=COLORS["surface"], background=COLORS["surface"],
                        foreground=COLORS["text"], arrowcolor=COLORS["text"],
                        selectbackground=COLORS["surface_hover"], selectforeground=COLORS["text"],
                        bordercolor=COLORS["surface"], lightcolor=COLORS["surface"],
                        darkcolor=COLORS["surface"])
        style.map("TCombobox",
                  fieldbackground=[("readonly", COLORS["surface"]), ("disabled", COLORS["surface"])],
                  background=[("readonly", COLORS["surface"])],
                  foreground=[("readonly", COLORS["text"]), ("disabled", COLORS["subtext"])])
        self.root.option_add("*TCombobox*Listbox.background", COLORS["surface"])
        self.root.option_add("*TCombobox*Listbox.foreground", COLORS["text"])
        self.root.option_add("*TCombobox*Listbox.selectBackground", COLORS["surface_hover"])
        self.root.option_add("*TCombobox*Listbox.selectForeground", COLORS["text"])
        self.root.option_add("*TCombobox*Listbox.font", (F, 9))

    # ---------------- main UI ---------------- #

    def _build_main_ui(self):
        for widget in self.root.winfo_children():
            try:
                # never destroy the chrome's own pieces (title bar, corner
                # masks, grips) — they are chrome, not rebuildable content.
                # Grips added to the skip list after the bottom-corners bug:
                # a rebuild without this killed every resize edge.
                chrome = self._chrome
                if widget in (getattr(chrome, "_titlebar", None),):
                    continue
                if widget in (getattr(chrome, "_corners", []) or []):
                    continue
                if widget in (getattr(chrome, "_grip_widgets", {}) or {}).values():
                    continue
            except Exception:
                pass
            widget.destroy()

        self._build_menu()

        # content host: everything the app packs goes in here, leaving
        # the top TITLE_H strip for the custom title bar
        host = self._content_host = tk.Frame(self.root, bg=COLORS["bg"])
        host.place(x=0, y=BorderlessChrome.TITLE_H, relwidth=1.0,
                   relheight=1.0, height=-BorderlessChrome.TITLE_H)

        # Toolbar row (user redesign 2026-09): the Quick Tools dropdown and
        # the 4 feature pills are GONE — everything they did now lives in
        # the Tools tab (embedded panels + shortcut link-rows). What
        # remains is the privilege mode on the right; the toolbar is a
        # thin, quiet strip.
        toolbar = tk.Frame(host, bg=COLORS["bg"])
        toolbar.pack(fill="x", padx=26, pady=(8, 0))
        
        # Update notification banner (hidden by default, shown when update available)
        self._update_banner_frame = tk.Frame(toolbar, bg=COLORS["accent_yellow"])
        self._update_banner_lbl = tk.Label(
            self._update_banner_frame, 
            text="🔄 Update available", 
            font=(F, 9, "bold"),
            bg=COLORS["accent_yellow"], 
            fg=COLORS["black"],
            cursor="hand2"
        )
        self._update_banner_lbl.pack(side="left", padx=10, pady=4)
        self._update_banner_lbl.bind("<Button-1>", lambda e: self._show_update_dialog())
        self._update_banner_frame.pack(side="left", padx=(0, 8))
        self._update_banner_frame.pack_forget()  # Hidden by default
        
        # Audit fix (Limited-mode clarity): this used to be a plain, inert
        # label — the user had no way to get to Administrator rights again
        # short of quitting and relaunching by hand. Clicking it in Limited
        # mode now offers to restart elevated, same UAC flow AdminGateFrame
        # already uses, just reachable after that first screen is gone.
        self._mode_lbl = tk.Label(toolbar, text="", font=(F, 9),
                                  bg=COLORS["bg"], fg=COLORS["subtext"])
        self._mode_lbl.pack(side="right", anchor="se")
        self._refresh_mode_label()

        # Per-tab icon, centered above the pill switcher (user request).
        # The image swaps on every tab switch; PhotoImages are kept on self
        # so Tk never garbage-collects the currently shown one.
        #
        # ICON SIZE CONTRACT (switcher-jump fix 2026-09): every icon must
        # DISPLAY at the same size or the icon label changes height per
        # tab and shoves the whole column (switcher included) up/down —
        # the "few px jump" bug report. Assets are normalized to 72x72 on
        # disk, and this loader subsamples any stray oversized file to
        # <=72px with a computed factor (not a magic /4) so one odd PNG
        # can never change the label height again.
        self._tab_icons: dict = {}
        for _tab, _fname in (("Clean", "clean.png"), ("Repair", "repair.png"),
                             ("Tweak", "tweak.png"), ("Install", "install.png"),
                             ("Tools", "tools.png")):
            for _p in self._asset_candidates(_fname):
                if not _p.is_file():
                    continue
                try:
                    _img = tk.PhotoImage(file=str(_p))
                    w, h = _img.width(), _img.height()
                    if w > 72 or h > 72:
                        _f = 2
                        while w // _f > 72 or h // _f > 72:
                            _f *= 2
                        _img = _img.subsample(_f, _f)
                    self._tab_icons[_tab] = _img
                    break
                except Exception:
                    continue
        self._tab_icon_lbl = tk.Label(host, bg=COLORS["bg"])
        self._tab_icon_lbl.pack(pady=(6, 8))

        # Pill switcher (Phase 2 #12: 5 tabs -> one sliding 3-way pill).
        # active_tab must exist before _build_tab_switch draws the thumb.
        self.active_tab = "Clean"
        self._update_tab_icon()
        self._build_tab_switch()

        # Tab pages (Install gets the catalog browser, Tools the embedded
        # power features, others the preset UI)
        self.page_container = tk.Frame(host, bg=COLORS["bg"])
        self.page_container.pack(fill="both", expand=True, padx=4, pady=(0, 4))
        self.tabs = {}
        for name in TAB_NAMES:
            if name == "Install":
                self.tabs[name] = InstallTab(self.page_container, self)
            elif name == "Tools":
                self.tabs[name] = ToolsTab(self.page_container, self)
            else:
                self.tabs[name] = TaskTab(self.page_container, self, name)
        self._show_tab("Clean")

        # Bottom: status line + details disclosure + progress + disk
        bottom = tk.Frame(host, bg=COLORS["bg"])
        bottom.pack(fill="x", padx=26, pady=(2, 10))

        stat_row = tk.Frame(bottom, bg=COLORS["bg"])
        stat_row.pack(fill="x")
        self.status_lbl = tk.Label(stat_row, text="Ready.", font=(F, 10, "bold"),
                                   bg=COLORS["bg"], fg=COLORS["subtext"], anchor="w")
        self.status_lbl.pack(side="left")
        self._status_fade_ids = []
        # drive chips (user request): one clickable chip per local drive /
        # USB stick, color-coded by fill level; click opens it in Explorer.
        # Up to MAX_INLINE_CHIPS inline; the rest live behind a "+N ▾" menu.
        self._drive_chips_frame = tk.Frame(stat_row, bg=COLORS["bg"])
        self._drive_chips_frame.pack(side="right")
        self._drive_chips: list = []      # [(label_widget, drive_root)]
        self._drive_extra: list = []      # drives behind the +N menu
        self._drive_last_key = None       # rebuild only when the set changes
        # low-space nudge chips (user-approved feature 4, 2026-09): one
        # amber chip per nearly-full drive, packed LEFT of the drive
        # chips (later side="right" packs land further left). Created and
        # destroyed on red transitions — never a permanent fixture.
        self._nudge_frame = tk.Frame(stat_row, bg=COLORS["bg"])
        self._nudge_frame.pack(side="right", padx=(8, 0))
        self._nudge_chips: dict = {}      # letter -> {"frame", "label", "tip"}
        self._drive_was_red: dict = {}    # letter -> bool (hysteresis state)
        self._nudge_dismissed: set = set()  # session dismissals (respected)
        self._nudge_estimate = None       # (bytes, epoch) or None
        self._nudge_scan_running = False
        self._nudge_scan_token = [False]  # cancel token (app close / phase-6)
        self._last_drives: list = []      # raw monitor list (tooltip totals)

        # progress overlay (segmented animated bar) — hidden until a run
        # starts (gamers see a clean window, not an empty progress track)
        self.progress_bar = AnimatedProgressBar(bottom, accent=COLORS["accent_green"])
        self._progress_hidden = True

        # Stop button — slots into the status row while a run is active
        self.cancel_btn = AnimatedButton(
            stat_row, text="✕ Stop", command=self._request_cancel,
            bg=COLORS["surface"], fg=COLORS["accent_red"], font=(F, 9, "bold"),
            padx=14, pady=6,
        )
        self.cancel_btn.set_enabled(False)

        # Bottom-corner shortcuts: Auto Maintenance, Quick Tools, Export
        # Logs. Startup Manager and Uninstall Programs moved to Tools tab
        # cards (user request, 2026-09) — the Tools grid had exactly two
        # empty slots on its last row, and both dialogs already existed
        # as ThemedModal popups launched the same way every other Tools
        # card is, so this is a straight relocation, not new plumbing.
        # Three equal-width columns, each icon centered — gaps stay identical.
        corners = tk.Frame(host, bg=COLORS["bg"])
        corners.pack(fill="x", padx=26, pady=(0, 8))
        for _ci in range(3):
            corners.columnconfigure(_ci, weight=1, uniform="corner")

        def _corner_icon(col, icon, tip, command):
            lbl = tk.Label(corners, text=icon, font=(F, 13),
                           bg=COLORS["bg"], fg=COLORS["subtext"],
                           cursor="hand2", bd=0, highlightthickness=0)
            lbl.grid(row=0, column=col)
            Tooltip(lbl, tip)
            lbl.bind("<Button-1>", lambda e: command(), add="+")
            lbl.bind("<Enter>", lambda e: lbl.config(fg=COLORS["text"]), add="+")
            lbl.bind("<Leave>", lambda e: lbl.config(fg=COLORS["subtext"]), add="+")
            # Headless-harness hook: event_generate("<Button-1>") is never
            # delivered to an unmapped widget, so the smoke test invokes
            # this exact callable instead of depending on Tk event delivery.
            try:
                lbl._corner_cmd = command
            except Exception:
                pass
            return lbl

        self._corner_maint = _corner_icon(
            0, "🛠️", "Auto Maintenance", lambda: self._show_schedule_dialog())
        self._corner_quick = _corner_icon(
            1, "🧰", "Quick Tools", lambda: self._open_quick_tools())
        self._corner_logs = _corner_icon(
            2, "📋", "Export Logs", lambda: self.export_logs())

        self._build_log(bottom)
        self._start_disk_monitor()

        # REL-002: surface a corrupt-config reset here instead of leaving
        # it stderr-only (invisible in the windowed frozen exe/pythonw —
        # the user would otherwise just see their tweak badges and
        # selections silently reset with zero explanation).
        try:
            from app.config_persist import consume_quarantine_notice
            _notice = consume_quarantine_notice()
            if _notice:
                self.log(f"  ! {_notice}")
        except Exception:
            pass

        # improvement-report 2.2: keyboard shortcuts. Bound on root (not
        # add="+") so a rebuild replaces rather than stacks bindings.
        self._bind_global_shortcuts()

        # Chrome restack (bottom-corners bug fix): everything above was
        # just (re)built — new siblings stacked above the chrome pieces
        # (title bar / grips / corner masks), burying the BOTTOM corner
        # masks under the content host. Re-assert the chrome z-order.
        try:
            self._chrome.restack_chrome()
        except Exception:
            pass

        self.set_status("Ready. Pick a preset and press Run.")

        # Check for updates in the background (non-blocking)
        self._schedule_update_check()

        # F6: the window is up — pre-warm the first-entry Custom/Undo
        # bodies and the Install catalog in idle time instead of on the
        # user's first click / tab switch.
        self._schedule_idle_prewarm()

        # Auto-Pilot resume (feature 6): the watcher only ever runs while
        # the GUI runs, so "enabled" just means resume watching now
        try:
            from app.config_persist import load_config as _load
            if _load().get("session_pilot_enabled", False):
                self._pilot_start()
        except Exception:
            pass

        # --tray boot hides here (pre-mainloop, nothing flashes). The Gate
        # path converges here too, through its own continue callback.
        try:
            self._tray = None
        except Exception:
            pass
        self._maybe_hide_for_tray()

    # ---------------- Update checker ---------------- #

    def _schedule_update_check(self):
        """Schedule a background check for app updates."""
        # Check after 2 seconds to let the app start up smoothly
        self.root.after(2000, self._check_for_updates)
    
    def _check_for_updates(self):
        """Background check for updates using a worker thread."""
        def _worker():
            try:
                from app.update_checker import check_for_updates
                result = check_for_updates(UPDATE_REPO, timeout=10)
                if result.get('update_available'):
                    # Show update banner on main thread (guard destroyed root)
                    try:
                        if self.root.winfo_exists():
                            self._dispatch.post(lambda: self._show_update_banner(result))
                    except Exception:
                        pass
            except Exception:
                # Silently fail - update check is optional
                pass
        
        # Run in background thread to avoid blocking UI
        import threading
        thread = threading.Thread(target=_worker, daemon=True)
        thread.start()
    
    def _show_update_banner(self, update_info: dict):
        """Show the update notification banner."""
        try:
            if hasattr(self, '_update_banner_frame') and self._update_banner_frame.winfo_exists():
                self._update_banner_frame.pack(side="left", padx=(0, 8))
                self._update_info = update_info
        except Exception:
            pass
    
    def _show_update_dialog(self):
        """Show the update dialog with download link."""
        try:
            update_info = getattr(self, '_update_info', None)
            if not update_info:
                return
            
            latest_ver = update_info.get('latest_version', 'unknown')
            current_ver = update_info.get('current_version', 'unknown')
            download_url = update_info.get('download_url', '')
            notes = update_info.get('release_notes', '')
            
            message = f"A new version of Cleaner Tool is available!\n\n"
            message += f"Current version: {current_ver}\n"
            message += f"Latest version: {latest_ver}\n\n"
            
            if notes:
                # Truncate very long release notes
                if len(notes) > 500:
                    notes = notes[:500] + "..."
                message += f"What's new:\n{notes}\n\n"
            
            message += "Click OK to open the download page in your browser."

            if messagebox.askyesno("Update Available", message):
                if download_url:
                    # MED-012: re-validate at the SINK, not just where the URL
                    # was parsed. `_update_info` is an attribute that anything
                    # could have written, so the open call must not be the only
                    # thing standing between a server-controlled string and the
                    # user's browser. check_for_updates already filters, but a
                    # defence that lives in one place is a single point of
                    # failure - this is the one that actually opens a browser.
                    from app.update_checker import _trusted_release_url
                    safe = _trusted_release_url(download_url, UPDATE_REPO)
                    if not safe:
                        self.set_status(
                            "Refusing to open the update link: it is not an "
                            "https GitHub address for this project.")
                        return
                    import webbrowser
                    webbrowser.open(safe)
        except Exception:
            pass

    # ---------------- F6: idle pre-warm chain ---------------- #

    _PREWARM_START_MS = 900    # let the window settle + first paint finish
    _PREWARM_STEP_GAP_MS = 250 # breathing room between monolithic builds

    def _schedule_idle_prewarm(self):
        """F6(b)+F6(a): after startup, one idle chain (a) pre-warms the
        four first-entry bodies — Clean/Repair/Tweak Custom + Tweak Undo —
        into the per-tab body caches and (b) hands off to the Install tab's
        chunked catalog builder.

        Each monolithic grid build is its own step and steps are spread
        ~250 ms apart, so a step never lands on top of user interaction
        that arrived during the previous one. The grid steps run FIRST on
        purpose: they pay the one-off per-process Tk costs (font loads,
        notably the ~200 ms first Segoe UI Emoji font) so every Install
        catalog slice that follows stays well under the ~30 ms budget.
        Guards: busy runs postpone the chain; a failing step is dropped;
        bodies the user already built are cache hits and skip instantly."""
        if getattr(self, "_idle_chain_scheduled", False):
            return
        self._idle_chain_scheduled = True
        steps = []
        for tab_name, mode in (("Clean", "custom"), ("Repair", "custom"),
                               ("Tweak", "custom"), ("Tweak", "undo")):
            page = self.tabs[tab_name]
            steps.append(lambda p=page, m=mode: p._prewarm_body(m))
        steps.append(lambda: self.tabs["Install"]._start_catalog_build())
        # PERF (startup audit 2026-09): the Install page's FIRST visit
        # still costs ~900 ms because place()ing ~950 widgets maps the
        # whole subtree on the click. Pre-map the page NOW, underneath
        # the others so the first user click is only a tkraise of an
        # already-laid-out page (~50 ms, like warm visits).
        # Flicker fix (user bug report: main window flashed the Install
        # tab while an Auto-Pilot dialog was open): the old sequence
        # place()d the page, RAISED it to the top of the stack (mapping
        # it visibly for the rest of the frame), then re-buried it — and
        # a postponed step could run that raise between the user's popup
        # and the main window. The raise was only ever there to make
        # update_idletasks lay the page out. lower() achieves the same
        # layout pass WITHOUT the page ever stacking above the visible
        # tab: geometry is computed for withdrawn-lowered windows too,
        # and _show_tab's tkraise does the one-and-only visible raise on
        # the user's first real visit.
        def _premap_install():
            try:
                page = self.tabs["Install"]
                if page.winfo_manager() != "place":
                    page.place(relx=0, rely=0, relwidth=1, relheight=1)
                    page.lower()          # lay out UNDERNEATH — never raised
                    self.root.update_idletasks()
                    # (no re-bury loop needed anymore — lower() never
                    # stacked the page above anything in the first place)
            except Exception:
                pass
        steps.append(_premap_install)
        self._idle_steps = steps

        def _step():
            if not getattr(self, "_idle_chain_scheduled", False):
                return
            if self._prewarm_should_wait():
                # a run is active — or a popup is open — postpone rather
                # than map/raise/layout pages behind the user's context
                # (user bug report: background flicker behind first-open
                # Auto-Pilot). The chain resumes on its own afterwards.
                try:
                    self.root.after(500, _step)
                except Exception:
                    pass
                return
            if self._idle_steps:
                step = self._idle_steps.pop(0)
                try:
                    step()
                except Exception:
                    pass  # a failed pre-warm must never crash the app
            if self._idle_steps:
                try:
                    self.root.after(self._PREWARM_STEP_GAP_MS, _step)
                except Exception:
                    pass

        try:
            self.root.after(self._PREWARM_START_MS, _step)
        except Exception:
            pass

    # Asset resolution delegates to utils.resolve_asset_path rather than
    # `Path(__file__).with_name("assets")`. That expression is only correct
    # while the caller sits directly in app/; from app/ui/... it points at a
    # directory that does not exist, so the icon silently fails to load. The
    # helper anchors on the app package and also probes the frozen _MEIPASS
    # layouts, so it is correct at any depth.
    def _asset_candidates(self, fname):
        """Existing callers iterate the result and skip anything missing, so
        this keeps the list contract. The list now has at most one entry: the
        single path utils.resolve_asset_path found, or empty. It used to build
        candidates from `__file__`, which pointed at app/ui/assets/ once this
        class left app/gui.py (H14) and silently loaded no icons at all.
        GamepadDialog._load_pad_artwork also depends on this method."""
        found = resolve_asset_path(fname)
        return [pathlib.Path(found)] if found else []

    # ---------------- pill switcher ---------------- #

    # User-approved faster switch (2026-09): 240 -> 160 ms. Still the same
    # time-based ease-out at ~60 fps ticks; the slide just completes sooner.
    SWITCH_ANIM_MS = 160

    def _build_tab_switch(self):
        """One pill, three labels, one thumb that slides + recolors (#12,
        user idea: 'combine the 3 tab buttons into one animated switch').

        Flicker fix (user feedback): all canvas items are created ONCE and
        the animation only moves/recolors them (coords/itemconfigure). The
        old version did delete("all") + full recreate every frame, which
        leaves a blank-frame window between delete and recreate and
        re-rasterizes the text glyphs each frame — that was the visible
        flicker/micro-stutter. Persistent items let Tk repaint only the
        small damaged region. Motion is also time-based (eased), so a late
        frame never changes the animation's speed — only its smoothness."""
        # Phase-2 fix: the switcher must live in the content host, below
        # the custom title bar — packing to self.root slid it under the
        # toolbar clip (user bug report: switcher clipped at the top)
        switch_wrap = tk.Frame(self._content_host, bg=COLORS["bg"])
        switch_wrap.pack(pady=(10, 10))
        # width scales with the number of tabs (4 tabs now: Install added)
        n_tabs = len(TAB_NAMES)
        self._switch_labels = TAB_NAMES
        self._switch_w = max(444, 148 * n_tabs)
        self.switch = tk.Canvas(switch_wrap, width=self._switch_w, height=44,
                                highlightthickness=0, bd=0, bg=COLORS["bg"])
        self.switch.pack()
        self._seg = self._switch_w / n_tabs
        self._switch_pos = float(TAB_NAMES.index(self.active_tab))
        self._switch_anim_after = None
        self.switch.bind("<Button-1>", self._on_switch_click)
        self.switch.bind("<Motion>", self._on_switch_motion)
        self.switch.bind("<Leave>", lambda e: self.switch.config(cursor="arrow"))
        # keyboard accessibility (audit a11y): the pill accepts Tab focus;
        # Left/Right arrows move between tabs. Focus is indicated by the
        # thumb's own canvas — no native highlightthickness rectangle
        # (user feedback: stray outline).
        self.switch.configure(takefocus=1, highlightthickness=0)
        self.switch.bind("<Left>", lambda e: self._switch_to(
            TAB_NAMES[max(0, TAB_NAMES.index(self.active_tab) - 1)]))
        self.switch.bind("<Right>", lambda e: self._switch_to(
            TAB_NAMES[min(len(TAB_NAMES) - 1, TAB_NAMES.index(self.active_tab) + 1)]))
        self.switch.bind("<Return>", lambda e: None)  # arrows are the activation
        self.switch.bind("<space>", lambda e: None)
        # persistent items — created once, never deleted
        self._switch_track = self._canvas_round_rect(
            self.switch, 0, 0, self._switch_w, 44, 22,
            fill=COLORS["bg_alt"], outline="")
        thumb_w = self._seg - 8
        x = self._thumb_x(self._switch_pos)
        self._switch_thumb = self._canvas_round_rect(
            self.switch, x, 4, x + thumb_w, 40, 18,
            fill=TAB_ACCENTS[self.active_tab], outline="")
        self._switch_texts = []
        for i, name in enumerate(self._switch_labels):
            cx = self._seg * i + self._seg / 2
            self._switch_texts.append(self.switch.create_text(cx, 22, text=name))
        self._style_switch_labels()

    def _thumb_x(self, t):
        thumb_w = self._seg - 8
        return 4 + t * self._seg + (self._seg - thumb_w - 8) / 2

    def _place_switch_thumb(self, t, fill):
        thumb_w = self._seg - 8
        x = self._thumb_x(t)
        points = self._round_points(x, 4, x + thumb_w, 40, 18)
        self.switch.coords(self._switch_thumb, *points)
        self.switch.itemconfigure(self._switch_thumb, fill=fill)

    def _style_switch_labels(self):
        # Same-size labels for every state (user bug report: the active
        # label's 10->11-bold growth read as the pill "jumping"). The
        # active tab is marked by the thumb + black text alone — a
        # color-only switch cannot shift pixels.
        for i, tid in enumerate(self._switch_texts):
            active = self._switch_labels[i] == self.active_tab
            self.switch.itemconfigure(
                tid,
                fill=COLORS["black"] if active else COLORS["subtext"],
                font=(F, 10, "bold") if active else (F, 10),
            )

    def _on_switch_click(self, event):
        seg = min(len(TAB_NAMES) - 1, max(0, int(event.x // self._seg)))
        self._switch_to(TAB_NAMES[seg])

    def _on_switch_motion(self, event):
        seg = min(len(TAB_NAMES) - 1, max(0, int(event.x // self._seg)))
        self.switch.config(cursor="hand2" if seg != TAB_NAMES.index(self.active_tab) else "arrow")

    def _switch_to(self, name):
        if self._busy or name == self.active_tab:
            # audit fix (P2-08): silently swallowing clicks while busy reads
            # as "app is frozen". Show the busy state instead.
            if self._busy:
                self.set_status("Busy — wait for the current task to finish (or press ✕ Stop).")
            return
        from_accent = TAB_ACCENTS[self.active_tab]
        self.active_tab = name
        self._update_tab_icon()
        # cancel any in-flight animation so a fast re-click retargets cleanly
        if self._switch_anim_after is not None:
            try:
                self.root.after_cancel(self._switch_anim_after)
            except Exception:
                pass
        self._switch_anim = {
            "start_t": self._switch_pos,
            "target_t": float(TAB_NAMES.index(name)),
            "start_ms": time.monotonic(),
            "from_accent": from_accent,
            "to_accent": TAB_ACCENTS[name],
        }
        self._style_switch_labels()
        # F5 (perf): swap the content page and flush its geometry BEFORE
        # the slide starts. The old order ticked the thumb first and laid
        # the new page out inside the 240 ms animation window — on heavy
        # pages (Install ~950 widgets) the synchronous layout/paint stalled
        # the slide, which then jumped to the end. update_idletasks() does
        # the page's geometry pass once so the animation's first frame runs
        # on an already-settled page.
        #
        # BENCHMARK A/B (2026-09): moving that flush out of the click
        # handler (letting idle-time layout land inside the animation
        # window) was measured as a net LOSS: click sync dropped ~10 ms,
        # but total click-to-settled grew ~12 ms and one update round
        # inside the slide absorbed the whole layout (~10 ms on the Tweak
        # grids, maxR 0.8 -> 12 ms) — a dropped frame right where the
        # slide should look smoothest. The flush stays here.
        #
        # Under the stacked-pages PROTO (see _show_tab) the warm-switch
        # flush is near-free (the raised page was already laid out, so
        # update_idletasks has ~nothing to do) but still required on a
        # page's FIRST visit, where place() maps the whole subtree: that
        # layout must settle before the slide's first frame, same as it
        # did under pack. Re-checked by measurement (2026-09 bench, see
        # the session report): warm flush cost stayed negligible, so the
        # flush is kept under the PROTO as well.
        self._show_tab(name)
        self.root.update_idletasks()
        self._tick_switch()

    def _tick_switch(self):
        anim = getattr(self, "_switch_anim", None)
        if anim is None:
            return
        # monotonic() is in SECONDS — convert to ms before dividing by the
        # ms-named duration (units bug made the slide take 240 *seconds*).
        p = (time.monotonic() - anim["start_ms"]) * 1000.0 / self.SWITCH_ANIM_MS
        if p >= 1.0:
            self._switch_pos = anim["target_t"]
            self._place_switch_thumb(self._switch_pos, anim["to_accent"])
            self._switch_anim = None
            self._switch_anim_after = None
            return
        # ease-out cubic: fast start, gentle landing — no visible snap
        eased = 1 - (1 - p) ** 3
        t = anim["start_t"] + (anim["target_t"] - anim["start_t"]) * eased
        self._switch_pos = t
        self._place_switch_thumb(t, _hex_lerp(anim["from_accent"], anim["to_accent"], eased))
        # ~60 fps ticks (F5): Tk cannot present faster than the display
        # refresh, and 100 wakeups/s during a 240 ms slide buys nothing —
        # each tick just re-times the eased position, so the pacing is
        # unchanged (time-based math above), only the wakeup rate drops.
        self._switch_anim_after = self.root.after(16, self._tick_switch)

    def _canvas_round_rect(self, c, x0, y0, x1, y1, r, **kw):
        return c.create_polygon(self._round_points(x0, y0, x1, y1, r), smooth=True, **kw)

    @staticmethod
    def _round_points(x0, y0, x1, y1, r):
        return [
            x0 + r, y0, x1 - r, y0, x1, y0, x1, y0 + r,
            x1, y1 - r, x1, y1, x1 - r, y1, x0 + r, y1,
            x0, y1, x0, y1 - r, x0, y0 + r, x0, y0,
        ]

    def _update_tab_icon(self):
        """Swap the icon above the pill to the active tab's asset image."""
        try:
            lbl = getattr(self, "_tab_icon_lbl", None)
            if lbl is None or not lbl.winfo_exists():
                return
            img = getattr(self, "_tab_icons", {}).get(getattr(self, "active_tab", ""))
            if img is not None:
                lbl.config(image=img)
            else:
                lbl.config(image="")
        except Exception:
            pass

    def _show_tab(self, name):
        # -------------------------------------------------------------
        # PROTO (user-approved experiment, 2026-09): always-mapped STACKED
        # tab pages. All pages live in page_container (the "stage" — it is
        # the shared container the pages were already direct children of;
        # nothing else is packed into it). A page is place()d once into the
        # stage on its FIRST show (relx/rely/relwidth/relheight=1 fills the
        # stage exactly, so a page's geometry equals the old pack result),
        # then switching only tkraise()s the target above its siblings —
        # no pack_forget/pack anywhere in the switch path, so switching
        # never unmaps a page and never triggers a full re-layout/re-raster
        # of the shown page ("I can watch the content render" stutter).
        # Every page root frame is opaque (bg=COLORS["bg"]), so a lower
        # page can never show through. Pages are stacked ONLY after their
        # first visit: an unvisited page is not managed at all (same as the
        # old pack_forget state — zero startup cost, and the Install page's
        # idle catalog slices build into an unmanaged frame exactly as
        # before). Rollback: restore the backup at
        #   ~/.openclaw-autoclaw/agents/auto-coder/workspace/.openclaw/tmp/
        #   gui.py.bak-20260905-054920  (sha256 4289ED3C...2132D8)
        # and revert the _switch_to flush note if kept.
        # -------------------------------------------------------------
        # F6(a)+progressive show (2026-09): the Install catalog builds in
        # idle-time chunks after startup. Reaching Install mid-build used to
        # force-drain the remaining queue synchronously HERE (measured
        # 700-900 ms cold on the click). Now the queue is NOT drained: we
        # only make sure the slice chain is running (or restart it if a
        # cancelled chain left the queue non-empty — the page must never
        # sit on its placeholder forever), then show the page as-is with
        # the "Preparing catalog…" placeholder; the queued slices keep
        # building in idle and _catalog_finish drops the placeholder and
        # applies pending badge/search/count state when they drain.
        if name == "Install":
            _start = getattr(self.tabs[name], "_start_catalog_build", None)
            if _start is not None:
                try:
                    _start()   # guarded: no-op when ready / already in flight
                except Exception:
                    pass
        target = self.tabs[name]
        # TIMING (diagnostic): what the switch itself costs, and whether this
        # was a FIRST map of the page (large tree) or a plain re-raise.
        _t_show = time.perf_counter()
        _first_map = target.winfo_manager() != "place"
        if _first_map:
            target.place(relx=0, rely=0, relwidth=1, relheight=1)
        target.tkraise()
        try:
            self.update_idletasks()
        except Exception:
            pass
        if name == "Install":
            try:
                self.log("Install show: %.0f ms (%s), catalog_ready=%s"
                         % ((time.perf_counter() - _t_show) * 1000.0,
                            "FIRST map" if _first_map else "re-raise",
                            getattr(target, "_catalog_ready", None)))
            except Exception:
                pass
        # Tweak context strip follows tab shows (the registry may have
        # changed while another tab owned the screen)
        if name == "Tweak":
            try:
                target._refresh_tweak_pills()
            except Exception:
                pass
        # TEXT-002: reset the status bar to the incoming tab's own copy.
        # It used to hold one static string set once at startup, so Install
        # and Tools (no preset cards, no Run button) told the user to pick a
        # preset and press Run -- an action that does not exist on screen.
        # active_tab is already updated by _switch_to before this runs, so
        # set_status picks up the correct per-tab accent colour.
        #
        # Never clobber a live run: _switch_to already refuses to switch
        # while _busy, but the initial show and any future caller may not.
        if not getattr(self, "_busy", False):
            try:
                self.set_status(_TAB_STATUS.get(name, ""))
            except Exception:
                pass
        # progress bar may not exist yet during initial build
        if hasattr(self, "progress_bar"):
            accent = TAB_ACCENTS[name]
            self.progress_bar._accent = accent
            self.progress_bar._draw()
        # one global wheel router: scroll whichever rounded panel on the
        # visible page contains the pointer (per-page bind_all would let the
        # LAST-bound page eat every wheel event for the whole app)
        if not getattr(self, "_wheel_bound", False):
            self._wheel_bound = True
            self.root.bind_all("<MouseWheel>", self._on_wheel_routed)

    def _on_wheel_routed(self, e):
        page = self.tabs.get(self.active_tab)
        if page is None or not page.winfo_ismapped():
            return
        target = None
        w = self.root.winfo_containing(e.x_root, e.y_root)
        node = w
        while node is not None:
            if isinstance(node, ScrollableRoundedPanel):
                target = node
                break
            node = node.master
        if target is not None:
            target.on_wheel(e.delta)

    # ---------------- menu ---------------- #

    def _build_menu(self):
        # (user redesign 2026-09): the Quick Tools dropdown is GONE — its
        # items now live in the Tools tab as link-rows. This method keeps
        # ONLY the no-native-menubar guard: a native menu renders as a
        # white strip Tk cannot theme, so one must never be attached.
        self._tools_menu = None
        try:
            self.root.config(menu="")
        except Exception:
            pass

    def _open_storage_insight(self):
        """Storage Insight entry (Tools tab card + drive-chip right-click +
        low-space nudge chip + Health fix-action). Opens the X-button
        popup (user call: in-tab panels have no room at 1040x800 — the
        800x600 modal is the right surface). No-op while a run owns the
        progress UI."""
        try:
            with self._busy_lock:
                busy = self._busy
        except Exception:
            busy = False
        if busy:
            try:
                self.set_status("Busy — wait for the current task to finish (or press ✕ Stop).")
            except Exception:
                pass
            return
        try:
            StorageInsightDialog(self.root, self).wait()
        except Exception:
            pass

    def _open_dns_tester(self):
        """gaming_dns row button entry (delegates here). Opens the DNS
        popup. No busy-guard: opening never touches the run engine —
        only Apply does, and run_tasks guards busy itself."""
        try:
            DnsTesterDialog(self.root, self).wait()
        except Exception:
            pass

    def _open_process_manager(self):
        """Process Manager (Tools tab card). Live top processes by CPU/RAM
        with one-click Close. No busy-guard needed — sampling is lightweight
        and closing is an explicit user action."""
        try:
            ProcessManagerDialog(self.root, self).wait()
        except Exception:
            pass

    # ---------------- Game Session Auto-Pilot (feature 6) ---------------- #
    # The watcher thread lives only while the GUI runs (started here on
    # boot when enabled, stopped on close/toggle-off). Detection and
    # state live in app/session_pilot.py (headless-tested); THESE methods
    # are the GUI half: resolve preset tasks, call run_tasks, toast, and
    # repaint the dialog's live line when it's open. run_tasks itself is
    # async + thread-safe by design (worker + Tk hops), so the watcher
    # thread only ever schedules after() hops — never touches widgets.

    def _open_pilot_dialog(self):
        """Pilot entry (Tools tab card). Opens the setup popup (setup
        only, no busy-guard needed)."""
        try:
            PilotDialog(self.root, self).wait()
        except Exception:
            pass

    def _open_speed_test(self):
        """Speed Test entry (Tools tab card). Opens the speed popup
        (read-only measurement, no busy-guard needed — same rationale
        as the DNS tester)."""
        try:
            SpeedTestDialog(self.root, self).wait()
        except Exception:
            pass

    def _open_hw_monitor(self):
        """Hardware Monitor entry (Tools tab card). Read-only live
        stats, no busy-guard needed — same rationale as Speed Test/DNS."""
        try:
            HardwareMonitorDialog(self.root, self).wait()
        except Exception:
            pass

    def _refresh_mode_label(self):
        """Repaint the Administrator/Limited badge, and (re)bind the
        click-to-unlock handler only when it's actually clickable."""
        try:
            self._mode_lbl.unbind("<Button-1>")
            self._mode_lbl.unbind("<Enter>")
            self._mode_lbl.unbind("<Leave>")
        except Exception:
            pass
        if is_admin():
            self._mode_lbl.config(text="Administrator", cursor="")
            return
        self._mode_lbl.config(text="Limited — cleaning only  🔓",
                              cursor="hand2")
        try:
            Tooltip(self._mode_lbl, "Click to unlock full features "
                                    "(restart as Administrator)")
        except Exception:
            pass
        self._mode_lbl.bind("<Button-1>",
                            lambda e: self._offer_restart_elevated(), add="+")
        self._mode_lbl.bind("<Enter>",
                            lambda e: self._mode_lbl.config(fg=COLORS["text"]), add="+")
        self._mode_lbl.bind("<Leave>",
                            lambda e: self._mode_lbl.config(fg=COLORS["subtext"]), add="+")

    def _offer_restart_elevated(self):
        """Limited-mode badge click: same UAC relaunch AdminGateFrame uses,
        just reachable after that first screen is already gone. Confirms
        first — this restarts the whole app, so an accidental click must
        not surprise anyone mid-task."""
        if not _themed_askyesno(
                self.root, "Restart as Administrator?",
                "This closes Cleaner Tool and reopens it with "
                "Administrator rights (a UAC prompt will appear).\n\n"
                "Repairs and tweaks that are greyed out or skipped in "
                "Limited mode will then work.",
                accent=COLORS["accent_yellow"]):
            return
        if not relaunch_as_admin():
            messagebox.showerror(
                "Elevation Failed",
                "Could not request administrator rights. "
                "You can keep using Limited mode.")
            return

        def _wait_thread():
            from app.elevation import wait_for_elevated_process
            success = wait_for_elevated_process(timeout=60.0)

            def _on_done():
                if success:
                    try:
                        self.root.destroy()
                    except Exception:
                        pass
                    sys.exit(0)
                else:
                    try:
                        messagebox.showwarning(
                            "Elevation Cancelled",
                            "Administrator elevation was cancelled or timed out.\n\n"
                            "If an elevated window opened, use that one and close this.")
                    except Exception:
                        pass
            # MED-014: was `self.root.after(0, _on_done)` from the waiting
            # thread. If that after() is dropped the elevation handshake never
            # completes and you are left with TWO main windows - the exact bug
            # the comment above this block claims to have fixed.
            self._dispatch.post(_on_done)
        threading.Thread(target=_wait_thread, daemon=True).start()

    def _open_quick_tools(self):
        """Quick Tools entry (Tools tab card). Opens the shortcuts
        popup (read-only launchers, no busy-guard needed)."""
        try:
            tools_tab = self.tabs.get("Tools")
            open_target = getattr(tools_tab, "_open_target", None)
            if open_target is None:
                return
            QuickToolsDialog(self.root, self, open_target).wait()
        except Exception:
            pass

    def _open_gamepad_tester(self):
        """Gamepad Tester entry (Tools tab card). Opens the tester
        popup (read-only XInput polls, no busy-guard needed — same
        rationale as the speed popup)."""
        try:
            GamepadDialog(self.root, self).wait()
        except Exception:
            pass

    def _open_mic_check(self):
        """Mic Check entry (Tools tab card). Opens the check
        popup (read-only device/consent reads + meter, no busy-guard
        needed — same rationale as the speed popup)."""
        try:
            MicCheckDialog(self.root, self).wait()
        except Exception:
            pass

    def _open_game_server_ping(self):
        """Game Server Ping entry (Tools tab card). Opens the ping
        popup (read-only measurement, no busy-guard needed — same
        rationale as the speed popup)."""
        try:
            GameServerPingDialog(self.root, self).wait()
        except Exception:
            pass

    def _open_keyboard_tester(self):
        """Keyboard Tester entry (Tools tab card). Opens the tester
        popup (pure Tk key events, no busy-guard needed)."""
        try:
            KeyboardTesterDialog(self.root, self).wait()
        except Exception:
            pass

    def _open_monitor_test(self):
        """Monitor Test entry (Tools tab card). Opens the Info + Dead
        Pixels popup (read-only query + solid fills, no busy-guard)."""
        try:
            MonitorTestDialog(self.root, self).wait()
        except Exception:
            pass

    def _open_speaker_test(self):
        """Speaker Test entry (Tools tab card). Plays test tones
        (winsound only, no busy-guard needed)."""
        try:
            SpeakerTestDialog(self.root, self).wait()
        except Exception:
            pass

    # ---------------- Game Night (feature 9) ---------------- #
    # One press to get the PC ready to play, one press to put it back.
    # Deliberately built on the same two rules Session Pilot already
    # proved out (see _pilot_apply / _pilot_revert):
    #   * apply and revert go through run_tasks(), so cancel, the Admin
    #     Gate, snapshots and applied-tweak bookkeeping all behave as
    #     normal — there is no second code path that changes Windows
    #   * only tweaks Game Night itself turned on are reverted at the
    #     end. Anything the user already had applied is left alone,
    #     because undoing it would undo their own setup, not ours

    def _game_night_tasks(self):
        """Task objects for the Game Session preset. [] if none resolve."""
        try:
            from app.game_night import preset_task_keys
            keys = preset_task_keys()
            by_key = {t.key: t for t in TABS.get("Tweak", [])}
            return [by_key[k] for k in keys if k in by_key]
        except Exception:
            return []

    def _game_in_progress(self):
        """True when the Auto-Pilot watcher currently sees a game running.

        Used only to decide whether the run may pop a scorecard: a modal
        window over someone's fullscreen game is hostile, so a Game Night
        pressed mid-session reports through the log and a toast instead."""
        try:
            pilot = getattr(self, "_pilot", None)
            # active_hit is a PROPERTY on SessionPilot (the exe name, or
            # None when idle) — calling it would raise, and the except
            # below would quietly turn that into "no game running".
            return bool(pilot is not None and pilot.active_hit)
        except Exception:
            return False

    def start_game_night(self, quiet=None):
        """Apply the Game Session preset and remember what we turned on.

        quiet=None (default) preserves the old contract: a modal scorecard
        unless a game is already in progress. The tray passes quiet=True
        explicitly — a tray click may come from inside a game, so it never
        risks a modal over it (one state toast instead)."""
        tasks = self._game_night_tasks()
        if not tasks:
            self.set_status("Game Night: no gaming tweaks available.")
            if quiet:
                self._toast_async("Game Mode", "No gaming settings available.")
            return
        runnable = list(tasks)
        if not is_admin():
            # Limited mode: run what needs no admin rather than refusing
            # outright, and say so — the session still gets most of it.
            runnable = [t for t in tasks if not t.admin_required]
            if len(runnable) != len(tasks):
                self.log("Game Night: not running as Administrator — "
                         f"applying {len(runnable)} of {len(tasks)} settings.")
        if not runnable:
            self.set_status("Game Night needs Administrator for these settings.")
            if quiet:
                self._toast_async("Game Mode needs admin",
                                  "Restart as Administrator to turn it on.")
            return
        # Fresh keys only (the pilot's rule): tweaks already applied before
        # this press stay applied when Game Night ends.
        try:
            before = set(get_tweak_state() or {})
        except Exception:
            before = set()
        fresh = [t.key for t in runnable if t.key not in before]
        try:
            from app.config_persist import set_game_night
            set_game_night(True, fresh)
            # Stay-awake activates only once the session is actually
            # recorded active (matches end_game_night's unconditional
            # release below — the UI only ever calls end_game_night after
            # a successful start, so these stay balanced).
            self._set_stay_awake(True)
        except Exception:
            pass
        # Optional background quieting — only apps the user switched on.
        try:
            from app.config_persist import get_game_night
            from app.game_night import close_apps
            chosen = get_game_night().get("close_apps") or []
            if chosen:
                self.log("Game Night: asking the apps you picked to close…")
                closed = close_apps(chosen, log=self.log)
                if closed:
                    self.log("Game Night: closed " + ", ".join(closed) + ".")
        except Exception:
            pass
        quiet = self._game_in_progress() if quiet is None else bool(quiet)
        try:
            self.log("===== Game Night: getting your PC ready to play =====")
            self.run_tasks("Tweak", runnable, mode="run", quiet=quiet,
                           toast_kind=("gm_on" if quiet else None),
                           toast_extra={"admin_missing": len(tasks) - len(runnable)})
        except Exception:
            pass
        self._refresh_pilot_dialog_manual_btn()

    def end_game_night(self, quiet=None):
        """Put back exactly the tweaks Game Night turned on.

        quiet=None preserves the old auto contract; the tray passes
        quiet=True explicitly (same no-modal-over-games rationale)."""
        # Unconditional (covers the "already back" early-return path too):
        # matches the single _set_stay_awake(True) in start_game_night, so
        # the reference count stays balanced regardless of which path this
        # takes. Only end_game_night ever calls this — never a bare
        # decrement anywhere else for the manual session.
        self._set_stay_awake(False)
        try:
            from app.config_persist import get_game_night, set_game_night
            saved = list(get_game_night().get("keys") or [])
        except Exception:
            saved = []
        if not saved:
            try:
                set_game_night(False)
            except Exception:
                pass
            self.set_status("Game Night ended — your settings were already back.")
            if quiet:
                self._toast_async("Game Mode off", "Normal settings are back.")
            return
        # Intersect with what is STILL applied: the user may have undone
        # some of it manually in the meantime, and reverting a tweak that
        # is already off is both pointless and confusing in the log.
        try:
            current = set(get_tweak_state() or {})
        except Exception:
            current = set()
        keys = [k for k in saved if k in current]
        try:
            by_key = {t.key: t for t in TABS.get("Tweak", [])}
            tasks = [by_key[k] for k in keys
                     if k in by_key and by_key[k].revert is not None]
        except Exception:
            tasks = []
        try:
            set_game_night(False)
        except Exception:
            pass
        if not tasks:
            self.set_status("Game Night ended — your settings were already back.")
            if quiet:
                self._toast_async("Game Mode off", "Normal settings are back.")
            return
        quiet = self._game_in_progress() if quiet is None else bool(quiet)
        try:
            self.log("===== Game Night: putting your settings back =====")
            self.run_tasks("Tweak", tasks, mode="revert", quiet=quiet,
                           toast_kind=("gm_off" if quiet else None))
        except Exception:
            pass
        self._refresh_pilot_dialog_manual_btn()

    def _refresh_pilot_dialog_manual_btn(self):
        """If Auto-Pilot's dialog (where the merged Game Night controls now
        live) is open, keep its Start/End button in sync — it's the only
        UI surface for this state now that the dialog can be reached from
        two paths (its own button, or session_pilot's own auto-trigger
        indirectly changing what's "already applied")."""
        try:
            dlg = getattr(self, "_pilot_dialog", None)
            if dlg is not None:
                dlg._refresh_manual_btn()
        except Exception:
            pass

    def _open_drive_toolkit(self):
        """Drive Toolkit entry (Tools tab card)."""
        try:
            DriveToolkitDialog(self.root, self).wait()
        except Exception:
            pass

    def _open_startup_manager(self):
        """Startup Manager entry (Tools tab card)."""
        try:
            StartupManagerDialog(self.root, self).wait()
        except Exception:
            pass

    def _open_uninstall_programs(self):
        """Uninstall Programs entry (Tools tab card — moved off the
        bottom-corner icon row, 2026-09)."""
        try:
            UninstallProgramsDialog(self.root, self).wait()
        except Exception:
            pass

    def _open_webcam_test(self):
        """Webcam Test entry (Tools tab card). Opens the popup that
        drives the loopback getUserMedia test page (app/webcam_web.py).
        The camera is released when either window closes, so no
        busy-guard is needed — same rationale as the speed popup."""
        try:
            WebcamDialog(self.root, self).wait()
        except Exception:
            pass

    def _open_mouse_tester(self):
        """Mouse Tester entry (Tools tab card). Buttons + aim-test
        popup (pure Tk events, no busy-guard needed)."""
        try:
            MouseTesterDialog(self.root, self).wait()
        except Exception:
            pass

    def _open_pc_specs(self):
        """PC Specs entry (Tools tab card). Read-only summary popup
        (registry + SMBIOS reads, no busy-guard needed)."""
        try:
            SpecsDialog(self.root, self).wait()
        except Exception:
            pass

    def _pilot_preset_tasks(self):
        """Task objects for the configured session preset (tolerant:
        unknown preset names / stale keys degrade to Game Session, then
        to whatever resolves — never raise)."""
        try:
            from app.config_persist import load_config as _load
            want = _load().get("session_pilot_preset", "Game Session")
        except Exception:
            want = "Game Session"
        try:
            presets = PRESETS.get("Tweak", {})
            keys = presets.get(want) or presets.get("Game Session", [])
            by_key = {t.key: t for t in TABS.get("Tweak", [])}
            tasks = [by_key[k] for k in keys if k in by_key]
            if tasks:
                return tasks
            return [by_key[k] for k in presets.get("Game Session", [])
                    if k in by_key]
        except Exception:
            return []

    def _pilot_ensure(self):
        """(Re)build the pilot object from current config (called on
        boot, toggle-on, and watchlist edits — cheap, idempotent).

        Phase-5 watchlist assembly (3 layers):
          1. built-in famous-title basenames (legacy contract)
          2. user picks — full PATHS when known (path-verified matching:
             a same-named exe in another folder never fires), bare names
             for legacy entries
          3. 'watch all installed games' (opt-in): every game found by
             app.game_catalog — resolved FRESH so a newly installed game
             is picked up on the next toggle/start, never a stale cache.
             Unresolvable-exe games contribute their folder's plausible
             exes as bare names (heuristic already filtered
             redists/launchers at catalog level).

        Flicker fix (user bug report): layer 3 is a DISK SWEEP and used
        to run synchronously here — on a big library that froze the Tk
        thread for seconds on toggle/boot. Now the pilot builds + starts
        immediately on layers 1-2 (fast, config-only) and layer 3 merges
        in from a worker thread (generation-guarded, see below)."""
        # H15: the queue and its pump belong to SessionOrchestrator, which
        # starts them idempotently and — unlike the pre-H15
        # `_pilot_pump_started` bool — can stop them again.
        self._pilot_sess.ensure_pump()
        try:
            from app.config_persist import load_config as _load
            from app.session_pilot import (KNOWN_GAME_EXES, SessionPilot,
                                           normalize_exe)
            cfg = _load()
            watched = self._pilot_watched_fast(cfg, normalize_exe)
            # dedupe: a game both hand-picked AND watch-all must not
            # double-register (set() at the pilot level handles it)
            old = getattr(self, "_pilot", None)
            try:
                if old is not None:
                    old.stop()
            except Exception:
                pass
            # The watcher gets the orchestrator's queue producers directly:
            # they are the only two methods it is allowed to call, and they
            # do nothing but queue.put (no cross-thread Tcl, ever).
            pilot = SessionPilot(
                watched=watched,
                on_apply=self._pilot_sess.post_apply,
                on_revert=self._pilot_sess.post_revert)
            self._pilot = pilot
            if cfg.get("session_pilot_watch_all"):
                self._pilot_merge_catalog(pilot)
        except Exception:
            pass

    @staticmethod
    def _pilot_watched_fast(cfg, normalize_exe):
        """Layers 1-2: built-ins + user picks. Pure config, no disk —
        safe on the Tk thread."""
        try:
            from app.session_pilot import KNOWN_GAME_EXES
        except Exception:
            return []
        watched = list(KNOWN_GAME_EXES)
        # user picks: paths stay paths (dir-pinned), names stay names
        for g in cfg.get("session_pilot_games", []) or []:
            try:
                n = normalize_exe(g)
                if n:
                    watched.append(n)
            except Exception:
                pass
        for pth in cfg.get("session_pilot_paths", []) or []:
            if isinstance(pth, str) and pth.strip():
                watched.append(pth)
        return watched

    def _pilot_merge_catalog(self, pilot):
        """Worker-thread layer-3 sweep: enumerate installed games off the
        Tk thread, then merge into the LIVE pilot via a Tk-thread poll.
        Generation-guarded: if the pilot was replaced/stopped meanwhile
        (rapid toggles, app closing), the stale result is dropped instead
        of resurrecting dead state. A game already running when the merge
        lands is caught by the next 15s poll.

        Threading contract (measured 2026-09): worker threads NEVER call
        Tk here — not even winfo_exists/after (a refused foreign Tk call
        burns ~1s then raises). The worker writes a plain holder dict;
        the poller below (Tk thread, scheduled by the caller on the Tk
        thread) publishes."""
        try:
            import threading as _th
        except Exception:
            return
        try:
            seq = getattr(self, "_merge_seq", 0) + 1
        except Exception:
            seq = 1
        self._merge_seq = seq
        holder = {"done": False, "extra": []}

        def _run():
            extra = []
            try:
                from app.game_catalog import installed_games
                for g in installed_games() or []:
                    gp = (g.get("path") or "").strip()
                    if gp:
                        extra.append(gp)
                        continue
                    folder = (g.get("folder") or "").strip()
                    try:
                        import glob as _glob
                        lvl1 = _glob.glob(os.path.join(folder, "*.exe"))
                        lvl2 = _glob.glob(
                            os.path.join(folder, "*", "*.exe"))
                        for cand in (lvl1 + lvl2)[:6]:
                            extra.append(os.path.basename(cand))
                    except Exception:
                        pass
            except Exception:
                pass
            try:
                holder["extra"] = extra
            except Exception:
                pass
            try:
                holder["done"] = True
            except Exception:
                pass

        try:
            _th.Thread(target=_run, daemon=True,
                       name="PilotCatalogMerge").start()
        except Exception:
            return
        try:
            self.root.after(
                150, lambda: self._check_merge(pilot, holder, seq))
        except Exception:
            pass

    def _check_merge(self, pilot, holder, seq):
        """Tk-thread poller for one catalog sweep. Drops superseded
        results (newer ensure bumped the sequence), re-arms while the
        sweep flies, lands exactly once."""
        try:
            if getattr(self, "_merge_seq", 0) != seq:
                return              # superseded — drop silently
        except Exception:
            return
        try:
            if not self.root.winfo_exists():
                return
        except Exception:
            return
        try:
            done = holder.get("done")
        except Exception:
            return
        if not done:
            try:
                self.root.after(
                    150, lambda: self._check_merge(pilot, holder, seq))
            except Exception:
                pass
            return
        self._land_catalog(pilot, holder.get("extra") or [])

    def _land_catalog(self, pilot, extra):
        """Tk thread: merge a swept catalog into the pilot IFF it is
        still the current one (else drop — superseded or stopped)."""
        try:
            if not extra:
                return
            if getattr(self, "_pilot", None) is not pilot:
                return
            try:
                from app.config_persist import load_config as _load
                from app.session_pilot import normalize_exe
                cfg = _load()
            except Exception:
                return
            # rebuild from CURRENT config (a pick made mid-sweep must
            # survive) + the swept layer, then swap atomically-ish
            # (single set_watched call; a concurrent poll may use mixed
            # tables for one tick at most — self-heals next poll)
            watched = self._pilot_watched_fast(cfg, normalize_exe)
            watched.extend(extra)
            try:
                pilot.set_watched(watched)
            except Exception:
                pass
            try:
                self.log("Game Session Auto-Pilot: installed-games "
                         "watchlist merged.")
            except Exception:
                pass
        except Exception:
            pass

    def _pilot_start(self):
        try:
            self._pilot_ensure()
            if self._pilot is not None:
                self._pilot_sess.start()
                self.log("Game Session Auto-Pilot: watching for game launches.")
                # one-time notice when watch-all is active (the new-config
                # default): the auto-detection sweep is disclosed, never a
                # surprise — and points at the exact switch that turns it off.
                try:
                    from app.config_persist import load_config as _lc
                    if _lc().get("session_pilot_watch_all"):
                        if not getattr(self, "_pilot_watchall_notice_shown", False):
                            self._pilot_watchall_notice_shown = True
                            self.set_status(
                                "Auto-Pilot is watching your installed games — "
                                "open the Auto-Pilot dialog to narrow the list.")
                except Exception:
                    pass
        except Exception:
            pass

    def _pilot_stop(self):
        # Stopping NEVER reverts: the user may still be playing, and app
        # close must be side-effect free. Applied tweaks stay badged
        # until Undo reverts them (said plainly in the dialog).
        try:
            # H15: also cancels the pump's pending after(). Pre-H15 the
            # 50ms drain loop rescheduled itself for the rest of the app's
            # life even with the feature switched off.
            self._pilot_sess.stop()
            self.log("Game Session Auto-Pilot: stopped watching.")
        except Exception:
            pass

    def _pilot_refresh_watchlist(self):
        """User added/removed an .exe: rebuild the watchlist live."""
        try:
            running = self._pilot_sess.is_running()
            self._pilot_ensure()
            if running:
                self._pilot_sess.start()
        except Exception:
            pass

    def _pilot_running(self) -> bool:
        return self._pilot_sess.is_running()

    # ------------------------------------------------------------------ #
    # H15 forwarders. Each of these used to carry the logic; they now hand
    # off to SessionOrchestrator, which is the only thing that should own
    # the Auto-Pilot's state. Kept as named methods because
    # tools/smoke_async.py and PilotDialog call them by name.
    # ------------------------------------------------------------------ #
    @property
    def _pilot(self):
        return self._pilot_sess.pilot

    @_pilot.setter
    def _pilot(self, value):
        self._pilot_sess.pilot = value

    @property
    def _pilot_evt_q(self):
        return self._pilot_sess.queue

    @property
    def _pilot_applied_keys(self) -> list:
        return self._pilot_sess.applied_keys

    @_pilot_applied_keys.setter
    def _pilot_applied_keys(self, value):
        self._pilot_sess.applied_keys = value

    @property
    def _pilot_fresh_keys(self) -> list:
        return self._pilot_sess.fresh_keys

    @_pilot_fresh_keys.setter
    def _pilot_fresh_keys(self, value):
        self._pilot_sess.fresh_keys = value

    @property
    def _pilot_state(self) -> SessionState:
        return self._pilot_sess.state

    def _pilot_sink(self, text, color=None):
        """Live line repaint for the open pilot dialog (no-op closed)."""
        try:
            dlg = getattr(self, "_pilot_dialog", None)
            if dlg is not None:
                dlg.set_live_status(text, color)
        except Exception:
            pass

    def _pilot_on_detect(self, hits):
        """Watcher thread → queue only. Kept as a named method because
        the pre-H15 SessionPilot was handed this; it now delegates to the
        orchestrator's producer, which does nothing but queue.put — never
        root.after(), which is what used to drop events silently."""
        self._pilot_sess.post_apply(hits)

    def _pilot_pump(self):
        """Tk thread only: drain pilot events queued by the watcher thread.
        The loop itself moved to SessionOrchestrator in H15 — including the
        part that decides whether to keep going. Pre-H15 this rescheduled
        itself every 50ms for the rest of the app's life, even with the
        feature off; it now runs only while a pilot is attached."""
        self._pilot_sess.pump()

    def _modal_open(self):
        """True while any themed popup is up (a dialog is open).

        User bug report: opening Auto-Pilot right after launch flickered
        the main window behind the popup — the startup pre-warm chain and
        the Install catalog builder kept mapping/raising/laying-out pages
        underneath the modal. Both chains now pause while this is true
        (same postpone-and-retry pattern as the busy-run guard).

        Flicker fix (dropdown variant of the same bug): the grab_current()
        check this used to make reads FALSE the moment a Combobox popdown
        inside the dialog takes the global grab — clicking the preset
        dropdown re-armed the background chains behind the open popup.
        The open-instance registry (ThemedModal.any_open) is the detector
        now; the grab check remains only as the registry's own fallback."""
        return ThemedModal.any_open()

    def _prewarm_should_wait(self):
        """Background chains run only when the UI is truly idle: no run
        in flight AND no popup open (a run owns the progress UI; a modal
        owns the user's eyes — building pages behind either is churn)."""
        try:
            if self._busy:
                return True
        except Exception:
            pass
        return self._modal_open()

    def _pilot_ui_free(self):
        """True when a pilot-triggered run may safely choreograph the
        shared run UI (progress bar, tab buttons, status stream): no
        modal dialog open AND no run in flight.

        User bug report: a game launching/exiting while the Auto-Pilot
        setup dialog (or any modal) was open made the whole app churn
        behind the popup. Flicker fix: the detector is the open-instance
        registry (ThemedModal.any_open), not grab_current() — a Combobox
        popdown inside an open dialog releases the grab and would read
        'free' at the exact moment the user is mid-dropdown."""
        try:
            with self._busy_lock:
                if self._busy:
                    return False
        except Exception:
            pass
        if ThemedModal.any_open():
            return False
        return True

    def _set_stay_awake(self, on: bool):
        """SetThreadExecutionState during a game session — audit fix
        (stay-awake): neither the manual "Game Night" session nor
        Auto-Pilot's automatic per-game trigger stopped Windows sleeping
        or the display turning off mid-game (a real, common annoyance —
        most games don't call this themselves, relying on the OS's normal
        "user is active" idle detection, which a controller/gamepad-only
        session doesn't feed).

        Reference-counted rather than a bare on/off: the manual session
        and the automatic trigger are independent and CAN overlap (a
        manually-started session while a different tracked game also
        launches) — SetThreadExecutionState itself is a single global flag
        per process, not reference-counted, so naively calling it off when
        EITHER session ends would cut the other's hold still in progress.
        Only the count reaching zero actually releases it.

        BUG-023: the read-modify-write of the counter was NOT atomic, and both
        callers can run off the Tk thread (the Auto-Pilot watcher thread calls
        in from _pilot_apply / _pilot_revert). Two racing increments against one
        decrement leave the count permanently at 1, which means
        SetThreadExecutionState(ES_CONTINUOUS) is never called again: THE PC
        NEVER SLEEPS and the display never turns off until the app exits. A
        comment elsewhere asserted an invariant that only held per-thread.

        So the whole count-then-set is now done under the existing
        _busy_lock, and the counter is initialised in __init__ rather than
        being conjured by getattr on first use.
        """
        if not IS_WINDOWS:
            return
        try:
            import ctypes
            ES_CONTINUOUS = 0x80000000
            ES_SYSTEM_REQUIRED = 0x00000001
            ES_DISPLAY_REQUIRED = 0x00000002
            with self._busy_lock:
                count = getattr(self, "_stay_awake_count", 0)
                count = max(0, count + (1 if on else -1))
                self._stay_awake_count = count
                if count > 0:
                    ctypes.windll.kernel32.SetThreadExecutionState(
                        ES_CONTINUOUS | ES_SYSTEM_REQUIRED | ES_DISPLAY_REQUIRED)
                else:
                    ctypes.windll.kernel32.SetThreadExecutionState(ES_CONTINUOUS)
        except Exception:
            pass

    def _pilot_apply(self, hits):
        """Tk thread: apply the session preset (or defer quietly if the
        user is mid-run — the pilot retries nothing; the session simply
        runs untweaked and the exit path no-ops via registry check)."""
        # Stay-awake covers every path below (busy/modal/no-tasks included)
        # — a game IS being played the moment this fires, whether or not
        # any tweak actually landed. session_pilot.py's own state machine
        # guarantees this fires exactly once per idle->active transition
        # (see _poll: only when leaving "idle"), so this can never
        # over-increment from a single session.
        self._set_stay_awake(True)
        try:
            tasks = self._pilot_preset_tasks()
        except Exception:
            tasks = []
        # H15: a detection IS an active session even when nothing could be
        # applied — the user is playing, which is what the state means.
        self._pilot_sess.note_session_active(True)
        if not tasks:
            return
        busy = False
        try:
            with self._busy_lock:
                busy = self._busy
        except Exception:
            pass
        # flicker fix: registry check (see _pilot_ui_free) — the old
        # grab_current() read False during any open dialog's dropdown
        modal = ThemedModal.any_open()
        runnable = list(tasks)
        if not is_admin():
            # limited mode: only what needs no admin, SILENTLY (a
            # background trigger must never pop "Administrator Required"
            # over someone's game — run_tasks would do exactly that)
            runnable = [t for t in tasks if not t.admin_required]
        # Module-8: intent must reflect what will actually run — capturing
        # applied_keys pre-filter claimed admin tweaks the limited-mode run
        # never applied, leaving revert overbroad (saved only by the live
        # registry intersection downstream).
        try:
            self._pilot_applied_keys = [t.key for t in runnable]
        except Exception:
            self._pilot_applied_keys = []
        try:
            # Fresh-keys only: exclude tweaks that were ALREADY applied
            # before this session (the user may want those persistent —
            # reverting them at session end would undo the user's own
            # deliberate setup, not the pilot's work).
            before = set(get_tweak_state() or {})
        except Exception:
            before = set()
        try:
            self._pilot_fresh_keys = [k for k in self._pilot_applied_keys
                                      if k not in before]
        except Exception:
            self._pilot_fresh_keys = list(self._pilot_applied_keys)
        if busy or modal or not runnable:
            try:
                self.log("Game Session Auto-Pilot: game detected "
                         f"({(hits or ['?'])[0]}) but "
                         + ("a run is active — session runs untweaked."
                            if busy else
                            ("a dialog is open — session runs untweaked."
                             if modal else
                             "no runnable tweaks in limited mode — session runs untweaked.")))
            except Exception:
                pass
            return
        try:
            self.log(f"Game Session Auto-Pilot: applying session preset for {(hits or ['?'])[0]}.")
            self._pilot_sink(f"🎮 {(hits or ['?'])[0]} — session active",
                             COLORS["accent_green"])
            try:
                import threading as _th
                _th.Thread(target=show_toast,
                           args=("🎮 Game Mode ON",
                                 "Session tweaks applied — enjoy the game. "
                                 "They restore automatically when you quit."),
                           daemon=True).start()
            except Exception:
                pass
            self.run_tasks("Tweak", runnable, mode="run", quiet=True)
        except Exception:
            pass

    def _pilot_on_exit(self):
        """Watcher thread → queue only (see _pilot_on_detect)."""
        self._pilot_sess.post_revert()

    def _pilot_revert(self):
        """Tk thread: revert ONLY fresh session keys still marked applied
        (registry intersection). Excluded, and therefore never touched:
        keys the run never managed to apply (busy-skip, failure,
        user-undone mid-session) AND keys that were already applied
        before the session (the user's persistent setup — reverting those
        would undo deliberate choices, not pilot work). Silent when
        there's nothing to do."""
        # H15: the session is over as soon as reconciliation begins; the
        # orchestrator's state is what the dialog and the tray read.
        self._pilot_sess.note_session_active(False)

        # Release stay-awake unconditionally (before the fresh-keys early
        # return below) — the game session is over either way, whether or
        # not there happen to be any tweaks left to put back.
        self._set_stay_awake(False)
        try:
            fresh = list(getattr(self, "_pilot_fresh_keys", []) or [])
        except Exception:
            fresh = []
        if not fresh:
            return
        # UI-freedom guard (user bug report + audit find): a revert that
        # fires while a run owns the progress UI — or while ANY modal
        # dialog is open — must not choreograph shared UI behind the
        # user's context. Worse, run_tasks would pop a "Busy" dialog
        # (possibly over a game) AND the revert would be lost. Skip with
        # an honest log line instead; Undo Tweaks restores manually and
        # the applied-badges stay truthful about what is still on.
        # F01: intent is preserved (keys NOT cleared) so a later exit
        # event can still revert; keys clear only on the runnable path.
        _busy = False
        try:
            with self._busy_lock:
                _busy = self._busy
        except Exception:
            pass
        # flicker fix: registry check (see _pilot_ui_free) — the old
        # grab_current() read False during any open dialog's dropdown
        _modal = ThemedModal.any_open()
        if _busy or _modal:
            try:
                self.log("Game Session Auto-Pilot: game closed but the UI is "
                         "occupied (" + ("a run is active" if _busy else "a dialog is open") +
                         ") — session tweaks stay applied; Undo Tweaks restores them.")
            except Exception:
                pass
            try:
                self._pilot_sink("● Watching…", TAB_ACCENTS["Tweak"])
            except Exception:
                pass
            return
        try:
            current = set(get_tweak_state() or {})
        except Exception:
            current = set()
        keys = [k for k in fresh if k in current]
        if not keys:
            try:
                self._pilot_applied_keys = []
                self._pilot_fresh_keys = []
            except Exception:
                pass
            try:
                self.log("Game Session Auto-Pilot: session ended — settings already restored.")
            except Exception:
                pass
            try:
                self._pilot_sink("● Watching…", TAB_ACCENTS["Tweak"])
            except Exception:
                pass
            return
        try:
            by_key = {t.key: t for t in TABS.get("Tweak", [])}
            tasks = [by_key[k] for k in keys
                     if k in by_key and by_key[k].revert is not None]
        except Exception:
            tasks = []
        if not tasks:
            try:
                self._pilot_applied_keys = []
                self._pilot_fresh_keys = []
            except Exception:
                pass
            return
        try:
            self.log("Game Session Auto-Pilot: game closed — restoring session tweaks.")
            self._pilot_sink("Restoring settings…", COLORS["accent_yellow"])
            try:
                import threading as _th
                _th.Thread(target=show_toast,
                           args=("Game Mode OFF",
                                 "Session tweaks restored to your normal settings."),
                           daemon=True).start()
            except Exception:
                pass
            self.run_tasks("Tweak", tasks, mode="revert", quiet=True)
        except Exception:
            pass
        try:
            self._pilot_applied_keys = []
            self._pilot_fresh_keys = []
        except Exception:
            pass
        try:
            self._pilot_sink("● Watching…", TAB_ACCENTS["Tweak"])
        except Exception:
            pass

    # F02: exact allowlist — the old shell=True Popen ran every target
    # through cmd.exe (latent injection if any caller ever passed
    # non-constant input). Exes go via shell=False argv; documents
    # (.cpl/.msc) via os.startfile; anything else is refused.
    _LAUNCH_EXES = {
        "taskmgr": ("taskmgr",),
        "cleanmgr": ("cleanmgr",),
        "perfmon /rel": ("perfmon", "/rel"),
        "rstrui": ("rstrui",),
        "msinfo32": ("msinfo32",),
    }
    _LAUNCH_DOCS = ("powercfg.cpl", "devmgmt.msc")

    def _launch(self, command):
        try:
            if command in self._LAUNCH_EXES:
                subprocess.Popen(list(self._LAUNCH_EXES[command]), shell=False)
            elif command in self._LAUNCH_DOCS:
                os.startfile(command)  # noqa: S606 — allowlisted document only
            else:
                messagebox.showerror("Could Not Launch", f"Refusing to launch unknown target: {command!r}")
        except Exception as exc:
            messagebox.showerror("Could Not Launch", str(exc))

    def _launch_settings(self, uri: str):
        """Open a Windows Settings page via its ms-settings: URI."""
        try:
            os.startfile(uri)  # noqa: S606 — shell-resolved URI, not an exe
        except Exception as exc:
            messagebox.showerror("Could Not Open", f"Couldn't open Windows Settings:\n{exc}")

    # ---------------- Auto Maintenance dialog ---------------- #

    def _maybe_show_automaint_tip(self):
        """One-time nudge toward Auto-Maintenance after the first
        successful Clean run (see the audit-fix note at the call site).
        Never shown again after this, and never shown at all if the user
        already has a maintenance schedule set up."""
        try:
            # Headless guard (CI/harness): the smoke suites drive real
            # Clean runs with the root withdrawn — an invisible modal would
            # still register in ThemedModal._MODAL_OPEN and make the pilot
            # defer every later apply ("a dialog is open — session runs
            # untweaked"), stalling poll_pilot_applied forever on fresh
            # configs. A tip nobody can see must never block automation;
            # return WITHOUT marking so a real user run still shows it.
            try:
                if self.root is None or not self.root.winfo_exists():
                    return
                if not self.root.winfo_ismapped():
                    return
            except Exception:
                return
            from app.config_persist import has_seen_tip, mark_tip_seen
            if has_seen_tip("automaint_after_first_clean"):
                return
            from app.scheduler import get_schedule_status
            already_on, _ = get_schedule_status()
            if already_on:
                mark_tip_seen("automaint_after_first_clean")
                return
            mark_tip_seen("automaint_after_first_clean")
            if _themed_askyesno(
                    self.root, "Keep it clean automatically?",
                    "Cleaner Tool can run this same clean on a schedule — "
                    "no need to remember to open it.\n\n"
                    "Set up Auto-Maintenance now? (You can always find this "
                    "later via the 🛠️ icon, bottom-left.)",
                    accent=TAB_ACCENTS["Clean"]):
                self._show_schedule_dialog()
        except Exception:
            pass

    def _show_schedule_dialog(self):
        from app.config_persist import load_config
        from app.scheduler import (enable_schedule, disable_schedule,
                                   get_schedule_status, run_auto_update)

        config = load_config()
        enabled = config.get("schedule_enabled", False)
        update_enabled = config.get("schedule_update_enabled", False)
        freq = config.get("schedule_frequency", "weekly")
        time_str = config.get("schedule_time", "03:00")

        # ThemedModal chrome (was the lone fixed-420x420 native-titled
        # dialog): shared 800x600 card, X close, Escape, modal wait —
        # content layout below is unchanged
        modal = ThemedModal(self.root, title="Auto Maintenance",
                            accent=COLORS["accent_green"])
        dlg = modal._dlg

        # Simplified layout: one centered column, every row = label on the
        # left + a control of the SAME size on the right, so the edges line
        # up. Only the essentials are shown; long explanations are gone.
        col = tk.Frame(modal.body, bg=COLORS["bg"])
        col.pack(fill="x", padx=70, pady=(12, 0))
        _CTL_W, _CTL_H = 120, 34

        def _row():
            r = tk.Frame(col, bg=COLORS["bg"])
            r.pack(fill="x", pady=6)
            return r

        def _toggle_row(label, initial, tip=None):
            var = tk.BooleanVar(value=bool(initial))
            r = _row()
            lbl = tk.Label(r, text=label, bg=COLORS["bg"], fg=COLORS["text"],
                           font=(F, 11))
            lbl.pack(side="left")
            btn = AnimatedButton(
                r, text="On" if var.get() else "Off",
                command=lambda: var.set(not var.get()),
                bg=COLORS["surface"], fg=COLORS["text"],
                font=(F, 10, "bold"), width=_CTL_W, height=_CTL_H)
            btn.pack(side="right")
            def _sync(*_):
                on = var.get()
                btn.config_text("On" if on else "Off")
                btn.set_style(bg=COLORS["accent_green"] if on else COLORS["surface"],
                              fg=COLORS["black"] if on else COLORS["text"])
            var.trace_add("write", _sync)
            _sync()
            if tip:
                try:
                    Tooltip(lbl, tip)
                    Tooltip(btn, tip)
                except Exception:
                    pass
            return var

        def _holder(r):
            h = tk.Frame(r, width=_CTL_W, height=_CTL_H, bg=COLORS["surface"])
            h.pack(side="right")
            h.pack_propagate(False)
            return h

        try:
            _elev0 = bool(config.get("auto_elevate", False))
        except Exception:
            _elev0 = False
        try:
            _tray0 = bool(config.get("startup_tray_enabled", False))
        except Exception:
            _tray0 = False
        try:
            _closetray0 = bool(config.get("close_to_tray_enabled", True))
        except Exception:
            _closetray0 = True

        enabled_var = _toggle_row("Auto Clean", enabled,
                                  "Runs your last selected tasks on a schedule")
        upd_var = _toggle_row("Update Apps", update_enabled,
                              "Also runs 'winget upgrade --all' on the schedule")
        elev_var = _toggle_row("Start as Admin", _elev0,
                               "Approve once, then no UAC prompt at launch")
        tray_var = _toggle_row("Start with Windows", _tray0,
                               "Boots minimized to the tray at logon")
        closetray_var = _toggle_row("Close to Tray", _closetray0,
                               "Window X hides to the tray instead of quitting")

        tk.Frame(col, height=1, bg=COLORS["hairline"]).pack(fill="x", pady=(10, 6))

        freq_row = _row()
        tk.Label(freq_row, text="How often", bg=COLORS["bg"], fg=COLORS["text"],
                 font=(F, 11)).pack(side="left")
        freq_var = tk.StringVar(value=freq)
        freq_combo = tk.OptionMenu(_holder(freq_row), freq_var, "daily", "weekly", "monthly")
        freq_combo.config(bg=COLORS["surface"], fg=COLORS["text"],
                          activebackground=COLORS["surface_hover"], activeforeground=COLORS["text"],
                          highlightthickness=0, bd=0, relief="flat",
                          font=(F, 10), indicatoron=True)
        try:
            menu = freq_combo["menu"]
            menu.config(bg=COLORS["surface"], fg=COLORS["text"],
                        activebackground=COLORS["surface_hover"], activeforeground=COLORS["text"],
                        bd=0, relief="flat", font=(F, 10))
        except Exception:
            pass
        freq_combo.pack(fill="both", expand=True)

        time_row = _row()
        tk.Label(time_row, text="Time (24h)", bg=COLORS["bg"], fg=COLORS["text"],
                 font=(F, 11)).pack(side="left")
        time_var = tk.StringVar(value=time_str)
        tk.Entry(_holder(time_row), textvariable=time_var, justify="center",
                 bg=COLORS["surface"], fg=COLORS["text"], insertbackground=COLORS["text"],
                 font=(F, 10), bd=0, highlightthickness=1,
                 highlightbackground=COLORS["hairline"],
                 highlightcolor=COLORS["accent_green"]).pack(fill="both", expand=True)

        total_selected = sum(len(v) for v in config.get("selected_tasks", {}).values())
        tk.Label(modal.body, text=f"Runs {total_selected} tasks from your last run",
                 bg=COLORS["bg"], fg=COLORS["subtext"], font=(F, 9)).pack(pady=(10, 0))

        status_var = tk.StringVar(value="")
        tk.Label(modal.body, textvariable=status_var, bg=COLORS["bg"],
                 fg=COLORS["accent_blue"], font=(F, 9), wraplength=440,
                 justify="center").pack(pady=(4, 0))

        def refresh_status():
            # F14: reconcile live schtasks state into config (external
            # /Delete no longer leaves the toggle stale-True). No status
            # text any more — the dialog stays quiet unless something needs
            # the user's attention (see apply()).
            try:
                from app.scheduler import resync_schedule_flag as _resync
                _resync()
                _resync(["--auto-update"])
            except Exception:
                pass
            try:
                from app.scheduler import resync_startup_flag as _resync_tray
                _resync_tray()
            except Exception:
                pass

        def apply():
            results = []
            # NOTE (audit HIGH fix): this dialog captured its config dict at
            # open time. Saving that WHOLE stale copy rolled back everything
            # a concurrent run wrote meanwhile (applied_tweaks, snapshots) —
            # and the four save_config() calls below were redundant anyway:
            # enable_schedule/disable_schedule already load a FRESH config
            # and persist exactly these schedule fields (scheduler.py).
            # So this function now only calls the scheduler helpers and
            # never persists the stale dict.
            # main maintenance schedule
            if enabled_var.get():
                ok, msg = enable_schedule(freq_var.get(), time_var.get())
                if ok:
                    results.append("Maintenance enabled")
                else:
                    messagebox.showerror("Failed", f"Could not create maintenance task:\n{msg}", parent=dlg)
            else:
                ok, msg = disable_schedule()
                if ok:
                    results.append("Maintenance disabled")
                else:
                    messagebox.showerror("Failed", f"Could not remove maintenance task:\n{msg}", parent=dlg)
            # update-everything schedule (separate schtasks entry)
            if upd_var.get():
                ok, msg = enable_schedule(freq_var.get(), time_var.get(), extra_args=["--auto-update"])
                if ok:
                    results.append("Update Everything enabled")
                else:
                    messagebox.showerror("Failed", f"Could not create update task:\n{msg}", parent=dlg)
            else:
                ok, msg = disable_schedule(["--auto-update"])
                if ok:
                    results.append("Update Everything disabled")
            # always-start-as-admin: flag first (single source of truth),
            # then converge the helper task immediately when possible.
            # Unelevated creation fails honestly here (needs admin once);
            # the reconciler finishes the job on the next elevated launch.
            try:
                from app import elevated_launch as _el3
                from app.elevation import is_admin as _is_admin3
                _el3.save_gate_prefs(bool(elev_var.get()),
                                     bool(config.get("remember_limited", False)))
                if bool(elev_var.get()):
                    if _is_admin3():
                        _ok3, _msg3 = _el3.reconcile()
                        results.append("Auto-elevate on"
                                       if _ok3 else f"Auto-elevate flag on (task: {_msg3})")
                    else:
                        results.append("Auto-elevate will activate next time you run as admin")
                else:
                    if _is_admin3():
                        _ok3, _msg3 = _el3.reconcile()
                        results.append("Auto-elevate off"
                                       if _ok3 else f"Auto-elevate flag off (task: {_msg3})")
                    else:
                        # best-effort now (fails without admin, silently);
                        # the reconciler removes it on the next elevated run
                        try:
                            _el3.delete_task()
                        except Exception:
                            pass
                        results.append("Auto-elevate off (helper task removed next time you run as admin)")
            except Exception as exc:
                results.append(f"Auto-elevate setting saved ({exc})")
            # startup tray (Phase 5): same honest-reporting contract as the
            # schedules above — flag persists through the helper either way.
            try:
                from app.scheduler import enable_startup_tray, disable_startup_tray
                if bool(tray_var.get()):
                    _ok5, _msg5 = enable_startup_tray()
                    results.append("Start with Windows on"
                                   if _ok5 else f"Start with Windows failed: {_msg5}")
                else:
                    _ok5, _msg5 = disable_startup_tray()
                    results.append("Start with Windows off"
                                   if _ok5 else f"Start with Windows off failed: {_msg5}")
            except Exception as exc:
                results.append(f"Start with Windows unset ({exc})")
            # close-to-tray: a plain flag, no helper task — load fresh
            # (same reason as the note above: never save a stale whole
            # config) and persist just this one field.
            try:
                from app.config_persist import save_config as _save_ctr
                _fresh = load_config()
                _fresh["close_to_tray_enabled"] = bool(closetray_var.get())
                _save_ctr(_fresh)
                results.append("Close to Tray on" if closetray_var.get()
                               else "Close to Tray off")
            except Exception as exc:
                results.append(f"Close to Tray unset ({exc})")
            refresh_status()
            # short + quiet: "Saved." unless something failed or is pending
            _attn = [r for r in results
                     if any(k in r.lower() for k in ("fail", "next time", "will activate"))]
            status_var.set("; ".join(_attn) if _attn else "Saved.")

        btn_frame = tk.Frame(modal.body, bg=COLORS["bg"])
        btn_frame.pack(fill="x", padx=70, pady=(12, 16))
        # C-1 audit fix: close through the modal (scrim + bindings + closed
        # flag), not by destroying its Toplevel.
        AnimatedButton(btn_frame, text="Update Apps Now",
                       command=lambda: self._run_update_now(modal),
                       bg=COLORS["surface"], fg=COLORS["text"], font=(F, 10),
                       ).pack(side="left")
        AnimatedButton(btn_frame, text="Apply", command=apply,
                       bg=COLORS["accent_green"], fg=COLORS["black"], font=(F, 10, "bold"),
                       ).pack(side="right")
        AnimatedButton(btn_frame, text="Close", command=modal.close,
                       bg=COLORS["surface"], fg=COLORS["text"], font=(F, 10),
                       ).pack(side="right", padx=(0, 8))
        refresh_status()
        # C-5 audit fix: enter the modal wait like every other dialog (grab +
        # wait_window). Without this the schedule dialog floated grab-less:
        # keyboard focus stayed on the dimmed main window, and the modal
        # detectors (_modal_open/_prewarm_should_wait) could not see it, so
        # background chains kept building pages behind the open dialog.
        modal.wait()

    def _run_update_now(self, parent_modal=None):
        """'Update Everything Now' — runs the Install tab's Update button task
        through the standard worker flow (progress bar + Stop button),
        without touching any schedules.

        C-1 audit fix: close the schedule dialog through the modal's close()
        path (scrim + bindings + closed flag). The old parent_dlg.destroy()
        leaked a mapped scrim that dimmed the app for the rest of the
        session, leaked the scrim's root event bindings, and never set the
        modal's closed flag."""
        from app.tasks.install_tasks import UPDATE_ALL_TASK
        if parent_modal is not None:
            try:
                parent_modal.close()
            except Exception:
                pass
        self.install_selected_mixed([], [UPDATE_ALL_TASK])

    # ---------------- disk monitor (drive chips) ---------------- #
    # User request: show EVERY drive (internal, USB, SD — anything with a
    # drive letter), color-coded by fill level, click opens it in Explorer.
    # Rebuilt each tick so a freshly plugged USB stick shows up within one
    # poll (30s) without a restart. MTP phone storage is deliberately out
    # of scope: no drive letter, slow/flaky free-space queries.

    _MAX_INLINE_CHIPS = 3

    @staticmethod
    def _query_drive_free_total(root_path: str):
        """(free_bytes, total_bytes) for one drive root, or None if the
        query fails. Same Win32 call the pre-flight check and the drive
        chips use; wrapped here so the scorecard's before/after capture
        and any later caller share one honest reader."""
        try:
            import ctypes as _ct
            free = _ct.c_ulonglong(0)
            total = _ct.c_ulonglong(0)
            avail = _ct.c_ulonglong(0)
            if _ct.windll.kernel32.GetDiskFreeSpaceExW(
                    _ct.c_wchar_p(root_path), _ct.byref(avail),
                    _ct.byref(total), _ct.byref(free)):
                return free.value, total.value
        except Exception:
            pass
        return None

    @staticmethod
    def _query_drives():
        """All ready drives with free/total bytes. Uses GetLogicalDrives +
        GetDiskFreeSpaceExW (both instant, no WMI) — returns
        [(letter, root_path, free_gb, total_gb)], C: first, rest sorted."""
        import ctypes
        drives = []
        bitmask = ctypes.windll.kernel32.GetLogicalDrives()
        if not bitmask:
            return drives
        for i in range(26):
            if not (bitmask >> i) & 1:
                continue
            letter = chr(ord("A") + i)
            root = f"{letter}:\\"
            free = ctypes.c_ulonglong(0)
            total = ctypes.c_ulonglong(0)
            avail = ctypes.c_ulonglong(0)
            # drives with 0 total = no media (empty card reader slots) — skip
            res = ctypes.windll.kernel32.GetDiskFreeSpaceExW(
                ctypes.c_wchar_p(root),
                ctypes.byref(avail), ctypes.byref(total), ctypes.byref(free),
            )
            if res and total.value > 0:
                gb = 1024 ** 3
                drives.append((letter, root, free.value / gb, total.value / gb))
        # C: first (always the one people glance for), then alphabetical
        drives.sort(key=lambda d: (d[0] != "C", d[0]))
        return drives

    @staticmethod
    def _drive_fill_color(free_gb, total_gb):
        """Green normally, amber under ~15% free, red under ~5% — the
        'will this game fit?' glance check."""
        if total_gb <= 0:
            return COLORS["subtext"]
        ratio = free_gb / total_gb
        if ratio <= 0.05:
            return COLORS["accent_red"]
        if ratio <= 0.15:
            return COLORS["accent_yellow"]
        return COLORS["accent_green"]

    def _open_drive(self, root):
        """Open a drive in Explorer via os.startfile (ShellExecute) — the
        right route for folders; the browser-raising _open_url path is for
        web URLs only. Works on stripped Windows where webbrowser-based
        opens fail silently."""
        import os as _os
        try:
            _os.startfile(root)
        except Exception as exc:
            messagebox.showerror("Could Not Open Drive",
                                 f"Couldn't open {root}\n\n{exc}")

    def _show_extra_drives_menu(self):
        """The '+N ▾' overflow menu: every drive that didn't fit inline,
        with free space and the same color rules; click opens in Explorer."""
        if not self._drive_extra:
            return
        menu = tk.Menu(self.root, tearoff=0, bg=COLORS["surface"],
                       fg=COLORS["text"], activebackground=COLORS["surface_hover"],
                       activeforeground=COLORS["text"], bd=0,
                       font=(F, 9))
        for i, (letter, root, free_gb, total_gb) in enumerate(self._drive_extra):
            col = self._drive_fill_color(free_gb, total_gb)
            menu.add_command(
                label=f"{letter}:  {free_gb:.0f} GB free of {total_gb:.0f} GB",
                command=lambda r=root: self._open_drive(r),
            )
            try:
                menu.entryconfig(i, foreground=col)
            except Exception:
                pass
        try:
            menu.tk.call("tk_popup", menu, self.root.winfo_pointerx(),
                          self.root.winfo_pointery() + 12)
        except Exception:
            pass

    # ---------------- low-space nudge (feature 4) -------------------- #
    # A per-drive hysteresis state machine driven by the disk-monitor
    # tick: fire at <=5% free, clear only above 7% (a drive hovering at
    # the boundary must not re-toast every 30s). Firing = native toast
    # (in a thread — subprocess must never block the Tk thread) + an
    # amber chip in the status row that opens Storage Insight. A
    # background dry-run scan enriches the chip tooltip with a real
    # reclaimable number when it lands; the dialog itself always
    # re-scans live, so a stale estimate can never cause a wrong clean.

    _NUDGE_FIRE_RATIO = 0.05
    _NUDGE_CLEAR_RATIO = 0.07

    def _check_low_space(self, drives):
        """Tick entry — call with the monitor's drive list (Tk thread).
        Pure state transitions + widget updates; the only subprocess
        (the toast) is handed to a daemon thread."""
        try:
            by_letter = {d[0]: d for d in drives}
        except Exception:
            return
        prev = getattr(self, "_drive_was_red", {}) or {}
        now = {}
        try:
            for letter, (_l2, _root, free_gb, total_gb) in by_letter.items():
                ratio = (free_gb / total_gb) if total_gb and total_gb > 0 else 1.0
                was = prev.get(letter)
                if was is True:
                    is_red = ratio <= self._NUDGE_CLEAR_RATIO
                    if not is_red:
                        self._hide_nudge_chip(letter)   # recovered
                else:
                    is_red = ratio <= self._NUDGE_FIRE_RATIO
                    if is_red and letter not in getattr(self, "_nudge_dismissed", set()):
                        self._fire_low_space_nudge(letter, free_gb)
                now[letter] = is_red
            # drives that vanished (unplugged USB) take their chips along
            for letter in list(getattr(self, "_nudge_chips", {}) or {}):
                if letter not in by_letter:
                    self._hide_nudge_chip(letter)
            # in-place free-GB refresh for visible chips (no widget churn)
            for letter, (_l2, _root, free_gb, _t) in by_letter.items():
                chip = (getattr(self, "_nudge_chips", {}) or {}).get(letter)
                if chip is not None:
                    try:
                        lbl = chip.get("label")
                        if lbl is not None and lbl.winfo_exists():
                            lbl.config(text=f" ⚠ {letter}: {free_gb:.1f} GB free ")
                    except Exception:
                        pass
            self._drive_was_red = now
        except Exception:
            pass

    def _fire_low_space_nudge(self, letter, free_gb):
        """New red episode: toast (threaded) + chip + estimate scan."""
        try:
            import threading as _th
            _th.Thread(target=notify_low_space,
                       args=(letter, float(free_gb)),
                       daemon=True).start()
        except Exception:
            pass
        self._show_nudge_chip(letter, free_gb)
        self._ensure_nudge_estimate()

    def _nudge_chip_text(self, letter, free_gb):
        return f" ⚠ {letter}: {free_gb:.1f} GB free "

    def _nudge_tip_text(self, letter, free_gb, total_gb):
        base = (f"{letter}:\\ — {free_gb:.0f} GB free of {total_gb:.0f} GB "
                f"({(free_gb / total_gb * 100 if total_gb else 0):.0f}% free). "
                "Click for Storage Insight.")
        est = getattr(self, "_nudge_estimate", None)
        if est is not None:
            try:
                from app.utils import format_bytes as _fb
                import time as _ti
                base += (f"\n~{_fb(est[0])} of safe junk found "
                         f"(scanned {_ti.strftime('%H:%M', _ti.localtime(est[1]))}) — "
                         "click to review and clean.")
            except Exception:
                pass
        return base

    def _show_nudge_chip(self, letter, free_gb):
        chips = getattr(self, "_nudge_chips", None)
        frame_holder = getattr(self, "_nudge_frame", None)
        if chips is None or frame_holder is None:
            return
        if letter in chips:
            return
        try:
            if not frame_holder.winfo_exists():
                return
            # drives list for the tooltip totals (best-effort snapshot)
            total_gb = 0.0
            try:
                for _letter, _r, _f, _t in self._last_drives or []:
                    if _letter == letter:
                        total_gb = _t
                        break
            except Exception:
                pass
            box = tk.Frame(frame_holder, bg="#3A2C12")
            lbl = tk.Label(box, text=self._nudge_chip_text(letter, free_gb),
                           font=(F, 9, "bold"), bg="#3A2C12",
                           fg=COLORS["accent_yellow"], cursor="hand2")
            lbl.pack(side="left", padx=(8, 2), pady=3)
            lbl.bind("<Button-1>", lambda e: self._open_storage_insight())
            tip = Tooltip(lbl, self._nudge_tip_text(letter, free_gb, total_gb))
            x = tk.Label(box, text="✕", font=(F, 8), bg="#3A2C12",
                         fg=COLORS["subtext"], cursor="hand2")
            x.pack(side="left", padx=(0, 8))
            x.bind("<Button-1>", lambda e, l=letter: self._dismiss_nudge(l))
            Tooltip(x, "Dismiss for this session")
            box.pack(side="left", padx=(0, 6))
            chips[letter] = {"frame": box, "label": lbl, "tip": tip}
        except Exception:
            pass

    def _hide_nudge_chip(self, letter):
        try:
            chips = getattr(self, "_nudge_chips", None) or {}
            chip = chips.pop(letter, None)
            if chip is not None:
                try:
                    chip["frame"].destroy()
                except Exception:
                    pass
        except Exception:
            pass

    def _dismiss_nudge(self, letter):
        """Session dismissal: respected even if the drive re-reddens."""
        try:
            self._nudge_dismissed.add(letter)
        except Exception:
            pass
        self._hide_nudge_chip(letter)

    def _ensure_nudge_estimate(self):
        """One background dry-run scan per session (the Storage Insight
        engine): enriches chip tooltips with a real reclaimable number.
        Skipped while a run owns the disk, while one is already flying,
        or when an estimate already landed. Phase-6: consults the
        shared cache first (a Storage dialog walk often just filled it),
        and the walk itself is cancelable via the session token."""
        try:
            if getattr(self, "_nudge_estimate", None) is not None:
                return
            if getattr(self, "_nudge_scan_running", False):
                return
            with self._busy_lock:
                if self._busy:
                    return
            self._nudge_scan_running = True
        except Exception:
            return

        def _worker():
            try:
                import time as _ti
                from app.storage_scan import cached_estimate, measure_all_cached
                # cache hit (Storage/Health walked recently): instant
                hit = cached_estimate()
                if hit is not None:
                    total, stamp = hit
                else:
                    token = getattr(self, "_nudge_scan_token", [False])
                    total = measure_all_cached(cancelled=lambda: token[0])
                    stamp = _ti.time()
                try:
                    # F10: no winfo_exists off-thread — landing on a dead
                    # root raises inside after() and is swallowed.
                    self._dispatch.post(lambda: self._land_nudge_estimate(total, stamp))
                except Exception:
                    pass
            except Exception:
                pass
            finally:
                self._nudge_scan_running = False

        try:
            import threading as _th
            _th.Thread(target=_worker, daemon=True).start()
        except Exception:
            self._nudge_scan_running = False

    def _land_nudge_estimate(self, total, stamp):
        """Tk-thread landing for the estimate: store + refresh tooltips."""
        # F11: None = scan busy/cancelled — keep prior estimate, don't
        # store a None that _fb() would choke on.
        if total is None:
            return
        try:
            self._nudge_estimate = (total, stamp)
            for letter, chip in (getattr(self, "_nudge_chips", {}) or {}).items():
                try:
                    tip = chip.get("tip")
                    if tip is not None:
                        tip.text = self._refresh_chip_tip(letter)
                except Exception:
                    pass
        except Exception:
            pass

    def _refresh_chip_tip(self, letter):
        """Rebuild one chip's tooltip from live drive data + estimate."""
        try:
            for _letter, _r, _f, _t in self._last_drives or []:
                if _letter == letter:
                    return self._nudge_tip_text(letter, _f, _t)
        except Exception:
            pass
        return "Click for Storage Insight."

    def _rebuild_drive_chips(self, drives):
        """Recreate the inline chips (only when the drive set changed —
        labels update in place otherwise). C: always inline; overflow
        collapses into the '+N' menu chip."""
        for w in self._drive_chips_frame.winfo_children():
            w.destroy()
        self._drive_chips = []
        self._drive_extra = []
        inline, extra = drives[:self._MAX_INLINE_CHIPS], drives[self._MAX_INLINE_CHIPS:]
        for letter, root, free_gb, total_gb in inline:
            col = self._drive_fill_color(free_gb, total_gb)
            # fixed width stops the status row from shifting every poll as
            # the free-GB number changes digit count (audit minor CLS)
            lbl = tk.Label(self._drive_chips_frame,
                            text=f" {letter}: {free_gb:5.0f} GB free ",
                            font=(F, 9, "bold"), bg=COLORS["bg"], fg=col,
                            cursor="hand2")
            lbl.pack(side="left", padx=(6, 0))
            lbl.bind("<Button-1>", lambda e, r=root: self._open_drive(r))
            # Storage Insight (feature 3): right-click opens the insight
            # dialog; left-click keeps the long-standing Explorer behavior
            lbl.bind("<Button-3>", lambda e: self._open_storage_insight())
            Tooltip(lbl, f"{root} — {free_gb:.0f} GB free of {total_gb:.0f} GB. "
                         "Click to open in Explorer, right-click for Storage Insight.")
            self._drive_chips.append((lbl, root))
        if extra:
            self._drive_extra = extra
            more = tk.Label(self._drive_chips_frame,
                            text=f" +{len(extra)} drives ▾ ", font=(F, 9, "bold"),
                            bg=COLORS["bg"], fg=COLORS["subtext"], cursor="hand2")
            more.pack(side="left", padx=(6, 0))
            more.bind("<Button-1>", lambda e: self._show_extra_drives_menu())
            Tooltip(more, "More connected drives:\n" +
                    "\n".join(f"{d[0]}: {d[2]:.0f} GB free" for d in extra))

    def _start_disk_monitor(self):
        if hasattr(self, "_disk_monitor_after_id") and self._disk_monitor_after_id:
            try:
                self.root.after_cancel(self._disk_monitor_after_id)
            except Exception:
                pass
        self._disk_monitor_running = True

        # audit fix (M9): _query_drives() called GetDiskFreeSpaceExW for up
        # to 26 drive letters synchronously on the Tk thread every 30s (plus
        # once at startup). An unreachable mapped network drive can make
        # that Win32 call stall for its OS-level timeout, freezing the
        # entire UI (no repaint, no button clicks) until it returns. Query
        # in a daemon thread and marshal only the paint back to Tk via
        # root.after(0, ...).
        import threading as _threading

        def _apply_drives(drives):
            if not getattr(self, "_disk_monitor_running", False):
                return
            try:
                # remember the raw list for nudge tooltips (free/total)
                self._last_drives = list(drives or [])
                # rebuild widgets only when the drive SET changes (plug /
                # unplug); color+text refresh in place on every tick
                key = tuple(d[0] for d in drives)
                if key != self._drive_last_key:
                    self._drive_last_key = key
                    self._rebuild_drive_chips(drives)
                for lbl, root in self._drive_chips:
                    for letter, r2, free_gb, total_gb in drives:
                        if r2 == root:
                            lbl.config(
                                text=f" {letter}: {free_gb:5.0f} GB free ",
                                fg=self._drive_fill_color(free_gb, total_gb),
                            )
                            break
                # low-space nudge state machine (own try: a nudge bug must
                # never break the chip refresh above)
                try:
                    self._check_low_space(drives)
                except Exception:
                    pass
            except Exception:
                pass
            try:
                if getattr(self, "_disk_monitor_running", False) and self.root.winfo_exists():
                    self._disk_monitor_after_id = self.root.after(30000, update_disk)
            except Exception:
                pass

        def _query_then_apply():
            try:
                drives = self._query_drives()
            except Exception:
                drives = []
            # F10: flag-check only off-thread; dead root raises in after().
            try:
                if getattr(self, "_disk_monitor_running", False):
                    self._dispatch.post(lambda: _apply_drives(drives))
            except Exception:
                pass

        def update_disk():
            if not getattr(self, "_disk_monitor_running", False):
                return
            _threading.Thread(target=_query_then_apply, daemon=True).start()

        try:
            # MED-014: deliberately NOT _dispatch.post(). This runs on the Tk
            # thread (the monitor is started from _build_chrome) and the id is
            # kept so _stop_disk_monitor can after_cancel() it. Posting would
            # take the callback off the after() list and the monitor would
            # become unstoppable at shutdown. The worker-side hop in
            # _query_then_apply above is the one that needed converting.
            self._disk_monitor_after_id = self.root.after(0, update_disk)
        except Exception:
            pass

    def _stop_disk_monitor(self):
        self._disk_monitor_running = False
        if hasattr(self, "_disk_monitor_after_id") and self._disk_monitor_after_id:
            try:
                self.root.after_cancel(self._disk_monitor_after_id)
            except Exception:
                pass
            self._disk_monitor_after_id = None

    # ---------------- log (in-memory only, #16 simplified per user) ------- #
    # No log window in the UI anymore — the log accumulates in memory and
    # "Export Logs" (Quick Tools menu) saves it to a .txt for debugging.

    def _build_log(self, parent):
        self._log_lines = []
        self._log_lines.append("Welcome to Cleaner Tool.")

    def log(self, text):
        def _do():
            self._log_lines.append(f"[{time.strftime('%H:%M:%S')}] {text}")
            if len(self._log_lines) > 8000:
                del self._log_lines[:2000]
        try:
            if threading.current_thread() is threading.main_thread():
                _do()
            else:
                self._dispatch.post(_do)
        except Exception:
            pass

    # keep old internal name working for close-guard message
    def _log_full(self, text):
        self.log(text)

    def export_logs(self):
        """Save the full in-memory log to a .txt the user picks (user
        request: replaces the old View Details window — keeps debugging
        possible without console clutter in the UI)."""
        from tkinter import filedialog
        try:
            default_name = f"CleanerTool_Log_{time.strftime('%Y%m%d_%H%M%S')}.txt"
            path = filedialog.asksaveasfilename(
                parent=self.root,
                title="Export Logs",
                defaultextension=".txt",
                initialfile=default_name,
                filetypes=[("Text files", "*.txt"), ("All files", "*.*")],
            )
            if not path:
                return
            header = (
                f"Cleaner Tool log export\n"
                f"Exported: {time.strftime('%Y-%m-%d %H:%M:%S')}\n"
                f"Edition: {self._windows_edition_str()}\n"
                f"{'=' * 60}\n"
            )
            with open(path, "w", encoding="utf-8") as f:
                f.write(header + "\n".join(self._log_lines) + "\n")
            # user bug: exporting mid-run overwrote the live "Running X…"
            # status with "Log saved to …" and it never came back. When a
            # run is active the status belongs to the run — log only.
            if not getattr(self, "_busy", False):
                self.set_status(f"Log saved to {path}")
            self.log(f"Log exported to {path}")
        except Exception as exc:
            messagebox.showerror("Export Failed", f"Could not save the log:\n{exc}")

    def _windows_edition_str(self) -> str:
        try:
            import winreg
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                                r"SOFTWARE\Microsoft\Windows NT\CurrentVersion") as k:
                product = winreg.QueryValueEx(k, "ProductName")[0]
                display = winreg.QueryValueEx(k, "DisplayVersion")[0]
            return f"{product} ({display})"
        except Exception:
            return "Windows"

    # ---------------- status line (fading, #16) ---------------- #

    def set_status(self, text):
        def _do():
            try:
                if hasattr(self, "status_lbl") and self.status_lbl.winfo_exists():
                    # cancel pending fades
                    for aid in getattr(self, "_status_fade_ids", []):
                        try:
                            self.root.after_cancel(aid)
                        except Exception:
                            pass
                    self._status_fade_ids = []
                    self.status_lbl.config(text=text, fg=TAB_ACCENTS.get(self.active_tab, COLORS["accent_green"]))
                    # fade back to muted after 4s (unless running)
                    if not self._busy:
                        aid = self.root.after(4000, lambda: self._fade_status())
                        self._status_fade_ids.append(aid)
            except Exception:
                pass
        try:
            if threading.current_thread() is threading.main_thread():
                _do()
            else:
                self._dispatch.post(_do)
        except Exception:
            pass

    def _fade_status(self):
        try:
            self.status_lbl.config(fg=COLORS["subtext"])
        except Exception:
            pass

    # ---------------- progress ---------------- #

    def _ui_hop(self, fn) -> None:
        """Marshal a callable onto the Tk thread, from either thread.

        MED-014, and the reason this is not just _dispatch.post() everywhere:
        a 0 ms `after()` timer is only ever at risk when it is scheduled from
        a WORKER thread, because whether it fires depends on the Tk thread
        happening to be inside Tcl's event loop when the cross-thread
        notifier arrives. From the Tk thread it is reliable by construction.

        So the worker path goes through TkDispatcher, whose delivery is
        guaranteed, while the main-thread path keeps the historical
        `after(0, ...)` deferral. That deferral is load-bearing: the
        pre-flight countdown relies on that one tick, and making it inline
        advanced the countdown enough to outrun the stop.
        """
        try:
            if threading.current_thread() is threading.main_thread():
                self.root.after(0, fn)
            else:
                self._dispatch.post(fn)
        except Exception:
            pass

    def _set_progress(self, value, maximum=None):
        def _do():
            try:
                # M10: `if maximum:` misrouted maximum=0 into the fraction
                # branch (and a real value/0 would raise); be explicit.
                if maximum is not None:
                    self.progress_bar.set_fraction(value / maximum if maximum else 0.0)
                else:
                    self.progress_bar.set_fraction(value)
            except Exception:
                pass
        self._ui_hop(_do)

    def _stop_indeterminate(self):
        """Turn off the shimmer and paint the full bar — ON THE TK THREAD.

        F-003 fix: _run_tasks_worker used to call
        progress_bar.set_indeterminate(False) directly from the worker
        thread. set_indeterminate(False) does after_cancel + Canvas _draw
        (delete/all + item creation) — real Tcl calls. Tkinter is not
        thread-safe; the identical Install-tab path (install_selected_mixed
        _done) was already fixed to marshal via root.after for exactly this
        reason, but this call site never got the fix. Same shape as
        _set_progress above: try/except only guards the shutdown race."""
        def _do():
            try:
                self.progress_bar.set_indeterminate(False)
            except Exception:
                pass
        self._ui_hop(_do)

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
            # bottom frame = the progress bar's own parent; pack at its top
            self.progress_bar.pack(fill="x", pady=(6, 2))
            self.cancel_btn.pack(side="right")
            self._progress_hidden = False
        except Exception:
            pass

    def _progress_hide(self):
        try:
            self.progress_bar.pack_forget()
            self.cancel_btn.pack_forget()
            self._progress_hidden = True
        except Exception:
            pass

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
            _themed_showinfo(self.root, "Busy",
                             "Please wait for the current operation to finish.",
                             accent=COLORS["accent_yellow"])
            return
        gen = self._claim_run()
        if gen is None:
            return

        # Admin gating for Essentials that need it (same rule as other tabs)
        if not is_admin():
            admin_blocked = [t for t in tasks if t.admin_required]
            if admin_blocked:
                names = ", ".join(t.label for t in admin_blocked)
                self.log(f"Limited mode: skipping {len(admin_blocked)} admin-only installer(s): {names}")
                tasks = [t for t in tasks if not t.admin_required]
                if tasks or apps:
                    _themed_showinfo(
                        self.root, "Administrator Required",
                        f"Skipped {len(admin_blocked)} installer(s) that need "
                        f"admin:\n{names}\n\nContinuing with the rest.",
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
        self.set_status(f"Installing {total} item(s)...")
        self.log(f"===== Installing {total} selected item(s) =====")

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

        def _done(ok, summary, ok_n=0, fail_n=0):
            if _done_called[0]:
                return
            _done_called[0] = True
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
                self._release_run(ok)
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
                # as everything else (was the last white OS box in the app)
                _themed_showinfo(self.root, "Install Done", summary,
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
                self.set_status(f"Running {task.label}...")
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
            if stopped:
                self.log("Stopped — remaining items were skipped.")
                _done(False, f"Stopped: {ok_n} finished before stopping.", ok_n, fail_n)
            else:
                summary = f"Install complete: {ok_n} succeeded" + (f", {skipped_n} skipped" if skipped_n else "") + (f", {fail_n} failed" if fail_n else "") + "."
                _done(True, summary, ok_n, fail_n)

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
                _done(False, "Stopped before anything finished.")
            except BaseException as exc:      # noqa: BLE001 - see above
                # Deliberately BaseException: on a worker thread the only two
                # sane options are report-it or wedge-the-UI, and a base
                # exception escaping here is exactly how the wedge happened.
                self.log(f"  ! install worker crashed: {exc!r}")
                _done(False, f"Install failed unexpectedly: {exc}")
            finally:
                # Unconditional release. _done is idempotent, so on the normal
                # path this is a no-op; if the body died without reporting,
                # the UI is still reset and the user can try again.
                _done(False, "Install ended without reporting a result.")

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
                    _themed_showinfo(
                        self.root,
                        "Administrator Required",
                        f"Skipped {len(admin_tasks)} admin-only task(s):\n{names}\n\n"
                        "Running the rest in limited mode.",
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
                _themed_showinfo(self.root, "Busy", "Please wait for the current operation to finish.",
                                 accent=COLORS["accent_yellow"])
            return
        gen = self._claim_run()
        if gen is None:
            return

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
                if not _themed_askyesno(self.root, "Pre-flight Check", msg + "\n\nStart anyway?",
                                        accent=COLORS["accent_yellow"]):
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
                msg += "\n\nDo you want to continue?"
                if not _themed_askyesno(self.root, "Confirm Task Combination", msg,
                                        accent=COLORS["accent_red"]):
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

        # Scorecard 'before' snapshot: C: free/total bytes captured on the
        # Tk thread right before the worker starts (one instant
        # GetDiskFreeSpaceExW call, same source as _preflight_check /
        # _query_drives). The worker reads it via self._run_before_bytes;
        # a failed read (None) simply renders the scorecard without the
        # before/after bar — never invented numbers.
        self._run_before_bytes = self._query_drive_free_total(
            _system_drive_root())
        run_before_bytes = self._run_before_bytes
        # Feature 4: same instant reading for every ready drive, so the
        # scorecard can name each drive that gained space (a clean often
        # spans the system SSD and a games drive).
        self._run_before_drives = _snapshot_all_drives(
            self._query_drives, self._query_drive_free_total)
        run_before_drives = self._run_before_drives
        # H10: the run owns its results list. `self._run_results` still points
        # at it (tools/smoke_async.py reads that attribute after a run), but
        # rebinding it on the next run no longer corrupts this run's data: the
        # worker and the deferred result card both close over the local.
        run_results = []
        self._run_results = run_results

        thread = threading.Thread(target=self._run_tasks_worker,
                                  args=(tab_name, tasks, mode, quiet,
                                        toast_kind, toast_extra, gen,
                                        run_results, run_before_bytes,
                                        run_before_drives), daemon=True)
        self._mark_running(thread)
        thread.start()

    def _bind_global_shortcuts(self):
        """improvement-report 2.2: Ctrl+Enter runs the active tab, Escape
        stops a running task (dialogs already bind their own Escape-to-
        close — see ThemedModal — so this never conflicts, it only fires
        when the main window itself has focus), Ctrl+1..5 switches tabs,
        Ctrl+L copies the run log. Best-effort: any failure here should
        never block the app from starting."""
        try:
            self.root.bind("<Control-Return>", lambda _e: self._shortcut_run())
            self.root.bind("<Control-KP_Enter>", lambda _e: self._shortcut_run())
            self.root.bind("<Escape>", lambda _e: self._request_cancel())
            for i, name in enumerate(TAB_NAMES, start=1):
                if i > 9:
                    break
                self.root.bind(f"<Control-Key-{i}>",
                               lambda _e, n=name: self._switch_to(n))
            self.root.bind("<Control-l>", lambda _e: self._shortcut_copy_log())
            self.root.bind("<Control-L>", lambda _e: self._shortcut_copy_log())
        except Exception:
            pass

    def _shortcut_run(self):
        if self._busy:
            return
        try:
            tab = self.tabs.get(self.active_tab)
            if tab is not None and hasattr(tab, "_run_selected"):
                tab._run_selected()
        except Exception:
            pass

    def _shortcut_copy_log(self):
        try:
            text = "\n".join(self._log_lines)
            self.root.clipboard_clear()
            self.root.clipboard_append(text)
            self.set_status("Log copied to clipboard.")
        except Exception:
            self.set_status("Could not copy the log.")

    @property
    def tweak_state(self) -> dict:
        """Read-only LIVE view of which tweaks are applied: {task_id: True}.

        H11 replaced a snapshot attribute that four call sites refreshed by
        hand. The refreshes were the bug: any run that finished through a path
        nobody remembered left the badges showing pre-run state, and
        TaskTab._context_applied_count worked around it by reading the registry
        directly with a comment admitting the cache "can lag a run-completion
        hop".

        Two properties make this safe to read from anywhere:

          * It always re-reads, so it cannot go stale. A run finishing needs no
            invalidation, because there is nothing to invalidate.
          * It returns a FRESH dict every access. A caller that mutates the
            result corrupts nothing but its own copy — which is why this can
            be read-only at all.

        Never raises: a registry read failure yields {}, because a badge is
        never worth an exception. The bare `get_tweak_state` name is resolved
        at call time on purpose, so tools/smoke_async.py can still substitute
        a deterministic registry by patching this module.
        """
        try:
            return dict(get_tweak_state() or {})
        except Exception:
            return {}

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

    def _release_run(self, ok=True):
        """End of a run: clear the worker thread and the cancel token and
        return to IDLE, all under one lock. Replaces the three separate
        statements that used to be able to interleave with a second run's
        claim.

        The outcome is recorded in `last_run_outcome` rather than parked in
        self._state for two statements. Setting DONE/ERROR and then
        immediately overwriting it with IDLE would make those two enum
        members unobservable — a race, not a state — so they describe the last
        run instead, where a caller can actually read them.
        """
        with self._busy_lock:
            self._last_run_ok = bool(ok)
            self._worker_thread = None
            self._state = RunState.IDLE
        self._cancel_requested = False

    @property
    def last_run_outcome(self) -> "RunState | None":
        """DONE/ERROR for the most recent run, or None if none has finished.
        CANCELLING is deliberately not an outcome: a stopped run reports
        through the run summary, and the state has already moved on."""
        if not hasattr(self, "_last_run_ok"):
            return None
        return RunState.DONE if self._last_run_ok else RunState.ERROR

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
                          run_results=None, run_before_bytes=None,
                          run_before_drives=None):
        verb = "Undoing" if mode == "revert" else "Running"
        self.log(f"===== {verb} {len(tasks)} {tab_name} task(s) =====")

        ctx = TaskContext(log=self.log, set_status=self.set_status,
                          cancelled=lambda: self._cancel_requested)

        total_bytes = 0
        completed, failed = 0, 0
        skipped_n = 0
        cancelled = False
        self._set_progress(0, len(tasks))

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

        def _record(idx, task, status, task_bytes, exc):
            # F2-1: the single place mapping the shared helper's outcome to
            # the scorecard rows + counters + applied-tweak bookkeeping
            # (worker-thread locals only; one result row per attempted task,
            # same as the old inline branches).
            nonlocal total_bytes, completed, failed, skipped_n, cancelled
            if status == "ok":
                completed += 1
                total_bytes += task_bytes
                results.append((task.label, "ok", task_bytes, None))
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
                results.append((task.label, "skip", 0, str(exc) if exc else None))
                self.log(f"  (skipped) {task.label}: {exc}")
            elif status == "stop":
                # F-2 audit fix: utils.run_cmd_checked deliberately raises
                # TaskCancelled for a user Stop (its H6 contract) — the run
                # was stopped, not failed; no tweak is marked applied/
                # reverted, remaining tasks are skipped by the next
                # iteration's cancelled() check, summary reports 'stopped'.
                cancelled = True
                results.append((task.label, "stop", 0, None))
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
                results.append((task.label, "fail", 0, reason))
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
                    self.log("Cancelled by user — remaining tasks were skipped.")
                    cancelled = True
                    break
                self.set_status(f"{verb}: {task.label}...")
                status, task_bytes, exc = self._invoke_single_task(ctx, task, mode)
                _record(idx, task, status, task_bytes, exc)
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
                results.append((t.label, "fail", 0, str(exc)))
                failed += 1

        # tasks never attempted (user Stop mid-run) show as 'stopped' rows
        # so the scorecard tells the whole story. Every attempted task
        # appended exactly one result, so len(results) == attempted count;
        # the remainder are the never-started ones. (Only after a real
        # stop — a completed loop leaves nothing remaining.)
        if cancelled and len(results) < len(tasks):
            for t in tasks[len(results):]:
                results.append((t.label, "stop", 0, None))

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
        # B5: skipped tasks (nothing to do on this machine) get their own
        # honest count instead of being lumped into 'succeeded'.
        summary_core = f"{verb} {'stopped early' if cancelled else 'complete'}: {completed} succeeded"
        if skipped_n:
            summary_core += f", {skipped_n} skipped (nothing to change)"
        summary_core += f", {failed} failed."
        summary_lines = [summary_core]
        if crashed is not None:
            # The summary must not read as a clean completion.
            summary_lines.append(
                f"The run ended early: {crashed!r}. See the log for the full "
                "error; any tasks that had already run were NOT rolled back."
            )
        if cancelled:
            summary_lines.append("Remaining tasks were skipped (cancelled by user).")
        if total_bytes > 0:
            summary_lines.append(f"Disk space freed: {format_bytes(total_bytes)}")
        if needs_reboot:
            summary_lines.append("Reboot system for changes to take effect.")
        summary = "\n".join(summary_lines)
        self.log("=" * 48)
        self.log(summary)
        if needs_reboot:
            self.log("⚠️ Reboot system for changes to take effect.")
        self.log("=" * 48)

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

        self._release_run(ok=not cancelled)
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
                    toast_kind, toast_extra, total_bytes, completed, failed,
                    skipped_n, cancelled)
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
                                     args=(total_bytes, completed, failed, skipped_n),
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
            if mode == "run" and total_bytes and total_bytes > 0:
                try:
                    from app.config_persist import record_run
                    record_run(total_bytes)
                except Exception:
                    pass
            if quiet:
                try:
                    if tab_name == "Clean":
                        self.set_status(
                            f"Freed {format_bytes(total_bytes)}." if total_bytes
                            else "Nothing to clean.")
                    else:
                        verb = "restored" if mode == "revert" else "applied"
                        self.set_status(f"Auto-Pilot: session tweaks {verb}.")
                except Exception:
                    pass
                return
            try:
                after = self._query_drive_free_total(_system_drive_root())
            except Exception:
                after = None
            try:
                after_drives = _snapshot_all_drives(
                    self._query_drives, self._query_drive_free_total)
            except Exception:
                after_drives = {}
            run_again = None
            if (not cancelled and tab_name == "Clean" and mode == "run"):
                # 'Clean Again' — re-run the same selection. The task list
                # is captured by closure; in preset mode the page still
                # holds the same preset, in custom the same toggles.
                same_tasks = list(tasks)
                def run_again():
                    self.run_tasks(tab_name, same_tasks, mode="run")
            try:
                card = ScorecardDialog(
                    self.root,
                    tab_name=tab_name, mode=mode, cancelled=cancelled,
                    # H10: `results` is this run's own list (closed over),
                    # not self._run_results, which a newer run may have
                    # rebound by the time this hop runs.
                    results=list(results),
                    total_bytes=total_bytes,
                    before=run_before_bytes,
                    after=after,
                    needs_reboot=needs_reboot,
                    on_run_again=run_again,
                    before_drives=run_before_drives,
                    after_drives=after_drives,
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
                    if cancelled:
                        _themed_showinfo(self.root, "Stopped", summary,
                                         accent=COLORS["accent_yellow"])
                    else:
                        _themed_showinfo(self.root, "Done", summary,
                                         accent=COLORS["accent_green"])
                except Exception:
                    pass
        # Routed through the dispatcher rather than root.after(0) called from
        # this worker thread: a cross-thread .after() can be dropped outright,
        # and this is the one hop whose loss means the user never sees the
        # result card. The dispatcher also stops cleanly on a destroyed root,
        # which is what the old H6 TclError guard was there for.
        self._dispatch.post(_done_popup)
