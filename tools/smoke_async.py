"""End-to-end async smoke test for the run engine + Tweak Health check.

Complements tools/smoke_gui.py: that harness pumps root.update()
manually, under which worker-thread after() delivery starves (Tcl
inter-thread wakeup starvation — proven during Feature 1 dev, where a
worker wedged inside createcommand for the full pump window). The real
app runs mainloop(), where that machinery works — so THIS script runs
a real mainloop, headless (root withdrawn), with timer-chained steps:

  1. real run_tasks(Clean, [stub]) -> worker completes -> _done_popup
     fires -> ScorecardDialog builds with the genuinely captured rows
     -> assert title/counts/rows -> auto-close -> assert app idle again
  2. mark a real HKCU key applied -> _enter_health -> the check worker's
     hop lands -> assert the result -> restore config -> exit 0

A watchdog timer FAILS the process (os._exit(1)) if any step stalls,
so CI can never wedge on a modal or a hung worker. Headless-safe:
nothing here needs a mapped window (dialogs never map under a
withdrawn root, but wait_window still runs and timers still fire).

Run:  python -X utf8 -m tools.smoke_async
"""

import os
import sys
import pathlib

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import tkinter as tk

from app import config_persist
from app import gui
from app.elevation import is_admin

WATCHDOG_MS = 150000   # generous PER-STEP budget: the nudge phase runs a
                       # REAL full allowlist estimate scan (stat-only, but
                       # TEMP + browser caches can be large on lived-in
                       # PCs), and DNS testing makes real network probes.
                       # This is the ceiling for any ONE step, not the
                       # whole script — see _watchdog_tick: a one-shot
                       # timer for the total runtime blamed whichever step
                       # happened to be current when a handful of earlier,
                       # legitimately-slow-but-still-progressing steps ate
                       # the shared budget, which is misleading on a loaded
                       # CI box and not what "a step stalled" should mean.
_state = {"failed": False, "step": "init"}


def _restore_clean_selection():
    """Write back the snapshotted Clean selection with retries, then VERIFY
    by re-reading from disk (bypassing the in-memory cache). Returns True
    only when the user's file provably holds the snapshot.

    Module-level (not nested in main) so both restore_and_exit() and fail()
    can reach it. The snapshot travels via _state["saved_clean"], set once
    in main() before any run — fail() may fire before that on exotic paths,
    hence the None guard.

    Hardening (proven by a real failure in this environment): a transient
    os.replace lock (AV/indexer) fails save_config AFTER a complete tmp was
    written — the old fire-and-forget restore then reported ALL PASS while
    the stub selection stayed on disk. Never trust the write; re-read it."""
    import time as _t
    saved = _state.get("saved_clean", None)
    if saved is None:
        return True
    last = None
    for _ in range(5):
        try:
            cfg = config_persist.load_config()
            cfg.setdefault("selected_tasks", {})["Clean"] = saved
            config_persist.save_config(cfg)
            # verify from DISK, not the cache (a failed save leaves the
            # cache ahead of the file — exactly what must be detected)
            config_persist._config_cache = None
            check = config_persist.load_config()
            if check.setdefault("selected_tasks", {}).get("Clean") == saved:
                return True
            last = "verify mismatch after save"
        except Exception as exc:
            last = exc
        try:
            _t.sleep(0.3)
        except Exception:
            pass
    try:
        print(f"  restore: Clean selection NOT restored ({last!r})", flush=True)
    except Exception:
        pass
    return False


def _restore_tip_seen():
    """Write back the snapshotted seen_tips dict (best-effort, never raises).

    The suite pre-marks the one-time automaint tip so a fresh config can
    never pop its modal mid-run (that modal registers in
    ThemedModal._MODAL_OPEN and makes the pilot defer every apply,
    stalling poll_pilot_applied — the exact CI failure). The user's real
    tip state must survive the suite either way."""
    try:
        saved = _state.get("saved_tips", None)
        if saved is None:
            return
        import copy as _copy
        cfg = config_persist.load_config()
        cfg["seen_tips"] = _copy.deepcopy(saved)
        config_persist.save_config(cfg)
        config_persist._config_cache = None
    except Exception:
        pass


def fail(msg):
    if _state["failed"]:
        return
    _state["failed"] = True
    print(f"ASYNC SMOKE: FAIL at step { _state['step']}: {msg}", flush=True)
    try:
        # best-effort restore even on failure paths: fail() exits the
        # process directly via os._exit, bypassing restore_and_exit, so
        # without this any stub selection persists in the user's file.
        # Pure file I/O, no Tk — safe even when the mainloop is wedged.
        _restore_clean_selection()
    except Exception:
        pass
    try:
        _restore_tip_seen()
    except Exception:
        pass
    try:
        root.destroy()
    except Exception:
        pass
    os._exit(1)


class _NullTask:
    key = "smoke_null_task"
    label = "Smoke Null Task"
    description = ""
    admin_required = False
    risk = "SAFE"
    revert = None

    def run(self, ctx):
        return 123


class _SlowStopTask:
    """Slow enough to observe the in-run UI, stop-aware so the suite can
    exercise the real Stop path (A-2 regression cover for the swallowed
    set_run_enabled outage: Stop was hidden+disabled and the bar invisible
    on every run, without any test asserting mid-run state)."""
    key = "slow_stop_task"
    label = "Slow Stop Task"
    description = ""
    admin_required = False
    risk = "SAFE"
    revert = None

    def run(self, ctx):
        import time as _t
        from app.utils import TaskCancelled as _TC
        for _ in range(200):
            if ctx.cancelled():
                raise _TC("stopped by user")
            _t.sleep(0.05)
        return 7


def main():
    global root
    root = tk.Tk()
    root.withdraw()

    app = gui.Application(root)
    if not is_admin():
        gate = None
        for w in root.winfo_children():
            if isinstance(w, gui.AdminGateFrame):
                gate = w
                break
        assert gate is not None, "Non-admin but AdminGateFrame not shown"
        gate._continue_limited()

    # don't let one test's selection leak into the user's scheduler:
    # snapshot the Clean selection, restore it before exit
    try:
        _saved_clean = list(config_persist.load_config()
                             .get("selected_tasks", {}).get("Clean", []))
    except Exception:
        _saved_clean = None
    # One-time automaint tip: a fresh config pops its modal after the
    # first successful Clean run. That modal registers in
    # ThemedModal._MODAL_OPEN, and the pilot defers every apply while any
    # modal is open — stalling poll_pilot_applied forever on CI (fresh
    # runner) while passing locally (tip already seen). Pre-mark it so no
    # run in this suite can ever pop it; restore the user's real flag on
    # exit (both paths). The app also suppresses the tip when the root is
    # withdrawn — belt and suspenders.
    try:
        import copy as _copy
        _saved_tips = _copy.deepcopy(config_persist.load_config().get("seen_tips", {}) or {})
    except Exception:
        _saved_tips = None
    try:
        cfg = config_persist.load_config()
        seen = dict(cfg.get("seen_tips", {}) or {})
        seen["automaint_after_first_clean"] = True
        cfg["seen_tips"] = seen
        config_persist.save_config(cfg)
        config_persist._config_cache = None
    except Exception:
        pass

    before_tops = set()
    stop_tops = set()  # toplevel baseline for the mid-run/stop regression step
    _state["saved_clean"] = _saved_clean  # module-level restore helper reads it
    _state["saved_tips"] = _saved_tips

    def _close_stray_modals(exclude_ids=()):
        """Best-effort drain of any open ThemedModal + raw Toplevels.

        Scorecard cleanup used to destroy exactly one Toplevel — a second
        modal (e.g. the automaint tip that follows the first Clean card)
        survived, keeping any_open() True and deferring every later pilot
        apply. Close via the modal registry first (runs on_close hooks,
        drops the scrim), then destroy any leftover raw Toplevels."""
        try:
            mods = list(getattr(gui.ThemedModal, "_MODAL_OPEN", {}).values())
        except Exception:
            mods = []
        for m in mods:
            try:
                if id(m) in (exclude_ids or ()):
                    continue
                m.close()
            except Exception:
                pass
        try:
            root.update()
        except Exception:
            pass
        try:
            for w in list(root.winfo_children()):
                try:
                    if isinstance(w, tk.Toplevel) and id(w) not in (exclude_ids or ()):
                        w.destroy()
                except Exception:
                    pass
        except Exception:
            pass
        try:
            root.update()
        except Exception:
            pass

    def restore_and_exit(code):
        restored_ok = _restore_clean_selection()
        try:
            _restore_tip_seen()
        except Exception:
            pass
        try:
            root.destroy()
        except Exception:
            pass
        if code == 0 and not restored_ok:
            # steps passed but the user's file is left modified — that is a
            # suite failure, not a pass
            print("ASYNC SMOKE: STEPS PASS BUT CONFIG RESTORE FAILED", flush=True)
            sys.exit(2)
        if code == 0:
            print("ASYNC SMOKE: ALL PASS (user config restored + verified)", flush=True)
        sys.exit(code)

    # ---- step 1: real run -> scorecard dialog ------------------------ #
    def step_run():
        _state["step"] = "run"
        before_tops.clear()
        before_tops.update(id(w) for w in root.winfo_children()
                           if isinstance(w, tk.Toplevel))
        try:
            app.run_tasks("Clean", [_NullTask()], mode="run")
        except Exception as exc:
            fail(f"run_tasks raised: {exc!r}")
            return
        root.after(150, poll_scorecard)

    def poll_scorecard():
        _state["step"] = "poll_scorecard"
        new = [w for w in root.winfo_children()
               if isinstance(w, tk.Toplevel) and id(w) not in before_tops]
        if not new:
            root.after(150, poll_scorecard)
            return
        dlg = new[0]
        # the dialog object isn't directly reachable from the widget —
        # find it via the app's worker state instead: results captured?
        results = getattr(app, "_run_results", None)
        if not results:
            fail("scorecard shown but _run_results empty")
            return
        assert results[0][0] == "Smoke Null Task", results
        assert results[0][1] == "ok", results
        assert results[0][2] == 123, results
        print("  run engine: worker captured (label, ok, 123) OK", flush=True)
        # close whatever dialog is up (scorecard, or the themed fallback).
        # Drain ALL new toplevels, not just the first: the one-time
        # automaint tip can follow the first Clean card on fresh configs,
        # and any survivor keeps any_open() True, deferring every later
        # pilot apply (the CI stall). Registry-first so on_close/scrim run.
        try:
            _close_stray_modals(exclude_ids=tuple(before_tops))
        except Exception as exc:
            fail(f"dialog destroy raised: {exc!r}")
            return
        try:
            assert gui.ThemedModal.any_open() is False, \
                f"modal leaked after run step: {list(getattr(gui.ThemedModal, '_MODAL_OPEN', {}))}"
        except AssertionError as exc:
            fail(f"post-run modal drain: {exc!r}")
            return
        except Exception as exc:
            fail(f"dialog destroy raised: {exc!r}")
            return
        root.after(400, step_stoprun)

    # ---- step 1b: mid-run Stop/progress UI + real stop classification ---- #
    # A-2 regression cover: the swallowed set_run_enabled crash left every
    # run without a visible/clickable Stop and without a progress bar.
    # Assert the in-run UI while a slow task executes, click the real Stop
    # command, then assert the 'stop' classification and UI restoration.
    def step_stoprun():
        _state["step"] = "stoprun"
        stop_tops.clear()
        stop_tops.update(id(w) for w in root.winfo_children()
                         if isinstance(w, tk.Toplevel))
        try:
            app.run_tasks("Clean", [_SlowStopTask()], mode="run")
        except Exception as exc:
            fail(f"stoprun run_tasks raised: {exc!r}")
            return
        root.after(250, poll_midrun)

    def poll_midrun():
        _state["step"] = "poll_midrun"
        try:
            assert getattr(app, "_busy", False) is True, "run did not start"
            # the in-run UI the old bug killed outright:
            assert app.cancel_btn._enabled is True, "Stop not enabled mid-run"
            assert app.cancel_btn.winfo_manager() != "", "Stop not shown mid-run"
            assert app._progress_hidden is False, "progress bar hidden mid-run"
            assert app.tabs["Clean"].run_btn._enabled is False, "Run not gated mid-run"
        except AssertionError as exc:
            fail(f"mid-run UI: {exc!r}")
            return
        except Exception as exc:
            fail(f"mid-run UI raised: {exc!r}")
            return
        print("  run UI: Stop visible+enabled, progress shown, run gated OK", flush=True)
        # the real Stop path (same command the user clicks, minus the 60ms
        # press animation the button adds around it)
        try:
            app.cancel_btn._command()
        except Exception as exc:
            fail(f"stop click raised: {exc!r}")
            return
        root.after(250, poll_stopscore)

    def poll_stopscore():
        _state["step"] = "poll_stopscore"
        cards = [w for w in root.winfo_children()
                 if isinstance(w, tk.Toplevel) and id(w) not in stop_tops]
        if not cards:
            # worker still winding down — keep polling (watchdog bounds us)
            root.after(250, poll_stopscore)
            return
        try:
            results = getattr(app, "_run_results", None) or []
            assert results and results[0][0] == "Slow Stop Task", results
            assert results[0][1] == "stop", results
            assert getattr(app, "_busy", True) is False, "busy stuck after stop"
            assert getattr(app, "_cancel_requested", True) is False, "cancel flag not consumed"
            assert app.cancel_btn._enabled is False, "Stop left enabled after run"
            assert app._progress_hidden is True, "progress bar left shown after run"
            assert app.tabs["Clean"].run_btn._enabled is True, "Run not re-enabled after run"
        except AssertionError as exc:
            fail(f"stop classification: {exc!r}")
            return
        except Exception as exc:
            fail(f"stop classification raised: {exc!r}")
            return
        print("  run stop: 'stop' classification, UI restored OK", flush=True)
        try:
            _close_stray_modals(exclude_ids=tuple(stop_tops))
        except Exception as exc:
            fail(f"stop scorecard destroy raised: {exc!r}")
            return
        try:
            assert gui.ThemedModal.any_open() is False, \
                f"modal leaked after stop step: {list(getattr(gui.ThemedModal, '_MODAL_OPEN', {}))}"
        except AssertionError as exc:
            fail(f"post-stop modal drain: {exc!r}")
            return
        except Exception as exc:
            fail(f"stop scorecard destroy raised: {exc!r}")
            return
        root.after(400, step_health)

    # ---- step 2: health check worker lands ---------------------------- #
    def step_health():
        _state["step"] = "health"
        try:
            config_persist.mark_tweak_applied("mouse_accel")
        except Exception as exc:
            fail(f"mark applied raised: {exc!r}")
            return
        try:
            app.tabs["Tweak"]._enter_health()
        except Exception as exc:
            fail(f"_enter_health raised: {exc!r}")
            return
        root.after(150, poll_health)

    def poll_health():
        _state["step"] = "poll_health"
        page = app.tabs["Tweak"]
        res = getattr(page, "_health_results", {}) or {}
        if "mouse_accel" not in res:
            root.after(150, poll_health)
            return
        v = res["mouse_accel"]
        assert v in (True, False, None), v
        print(f"  health check: worker hop landed (mouse_accel={v}) OK", flush=True)
        try:
            config_persist.mark_tweak_reverted("mouse_accel")
        except Exception as exc:
            fail(f"revert raised: {exc!r}")
            return
        root.after(400, step_storage)

    # ---- step 3: storage scan worker lands ---------------------------- #
    # Bounded scope (NOT a full machine scan — that belongs to manual
    # QA): one tiny real task proves the measure path (real task.run
    # with a dry ctx, delivered through the worker hop); the games
    # paint hop is proven with stub data. Full-tree sizing itself is
    # covered by fixture unit tests in smoke_gui.py.
    def step_storage():
        _state["step"] = "storage"
        try:
            import app.storage_scan as _ss
        except Exception as exc:
            fail(f"storage_scan import raised: {exc!r}")
            return
        _saved_cats = _ss.SCAN_CATEGORIES
        _saved_games = _ss.find_game_libraries
        _state["saved_cats"] = _saved_cats
        _state["saved_games"] = _saved_games
        _ss.SCAN_CATEGORIES = (("Probe", ("gpu_watchdog_dumps",),
                                "tiny real task"),)
        _ss.find_game_libraries = lambda cancelled=None, on_game=None: [
            {"launcher": "Steam", "name": "Probe Game",
             "path": "C:\\Games\\Probe", "bytes": 42 * 1024**3,
             "drive": "C:"},
        ]
        try:
            _state["dlg"] = gui.StorageInsightDialog(root, app)
        except Exception as exc:
            _ss.SCAN_CATEGORIES = _saved_cats
            _ss.find_game_libraries = _saved_games
            fail(f"storage dialog build raised: {exc!r}")
            return
        root.after(150, poll_storage)

    def poll_storage():
        _state["step"] = "poll_storage"
        import app.storage_scan as _ss
        dlg = _state.get("dlg")
        if dlg is None or not getattr(dlg, "_scan_done", False):
            root.after(150, poll_storage)
            return
        try:
            assert dlg._cat_sizes.get("Probe") is not None, "probe category never painted"
            assert isinstance(dlg._cat_sizes["Probe"], int)
            assert len(dlg._games) == 1, dlg._games
            assert dlg._games[0]["name"] == "Probe Game"
            print("  storage scan: measure + games hops landed OK", flush=True)
        except AssertionError as exc:
            fail(f"storage asserts: {exc!r}")
            return
        except Exception as exc:
            fail(f"storage asserts raised: {exc!r}")
            return
        finally:
            _ss.SCAN_CATEGORIES = _state.pop("saved_cats")
            _ss.find_game_libraries = _state.pop("saved_games")
            try:
                dlg._close()
            except Exception:
                pass
        root.after(400, step_nudge)

    # ---- step 4: nudge fires + real estimate scan completes ------------ #
    # The chip/toast decision is proven in smoke_gui.py; here the REAL
    # background estimate worker runs the full allowlist scan on this
    # machine and its landing hop must deliver (generous budget: TEMP +
    # browser caches can be large on lived-in PCs).
    def step_nudge():
        _state["step"] = "nudge"
        try:
            app._nudge_estimate = None
            app._nudge_scan_running = False
            app._drive_was_red = {}
            app._nudge_dismissed = set()
            app._check_low_space([("C", "C:\\", 4.0, 100.0)])
        except Exception as exc:
            fail(f"nudge fire raised: {exc!r}")
            return
        try:
            assert "C" in app._nudge_chips, "nudge chip missing"
        except AssertionError as exc:
            fail(f"nudge chip: {exc!r}")
            return
        print("  nudge chip: shown for red drive OK", flush=True)
        # hide it again so the harness leaves no trace (the estimate
        # worker keeps running — landing on no chips is a tested no-op)
        try:
            app._hide_nudge_chip("C")
        except Exception:
            pass
        root.after(150, poll_estimate)

    def poll_estimate():
        _state["step"] = "poll_estimate"
        est = getattr(app, "_nudge_estimate", None)
        if est is None:
            # worker still scanning — keep polling (watchdog bounds us)
            root.after(1000, poll_estimate)
            return
        try:
            assert isinstance(est, tuple) and len(est) == 2, est
            assert isinstance(est[0], int) and est[0] >= 0, est
            assert getattr(app, "_nudge_scan_running", True) is False, \
                "scan flag stuck on after landing"
        except AssertionError as exc:
            fail(f"estimate landing: {exc!r}")
            return
        print(f"  nudge estimate: real scan landed ({est[0]} bytes) OK", flush=True)
        try:
            app._nudge_estimate = None
            app._drive_was_red = {}
        except Exception:
            pass
        root.after(400, step_dns)

    # ---- step 5: DNS tester runs real probes end-to-end ---------------- #
    # Values are machine/network-dependent (offline CI lands all-None,
    # which is itself a tested honest path) — assert structure, types,
    # and completion, never specific latencies. Apply is NEVER clicked
    # here (it would rewrite this machine's DNS).
    def step_dns():
        _state["step"] = "dns"
        try:
            _state["dns_dlg"] = gui.DnsTesterDialog(root, app)
        except Exception as exc:
            fail(f"dns dialog build raised: {exc!r}")
            return
        root.after(150, poll_dns)

    def poll_dns():
        _state["step"] = "poll_dns"
        dlg = _state.get("dns_dlg")
        try:
            assert dlg is not None and dlg._dlg.winfo_exists()
        except AssertionError as exc:
            fail(f"dns dialog vanished: {exc!r}")
            return
        if not getattr(dlg, "_finished", False):
            root.after(500, poll_dns)
            return
        try:
            rows = getattr(dlg, "_row_widgets", {}) or {}
            assert len(rows) >= 4, f"expected 4+ resolver rows, got {len(rows)}"
            for ip, handles in rows.items():
                v = dlg._results.get(ip, "missing")
                assert v == "missing" or v is None or isinstance(v, float), (ip, v)
            n_answered = sum(1 for v in dlg._results.values() if v is not None)
            print(f"  dns tester: finished, {n_answered}/{len(rows)} answered, "
                  f"apply={'on' if dlg._apply_btn._enabled else 'off'} OK", flush=True)
        except AssertionError as exc:
            fail(f"dns results: {exc!r}")
            return
        except Exception as exc:
            fail(f"dns results raised: {exc!r}")
            return
        try:
            dlg._close()
        except Exception:
            pass
        root.after(400, step_pilot)

    # ---- step 6: pilot detect -> apply -> exit -> revert ---------------- #
    # run_tasks is CAPTURED, never executed: no real tweak may apply on
    # a test machine. Fake process source + deterministic registry make
    # the whole session reproducible.
    def step_pilot():
        _state["step"] = "pilot"
        # The pilot defers every apply while ANY modal is open (by design:
        # no run UI over a dialog). A leaked modal here stalls
        # poll_pilot_applied until the watchdog — fail fast instead, after
        # one best-effort drain so a transient closer can't flake the suite.
        try:
            if gui.ThemedModal.any_open():
                _close_stray_modals()
            assert gui.ThemedModal.any_open() is False, \
                f"modal still open at pilot start: {list(getattr(gui.ThemedModal, '_MODAL_OPEN', {}))}"
        except AssertionError as exc:
            fail(f"pilot preflight: {exc!r}")
            return
        except Exception as exc:
            fail(f"pilot preflight raised: {exc!r}")
            return
        try:
            import app.session_pilot as _sp
            from app import session_pilot as _spm
        except Exception as exc:
            fail(f"pilot import raised: {exc!r}")
            return
        _state["spm"] = _spm
        _state["orig_procs"] = _spm.default_get_processes
        _state["orig_state"] = gui.get_tweak_state
        _state["orig_run"] = app.run_tasks
        _state["procs"] = [[("eldenring.exe", "C:\\Games\\ER\\eldenring.exe")]]
        # game_mode was already applied BEFORE the session (the user's own
        # persistent setup) — the pilot must never revert it afterwards
        _state["registry"] = {"game_mode": True}
        _state["calls"] = []
        # hermetic preset: the assertions below pin the Game Session preset,
        # but the live user config may hold another pick (e.g. Recommended
        # chosen in the real pilot dialog) — snapshot it, force Game Session
        # for this step, restore in the step's finally (same discipline as
        # _saved_clean / _orig_run above). Without this the suite fails on
        # any machine whose user picked a different session preset.
        try:
            _state["orig_preset"] = config_persist.load_config().get(
                "session_pilot_preset", "Game Session")
            _cfg_p = config_persist.load_config()
            _cfg_p["session_pilot_preset"] = "Game Session"
            config_persist.save_config(_cfg_p)
        except Exception as exc:
            fail(f"pilot preset setup raised: {exc!r}")
            return
        try:
            # Phase-5 source contract: (name, full_path) tuples — the
            # pilot's path-verified matching needs both (path "" would
            # also work for a bare-name watchlist entry)
            _spm.default_get_processes = lambda timeout=10: list(_state["procs"][0])
            # deterministic registry: pre-existing game_mode now, plus
            # ultimate_performance once the session "applies" it
            gui.get_tweak_state = lambda: dict(_state["registry"])
            def _cap(tab, tasks, mode="run", quiet=False):
                _state["calls"].append(
                    (tab, mode, sorted(t.key for t in tasks)))
                # quiet contract: pilot runs must never show a modal
                # scorecard — record the flag so the assert below proves
                # the pilot passed it
                _state.setdefault("quiet_flags", []).append(quiet)
            app.run_tasks = _cap
            # Build the pilot the same way _pilot_start() would (_pilot_ensure
            # is the exact same object-construction path) but start it ONCE,
            # directly at the fast test interval — never at the 15s
            # production default. Starting-then-stopping-then-restarting the
            # same SessionPilot let the first (slow) thread's very first tick
            # detect the already-faked process and fire on_apply's
            # self.root.after(0, ...) from that background thread at almost
            # the same instant the main thread called .stop()/.start() on the
            # same object — a genuine cross-thread Tk race that occasionally
            # dropped the callback and hung poll_pilot_applied forever.
            app._pilot_ensure()
            assert app._pilot is not None, "pilot object not built"
            assert app._pilot.start(interval_s=0.3) is True
            assert app._pilot_running(), "pilot thread did not start"
        except AssertionError as exc:
            fail(f"pilot start: {exc!r}")
            return
        except Exception as exc:
            fail(f"pilot start raised: {exc!r}")
            return
        root.after(300, poll_pilot_applied)

    def poll_pilot_applied():
        _state["step"] = "poll_pilot_applied"
        calls = _state.get("calls", [])
        runs = [c for c in calls if c[1] == "run"]
        if not runs:
            # Diagnostics: this step has stalled on Windows CI with no
            # local repro despite the queue-based cross-thread fix. Rather
            # than guess again, print WHY every ~3s so the next CI run
            # tells us directly instead of just "it stalled".
            n = _state.get("_pilot_poll_n", 0) + 1
            _state["_pilot_poll_n"] = n
            if n % 10 == 0:
                try:
                    pilot = app._pilot
                    print(f"  [diag] poll_pilot_applied poll {n}: "
                          f"pilot.state={pilot.state!r} "
                          f"running={pilot.running()} "
                          f"thread_alive={pilot._thread.is_alive() if pilot._thread else None} "
                          f"calls={calls!r} "
                          f"evt_q_size={app._pilot_evt_q.qsize() if hasattr(app, '_pilot_evt_q') else 'N/A'} "
                          f"any_open={gui.ThemedModal.any_open()} "
                          f"busy={getattr(app, '_busy', '?')}",
                          flush=True)
                except Exception as exc:
                    print(f"  [diag] poll_pilot_applied poll {n}: "
                          f"diag raised {exc!r}", flush=True)
            root.after(300, poll_pilot_applied)
            return
        try:
            from app.elevation import is_admin as _ia
            from app.tab_presets import PRESETS as _PR
            expect = set(_PR["Tweak"]["Game Session"])
            if not _ia():
                from app.tab_presets import TABS as _TB
                by_key = {t.key: t for t in _TB["Tweak"]}
                expect = {k for k in expect
                          if k in by_key and not by_key[k].admin_required}
            assert runs[0][0] == "Tweak", runs
            assert set(runs[0][2]) == expect, (runs[0][2], sorted(expect))
            assert _state.get("quiet_flags", [None])[0] is True, \
                "pilot apply must pass quiet=True (no modal over a game)"
            print(f"  pilot apply: {len(runs[0][2])} session tasks via engine (quiet) OK",
                  flush=True)
        except AssertionError as exc:
            fail(f"pilot apply: {exc!r}")
            return
        # game exits -> two clean polls -> revert of the FRESH keys only:
        # background_apps (session-applied, non-admin so it runs in both
        # elevated and limited mode) is reverted, game_mode (pre-existing)
        # is left strictly alone. Module-8: intent reflects what actually
        # ran — an admin-only key like ultimate_performance would never have
        # been applied in limited mode, so it must not be expected here.
        _state["registry"] = {"game_mode": True, "background_apps": True}
        _state["procs"][0] = []
        root.after(300, poll_pilot_reverted)

    def poll_pilot_reverted():
        _state["step"] = "poll_pilot_reverted"
        calls = _state.get("calls", [])
        revs = [c for c in calls if c[1] == "revert"]
        if not revs:
            root.after(300, poll_pilot_reverted)
            return
        try:
            assert revs[0][0] == "Tweak", revs
            assert set(revs[0][2]) == {"background_apps"}, revs
            print("  pilot revert: fresh-only keys via engine OK", flush=True)
        except AssertionError as exc:
            fail(f"pilot revert: {exc!r}")
            return
        except Exception as exc:
            fail(f"pilot revert raised: {exc!r}")
            return
        finally:
            try:
                _spm = _state.pop("spm")
                _spm.default_get_processes = _state.pop("orig_procs")
                gui.get_tweak_state = _state.pop("orig_state")
                app.run_tasks = _state.pop("orig_run")
                _cfg_r = config_persist.load_config()
                _cfg_r["session_pilot_preset"] = _state.pop(
                    "orig_preset", "Game Session")
                config_persist.save_config(_cfg_r)
                app._pilot_stop()
            except Exception:
                pass
        root.after(400, step_health_report)

    # ---- step 7: health report streams live cards, cancel is safe ------ #
    # Full scans (DISM + junk walk) take minutes — this phase proves the
    # risky machinery instead: real worker start, live card-paint hops
    # under mainloop, then close mid-scan (worker must stop, no crash,
    # no wedge). Sensor LOGIC is covered by sync stub tests.
    def step_health_report():
        _state["step"] = "health_report"
        try:
            _state["hd"] = gui.HealthReportDialog(root, app)
        except Exception as exc:
            fail(f"health dialog build raised: {exc!r}")
            return
        root.after(150, poll_health_cards)

    def poll_health_cards():
        _state["step"] = "poll_health_cards"
        dlg = _state.get("hd")
        try:
            assert dlg is not None and dlg._dlg.winfo_exists()
        except AssertionError as exc:
            fail(f"health dialog vanished: {exc!r}")
            return
        try:
            cards = getattr(dlg, "_cards", {}) or {}
        except Exception:
            cards = {}
        # instant sensors (disk + reboot-folded windows note path) land
        # in seconds; GPU/SMART PowerShell calls follow. Two painted
        # cards prove the worker->paint pipeline end-to-end.
        if len(cards) < 2:
            root.after(500, poll_health_cards)
            return
        try:
            for key, card in cards.items():
                assert isinstance(card, dict), (key, card)
                assert card.get("grade", "?") in "ABCDF?", (key, card)
                assert card.get("headline"), (key, card)
            print(f"  health report: {len(cards)} live cards streamed OK "
                  f"({sorted(cards)})", flush=True)
        except AssertionError as exc:
            fail(f"health cards: {exc!r}")
            return
        except Exception as exc:
            fail(f"health cards raised: {exc!r}")
            return
        # close MID-SCAN (slow sensors still running): must stop cleanly
        try:
            dlg._close()
            root.update()
        except Exception as exc:
            fail(f"health close raised: {exc!r}")
            return
        print("  health report: mid-scan close safe OK", flush=True)
        restore_and_exit(0)  # prints ALL PASS itself once restore is verified

    def _watchdog_tick():
        """Recurring check (not a one-shot timer): fails only when
        _state['step'] has not CHANGED for WATCHDOG_MS — i.e. a genuine
        stall — instead of when the script's total runtime crosses a
        fixed ceiling. A step that's still legitimately working (a real
        disk scan, a real DNS probe) keeps the clock reset just by
        progressing; only a step that never advances trips this."""
        import time as _t
        if _state.get("failed"):
            return
        now = _t.monotonic()
        step = _state.get("step")
        if step != _state.get("_wd_step"):
            _state["_wd_step"] = step
            _state["_wd_since"] = now
        elif now - _state.get("_wd_since", now) > WATCHDOG_MS / 1000.0:
            fail(f"watchdog timeout — step {step!r} stalled")
            return
        root.after(1000, _watchdog_tick)

    root.after(1000, _watchdog_tick)
    root.after(400, step_run)   # let construction + prewarm settle first
    root.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
