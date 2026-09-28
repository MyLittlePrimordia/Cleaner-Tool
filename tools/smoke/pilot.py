"""Game Session Auto-Pilot

H16: extracted verbatim from tools/smoke_gui.py main(), L2421-2855.

Nothing here was reformatted or reordered. The section banners are
kept so the original structure stays greppable, and ORDER MATTERS —
sections share live Application state, so run_all() calls these in
sequence.
"""


def run(ctx):
    # names main() used to provide as locals
    root = ctx.root
    app = ctx.app
    gui = ctx.gui
    is_admin = ctx.is_admin
    config_persist = ctx.config_persist
    _save_config_verified = ctx.save_config_verified
    import copy
    import sys
    import tkinter as tk

    # config-restore failures are recorded on the harness result list, which
    # pre-H16 was a module global of smoke_gui
    from tools.smoke import results
    # --- Game Session Auto-Pilot (user-approved feature 6, 2026-09) ----
    import app.session_pilot as _sp
    # watchlist hygiene: non-empty, normalized, no launchers/services
    # (an idle launcher or boot service must never read as "playing")
    assert len(_sp.KNOWN_GAME_EXES) >= 15, "watchlist too small to be useful"
    assert all(e == e.lower() and e.endswith(".exe") for e in _sp.KNOWN_GAME_EXES)
    for _bad in ("steam.exe", "explorer.exe", "vgc.exe", "beservice.exe"):
        assert _bad not in _sp.KNOWN_GAME_EXES, f"{_bad} must not trigger sessions"
    assert _sp.normalize_exe("ELDENRING.EXE") == "eldenring.exe"
    assert _sp.normalize_exe(r"C:\Games\Foo\Bar.exe") == "bar.exe"
    assert _sp.normalize_exe("game") == "game.exe"
    assert _sp.normalize_exe("") == ""
    print("  pilot watchlist: normalized, no launchers/services")
    # state machine, driven tick-by-tick (fully deterministic, no threads).
    # Phase-5 source contract: (name, path) tuples — path may be "" when
    # a synthetic/legacy source can't resolve it (basename-only matching
    # then applies, exactly like the old contract).
    _applied, _reverted = [], []
    _procs = [[]]
    _pilot = _sp.SessionPilot(
        get_processes=lambda: list(_procs[0]),
        watched={"eldenring.exe", "cs2.exe"},
        on_apply=lambda hits: _applied.append(list(hits)),
        on_revert=lambda: _reverted.append(True),
        stable_polls=2)
    assert _pilot.tick() == "idle" and not _applied and not _reverted
    _procs[0] = [("eldenring.exe", ""), ("explorer.exe", "")]
    assert _pilot.tick() == "active", "launch not detected"
    assert _applied == [["eldenring.exe"]] and not _reverted
    _procs[0] = [("eldenring.exe", "C:\\Games\\ER\\eldenring.exe")]
    assert _pilot.tick() == "active", "must not re-apply mid-session"
    assert len(_applied) == 1 and not _reverted
    _procs[0] = []
    assert _pilot.tick() == "active", "single miss must not end session"
    assert not _reverted
    assert _pilot.tick() == "idle", "second clean miss must end session"
    assert _reverted == [True]
    assert _pilot.active_hit is None
    _procs[0] = [("CS2.EXE", "")]
    assert _pilot.tick() == "active", "re-launch must start a fresh session"
    assert len(_applied) == 2, "re-launch must re-apply"
    # path-pinning: a watched PATH only fires from its registered dir
    _pp = _sp.SessionPilot(get_processes=lambda: list(_procs[0]),
                           watched={"C:/SteamLIB/games/BG3/baldursgate3.exe"},
                           on_apply=lambda h: _applied.append(h))
    _procs[0] = [("baldursgate3.exe", "D:\\Other\\baldursgate3.exe")]
    _pp.tick()
    assert _pp.state == "idle", "same-name exe in wrong dir must NOT fire"
    _procs[0] = [("baldursgate3.exe", "C:\\SteamLIB\\games\\BG3\\baldursgate3.exe")]
    _pp.tick()
    assert _pp.state == "active", "registered dir must fire"
    # exploding process source must never kill the watcher
    _boom = _sp.SessionPilot(get_processes=lambda: 1 / 0,
                             watched={"a.exe"},
                             on_apply=lambda h: _applied.append(h),
                             on_revert=lambda: _reverted.append(True))
    assert _boom.tick() == "idle"
    # start/stop/running with a fake source (real thread, short-lived)
    _t2 = _sp.SessionPilot(get_processes=lambda: [], watched={"a.exe"})
    assert _t2.start(interval_s=0.2) is True
    assert _t2.running() is True
    assert _t2.start(interval_s=0.2) is False, "double start must refuse"
    _t2.stop()
    assert _t2.running() is False
    print("  pilot state machine: edges + grace + path-pinning + restart-proof")
    # config round-trip (snapshot + restore: never pollute the user's file)
    _cfg0 = config_persist.load_config()
    _snap = {k: _cfg0.get(k) for k in
             ("session_pilot_enabled", "session_pilot_games", "session_pilot_preset")}
    try:
        _c = config_persist.load_config()
        _c["session_pilot_enabled"] = True
        _c["session_pilot_games"] = ["probe.exe"]
        _c["session_pilot_preset"] = "Recommended"
        config_persist.save_config(_c)
        _c2 = config_persist.load_config()
        assert _c2["session_pilot_enabled"] is True
        assert _c2["session_pilot_games"] == ["probe.exe"]
        assert _c2["session_pilot_preset"] == "Recommended"
        # junk coerces back to defaults on the DISK-load path (where the
        # H8/H9 validators live — the in-memory cache is only ever written
        # by the app itself, so save+load round-trips it verbatim)
        _c3 = config_persist.load_config()
        _c3["session_pilot_games"] = "notalist"
        _c3["session_pilot_preset"] = 123
        config_persist.save_config(_c3)
        config_persist._config_cache = None   # force disk reload
        _c4 = config_persist.load_config()
        assert _c4["session_pilot_games"] == [], "string games must coerce"
        assert isinstance(_c4["session_pilot_preset"], str)
    finally:
        # verified restore (never assume the write landed — see helper)
        if not _save_config_verified(dict(_snap)):
            results.append(("pilot config restore", False,
                            "user config left modified by suite"))
    print("  pilot config: round-trip + coerce OK")
    # Tools tab (user redesign 2026-09): Pilot is a card there that opens
    # the X-button popup (in-tab panels were reverted — no room at 980x720)
    tools_page = app.tabs.get("Tools")
    assert tools_page is not None and isinstance(tools_page, gui.ToolsTab), \
        "Tools tab missing / not a ToolsTab"
    _started, _stopped = [], []
    _o_start, _o_stop = app._pilot_start, app._pilot_stop
    app._pilot_start = lambda: _started.append(True)
    app._pilot_stop = lambda: _stopped.append(True)
    try:
        _pdlg = gui.PilotDialog(root, app)
        try:
            root.update()
            # intro blurb must stay one row (user call — no 2-line wrap)
            _intro = next((w for w in _pdlg.body.winfo_children()
                           if isinstance(w, tk.Label)), None)
            assert _intro is not None
            _intro.update_idletasks()
            assert _intro.winfo_reqheight() <= 28, _intro.winfo_reqheight()
            assert _pdlg._preset_var.get() in ("Minimal", "Recommended", "Game Session")
            _pdlg._enabled_var.set(True)
            root.update()
            assert _started, "toggle ON must start the watcher"
            _pdlg._enabled_var.set(False)
            root.update()
            assert _stopped, "toggle OFF must stop the watcher"
            # preset picker persists
            _pdlg._preset_var.set("Recommended")
            _pdlg._on_preset()
            assert config_persist.load_config()["session_pilot_preset"] == "Recommended"
            # add/remove a game (file picker stubbed — never blocks).
            # Phase-5: Browse stores the FULL path (dir-pinned matching)
            _probe_path = r"C:\Games\Probe\probe.exe"
            import tkinter.filedialog as _pfd
            _o_ask = _pfd.askopenfilename
            _pfd.askopenfilename = lambda **kw: _probe_path
            try:
                _pdlg._add_game()
            finally:
                _pfd.askopenfilename = _o_ask
            root.update()
            assert _probe_path in config_persist.load_config()["session_pilot_paths"], \
                "Browse pick must persist as a full path"
            _pdlg._remove_game(_probe_path)
            root.update()
            assert _probe_path not in config_persist.load_config()["session_pilot_paths"]
            # watch-all toggle persists and drives the watchlist rebuild
            _n_refresh = []
            _o_ref = app._pilot_refresh_watchlist
            app._pilot_refresh_watchlist = lambda: _n_refresh.append(True)
            try:
                _pdlg._watch_all_var.set(True)
                root.update()
                assert config_persist.load_config()["session_pilot_watch_all"] is True
                assert _n_refresh, "watch-all toggle must refresh the watchlist"
                _pdlg._watch_all_var.set(False)
                root.update()
                assert config_persist.load_config()["session_pilot_watch_all"] is False
            finally:
                app._pilot_refresh_watchlist = _o_ref
            # preset task resolution: Game Session keys, all revertible
            # (an unrevertible member would fail loudly at session end)
            _pdlg._close()
            root.update()
        finally:
            try:
                _pdlg._close()
            except Exception:
                pass
            root.update()
    finally:
        app._pilot_start, app._pilot_stop = _o_start, _o_stop
        # verified restore (see helper) — must not raise out of finally
        try:
            if not _save_config_verified(dict(_snap)):
                results.append(("pilot dialog restore", False,
                                "user config left modified by suite"))
        except Exception as exc:
            results.append(("pilot dialog restore", False,
                            f"{type(exc).__name__}: {exc}"))
    # Hermetic preset (same live-config coupling the async pilot step had):
    # _pilot_preset_tasks() resolves the AMBIENT pick, but the asserts below
    # pin the Game Session shape — a machine whose user picked Recommended /
    # Minimal in the real dialog would fail here. Force Game Session around
    # the asserts, restore the ambient pick after (verified; never assume).
    _snap_preset = config_persist.load_config().get("session_pilot_preset", "Game Session")
    try:
        if not _save_config_verified({"session_pilot_preset": "Game Session"}):
            raise RuntimeError("could not force Game Session preset for the assertion")
        _ptasks = app._pilot_preset_tasks()
        assert len(_ptasks) == 12, f"Game Session must resolve 12 tasks, got {len(_ptasks)}"
        assert all(t.revert is not None for t in _ptasks), "unrevertible session member!"
    finally:
        # verified restore (see helper) — must not raise out of finally
        try:
            if not _save_config_verified({"session_pilot_preset": _snap_preset}):
                results.append(("pilot preset restore", False,
                                "user config left modified by suite"))
        except Exception as exc:
            results.append(("pilot preset restore", False,
                            f"{type(exc).__name__}: {exc}"))
    print("  pilot dialog: toggle + preset + add/remove + task resolution OK")
    # picker opens INSTANTLY, streams rows async (user bug report: the
    # enumeration used to run on the Tk thread — big library = frozen UI
    # then a jerk). Slow controllable scan_fn proves the contract.
    import threading as _th2
    import time as _time2
    _gate = _th2.Event()
    _picked = []
    def _slow_scan():
        _gate.wait(timeout=10)
        return [{"name": "Probe Game", "launcher": "Steam",
                 "path": r"C:\G\probe.exe", "folder": r"C:\G"}]
    _pk = gui._CatalogPickerDialog(root, "Probe picker", scan_fn=_slow_scan,
                                   on_pick=lambda g: _picked.append(g))
    try:
        root.update()
        assert _pk._scanning is True, "picker must start in scanning state"
        assert _pk._all == [], "rows must not exist before the sweep lands"
        # close MID-SCAN must be safe (token drops the late landing)
        _pk2 = gui._CatalogPickerDialog(root, "Probe picker 2",
                                        scan_fn=_slow_scan)
        try:
            root.update()
            _pk2.close()
            root.update()
            assert not _pk2._dlg.winfo_exists(), "picker close failed"
        finally:
            try:
                _pk2.close()
            except Exception:
                pass
        _gate.set()
        for _ in range(150):
            root.update()
            if _pk._all:
                break
            _time2.sleep(0.02)
        assert _pk._all and _pk._all[0]["name"] == "Probe Game", \
            "swept rows never landed"
        assert _pk._scanning is False, "scanning flag stuck"
        _pk._pick(_pk._all[0])
        root.update()
        assert _picked and _picked[0]["name"] == "Probe Game", "pick routing broken"
        assert not _pk._dlg.winfo_exists(), "pick must close the picker"
    finally:
        try:
            _gate.set()
        except Exception:
            pass
        try:
            _pk.close()
        except Exception:
            pass
        root.update()
    print("  picker: instant open + async stream + mid-scan close OK")
    # watch-all catalog merges async with a generation guard (same bug
    # class: _pilot_ensure used to sweep the disk synchronously)
    import app.game_catalog as _gc
    _o_ig = _gc.installed_games
    _snap_wa = config_persist.load_config().get("session_pilot_watch_all", False)
    try:
        _gate2 = _th2.Event()
        _gc.installed_games = lambda: (_gate2.wait(timeout=10), [{
            "name": "Merge Game", "launcher": "Steam",
            "path": r"C:\Merge\merge.exe", "folder": r"C:\Merge"}])[1]
        _c = config_persist.load_config()
        _c["session_pilot_watch_all"] = True
        config_persist.save_config(_c)
        _t0 = _time2.perf_counter()
        app._pilot_ensure()
        _dt_ms = (_time2.perf_counter() - _t0) * 1000
        assert _dt_ms < 2000, f"_pilot_ensure blocked {_dt_ms:.0f}ms (disk sweep on Tk thread!)"
        assert app._pilot is not None, "pilot not built"
        assert "merge.exe" not in (app._pilot.watched or set()), \
            "catalog must not be merged before the sweep lands"
        _gate2.set()
        for _ in range(150):
            root.update()
            if "merge.exe" in (app._pilot.watched or set()):
                break
            _time2.sleep(0.02)
        assert "merge.exe" in (app._pilot.watched or set()), "catalog merge never landed"
        # stale landing dropped: supersede, then release the old sweep
        _gate3 = _th2.Event()
        _gc.installed_games = lambda: (_gate3.wait(timeout=10), [{
            "name": "Stale Game", "launcher": "Steam",
            "path": r"C:\Stale\stale.exe", "folder": r"C:\Stale"}])[1]
        app._pilot_ensure()
        _first = app._pilot
        _c = config_persist.load_config()
        _c["session_pilot_watch_all"] = False
        config_persist.save_config(_c)
        app._pilot_ensure()
        assert app._pilot is not _first, "re-ensure must replace the pilot"
        _gate3.set()
        for _ in range(60):
            root.update()
            _time2.sleep(0.02)
        assert "stale.exe" not in (app._pilot.watched or set()), \
            "stale sweep resurrected dead state"
    finally:
        try:
            _gc.installed_games = _o_ig
        except Exception:
            pass
        # verified restore (see helper) — must not raise out of finally
        try:
            if not _save_config_verified({"session_pilot_watch_all": _snap_wa}):
                results.append(("pilot watch-all restore", False,
                                "user config left modified by suite"))
        except Exception as exc:
            results.append(("pilot watch-all restore", False,
                            f"{type(exc).__name__}: {exc}"))
        try:
            app._pilot_stop()
        except Exception:
            pass
        root.update()
    print("  pilot watch-all: fast ensure + async merge + stale-drop OK")
    # pilot fires NEVER choreograph shared UI behind a modal or a run
    # (user bug report: app churned behind the open setup dialog).
    # _pilot_ui_free is the single gate; apply/revert honor it.
    assert app._pilot_ui_free() is True, "idle UI must read free"
    _o_run = app.run_tasks
    _ran = []
    app.run_tasks = lambda *a, **k: _ran.append((a, k))
    from app.ui import app as _appmod2
    _o_toast = _appmod2.show_toast
    _appmod2.show_toast = lambda *a, **k: None
    try:
        # modal open: a REAL themed dialog (registry-era detector — a bare
        # root.grab_set() no longer reads as "modal", correctly, because no
        # ThemedModal exists; the old grab-only test simulated one poorly)
        _gdlg = gui.PilotDialog(root, app)
        root.update()
        try:
            assert app._pilot_ui_free() is False, "open dialog must read occupied"
            app._pilot_apply(["cs2.exe"])
            root.update()
            assert not _ran, "apply ran during an open dialog!"
            app._pilot_fresh_keys = ["game_mode"]
            app._pilot_revert()
            root.update()
            assert not _ran, "revert ran during an open dialog!"
        finally:
            try:
                _gdlg._close()
            except Exception:
                pass
            root.update()
        assert app._pilot_ui_free() is True, "released UI must read free"
        # busy run: revert defers WITHOUT losing intent (F01: keys are
        # preserved so a later exit event still reverts; Undo stays backup)
        try:
            with app._busy_lock:
                app._busy = True
            assert app._pilot_ui_free() is False, "busy UI must read occupied"
            app._pilot_fresh_keys = ["game_mode"]
            app._pilot_revert()
            root.update()
            assert not _ran, "revert ran during a busy run (Busy popup over game!)"
            assert app._pilot_fresh_keys == ["game_mode"], "deferred revert must preserve keys"
        finally:
            try:
                with app._busy_lock:
                    app._busy = False
            except Exception:
                pass
            root.update()
        # UI free again: the preserved intent reconciles and keys clear
        app._pilot_revert()
        root.update()
        assert app._pilot_fresh_keys == [], "free-UI revert must consume keys"
        # free UI: apply proceeds to the engine again
        app._pilot_apply(["cs2.exe"])
        root.update()
        assert _ran, "apply must run when UI is free"
    finally:
        app.run_tasks = _o_run
        try:
            _appmod2.show_toast = _o_toast
        except Exception:
            pass
        try:
            app._pilot_stop()
        except Exception:
            pass
        root.update()
    print("  pilot defer: modal/busy fires skip run UI, revert never lost to Busy popup OK")
    # background chains pause behind modals (user bug report: first-open
    # Auto-Pilot flickered — prewarm + Install catalog kept mapping pages
    # behind the popup; second open was clean because chains had drained).
    assert app._modal_open() is False, "idle UI must read no-modal"
    assert app._prewarm_should_wait() is False, "idle UI must read prewarm-go"
    # registry-era detector: a REAL dialog, not a simulated bare grab
    _mdlg = gui.PilotDialog(root, app)
    root.update()
    try:
        assert app._modal_open() is True, "open dialog must read modal-open"
        assert app._prewarm_should_wait() is True, \
            "prewarm must pause behind a modal"
        inst = app.tabs["Install"]
        _q0, _r0, _a0 = inst._catalog_queue, inst._catalog_ready, inst._catalog_after
        _built = []
        try:
            inst._catalog_ready = False
            inst._catalog_queue = [lambda: _built.append(True)]
            inst._catalog_step()
            root.update()
            assert not _built, "catalog slice built behind a modal!"
            assert inst._catalog_queue, "deferred slice must keep its place"
            assert inst._catalog_after is not None, "retry must be chained"
        finally:
            try:
                if inst._catalog_after is not None:
                    inst.after_cancel(inst._catalog_after)
            except Exception:
                pass
            inst._catalog_queue, inst._catalog_ready, inst._catalog_after = _q0, _r0, _a0
    finally:
        try:
            _mdlg._close()
        except Exception:
            pass
        root.update()
    assert app._modal_open() is False, "released UI must read no-modal"
    try:
        with app._busy_lock:
            app._busy = True
        assert app._prewarm_should_wait() is True, "prewarm must pause mid-run"
    finally:
        try:
            with app._busy_lock:
                app._busy = False
        except Exception:
            pass
    print("  modal-pause: prewarm + catalog chains defer behind popups OK")

