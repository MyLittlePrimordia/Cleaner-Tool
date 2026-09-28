"""Tab pages: every tab/preset/custom/undo, install catalog, menu, status

H16: extracted verbatim from tools/smoke_gui.py main(), L586-924.

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
    # row icons: every catalog row shows its app logo (user call), max 22px
    # so rows stay compact; PhotoImages cached (no GC thinning, no reloads)
    _icons = getattr(ipage, "_icon_cache", {})
    _got = [k for k, v in _icons.items() if v is not None]
    # Audit fix: this used to hard-code the expected total (166+13+4+13 =
    # 196) — a magic sum of catalog + manual + embedded-bundle + Essentials
    # counts that silently went stale the moment any of those four grew
    # (it already had, twice: 200 real vs 196 expected). The actual thing
    # worth catching is a MISS (a wrong filename or bad image bytes leaving
    # an entry None) — pin that invariant, not a row count nothing here
    # computes from source, so this can never go stale again.
    assert _icons, "no catalog rows were built — icon cache is empty"
    assert len(_got) == len(_icons), \
        f"{len(_got)}/{len(_icons)} row icons loaded — some icon failed to load"
    assert len(_icons) >= len(APP_CATALOG), \
        f"icon cache ({len(_icons)}) smaller than the catalog itself ({len(APP_CATALOG)})"
    for _img in _icons.values():
        if _img is not None:
            assert _img.width() <= 32 and _img.height() <= 32, \
                (_img.width(), _img.height())
    _srow = ipage._app_rows["Valve.Steam"]
    assert any(isinstance(w, tk.Label) and str(w.cget("image")).strip()
               for w in _srow.winfo_children()), "Steam row has no icon"
    # Essentials: checkbox rows exist and select into the shared pool
    ess = ipage.ess_vars
    assert len(ess) >= 13, f"expected 13 Essentials, got {len(ess)}"
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
    # Drive the exact callable a header click runs. This used to synthesise
    # <Button-1> on the arrow Label, which only worked by accident: this
    # page's content is embedded in a ScrollableRoundedPanel canvas, and Tk
    # does NOT deliver event_generate to an unmapped widget. Under this
    # suite's headless (withdrawn) root the click was silently dropped, so
    # the assertion below tested nothing. That is the same hazard the
    # suite already documents at the Tweak pills ("never event_generate
    # (unmapped widgets eat it)"). InstallTab now exposes the toggle
    # callable, which is what the old comment here said it wanted.
    _toggle = ipage._cat_toggles[first_cat]
    # the arrow label still has to say the right thing, so keep that check
    _arrows = [w.cget("text") for w in hdr.winfo_children()
               if isinstance(w, tk.Label)]
    assert "\u25be" in _arrows, f"no down-arrow on the category header: {_arrows}"
    _toggle()
    root.update()
    assert not ipage._cat_frames[first_cat][2], "category did not collapse"
    _arrows = [w.cget("text") for w in hdr.winfo_children()
               if isinstance(w, tk.Label)]
    assert "\u25b8" in _arrows, f"arrow did not flip to collapsed: {_arrows}"
    _toggle()
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

    # --- menu structure (user redesign 2026-09: Quick Tools GONE) ---
    # NOTE: never root.tk.call('menu', path, ...) — in Tcl, `menu <path>`
    # is the widget-CREATION command, so querying that way tries to create
    # an existing window ("window name already exists"). Use the Menu
    # widget's own python methods instead.
    # The dropdown is retired: no toolbar button, no tk.Menu. Its items
    # live in the Tools tab as link-rows (asserted below).
    assert not str(app.root.cget("menu")), "native menubar must stay detached (white strip)"
    assert not hasattr(app, "_tools_btn"), "Quick Tools button must be gone (Tools tab owns it)"
    assert getattr(app, "_tools_menu", None) is None, "Quick Tools menu must be gone"
    print("  menu: Quick Tools retired (Tools tab owns shortcuts)")

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
                if getattr(w, "_harness_skip", False):
                    # e.g. the DNS Test button: covered by dedicated tests,
                    # and a synth click would open a live dialog mid-suite
                    pass
                else:
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

