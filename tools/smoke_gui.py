"""Headless smoke test for the Phase 2 GUI — driver (H16).

Drives the REAL Application class through its real init flow:
  * If the process is non-admin (normal case), the admin gate shows first,
    and _build_main_ui only runs after _continue_limited() — reproducing
    the exact flow the previous session's test missed.
  * Then, area by area, clicks every tab, selects every preset, enters Custom
    and Undo, and exercises the status/progress/log paths.

This file used to hold all of it — a 2,900-line main() carrying ~400 checks.
H16 moved the section bodies into tools/smoke/<area>.py VERBATIM, taking their
original section banners with them so the old structure stays greppable. What
is left here is the part that cannot move: the harness checks, the Tk root,
the admin gate, and the pass/fail report.

The areas are NOT independent. They share one live Application, and
tools.smoke.AREAS fixes the order they must run in. Read
tools/smoke/__init__.py before adding one.

Run:  python -m tools.smoke_gui
      python -m tools.smoke_gui --only dns --verbose   (one area, while iterating)
"""
import copy
import sys
import pathlib

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from app import config_persist
from app import elevated_launch as _el_gate
from app.elevation import is_admin

def _save_config_verified(expected):
    """Write {key: value} pairs to the user config with retries, then VERIFY
    by re-reading from disk (bypassing the in-memory cache). Returns True
    only when the file provably holds every pair. Never raises (finally-block
    safe) — callers record False into `results` instead.

    Hardening: transient os.replace locks (AV/indexer) can fail save_config
    AFTER a complete tmp was written — a bare save in a finally block then
    reports success while user data stays modified (proven by a real leftover
    tmp + un-restored selection in this environment). Retry, re-read, prove.
    A None expected value means 'key was absent pre-suite': the disk loader
    coerces those to DEFAULT_CONFIG, so compare against the default."""
    import time as _t
    try:
        from app import config_persist as _cp
    except Exception:
        return False
    last = None
    for _ in range(5):
        try:
            cfg = _cp.load_config()
            for _k, _v in expected.items():
                cfg[_k] = _v
            _cp.save_config(cfg)
            _cp._config_cache = None  # force disk re-read for verification
            check_cfg = _cp.load_config()
            ok = True
            for _k, _v in expected.items():
                want = _cp.DEFAULT_CONFIG.get(_k, _v) if _v is None else _v
                if check_cfg.get(_k) != want:
                    ok = False
                    last = f"key {_k!r} not restored"
                    break
            if ok:
                return True
        except Exception as exc:
            last = exc
        try:
            _t.sleep(0.3)
        except Exception:
            pass
    return False


def run_checks():
    """Audit fix: check() had ZERO call sites — `results` was always empty,
    so 'ALL PASS' printed unconditionally (vacuous). These checks exercise
    the pieces that don't need a display; the interactive asserts below
    still run inline (and crash loudly on regression, which is fine)."""
    import tkinter
    check("tkinter importable", lambda: tkinter.Tk().destroy())
    check("tab_presets validates", lambda: __import__("app.tab_presets", fromlist=["x"]))
    from app import config_persist
    def _migration():
        legacy = {"selected_tasks": {"Clean": [], "Tweak": [], "Games": "gamer_launchers"}}
        out = config_persist._migrate_selected_tasks(legacy)
        assert out["selected_tasks"]["Clean"] == ["launcher_cache"], out
    check("migration folds string lists correctly", _migration)
    from app.warnings import check_dangerous_combos
    def _warning_anchor():
        assert not check_dangerous_combos(["disable_nagle", "network_throttling"])
        assert check_dangerous_combos(["network_reset", "disable_nagle"])
    check("warnings anchor logic", _warning_anchor)
    from app.utils import format_bytes
    def _fmt():
        assert format_bytes(1023.6) == "1.00 KB", format_bytes(1023.6)
        assert format_bytes(2.5) == "3 Bytes", format_bytes(2.5)
        assert format_bytes(-5) == "0 Bytes"
    check("format_bytes boundaries", _fmt)
    from app import tab_presets as _tp
    def _task_lists():
        for tab in ("Clean", "Repair", "Tweak", "Install"):
            assert _tp.TABS[tab], f"{tab} empty"
        clean_keys = {t.key for t in _tp.TABS["Clean"]}
        assert {"backup_saves", "winget_cache"} <= clean_keys, "new Clean tasks missing"
        repair_keys = {t.key for t in _tp.TABS["Repair"]}
        assert {"restart_bluetooth", "arp_flush", "gpu_reset", "anticheat_repair"} <= repair_keys
        tweak_keys = {t.key for t in _tp.TABS["Tweak"]}
        assert {"eee_disable", "ntfs_8dot3", "dynamic_tick_off"} <= tweak_keys
    check("new tasks registered", _task_lists)
    def _essentials_icons():
        # every Essentials/LTSC task label must resolve to an icon file
        # via _row_icon's normalization (spacer-only rows regressed once)
        import os, re
        from app.tasks import install_tasks as _it
        missing = []
        for t in _it.TASKS:
            base = re.sub(r"\s*\(.*?\)", "", str(t.label))
            base = re.sub(r"[^a-z0-9]", "", base.lower()) + ".png"
            if not os.path.isfile(os.path.join("app/assets/apps", base)):
                missing.append((t.label, base))
        assert not missing, f"Essentials tasks without icons: {missing}"
    check("essentials icons resolve", _essentials_icons)
    def _com_call_shape():
        # _mm_vfn(ct, iface, ...) — a dropped leading _ct once turned
        # every vtable call into garbage reads (silent [] everywhere).
        # Static audit: every call site must pass _ct first (same or
        # next line — multi-line calls put it after the paren).
        import re
        src = open("app/gui.py", encoding="utf-8").read()
        bad = []
        for m in re.finditer(r"_mm_vfn\(", src):
            if src[max(0, m.start() - 4):m.start()] == "def ":
                continue
            tail = src[m.end():m.end() + 120].lstrip()
            if not (tail.startswith("_ct,") or tail.startswith("_ct)")):
                bad.append(src[:m.start()].count("\n") + 1)
        assert not bad, f"_mm_vfn calls missing _ct at lines {bad}"
    check("COM vtable call shape", _com_call_shape)
    def _junction_guard():
        # re-execute the audit's empirical probe, read-only this time:
        # _is_reparse_point must at least identify real junctions
        import os, tempfile
        from app.utils import _is_reparse_point
        tmp = tempfile.mkdtemp(prefix="jn_probe_")
        target = os.path.join(tmp, "t"); os.makedirs(target, exist_ok=True)
        jn = os.path.join(tmp, "jn")
        try:
            import subprocess
            subprocess.run(["cmd", "/c", f"mklink /J \"{jn}\" \"{target}\""],
                           capture_output=True, timeout=10)
            if os.path.exists(jn):  # junction created
                assert _is_reparse_point(jn), "junction not detected"
                assert not _is_reparse_point(target), "plain dir flagged"
            # else: junction creation unavailable — skip silently
        finally:
            import shutil
            shutil.rmtree(tmp, ignore_errors=True)
    check("junction guard detects reparse points", _junction_guard)
    def _single_instance():
        # hermetic by construction: unique names per run (pid + random),
        # always released in finally — can never collide with a real
        # running app or leak a kernel object. Kernel-only, no Tk/shell.
        import os as _os
        import random as _rnd
        import sys as _sys
        from app import single_instance as _si
        # exemptions are pure-python: always asserted, any platform
        assert _si.should_enforce([]) is True
        assert _si.should_enforce(["app", "--elevation-token=abc"]) is False
        assert _si.is_elevation_child(["app", "--elevation-token=abc"]) is True
        assert _si.should_enforce(["app", "--auto-clean"]) is False
        assert _si.should_enforce(["app", "--auto-update"]) is False
        assert _si.is_headless_run(["app", "--auto-clean"]) is True
        assert _si.should_enforce(["app", "--no-single-instance"]) is False
        if not _sys.platform.startswith("win"):
            return
        _tag = f"test_{_os.getpid()}_{_rnd.randrange(1 << 30)}"
        _mname, _ename = f"si_mutex_{_tag}", f"si_evt_{_tag}"
        _h1 = _si.acquire(_mname)
        try:
            assert _h1 is not None, "first acquire failed"
            assert _si.acquire(_mname) is None, "second acquire must lose"
            # show-event loopback on an isolated name (never the real one)
            _eh = _si.ensure_show_event(_ename)
            assert _eh is not None, "event create failed"
            try:
                assert _si.check_signalled(_ename) is False, "fresh event must be quiet"
                assert _si.signal_existing(_ename) is True, "signal failed"
                assert _si.check_signalled(_ename) is True, "pulse lost"
                assert _si.check_signalled(_ename) is False, "auto-reset did not drain"
            finally:
                try:
                    from app.single_instance import _kernel32 as _k
                    _k().CloseHandle(_eh)
                except Exception:
                    pass
        finally:
            _si.release(_h1)
        # released handle is acquirable again (no leak, no ghost owner)
        _h2 = _si.acquire(_mname)
        try:
            assert _h2 is not None, "re-acquire after release failed"
        finally:
            _si.release(_h2)
    check("single-instance mutex + exemptions + loopback", _single_instance)
    def _auto_elevate():
        # builders are pure (no schtasks writes); the only live call is a
        # read-only /Query probe asserting types, never state.
        import subprocess as _sp
        from app import elevated_launch as _el
        assert _el.TASK_NAME == "CleanerTool_ElevatedLaunch"
        _spaced = "C:\\Program Files\\Cleaner Tool\\Cleaner-Tool.exe"
        _cmd = _el.build_create_cmd(exe_override=_spaced)
        assert _cmd[0].lower().endswith("schtasks.exe"), _cmd[0]
        assert "/RL" in _cmd and "HIGHEST" in _cmd, "must run elevated"
        assert "/IT" in _cmd, "must stay interactive (visible GUI, same user)"
        assert "/SC" in _cmd and "ONCE" in _cmd, "must be on-demand only"
        _tr = _cmd[_cmd.index("/TR") + 1]
        assert _tr == _sp.list2cmdline([_spaced]), f"/TR quoting broken: {_tr!r}"
        assert _el.build_run_cmd()[1:3] == ["/Run", "/TN"]
        assert _el.build_delete_cmd()[1:3] == ["/Delete", "/TN"]
        # query-parse on synthetic output (never the live service)
        _fake = ('Folder: \\\r\nTaskName: \\CleanerTool_ElevatedLaunch\r\n'
                 'Task To Run: "C:\\Tools\\Cleaner-Tool.exe"\r\nStatus: Ready\r\n')
        assert _el.parse_task_run(_fake) == '"C:\\Tools\\Cleaner-Tool.exe"'
        assert _el.parse_task_run("no such task") is None
        assert _el.parse_task_run("") is None
        # Gate-skip decision is pure over injected tables
        class _T:
            def __init__(self, key, adm):
                self.key = key
                self.admin_required = adm
        _tabs = {"Clean": [_T("a", False)], "Tweak": [_T("b", True)]}
        assert _el.selection_needs_admin({"Clean": ["a"]}, _tabs) is False
        assert _el.selection_needs_admin({"Tweak": ["b"]}, _tabs) is True
        assert _el.selection_needs_admin({"Tweak": ["ghost"]}, _tabs) is False
        assert _el.selection_needs_admin({}, _tabs) is False
        # prefs round-trip with exact restore (never leak harness marks)
        try:
            _cfg0 = config_persist.load_config()
            _a0 = bool(_cfg0.get("auto_elevate", False))
            _r0 = bool(_cfg0.get("remember_limited", False))
        except Exception:
            _a0, _r0 = False, False
        try:
            _el.save_gate_prefs(True, False)
            config_persist._config_cache = None
            assert config_persist.load_config().get("auto_elevate") is True
            _el.save_gate_prefs(False, True)
            config_persist._config_cache = None
            _c = config_persist.load_config()
            assert _c.get("auto_elevate") is False and _c.get("remember_limited") is True
            # handoff pre-check degrades honestly with no task present
            _ready, _why = _el.handoff_ready()
            assert isinstance(_ready, bool) and isinstance(_why, str)
            assert _el.notice_for_gate() is None or isinstance(_el.notice_for_gate(), str)
        finally:
            _el.save_gate_prefs(_a0, _r0)
            config_persist._config_cache = None
        # live read-only probe: types only (True/False regardless of the
        # machine's scheduler state — asserts plumbing, never outcome)
        _ex, _match, _out = _el.status()
        assert isinstance(_ex, bool) and isinstance(_match, bool) and isinstance(_out, str)
    check("auto-elevate builders + gate prefs + probe", _auto_elevate)
    def _tray():
        # hermetic: private message-only flow is untestable headless, so
        # this covers the contract surface — unique tooltip per run, icon
        # lifecycle (add -> tooltip -> stop, always released), routing via
        # fire() (no synthesized clicks), and the Application wiring.
        import sys as _sys
        if not _sys.platform.startswith("win"):
            return
        from app import tray as _tm
        _seen = []
        _icon = _tm.TrayIcon(
            tooltip=f"CleanerTool smoke {__import__('os').getpid()}",
            items=[(5001, "Open", lambda: _seen.append("open")),
                   (None, None, None),
                   (5002, "Quit", lambda: _seen.append("quit"))],
            on_ui_thread=None)
        try:
            _started = _icon.start()
            if not _started:
                # no shell (exotic runner): stop() must still be safe,
                # then soft-pass — the lifecycle below needs a real tray.
                print("  tray: no shell, lifecycle skipped (stop-safe only)",
                      flush=True)
                _icon.stop()
                _icon.stop()
                return
            assert _icon.running is True
            assert _icon.update_tooltip("probe") is True
            assert _icon.fire(5001) is True and _seen == ["open"]
            assert _icon.fire(9999) is False
            assert _icon.fire(5002) is True and _seen == ["open", "quit"]
        finally:
            _icon.stop()
            _icon.stop()  # idempotent
        assert _icon.running is False
        # Application wiring: actions exist, quiet overrides keep defaults
        from app import gui as _gui
        import inspect as _insp
        for _m in ("tray_open", "tray_quit", "tray_toggle_game_session",
                   "tray_quick_clean", "tray_check_ping", "tray_speed_test",
                   "_ensure_tray", "_hide_to_tray", "_stop_tray",
                   "_maybe_hide_for_tray"):
            assert callable(getattr(_gui.Application, _m, None)), f"missing {_m}"
        assert "quiet" in _insp.signature(_gui.Application.start_game_night).parameters
        assert "quiet" in _insp.signature(_gui.Application.end_game_night).parameters
    check("tray lifecycle + routing + wiring", _tray)
    def _startup_tray():
        # Phase 5 (startup tray): pure builders + read-only probe only —
        # never a live /Create or /Delete here, same discipline as the
        # auto-elevate check above (CleanerTool_StartupTray is a fixed
        # global task name; a live write would leave CI's scheduler
        # state dirty). enable_/disable_startup_tray themselves are
        # exercised manually per the Phase 2-5 QA matrix, not headlessly.
        import subprocess as _sp
        from app import scheduler as _sch
        assert _sch.TASK_STARTUP == "CleanerTool_StartupTray"
        _spaced = "C:\\Program Files\\Cleaner Tool\\Cleaner-Tool.exe"
        _cmd = _sch._build_startup_cmd(exe_override=_spaced)
        assert _cmd[0].lower().endswith("schtasks.exe"), _cmd[0]
        assert "/Create" in _cmd and "/TN" in _cmd and _sch.TASK_STARTUP in _cmd
        assert "/SC" in _cmd and "ONLOGON" in _cmd, "must be a logon task"
        assert "/IT" in _cmd, "must stay interactive (visible tray, same user)"
        # deliberately standard-rights: a boot-time /RL HIGHEST would be a
        # permanently-elevated desktop process, which the design notes
        # in scheduler.py explicitly reject.
        assert "/RL" not in _cmd, "startup tray task must not request HIGHEST"
        _tr = _cmd[_cmd.index("/TR") + 1]
        assert _tr == _sp.list2cmdline([_spaced, "--tray"]), f"/TR quoting broken: {_tr!r}"
        assert _sch._build_startup_delete_cmd()[1:3] == ["/Delete", "/TN"]
        assert _sch._build_startup_query_cmd()[1:3] == ["/Query", "/TN"]
        # query-parse on synthetic output — matches, mismatches, and the
        # "no such row" case all resolve without touching a real task
        _want = _sch._want_startup_tr(_spaced)
        _fake_match = f'Folder: \\\r\nTaskName: \\{_sch.TASK_STARTUP}\r\nTask To Run: {_want}\r\nStatus: Ready\r\n'
        _fake_stale = ('Folder: \\\r\nTaskName: \\CleanerTool_StartupTray\r\n'
                       'Task To Run: "C:\\Old\\Cleaner-Tool.exe" --tray\r\nStatus: Ready\r\n')
        assert _sch._startup_tr_matches(_fake_match, _spaced) is True
        assert _sch._startup_tr_matches(_fake_stale, _spaced) is False
        assert _sch._startup_tr_matches("no such task", _spaced) is None
        assert _sch._startup_tr_matches("", _spaced) is None
        # flag round-trip with exact restore (never leak a harness mark
        # into the user's real config, same contract as auto_elevate above)
        try:
            _t0 = bool(config_persist.load_config().get("startup_tray_enabled", False))
        except Exception:
            _t0 = False
        try:
            _sch._set_startup_flag(True)
            config_persist._config_cache = None
            assert config_persist.load_config().get("startup_tray_enabled") is True
            _sch._set_startup_flag(False)
            config_persist._config_cache = None
            assert config_persist.load_config().get("startup_tray_enabled") is False
        finally:
            _sch._set_startup_flag(_t0)
            config_persist._config_cache = None
        # live read-only probe only (types, never outcome — the CI runner
        # almost certainly has no such task, and that's fine either way)
        _ok, _out = _sch.get_startup_status()
        assert isinstance(_ok, bool) and isinstance(_out, str)
        # resync is itself read-probe + flag-write; restore afterward
        try:
            _live = _sch.resync_startup_flag()
            assert isinstance(_live, bool)
        finally:
            _sch._set_startup_flag(_t0)
            config_persist._config_cache = None
        # GUI wiring: the Auto Maintenance dialog's checkbox handlers exist
        from app import scheduler as _sch2
        for _f in ("enable_startup_tray", "disable_startup_tray", "resync_startup_flag"):
            assert callable(getattr(_sch2, _f, None)), f"missing {_f}"
    check("startup tray builders + probe + flag round-trip", _startup_tray)
    def _maintenance_schedule():
        # Original Auto Maintenance scheduler (predates Phase 5, never had
        # builder-level coverage): pure builders + validation + read-only
        # probes only. enable_/disable_schedule themselves touch real
        # schtasks state under TASK_NAME, so — same discipline as the
        # auto-elevate and startup-tray checks above — this exercises the
        # command shape and the fail-fast validation path (which returns
        # False before ever calling schtasks), never a live /Create.
        import datetime as _dt
        from app import scheduler as _sch
        # frequency/time validation (F14: typos used to silently -> WEEKLY)
        assert _sch._validate_frequency("Daily") == "daily"
        assert _sch._validate_frequency(" WEEKLY ") == "weekly"
        for _bad in ("fortnightly", "", None, "dailyish"):
            try:
                _sch._validate_frequency(_bad)
                assert False, f"expected ValueError for {_bad!r}"
            except ValueError:
                pass
        assert _sch._validate_time_str("03:00") == "03:00"
        assert _sch._validate_time_str("23:59") == "23:59"
        for _bad in ("3:00", "24:00", "12:60", "noon", ""):
            try:
                _sch._validate_time_str(_bad)
                assert False, f"expected ValueError for {_bad!r}"
            except ValueError:
                pass
        # /D pinning: locale-independent weekday tokens, month clamped to
        # 28 so /D never silently skips a short month (schtasks quirk)
        _mon = _dt.date(2024, 1, 1)   # known Monday
        _sun = _dt.date(2024, 1, 7)   # known Sunday
        assert _sch._schedule_day_args("WEEKLY", _mon) == ["/D", "MON"]
        assert _sch._schedule_day_args("WEEKLY", _sun) == ["/D", "SUN"]
        assert _sch._schedule_day_args("MONTHLY", _dt.date(2024, 1, 31)) == ["/D", "28"]
        assert _sch._schedule_day_args("MONTHLY", _dt.date(2024, 1, 15)) == ["/D", "15"]
        assert _sch._schedule_day_args("DAILY", _mon) == []
        # full command shape, injected `today` for determinism
        _cmd = _sch._build_schtasks_cmd("weekly", "03:00", today=_mon)
        assert _cmd[0].lower().endswith("schtasks.exe"), _cmd[0]
        assert "/Create" in _cmd and "/TN" in _cmd
        assert _cmd[_cmd.index("/TN") + 1] == _sch.TASK_NAME
        assert "/SC" in _cmd and "WEEKLY" in _cmd
        assert _cmd[_cmd.index("/ST") + 1] == "03:00"
        assert "/D" in _cmd and _cmd[_cmd.index("/D") + 1] == "MON"
        assert "/F" in _cmd and "/IT" in _cmd
        # H1 fix: /RL HIGHEST only when the CREATING process already runs
        # elevated — never requested unconditionally (that would make
        # non-admin users' Enable silently fail with Access Denied).
        assert ("/RL" in _cmd) == bool(is_admin())
        # a distinct task name per schedule kind (--auto-update coexists
        # with the default clean/repair task rather than overwriting it)
        _cmd_upd = _sch._build_schtasks_cmd("daily", "04:30", extra_args=["--auto-update"], today=_mon)
        assert _cmd_upd[_cmd_upd.index("/TN") + 1] == _sch.TASK_NAME + "_auto_update"
        assert "/D" not in _cmd_upd, "DAILY must not carry a /D"
        # invalid input fails fast and never reaches schtasks
        assert _sch._build_schtasks_cmd_str("weekly", "03:00", today=_mon)  # legacy string form is quoted, non-empty
        _ok_bad, _msg_bad = _sch.enable_schedule("never", "03:00")
        assert _ok_bad is False and "Unknown schedule frequency" in _msg_bad
        _ok_bad2, _msg_bad2 = _sch.enable_schedule("daily", "3pm")
        assert _ok_bad2 is False and "schedule time" in _msg_bad2
        # live read-only probes only (types, never outcome)
        _ok, _out = _sch.get_schedule_status()
        assert isinstance(_ok, bool) and isinstance(_out, str)
        _ok_upd, _out_upd = _sch.get_schedule_status(["--auto-update"])
        assert isinstance(_ok_upd, bool) and isinstance(_out_upd, str)
        # resync is read-probe + single-lock flag-write; restore after
        try:
            _cfg0 = config_persist.load_config()
            _s0 = bool(_cfg0.get("schedule_enabled", False))
            _u0 = bool(_cfg0.get("schedule_update_enabled", False))
        except Exception:
            _s0, _u0 = False, False
        try:
            _live = _sch.resync_schedule_flag()
            assert isinstance(_live, bool)
            _live_upd = _sch.resync_schedule_flag(["--auto-update"])
            assert isinstance(_live_upd, bool)
        finally:
            def _restore(cfg, _s0=_s0, _u0=_u0):
                cfg["schedule_enabled"] = _s0
                cfg["schedule_update_enabled"] = _u0
            from app.config_persist import update_config as _upd
            try:
                _upd(_restore)
            except Exception:
                pass
            config_persist._config_cache = None
    check("maintenance schedule builders + validation + probe", _maintenance_schedule)
    def _tray_icon_path():
        # Cross-platform on purpose: this is exactly the part of icon
        # loading that has no ctypes in it, and the part that broke once
        # already (a stray `os.path...` instead of `_os.path...` inside a
        # broad except — NameError, silently swallowed, tray fell back to
        # the plain stock icon on every run). Runs on every OS/CI, not
        # just windows-latest, so this class of bug can't ship silently
        # again regardless of which runner catches it first.
        import os as _os
        from app import tray as _tm
        icon = _tm.TrayIcon(icon_path=None)
        cands = icon._icon_candidates()
        assert cands, "no icon candidates at all — tray will always fall back to the stock icon"
        bundled = [c for c in cands if c.replace("\\", "/").endswith("assets/icon.ico")]
        assert bundled, f"bundled icon.ico missing from candidates: {cands}"
        assert _os.path.isfile(bundled[0]), f"candidate path does not exist on disk: {bundled[0]}"
        # an explicit icon_path is tried first
        icon2 = _tm.TrayIcon(icon_path="/some/override.ico")
        assert icon2._icon_candidates()[0] == "/some/override.ico"
    check("tray icon candidate paths resolve to a real file", _tray_icon_path)


from tools import smoke as _smoke
from tools.smoke import AREAS, Ctx, check, results, run_all


def _save_config_verified(expected):
    """Write {key: value} pairs to the user config with retries, then VERIFY
    by re-reading from disk (bypassing the in-memory cache). Returns True
    only when the file provably holds every pair. Never raises (finally-block
    safe) — callers record False into `results` instead.

    Hardening: transient os.replace locks (AV/indexer) can fail save_config
    AFTER a complete tmp was written — a bare save in a finally block then
    reports success while user data stays modified (proven by a real leftover
    tmp + un-restored selection in this environment). Retry, re-read, prove.
    A None expected value means 'key was absent pre-suite': the disk loader
    coerces those to DEFAULT_CONFIG, so compare against the default."""
    import time as _t
    try:
        from app import config_persist as _cp
    except Exception:
        return False
    last = None
    for _ in range(5):
        try:
            cfg = _cp.load_config()
            for _k, _v in expected.items():
                cfg[_k] = _v
            _cp.save_config(cfg)
            _cp._config_cache = None  # force disk re-read for verification
            check_cfg = _cp.load_config()
            ok = True
            for _k, _v in expected.items():
                want = _cp.DEFAULT_CONFIG.get(_k, _v) if _v is None else _v
                if check_cfg.get(_k) != want:
                    ok = False
                    last = f"key {_k!r} not restored"
                    break
            if ok:
                return True
        except Exception as exc:
            last = exc
        try:
            _t.sleep(0.3)
        except Exception:
            pass
    return False


def run_checks():
    """Audit fix: check() had ZERO call sites — `results` was always empty,
    so 'ALL PASS' printed unconditionally (vacuous). These checks exercise
    the pieces that don't need a display; the interactive asserts below
    still run inline (and crash loudly on regression, which is fine)."""
    import tkinter
    check("tkinter importable", lambda: tkinter.Tk().destroy())
    check("tab_presets validates", lambda: __import__("app.tab_presets", fromlist=["x"]))
    from app import config_persist
    def _migration():
        legacy = {"selected_tasks": {"Clean": [], "Tweak": [], "Games": "gamer_launchers"}}
        out = config_persist._migrate_selected_tasks(legacy)
        assert out["selected_tasks"]["Clean"] == ["launcher_cache"], out
    check("migration folds string lists correctly", _migration)
    from app.warnings import check_dangerous_combos
    def _warning_anchor():
        assert not check_dangerous_combos(["disable_nagle", "network_throttling"])
        assert check_dangerous_combos(["network_reset", "disable_nagle"])
    check("warnings anchor logic", _warning_anchor)
    from app.utils import format_bytes
    def _fmt():
        assert format_bytes(1023.6) == "1.00 KB", format_bytes(1023.6)
        assert format_bytes(2.5) == "3 Bytes", format_bytes(2.5)
        assert format_bytes(-5) == "0 Bytes"
    check("format_bytes boundaries", _fmt)
    from app import tab_presets as _tp
    def _task_lists():
        for tab in ("Clean", "Repair", "Tweak", "Install"):
            assert _tp.TABS[tab], f"{tab} empty"
        clean_keys = {t.key for t in _tp.TABS["Clean"]}
        assert {"backup_saves", "winget_cache"} <= clean_keys, "new Clean tasks missing"
        repair_keys = {t.key for t in _tp.TABS["Repair"]}
        assert {"restart_bluetooth", "arp_flush", "gpu_reset", "anticheat_repair"} <= repair_keys
        tweak_keys = {t.key for t in _tp.TABS["Tweak"]}
        assert {"eee_disable", "ntfs_8dot3", "dynamic_tick_off"} <= tweak_keys
    check("new tasks registered", _task_lists)
    def _essentials_icons():
        # every Essentials/LTSC task label must resolve to an icon file
        # via _row_icon's normalization (spacer-only rows regressed once)
        import os, re
        from app.tasks import install_tasks as _it
        missing = []
        for t in _it.TASKS:
            base = re.sub(r"\s*\(.*?\)", "", str(t.label))
            base = re.sub(r"[^a-z0-9]", "", base.lower()) + ".png"
            if not os.path.isfile(os.path.join("app/assets/apps", base)):
                missing.append((t.label, base))
        assert not missing, f"Essentials tasks without icons: {missing}"
    check("essentials icons resolve", _essentials_icons)
    def _com_call_shape():
        # _mm_vfn(ct, iface, ...) — a dropped leading _ct once turned
        # every vtable call into garbage reads (silent [] everywhere).
        # Static audit: every call site must pass _ct first (same or
        # next line — multi-line calls put it after the paren).
        import re
        src = open("app/gui.py", encoding="utf-8").read()
        bad = []
        for m in re.finditer(r"_mm_vfn\(", src):
            if src[max(0, m.start() - 4):m.start()] == "def ":
                continue
            tail = src[m.end():m.end() + 120].lstrip()
            if not (tail.startswith("_ct,") or tail.startswith("_ct)")):
                bad.append(src[:m.start()].count("\n") + 1)
        assert not bad, f"_mm_vfn calls missing _ct at lines {bad}"
    check("COM vtable call shape", _com_call_shape)
    def _junction_guard():
        # re-execute the audit's empirical probe, read-only this time:
        # _is_reparse_point must at least identify real junctions
        import os, tempfile
        from app.utils import _is_reparse_point
        tmp = tempfile.mkdtemp(prefix="jn_probe_")
        target = os.path.join(tmp, "t"); os.makedirs(target, exist_ok=True)
        jn = os.path.join(tmp, "jn")
        try:
            import subprocess
            subprocess.run(["cmd", "/c", f"mklink /J \"{jn}\" \"{target}\""],
                           capture_output=True, timeout=10)
            if os.path.exists(jn):  # junction created
                assert _is_reparse_point(jn), "junction not detected"
                assert not _is_reparse_point(target), "plain dir flagged"
            # else: junction creation unavailable — skip silently
        finally:
            import shutil
            shutil.rmtree(tmp, ignore_errors=True)
    check("junction guard detects reparse points", _junction_guard)
    def _single_instance():
        # hermetic by construction: unique names per run (pid + random),
        # always released in finally — can never collide with a real
        # running app or leak a kernel object. Kernel-only, no Tk/shell.
        import os as _os
        import random as _rnd
        import sys as _sys
        from app import single_instance as _si
        # exemptions are pure-python: always asserted, any platform
        assert _si.should_enforce([]) is True
        assert _si.should_enforce(["app", "--elevation-token=abc"]) is False
        assert _si.is_elevation_child(["app", "--elevation-token=abc"]) is True
        assert _si.should_enforce(["app", "--auto-clean"]) is False
        assert _si.should_enforce(["app", "--auto-update"]) is False
        assert _si.is_headless_run(["app", "--auto-clean"]) is True
        assert _si.should_enforce(["app", "--no-single-instance"]) is False
        if not _sys.platform.startswith("win"):
            return
        _tag = f"test_{_os.getpid()}_{_rnd.randrange(1 << 30)}"
        _mname, _ename = f"si_mutex_{_tag}", f"si_evt_{_tag}"
        _h1 = _si.acquire(_mname)
        try:
            assert _h1 is not None, "first acquire failed"
            assert _si.acquire(_mname) is None, "second acquire must lose"
            # show-event loopback on an isolated name (never the real one)
            _eh = _si.ensure_show_event(_ename)
            assert _eh is not None, "event create failed"
            try:
                assert _si.check_signalled(_ename) is False, "fresh event must be quiet"
                assert _si.signal_existing(_ename) is True, "signal failed"
                assert _si.check_signalled(_ename) is True, "pulse lost"
                assert _si.check_signalled(_ename) is False, "auto-reset did not drain"
            finally:
                try:
                    from app.single_instance import _kernel32 as _k
                    _k().CloseHandle(_eh)
                except Exception:
                    pass
        finally:
            _si.release(_h1)
        # released handle is acquirable again (no leak, no ghost owner)
        _h2 = _si.acquire(_mname)
        try:
            assert _h2 is not None, "re-acquire after release failed"
        finally:
            _si.release(_h2)
    check("single-instance mutex + exemptions + loopback", _single_instance)
    def _auto_elevate():
        # builders are pure (no schtasks writes); the only live call is a
        # read-only /Query probe asserting types, never state.
        import subprocess as _sp
        from app import elevated_launch as _el
        assert _el.TASK_NAME == "CleanerTool_ElevatedLaunch"
        _spaced = "C:\\Program Files\\Cleaner Tool\\Cleaner-Tool.exe"
        _cmd = _el.build_create_cmd(exe_override=_spaced)
        assert _cmd[0].lower().endswith("schtasks.exe"), _cmd[0]
        assert "/RL" in _cmd and "HIGHEST" in _cmd, "must run elevated"
        assert "/IT" in _cmd, "must stay interactive (visible GUI, same user)"
        assert "/SC" in _cmd and "ONCE" in _cmd, "must be on-demand only"
        _tr = _cmd[_cmd.index("/TR") + 1]
        assert _tr == _sp.list2cmdline([_spaced]), f"/TR quoting broken: {_tr!r}"
        assert _el.build_run_cmd()[1:3] == ["/Run", "/TN"]
        assert _el.build_delete_cmd()[1:3] == ["/Delete", "/TN"]
        # query-parse on synthetic output (never the live service)
        _fake = ('Folder: \\\r\nTaskName: \\CleanerTool_ElevatedLaunch\r\n'
                 'Task To Run: "C:\\Tools\\Cleaner-Tool.exe"\r\nStatus: Ready\r\n')
        assert _el.parse_task_run(_fake) == '"C:\\Tools\\Cleaner-Tool.exe"'
        assert _el.parse_task_run("no such task") is None
        assert _el.parse_task_run("") is None
        # Gate-skip decision is pure over injected tables
        class _T:
            def __init__(self, key, adm):
                self.key = key
                self.admin_required = adm
        _tabs = {"Clean": [_T("a", False)], "Tweak": [_T("b", True)]}
        assert _el.selection_needs_admin({"Clean": ["a"]}, _tabs) is False
        assert _el.selection_needs_admin({"Tweak": ["b"]}, _tabs) is True
        assert _el.selection_needs_admin({"Tweak": ["ghost"]}, _tabs) is False
        assert _el.selection_needs_admin({}, _tabs) is False
        # prefs round-trip with exact restore (never leak harness marks)
        try:
            _cfg0 = config_persist.load_config()
            _a0 = bool(_cfg0.get("auto_elevate", False))
            _r0 = bool(_cfg0.get("remember_limited", False))
        except Exception:
            _a0, _r0 = False, False
        try:
            _el.save_gate_prefs(True, False)
            config_persist._config_cache = None
            assert config_persist.load_config().get("auto_elevate") is True
            _el.save_gate_prefs(False, True)
            config_persist._config_cache = None
            _c = config_persist.load_config()
            assert _c.get("auto_elevate") is False and _c.get("remember_limited") is True
            # handoff pre-check degrades honestly with no task present
            _ready, _why = _el.handoff_ready()
            assert isinstance(_ready, bool) and isinstance(_why, str)
            assert _el.notice_for_gate() is None or isinstance(_el.notice_for_gate(), str)
        finally:
            _el.save_gate_prefs(_a0, _r0)
            config_persist._config_cache = None
        # live read-only probe: types only (True/False regardless of the
        # machine's scheduler state — asserts plumbing, never outcome)
        _ex, _match, _out = _el.status()
        assert isinstance(_ex, bool) and isinstance(_match, bool) and isinstance(_out, str)
    check("auto-elevate builders + gate prefs + probe", _auto_elevate)
    def _tray():
        # hermetic: private message-only flow is untestable headless, so
        # this covers the contract surface — unique tooltip per run, icon
        # lifecycle (add -> tooltip -> stop, always released), routing via
        # fire() (no synthesized clicks), and the Application wiring.
        import sys as _sys
        if not _sys.platform.startswith("win"):
            return
        from app import tray as _tm
        _seen = []
        _icon = _tm.TrayIcon(
            tooltip=f"CleanerTool smoke {__import__('os').getpid()}",
            items=[(5001, "Open", lambda: _seen.append("open")),
                   (None, None, None),
                   (5002, "Quit", lambda: _seen.append("quit"))],
            on_ui_thread=None)
        try:
            _started = _icon.start()
            if not _started:
                # no shell (exotic runner): stop() must still be safe,
                # then soft-pass — the lifecycle below needs a real tray.
                print("  tray: no shell, lifecycle skipped (stop-safe only)",
                      flush=True)
                _icon.stop()
                _icon.stop()
                return
            assert _icon.running is True
            assert _icon.update_tooltip("probe") is True
            assert _icon.fire(5001) is True and _seen == ["open"]
            assert _icon.fire(9999) is False
            assert _icon.fire(5002) is True and _seen == ["open", "quit"]
        finally:
            _icon.stop()
            _icon.stop()  # idempotent
        assert _icon.running is False
        # Application wiring: actions exist, quiet overrides keep defaults
        from app import gui as _gui
        import inspect as _insp
        for _m in ("tray_open", "tray_quit", "tray_toggle_game_session",
                   "tray_quick_clean", "tray_check_ping", "tray_speed_test",
                   "_ensure_tray", "_hide_to_tray", "_stop_tray",
                   "_maybe_hide_for_tray"):
            assert callable(getattr(_gui.Application, _m, None)), f"missing {_m}"
        assert "quiet" in _insp.signature(_gui.Application.start_game_night).parameters
        assert "quiet" in _insp.signature(_gui.Application.end_game_night).parameters
    check("tray lifecycle + routing + wiring", _tray)
    def _startup_tray():
        # Phase 5 (startup tray): pure builders + read-only probe only —
        # never a live /Create or /Delete here, same discipline as the
        # auto-elevate check above (CleanerTool_StartupTray is a fixed
        # global task name; a live write would leave CI's scheduler
        # state dirty). enable_/disable_startup_tray themselves are
        # exercised manually per the Phase 2-5 QA matrix, not headlessly.
        import subprocess as _sp
        from app import scheduler as _sch
        assert _sch.TASK_STARTUP == "CleanerTool_StartupTray"
        _spaced = "C:\\Program Files\\Cleaner Tool\\Cleaner-Tool.exe"
        _cmd = _sch._build_startup_cmd(exe_override=_spaced)
        assert _cmd[0].lower().endswith("schtasks.exe"), _cmd[0]
        assert "/Create" in _cmd and "/TN" in _cmd and _sch.TASK_STARTUP in _cmd
        assert "/SC" in _cmd and "ONLOGON" in _cmd, "must be a logon task"
        assert "/IT" in _cmd, "must stay interactive (visible tray, same user)"
        # deliberately standard-rights: a boot-time /RL HIGHEST would be a
        # permanently-elevated desktop process, which the design notes
        # in scheduler.py explicitly reject.
        assert "/RL" not in _cmd, "startup tray task must not request HIGHEST"
        _tr = _cmd[_cmd.index("/TR") + 1]
        assert _tr == _sp.list2cmdline([_spaced, "--tray"]), f"/TR quoting broken: {_tr!r}"
        assert _sch._build_startup_delete_cmd()[1:3] == ["/Delete", "/TN"]
        assert _sch._build_startup_query_cmd()[1:3] == ["/Query", "/TN"]
        # query-parse on synthetic output — matches, mismatches, and the
        # "no such row" case all resolve without touching a real task
        _want = _sch._want_startup_tr(_spaced)
        _fake_match = f'Folder: \\\r\nTaskName: \\{_sch.TASK_STARTUP}\r\nTask To Run: {_want}\r\nStatus: Ready\r\n'
        _fake_stale = ('Folder: \\\r\nTaskName: \\CleanerTool_StartupTray\r\n'
                       'Task To Run: "C:\\Old\\Cleaner-Tool.exe" --tray\r\nStatus: Ready\r\n')
        assert _sch._startup_tr_matches(_fake_match, _spaced) is True
        assert _sch._startup_tr_matches(_fake_stale, _spaced) is False
        assert _sch._startup_tr_matches("no such task", _spaced) is None
        assert _sch._startup_tr_matches("", _spaced) is None
        # flag round-trip with exact restore (never leak a harness mark
        # into the user's real config, same contract as auto_elevate above)
        try:
            _t0 = bool(config_persist.load_config().get("startup_tray_enabled", False))
        except Exception:
            _t0 = False
        try:
            _sch._set_startup_flag(True)
            config_persist._config_cache = None
            assert config_persist.load_config().get("startup_tray_enabled") is True
            _sch._set_startup_flag(False)
            config_persist._config_cache = None
            assert config_persist.load_config().get("startup_tray_enabled") is False
        finally:
            _sch._set_startup_flag(_t0)
            config_persist._config_cache = None
        # live read-only probe only (types, never outcome — the CI runner
        # almost certainly has no such task, and that's fine either way)
        _ok, _out = _sch.get_startup_status()
        assert isinstance(_ok, bool) and isinstance(_out, str)
        # resync is itself read-probe + flag-write; restore afterward
        try:
            _live = _sch.resync_startup_flag()
            assert isinstance(_live, bool)
        finally:
            _sch._set_startup_flag(_t0)
            config_persist._config_cache = None
        # GUI wiring: the Auto Maintenance dialog's checkbox handlers exist
        from app import scheduler as _sch2
        for _f in ("enable_startup_tray", "disable_startup_tray", "resync_startup_flag"):
            assert callable(getattr(_sch2, _f, None)), f"missing {_f}"
    check("startup tray builders + probe + flag round-trip", _startup_tray)
    def _maintenance_schedule():
        # Original Auto Maintenance scheduler (predates Phase 5, never had
        # builder-level coverage): pure builders + validation + read-only
        # probes only. enable_/disable_schedule themselves touch real
        # schtasks state under TASK_NAME, so — same discipline as the
        # auto-elevate and startup-tray checks above — this exercises the
        # command shape and the fail-fast validation path (which returns
        # False before ever calling schtasks), never a live /Create.
        import datetime as _dt
        from app import scheduler as _sch
        # frequency/time validation (F14: typos used to silently -> WEEKLY)
        assert _sch._validate_frequency("Daily") == "daily"
        assert _sch._validate_frequency(" WEEKLY ") == "weekly"
        for _bad in ("fortnightly", "", None, "dailyish"):
            try:
                _sch._validate_frequency(_bad)
                assert False, f"expected ValueError for {_bad!r}"
            except ValueError:
                pass
        assert _sch._validate_time_str("03:00") == "03:00"
        assert _sch._validate_time_str("23:59") == "23:59"
        for _bad in ("3:00", "24:00", "12:60", "noon", ""):
            try:
                _sch._validate_time_str(_bad)
                assert False, f"expected ValueError for {_bad!r}"
            except ValueError:
                pass
        # /D pinning: locale-independent weekday tokens, month clamped to
        # 28 so /D never silently skips a short month (schtasks quirk)
        _mon = _dt.date(2024, 1, 1)   # known Monday
        _sun = _dt.date(2024, 1, 7)   # known Sunday
        assert _sch._schedule_day_args("WEEKLY", _mon) == ["/D", "MON"]
        assert _sch._schedule_day_args("WEEKLY", _sun) == ["/D", "SUN"]
        assert _sch._schedule_day_args("MONTHLY", _dt.date(2024, 1, 31)) == ["/D", "28"]
        assert _sch._schedule_day_args("MONTHLY", _dt.date(2024, 1, 15)) == ["/D", "15"]
        assert _sch._schedule_day_args("DAILY", _mon) == []
        # full command shape, injected `today` for determinism
        _cmd = _sch._build_schtasks_cmd("weekly", "03:00", today=_mon)
        assert _cmd[0].lower().endswith("schtasks.exe"), _cmd[0]
        assert "/Create" in _cmd and "/TN" in _cmd
        assert _cmd[_cmd.index("/TN") + 1] == _sch.TASK_NAME
        assert "/SC" in _cmd and "WEEKLY" in _cmd
        assert _cmd[_cmd.index("/ST") + 1] == "03:00"
        assert "/D" in _cmd and _cmd[_cmd.index("/D") + 1] == "MON"
        assert "/F" in _cmd and "/IT" in _cmd
        # H1 fix: /RL HIGHEST only when the CREATING process already runs
        # elevated — never requested unconditionally (that would make
        # non-admin users' Enable silently fail with Access Denied).
        assert ("/RL" in _cmd) == bool(is_admin())
        # a distinct task name per schedule kind (--auto-update coexists
        # with the default clean/repair task rather than overwriting it)
        _cmd_upd = _sch._build_schtasks_cmd("daily", "04:30", extra_args=["--auto-update"], today=_mon)
        assert _cmd_upd[_cmd_upd.index("/TN") + 1] == _sch.TASK_NAME + "_auto_update"
        assert "/D" not in _cmd_upd, "DAILY must not carry a /D"
        # invalid input fails fast and never reaches schtasks
        assert _sch._build_schtasks_cmd_str("weekly", "03:00", today=_mon)  # legacy string form is quoted, non-empty
        _ok_bad, _msg_bad = _sch.enable_schedule("never", "03:00")
        assert _ok_bad is False and "Unknown schedule frequency" in _msg_bad
        _ok_bad2, _msg_bad2 = _sch.enable_schedule("daily", "3pm")
        assert _ok_bad2 is False and "schedule time" in _msg_bad2
        # live read-only probes only (types, never outcome)
        _ok, _out = _sch.get_schedule_status()
        assert isinstance(_ok, bool) and isinstance(_out, str)
        _ok_upd, _out_upd = _sch.get_schedule_status(["--auto-update"])
        assert isinstance(_ok_upd, bool) and isinstance(_out_upd, str)
        # resync is read-probe + single-lock flag-write; restore after
        try:
            _cfg0 = config_persist.load_config()
            _s0 = bool(_cfg0.get("schedule_enabled", False))
            _u0 = bool(_cfg0.get("schedule_update_enabled", False))
        except Exception:
            _s0, _u0 = False, False
        try:
            _live = _sch.resync_schedule_flag()
            assert isinstance(_live, bool)
            _live_upd = _sch.resync_schedule_flag(["--auto-update"])
            assert isinstance(_live_upd, bool)
        finally:
            def _restore(cfg, _s0=_s0, _u0=_u0):
                cfg["schedule_enabled"] = _s0
                cfg["schedule_update_enabled"] = _u0
            from app.config_persist import update_config as _upd
            try:
                _upd(_restore)
            except Exception:
                pass
            config_persist._config_cache = None
    check("maintenance schedule builders + validation + probe", _maintenance_schedule)
    def _tray_icon_path():
        # Cross-platform on purpose: this is exactly the part of icon
        # loading that has no ctypes in it, and the part that broke once
        # already (a stray `os.path...` instead of `_os.path...` inside a
        # broad except — NameError, silently swallowed, tray fell back to
        # the plain stock icon on every run). Runs on every OS/CI, not
        # just windows-latest, so this class of bug can't ship silently
        # again regardless of which runner catches it first.
        import os as _os
        from app import tray as _tm
        icon = _tm.TrayIcon(icon_path=None)
        cands = icon._icon_candidates()
        assert cands, "no icon candidates at all — tray will always fall back to the stock icon"
        bundled = [c for c in cands if c.replace("\\", "/").endswith("assets/icon.ico")]
        assert bundled, f"bundled icon.ico missing from candidates: {cands}"
        assert _os.path.isfile(bundled[0]), f"candidate path does not exist on disk: {bundled[0]}"
        # an explicit icon_path is tried first
        icon2 = _tm.TrayIcon(icon_path="/some/override.ico")
        assert icon2._icon_candidates()[0] == "/some/override.ico"
    check("tray icon candidate paths resolve to a real file", _tray_icon_path)


def main(_only=None, _verbose=False):
    import tkinter as tk
    from app import gui

    # run the harness checks FIRST — they set `results`, which the old
    # vacuous flow never touched
    run_checks()

    root = tk.Tk()
    root.withdraw()  # headless: don't flash a window

    # The Admin Gate is only shown when the app's own gate triage decides it
    # can change something — and one of those decisions is "the saved
    # selection contains an admin task" (app/elevated_launch.
    # saved_selection_needs_admin). So the assertion below depends on the
    # USER's selected_tasks, not on this file. Pin the precondition to a
    # known admin task and put the user's own selection back afterwards;
    # otherwise the suite passes or fails based on what someone last ran.
    _gate_sel_saved = None
    if not is_admin():
        try:
            from app import config_persist as _cp0
            _gate_cfg = _cp0.load_config()
            _gate_sel_saved = copy.deepcopy(_gate_cfg.get("selected_tasks"))
            if not _el_gate.saved_selection_needs_admin():
                _cp0.update_config(
                    lambda c: c.setdefault("selected_tasks", {}).update(
                        {"Clean": ["driver_junk"]}))
                _cp0._config_cache = None
        except Exception:
            _gate_sel_saved = None

    app = gui.Application(root)

    # H16: the one place the shared names are assembled. Inside main() these
    # were locals; the areas now read them off the Ctx, so the dependency is
    # written down in one list instead of being four closure variables.
    ctx = Ctx(root=root, app=app, gui=gui, is_admin=is_admin,
              config_persist=config_persist,
              save_config_verified=_save_config_verified,
              run_checks=run_checks,
              gate_sel_saved=_gate_sel_saved)

    # H16: from here the areas run, in tools.smoke.AREAS order, against
    # this Ctx. Nothing below this line belongs in the driver — a new
    # check belongs in the area it is about.
    try:
        run_all(ctx, only=_only, verbose=_verbose)
    finally:
        try:
            root.destroy()
        except Exception:
            pass

    failures = [r for r in results if not r[1]]
    for name, ok, err in results:
        print(f"  {'PASS' if ok else 'FAIL'}  {name}"
              + (f"  ({err})" if err else ""))
    # overall assertions already raised if broken; reaching here = pass
    print("SMOKE TEST: ALL PASS" if not failures
          else f"SMOKE TEST: {len(failures)} FAILURES")
    return 1 if failures else 0


if __name__ == "__main__":
    _only = None
    _verbose = "--verbose" in sys.argv
    if "--only" in sys.argv:
        _i = sys.argv.index("--only") + 1
        _only = {a.strip() for a in sys.argv[_i].split(",") if a.strip()}
    sys.exit(main(_only, _verbose))
