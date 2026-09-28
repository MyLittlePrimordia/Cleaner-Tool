"""Tools tab and the Quick Tools popup

H16: extracted verbatim from tools/smoke_gui.py main(), L3043-3346.

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
    # --- Tools tab (9 cards 3x3, Tweak geometry; Quick Tools popup) ---
    # The 9 feature cards open the X-button popups (800x600 modals).
    # Shortcuts moved from the inline box into the Quick Tools popup —
    # assert tab/cards exist, each card routes, and the popup carries
    # the classic rows.
    tools_page = app.tabs["Tools"]
    assert isinstance(tools_page, gui.ToolsTab), type(tools_page).__name__
    # Derived from _CARDS, not a frozen list: this assertion used to
    # hard-code the original nine keys and had silently been failing ever
    # since the tab grew past them. Checking the contract (every declared
    # card is built, every card has a route) instead of the census means
    # adding a card can never break the suite for no reason again.
    _declared = [c[0] for c in gui.ToolsTab._CARDS]
    assert sorted(tools_page._card_frames) == sorted(_declared), \
        (sorted(tools_page._card_frames), sorted(_declared))
    assert len(_declared) == len(set(_declared)), "duplicate Tools card key"
    # Audit fix: Game Night card removed (merged into Auto-Pilot's "manual
    # session" section — see the PilotDialog assertions above), and Quick
    # Tools relocated from the grid to the corner toolbox icon. Startup
    # Manager is a full card again by design (own dialog + route), so
    # only Game Night / Quick Tools must stay out of the grid.
    assert "gamenight" not in tools_page._card_frames, \
        "Game Night card should be gone (merged into Auto-Pilot)"
    assert "quick" not in tools_page._card_frames, \
        "Quick Tools should stay out of the card grid (corner toolbox icon)"
    assert "startup" in tools_page._card_frames, \
        "Startup Manager is a first-class card (own dialog + route)"
    # 3-wide grid, same pads/uniform as the Tweak preset cards. A single
    # left-over card sits in the middle column so the last row is centred.
    _pos = {k: (v[0].grid_info()["row"], v[0].grid_info()["column"])
            for k, v in tools_page._card_frames.items()}
    _n = len(_declared)
    _expected = []
    _lone = (_n % 3) == 1
    _last_row = (_n - 1) // 3
    for _i in range(_n):
        _r, _c = divmod(_i, 3)
        _expected.append((_r, 1 if (_lone and _r == _last_row) else _c))
    assert sorted(_pos.values()) == sorted(_expected), (_pos, _expected)
    app._switch_to("Tools")
    root.update()
    _opened = []
    _o_storage, _o_dns = app._open_storage_insight, app._open_dns_tester
    _o_procman, _o_pilot = app._open_process_manager, app._open_pilot_dialog
    _o_speed = app._open_speed_test
    _o_gamepad, _o_mic = app._open_gamepad_tester, app._open_mic_check
    _o_gameping = app._open_game_server_ping
    _o_quick, _o_startup = app._open_quick_tools, app._open_startup_manager
    app._open_storage_insight = lambda: _opened.append("storage")
    app._open_dns_tester = lambda: _opened.append("dns")
    app._open_process_manager = lambda: _opened.append("procman")
    app._open_pilot_dialog = lambda: _opened.append("pilot")
    app._open_speed_test = lambda: _opened.append("speed")
    app._open_gamepad_tester = lambda: _opened.append("gamepad")
    app._open_mic_check = lambda: _opened.append("miccheck")
    app._open_game_server_ping = lambda: _opened.append("gameping")
    app._open_quick_tools = lambda: _opened.append("quick")
    app._open_startup_manager = lambda: _opened.append("startup")
    try:
        for key in ("storage", "procman", "dns", "pilot", "speed",
                    "gamepad", "miccheck", "gameping"):
            tools_page._mount(key)
            root.update()
            assert _opened and _opened[-1] == key, \
                f"Tools card {key} routed to {_opened}"
        # Audit fix: Quick Tools is the bottom corner icon (between Auto
        # Maintenance and Export Logs), not a grid card — exercise its
        # actual click binding directly. Startup Manager is a grid card
        # again (covered by the _mount loop above), so it has no corner
        # icon. Headless-safe: event_generate("<Button-1>") is NOT
        # delivered to unmapped widgets (withdrawn root => corner labels
        # report winfo_ismapped()==0, proven by repro), so a synth click
        # silently no-ops here while canvas-hosted Install arrows (mapped
        # via create_window) do fire. Assert the click binding EXISTS,
        # then invoke the exact callable the binding would call.
        assert app._corner_quick.bind("<Button-1>"), "Quick Tools corner missing click binding"
        assert not hasattr(app, "_corner_startup"), \
            "Startup must not have a corner icon (it is a grid card)"
        app._corner_quick._corner_cmd()
        root.update()
        assert _opened and _opened[-1] == "quick", \
            f"Quick Tools corner icon routed to {_opened}"
    finally:
        app._open_storage_insight, app._open_dns_tester = _o_storage, _o_dns
        app._open_process_manager, app._open_pilot_dialog = _o_procman, _o_pilot
        app._open_speed_test, app._open_quick_tools = _o_speed, _o_quick
        app._open_gamepad_tester, app._open_mic_check = _o_gamepad, _o_mic
        app._open_game_server_ping = _o_gameping
    # shortcuts present inside the Quick Tools popup (same dimensions)
    _qd = gui.QuickToolsDialog(root, app, lambda t: None)
    try:
        root.update()
        assert (_qd._modal_w, _qd._modal_h) == (800, 600), (_qd._modal_w, _qd._modal_h)
        shortcut_texts = []
        def _collect_labels(w, acc):
            for c in w.winfo_children():
                if isinstance(c, tk.Label):
                    acc.append(str(c.cget("text")))
                _collect_labels(c, acc)
        _collect_labels(_qd._dlg, shortcut_texts)
        for want in ("Task Manager", "Device Manager", "Windows Update",
                     "Storage Sense"):
            assert want in shortcut_texts, f"Quick Tools popup missing {want}"
        for gone in ("Auto Maintenance", "Export Logs", "About"):
            assert gone not in shortcut_texts, f"Quick Tools popup still lists {gone}"
    finally:
        try:
            _qd._close()
        except Exception:
            pass
        root.update()
    # bottom-corner shortcuts live on the main window (not in the popup)
    assert str(app._corner_maint.cget("text")) == "🛠️", "corner maintenance icon missing"
    assert str(app._corner_logs.cget("text")) == "📋", "corner export-logs icon missing"
    # new Tools dialogs: real construction, 800x600, ticks run, clean close
    _gd = gui.GamepadDialog(root, app)
    try:
        root.update()
        assert (_gd._modal_w, _gd._modal_h) == (800, 600), (_gd._modal_w, _gd._modal_h)
        assert len(_gd._pad_btns) == 14, len(_gd._pad_btns)
        _gd._paint_pad_idle()
        _shape, _text = _gd._pad_btns["A"]
        assert _gd._pad_cv.itemcget(_shape, "fill") == "", "overlay must idle transparent"
        assert _gd._pad_img is not None, "controller artwork must load"
        # Artwork is 1200px subsampled /3 (400px) since the UI fix that
        # bumped it from /4 (300px); derive from _SCALE so a future
        # resample can't silently stale this again.
        _pad_px = int(300 * getattr(_gd, "_SCALE", 4.0 / 3.0))
        assert (_gd._pad_img.width(), _gd._pad_img.height()) == (_pad_px, _pad_px), \
            (_gd._pad_img.width(), _gd._pad_img.height())
        assert len(_gd._trig_views) == 2, len(_gd._trig_views)
        for _lbl in (_gd._stat_rate_val, _gd._stat_ms_val, _gd._stat_drift_val):
            assert _lbl is not None and str(_lbl.cget("text")) != "", "stat block missing"
        # themed sliders drive their vars
        assert len(_gd._sliders) == 2, len(_gd._sliders)
        _gd._weak_var.set(0.75)
        assert abs(_gd._weak_var.get() - 0.75) < 1e-9
        _gd._tick()
        root.update()
        assert "connected" in str(_gd._status_lbl.cget("text")).lower() or \
            "no controller" in str(_gd._status_lbl.cget("text")).lower()
        if "no controller" in str(_gd._status_lbl.cget("text")).lower():
            assert _gd._rumble_btn._enabled is False, "rumble must gate on no pad"
    finally:
        try:
            _gd._close()
        except Exception:
            pass
        root.update()
    # report-rate math unit check (reference formula, synthetic packets)
    _st = gui._PadReportStats()
    import time as _t1
    for _i in range(10):
        _st.update(_i + 1)
        _t1.sleep(0.005)
    assert _st.packets == 10 and _st.hz > 0 and _st.peak_hz >= _st.hz, \
        (_st.packets, _st.hz, _st.peak_hz)
    _st.reset()
    assert _st.packets == 0 and _st.hz == 0, "stats reset broken"
    # rumble path with a fake pad: burst motors then cut (incl. close)
    _had_fn = hasattr(gui._xinput_state_fn, "_fn")
    _old_fn = getattr(gui._xinput_state_fn, "_fn", None)
    _old_rumble = gui._xinput_rumble
    _rcalls = []
    _fstate = {"packet": 0}
    try:
        gui._xinput_state_fn._fn = lambda s: dict(
            packet=_fstate.__setitem__("packet", _fstate["packet"] + 1) or _fstate["packet"],
            buttons=0, lt=0, rt=0, lx=0, ly=0, rx=0, ry=0) if s == 0 else None
        # H12: GamepadDialog moved to app/ui/dialogs/gamepad_dialog.py and
        # imported _xinput_rumble into its OWN namespace, so rebinding the
        # name in app.gui no longer reaches it. Patch the owning module.
        # (`gui._xinput_state_fn` below still works as-is: that one sets an
        # attribute on the shared function object, and both names refer to
        # the same object.)
        from app.ui.dialogs import gamepad_dialog as _gpd
        _gpd._xinput_rumble = lambda slot, w, st: _rcalls.append((slot, w, st)) or True
        _gr = gui.GamepadDialog(root, app)
        try:
            _gr._dlg.after = lambda ms, fn, *a: "tX"
            root.update()
            for _i in range(32):
                _gr._tick()
                _t1.sleep(0.004)
            assert _gr._stats.hz > 0, "no Hz from synthetic packets"
            assert "L " in str(_gr._stat_drift_val.cget("text")) and "%" in str(
                _gr._stat_drift_val.cget("text")), _gr._stat_drift_val.cget("text")
            assert "Hz" in str(_gr._stat_rate_val.cget("text")), \
                _gr._stat_rate_val.cget("text")
            _gr._weak_var.set(0.5)
            _gr._strong_var.set(1.0)
            _gr._test_rumble()
            assert _rcalls and _rcalls[-1][1:] == (0.5, 1.0), _rcalls
            _gr._stop_rumble()
            assert _rcalls[-1][1:] == (0, 0), _rcalls
            _gr._test_rumble()
            _gr._stop_all()
            assert _rcalls[-1][1:] == (0, 0), "close must cut motors"
        finally:
            try:
                _gr._close()
            except Exception:
                pass
            root.update()
    finally:
        if _had_fn:
            gui._xinput_state_fn._fn = _old_fn
        else:
            try:
                delattr(gui._xinput_state_fn, "_fn")
            except Exception:
                pass
        _gpd._xinput_rumble = _old_rumble
        root.update()
    _md = gui.MicCheckDialog(root, app)
    try:
        root.update()
        assert (_md._modal_w, _md._modal_h) == (800, 600), (_md._modal_w, _md._modal_h)
        _md._tick()
        root.update()
        _mic_texts = []
        _collect_labels(_md._dlg, _mic_texts)
        assert any("Microphone" in t for t in _mic_texts), "mic dialog missing devices"
        assert not any("not present" in t for t in _mic_texts), "ghost endpoints leaked into UI"
        assert not any("Mic app on file" in t for t in _mic_texts), "raw registry rows leaked into UI"
        assert str(_md._verdict_lbl.cget("text")) != "", "verdict line missing"
        # per-device meter bound (not just enumerated)
        assert getattr(_md, "_meter", None) is not None or \
            not [e for e in getattr(_md, "_inputs", []) if e.get("state") == 1], \
            "active input present but no meter bound"
        # picker offers a real choice on multi-mic machines
        _actives = [e for e in getattr(_md, "_inputs", []) if e.get("state") == 1]
        if len(_actives) > 1:
            # Picker is a ttk.Combobox (was an OptionMenu/Menubutton) —
            # accept either so a widget-swap can't stale this again.
            assert any(w.winfo_class() in ("Menubutton", "TCombobox", "Combobox")
                       for w in _md._pick_box.winfo_children()), \
                "multi-mic machine must show the device dropdown"
    finally:
        try:
            _md._close()
        except Exception:
            pass
        root.update()
    assert gui.ThemedModal.any_open() is False, "new dialogs leaked a modal"
    # game server ping: tables sane, dialog paints stubbed rows, drains
    from app import gameping as _gp
    assert len(_gp.FORTNITE_REGIONS) == 8 and len(_gp.COMPANY_EDGES) == 23
    assert len(_gp.VALORANT_REGIONS) == 10 and len(_gp.APEX_REGIONS) == 9
    assert len(_gp.PUBG_REGIONS) == 10
    assert len(_gp.MINECRAFT_SERVERS) == 2 and len(_gp.ROBLOX_EDGES) == 2
    # Valve relays are live-resolved via _valve_targets() (API + honest
    # fallback), so the static table is intentionally empty — pin that
    # contract instead of a stale count.
    assert tuple(_gp.VALVE_REGIONS) == () and len(_gp.COD_REGIONS) == 10
    assert _gp._valve_targets(timeout=3.0), "valve live-target resolver returned nothing"
    assert len(_gp.GAME_TABS) == 72
    assert all(h.endswith(".ds.on.epicgames.com") for _n, h in _gp.FORTNITE_REGIONS)
    assert all(_gp.targets_for(k) for _t, k in _gp.GAME_TABS if k != "custom")
    assert _gp.verdict(12)[0] == "Tournament" and _gp.verdict(125)[0] == "Poor"
    assert _gp.split_custom("a;b") == ("", None)
    from app.config_persist import load_config as _lc, update_config as _uc
    _uc(lambda cfg: cfg.update({"gameping_hosts": ["example.com", "example.com:25565"]}))
    assert _lc().get("gameping_hosts") == ["example.com", "example.com:25565"]
    _uc(lambda cfg: cfg.update({"gameping_hosts": []}))
    _o_icmp, _o_tcp = _gp.probe_icmp, _gp.probe_tcp
    try:
        _gp.probe_icmp = lambda *a, **k: (3, 3, 24.0)
        _gp.probe_tcp = lambda *a, **k: (3, 3, 41.0)
        _pd = gui.GameServerPingDialog(root, app)
        try:
            root.update()
            assert (_pd._modal_w, _pd._modal_h) == (800, 600)
            for _ in range(60):
                root.update()
                import time as _t5
                _t5.sleep(0.02)
                if _pd._landed[0] >= _pd._total[0] > 0:
                    break
            root.update()
            assert "Europe" in str(_pd._verdict_lbl.cget("text")) or \
                "Best:" in str(_pd._verdict_lbl.cget("text")), \
                _pd._verdict_lbl.cget("text")
        finally:
            try:
                _pd._close()
            except Exception:
                pass
            root.update()
    finally:
        _gp.probe_icmp, _gp.probe_tcp = _o_icmp, _o_tcp
    assert gui.ThemedModal.any_open() is False, "ping dialog leaked a modal"
    # new tasks resolve on their tabs (groups validated at import)
    from app.tab_presets import TABS as _TABS
    _clean_keys = {t.key for t in _TABS["Clean"]}
    _tweak_keys = {t.key for t in _TABS["Tweak"]}
    _repair_keys = {t.key for t in _TABS["Repair"]}
    for _k in ("riot_logs", "hoyoverse_logs", "rockstar_logs", "roblox_logs"):
        assert _k in _clean_keys, f"clean task missing: {_k}"
    for _k in ("taskbar_endtask", "mic_privacy_allow", "clock_seconds", "taskbar_left"):
        assert _k in _tweak_keys, f"tweak missing: {_k}"
    assert "micfix_audit" in _repair_keys, "repair task missing: micfix_audit"
    print("  tools tab: cards launch popups + shortcuts OK")

