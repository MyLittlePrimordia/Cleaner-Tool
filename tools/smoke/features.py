"""Install profiles, Game Night, Drive Toolkit, Startup Manager

H16: extracted verbatim from tools/smoke_gui.py main(), L1090-1308.

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
    # --- Feature 7: Install profiles ("My Setups") ----------------------
    _profiles_before = config_persist.get_install_profiles()
    try:
        _inst = app.tabs["Install"]
        # "My Setups" button was deliberately removed from the Install tab
        # (see app/gui.py) — only the underlying save/load logic is tested.
        _ids = _inst.profile_ids()
        assert isinstance(_ids, list)
        # tick one real catalog app, save it, clear, load it back
        _first_id = next(iter(_inst.vars), None)
        assert _first_id is not None, "Install catalog has no apps to profile"
        _inst.apply_profile_ids([])
        assert _inst.profile_ids() == [], "clearing the selection failed"
        _inst.vars[_first_id].set(True)
        _saved_ids = _inst.profile_ids()
        assert _first_id in _saved_ids
        assert config_persist.save_install_profile("smoke_setup", _saved_ids)
        _inst.apply_profile_ids([])
        _applied, _missing = _inst.apply_profile_ids(
            config_persist.get_install_profiles()["smoke_setup"])
        assert _applied == len(_saved_ids) and _missing == 0, (_applied, _missing)
        assert _inst.vars[_first_id].get(), "loaded profile did not tick the app"
        # an id that no longer exists is reported, never silently dropped
        _applied2, _missing2 = _inst.apply_profile_ids(["definitely-not-an-app"])
        assert (_applied2, _missing2) == (0, 1), (_applied2, _missing2)
        assert config_persist.delete_install_profile("smoke_setup")
        assert "smoke_setup" not in config_persist.get_install_profiles()
        # the dialog builds and lists what is stored
        _pdlg = gui.InstallProfilesDialog(root, _inst)
        root.update()
        assert _pdlg._rows_holder is not None
        _pdlg._close()
        root.update()
    finally:
        _inst.apply_profile_ids([])
        config_persist.update_config(
            lambda cfg: cfg.__setitem__("install_profiles", dict(_profiles_before)))

    # --- Feature 9: Game Night ------------------------------------------
    from app import game_night as _gn
    # the tweak set is the existing preset, never a private copy
    from app.tab_presets import PRESETS as _PRESETS
    assert _gn.preset_task_keys() == list(_PRESETS["Tweak"]["Game Session"])
    assert _gn.preset_task_keys(), "Game Session preset resolved to nothing"
    # only ticked apps ever map to an exe, unknown keys are ignored
    assert _gn.exe_names_for([]) == ()
    assert _gn.exe_names_for(["not-a-real-app"]) == ()
    assert "chrome.exe" in _gn.exe_names_for(["browsers"])
    assert "discord.exe" not in _gn.exe_names_for(["browsers"])
    # nothing on the closeable list may be a game launcher or a driver app
    _all_exes = " ".join(_gn.exe_names_for(list(_gn.APP_KEYS))).lower()
    for _banned in ("steam", "epicgames", "riot", "battle.net", "gog",
                    "nvidia", "amd", "razer", "logi", "synapse"):
        assert _banned not in _all_exes, f"unsafe app on the quiet list: {_banned}"
    # the tasks resolve to real, revertable Tweak tasks
    _gn_tasks = app._game_night_tasks()
    assert _gn_tasks, "Game Night resolved no tasks"
    assert all(t.revert is not None for t in _gn_tasks), \
        "every Game Night tweak must be undoable"
    # _game_in_progress reads active_hit as a PROPERTY: a regression to
    # active_hit() would be swallowed by the guard and silently report
    # "no game running" forever, so pin the contract here.
    from app.session_pilot import SessionPilot as _SP
    assert isinstance(getattr(_SP, "active_hit"), property), \
        "SessionPilot.active_hit is no longer a property"
    assert app._game_in_progress() in (True, False)

    # state round-trips and survives a reload
    _gn_before = config_persist.get_game_night()
    try:
        config_persist.set_game_night(True, ["game_mode"])
        assert config_persist.get_game_night()["active"] is True
        assert config_persist.get_game_night()["keys"] == ["game_mode"]
        # ending clears the keys so a later End can never re-revert them
        config_persist.set_game_night(False)
        assert config_persist.get_game_night()["active"] is False
        assert config_persist.get_game_night()["keys"] == []
        # End with nothing recorded must be a quiet no-op, not a run
        app.end_game_night()
        assert config_persist.get_game_night()["active"] is False
        # dialog builds in both states and shows the right button — merged
        # into PilotDialog (audit fix: GameNightDialog removed, its
        # controls now live as a "manual session" section in Auto-Pilot)
        config_persist.set_game_night(True, ["game_mode"])
        _pd = gui.PilotDialog(root, app)
        root.update()
        assert "Game Mode: On" in _pd._manual_btn._text, _pd._manual_btn._text
        assert len(_pd._close_vars) == len(_gn.APP_KEYS)
        assert not any(v.get() for v in _pd._close_vars.values()), \
            "optional app quieting must default to off"
        _pd._close()
        root.update()
        config_persist.set_game_night(False)
        _pd2 = gui.PilotDialog(root, app)
        root.update()
        assert "Game Mode: Off" in _pd2._manual_btn._text, _pd2._manual_btn._text
        _pd2._close()
        root.update()
    finally:
        config_persist.set_game_night(bool(_gn_before.get("active")),
                                      _gn_before.get("keys"))
        config_persist.set_game_night_close_apps(_gn_before.get("close_apps"))

    # --- Feature: Drive Toolkit (Health / Speed / Capacity) --------------
    from app import drive_toolkit as _dtk
    # pure helpers must never raise even with no real hardware behind them
    assert isinstance(_dtk.list_physical_disks(), list)
    assert isinstance(_dtk.list_fixed_drives(), list)
    assert isinstance(_dtk.list_removable_drives(), list)
    # capacity_test refuses non-removable drives regardless of caller —
    # this must hold even off Windows (IS_WINDOWS gate short-circuits
    # first there, but the removable-only gate is the one that matters
    # on a real machine, so pin its wording too).
    _cap_err = _dtk.capacity_test("C:\\", mode="quick")
    assert _cap_err["ok"] is False and _cap_err["error"], _cap_err
    # deterministic block generation: same (seed, index) -> same bytes,
    # different index -> different bytes (spoof-resistance depends on this)
    _b1 = _dtk._block_bytes(7, 3, 256)
    _b2 = _dtk._block_bytes(7, 3, 256)
    _b3 = _dtk._block_bytes(7, 4, 256)
    assert _b1 == _b2 and _b1 != _b3
    # dialog builds and defaults to the Health tab
    _dtd = gui.DriveToolkitDialog(root, app)
    root.update()
    assert _dtd._view == "health"
    assert hasattr(_dtd, "_health_panel")
    _dtd._switch("speed")
    root.update()
    assert hasattr(_dtd, "_speed_bar")
    _dtd._switch("capacity")
    root.update()
    assert hasattr(_dtd, "_cap_bar")
    _dtd._close()
    root.update()

    # --- Feature: Startup Manager -----------------------------------------
    from app import startup_manager as _sm
    assert isinstance(_sm.list_startup_items(), list)
    # config round-trips and coerces junk safely
    _su_before = config_persist.get_startup_disabled()
    try:
        assert config_persist.add_startup_disabled(
            {"source": "hkcu_run", "name": "SmokeTestItem",
             "command": "C:\\smoke.exe", "value_type": "REG_SZ"})
        _found = [r for r in config_persist.get_startup_disabled()
                  if r["name"] == "SmokeTestItem"]
        assert len(_found) == 1
        assert config_persist.remove_startup_disabled("hkcu_run", "SmokeTestItem")
        assert not [r for r in config_persist.get_startup_disabled()
                    if r["name"] == "SmokeTestItem"]
        # dialog builds and lists what's there
        _sud = gui.StartupManagerDialog(root, app)
        root.update()
        assert isinstance(_sud._rows, dict)
        _sud._close()
        root.update()
    finally:
        config_persist.update_config(
            lambda cfg: cfg.__setitem__("startup_disabled", list(_su_before)))

    # --- improvements.txt items: Discord Cache_Data, Windows.old age gate,
    #     light network repair -----------------------------------------
    import os as _os
    from app.tasks import launcher_paths as _lp
    _discord_leaves = [_os.path.basename(p) for p in _lp.DISCORD_CACHE_PATHS if p]
    assert "Cache_Data" in _discord_leaves, \
        "Discord Cache_Data path missing — the real cache-bulk folder"
    assert _discord_leaves.count("Cache_Data") == 4, \
        "Cache_Data should appear once per Discord branch (stable/PTB/Canary/Dev)"
    # nothing that could hold login/session data was added alongside it
    for _banned in ("Local Storage", "Session Storage", "IndexedDB", "databases"):
        assert not any(_banned in p for p in _lp.DISCORD_CACHE_PATHS), _banned

    import tempfile as _tf, time as _time
    from app.tasks import clean_tasks as _ct
    from app.utils import TaskContext as _TC
    _logs = []
    _cctx = _TC(log=_logs.append, set_status=lambda *_: None)
    _tmp_root = _tf.mkdtemp()
    _saved_root = _ct._SYSTEMDRIVE_ROOT
    try:
        _ct._SYSTEMDRIVE_ROOT = _tmp_root
        _wo = _os.path.join(_tmp_root, "Windows.old")
        _os.makedirs(_wo)
        with open(_os.path.join(_wo, "junk.bin"), "wb") as _f:
            _f.write(b"x" * 1000)
        # fresh -> must survive
        _logs.clear()
        _r1 = _ct.clean_windows_update_leftovers(_cctx)
        assert _r1 == 0 and _os.path.isdir(_wo), \
            "a same-day Windows.old must not be deleted"
        assert any("leaving it for now" in l for l in _logs)
        # aged 20 days -> must be removed
        _old_t = _time.time() - 20 * 86400
        _os.utime(_wo, (_old_t, _old_t))
        _r2 = _ct.clean_windows_update_leftovers(_cctx)
        assert _r2 > 0 and not _os.path.isdir(_wo), \
            "a 20-day-old Windows.old should be removed"
    finally:
        _ct._SYSTEMDRIVE_ROOT = _saved_root

    from app.tasks import repair_tasks as _rt
    assert any(t.key == "network_light" for t in gui.TABS["Repair"]), \
        "network_light task not registered on the Repair tab"
    _nl_task = next(t for t in gui.TABS["Repair"] if t.key == "network_light")
    assert _nl_task.default is False, "network_light must be opt-in like every repair task"
    _saved_run_cmd = _rt.run_cmd
    try:
        _seen_cmds = []
        _rt.run_cmd = lambda ctx, cmd, timeout=None: (_seen_cmds.append(cmd), 0)[1]
        _logs2 = []
        from app.utils import TaskContext as _TC2
        _rt.repair_network_light(_TC2(log=_logs2.append, set_status=lambda *_: None))
        assert _seen_cmds == ["ipconfig /flushdns", "netsh winsock reset"], _seen_cmds
        assert not any("/release" in c or "/renew" in c for c in _seen_cmds), \
            "light network fix must never touch the IP lease"
    finally:
        _rt.run_cmd = _saved_run_cmd

