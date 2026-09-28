"""Tweak Health Center and the run-row pills

H16: extracted verbatim from tools/smoke_gui.py main(), L1309-1514.

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
    # --- Tweak Health Center (user-approved feature 2, 2026-09) --------
    # Synchronous part only (entry/rows/banner/buttons/verify contract).
    # Real async runs (run_tasks worker -> scorecard dialog, health
    # check worker -> results) are covered by tools/smoke_async.py:
    # worker-thread after() delivery is reliable under a real mainloop
    # but NOT under this harness's manual root.update() pump (Tcl
    # inter-thread wakeup starvation — proven during Feature 1 dev), so
    # no threaded run is driven here at all.
    # verify fns are READ-ONLY: they must never raise, and must return
    from app.tasks import tweak_tasks as _tt
    _verify_names = [n for n in dir(_tt) if n.startswith("verify_")]
    assert len(_verify_names) >= 20, f"expected 20+ verify fns, got {len(_verify_names)}"
    for _vn in _verify_names:
        try:
            _v = getattr(_tt, _vn)()
        except Exception as exc:
            raise AssertionError(f"{_vn} raised: {exc!r}")
        assert _v in (True, False, None), f"{_vn} returned { _v!r}"
    print(f"  verify fns: {len(_verify_names)} read-only, contract OK")

    # 3 cards on every tab (Tweak symmetry: Minimal/Recommended/Custom;
    # Game Session lives in the Auto-Pilot dialog, Undo/Health in the
    # context strip — cards are for runnable presets only)
    assert sorted(app.tabs["Tweak"].preset_cards) == ["Custom", "Minimal", "Recommended"], \
        f"Tweak cards: {sorted(app.tabs['Tweak'].preset_cards)}"
    assert "Game Session" not in app.tabs["Tweak"].preset_cards
    assert "Undo Tweaks" not in app.tabs["Tweak"].preset_cards
    assert "Tweak Health" not in app.tabs["Tweak"].preset_cards
    # ...but the Game Session preset DATA stays (pilot + game-night engine)
    from app.tab_presets import PRESETS as _PR2
    assert "Game Session" in _PR2["Tweak"] and len(_PR2["Tweak"]["Game Session"]) == 12, \
        f"Game Session preset: {len(_PR2['Tweak']['Game Session'])} tasks (9 base + 3 FPS pass)"
    assert len(app.tabs["Clean"].preset_cards) == 3   # 2 presets + Custom
    assert len(app.tabs["Repair"].preset_cards) == 3  # 2 presets + Custom
    # summary density is uniform: small AND large presets all flow 3
    # across (scroll absorbs the rest) — no 4/5-column squeeze that wraps
    # labels mid-word on wide presets like Recommended / Deep Clean.
    for _tab, _preset in (("Tweak", "Minimal"), ("Tweak", "Recommended"),
                          ("Clean", "Deep Clean"), ("Repair", "Deep Repair")):
        _pg = app.tabs[_tab]
        _pg._select_preset(_preset)
        root.update()
        assert getattr(_pg, "_cell_cols", None) == 3, \
            f"{_tab}/{_preset}: {_pg._cell_cols} columns, want 3"
    app.tabs["Tweak"]._enter_custom()
    root.update()

    # health entry with a REAL applied key (mouse_accel: HKCU, readable
    # without admin). try/finally restores the registry-of-trust so a
    # crashed harness never leaves a lying badge behind.
    tpage = app.tabs["Tweak"]
    config_persist.mark_tweak_applied("mouse_accel")
    try:
        tpage._enter_health()
        root.update()
        assert tpage.mode == "health", f"mode is {tpage.mode}"
        keys = [k for k, _t, _s in tpage._health]
        assert "mouse_accel" in keys, f"applied key missing from health rows: {keys}"
        assert "mouse_accel" in tpage._health_rows, "health row widget missing"
        assert tpage._health_reapply_btn is not None and tpage._health_reapply_btn.winfo_exists()
        # health is a status view: no selection, count refresh is a no-op
        assert tpage.selected_tasks() == [], "health mode must select nothing"
        tpage._refresh_run_count()   # must not relabel the Check button
        root.update()
        # The entry auto-kicks the check worker; under this harness's
        # manual update() pump its after() hop may never fire (proven Tcl
        # inter-thread starvation — the async delivery itself is proven
        # by tools/smoke_async.py under a real mainloop). Drive the
        # finish step deterministically on the main thread with the real
        # verify outcome for this machine.
        import time as _tq
        _deadline = _tq.monotonic() + 5
        while _tq.monotonic() < _deadline and "mouse_accel" not in tpage._health_results:
            root.update()
            _tq.sleep(0.01)
        if "mouse_accel" not in tpage._health_results:
            # worker hop starved: compute the same result synchronously
            # (verify fns are pure reads, no Tk) and finish directly
            try:
                _v = tpage.task_by_key["mouse_accel"].verify()
            except Exception:
                _v = None
            tpage._finish_health_check({"mouse_accel": _v})
            root.update()
        assert "mouse_accel" in tpage._health_results
        assert tpage._health_results["mouse_accel"] in (True, False, None)
        # banner show/hide logic (forced outcomes — deterministic)
        tpage._health_results = {"mouse_accel": False}
        tpage._paint_health_rows()
        root.update()
        assert tpage._health_banner.winfo_manager() == "pack", "drift banner not shown"
        assert "1" in tpage._health_banner_lbl.cget("text"), "banner count wrong"
        tpage._health_results = {"mouse_accel": True}
        tpage._paint_health_rows()
        root.update()
        assert tpage._health_banner.winfo_manager() == "", "banner not hidden when clean"
        # busy-gating covers both health buttons
        tpage.set_run_enabled(False)
        tpage.set_run_enabled(True)
        # leaving health restores normal mode machinery
        tpage._enter_custom()
        root.update()
        assert tpage.mode == "custom"
    finally:
        config_persist.mark_tweak_reverted("mouse_accel")
    print("  tweak health: entry + check pipeline + banner OK")

    # --- Tweak run-row pills (3-card symmetry entries) ------------------
    # headless-safe: invoke each pill's stored _cmd (the exact callable a
    # click would run) — never event_generate (unmapped widgets eat it).
    # Hermetic applied-state: snapshot the live registry, drive it to
    # empty for the quiet-state asserts, then to one applied key for
    # the lit-state asserts, then restore exactly.
    from app.config_persist import get_tweak_state as _live_state
    # get_tweak_state merges applied_tweaks + tweak_snapshots — snapshot
    # BOTH lists to drive the registry hermetically.
    try:
        _cfg_snap = config_persist.load_config()
        _snap_applied = list(_cfg_snap.get("applied_tweaks", []) or [])
        import copy as _copy
        _snap_shots = _copy.deepcopy(_cfg_snap.get("tweak_snapshots", {}) or {})
    except Exception:
        _snap_applied, _snap_shots = [], {}
    def _set_registry(applied, shots):
        try:
            _cfg = config_persist.load_config()
            _cfg["applied_tweaks"] = list(applied)
            import copy as _copy2
            _cfg["tweak_snapshots"] = _copy2.deepcopy(shots)
            config_persist.save_config(_cfg)
            config_persist._config_cache = None
        except Exception:
            pass
    try:
        _set_registry([], {})
        tpage._enter_custom()
        tpage._refresh_tweak_pills()
        root.update()
        for _w in ("_rr_check", "_rr_undo", "_rr_session"):
            assert getattr(tpage, _w, None) is not None, f"Tweak run row missing {_w}"
        # other tabs never build them
        assert getattr(app.tabs["Clean"], "_rr_undo", None) is None, "Clean must not have tweak pills"
        assert getattr(app.tabs["Repair"], "_rr_undo", None) is None, "Repair must not have tweak pills"
        # hero still leads the row
        assert "Run" in str(tpage.run_btn._text), tpage.run_btn._text
        # with nothing applied: Check plain, Undo hidden, Session shown
        assert tpage._rr_check._text == "Check", tpage._rr_check._text
        assert tpage._rr_check.winfo_manager() == "pack"
        assert tpage._rr_undo.winfo_manager() == "", "Undo must hide with nothing applied"
        assert "Game Session" in str(tpage._rr_session._text), tpage._rr_session._text
        assert tpage._rr_session.winfo_manager() == "pack"
        # with a real applied key: counts light up
        config_persist.mark_tweak_applied("mouse_accel")
        try:
            tpage._refresh_tweak_pills()
            root.update()
            assert "1" in str(tpage._rr_check._text), tpage._rr_check._text
            assert tpage._rr_undo.winfo_manager() == "pack", "Undo must show when applied"
            assert "1" in str(tpage._rr_undo._text), tpage._rr_undo._text
            # routing: Check -> health mode, Undo -> undo mode,
            # Game Session -> its preset summary (review-then-Run, like cards)
            tpage._rr_check._cmd()
            root.update()
            assert tpage.mode == "health", f"Check pill landed in {tpage.mode}"
            tpage._enter_custom()
            root.update()
            tpage._rr_undo._cmd()
            root.update()
            assert tpage.mode == "undo", f"Undo pill landed in {tpage.mode}"
            tpage._enter_custom()
            root.update()
            tpage._rr_session._cmd()
            root.update()
            assert tpage.mode == "preset" and tpage._selected_preset == "Game Session", \
                f"session pill landed in {tpage.mode}/{tpage._selected_preset}"
            assert tpage.selected_tasks(), "Game Session preset selected nothing"
            tpage._enter_custom()
            root.update()
            # undo/health modes own their rows: no pills beside them
            tpage._enter_undo()
            root.update()
            assert getattr(tpage, "_rr_check", None) is None, "pills must shed in undo mode"
            assert "Undo Selected" in str(tpage.run_btn._text)
            tpage._enter_health()
            root.update()
            assert getattr(tpage, "_rr_check", None) is None, "pills must shed in health mode"
            assert "Check Health" in str(tpage.run_btn._text)
            tpage._enter_custom()
            root.update()
            assert getattr(tpage, "_rr_check", None) is not None, "pills must return in custom mode"
        finally:
            config_persist.mark_tweak_reverted("mouse_accel")
            tpage._enter_custom()
            root.update()
    finally:
        # restore the live registry exactly (never leave the harness's
        # marks behind, never drop the user's own applied flags/snapshots)
        try:
            _set_registry(_snap_applied, _snap_shots)
            tpage._enter_custom()
            tpage._refresh_tweak_pills()
            root.update()
        except Exception:
            pass
    print("  tweak pills: run-row entries + conditional Undo + routing + mode matrix OK")

