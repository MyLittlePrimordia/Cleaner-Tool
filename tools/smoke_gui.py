"""Headless smoke test for the Phase 2 GUI.

Drives the REAL Application class through its real init flow:
  * If the process is non-admin (normal case), the admin gate shows first,
    and _build_main_ui only runs after _continue_limited() — reproducing
    the exact flow the previous session's test missed.
  * Then clicks every tab via the pill switcher, selects every preset on
    every tab, enters Custom and toggles a task, enters Undo and toggles a
    revert, and exercises set_status/progress/log paths.

Run:  python -m tools.smoke_gui
"""

import sys
import pathlib

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from app import config_persist
from app.elevation import is_admin

results = []


def check(name, fn):
    try:
        fn()
        results.append((name, True, ""))
    except Exception as e:
        results.append((name, False, f"{type(e).__name__}: {e}"))


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


def main():
    import tkinter as tk
    from app import gui

    # run the harness checks FIRST — they set `results`, which the old
    # vacuous flow never touched
    run_checks()

    root = tk.Tk()
    root.withdraw()  # headless: don't flash a window

    app = gui.Application(root)

    # --- real init flow: pass the admin gate if it's showing ---
    if not is_admin():
        gate = None
        for w in root.winfo_children():
            if isinstance(w, gui.AdminGateFrame):
                gate = w
                break
        assert gate is not None, "Non-admin but AdminGateFrame not shown"
        gate._continue_limited()

    assert hasattr(app, "tabs"), "Application.tabs missing — _build_main_ui did not run"
    assert list(app.tabs.keys()) == ["Clean", "Repair", "Tweak", "Install"], \
        f"expected 4 tabs, got {list(app.tabs.keys())}"

    # --- drive every tab + preset + custom + undo ---
    for tab_name in ("Clean", "Repair", "Tweak"):
        app._switch_to(tab_name)
        page = app.tabs[tab_name]

        for preset in gui.PRESETS[tab_name].keys():
            page._select_preset(preset)
            selected = page.selected_tasks()
            expected = len(gui.PRESETS[tab_name][preset])
            assert len(selected) == expected, f"{tab_name}/{preset}: {len(selected)} != {expected}"

        page._enter_custom()
        assert page.mode == "custom"
        # toggle first task on (via its BooleanVar, as the switch would)
        key0 = page.tasks[0].key
        assert key0 in page.vars, f"{key0} missing from custom grid vars"
        page.vars[key0].set(True)
        sel = page.selected_tasks()
        assert any(t.key == key0 for t in sel), "custom selection broken"

        if tab_name == "Tweak":
            page._enter_undo()
            assert page.mode == "undo"
            reversible = [t for t in page.tasks if t.revert is not None]
            assert reversible, "no reversible tweaks on Tweak tab"
            # all undo vars default off
            assert all(not v.get() for v in page.vars.values()), "undo grid not all-off"
            first_rev = reversible[0].key
            page.vars[first_rev].set(True)
            undo_sel = page.undo_selected_tasks()
            assert any(t.key == first_rev for t in undo_sel), "undo selection broken"

    # --- Install tab: catalog browser, categories, selection, badges ---
    app._switch_to("Install")
    ipage = app.tabs["Install"]
    # Harness completeness (progressive-show change, 2026-09): the UI no
    # longer drains the catalog queue synchronously on the switch — it
    # shows the page with its placeholder while idle slices build. The
    # assertions below need complete rows, so this harness (a documented
    # direct caller, not the UI) finishes the queue explicitly.
    ipage._ensure_catalog_built()
    from app.app_catalog import APP_CATALOG, CATEGORY_ORDER
    assert isinstance(ipage, gui.InstallTab), "Install tab is not the catalog browser"
    # every catalog app has a var (plus Essentials 'task:' vars)
    n_task_vars = sum(1 for k in ipage.vars if k.startswith("task:"))
    n_app_vars = len(ipage.vars) - n_task_vars
    assert n_app_vars == len(APP_CATALOG), f"catalog vars {n_app_vars} != {len(APP_CATALOG)} apps"
    # audit fix: this used to be `assert A if B else True` — always True when
    # the hasattr guard failed, vacuously passing. Compare directly.
    assert n_task_vars == len(gui.TABS["Install"]), (
        f"essentials vars {n_task_vars} != {len(gui.TABS['Install'])} install tasks")
    # Essentials: checkbox rows exist and select into the shared pool
    ess = ipage.ess_vars
    assert len(ess) >= 11, f"expected 11 Essentials, got {len(ess)}"
    ess_key = next(iter(ess))
    ess[ess_key].set(True)
    sel = ipage.selected_apps()
    kinds = {(k, s.key if k == "task" else s["id"]) for k, s in sel}
    assert ("task", ess_key) in kinds, "checked Essential not in selection"
    ess[ess_key].set(False)
    # every app has a var; select-all on first category then none
    first_cat = CATEGORY_ORDER[0]
    ipage._set_category(first_cat, True)
    cat_apps = [a for a in APP_CATALOG if a["category"] == first_cat]
    sel = ipage.selected_apps()
    app_sel = [s["id"] for kind, s in sel if kind == "app"]
    assert app_sel == [a["id"] for a in cat_apps], "category select-all broken"
    ipage._set_category(first_cat, False)
    assert len(ipage.selected_apps()) == 0, "category deselect broken"
    # toggle one app directly
    some_id = APP_CATALOG[3]["id"]
    ipage.vars[some_id].set(True)
    app_sel = [s["id"] for kind, s in ipage.selected_apps() if kind == "app"]
    assert app_sel == [some_id]
    ipage.vars[some_id].set(False)
    # category collapse: toggle twice, body must hide then show
    hdr, body, open_ = ipage._cat_frames[first_cat]
    assert open_
    kids_before = len(body.winfo_children())
    assert kids_before > 0
    # simulate the header click handler by invoking toggle logic via the
    # stored state (the lambda closure isn't reachable; drive via arrow label)
    for w in hdr.winfo_children():
        if isinstance(w, tk.Label) and w.cget("text") == "▾":
            w.event_generate("<Button-1>")
            break
    root.update()
    assert not ipage._cat_frames[first_cat][2], "category did not collapse"
    for w in hdr.winfo_children():
        if isinstance(w, tk.Label) and w.cget("text") == "▸":
            w.event_generate("<Button-1>")
            break
    root.update()
    assert ipage._cat_frames[first_cat][2], "category did not re-open"
    app._switch_to("Clean")

    # --- pill switcher: persistent items + smooth animation (user-reported
    # flicker). The old code deleted all canvas items each frame; the fix
    # keeps item ids stable and only coords/colors change.
    sw = app.switch
    before_ids = sw.find_all()

    def pump_for(ms):
        # root.after callbacks only fire while the event loop is pumped —
        # without mainloop() we must update() in a loop, not once.
        import time as _t
        end = _t.monotonic() + ms / 1000.0
        while _t.monotonic() < end:
            root.update()
            _t.sleep(0.01)

    app._switch_to("Repair")
    pump_for(400)
    after_ids = sw.find_all()
    assert before_ids == after_ids, f"switch items recreated during animation (flicker): {before_ids} -> {after_ids}"
    assert app._switch_pos == 1.0, f"thumb did not reach target: {app._switch_pos}"
    app._switch_to("Tweak")
    pump_for(400)
    assert sw.find_all() == before_ids, "switch items recreated (flicker regression)"
    assert app._switch_pos == 2.0, f"thumb did not reach target: {app._switch_pos}"
    app._switch_to("Clean")
    pump_for(400)
    assert app._switch_pos == 0.0

    # rapid re-click retarget: fire three switches quickly, must land clean
    app._switch_to("Repair")
    app._switch_to("Tweak")   # retarget mid-flight
    pump_for(500)
    assert app._switch_pos == 2.0, f"retarget mid-flight broke: {app._switch_pos}"
    app._switch_to("Clean")
    pump_for(400)
    assert app._switch_pos == 0.0

    # --- menu structure (user-requested changes) ---
    # NOTE: never root.tk.call('menu', path, ...) — in Tcl, `menu <path>`
    # is the widget-CREATION command, so querying that way tries to create
    # an existing window ("window name already exists"). Use the Menu
    # widget's own python methods instead.
    menubar = None
    for w in root.winfo_children():
        if isinstance(w, tk.Menu):
            menubar = w
            break
    assert menubar is not None, "no menubar found on root"
    mb_end = int(menubar.index("end"))
    cascades = [menubar.entrycget(i, "label")
                for i in range(0, mb_end + 1)
                if menubar.type(i) == "cascade"]
    assert cascades == ["Quick Tools"], f"expected only Quick Tools, got {cascades}"
    tools_menu = root.nametowidget(menubar.entrycget(menubar.index("Quick Tools"), "menu"))
    idx_end = int(tools_menu.index("end"))
    item_labels = [tools_menu.entrycget(i, "label") if tools_menu.type(i) != "separator" else "—"
                   for i in range(0, idx_end + 1)]
    assert "Event Viewer" not in item_labels, "Event Viewer still in menu"
    assert "Task Manager" in item_labels and "Task Manager (Startup tab)" not in item_labels, \
        f"Task Manager rename broken: {item_labels}"
    for want in ("Startup Apps", "Uninstall Programs", "Storage Sense",
                 "Windows Update", "Network Settings", "Sound Settings",
                 "About", "Export Logs"):
        assert want in item_labels, f"menu missing {want}: {item_labels}"
    print("  menu items:", item_labels)

    # --- export logs: no UI log window anymore; in-memory log exports
    # to .txt via save dialog (dialog monkeypatched for the test) ---
    assert not hasattr(app, "log_area"), "log window should be gone from the UI"
    assert not hasattr(app, "_details_btn"), "View Details button should be gone"
    import tkinter.filedialog as _fd
    import os as _os
    out_path = _os.path.join(_os.environ.get("TEMP", "."), "cleaner_smoke_export.txt")
    if _os.path.exists(out_path):
        _os.remove(out_path)
    app.log("SMOKE TEST LOG LINE")
    root.update()
    _orig_as = _fd.asksaveasfilename
    _fd.asksaveasfilename = lambda **kw: out_path
    try:
        app.export_logs()
    finally:
        _fd.asksaveasfilename = _orig_as
    root.update()
    content = open(out_path, encoding="utf-8").read()
    assert "SMOKE TEST LOG LINE" in content, "exported log missing lines"
    assert "Cleaner Tool log export" in content, "exported log missing header"
    print("  export OK ->", out_path)

    # --- status / progress / log thread-safety paths ---
    app.set_status("Test status")
    app._set_progress(1, 2)
    app.log("test line")
    root.update()
    app._set_buttons_enabled(False)
    root.update()
    app._set_buttons_enabled(True)
    root.update()

    # --- REAL mouse-event regression (user-reported crash: <Enter> fired
    # before _pressing/_hovering existed). Synthesize Enter/Press/Release/
    # Leave over every animated widget on every tab in every mode.
    def synth_events(widget):
        widget.event_generate("<Enter>")
        root.update()
        widget.event_generate("<ButtonPress-1>")
        root.update()
        widget.event_generate("<ButtonRelease-1>")
        root.update()
        widget.event_generate("<Leave>")
        root.update()

    def collect_animated(container, acc):
        for w in container.winfo_children():
            if isinstance(w, (gui.AnimatedButton, gui.ToggleSwitch)):
                acc.append(w)
            collect_animated(w, acc)

    for tab_name in ("Clean", "Repair", "Tweak"):
        app._switch_to(tab_name)
        page = app.tabs[tab_name]
        for mode_fn in (page._enter_custom, page._enter_undo if tab_name == "Tweak" else (lambda: None)):
            mode_fn()
            root.update()
            widgets = []
            collect_animated(page, widgets)
            for w in widgets:
                synth_events(w)
        # preset cards too
        page._select_preset(list(gui.PRESETS[tab_name].keys())[0])
        root.update()
        widgets = []
        collect_animated(page, widgets)
        for w in widgets:
            synth_events(w)

    # --- tooltip wrap cap: long descriptions must be flattened+cut ---
    long_desc = "This is a very long description " * 10
    tl = gui.Tooltip(app.status_lbl, long_desc)
    tl._show()
    shown = gui.Tooltip._shared_label.cget("text")
    assert "\n" not in shown and len(shown) <= 171 and shown.endswith("…"), f"tooltip not capped: {shown!r}"
    assert int(gui.Tooltip._shared_label.cget("wraplength")) > 0, "tooltip should wrap, not cut"
    tl._hide()
    root.update()

    # --- toggle switch geometry: MK3 — all smooth polygons (antialiased) ---
    tvar = tk.BooleanVar(value=True)
    tswitch = gui.ToggleSwitch(root, tvar)
    root.update()
    types = [tswitch.type(i) for i in tswitch.find_all()]
    # MK3 (user feedback 'low-res'): capsule + knob are smooth polygons now
    # (Tk antialiases them), never rect/oval (whose rasterization is jagged
    # on Windows). 3 items: capsule, shadow knob, knob.
    assert types == ["polygon", "polygon", "polygon"], f"not all smooth polygons: {types}"
    assert tswitch.W == 44 and tswitch.H == 24, "MK3 size changed"
    assert tswitch.W > tswitch.H, "switch should be wider than tall"
    # critically-damped settle — NO overshoot by design (audit note: the
    # old `overshot` flag asserted nothing; a spring that no longer exists)
    import time as _tt
    t0 = _tt.monotonic()
    tvar.set(False)
    while _tt.monotonic() - t0 < 1.5:
        root.update(); _tt.sleep(0.01)
        if tswitch._anim_after is None:
            break
    assert tswitch._pos == 0.0, f"animation did not settle: {tswitch._pos}"
    root.update()
    print("  toggle: MK3 smooth-polygon capsule OK, critically-damped settle verified")

    # --- tooltip regression (user-reported crash spam): hover, then
    # destroy the hovered widget's page (tab rebuild), then hover again.
    # The shared tooltip must survive page rebuilds without dead refs. ---
    test_lbl = tk.Label(root, text="hover me", bg="#0D1117")
    test_lbl.pack()
    tl = gui.Tooltip(test_lbl, "Test tooltip text")
    tl._show()
    root.update()
    assert gui.Tooltip._shared_tip is not None and gui.Tooltip._shared_tip.winfo_exists()
    shown_w = gui.Tooltip._shared_tip
    test_lbl.destroy()  # simulate the page rebuild destroying the hovered label
    root.update()
    tl2_lbl = tk.Label(root, text="hover me 2")
    tl2_lbl.pack()
    tl2 = gui.Tooltip(tl2_lbl, "Second tooltip")
    tl2._show()  # must not raise 'bad window path name'
    root.update()
    tl2._hide()
    root.update()
    assert gui.Tooltip._shared_tip.winfo_exists(), "shared tooltip died with its first parent"

    # --- RAM purge honest-failure contract: the runtime PS script must
    # contain no backslash-quote artifacts (they parse-error in PS) ---
    from app.tasks import clean_tasks as _ct
    captured = {}
    def _fake_run_cmd(ctx, command, shell=True, timeout=None):
        captured["cmd"] = command
        captured["shell"] = shell
        return 0
    class _StubCtx:
        def log(self, m): pass
        def set_status(self, m): pass
        def cancelled(self): return False
    orig_run = _ct.run_cmd
    _ct.run_cmd = _fake_run_cmd
    try:
        _ct.purge_ram_working_sets(_StubCtx())
    finally:
        _ct.run_cmd = orig_run
    ps = captured["cmd"][3] if isinstance(captured["cmd"], list) else captured["cmd"]
    assert captured["shell"] is False, "RAM purge must run with shell=False"
    assert "kernel32.dll" in ps and "SetProcessWorkingSetSize" in ps
    # the PS source must use single-quoted strings — literal backslash-quote
    # sequences in the SCRIPT TEXT (not python source) are what broke it
    script_text = ps if isinstance(ps, str) else str(ps)
    assert '\\\\"' not in script_text, "backslash-quote artifact in runtime PS script"

    # --- task-state registry round trip (badge source) ---
    config_persist.mark_tweak_applied("smoke_test_tweak")
    assert config_persist.get_tweak_state().get("smoke_test_tweak")
    config_persist.mark_tweak_reverted("smoke_test_tweak")
    assert not config_persist.get_tweak_state().get("smoke_test_tweak")

    # --- legacy config migration round trip ---
    legacy = {
        "schedule_enabled": False,
        "selected_tasks": {
            "Clean": ["shader_cache"],
            "Games": ["gamer_launchers", "game_files"],
            "Advanced": ["adv_vmp", "adv_copilot"],
        },
    }
    migrated = config_persist._migrate_selected_tasks(legacy)
    st = migrated["selected_tasks"]
    assert "Games" not in st and "Advanced" not in st, "legacy tabs not folded"
    assert "launcher_cache" in st["Clean"], "gamer_launchers not remapped to launcher_cache"
    assert "game_files" in st["Clean"]
    assert "adv_vmp" not in st["Tweak"], "cut task migrated"
    assert "adv_copilot" in st["Tweak"]

    root.destroy()

    failures = [r for r in results if not r[1]]
    for name, ok, err in results:
        print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"  ({err})" if err else ""))
    # overall assertions already raised if broken; reaching here = pass
    print("SMOKE TEST: ALL PASS" if not failures else f"SMOKE TEST: {len(failures)} FAILURES")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
