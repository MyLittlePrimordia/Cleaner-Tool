"""Application: the window shell and tab routing.

Extracted from app/gui.py (H14). This is the composition root — it is the
one module allowed to know about every tab and every dialog, because it
is what opens them. Everything below it in app/ui knows only about the
layer beneath itself.

Two subsystems that had grown inside it now live next door (audit 2026-10,
R-001) and are composed back in as bases, so every call site kept its name:

    app/ui/runflow.py       the run engine: claim, pre-flight, workers,
                            summary, release (RunFlow)
    app/ui/traycontrols.py  the resident tray menu (TrayControls)
    app/ui/logsurface.py    the log buffer, status line and progress bar
                            (LogSurface)

Why bases rather than objects the window owns: both halves reach for
`self.tabs`, `self.root`, `self._dispatch` and the drive queries on almost
every line, so passing those in would have moved the coupling without
removing it. The split buys one file per concern, not a new abstraction.

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
from app.config import (APP_NAME, WINDOW_SIZE, resolve_window_minimum,
                        resolve_window_size)
from app.config_persist import get_tweak_state
from app.elevation import is_admin, relaunch_as_admin
from app.runner import CancellationToken, RunState
from app.ui.session import SessionOrchestrator, SessionState
from app.ui.tkdispatch import TkDispatcher
from app.tab_presets import PRESETS, TABS, TAB_NAMES
from app.toast import show_toast
from app import watch


from app.ui.logsurface import LogSurface
from app.ui.runflow import (  # noqa: F401 - re-exported for tests/facade
    RunFlow, _DONE_COPY, _install_done_copy, _install_start_copy,
    _install_task_status, _run_header_line, _run_outcome_word,
    _run_summary_lines,
)
from app.ui.traycontrols import TrayControls


#: The names this module exposes besides `Application`. The run-flow copy
#: helpers and `_log_block` moved to their own modules (R-001) but are still
#: imported FROM HERE by the facade and by tests, so they stay listed: that is
#: the contract, not an accident. `__all__` also tells pyflakes these imports
#: are deliberate re-exports rather than dead ones.
__all__ = [
    "Application", "RunFlow", "TrayControls", "_DONE_COPY", "_log_block",
    "_install_done_copy", "_install_start_copy", "_install_task_status",
    "_run_header_line", "_run_outcome_word", "_run_summary_lines",
]

from app.ui.base import (ThemedModal, Tooltip, _themed_askyesno,
                         _themed_showinfo, _themed_showerror,
                         _themed_showwarning)
from app.ui.dialogs.registry import open_dialog
from app.ui.theme import (
    COLORS, ICON_CHIP, ICON_CONTROL, ICON_NAV, PAGE_GUTTER, SPACE_1, SPACE_2,
    SPACE_3, SPACE_4, TAB_ACCENTS, TYPE, _TAB_STATUS, _hex_lerp,
    _round_rect_points, icon_target_px, scaled_photo, ui_scale,
)
from app.ui.metrics import (
    CONTROL_GAP, GROUP_GAP, HAIRLINE_NUDGE, MEASURE_CAP, TEXT_NUDGE, TIGHT_GAP,
)
from app.ui.widgets import (AnimatedButton, ProgressRow,
                            ScrollableRoundedPanel, StatusLine)
from app.ui.widgets.dropdown import M3Dropdown
from app.ui.widgets.chrome import BorderlessChrome, _enable_dark_titlebar, _set_window_icon
from app.runner.task_runner import log_block as _log_block
from app.utils import IS_WINDOWS, format_bytes
from app.ui.tabs.admingateframe import AdminGateFrame
from app.ui.tabs.tasktab import TaskTab
from app.ui.tabs.installtab import InstallTab
from app.ui.tabs.toolstab import ToolsTab
from app.ui.tabs.healthtab import HealthTab

#: The one project this app checks for updates. Used both to query the
#: release API and to validate the download link that comes back from it
#: (MED-012) - if those ever disagree, the link check stops matching.
#: Defined in app.self_update (the module that also verifies and applies the
#: download) so there is a single source of truth for the slug.
from app.self_update import UPDATE_REPO


# DESIGN AUDIT (2026-10): _TAB_STATUS now lives in app/ui/theme.py, next to
# TAB_ACCENTS, and is re-exported here. It was defined twice -- once here and
# once in theme.py, byte-identical and neither reading the other -- and it now
# also has a second consumer (TaskTab's Tweak status line), which is exactly
# the situation two copies drift in. theme.py is the bottom of the UI layering,
# so both the shell (this file, re-exported for app/gui.py) and the tab pages
# can read one table. Do NOT re-define it here: this import is the binding.

#: Displayed size of the per-tab icon that sits inline with the pill
#: switcher. DESIGN AUDIT (2026-10): was 72px in a band of its own, which
#: cost ~86px of vertical chrome for a decoration that duplicated the tab
#: name immediately below it. One constant, because the whole point is that
#: every tab's icon displays at the SAME size - a per-tab size difference
#: changes this row's height and shoves the switcher up or down, which was
#: the original "few px jump" bug. Sourced in _build_main_ui.
#:
#: 36 is exactly 72/2, which PhotoImage.subsample can reach with an integer
#: factor. The app is intentionally stdlib-only at runtime (no PIL - see
#: requirements.txt), so integer subsampling is the only scaler available and
#: the constant is chosen to be reachable by it.
TAB_ICON_PX = ICON_NAV


#: Horizontal breathing room between neighbours in the bottom status row
#: (privilege label, update chip, the three corner icons). DESIGN AUDIT
#: (2026-10): the status row went from three widgets to eight in one pass,
#: and without a single shared inset each new widget would bring its own
#: ad-hoc padx and the row would read as unrelated clumps.
GAP_X = SPACE_3

#: Emoji fallbacks for the status-row shortcut glyphs (redesign 2026-10).
#: Used ONLY when the PNG is missing or will not decode, so a packaging
#: mistake degrades to the previous look instead of a blank button.
_CORNER_FALLBACK = {"automaint": "🛠️", "exports": "📋"}

#: Displayed size of a status-row glyph. The preset cards use 24; these are a
#: little smaller because they sit in a dense strip beside the privilege
#: label and the drive chips, and 24px there competes with the drive readout.
STATUS_ICON_PX = ICON_CHIP


class Application(LogSurface, RunFlow, TrayControls):
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title(APP_NAME)
        # Derived from the work area, not hardcoded (app/config.py explains
        # why). Falls back to WINDOW_SIZE when the work area is unreadable.
        self._window_size = resolve_window_size(self.root, WINDOW_SIZE)
        self.root.geometry(self._window_size)
        # DPI-SCALED (2026-10 visual pass): geometry and minsize are in
        # physical pixels, fonts are not. resolve_window_size scales the
        # former; this scales the floor with it, capped at the window that
        # actually resolved (a 200% display's work area can be shorter than
        # the scaled 700px minimum).
        self.root.minsize(*resolve_window_minimum(self.root,
                                                  self._window_size))
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self.root.configure(bg=COLORS["shell"])
        _set_window_icon(self.root)
        _enable_dark_titlebar(self.root)
        # Local crash log: Tk swallows callback exceptions before they reach
        # sys.excepthook, so the root needs its own hook. Best-effort.
        try:
            from app.diagnostics import install_hooks as _install_crash_hooks
            _install_crash_hooks(self.root)
        except Exception:
            pass
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
        # N-006: one quiet-watch tick at a time. Not a lock: a tick that finds
        # the previous one still reading the Task Scheduler is simply skipped,
        # and the next one is thirty seconds away.
        self._watch_running = False
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
        # _pilot* names below are thin forwarders, kept so callers that
        # drove the watcher through them keep working.
        self._pilot_sess = SessionOrchestrator(
            root=self.root,
            on_apply=self._pilot_apply,
            on_revert=self._pilot_revert,
            log=self.log)
        self._pilot_dialog = None

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
                        # REDESIGN 2026-10: the 🔓 is gone from this string
                        # too. It was not merely redundant on screen - it
                        # pointed at a badge that no longer exists, so the
                        # instruction could no longer be followed.
                        self.set_status("Auto-elevate is on but needs one more "
                                        "approval (click the Limited badge).")
                    except Exception:
                        pass
        # single-instance focus handoff (tray/auto-elevate foundation): a
        # second launch pulses our show-event instead of opening a twin —
        # surface here when it arrives. Best-effort, self-rescheduling.
        self._start_single_instance_poll()

    def _start_single_instance_poll(self):
        """Begin the show-event watcher (see app/single_instance). A second
        launch focuses this window instead of opening a twin. The wait runs
        on a daemon thread, so the UI thread pays NOTHING for it (the old
        `after(500, ...)` chain woke Tk twice a second forever); the pulse
        is posted back through TkDispatcher, never delivered cross-thread."""
        try:
            from app import single_instance as _si
            if not _si.poll_enabled():
                return
        except Exception:
            return
        self._si_stop = threading.Event()
        try:
            th = threading.Thread(target=self._si_watch, daemon=True,
                                  name="SingleInstanceWatch")
            th.start()
        except Exception:
            pass

    def _si_watch(self):
        """Worker thread: block on the show-event, post the focus to Tk."""
        try:
            from app import single_instance as _si
            _si.watch_show_event(
                lambda: self._dispatch.post(self._si_focus),
                self._si_stop)
        except Exception:
            pass

    def _si_focus(self):
        """Tk thread: surface the window for a second launch."""
        for _fn in (self.root.deiconify, self.root.lift,
                    self.root.focus_force):
            try:
                _fn()
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
            self._tray_last = icon      # kept so a failure can say WHY
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
                    _why = getattr(getattr(self, "_tray_last", None),
                                   "last_error", "") or "unknown stage"
                    log_security_event("tray", "tray icon creation failed (%s);"
                                       " keeping window open instead of "
                                       "hiding" % _why)
                except Exception:
                    pass
                try:
                    _themed_showinfo(
                        self.root,
                        "System Tray Unavailable",
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
                "Stop the Task and Close?",
                "A task is still running. Closing now may leave a Windows "
                "service stopped until you restart it manually.",
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
        # stop the single-instance watcher's blocking wait: it is a daemon
        # thread, but leaving it holding a kernel wait object through
        # interpreter teardown is pointless work at exit
        try:
            self._si_stop.set()
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

    # NOTE (2026-10): `_build_style` lived here and configured ttk's
    # TCombobox/TProgressbar. Both widgets are gone (every picker is
    # M3Dropdown, every progress bar is AnimatedProgressBar), so the method
    # was styling controls no window contains. It is removed rather than
    # left as dead theming: a ttk style in the shell is also the easiest way
    # for the next native widget to look "close enough" to ship. The design
    # guard (dev/tests/test_design_guard.py) now rejects ttk outright.

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
        # the top TITLE_H strip for the custom title bar and the chrome's
        # edge band (BorderlessChrome.PAD) for the resize grips. The host
        # used to sit flush against the window edges, so the outer 6px of
        # every page was covered by a grip frame — a click there started a
        # resize instead of hitting the control under it (audit B11).
        # BorderlessChrome.content_box() is the same arithmetic, and the
        # chrome tests compare the two.
        host = self._content_host = tk.Frame(self.root, bg=COLORS["shell"])
        BorderlessChrome.place_content(host)
        _edge = BorderlessChrome.PAD

        # DESIGN AUDIT (2026-10) — the toolbar band is GONE.
        #
        # It used to be a full-width strip whose only permanent contents were
        # a right-aligned privilege label and a hidden update banner: ~26px of
        # chrome (plus its own 8px top pad) spent on one word, on every launch,
        # forever. Both of its two widgets now live where they belong:
        #
        #   * the privilege label  -> the status row (stat_row below), beside
        #     the other live app state. It IS status: "what can this app do
        #     right now". It was a toolbar icon with no toolbar around it.
        #   * the update banner   -> the status row too. A one-line notice
        #     about a newer build is transient information, and the status
        #     line is where transient information already lives. Packing it
        #     at the top of the window made a non-blocking notification look
        #     like a modal alert.
        #
        # Nothing is lost: _refresh_mode_label() and _show_update_banner()
        # are unchanged in behaviour, only in which parent they pack into.
        # Both widgets are created further down, inside the status row.
        # ----------------------------------------------------------------

        # Pill switcher + its inline per-tab icon (DESIGN AUDIT 2026-10).
        #
        # The icon was a 72px band of its own ABOVE the switcher - ~86px with
        # padding, the single largest chrome consumer in the window, for a
        # decoration that duplicates the tab name sitting directly beneath it.
        # It is now a TAB_ICON_PX mark in the switcher's own centered row,
        # which reclaims ~50px of page height and reads as one navigation unit
        # instead of two. See _build_tab_switch and _load_tab_icons.
        #
        # active_tab must exist BEFORE _build_tab_switch runs: it reads
        # TAB_NAMES.index(self.active_tab) to place the thumb on first paint.
        self.active_tab = "Clean"
        self._build_tab_switch()

        # Tab pages (Install gets the catalog browser, Tools the embedded
        # power features, others the preset UI)
        # M3 layout: the navigation rail (built above, packed left) is one
        # column; everything else lives in a main column to its right: a top
        # app bar carrying the destination title, then the page, then status.
        main = self._main_col = tk.Frame(host, bg=COLORS["shell"])
        main.pack(side="left", fill="both", expand=True)

        # SHELL GEOMETRY (design audit L-001/L-002).
        #
        # The page face is inset from the shell on BOTH sides now, and every
        # band (app bar, page, status) computes its x from the same three
        # numbers: the panel's outer inset, the panel's corner inset, and the
        # page gutter. Before this, the app bar started at 8px while the page
        # started at 32px, the status row at 24px, and the page was 6px wider
        # on the left than the right - three left edges and an off-centre
        # page, which is the single loudest "unfinished" signal in the app.
        _panel_inset = max(0, 12 - _edge)
        _p_r, _p_in = 24, 8
        _page_x = _panel_inset + _p_in + PAGE_GUTTER

        # Top app bar: destination title + one-line description, on the shell.
        # Sized by its content: a fixed 100px band clipped at high DPI and
        # spent ~40% of its height on nothing at 100%.
        appbar = self._appbar = tk.Frame(main, bg=COLORS["shell"])
        appbar.pack(fill="x")
        titles = tk.Frame(appbar, bg=COLORS["shell"])
        titles.pack(side="left", padx=(_page_x, 0), pady=(GROUP_GAP, GROUP_GAP), anchor="n")
        # The one screen title in the app. Stage 3 promoted it from 18pt
        # regular to the semantic screen_title (18pt bold): the largest label
        # on a page was the only heading that was not bold, so it read as a
        # caption above the page rather than as its title.
        self._appbar_title = tk.Label(
            titles, text=self.active_tab, font=TYPE["screen_title"],
            bg=COLORS["shell"], fg=COLORS["text"], anchor="w")
        self._appbar_title.pack(anchor="w")
        self._appbar_sub = tk.Label(
            titles, text=self._TAB_SUBTITLES.get(self.active_tab, ""),
            font=TYPE["body_medium"], bg=COLORS["shell"], fg=COLORS["subtext"],
            anchor="w")
        self._appbar_sub.pack(anchor="w")
        # The page lives in a rounded panel set into the shell (the Gmail /
        # Drive / Gemini layout). The inner frame is inset by the corner-arc
        # depth so its square corners never poke out of the rounded face.
        panel = self._panel = tk.Canvas(main, bg=COLORS["shell"],
                                        highlightthickness=0, bd=0)
        # symmetric inset: a page panel inset on one side only is what made
        # the page column 6px off-centre before this.
        panel.pack(fill="both", expand=True,
                   padx=(_panel_inset, _panel_inset),
                   pady=(0, _panel_inset))
        body = self._panel_body = tk.Frame(panel, bg=COLORS["bg"])
        _p_win = panel.create_window(_p_in, _p_in, window=body, anchor="nw")

        def _panel_cfg(e, _c=panel, _w=_p_win, _r=_p_r, _i=_p_in):
            if e.width < 8 or e.height < 8:
                return
            _c.delete("face")
            _c.create_polygon(
                _round_rect_points(0, 0, e.width - 1, e.height - 1, _r),
                smooth=True, fill=COLORS["bg"], outline="", tags="face")
            _c.tag_lower("face")
            _c.itemconfigure(_w, width=max(1, e.width - 2 * _i),
                             height=max(1, e.height - 2 * _i))
        panel.bind("<Configure>", _panel_cfg)
        self.page_container = tk.Frame(body, bg=COLORS["bg"])
        self.page_container.pack(fill="both", expand=True, padx=(0, 0),
                                 pady=(0, SPACE_1))
        self.tabs = {}
        # PERF (2026-10, AUDIT TL-013): a page is built on FIRST VISIT unless it
        # is the one the app opens on. Settings was the first (its constructor
        # runs three schtasks probes), and Tools/Drivers follow for the same
        # reason: Tools builds 19 compact rows plus 19 icon loads for a page most
        # sessions never open, and Drivers walks the driver folders. Building
        # those before the first paint is why the window took seconds to appear.
        # _ensure_tab materializes a lazy page exactly once; everything else
        # keeps using self.tabs as before. Install stays eager because its
        # catalog is deliberately built in idle slices (and its badge/warm-scan
        # state is set up while the page exists).
        self._tab_factories = {}
        for name in TAB_NAMES:
            if name == "Install":
                self.tabs[name] = InstallTab(self.page_container, self)
            elif name == "Health":
                self.tabs[name] = HealthTab(self.page_container, self)
            elif name == "Tools":
                self._tab_factories[name] = (
                    lambda _cls=ToolsTab:
                    _cls(self.page_container, self))
            elif name == "Drivers":
                from app.ui.tabs.driverstab import DriversTab
                self._tab_factories[name] = (
                    lambda _cls=DriversTab:
                    _cls(self.page_container, self))
            elif name == "Settings":
                from app.ui.tabs.settingstab import SettingsTab
                self._tab_factories[name] = (
                    lambda _cls=SettingsTab:
                    _cls(self.page_container, self))
            else:
                self.tabs[name] = TaskTab(self.page_container, self, name)
        self._show_tab("Clean")

        # Bottom: status line + details disclosure + progress + disk.
        # It lives inside the panel body, so PAGE_GUTTER puts it on the exact
        # same column as the page above it (L-001).
        bottom = tk.Frame(body, bg=COLORS["bg"])
        bottom.pack(fill="x", padx=PAGE_GUTTER, pady=(TEXT_NUDGE, SPACE_2))

        stat_row = tk.Frame(bottom, bg=COLORS["bg"])
        stat_row.pack(fill="x")
        # The status sentence gets its own expanding slot and never shares a
        # line with the notification badge again. A plain Label clipped long
        # DISM/SFC lines mid-word; StatusLine centres them while they fit and
        # pans them when they do not (user request: no clipping, no marquee
        # gymnastics, centred rather than left-aligned).
        self._status_wrap = tk.Frame(stat_row, bg=COLORS["bg"])
        self._status_wrap.pack(side="left", fill="x", expand=True)
        # 30px, not 26: the Stop button that slots into this row during a
        # run is 32px tall including its pady, so a shorter status line made
        # the row grow by 4px on every run start and shove the page above it.
        # The strip's whole reason for existing is that nothing above it
        # moves, so its resting height already includes the run-time control.
        self.status_lbl = StatusLine(self._status_wrap, font=TYPE["label_large"],
                                     bg=COLORS["bg"], fg=COLORS["subtext"],
                                     height=30)
        self.status_lbl.pack(fill="x", expand=True, pady=(HAIRLINE_NUDGE, HAIRLINE_NUDGE))
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
        self._nudge_frame.pack(side="right", padx=(SPACE_2, 0))
        self._nudge_chips: dict = {}      # letter -> {"frame", "label", "tip"}
        self._drive_was_red: dict = {}    # letter -> bool (hysteresis state)
        self._nudge_dismissed: set = set()  # session dismissals (respected)
        self._nudge_estimate = None       # (bytes, epoch) or None
        self._nudge_scan_running = False
        self._nudge_scan_token = [False]  # cancel token (app close / phase-6)
        self._last_drives: list = []      # raw monitor list (tooltip totals)

        # Audit fix (Limited-mode clarity): this used to be a plain, inert
        # label — the user had no way to get to Administrator rights again
        # short of quitting and relaunching by hand. Clicking it in Limited
        # mode now offers to restart elevated, same UAC flow AdminGateFrame
        # already uses, just reachable after that first screen is gone.
        #
        # DESIGN AUDIT (2026-10): it lived in a top toolbar strip that held
        # nothing else, so it cost a whole band of chrome to say one word.
        # It is app state ("what can this process do right now"), which is
        # exactly what the status row is for, so it moves here — packed
        # side="right" BEFORE the drive chips above, so Tk's last-packed-
        # furthest-left rule puts it to the left of them and it can never
        # be pushed off the row by a newly discovered drive.
        self._mode_lbl = tk.Label(stat_row, text="", font=TYPE["caption"],
                                  bg=COLORS["bg"], fg=COLORS["subtext"])
        self._mode_lbl.pack(side="right", padx=(0, GAP_X))
        self._refresh_mode_label()

        # PC Health badge (2026-10): the always-visible half of PC Health.
        # One word - Great / Good / Fair / Bad, or "Check PC" before the
        # first check - and a click opens the check (which scans at once).
        # It re-checks by itself on the schedule from Settings; that check
        # is read-only (see app/health_badge.py).
        self._health_lbl = tk.Label(stat_row, text="", font=TYPE["label_medium"],
                                    bg=COLORS["bg"], fg=COLORS["subtext"],
                                    cursor="hand2")
        self._health_lbl.pack(side="right", padx=(0, GAP_X))
        self._health_lbl.bind("<Button-1>", lambda e: self._open_health_report())
        self._health_tip = None
        self._health_checking = False
        self._refresh_health_badge()

        # Update notification banner (hidden by default, shown when an update
        # is available). Also moved out of the deleted toolbar strip, for the
        # same reason: it is transient information, and a notification that
        # appears at the very top of the window reads as a blocking alert
        # rather than as "there is a newer build, if you want it".
        #
        # REDESIGN 2026-10: the 🔄 emoji became the generated `update` asset,
        # and the banner is now [icon][label] side by side rather than one
        # Label carrying an emoji inside its text - an emoji inside a string
        # cannot be themed or sized independently of the label's font.
        # DECLUTTER 2026-10: a solid yellow block in the status row was the
        # loudest thing in the window for a "there is a newer build". It is
        # now a quiet yellow-on-black label like its neighbours.
        #
        # MOVED AGAIN (user feedback 2026-10): packed beside the live status
        # sentence it crowded the one place a DISM/SFC run needs room. The
        # app bar has been empty to the right of the tab title since launch;
        # a "there is a newer build" notice belongs there, quiet and out of
        # the run's way.
        self._appbar_trailing = tk.Frame(self._appbar, bg=COLORS["shell"])
        self._appbar_trailing.pack(side="right", padx=(0, _page_x))
        # The update notice is created below but stays hidden until needed.
        self._update_banner_frame = tk.Frame(self._appbar_trailing,
                                             bg=COLORS["shell"])
        _upd_img = self._status_icon("update")
        if _upd_img is not None:
            self._update_banner_icon = tk.Label(
                self._update_banner_frame, image=_upd_img,
                bg=COLORS["shell"], bd=0, highlightthickness=0)
            self._update_banner_icon.pack(side="left", padx=(SPACE_2, 0), pady=SPACE_1)
            _upd_text = "Update available"
        else:
            self._update_banner_icon = None
            _upd_text = "🔄 Update available"
        self._update_banner_lbl = tk.Label(
            self._update_banner_frame,
            text=_upd_text,
            font=TYPE["label_medium"],
            bg=COLORS["shell"],
            fg=COLORS["accent_yellow"],
            cursor="hand2", bd=0)
        self._update_banner_lbl.pack(side="left", padx=(CONTROL_GAP, SPACE_2), pady=SPACE_1)
        self._update_banner_lbl.bind("<Button-1>", lambda e: self._show_update_dialog())
        self._update_banner_frame.pack_forget()  # Hidden by default

        # progress strip — the row is ALWAYS packed and its height is
        # reserved, so starting a run no longer grows `bottom` and shoves
        # the page above it. Only the bar inside it appears/disappears (a
        # blank strip is quieter than a permanent empty track).
        self.progress_row = ProgressRow(bottom, accent=COLORS["accent_green"])
        self.progress_row.pack(fill="x", pady=(CONTROL_GAP, TEXT_NUDGE))
        self.progress_bar = self.progress_row.bar
        self._progress_hidden = True

        # Stop button — slots into the status row while a run is active
        self.cancel_btn = AnimatedButton(
            stat_row, text="✕ Stop", command=self._request_cancel,
            role="danger", size="sm",
        )
        self.cancel_btn.set_enabled(False)

        # Bottom-corner shortcuts: Auto Maintenance and Export
        # Logs. Startup Manager and Uninstall Programs moved to Tools tab
        # cards (user request, 2026-09) — the Tools grid had exactly two
        # empty slots on its last row, and both dialogs already existed
        # as ThemedModal popups launched the same way every other Tools
        # card is, so this is a straight relocation, not new plumbing.
        #
        # DESIGN AUDIT (2026-10): these were a full-width band of their own
        # below the status row, three `uniform` columns across the entire
        # window so three 16px glyphs could sit as far apart as the window is
        # wide. They were the least dense, least purposeful row in the app:
        # ~30px of height, spread edge to edge, to show three icons.
        #
        # They are now packed side="right" into the status row's own icon
        # cluster, left of the privilege label, at the same weight as the
        # drive chips beside them. Three related actions, one control strip,
        # one band of height instead of two.
        #
        # REDESIGN 2026-10 (second pass): the glyphs were 🛠️ 🧰 📋 - full
        # colour system emoji, and because they live in the STATUS ROW they
        # were on screen in EVERY tab, all the time. That is the most-watched
        # strip in the window, and it was the last place in the app where a
        # control ignored the palette: Tk renders emoji from the system font,
        # so no amount of theming reaches them.
        #
        # They keep their tooltips and click bindings; only the glyph changed,
        # to themed assets from the same generated set the preset cards use
        # (app/assets/preset_icons/, drawn in `subtext` because a global
        # control must not wear whichever tab's accent happens to be open).
        #
        # 2026-10: Quick Tools (the third of them) is gone - its card was
        # dropped from the Tools tab and this icon had never been packed, so
        # the widget went with the feature instead of lingering as a hook
        # nothing could reach.
        #
        # Still a tk.Label, not an AnimatedButton: dev/tools/smoke/tools.py
        # asserts on `_corner_logs.cget("text")` and drives these through
        # `_corner_cmd`, so the widget type and that hook both have to
        # survive the change.
        def _corner_icon(icon_key, tip, command):
            img = self._status_icon(icon_key)
            if img is not None:
                lbl = tk.Label(stat_row, image=img, bg=COLORS["bg"],
                               cursor="hand2", bd=0, highlightthickness=0)
            else:
                lbl = tk.Label(stat_row,
                               text=_CORNER_FALLBACK.get(icon_key, "•"),
                               font=TYPE["body_xlarge"], bg=COLORS["bg"],
                               fg=COLORS["subtext"],
                               cursor="hand2", bd=0, highlightthickness=0)
            # Built but NOT packed (DECLUTTER 2026-10): the "..." menu below
            # is the visible control; these stay as the actions' single
            # source of truth and as the smoke test's hooks.
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

        # Built in reading order.
        self._corner_maint = _corner_icon(
            "automaint", "Auto Maintenance",
            lambda: self._show_schedule_dialog())
        self._corner_logs = _corner_icon(
            "exports", "Export Logs", lambda: self.export_logs())
        # DECLUTTER 2026-10 (second pass): both of these actions now live in
        # the Settings tab, so the strip carries no menu at all. The widgets
        # are still BUILT - never packed - only because dev/tools/smoke/tools.py
        # drives their click handlers; they are not a user-facing control.

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

        # U-006: the Quarantine bin's retention window, applied here and not
        # when Settings saves it - shortening the window must not delete a
        # file the instant the dropdown changes. Runs off the Tk thread
        # because it stats and removes files; log() is thread-safe.
        try:
            from app import quarantine as _quarantine
            _keep_days = _quarantine.retention_days()
            if _keep_days:
                def _expire_bin(days=_keep_days):
                    try:
                        count, freed = _quarantine.expire(days=days)
                    except Exception:
                        return
                    if count:
                        from app.utils import format_bytes
                        self.log("  Quarantine: cleared %d file(s) older than "
                                 "%d days (%s)." % (count, days,
                                                    format_bytes(freed)))
                threading.Thread(target=_expire_bin, daemon=True,
                                 name="quarantine-expire").start()
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

        # DESIGN AUDIT (2026-10) — TEXT-004. This used to be a hardcoded
        # "Ready. Pick a preset and press Run." set here at the end of the
        # build, immediately AFTER _show_tab("Clean") had already put the
        # per-tab copy in the bar. So the one place the string was supposed
        # to be the single source of truth was also the one place it
        # overwrote the single source of truth — and it told the user to
        # "pick a preset and press Run" on the Install and Tools tabs, which
        # have neither a preset card nor a Run button.
        #
        # It now reads from _TAB_STATUS like every other tab switch does, so
        # there is exactly one table and one rule. _show_tab has already run
        # for "Clean" and set the same value, so this is now a no-op in the
        # happy path; it is kept as an explicit initializer for the rebuild
        # path (where _show_tab is not called) rather than left to chance.
        self.set_status(_TAB_STATUS.get(self.active_tab, ""))

        # Read the real scheduler state back into config in the background.
        # Settings used to do this synchronously in its constructor.
        self._resync_scheduler_flags_async()

        # Check for updates in the background (non-blocking)
        self._schedule_update_check()
        self._schedule_health_check()

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

    # ---------------- PC Health badge ---------------- #

    def _refresh_health_badge(self, checking=False):
        """Repaint the badge from the saved result (or show 'Checking...')."""
        from app import health_badge as hb
        lbl = getattr(self, "_health_lbl", None)
        if lbl is None:
            return
        try:
            if checking:
                lbl.config(text="PC: checking...", fg=COLORS["subtext"])
                return
            _mode, grade, ts = hb.load_state()
            word, ckey = hb.badge_for(grade)
            lbl.config(text=("PC: " + word) if grade else word,
                       fg=COLORS.get(ckey, COLORS["subtext"]))
            tip = ("PC health: %s (checked %s). Click to open Health."
                   % (word, hb.age_text(ts))) if grade else \
                "Click to open Health and run a check"
            if self._health_tip is None:
                self._health_tip = Tooltip(lbl, tip)
            else:
                try:
                    self._health_tip.text = tip
                except Exception:
                    pass
        except Exception:
            pass

    def _schedule_health_check(self):
        """Once, a few seconds after launch (after the update check), run the
        read-only health check if the user's schedule says it is due."""
        try:
            self.root.after(4500, self._maybe_auto_health_check)
        except Exception:
            pass

    def _maybe_auto_health_check(self):
        from app import health_badge as hb
        import os
        # Test harnesses drive a real Application over the user's real
        # config; a scheduled check would write health_last_* into it.
        if os.environ.get("CLEANER_TOOL_NO_AUTO_HEALTH"):
            return
        mode, _grade, ts = hb.load_state()
        if not hb.is_due(mode, ts, at_startup=True):
            return
        if self._health_checking or getattr(self, "_busy", False):
            return
        self._health_checking = True
        self._refresh_health_badge(checking=True)

        def _worker():
            try:
                hb.quick_check()
            except Exception:
                pass
            try:
                self._dispatch.post(self._health_check_done)
            except Exception:
                pass

        threading.Thread(target=_worker, daemon=True).start()

    def _health_check_done(self):
        self._health_checking = False
        self._refresh_health_badge()

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
                # U-006: a build that was downloaded but not yet applied must
                # surface even when the network check finds nothing (offline,
                # or the rolling release already matches), so the user can
                # finish the update they started.
                staged = False
                try:
                    from app import self_update
                    staged = bool(self_update.staged_ready())
                except Exception:
                    staged = False
                result['staged'] = staged
                if result.get('update_available') or staged:
                    # Hand straight to the dispatcher. It owns the dead-root
                    # check (see TkDispatcher._pump) and it does it on the Tk
                    # thread.
                    #
                    # This used to guard with `if self.root.winfo_exists():`
                    # right here -- a Tcl call from the worker thread, under
                    # a comment claiming it was the main thread. It was the
                    # last off-thread Tk call in the file, and it contradicted
                    # three other comments in this same module plus
                    # tkdispatch.py, all of which say worker threads must not
                    # touch Tk at all: a foreign Tk call waits on the
                    # interpreter and then raises, and on a destroyed root it
                    # burns about a second first.
                    try:
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
                self._update_banner_frame.pack(side="right")
                self._update_info = update_info
        except Exception:
            pass
    
    def _show_update_dialog(self):
        """Open the app's own update dialog (U-006).

        This used to be a messagebox that could only hand the download URL to
        the browser. app/self_update.py can now fetch+verify+apply the build,
        so the choice lives in a real dialog. See app/ui/dialogs/registry.py.
        """
        open_dialog(self, "app_update")

    # ---------------- scheduler flag resync (off the UI thread) ---------- #

    def _resync_scheduler_flags_async(self):
        """Read the real Task Scheduler state back into config, off-thread.

        This used to run inside SettingsTab.__init__ on the Tk thread: three
        `schtasks /Query` calls (85-244 ms measured) before the page could
        paint. It is a status probe, so it belongs on a worker; the Settings
        page refreshes its toggle vars when the result lands (if it is built).
        """
        if getattr(self, "_flags_resync_started", False):
            return
        self._flags_resync_started = True

        def _work():
            try:
                from app.settings_apply import resync_flags
                resync_flags()
            except Exception:
                pass
            try:
                self._dispatch.post(self._apply_resynced_flags)
            except Exception:
                pass

        try:
            threading.Thread(target=_work, daemon=True,
                             name="SchedulerFlagResync").start()
        except Exception:
            pass

    def _apply_resynced_flags(self):
        """Tk-thread hop: refresh Settings toggles if that page exists."""
        try:
            page = self.tabs.get("Settings")
            if page is not None and hasattr(page, "refresh_scheduler_flags"):
                page.refresh_scheduler_flags()
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
        # Preset summaries are the other body build that used to land on a
        # click. The default preset is already built; the rest are hidden
        # until the user picks them, so build them now, one idle step each.
        for tab_name in ("Clean", "Repair", "Tweak"):
            page = self.tabs.get(tab_name)
            if page is None:
                continue
            for name in getattr(page, "preset_cards", {}):
                if name == "Custom" or name == getattr(
                        page, "_selected_preset", None):
                    continue
                steps.append(
                    lambda p=page, n=name: p._prewarm_body("preset:" + n))
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
        #
        # PERF (2026-10, second pass): this used to pre-map ONLY Install.
        # Every other tab therefore paid its first full geometry pass on
        # the click, which is why the window felt slow on the first visit
        # even after the click path itself was reduced to tkraise(). Map
        # each built page here, underneath, one idle step each; a user's
        # first visit is then a raise like every later visit.
        def _premap_page(name):
            def _do(_name=name):
                try:
                    page = self.tabs.get(_name)
                    if page is None or page.winfo_manager() == "place":
                        return
                    page.place(relx=0, rely=0, relwidth=1, relheight=1)
                    page.lower()          # lay out UNDERNEATH — never raised
                    self.root.update_idletasks()
                except Exception:
                    pass
            return _do
        # Settings is lazy by design (its constructor runs three schtasks
        # probes). Build it during idle too, then premap it, so the one page
        # whose first click still paid a constructor does not stand out from
        # the rest.
        if "Settings" not in self.tabs:
            steps.append(lambda: self._ensure_tab("Settings"))
        for _name in TAB_NAMES:
            if _name in self.tabs or _name == "Settings":
                steps.append(_premap_page(_name))
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
        class left app/gui.py (H14) and silently loaded no icons at all. The
        status-row glyphs and the tool rows are the callers."""
        found = resolve_asset_path(fname)
        return [pathlib.Path(found)] if found else []

    # ---------------- Material 3 navigation rail ---------------- #

    # The seven destinations live in a vertical M3 navigation rail on the left
    # edge: an 88px column at 100% density, each destination an icon over a
    # label, with a secondary-container "active indicator" that grows out of
    # the icon's centre when the destination is selected. Hover paints the 8%
    # state layer inside the same shape. Up/Down (and Left/Right) move between
    # destinations from the keyboard. The column's pixel metrics are derived
    # per display in `_rail_metrics()`: the icon grows with the display, so a
    # fixed pill and a fixed icon-to-label offset crowded the two together
    # (at 150% the 36px mark left ~3px above its 9pt label).
    _TAB_SUBTITLES = {
        "Clean": "Free up space by clearing junk files and caches",
        "Repair": "Fix Windows components and common problems",
        "Tweak": "Tune Windows for speed and gaming",
        "Health": "A plain-English checkup of your whole PC",
        "Install": "Install apps, runtimes and missing components",
        "Drivers": "Keep your hardware drivers up to date",
        "Tools": "Diagnostics and everyday utilities",
        "Settings": "Customize how Cleaner Tool behaves",
    }
    #: The rail's 100% width and top padding. Its other metrics are derived in
    #: `_rail_metrics()` from the icon at the CURRENT display scale, because
    #: the glyphs scale with the display while the old fixed pixel values did
    #: not: at 150% the 24px rail icon renders at 36px, which overflowed the
    #: fixed 32px pill and left about 3px of air above the label.
    RAIL_W = 88
    RAIL_TOP = SPACE_3
    #: Air between a destination's icon pill and its label. The label used to
    #: be placed by its CENTRE 12px under the pill's edge, so the gap the user
    #: actually saw was ~3px and the icon and text read as one crowded block.
    RAIL_LABEL_GAP = SPACE_2
    SWITCH_ANIM_MS = 200
    HOVER_ANIM_MS = 120

    def _rail_metrics(self):
        """(rail_w, item_h, ind_w, ind_h, top, gap) for this display.

        The pill is built AROUND the icon instead of fitting the icon into a
        pixel pill: M3's 24px icon in a 56x32 pill is exactly SPACE_4 either
        side and SPACE_1 above and below, so a scaled icon can never overflow
        its own indicator. The row height then follows the pill, the label gap
        and the label's own line height, which is what keeps an icon evenly
        separated from ITS label at every DPI rather than only at 100%.
        """
        scale = ui_scale(self.root)
        icon = icon_target_px(ICON_CONTROL, self.root)
        ind_w = icon + 2 * SPACE_4
        ind_h = icon + 2 * SPACE_1
        try:
            import tkinter.font as tkfont
            line = int(tkfont.Font(
                font=TYPE["label_medium"]).metrics("linespace"))
        except Exception:
            line = int(round(17 * scale))
        item_h = ind_h + self.RAIL_LABEL_GAP + line + SPACE_4
        rail_w = max(ind_w + 2 * SPACE_4, int(round(self.RAIL_W * scale)))
        return (rail_w, item_h, ind_w, ind_h, self.RAIL_TOP,
                self.RAIL_LABEL_GAP)

    def _build_tab_switch(self):
        """Build the navigation rail (replaces the old top pill switcher)."""
        (rail_w, item_h, ind_w, ind_h, top_pad,
         label_gap) = self._rail_metrics()
        self._rail_w = rail_w
        self._rail_item_h = item_h
        self._rail_ind_w = ind_w
        self._rail_ind_h = ind_h
        self._rail_top = top_pad
        self._rail_gap = label_gap
        rail = tk.Frame(self._content_host, bg=COLORS["shell"], width=rail_w)
        rail.pack(side="left", fill="y")
        rail.pack_propagate(False)
        self._rail_frame = rail

        # Kept for icon-set contracts (never packed).
        self._tab_icon_lbl = tk.Label(rail, bg=COLORS["shell"],
                                      bd=0, highlightthickness=0)
        self._load_tab_icons()
        self._update_tab_icon()

        n_tabs = len(TAB_NAMES)
        self._switch_labels = TAB_NAMES
        self._switch_h = top_pad + n_tabs * item_h
        self._switch_w = rail_w
        self._switch_pos = float(TAB_NAMES.index(self.active_tab))
        self._switch_anim = None
        self._switch_anim_after = None
        self._hover_seg = None
        self._hover_t = 0.0
        self._hover_anim = None
        self._hover_anim_after = None
        self._rail_t = [1.0 if nm == self.active_tab else 0.0
                        for nm in TAB_NAMES]
        self._pill_items = []      # (indicator_id, icon_id, text_id, y0, y1)
        self._switch_texts = []
        self._switch_icon_items = []
        self._hover_bgs = []
        #: True while the rail owns keyboard focus. Painted as a 1px focus
        #: ring on the active destination, the same cue a focused row uses.
        self._kb_focus = False

        self.switch = tk.Canvas(rail, width=rail_w,
                                height=self._switch_h, highlightthickness=0,
                                bd=0, bg=COLORS["shell"])
        self.switch.pack(fill="both", expand=True)
        self.switch.bind("<Button-1>", self._on_switch_click)
        self.switch.bind("<Motion>", self._on_switch_motion)
        self.switch.bind("<Leave>", self._on_switch_leave)
        self.switch.configure(takefocus=1, highlightthickness=0)

        def _step(delta):
            i = TAB_NAMES.index(self.active_tab) + delta
            i = max(0, min(len(TAB_NAMES) - 1, i))
            self._switch_to(TAB_NAMES[i])
        self.switch.bind("<Up>", lambda e: _step(-1))
        self.switch.bind("<Left>", lambda e: _step(-1))
        self.switch.bind("<Down>", lambda e: _step(1))
        self.switch.bind("<Right>", lambda e: _step(1))
        # Return/Space ACTIVATE the destination the rail is on. They used to
        # be bound to `lambda e: None`: a dead binding, so the two keys every
        # keyboard user reaches for did nothing on the app's only navigation
        # control. Arrow keys already move and switch (the M3 tab pattern), so
        # activation here confirms and, when a run is in flight, says why it
        # will not switch instead of silently ignoring the key.
        self.switch.bind("<Return>", self._on_switch_activate)
        self.switch.bind("<space>", self._on_switch_activate)
        self.switch.bind("<FocusIn>", lambda _e: self._set_switch_focus(True),
                         add="+")
        self.switch.bind("<FocusOut>", lambda _e: self._set_switch_focus(False),
                         add="+")

        cx = rail_w / 2.0
        for i, name in enumerate(self._switch_labels):
            top = top_pad + i * item_h
            hov = self._canvas_round_rect(
                self.switch, cx - ind_w / 2.0, top,
                cx + ind_w / 2.0, top + ind_h,
                ind_h / 2.0, fill=COLORS["shell"], outline="")
            self._hover_bgs.append(hov)
            ind = self._canvas_round_rect(
                self.switch, cx - 1, top, cx + 1, top + ind_h, 1,
                fill=COLORS["secondary_container"], outline="",
                state="hidden")
            icon = getattr(self, "_pill_icons", {}).get(name)
            icon_id = None
            if icon is not None:
                icon_id = self.switch.create_image(
                    cx, top + ind_h / 2.0, image=icon)
                self._switch_icon_items.append(icon_id)
            # anchor="n" so the gap is the SPACE between the pill's edge and
            # the text's top, not a centre offset that changes with the font.
            text_id = self.switch.create_text(
                cx, top + ind_h + label_gap, text=name, anchor="n",
                font=TYPE["caption"], fill=COLORS["subtext"])
            self._switch_texts.append(text_id)
            self._pill_items.append(
                (ind, icon_id, text_id, top, top + item_h))
        self._paint_pills()

    def _rail_indicator_points(self, i, t):
        cx = self._rail_w / 2.0
        top = self._rail_top + i * self._rail_item_h
        half = max(1.0, (self._rail_ind_w / 2.0) * max(0.0, min(1.0, t)))
        return self._round_points(cx - half, top, cx + half,
                                  top + self._rail_ind_h,
                                  min(self._rail_ind_h / 2.0, half))

    def _paint_pills(self):
        """Repaint indicators, hover layers and labels from current state."""
        try:
            for i, (ind, icon_id, text_id, y0, y1) in enumerate(
                    self._pill_items):
                t = self._rail_t[i]
                is_active = (TAB_NAMES[i] == self.active_tab)
                is_hover = (self._hover_seg == i and not is_active)
                if t > 0.02:
                    self.switch.coords(ind, *self._rail_indicator_points(i, t))
                    self.switch.itemconfigure(ind, state="normal")
                else:
                    self.switch.itemconfigure(ind, state="hidden")
                hov = COLORS["shell"]
                if is_hover:
                    hov = _hex_lerp(COLORS["shell"], COLORS["text"],
                                    0.08 * self._hover_t)
                focused = bool(self._kb_focus) and is_active
                self.switch.itemconfigure(
                    self._hover_bgs[i], fill=hov,
                    outline=COLORS["text"] if focused else "",
                    width=1 if focused else 0)
                self.switch.itemconfigure(
                    text_id,
                    fill=COLORS["text"] if is_active else COLORS["subtext"],
                    font=TYPE["label_medium"] if is_active else TYPE["caption"])
        except Exception:
            pass

    def _style_switch_labels(self):
        self._paint_pills()

    def _seg_at(self, y):
        i = int((y - self._rail_top) // self._rail_item_h)
        if 0 <= i < len(TAB_NAMES) and y >= self._rail_top:
            return i
        return None

    def _on_switch_click(self, event):
        seg = self._seg_at(event.y)
        if seg is not None:
            try:
                self.switch.focus_set()
            except Exception:
                pass
            self._switch_to(TAB_NAMES[seg])

    def _on_switch_activate(self, _event=None):
        """Return/Space on the rail: activate the destination it is on."""
        seg = self._hover_seg
        if seg is None:
            try:
                seg = TAB_NAMES.index(self.active_tab)
            except ValueError:
                return "break"
        if 0 <= seg < len(TAB_NAMES):
            self._switch_to(TAB_NAMES[seg])
        return "break"

    def _set_switch_focus(self, on):
        self._kb_focus = bool(on)
        self._paint_pills()

    def _on_switch_motion(self, event):
        seg = self._seg_at(event.y)
        active_idx = TAB_NAMES.index(self.active_tab)
        self.switch.config(
            cursor="hand2" if seg is not None and seg != active_idx else "arrow")
        if seg == self._hover_seg:
            return
        self._hover_seg = seg
        self._start_hover_anim()

    def _on_switch_leave(self, _event=None):
        self.switch.config(cursor="arrow")
        if self._hover_seg is None:
            return
        self._hover_seg = None
        self._start_hover_anim()

    def _start_hover_anim(self):
        if self._hover_anim_after is not None:
            try:
                self.root.after_cancel(self._hover_anim_after)
            except Exception:
                pass
        self._hover_anim = {
            "start_ms": time.monotonic(),
            "start_t": self._hover_t,
            "target_t": 1.0 if self._hover_seg is not None else 0.0,
        }
        self._tick_hover()

    def _tick_hover(self):
        anim = getattr(self, "_hover_anim", None)
        if anim is None:
            return
        p = (time.monotonic() - anim["start_ms"]) * 1000.0 / self.HOVER_ANIM_MS
        if p >= 1.0:
            self._hover_t = anim["target_t"]
            self._hover_anim = None
            self._hover_anim_after = None
            self._paint_pills()
            return
        eased = 1 - (1 - p) ** 3
        self._hover_t = anim["start_t"] + (anim["target_t"] - anim["start_t"]) * eased
        self._paint_pills()
        self._hover_anim_after = self.root.after(16, self._tick_hover)

    def _switch_to(self, name):
        if self._busy or name == self.active_tab:
            if self._busy:
                self.set_status(
                    "Busy \u2014 wait for the current task to finish (or press \u2715 Stop).")
            return
        # Resolve (and, for a lazy page, build) the target BEFORE committing
        # active_tab. The old order left the rail, app-bar title and status
        # accent on a tab whose page had failed to construct, so Run and
        # selection acted on a page the user could not see.
        if self._ensure_tab(name) is None:
            self.set_status("Could not open the %s page." % name)
            return
        old_idx = TAB_NAMES.index(self.active_tab)
        new_idx = TAB_NAMES.index(name)
        self.active_tab = name
        self._update_tab_icon()
        if self._switch_anim_after is not None:
            try:
                self.root.after_cancel(self._switch_anim_after)
            except Exception:
                pass
        self._switch_anim = {
            "old": old_idx, "new": new_idx,
            "old_t": self._rail_t[old_idx], "new_t": self._rail_t[new_idx],
            "start_ms": time.monotonic(),
        }
        if self._hover_seg == new_idx:
            self._hover_seg = None
            self._hover_t = 0.0
        self._style_switch_labels()
        self._show_tab(name)
        # PERF (2026-10): no synchronous layout flush here. Pages are stacked
        # with place() and switched with tkraise(), which needs no layout at
        # all; the old update_idletasks() made every click pay a full layout
        # pass over every hidden page (measured 42-89 ms per switch, 96-100%
        # of the click, vs 3-7 ms without it). Tk lays the page out on the
        # next idle turn, so the visible result is unchanged and the rail
        # animation gets its full frame budget instead of starting late.
        self._tick_switch()

    def _tick_switch(self):
        anim = getattr(self, "_switch_anim", None)
        if anim is None:
            return
        p = (time.monotonic() - anim["start_ms"]) * 1000.0 / self.SWITCH_ANIM_MS
        o, n = anim["old"], anim["new"]
        if p >= 1.0:
            for i in range(len(self._rail_t)):
                self._rail_t[i] = 1.0 if i == n else 0.0
            self._switch_pos = float(n)
            self._switch_anim = None
            self._switch_anim_after = None
            self._paint_pills()
            return
        eased = 1 - (1 - p) ** 3
        self._rail_t[o] = anim["old_t"] * (1 - eased)
        self._rail_t[n] = anim["new_t"] + (1.0 - anim["new_t"]) * eased
        self._paint_pills()
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

    def _load_tab_icons(self):
        """Load one icon asset per tab, scaled to TAB_ICON_PX at this DPI.

        Split out of _build_main_ui (DESIGN AUDIT 2026-10) so the label that
        displays them and the loading of them are not separated by a screen's
        worth of layout code - the previous arrangement had the loader in
        _build_main_ui and the label inside _build_tab_switch, which is
        exactly the distance a parent mistake hides in.

        PhotoImages are kept in self._tab_icons so Tk never garbage-collects
        the currently displayed one (a Label holds only a name, not the
        image).
        """
        self._tab_icons: dict = {}
        self._pill_icons: dict = {}
        for _tab, _fname in (("Clean", "clean.png"), ("Repair", "repair.png"),
                             ("Tweak", "tweak.png"), ("Health", "health.png"),
                             ("Install", "install.png"),
                             ("Drivers", "drivers.png"),
                             ("Tools", "tools.png"),
                             ("Settings", "settings.png")):
            for _p in self._asset_candidates(_fname):
                if not _p.is_file():
                    continue
                try:
                    _img = tk.PhotoImage(file=str(_p))
                    # ICON SIZE CONTRACT (switcher-jump fix 2026-09, still
                    # binding): every icon must DISPLAY at the same size or
                    # the icon label changes height per tab and shoves the
                    # whole navigation row - and everything under it - up or
                    # down. That was the "few px jump" bug report.
                    #
                    # DPI-SCALED (2026-10 visual pass): the target follows
                    # the display, so 200% gets the asset's full 72px instead
                    # of a 36px mark beside 30px tab labels. scaled_photo
                    # handles ratios an integer subsample cannot (48px from
                    # a 72px asset), and every tab still uses ONE target.
                    _img = scaled_photo(_img,
                                        icon_target_px(TAB_ICON_PX, self.root))
                    self._tab_icons[_tab] = _img
                    # 24px at 100%, 48px at 200%, shown INSIDE the pill.
                    _small = tk.PhotoImage(file=str(_p))
                    _small = scaled_photo(
                        _small, icon_target_px(ICON_CONTROL, self.root))
                    self._pill_icons[_tab] = _small
                    break
                except Exception:
                    continue

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

    def _ensure_tab(self, name):
        """Return the page for `name`, building a lazy one on first use.

        Tools, Drivers and Settings are lazy (see _build_main_ui). Construction
        runs on the Tk thread because it creates widgets; a failure is logged
        and returns None so the shell can report it rather than crash.
        """
        page = self.tabs.get(name)
        if page is not None:
            return page
        factory = getattr(self, "_tab_factories", {}).pop(name, None)
        if factory is None:
            return None
        try:
            page = factory()
        except Exception as exc:
            try:
                self.log("  ! could not build the %s page: %r" % (name, exc))
            except Exception:
                pass
            return None
        self.tabs[name] = page
        return page

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
        target = self._ensure_tab(name)
        if target is None:
            # A lazy page failed to construct. Say so instead of leaving the
            # previous page up with the wrong title, and keep the app alive.
            self.set_status("Could not open the %s page." % name)
            return
        if name == "Install":
            # Defer BOTH the first catalog slice and the (slow) winget scans
            # to the next event-loop turn: building a slice synchronously here
            # was measured at 433 ms on a cold catalog, and the scans are
            # read-only work that must not sit between the click and the paint.
            try:
                target.warm_scans()
            except Exception:
                pass
            _start = getattr(target, "_start_catalog_build", None)
            if _start is not None:
                try:
                    self.root.after(0, _start)
                except Exception:
                    pass
        try:
            self._appbar_title.config(text=name)
            self._appbar_sub.config(text=self._TAB_SUBTITLES.get(name, ""))
        except Exception:
            pass
        # TIMING (diagnostic): what the switch itself costs, and whether this
        # was a FIRST map of the page (large tree) or a plain re-raise.
        _t_show = time.perf_counter()
        _first_map = target.winfo_manager() != "place"
        if _first_map:
            target.place(relx=0, rely=0, relwidth=1, relheight=1)
        target.tkraise()
        # PERF (2026-10): the first-map layout flush that used to live here
        # is gone. It ran on the user's click (a full geometry pass over the
        # whole stacked page set) and was the dominant cost of a tab switch;
        # tkraise() itself needs no layout, and Tk's idle handler lays the
        # page out before it is next painted.
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
        # Pages that do work on arrival opt in here. Health deliberately does
        # not: its check starts only when the user presses Check PC.
        # Building the page at startup must stay cheap, so the constructor
        # only builds widgets; the scan starts on the user's first visit.
        _on_show = getattr(target, "on_show", None)
        if callable(_on_show):
            try:
                _on_show()
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
        # progress strip may not exist yet during the initial build
        if hasattr(self, "progress_row"):
            self.progress_row.set_accent(TAB_ACCENTS[name])
        elif hasattr(self, "progress_bar"):
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

    def _status_icon(self, icon_key):
        """A status-row glyph from the generated asset set, or None.

        Same contract as TaskTab._preset_icon_image: a missing asset must
        degrade to the emoji fallback rather than leave a hole, the
        PhotoImage is cached on the instance (a widget holds only the image's
        NAME, so an uncached one is collected and renders as nothing), and
        nothing may raise - a status row that fails to build would take the
        whole window with it.
        """
        if not icon_key:
            return None
        try:
            if not hasattr(self, "_status_icons"):
                self._status_icons = {}
            if icon_key in self._status_icons:
                return self._status_icons[icon_key]
            from app.utils import resolve_asset_path
            p = resolve_asset_path("preset_icons/%s.png" % icon_key)
            img = None
            if p:
                img = tk.PhotoImage(file=str(p))
                img = scaled_photo(
                    img, icon_target_px(STATUS_ICON_PX, self.root))
            self._status_icons[icon_key] = img
            return img
        except Exception:
            return None

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
        """Opens the storage_insight dialog. See app/ui/dialogs/registry.py."""
        open_dialog(self, "storage_insight")

    def _open_dns_tester(self):
        """Opens the dns_tester dialog. See app/ui/dialogs/registry.py."""
        open_dialog(self, "dns_tester")

    def _open_process_manager(self):
        """Opens the process_manager dialog. See app/ui/dialogs/registry.py."""
        open_dialog(self, "process_manager")

    # ---------------- Game Session Auto-Pilot (feature 6) ---------------- #
    # The watcher thread lives only while the GUI runs (started here on
    # boot when enabled, stopped on close/toggle-off). Detection and
    # state live in app/session_pilot.py (headless-tested); THESE methods
    # are the GUI half: resolve preset tasks, call run_tasks, toast, and
    # repaint the dialog's live line when it's open. run_tasks itself is
    # async + thread-safe by design (worker + Tk hops), so the watcher
    # thread only ever schedules after() hops — never touches widgets.

    def _open_pilot_dialog(self):
        """Opens the pilot dialog. See app/ui/dialogs/registry.py."""
        open_dialog(self, "pilot")

    def _open_speed_test(self):
        """Opens the speed_test dialog. See app/ui/dialogs/registry.py."""
        open_dialog(self, "speed_test")

    def _open_hw_monitor(self):
        """Opens the hw_monitor dialog. See app/ui/dialogs/registry.py."""
        open_dialog(self, "hw_monitor")

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
            # DECLUTTER 2026-10: full access is the normal state, so it says
            # nothing. The label exists to flag the ONE state that needs the
            # user's attention (Limited), not to read "Administrator" in the
            # corner of every window forever.
            self._mode_lbl.config(text="", cursor="")
            try:
                self._mode_lbl.pack_configure(padx=0)
            except Exception:
                pass
            return
        try:
            self._mode_lbl.pack_configure(padx=(0, GAP_X))
        except Exception:
            pass
        # REDESIGN 2026-10: the 🔓 is gone. "Limited - cleaning only" already
        # says everything the padlock was there to say, the whole label is
        # clickable, and it carries a tooltip that spells out the action
        # ("Click to unlock full features"). A padlock is a second, unthemeable
        # copy of a word that is already on screen.
        self._mode_lbl.config(text="Limited — cleaning only",
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
            _themed_showerror(
                self.root, "Elevation Failed",
                "Nothing has changed. You can keep using Limited mode.")
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
                        _themed_showwarning(
                            self.root, "Elevation Cancelled",
                            "If an elevated window opened, use that one and "
                            "close this.\n\nOtherwise nothing has changed.")
                    except Exception:
                        pass
            # MED-014: was `self.root.after(0, _on_done)` from the waiting
            # thread. If that after() is dropped the elevation handshake never
            # completes and you are left with TWO main windows - the exact bug
            # the comment above this block claims to have fixed.
            self._dispatch.post(_on_done)
        threading.Thread(target=_wait_thread, daemon=True).start()

    def _open_game_server_ping(self):
        """Opens the game_server_ping dialog. See app/ui/dialogs/registry.py."""
        open_dialog(self, "game_server_ping")

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
        """Opens the drive_toolkit dialog. See app/ui/dialogs/registry.py."""
        open_dialog(self, "drive_toolkit")

    def _open_battery_report(self):
        """Open Windows' own full battery report (N-003's card action)."""
        try:
            from app.health_sensors import open_battery_report
            if not open_battery_report():
                self.set_status("Could not open the battery report on this PC.")
        except Exception:
            pass

    def _open_health_history(self):
        """Opens the checkup-history dialog. See app/ui/dialogs/registry.py."""
        open_dialog(self, "health_history")

    def _open_driver_store(self):
        """U-005: the driver-store window.

        Not in app/ui/dialogs/registry.py: this one belongs to the Drivers
        TAB (it deletes from the same store that tab installs into), so it is
        opened through the tab when the tab exists. The fallback keeps a
        route working from a surface that opened before the tab was built.
        """
        try:
            tab = self.tabs.get("Drivers")
        except Exception:
            tab = None
        try:
            if tab is not None:
                tab._open_store()
                return
            from app.ui.dialogs.driver_store_dialog import DriverStoreDialog
            DriverStoreDialog(self.root, self).wait()
        except Exception as exc:                        # noqa: BLE001
            try:
                self.log(f"! Could not open the driver store: {exc}")
            except Exception:
                pass

    def _open_startup_manager(self):
        """Opens the startup_manager dialog. See app/ui/dialogs/registry.py."""
        open_dialog(self, "startup_manager")

    def _open_uninstall_programs(self):
        """Opens the uninstall_programs dialog. See app/ui/dialogs/registry.py."""
        open_dialog(self, "uninstall_programs")

    def _open_pc_specs(self):
        """Opens the pc_specs dialog. See app/ui/dialogs/registry.py."""
        open_dialog(self, "pc_specs")

    def _open_health_report(self):
        """Open the Health destination (2026-10: it is a tab, not a popup).

        The status-row badge and anything else that used to call this keep
        the same entry point; only the surface changed. The old dialog is
        still exported for compatibility, but nothing routes to it.
        """
        try:
            self._switch_to("Health")
        except Exception:
            pass

    def _open_network_connections(self):
        """Opens the network_connections dialog. See app/ui/dialogs/registry.py."""
        open_dialog(self, "network_connections")

    def _open_services(self):
        """Opens the services dialog. See app/ui/dialogs/registry.py."""
        open_dialog(self, "services")

    def _open_file_lock(self):
        """Opens the file_lock dialog. See app/ui/dialogs/registry.py."""
        open_dialog(self, "file_lock")

    def _open_duplicates(self):
        """Opens the duplicates dialog. See app/ui/dialogs/registry.py."""
        open_dialog(self, "duplicates")

    def _open_update_picker(self):
        """Opens the update_picker dialog. See app/ui/dialogs/registry.py."""
        open_dialog(self, "update_picker")

    def _open_restore_points(self):
        """Opens the restore_points dialog. See app/ui/dialogs/registry.py."""
        open_dialog(self, "restore_points")

    def _open_safety_center(self):
        """Opens the safety_center dialog. See app/ui/dialogs/registry.py."""
        open_dialog(self, "safety_center")

    def _open_game_saves(self):
        """Opens the game_saves dialog. See app/ui/dialogs/registry.py."""
        open_dialog(self, "game_saves")

    def _open_game_readiness(self):
        """Opens the game_readiness dialog. See app/ui/dialogs/registry.py."""
        open_dialog(self, "game_readiness")

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
            from app.session_pilot import (SessionPilot,
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
            # The stay-awake hold is released by _pilot_revert, and stopping
            # never reverts. Release it here when a session is live, or the
            # display/sleep stay suppressed for the rest of the process (the
            # user's PC "never sleeps" after turning Auto-Pilot off mid-game).
            _was_active = self._pilot_state is SessionState.ACTIVE
            # H15: also cancels the pump's pending after(). Pre-H15 the
            # 50ms drain loop rescheduled itself for the rest of the app's
            # life even with the feature switched off.
            self._pilot_sess.stop()
            if _was_active:
                self._set_stay_awake(False)
            self.log("Game Session Auto-Pilot: stopped watching.")
        except Exception as exc:
            from app.utils import log_or_swallow
            log_or_swallow(None, "ui.pilot_stop", exc)

    def _pilot_refresh_watchlist(self):
        """User added/removed an .exe: rebuild the watchlist live."""
        try:
            running = self._pilot_sess.is_running()
            self._pilot_ensure()
            if running:
                self._pilot_sess.start()
        except Exception:
            pass

    # ------------------------------------------------------------------ #
    # H15 forwarders. Each of these used to carry the logic; they now hand
    # off to SessionOrchestrator, which is the only thing that should own
    # the Auto-Pilot's state. Kept as named properties because this file
    # addresses them by name.
    # ------------------------------------------------------------------ #
    @property
    def _pilot(self):
        return self._pilot_sess.pilot

    @_pilot.setter
    def _pilot(self, value):
        self._pilot_sess.pilot = value


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
            # background trigger must never pop the "Skipped N Admin-Only
            # Tasks" notice over someone's game — run_tasks would do that)
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
        # user's context. Worse, run_tasks would pop an "already running" notice
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
                _themed_showerror(self.root, "Could Not Launch",
                                  f"Refusing to launch unknown target: {command!r}")
        except Exception as exc:
            _themed_showerror(self.root, "Could Not Launch", str(exc))


    # ---------------- Auto Maintenance dialog ---------------- #

    def _maybe_show_automaint_tip(self):
        """One-time nudge toward Auto Maintenance (now in the Settings tab) after the first
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
                    "Open Settings to set it up now? (You can always find "
                    "it later in the Settings tab.)",
                    accent=TAB_ACCENTS["Clean"], yes_text="Open Settings",
                    no_text="Not now"):
                self._switch_to("Settings")
        except Exception:
            pass

    def _show_schedule_dialog(self):
        from app.config_persist import load_config
        from app.scheduler import (enable_schedule, disable_schedule)

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
        col.pack(fill="x", padx=MEASURE_CAP, pady=(SPACE_3, 0))
        _CTL_W, _CTL_H = 120, 34

        def _row():
            r = tk.Frame(col, bg=COLORS["bg"])
            r.pack(fill="x", pady=CONTROL_GAP)
            return r

        def _toggle_row(label, initial, tip=None):
            var = tk.BooleanVar(value=bool(initial))
            r = _row()
            lbl = tk.Label(r, text=label, bg=COLORS["bg"], fg=COLORS["text"],
                           font=TYPE["body_large"])
            lbl.pack(side="left")
            btn = AnimatedButton(
                r, text="On" if var.get() else "Off",
                command=lambda: var.set(not var.get()),
                role="secondary", size="md", width=_CTL_W, height=_CTL_H)
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

        # PC Health badge schedule. Read-only check; never fixes anything by
        # itself. Stored as health_check_mode (off | startup | daily | weekly).
        _HC = (("Off", "off"), ("At startup", "startup"),
               ("Daily", "daily"), ("Weekly", "weekly"))
        _hc_names = {k: n for n, k in _HC}
        _hc_keys = {n: k for n, k in _HC}
        health_var = tk.StringVar(value=_hc_names.get(
            str(config.get("health_check_mode", "startup")), "At startup"))
        hc_row = _row()
        _hc_lbl = tk.Label(hc_row, text="PC Health check", bg=COLORS["bg"],
                           fg=COLORS["text"], font=TYPE["body_large"])
        _hc_lbl.pack(side="left")
        hc_menu = M3Dropdown(_holder(hc_row), textvariable=health_var,
                             values=[n for n, _k in _HC], state="readonly",
                             width=max(len(n) for n, _k in _HC) + 1,
                             font=TYPE["body_medium"])
        hc_menu.pack(fill="both", expand=True)
        try:
            Tooltip(_hc_lbl, "How often the PC Health badge re-checks your "
                             "PC. Checking is read-only; it never changes "
                             "anything.")
        except Exception:
            pass

        tk.Frame(col, height=1, bg=COLORS["hairline"]).pack(fill="x", pady=(GROUP_GAP, CONTROL_GAP))

        freq_row = _row()
        tk.Label(freq_row, text="How often", bg=COLORS["bg"], fg=COLORS["text"],
                 font=TYPE["body_large"]).pack(side="left")
        freq_var = tk.StringVar(value=freq)
        freq_combo = M3Dropdown(_holder(freq_row), textvariable=freq_var,
                                values=["daily", "weekly", "monthly"],
                                state="readonly", width=9, font=TYPE["body_medium"])
        freq_combo.pack(fill="both", expand=True)

        time_row = _row()
        tk.Label(time_row, text="Time (24h)", bg=COLORS["bg"], fg=COLORS["text"],
                 font=TYPE["body_large"]).pack(side="left")
        time_var = tk.StringVar(value=time_str)
        tk.Entry(_holder(time_row), textvariable=time_var, justify="center",
                 bg=COLORS["surface"], fg=COLORS["text"], insertbackground=COLORS["text"],
                 font=TYPE["body_medium"], bd=0, highlightthickness=1,
                 highlightbackground=COLORS["hairline"],
                 highlightcolor=COLORS["accent_green"]).pack(fill="both", expand=True)

        total_selected = sum(len(v) for v in config.get("selected_tasks", {}).values())
        tk.Label(modal.body, text=f"Runs {total_selected} tasks from your last run",
                 bg=COLORS["bg"], fg=COLORS["subtext"], font=TYPE["caption"]).pack(pady=(GROUP_GAP, 0))

        status_var = tk.StringVar(value="")
        tk.Label(modal.body, textvariable=status_var, bg=COLORS["bg"],
                 fg=COLORS["accent_blue"], font=TYPE["caption"], wraplength=440,
                 justify="center").pack(pady=(SPACE_1, 0))

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
                    _themed_showerror(dlg, "Maintenance Not Scheduled",
                                      msg or "Windows did not say why.")
            else:
                ok, msg = disable_schedule()
                if ok:
                    results.append("Maintenance disabled")
                else:
                    _themed_showerror(dlg, "Schedule Not Removed",
                                      msg or "Windows did not say why.")
            # update-everything schedule (separate schtasks entry)
            if upd_var.get():
                ok, msg = enable_schedule(freq_var.get(), time_var.get(), extra_args=["--auto-update"])
                if ok:
                    results.append("Update Everything enabled")
                else:
                    _themed_showerror(dlg, "Updates Not Scheduled",
                                      msg or "Windows did not say why.")
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
            try:
                from app.config_persist import update_config as _upd_hc
                _hc_val = _hc_keys.get(health_var.get(), "startup")
                _upd_hc(lambda c, _v=_hc_val: c.__setitem__(
                    "health_check_mode", _v))
            except Exception as exc:
                results.append(f"PC Health check unset ({exc})")
            refresh_status()
            # short + quiet: "Saved." unless something failed or is pending
            _attn = [r for r in results
                     if any(k in r.lower() for k in ("fail", "next time", "will activate"))]
            status_var.set("; ".join(_attn) if _attn else "Saved.")

        btn_frame = tk.Frame(modal.body, bg=COLORS["bg"])
        btn_frame.pack(fill="x", padx=MEASURE_CAP, pady=(SPACE_3, SPACE_4))
        # C-1 audit fix: close through the modal (scrim + bindings + closed
        # flag), not by destroying its Toplevel.
        AnimatedButton(btn_frame, text="Update Apps Now",
                       command=lambda: self._run_update_now(modal),
                       role="secondary", size="md",
                       ).pack(side="left")
        AnimatedButton(btn_frame, text="Apply", command=apply,
                       role="filled", size="md", accent=COLORS["accent_green"],
                       ).pack(side="right")
        AnimatedButton(btn_frame, text="Close", command=modal.close,
                       role="secondary", size="md",
                       ).pack(side="right", padx=(0, SPACE_2))
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

    # DECLUTTER 2026-10: only the system drive is shown in the status row; the
    # others are listed in its tooltip (a row of drive chips next to the PC
    # Health badge made the strip read as a dashboard).
    _MAX_INLINE_CHIPS = 1

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
            _themed_showerror(self.root, "Could Not Open Drive",
                              f"{root}\n\nWindows returned:\n{exc}")


    # ------------- low-space chip + quiet watch (feature 4 / N-006) ----- #
    # A per-drive hysteresis state machine driven by the disk-monitor tick.
    # The threshold is the user's own `watch_free_percent` (default 10%), and
    # an episode ends only above that plus a margin, so a drive hovering at
    # the boundary cannot re-mark itself every 30s.
    #
    # Two surfaces, one number: the CHIP in the status row is this function's
    # job (it opens Storage Insight and its tooltip carries a measured
    # reclaimable estimate), while the NOTIFICATION is `_watch_speak`'s, since
    # that also has to cover the health score and the schedule. Splitting them
    # this way is what keeps a drive crossing at 10% from producing two toasts
    # that disagree with each other.

    def _check_low_space(self, drives):
        """Tick entry — call with the monitor's drive list (Tk thread).

        Pure state transitions + widget updates. The threshold is read from
        the same settings quiet watch uses, so the amber chip and the notice
        can never disagree about which drives count as nearly full.
        """
        try:
            by_letter = {d[0]: d for d in drives}
        except Exception:
            return
        try:
            from app.config_persist import load_config
            cfg = load_config()
        except Exception:
            cfg = {}
        fire = watch.free_threshold(cfg) / 100.0
        clear = watch.clear_threshold(cfg) / 100.0
        prev = getattr(self, "_drive_was_red", {}) or {}
        now = {}
        try:
            # A-? (found by dev/tests/test_watch.py): this loop used to unpack
            # `(letter, (_l2, _root, free_gb, total_gb))` from a value that is
            # already the flat 4-tuple, so it raised ValueError on the first
            # drive and the outer handler swallowed it - the whole low-space
            # state machine, chips included, had never run once. Iterate the
            # values, which is what the month of silent logs was telling us.
            for letter, _root, free_gb, total_gb in by_letter.values():
                ratio = (free_gb / total_gb) if total_gb and total_gb > 0 else 1.0
                was = prev.get(letter)
                if was is True:
                    is_red = ratio <= clear
                    if not is_red:
                        self._hide_nudge_chip(letter)   # recovered
                else:
                    is_red = ratio <= fire
                    if is_red and letter not in getattr(self, "_nudge_dismissed", set()):
                        self._fire_low_space_nudge(letter, free_gb)
                now[letter] = is_red
            # drives that vanished (unplugged USB) take their chips along
            for letter in list(getattr(self, "_nudge_chips", {}) or {}):
                if letter not in by_letter:
                    self._hide_nudge_chip(letter)
            # in-place free-GB refresh for visible chips (no widget churn)
            for letter, _root, free_gb, _t in by_letter.values():
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
        """New low-space episode: the status chip and its estimate scan.

        Deliberately silent. The notification belongs to `_watch_speak`, which
        sees this same drive through the same threshold and folds it into one
        notice with any other condition; toasting here as well would be the
        duplicate this split exists to prevent.
        """
        self._show_nudge_chip(letter, free_gb)
        self._ensure_nudge_estimate()

    def _watch_speak(self, drives):
        """Ask quiet watch what crossed, and say it once (N-006).

        Off the Tk thread: `watch.check` may spawn the Task Scheduler query.
        The alerts come back as data, so the copy that reaches the user is
        written in one place (`watch.notice_text`) and logged here as well -
        a toast that has already faded must not be the only record.
        """
        if getattr(self, "_watch_running", False):
            return
        snapshot = []
        for d in drives or ():
            try:
                snapshot.append((str(d[0]), str(d[1]),
                                 float(d[2]), float(d[3])))
            except (IndexError, TypeError, ValueError):
                continue
        if not snapshot:
            return
        self._watch_running = True

        def _work():
            alerts = []
            try:
                alerts = watch.check(drives=snapshot)
            except Exception:
                alerts = []
            finally:
                self._watch_running = False
            if alerts:
                self._watch_announce(alerts)

        try:
            threading.Thread(target=_work, daemon=True,
                             name="QuietWatch").start()
        except Exception:
            self._watch_running = False

    def _watch_announce(self, alerts):
        """ONE notification for everything this tick found, plus a log line."""
        try:
            text = watch.notice_text(alerts)
        except Exception:
            text = None
        if text:
            try:
                show_toast(text[0], text[1], "long")
            except Exception:
                pass
        for alert in alerts or ():
            try:
                self.log("Quiet watch: %s \u2014 %s" % (alert.title, alert.body))
            except Exception:
                pass

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
            box = tk.Frame(frame_holder, bg=COLORS["alert_amber_bg"])
            lbl = tk.Label(box, text=self._nudge_chip_text(letter, free_gb),
                           font=TYPE["label_medium"], bg=COLORS["alert_amber_bg"],
                           fg=COLORS["accent_yellow"], cursor="hand2")
            lbl.pack(side="left", padx=(SPACE_2, TEXT_NUDGE), pady=TIGHT_GAP)
            lbl.bind("<Button-1>", lambda e: self._open_storage_insight())
            tip = Tooltip(lbl, self._nudge_tip_text(letter, free_gb, total_gb))
            x = tk.Label(box, text="✕", font=TYPE["micro_regular"], bg=COLORS["alert_amber_bg"],
                         fg=COLORS["subtext"], cursor="hand2")
            x.pack(side="left", padx=(0, SPACE_2))
            x.bind("<Button-1>", lambda e, l=letter: self._dismiss_nudge(l))
            Tooltip(x, "Dismiss for this session")
            box.pack(side="left", padx=(0, CONTROL_GAP))
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
        labels update in place otherwise). C: always inline; drives past
        the inline cap are named in the first chip's tooltip (the '+N'
        overflow menu went with the app-bar redesign, 2026-10 sweep)."""
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
                            font=TYPE["label_medium"], bg=COLORS["bg"], fg=col,
                            cursor="hand2")
            lbl.pack(side="left", padx=(CONTROL_GAP, 0))
            lbl.bind("<Button-1>", lambda e, r=root: self._open_drive(r))
            # Storage Insight (feature 3): right-click opens the insight
            # dialog; left-click keeps the long-standing Explorer behavior
            lbl.bind("<Button-3>", lambda e: self._open_storage_insight())
            _others = ("\n\nOther drives:\n" + "\n".join(
                f"{d[0]}: {d[2]:.0f} GB free" for d in extra)) if extra else ""
            Tooltip(lbl, f"{root} — {free_gb:.0f} GB free of {total_gb:.0f} GB. "
                         "Click to open in Explorer, right-click for Storage "
                         "Insight." + _others)
            self._drive_chips.append((lbl, root))
        self._drive_extra = extra

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
                # quiet watch (N-006) rides the same tick: the drive list is
                # already here, and the guard inside skips a tick whose
                # predecessor is still reading the Task Scheduler
                try:
                    self._watch_speak(drives)
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

    def _bind_global_shortcuts(self):
        """improvement-report 2.2: Ctrl+Enter runs the active tab, Escape
        stops a running task (dialogs already bind their own Escape-to-
        close — see ThemedModal — so this never conflicts, it only fires
        when the main window itself has focus), Ctrl+1..5 switches tabs,
        Ctrl+L copies the run log.
        Best-effort: any failure here should never block the app from
        starting.
        """
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
        at call time on purpose, so a test can substitute a deterministic
        registry by patching this module.
        """
        try:
            return dict(get_tweak_state() or {})
        except Exception:
            return {}
