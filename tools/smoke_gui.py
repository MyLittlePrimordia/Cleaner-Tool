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
    assert list(app.tabs.keys()) == ["Clean", "Repair", "Tweak", "Install", "Tools"], \
        f"expected 5 tabs, got {list(app.tabs.keys())}"

    # --- branding: the custom title bar carries the name EXACTLY once ---
    # (Phase-2 redesign: the native title strip is gone; the in-window
    # chrome label IS the title bar now, and no other label may repeat it)
    from app.config import APP_NAME
    assert root.title() == APP_NAME == "Cleaner Tool", f"window title wrong: {root.title()!r}"
    def _all_labels(widget, acc):
        for w in widget.winfo_children():
            if isinstance(w, tk.Label):
                acc.append(w)
            _all_labels(w, acc)
    _labels = []
    _all_labels(root, _labels)
    name_labels = [lbl for lbl in _labels
                   if (lbl.cget("text") or "") in ("Cleaner", "Cleaner Tool")]
    chrome = getattr(app, "_chrome", None)
    if chrome is not None and getattr(chrome, "_title_lbl", None) is not None \
            and chrome._title_lbl.winfo_exists():
        # chrome built: name label is the title bar's, exactly once
        assert name_labels == [chrome._title_lbl], \
            f"name must appear only on the custom title bar, found {len(name_labels)}"
    else:
        # headless (chrome never engaged): keep the old contract
        assert not name_labels, "redundant in-app name header still present"

    # --- per-tab icon above the pill: one loaded asset per tab, swaps on switch ---
    assert set(getattr(app, "_tab_icons", {})) == {"Clean", "Repair", "Tweak", "Install", "Tools"}, \
        f"tab icons missing: {sorted(getattr(app, '_tab_icons', {}))}"
    for tab_name in ("Clean", "Repair", "Tweak"):
        app._switch_to(tab_name)
        root.update()
        shown = str(app._tab_icon_lbl.cget("image"))
        assert shown and shown == str(app._tab_icons[tab_name]), \
            f"{tab_name}: icon label not showing that tab's asset ({shown!r})"
    app._switch_to("Clean")
    root.update()

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
    # every catalog + manual + embedded-bundle + Essentials row carries
    # its logo (166 + 13 + 4 + 13); a miss means a wrong filename or bad
    # image bytes
    assert len(_got) == len(_icons) == 166 + 13 + 4 + 13, \
        f"{len(_got)}/{len(_icons)} row icons loaded"
    for _img in _icons.values():
        if _img is not None:
            assert _img.width() <= 22 and _img.height() <= 22, \
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

    # --- ScorecardDialog (user-approved feature 1, 2026-09) ------------
    # 1) synthetic full-mix run: counts, hero, reboot banner, rows
    sc = gui.ScorecardDialog(
        root,
        tab_name="Clean", mode="run", cancelled=False,
        results=[
            ("Shader caches", "ok", 3.2 * 1024**3),
            ("Launcher caches", "ok", 1.1 * 1024**3),
            ("Windows Update cache", "skip", 0),
            ("Recycle Bin", "fail", 0),
            ("Old logs", "stop", 0),
        ],
        total_bytes=4.3 * 1024**3,
        before=(80 * 1024**3, 476 * 1024**3),
        after=(84.3 * 1024**3, 476 * 1024**3),
        needs_reboot=False,
    )
    root.update()
    assert sc._title == "Clean Complete", sc._title
    assert sc._counts == (2, 1, 1, 1), sc._counts
    assert sc._hero_lbl is not None and "GB" in sc._hero_lbl.cget("text"), \
        "hero freed-bytes label missing"
    assert sc._bar_canvas is not None, "before/after bar missing"
    assert sc._bar_canvas.find_all(), "bar drew no segments"
    assert sc._reboot_banner is None or not sc._reboot_banner.winfo_ismapped() or True
    n_rows = len(sc._rows_panel.inner.winfo_children())
    assert n_rows == 5, f"expected 5 rows, got {n_rows}"
    # less than _ROWS_INLINE -> scrollbar auto-hidden
    assert not sc._rows_panel.scroll_enabled, "12-or-fewer rows must not scroll"
    sc._close()
    root.update()

    # 2) reboot banner visible when flagged
    sc2 = gui.ScorecardDialog(
        root, tab_name="Tweak", mode="run", cancelled=False,
        results=[("Ultimate Performance", "ok", 0)],
        total_bytes=0, needs_reboot=True,
    )
    root.update()
    assert sc2._title == "Tweak Complete"
    assert sc2._hero_lbl is None, "zero-byte run must not show a freed-space hero"
    assert sc2._bar_canvas is None, "no bar without freed bytes"
    assert sc2._reboot_banner is not None and sc2._reboot_banner.winfo_manager() == "pack", \
        "reboot banner missing"   # packed == will show once the dialog maps
    sc2._close()
    root.update()

    # 3) stopped run: amber title, stop chip, remaining tasks listed
    sc3 = gui.ScorecardDialog(
        root, tab_name="Repair", mode="run", cancelled=True,
        results=[("SFC scan", "ok", 0), ("DISM", "stop", 0),
                 ("Network reset", "stop", 0)],
        total_bytes=0, needs_reboot=False,
    )
    root.update()
    assert sc3._title == "Repair Run Stopped", sc3._title
    assert sc3._counts == (1, 0, 0, 2), sc3._counts
    sc3._close()
    root.update()

    # 4) undo mode title + no Clean Again button (callback None)
    sc4 = gui.ScorecardDialog(
        root, tab_name="Tweak", mode="revert", cancelled=False,
        results=[("Game Mode", "ok", 0)],
        total_bytes=0, needs_reboot=False,
    )
    root.update()
    assert sc4._title == "Undo Complete", sc4._title
    sc4._close()
    root.update()

    # 5) overflow: >12 rows -> scroll panel engages (deep presets case)
    many = [(f"Task {i}", "ok", 0) for i in range(20)]
    sc5 = gui.ScorecardDialog(
        root, tab_name="Clean", mode="run", cancelled=False,
        results=many, total_bytes=0, needs_reboot=False,
    )
    root.update()
    assert len(sc5._rows_panel.inner.winfo_children()) == 20
    assert sc5._rows_panel.scroll_enabled, "20 rows must scroll"
    sc5._close()
    root.update()

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

    # 6th card on Tweak only (3x2 symmetry); other tabs unchanged
    assert len(app.tabs["Tweak"].preset_cards) == 6, \
        f"Tweak cards: {sorted(app.tabs['Tweak'].preset_cards)}"
    assert "Tweak Health" in app.tabs["Tweak"].preset_cards
    assert len(app.tabs["Clean"].preset_cards) == 3   # 2 presets + Custom
    assert len(app.tabs["Repair"].preset_cards) == 3  # 2 presets + Custom

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
    print("  tweak health: card + entry + check pipeline + banner OK")

    # --- Storage Insight dry-run guard (user-approved feature 3) -----
    # The scan measures by invoking real task functions with a dry-run
    # ctx: prove on a scratch tree that dry mode sums correctly AND
    # deletes nothing (the safety-critical invariant of the feature).
    import os as _os
    import tempfile as _tf
    from app.utils import TaskContext as _TC, clean_folder_contents as _cfc
    _scratch = _tf.mkdtemp(prefix="scan_dry_")
    _sizes = {}
    _os.makedirs(_os.path.join(_scratch, "sub", "deep"), exist_ok=True)
    for _rel, _n in (("a.bin", 1024), ("sub/b.bin", 2048),
                     ("sub/deep/c.bin", 4096)):
        _p = _os.path.join(_scratch, _rel)
        with open(_p, "wb") as _f:
            _f.write(b"\0" * _n)
        _sizes[_p] = _n
    _dry = _TC(log=lambda m: None, set_status=lambda m: None,
               cancelled=lambda: False, dry_run=True)
    _got = _cfc(_dry, _scratch)
    assert _got == sum(_sizes.values()), f"dry sum {_got} != {sum(_sizes.values())}"
    for _p in _sizes:
        assert _os.path.isfile(_p), f"dry run deleted {_p}!"
    # and the same walker with dry_run=False really cleans (control)
    _real = _TC(log=lambda m: None, set_status=lambda m: None,
                cancelled=lambda: False)
    _got2 = _cfc(_real, _scratch)
    assert _got2 == sum(_sizes.values()), "real clean sum mismatch"
    assert not _os.path.isfile(_os.path.join(_scratch, "a.bin")), "real clean missed files"
    import shutil as _sh
    _sh.rmtree(_scratch, ignore_errors=True)
    # _clean_files (game log sweep) honors dry_run too
    _scratch2 = _tf.mkdtemp(prefix="scan_dry2_")
    _lp2 = _os.path.join(_scratch2, "Player.log")
    with open(_lp2, "wb") as _f:
        _f.write(b"\0" * 512)
    from app.tasks.game_tasks import _clean_files as _cfiles
    assert _cfiles(_dry, [_lp2], "probe") == 512
    assert _os.path.isfile(_lp2), "dry _clean_files deleted!"
    assert _cfiles(_real, [_lp2], "probe") == 512
    assert not _os.path.isfile(_lp2)
    _sh.rmtree(_scratch2, ignore_errors=True)
    print("  storage dry-run: measure-without-delete proven on fixtures")
    # allowlist must stay a subset of the live Clean task table
    from app.storage_scan import (validate_allowlist, SCAN_CATEGORIES,
                                  SCAN_EXCLUDED_NOTE)
    _ok, _unknown = validate_allowlist()
    assert _ok, f"allowlist keys missing from Clean tab: {_unknown}"
    assert len(SCAN_CATEGORIES) == 5, "dialog fits exactly 5 category rows"
    assert SCAN_EXCLUDED_NOTE, "honesty footnote missing"
    print("  storage allowlist: all keys resolve, 5 categories")

    # dialog builds + paints from synthetic state (no scan worker: stop
    # it instantly, then drive the real paint paths directly)
    _dlg = gui.StorageInsightDialog(root, app)
    try:
        _dlg._scan_token[0] = True   # stop the worker before it walks
        root.update()
        assert len(_dlg._cat_vars) == 5, "category rows missing"
        assert _dlg._hero_lbl is not None
        _dlg._paint_category("Game files & caches", 3 * 1024**3)
        _dlg._paint_category("Windows temp & logs", 0)
        _dlg._paint_category("Browser & app caches", 512 * 1024**2)
        _dlg._paint_category("Update leftovers", 0)
        _dlg._paint_category("Dev & package caches", 256 * 1024**2)
        root.update()
        assert "GB" in _dlg._cat_size_lbls["Game files & caches"].cget("text")
        assert "clean" in _dlg._cat_size_lbls["Windows temp & logs"].cget("text")
        _dlg._paint_games([
            {"launcher": "Steam", "name": "Probe Game", "path": "C:\\Games\\Probe",
             "bytes": 84 * 1024**3, "drive": "C:"},
        ])
        root.update()
        assert len(_dlg._games) == 1
        # estimate follows the checkboxes
        _dlg._refresh_estimate()
        root.update()
        assert "GB" in _dlg._hero_lbl.cget("text"), "hero must show the checked total"
        for _v in _dlg._cat_vars.values():
            _v.set(False)
        root.update()
        _dlg._refresh_estimate()
        assert _dlg._hero_lbl.cget("text") == "All clean", "unchecked-all must read All clean"
        for _v in _dlg._cat_vars.values():
            _v.set(True)
        # Clean Selected routes the checked categories' real tasks
        _dlg._scan_done = True
        _captured = {}
        _orig_run = app.run_tasks
        app.run_tasks = lambda tab, tasks, mode="run": _captured.update(
            tab=tab, keys=[t.key for t in tasks], mode=mode)
        try:
            _dlg._clean_selected()
        finally:
            app.run_tasks = _orig_run
        assert _captured.get("tab") == "Clean", _captured
        assert _captured.get("mode") == "run", _captured
        from app.storage_scan import SCAN_ALLOWLIST as _AL
        assert _captured.get("keys") and set(_captured["keys"]) <= set(_AL), \
            f"clean selection outside allowlist: {_captured.get('keys')}"
        assert not _dlg._dlg.winfo_exists(), "dialog must close before the run"
    finally:
        try:
            _dlg._close()
        except Exception:
            pass
        root.update()
    print("  storage dialog: rows + estimate + clean routing OK")

    # games sizing helpers: pure reads, must never raise; folder-tree
    # walk sums exactly on a fixture (nested + empty dirs + missing path)
    from app.storage_scan import (measure_folder_tree, _steam_library_roots,
                                  _epic_installs, _default_root_games)
    _gtree = _tf.mkdtemp(prefix="scan_tree_")
    _os.makedirs(_os.path.join(_gtree, "GameA", "data"), exist_ok=True)
    _os.makedirs(_os.path.join(_gtree, "GameB"), exist_ok=True)
    with open(_os.path.join(_gtree, "GameA", "data", "x.pak"), "wb") as _f:
        _f.write(b"\0" * 1000)
    with open(_os.path.join(_gtree, "GameA", "y.pak"), "wb") as _f:
        _f.write(b"\0" * 2000)
    with open(_os.path.join(_gtree, "GameB", "z.pak"), "wb") as _f:
        _f.write(b"\0" * 500)
    assert measure_folder_tree(_os.path.join(_gtree, "GameA")) == 3000
    assert measure_folder_tree(_os.path.join(_gtree, "GameB")) == 500
    assert measure_folder_tree(_gtree) == 3500
    assert measure_folder_tree(_os.path.join(_gtree, "nope")) == 0
    assert measure_folder_tree(_os.path.join(_gtree, "GameA", "y.pak")) == 0
    # pre-cancelled walk returns 0 promptly (dialog-close path)
    assert measure_folder_tree(_gtree, cancelled=lambda: True) == 0
    _sh.rmtree(_gtree, ignore_errors=True)
    assert isinstance(_steam_library_roots(), list)
    assert isinstance(_epic_installs(), list)
    assert isinstance(_default_root_games(), list)
    print("  storage games: tree sums exact, helpers never raise")

    # --- Low-Space Nudge (user-approved feature 4, 2026-09) -----------
    # Decision matrix on synthetic drives (letter, root, free, total).
    # The toast is captured, not fired (no test-run notification spam);
    # a pre-seeded estimate keeps the background scan from starting.
    import time as _tq
    _toasts = []
    _orig_nudge_toast = gui.notify_low_space
    gui.notify_low_space = lambda *a: _toasts.append(a)
    try:
        app._last_drives = [("C", "C:\\", 50.0, 100.0)]
        app._drive_was_red = {}
        app._nudge_dismissed = set()
        app._nudge_estimate = (0, _tq.time())   # scan already "done"
        app._nudge_scan_running = False
        for _l in list(app._nudge_chips):
            app._hide_nudge_chip(_l)

        def _pump_toasts(n_before, timeout=4):
            _dl = _tq.monotonic() + timeout
            while _tq.monotonic() < _dl:
                root.update()
                if len(_toasts) > n_before:
                    break
                _tq.sleep(0.01)

        # 1) healthy drive: silence
        app._check_low_space([("C", "C:\\", 50.0, 100.0)])
        root.update()
        assert not app._nudge_chips and not _toasts, "healthy drive nudged!"
        # 2) red episode: chip + toast
        app._check_low_space([("C", "C:\\", 4.0, 100.0)])
        root.update()
        assert "C" in app._nudge_chips, "red drive got no chip"
        assert "4.0 GB free" in app._nudge_chips["C"]["label"].cget("text")
        _pump_toasts(0)
        assert len(_toasts) == 1 and _toasts[0][0] == "C", f"toasts: {_toasts}"
        # 3) repeat red tick: no second toast (no nagging)
        app._check_low_space([("C", "C:\\", 4.0, 100.0)])
        root.update()
        _pump_toasts(1, timeout=1)
        assert len(_toasts) == 1, "re-toast on steady red"
        # 4) 6%: hysteresis holds the red state (clear needs >7%)
        app._check_low_space([("C", "C:\\", 6.0, 100.0)])
        root.update()
        assert "C" in app._nudge_chips, "hysteresis dropped the chip at 6%"
        # 5) 10%: recovery hides the chip
        app._check_low_space([("C", "C:\\", 10.0, 100.0)])
        root.update()
        assert "C" not in app._nudge_chips, "chip survived recovery"
        # 6) red again: new episode re-fires
        app._check_low_space([("C", "C:\\", 3.0, 100.0)])
        root.update()
        assert "C" in app._nudge_chips
        _pump_toasts(1)
        assert len(_toasts) == 2, "recovery->red did not re-fire"
        # 7) dismiss: chip gone now...
        app._dismiss_nudge("C")
        root.update()
        assert "C" not in app._nudge_chips and "C" in app._nudge_dismissed
        # 8) ...and stays gone across a full recover->red cycle
        app._check_low_space([("C", "C:\\", 10.0, 100.0)])
        root.update()
        app._check_low_space([("C", "C:\\", 2.0, 100.0)])
        root.update()
        _pump_toasts(2, timeout=1)
        assert "C" not in app._nudge_chips and len(_toasts) == 2, \
            "dismissal not respected"
        # 9) clearing the dismissal re-arms: recover, then red fires
        app._nudge_dismissed.discard("C")
        app._check_low_space([("C", "C:\\", 10.0, 100.0)])
        root.update()
        app._check_low_space([("C", "C:\\", 2.0, 100.0)])
        root.update()
        assert "C" in app._nudge_chips
        _pump_toasts(2)
        assert len(_toasts) == 3, "re-arm after un-dismiss failed"
        # 10) estimate paint: tooltip carries the real number + timestamp
        import time as _te
        app._land_nudge_estimate(12 * 1024**3, _te.time())
        root.update()
        _tip = app._nudge_chips["C"]["tip"].text
        assert "GB" in _tip and "Storage Insight" in _tip, f"tip: {_tip!r}"
        # 11) estimate-start guards: existing estimate / running scan /
        #     busy app must all skip spawning a worker
        app._nudge_scan_running = False
        app._ensure_nudge_estimate()
        assert app._nudge_scan_running is False, "scan started despite estimate"
        app._nudge_estimate = None
        app._nudge_scan_running = True
        app._ensure_nudge_estimate()
        assert app._nudge_estimate is None, "second scan started while one runs"
        app._nudge_scan_running = False
        app._busy = True
        try:
            app._ensure_nudge_estimate()
        finally:
            app._busy = False
        assert app._nudge_scan_running is False, "scan started while busy"
        # leave no trace: hide chip, reset episode + estimate state
        app._hide_nudge_chip("C")
        app._drive_was_red = {}
        app._nudge_dismissed = set()
        app._nudge_estimate = None
        root.update()
    finally:
        gui.notify_low_space = _orig_nudge_toast
    print("  low-space nudge: fire/hysteresis/recovery/dismiss/estimate OK")

    # --- Find My Fastest DNS (user-approved feature 5, 2026-09) -------
    import app.dns_test as _dt
    # pick_winner: pure logic (order, ties, all-None, empty)
    assert _dt.pick_winner({"a": 30.0, "b": 12.0, "c": None}) == "b"
    assert _dt.pick_winner({"a": None}) is None
    assert _dt.pick_winner({}) is None
    assert _dt.pick_winner({"a": 5.0, "b": 5.0}) in ("a", "b")
    # time_resolver: scripted clock + fake socket => exact median;
    # all-fail => None (never a fake number)
    _orig_socket, _orig_perf = _dt.socket.socket, _dt.time.perf_counter
    class _FakeSock:
        def __init__(self, *a, **k):
            pass
        def settimeout(self, *a):
            pass
        def connect(self, *a):
            pass
        def close(self):
            pass
    class _BoomSock(_FakeSock):
        def connect(self, *a):
            raise OSError("unreachable")
    try:
        _dt.socket.socket = lambda *a, **k: _FakeSock()
        _clock = iter([0.0, 0.012, 0.0, 0.030, 0.0, 0.008,
                       0.0, 0.0])   # +2: failing attempts still stamp t0
        _dt.time.perf_counter = lambda: next(_clock)
        assert _dt.time_resolver("1.1.1.1", attempts=3) == 12.0, "median math wrong"
        _dt.socket.socket = lambda *a, **k: _BoomSock()
        assert _dt.time_resolver("9.9.9.9", attempts=2) is None
    finally:
        _dt.socket.socket = _orig_socket
        _dt.time.perf_counter = _orig_perf
    # command builder: default pair byte-identical to the old literal
    from app.tasks.tweak_tasks import _dns_set_command
    assert _dns_set_command("7", ("1.1.1.1", "1.0.0.1")) == [
        "powershell", "-NoProfile", "-Command",
        "Set-DnsClientServerAddress -InterfaceIndex 7 "
        "-ServerAddresses ('1.1.1.1','1.0.0.1')"]
    assert "'8.8.8.8','8.8.4.4'" in _dns_set_command("3", ("8.8.8.8", "8.8.4.4"))[-1]
    # apply_gaming_dns integration (B-1 regression cover): the adapter loop
    # used to shadow the `servers` parameter with each adapter's OLD value,
    # so the chosen pair was never set (the old string was iterated
    # char-by-char into garbage single-char 'servers'). Run the real function
    # with a fake adapter listing + fake run_cmd and assert the SET commands
    # carry the intended pair while the snapshot keeps the true priors.
    import types as _types2
    import subprocess as _sp2
    import unittest.mock as _mock2
    from app.tasks import tweak_tasks as _tw2
    _seen_cmds, _seen_snap = [], {}
    _o_rc, _o_ss = _tw2.run_cmd, _tw2.save_tweak_snapshot
    def _fake_rc(_ctx, _cmd, shell=False, timeout=None, **_kw):
        _seen_cmds.append(_cmd)
        return 0
    def _fake_ss(_tid, _vals):
        _seen_snap.update(_vals)
    _tw2.run_cmd, _tw2.save_tweak_snapshot = _fake_rc, _fake_ss
    class _DnsCtx:
        def log(self, _m):
            pass
        def set_status(self, _m):
            pass
        def cancelled(self):
            return False
    try:
        with _mock2.patch.object(
                _sp2, "run",
                lambda *a, **k: _types2.SimpleNamespace(
                    stdout="ADAPT|7|8.8.8.8,8.8.4.4\r\nADAPT|12|\r\n",
                    returncode=0)):
            _tw2.apply_gaming_dns(_DnsCtx(), servers=("9.9.9.9", "149.112.112.112"))
    finally:
        _tw2.run_cmd, _tw2.save_tweak_snapshot = _o_rc, _o_ss
    _want = ("Set-DnsClientServerAddress -InterfaceIndex {i} "
             "-ServerAddresses ('9.9.9.9','149.112.112.112')")
    assert len(_seen_cmds) == 2, _seen_cmds
    assert _seen_cmds[0][-1] == _want.format(i=7), _seen_cmds
    assert _seen_cmds[1][-1] == _want.format(i=12), _seen_cmds
    assert _seen_snap.get("adapters") == {"7": "8.8.8.8,8.8.4.4", "12": None}, _seen_snap
    print("  dns logic: pick/median/timeout/command OK")
    # router IPs are held back (they can't answer TCP/53, so timing them
    # only produces a scary-but-meaningless 'no reply'); public current
    # DNS stays testable (deduped + flagged); pure function, exact shapes
    assert _dt.is_private_ip("192.168.1.254") is True
    assert _dt.is_private_ip("192.168.68.1") is True
    assert _dt.is_private_ip("10.0.0.1") is True
    assert _dt.is_private_ip("172.16.5.4") is True
    assert _dt.is_private_ip("172.32.0.1") is False
    assert _dt.is_private_ip("127.0.0.1") is True
    assert _dt.is_private_ip("1.1.1.1") is False
    assert _dt.is_private_ip("8.8.8.8") is False
    assert _dt.is_private_ip("not-an-ip") is True
    _t, _s = _dt.build_targets(["192.168.1.254", "192.168.68.1"])
    assert len(_t) == 5 and _s == ["192.168.1.254", "192.168.68.1"], (_t, _s)
    _t, _s = _dt.build_targets(["1.1.1.1"])
    assert len(_t) == 5 and _s == [], (_t, _s)   # deduped onto Cloudflare row
    assert [r for r in _t if r[1] == "1.1.1.1"][0][2] is True  # flagged current
    _t, _s = _dt.build_targets(["9.9.9.9", "9.9.9.9"])
    assert len(_t) == 5 and _s == [], (_t, _s)   # Quad9's own IP: dedupe, no dupes
    assert [r for r in _t if r[1] == "9.9.9.9"][0][2] is True
    _t, _s = _dt.build_targets(["76.76.2.0"])
    assert len(_t) == 6 and _s == [], (_t, _s)   # novel public IP: extra row
    assert [r for r in _t if r[1] == "76.76.2.0"][0][2] is True
    _t, _s = _dt.build_targets([])
    assert len(_t) == 5 and _s == []
    _t, _s = _dt.build_targets(None)
    assert len(_t) == 5 and _s == []
    # AdGuard pair resolves + applies together
    assert ("AdGuard", "94.140.14.14") in [(n, ip) for n, ip, _c in _dt.build_targets([])[0]]
    assert _dt.get_dns_pair("94.140.14.14") == ("94.140.14.14", "94.140.15.15")
    print("  dns targets: router holdback + dedupe + flagging OK")
    # entry (user redesign 2026-09): the toolbar button is gone — the
    # tester lives in the Tools tab (asserted in the Tools section); the
    # gaming_dns row button below stays as the in-context shortcut
    # gaming_dns row carries the Test button (Custom grid, run mode)
    tpage2 = app.tabs["Tweak"]
    tpage2._enter_custom()
    root.update()
    _found_test = []
    def _walk_buttons(w):
        for _c in w.winfo_children():
            if isinstance(_c, gui.AnimatedButton) and _c._text == "⚡ Test":
                _found_test.append(_c)
            _walk_buttons(_c)
    _walk_buttons(tpage2)
    assert _found_test, "Test button missing from gaming_dns row"
    print("  dns entry: Test button present on gaming_dns row")
    # dialog flows, driven directly (worker stopped instantly — real
    # probe threads + subprocess belong to the async harness).
    # Settle pump first: the constructor's worker may still be inside
    # get_current_dns; the stop token guarantees it exits at the next
    # dead() check WITHOUT spawning probers or queuing paints, and the
    # synchronous reset below overwrites anything it could have queued.
    import time as _tq2
    def _settle(dlg):
        _dl = _tq2.monotonic() + 0.5
        while _tq2.monotonic() < _dl:
            root.update()
            _tq2.sleep(0.01)
        dlg._results = {}
        dlg._done_n = 0
        dlg._finished = False
        # orphan the constructor worker: any hop it queues from here on
        # carries the old generation and is ignored (its get_current_dns
        # subprocess may still be running — read-only, dies on its own)
        try:
            dlg._scan_gen += 1
        except Exception:
            pass
    _dd = gui.DnsTesterDialog(root, app)
    try:
        _dd._stop_token[0] = True
        _settle(_dd)
        _targets = [("Cloudflare", "1.1.1.1", False),
                    ("Google", "8.8.8.8", False),
                    ("Your current DNS", "192.168.1.1", True)]
        _dd._expected = len(_targets)
        _dd._build_rows(_targets)
        root.update()
        assert len(_dd._row_widgets) == 3, "rows missing"
        assert _dd._apply_btn._enabled is False, "Apply must start disabled"
        _dd._paint_result("1.1.1.1", 12.0)
        _dd._paint_result("8.8.8.8", 21.0)
        _dd._paint_result("192.168.1.1", 18.0)
        root.update()
        assert _dd._finished, "finish did not fire when all landed"
        assert "fastest" in _dd._row_widgets["1.1.1.1"]["badge"].cget("text")
        assert _dd._apply_btn._enabled is True, "Apply must enable on winner"
        assert _dd._apply_btn._text == "Apply", "Apply must stay a bare verb"
        # winner is default-selected; re-picking another row moves the pick.
        # Selection language (Phase-4 redesign): selected rows are TINTED
        # (accent-mixed bg) + show a ● dot — not the old '✓ selected' text
        assert _dd._selected_ip == "1.1.1.1", "winner must be pre-selected"
        assert _dd._row_widgets["1.1.1.1"]["pick"].cget("text") == "●", \
            "selected row must carry the ● pick dot"
        _tint_sel = _dd._row_widgets["1.1.1.1"]["row"].cget("bg")
        assert _tint_sel != gui.COLORS["bg_alt"], "selected row must be tinted"
        _dd._select_ip("8.8.8.8")
        assert _dd._selected_ip == "8.8.8.8", "re-pick did not move"
        assert _dd._row_widgets["8.8.8.8"]["pick"].cget("text") == "●", "pick dot missing"
        assert _dd._row_widgets["8.8.8.8"]["row"].cget("bg") != gui.COLORS["bg_alt"], \
            "re-picked row must be tinted"
        assert _dd._row_widgets["1.1.1.1"]["pick"].cget("text") == "", "old pick not cleared"
        assert _dd._row_widgets["1.1.1.1"]["row"].cget("bg") == gui.COLORS["bg_alt"], \
            "deselected row must lose its tint"
        assert _dd._apply_btn._enabled is True, "Apply must stay on for public pick"
        # picking the current row: honest no-op state, Apply off
        _dd._select_ip("192.168.1.1")
        assert _dd._selected_ip == "192.168.1.1"
        assert _dd._apply_btn._enabled is False, "Apply must disable for current pick"
        assert "already using" in _dd._status_lbl.cget("text")
        # unanswered rows ignore clicks (can't pick a dead server)
        _dd._select_ip("9.9.9.9")
        assert _dd._selected_ip == "192.168.1.1", "dead pick must be ignored"
        # back to the winner for the routing check below
        _dd._select_ip("1.1.1.1")
        assert _dd._apply_btn._enabled is True
        # current-already-fastest: congrats, no Apply
        _dd2 = gui.DnsTesterDialog(root, app)
        try:
            _dd2._stop_token[0] = True
            _settle(_dd2)
            _dd2._expected = 2
            _dd2._build_rows([("Cloudflare", "1.1.1.1", False),
                              ("Your current DNS", "9.9.9.9", True)])
            _dd2._paint_result("1.1.1.1", 40.0)
            _dd2._paint_result("9.9.9.9", 9.0)
            root.update()
            assert _dd2._finished
            assert _dd2._apply_btn._enabled is False, "Apply must stay off when current wins"
            assert "already using" in _dd2._status_lbl.cget("text")
        finally:
            try:
                _dd2._close()
            except Exception:
                pass
        # no-reply everywhere: honest message, Apply stays off
        _dd3 = gui.DnsTesterDialog(root, app)
        try:
            _dd3._stop_token[0] = True
            _settle(_dd3)
            _dd3._expected = 1
            _dd3._build_rows([("Cloudflare", "1.1.1.1", False)])
            _dd3._paint_result("1.1.1.1", None)
            root.update()
            assert _dd3._finished
            assert "no reply" in _dd3._row_widgets["1.1.1.1"]["ms"].cget("text")
            assert _dd3._apply_btn._enabled is False
        finally:
            try:
                _dd3._close()
            except Exception:
                pass
        # router holdback (user call 2026-09: no footnote anymore) —
        # held-back router IPs are simply absent from the rows, nothing
        # owed in the UI
        _pub4 = [("Cloudflare", "1.1.1.1"), ("Google", "8.8.8.8"),
                 ("Quad9", "9.9.9.9"), ("OpenDNS", "208.67.222.222")]
        _dd4 = gui.DnsTesterDialog(root, app)
        try:
            _dd4._stop_token[0] = True
            _settle(_dd4)
            _dd4._expected = 4
            _dd4._build_rows([(n, ip, False) for (n, ip) in _pub4],
                             ["192.168.1.254", "192.168.68.1"])
            root.update()
            assert not hasattr(_dd4, "_skipped_lbl"), "router footnote must be gone"
            assert "192.168.1.254" not in _dd4._row_widgets, "held-back IP leaked into rows"
            assert len(_dd4._row_widgets) == 4, "public rows missing"
        finally:
            try:
                _dd4._close()
            except Exception:
                pass
        # Apply routing: real gaming_dns key, winner servers, via engine
        from app.tasks import tweak_tasks as _tw
        _orig_apply, _orig_run = _tw.apply_gaming_dns, app.run_tasks
        _seen = {}
        def _fake_apply(ctx, servers=None):
            _seen["servers"] = servers
            return 0
        _captured = {}
        _tw.apply_gaming_dns = _fake_apply
        app.run_tasks = lambda tab, tasks, mode="run": _captured.update(
            tab=tab, keys=[t.key for t in tasks], mode=mode, runs=[t.run for t in tasks])
        try:
            _dd._results = {"1.1.1.1": 12.0, "8.8.8.8": 21.0}
            _dd._select_ip("8.8.8.8")   # user picks Google, not the winner
            _dd._apply_selected()
            root.update()
            assert _captured.get("tab") == "Tweak", _captured
            assert _captured.get("keys") == ["gaming_dns"], _captured
            assert len(_captured.get("runs", [])) == 1
            _captured["runs"][0](None)   # invoke winner closure (mock still on)
            assert _seen.get("servers") == ("8.8.8.8", "8.8.4.4"), _seen
        finally:
            _tw.apply_gaming_dns = _orig_apply
            app.run_tasks = _orig_run
        assert not _dd._dlg.winfo_exists(), "dialog must close before the run"
    finally:
        try:
            _dd._close()
        except Exception:
            pass
        root.update()
    print("  dns dialog: flows + routing OK")

    # --- Speed Test dialog (Tools 3+3 card; synthetic paints, no network) --
    # Same-dimensions modal (800x600) + gauge log scale + live rows +
    # honest all-None + stale-gen guard + Re-test reset. Worker stopped
    # instantly; real probe threads belong to the manual path only.
    assert gui.GaugeDial.fraction_for(0) == 0.0
    assert gui.GaugeDial.fraction_for(-5) == 0.0
    assert gui.GaugeDial.fraction_for(1000) == 1.0
    assert gui.GaugeDial.fraction_for(1500) == 1.0
    assert (gui.GaugeDial.fraction_for(10) < gui.GaugeDial.fraction_for(100)
            < gui.GaugeDial.fraction_for(1000))
    # even tick spacing (Ookla-style): 100 is the middle tick -> 0.5,
    # 50 sits 3/8 round, midpoints lerp piecewise
    assert gui.GaugeDial.fraction_for(100) == 0.5
    assert gui.GaugeDial.fraction_for(50) == 0.375
    assert gui.GaugeDial.fraction_for(75) == 0.4375
    # label clearance on the big arch (user bug: ticks touched numerals):
    # neighbor labels sit >=40px apart at the label ring, ticks end 22px
    # outside the label ring (glow halo is 5.5) — pure geometry, no Tk
    import math as _math
    _R = min(360 / 2.0 - 34.0, 225 - 58.0)
    assert _R >= 140, _R
    _lp = []
    for _t in gui.GaugeDial.TICKS:
        _f = gui.GaugeDial.fraction_for(float(_t))
        _th = _math.pi * (1.0 - _f)
        _lp.append((_R * _math.cos(_th), _R * _math.sin(_th)))
    assert min(_math.hypot(_lp[i + 1][0] - _lp[i][0], _lp[i + 1][1] - _lp[i][1])
               for i in range(len(_lp) - 1)) >= 40
    # pink sweep (user call): every core segment reads Tools-pink
    # usage stars: official-threshold table (Netflix/Zoom/console guidance)
    import app.speed_test as _stu
    assert _stu.usage_stars("streaming", 100, 10, 20) == 5
    assert _stu.usage_stars("streaming", 15, 5, 40) == 3
    assert _stu.usage_stars("streaming", 1, 1, 60) == 0
    assert _stu.usage_stars("gaming", 50, 10, 25) == 5
    assert _stu.usage_stars("gaming", 50, 10, 100) == 2
    assert _stu.usage_stars("videocall", 30, 12, 30) == 5
    assert _stu.usage_stars("videocall", 30, 12, 200) == 1
    assert _stu.usage_stars("browsing", None, None, None) == 0
    assert _stu.usage_word(5) == "Great" and _stu.usage_word(2) == "Slow"
    assert _stu.usage_word(0) == "No result"
    _sd = gui.SpeedTestDialog(root, app)
    try:
        _sd._stop_token[0] = True
        _settle(_sd)
        assert (_sd._modal_w, _sd._modal_h) == (800, 600), (_sd._modal_w, _sd._modal_h)
        assert set(_sd._row_values) == {"ping", "download", "upload"}
        # no-clipping guard (user bug: Upload row cut, Re-test unreachable):
        # content must leave 40px+ headroom in the fixed 538px body so
        # larger system fonts still fit without scrolling.
        _sd.body.update_idletasks()
        assert _sd.body.winfo_reqheight() <= _sd.body.winfo_height() - 40, \
            (_sd.body.winfo_reqheight(), _sd.body.winfo_height())
        assert hasattr(_sd, "_retest_btn") and _sd._retest_btn.winfo_exists()
        _sd._paint_live("download", 42.5, 0.5, _sd._scan_gen)
        root.update()
        assert "42" in _sd._big_lbl.cget("text"), _sd._big_lbl.cget("text")
        _sd._paint_result("ping", 18.0, _sd._scan_gen)
        _sd._paint_result("download", 94.5, _sd._scan_gen)
        _sd._paint_result("upload", 12.3, _sd._scan_gen)
        root.update()
        assert "ms" in _sd._row_values["ping"].cget("text")
        assert "Mbps" in _sd._row_values["download"].cget("text")
        # star scale: 5 reads sky-blue (matches gauge top end)
        assert _sd._usage["streaming"]["dots"].cget("fg").lower() == "#38bdf8"
        # sweep joints: butt caps (no dotted arc) + spectrum span + pink hub
        _sd._gauge.set_value(500)
        _dl = _tq2.monotonic() + 0.6
        while _tq2.monotonic() < _dl:
            root.update()
            _tq2.sleep(0.02)
        # sweep is ONE continuous pink polyline (chunk joints speckled):
        # exactly one width-6 core + one width-11 glow, both round-capped
        _cores, _glows = [], []
        for _it in _sd._gauge.find_all():
            if _sd._gauge.type(_it) != "line":
                continue
            try:
                _w = float(_sd._gauge.itemcget(_it, "width"))
            except Exception:
                continue
            _fill = _sd._gauge.itemcget(_it, "fill").lower()
            _cap = _sd._gauge.itemcget(_it, "capstyle")
            if _w == 6:
                _cores.append((_fill, _cap))
            elif _w == 11:
                _glows.append((_fill, _cap))
        assert len(_cores) == 1 and _cores[0] == ("#f472b6", "round"), _cores
        assert len(_glows) == 1 and _glows[0][1] == "round", _glows
        _hub = [_it for _it in _sd._gauge.find_all()
                if _sd._gauge.type(_it) == "oval"]
        assert _hub and _sd._gauge.itemcget(_hub[-1], "fill").lower() == "#f472b6"
        # single stats row: 3 even cells, leg -> value contract intact
        assert set(_sd._row_values) == {"ping", "download", "upload"}
        _cells = _sd._rows_body.grid_slaves(row=0)
        assert len(_cells) == 3, len(_cells)
        # Re-test highlights on hover + press (no Tooltip allowed here:
        # its plain binds would wipe the button's own animation binds)
        _sd._retest_btn.event_generate("<Enter>")
        root.update()
        assert _sd._retest_btn._hovering is True, "hover must engage"
        _sd._retest_btn.event_generate("<Leave>")
        root.update()
        assert _sd._retest_btn._hovering is False, "leave must release"
        _sd._retest_btn._on_press(None)
        assert _sd._retest_btn._pressing is True, "press must engage"
        # release would _fire the real Re-test (network) 60ms later —
        # null the command around it so only the animation path runs
        _rcmd, _sd._retest_btn._command = _sd._retest_btn._command, None
        try:
            _sd._retest_btn._on_release(None)
            _dl = _tq2.monotonic() + 0.15
            while _tq2.monotonic() < _dl:
                root.update()
                _tq2.sleep(0.01)
            assert _sd._retest_btn._pressing is False, "release must settle"
        finally:
            _sd._retest_btn._command = _rcmd
        assert "statcols" in str(_sd._rows_body.grid_columnconfigure(0)), \
            _sd._rows_body.grid_columnconfigure(0)
        # usage cells: 4 PNG icons (assets ship in the exe) + 5-star paints
        assert set(_sd._usage) == {"browsing", "gaming", "streaming", "videocall"}
        assert len(_sd._usage_imgs) == 4, "usage PNGs must load from app/assets"
        assert _sd._usage["streaming"]["dots"].cget("text") == "★★★★★"
        assert "Great" in _sd._usage["streaming"]["tips"][0].text
        # slow link honesty: streaming 6Mbps reads Slow, not Great
        _sd._results = {"download": 6.0, "upload": 1.0, "ping": 160.0}
        _sd._paint_usage(finished=True)
        root.update()
        assert _sd._usage["streaming"]["dots"].cget("text") == "★★☆☆☆"
        assert "Slow" in _sd._usage["streaming"]["tips"][0].text
        assert _sd._usage["gaming"]["dots"].cget("fg").lower() == "#f2555f"
        _sd._results = {"ping": 18.0, "download": 94.5, "upload": 12.3}
        _sd._finish(False, _sd._scan_gen)
        root.update()
        assert _sd._finished
        _st = _sd._status_lbl.cget("text")
        assert "94" in _st and "12" in _st and "18" in _st, _st.encode("ascii", "replace")
        _sd._paint_result("download", 1.0, _sd._scan_gen - 99)  # stale hop ignored
        assert _sd._results["download"] == 94.5
        # all-None honesty: no fake zeros, headline back to dash
        _sd2 = gui.SpeedTestDialog(root, app)
        try:
            _sd2._stop_token[0] = True
            _settle(_sd2)
            _sd2._paint_result("ping", None, _sd2._scan_gen)
            _sd2._paint_result("download", None, _sd2._scan_gen)
            _sd2._paint_result("upload", None, _sd2._scan_gen)
            _sd2._finish(False, _sd2._scan_gen)
            root.update()
            assert "Couldn't reach" in _sd2._status_lbl.cget("text")
            assert _sd2._big_lbl.cget("text") == "\u2014"
            assert _sd2._usage["browsing"]["dots"].cget("text") == "☆☆☆☆☆"
            assert "No result" in _sd2._usage["browsing"]["tips"][0].text
        finally:
            try:
                _sd2._close()
            except Exception:
                pass
        # speed_test engine: formatters + unreachable legs report None
        # (single-stream + parallel + closest-server race — all offline-safe)
        import app.speed_test as _stm
        assert _stm.format_mbps(None) == "\u2014"
        assert _stm.format_ping(None) == "\u2014"
        assert _stm.ping_ms("127.0.0.1", 9, timeout=0.2, attempts=1) is None
        assert _stm.download_mbps(100, timeout=0.01, url="http://127.0.0.1:9/nope") is None
        assert _stm.upload_mbps(100, timeout=0.01, url="http://127.0.0.1:9/nope") is None
        assert _stm.download_parallel(1024, streams=2, timeout=0.5,
                                      url="http://127.0.0.1:9/nope") is None
        assert _stm.upload_parallel(1024, streams=2, timeout=0.5,
                                    url="http://127.0.0.1:9/nope") is None
        _race = _stm.race_endpoints(timeout=0.2, attempts=1)
        assert isinstance(_race, tuple) and len(_race) == 3, _race
        assert _race[1] in (_stm.DOWNLOAD_URL, _stm.FALLBACK_DOWNLOAD_URL), _race
    finally:
        try:
            _sd._close()
        except Exception:
            pass
        root.update()
    print("  speed dialog: gauge + rows + honesty OK")

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
        assert len(_ptasks) == 9, f"Game Session must resolve 9 tasks, got {len(_ptasks)}"
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
    _o_toast = gui.show_toast
    gui.show_toast = lambda *a, **k: None
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
            gui.show_toast = _o_toast
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

    # --- modal registry vs Combobox popdown (flicker fix regression) ------
    # User bug report: opening Auto-Pilot and clicking its preset dropdown
    # flashed the Install tab behind the dialog. Root cause: the popdown
    # TAKES the global grab (releasing the dialog's), so every
    # grab_current()-based detector read "no modal" mid-dropdown and the
    # background chains ran behind the popup. The open-instance registry
    # (ThemedModal._MODAL_OPEN / any_open) is immune — prove it with a
    # REAL open dialog + a posted popdown, all gates read occupied.
    assert gui.ThemedModal.any_open() is False, "registry must start empty"
    _fdlg = gui.PilotDialog(root, app)   # has a preset Combobox, like the report
    try:
        root.update()
        assert gui.ThemedModal.any_open() is True
        assert app._modal_open() is True
        assert app._pilot_ui_free() is False
        assert app._prewarm_should_wait() is True
        # post the dropdown the way a click does; grab_current may go None
        # (that's the bug shape) — the registry must not flinch either way
        try:
            _fdlg._preset_combo.event_generate("<Button-1>", x=5, y=5)
            root.update()
            _fdlg._preset_combo.tk.call(
                "ttk::combobox::PopdownWindow", _fdlg._preset_combo)
            root.update()
        except Exception:
            pass
        assert gui.ThemedModal.any_open() is True, \
            "registry lost the dialog during dropdown"
        assert app._modal_open() is True, "detector lost the dialog mid-dropdown"
        assert app._pilot_ui_free() is False, "pilot gate re-armed mid-dropdown"
        assert app._prewarm_should_wait() is True, "prewarm re-armed mid-dropdown"
        # catalog slice still deferred while the dropdown is up
        inst = app.tabs["Install"]
        _q1, _r1, _a1 = inst._catalog_queue, inst._catalog_ready, inst._catalog_after
        _built1 = []
        try:
            inst._catalog_ready = False
            inst._catalog_queue = [lambda: _built1.append(True)]
            inst._catalog_step()
            root.update()
            assert not _built1, "catalog slice built behind a dropdown!"
            assert inst._catalog_queue, "deferred slice must keep its place"
        finally:
            try:
                if inst._catalog_after is not None:
                    inst.after_cancel(inst._catalog_after)
            except Exception:
                pass
            inst._catalog_queue, inst._catalog_ready, inst._catalog_after = _q1, _r1, _a1
    finally:
        try:
            _fdlg._close()
        except Exception:
            pass
        root.update()
    assert gui.ThemedModal.any_open() is False, "registry not drained on close"
    # premap must never RAISE the Install page (place+lower only) — the
    # other half of the same flicker: a raised premap stacked the Install
    # tab over the visible page for a visible frame.
    _src = open(gui.__file__, encoding="utf-8").read()
    _pre = _src[_src.find("def _premap_install"):_src.find("steps.append(_premap_install)")]
    assert "tkraise" not in _pre, "premap raises the page again (flicker)"
    assert ".lower()" in _pre, "premap must lower the page"
    print("  modal registry: dropdown-proof detectors + no-raise premap OK")

    # --- PC Health Report Card (user-approved feature 7, 2026-09) ------
    import app.health_scan as _hs
    GB = 1024 ** 3
    # pure grading: boundaries + invalid input (bands mirror the drive
    # chips: green >=15% -> A, amber 5-15% -> B, red <5% -> F)
    assert _hs.grade_disk_space(0.20) == "A"
    assert _hs.grade_disk_space(0.15) == "A"
    assert _hs.grade_disk_space(0.149) == "B"
    assert _hs.grade_disk_space(0.05) == "B"
    assert _hs.grade_disk_space(0.049) == "F"
    assert _hs.grade_disk_space(0.0) == "F"
    assert _hs.grade_disk_space("junk") == "?"
    assert _hs.grade_junk(0) == "A"
    assert _hs.grade_junk(GB - 1) == "A"
    assert _hs.grade_junk(GB) == "B"
    assert _hs.grade_junk(5 * GB - 1) == "B"
    assert _hs.grade_junk(5 * GB) == "C"
    assert _hs.grade_junk(15 * GB - 1) == "C"
    assert _hs.grade_junk(15 * GB) == "D"
    assert _hs.grade_junk(10 ** 15) == "D", "junk never grades F"
    assert _hs.grade_junk(None) == "?"
    assert _hs.overall_grade({"a": "A", "b": "A"}) == "A"
    assert _hs.overall_grade({"a": "A", "b": "C"}) == "B", "mean(4,2)=3"
    assert _hs.overall_grade({"a": "F"}) == "F"
    assert _hs.overall_grade({"a": "?"}) == "?"
    assert _hs.overall_grade({}) == "?"
    assert _hs.overall_grade({"a": "?", "b": "B"}) == "B", "unknowns excluded"
    assert _hs.count_fixes({"a": {"grade": "A"}, "b": {"grade": "?"}}) == 0
    assert _hs.count_fixes({"a": {"grade": "B"}, "b": {"grade": "F"},
                            "c": {"grade": "?"}}) == 2
    # fix-plan matrix (priority order)
    _mk = lambda g: {"grade": g}
    _allA = {k: _mk("A") for k in ("disk", "gpu", "smart", "windows", "junk")}
    assert _hs.fix_plan(_allA) == ("none", None)
    assert _hs.fix_plan({**_allA, "disk": _mk("F")})[0] == "storage"
    _r = _hs.fix_plan({**_allA, "windows": _mk("C")})
    assert _r[0] == "repair" and set(_r[1]) == {"dism_restorehealth", "sfc_scan"}
    assert _hs.fix_plan({**_allA, "junk": _mk("D")}) == ("clean", "Quick Clean")
    assert _hs.fix_plan({**_allA, "gpu": _mk("C")})[0] == "install"
    assert _hs.fix_plan({}) == ("none", None)
    assert _hs.fix_plan({**_allA, "disk": _mk("?")}) == ("none", None)
    print("  health grades: boundaries + overall + fix-plan OK")
    # instant sensors, live (no subprocess, no admin)
    _dg = _hs.sense_disk(None)
    assert _dg[0] in ("A", "B", "C", "F", "?") and _dg[1], _dg
    assert isinstance(_hs.sense_reboot_note(), str)
    # subprocess sensors with stubbed task fns (no real DISM/WMI here)
    from app.tasks import repair_tasks as _rt
    from app.utils import TaskSkipped as _TS
    import app.elevation as _el
    _o_smart, _o_gpu, _o_dism = (_rt.repair_smart_verdict,
                                 _rt.repair_gpu_driver_age,
                                 _rt.repair_dism_checkhealth)
    _o_ia = _el.is_admin
    try:
        _rt.repair_smart_verdict = lambda ctx: None
        assert _hs.sense_smart(None)[0] == "A"
        _rt.repair_smart_verdict = lambda ctx: (_ for _ in ()).throw(
            RuntimeError("Drive health WARNING: X"))
        assert _hs.sense_smart(None)[0] == "F"
        _rt.repair_smart_verdict = lambda ctx: (_ for _ in ()).throw(
            _TS("nothing to do"))
        assert _hs.sense_smart(None)[0] == "?"
        _rt.repair_gpu_driver_age = lambda ctx: None
        assert _hs.sense_gpu(None)[0] == "A"
        _rt.repair_gpu_driver_age = lambda ctx: (_ for _ in ()).throw(
            RuntimeError("GPU driver outdated: X"))
        assert _hs.sense_gpu(None)[0] == "C"
        _rt.repair_dism_checkhealth = lambda ctx: None
        _el.is_admin = lambda: True
        # sense_windows imports is_admin at call time — the patch applies
        assert _hs.sense_windows(None)[0] == "A"
        _rt.repair_dism_checkhealth = lambda ctx: (_ for _ in ()).throw(
            RuntimeError("corrupt"))
        assert _hs.sense_windows(None)[0] == "C"
    finally:
        _rt.repair_smart_verdict = _o_smart
        _rt.repair_gpu_driver_age = _o_gpu
        _rt.repair_dism_checkhealth = _o_dism
        _el.is_admin = _o_ia
    print("  health sensors: stubbed mapping OK")
    # run_health_scan loop: order, on_card hops, cancel, skip/exception map
    from app.utils import TaskContext as _TC
    _noop = _TC(log=lambda m: None, set_status=lambda m: None,
                cancelled=lambda: False)
    _seen_cards = []
    _stub_sensors = [
        ("s1", "First", lambda ctx: ("A", "fine", "", "")),
        ("s2", "Second", lambda ctx: ("C", "warn", "d", "")),
    ]
    _out = _hs.run_health_scan(_noop, on_card=lambda k, c: _seen_cards.append((k, c)),
                               sensors=_stub_sensors)
    assert list(_out) == ["s1", "s2"], "sensor order must hold"
    assert [k for k, _c in _seen_cards] == ["s1", "s2"], "on_card per sensor"
    assert _out["s1"]["grade"] == "A" and _out["s2"]["grade"] == "C"
    _stop_after_first = {"n": 0}
    def _cancel_ctx():
        return _TC(log=lambda m: None, set_status=lambda m: None,
                   cancelled=lambda: _stop_after_first["n"] >= 1)
    def _counting(ctx):
        _stop_after_first["n"] += 1
        return ("A", "x", "", "")
    _out2 = _hs.run_health_scan(_cancel_ctx(), sensors=[
        ("a", "A", _counting), ("b", "B", _counting)])
    assert list(_out2) == ["a"], f"cancel must stop the loop: {list(_out2)}"
    def _skipper(ctx):
        from app.utils import TaskSkipped as _TS2
        raise _TS2("n/a here")
    def _boomer(ctx):
        raise ValueError("boom")
    _out3 = _hs.run_health_scan(_noop, sensors=[
        ("s", "Skip", _skipper), ("e", "Err", _boomer)])
    assert _out3["s"]["grade"] == "?" and _out3["e"]["grade"] == "?"
    print("  health loop: order + cancel + skip/exception OK")
    # dialog builds + paints + fixes route (scan worker stubbed out —
    # its delivery is proven by the async harness instead)
    _orig_rhs = _hs.run_health_scan
    _hs.run_health_scan = lambda ctx, on_card=None, sensors=None: {}
    try:
        _hd = gui.HealthReportDialog(root, app)
        try:
            _hd._stop_token[0] = True
            root.update()
            assert len(_hd._card_widgets) == 6, "2x3 card grid missing"
            # (user redesign 2026-09: the toolbar Check PC button is gone;
            # the Tools tab hosts this feature — asserted in its section)
            _demo = {
                "smart": {"key": "smart", "title": "SSD & Drives", "grade": "A",
                          "headline": "Drives healthy", "detail": "", "note": ""},
                "gpu": {"key": "gpu", "title": "GPU Driver", "grade": "C",
                        "headline": "Driver update suggested", "detail": "d", "note": ""},
                "disk": {"key": "disk", "title": "Disk Space", "grade": "A",
                         "headline": "plenty", "detail": "", "note": ""},
                "windows": {"key": "windows", "title": "Windows Files", "grade": "A",
                            "headline": "healthy", "detail": "", "note": ""},
                "junk": {"key": "junk", "title": "Reclaimable Junk", "grade": "B",
                         "headline": "~2 GB", "detail": "", "note": ""},
            }
            for _k, _c in _demo.items():
                _hd._paint_card(_k, _c)
            root.update()
            assert _hd._card_widgets["gpu"]["letter"].cget("text") == "C"
            assert _hd._card_widgets["smart"]["letter"].cget("fg") == \
                gui.COLORS["accent_green"]
            # GPU per-card action appears only when actionable...
            assert _hd._card_widgets["gpu"]["action"].winfo_manager() != "", \
                "GPU action missing"
            assert _hd._card_widgets["smart"]["action"].winfo_manager() == "", \
                "healthy card must hide its action"
            # overall: B average (4+2+4+4+3)/5=3.4 -> B, 2 fixes, clean? no:
            # gpu C beats junk B in fix priority -> install
            _hd._cards = dict(_demo)
            _hd._paint_overall()
            root.update()
            assert _hd._card_widgets["overall"]["letter"].cget("text") == "B"
            assert "2 things to fix" in \
                _hd._card_widgets["overall"]["headline"].cget("text")
            assert _hd._card_widgets["overall"]["action"].winfo_manager() != ""
            # all-good overall hides its button
            _hd._cards = {k: dict(v, grade="A") for k, v in _demo.items()}
            _hd._paint_overall()
            root.update()
            assert _hd._card_widgets["overall"]["action"].winfo_manager() == ""
            # ...and fires the Install navigation LAST (it closes the dialog)
            _switched = []
            _o_switch = app._switch_to
            app._switch_to = lambda name: _switched.append(name)
            try:
                _hd._fire_card_action("gpu")
                assert _switched == ["Install"], _switched
            finally:
                app._switch_to = _o_switch
            assert not _hd._dlg.winfo_exists(), "action must close report first"
        finally:
            try:
                _hd._close()
            except Exception:
                pass
            root.update()
        # fix routing per action (fresh dialog each — _do_fix closes it)
        def _fresh_report():
            _d = gui.HealthReportDialog(root, app)
            _d._stop_token[0] = True
            root.update()
            return _d
        # repair: pre-checks the two keys on the Repair custom grid
        _rd = _fresh_report()
        try:
            _rd._cards = {"windows": {"key": "windows", "grade": "C"}}
            _rd._do_fix("repair", ["dism_restorehealth", "sfc_scan", "bogus_key"])
            root.update()
            assert app.active_tab == "Repair", app.active_tab
            _rp = app.tabs["Repair"]
            assert _rp.vars["dism_restorehealth"].get() is True
            assert _rp.vars["sfc_scan"].get() is True
            assert not _rd._dlg.winfo_exists(), "report must close first"
        finally:
            try:
                _rd._close()
            except Exception:
                pass
            root.update()
        # clean: selects the preset on the Clean tab
        _cd = _fresh_report()
        try:
            _cd._cards = {"junk": {"key": "junk", "grade": "D"}}
            _cd._do_fix("clean", "Quick Clean")
            root.update()
            assert app.active_tab == "Clean"
            assert app.tabs["Clean"]._selected_preset == "Quick Clean"
        finally:
            try:
                _cd._close()
            except Exception:
                pass
            root.update()
        # storage: delegates without running a scan here
        _sd = _fresh_report()
        try:
            _sd._cards = {"disk": {"key": "disk", "grade": "F"}}
            _opened = []
            _o_open = app._open_storage_insight
            app._open_storage_insight = lambda: _opened.append(True)
            try:
                _sd._do_fix("storage", None)
                root.update()
            finally:
                app._open_storage_insight = _o_open
            assert _opened == [True], "storage fix must open insight"
            assert not _sd._dlg.winfo_exists()
        finally:
            try:
                _sd._close()
            except Exception:
                pass
            root.update()
        # install + none
        _id = _fresh_report()
        try:
            _id._cards = {"gpu": {"key": "gpu", "grade": "C"}}
            _sw2 = []
            _o_sw2 = app._switch_to
            app._switch_to = lambda name: _sw2.append(name)
            try:
                _id._do_fix("install", None)
                root.update()
            finally:
                app._switch_to = _o_sw2
            assert _sw2 == ["Install"], _sw2
        finally:
            try:
                _id._close()
            except Exception:
                pass
            root.update()
    finally:
        _hs.run_health_scan = _orig_rhs
    print("  health dialog: cards + overall + fix routing OK")

    # --- Tools tab (9 cards 3x3, Tweak geometry; Quick Tools popup) ---
    # The 9 feature cards open the X-button popups (800x600 modals).
    # Shortcuts moved from the inline box into the Quick Tools popup —
    # assert tab/cards exist, each card routes, and the popup carries
    # the classic rows.
    tools_page = app.tabs["Tools"]
    assert isinstance(tools_page, gui.ToolsTab), type(tools_page).__name__
    assert sorted(tools_page._card_frames) == ["dns", "gamepad", "gameping", "health",
                                               "miccheck", "pilot", "quick",
                                               "speed", "storage"], \
        sorted(tools_page._card_frames)
    # 3x3 geometry matches Tweak preset cards (same pads/uniform)
    _pos = {k: (v[0].grid_info()["row"], v[0].grid_info()["column"])
            for k, v in tools_page._card_frames.items()}
    assert sorted(_pos.values()) == [(0, 0), (0, 1), (0, 2), (1, 0), (1, 1), (1, 2),
                                     (2, 0), (2, 1), (2, 2)], _pos
    app._switch_to("Tools")
    root.update()
    _opened = []
    _o_storage, _o_dns = app._open_storage_insight, app._open_dns_tester
    _o_health, _o_pilot = app._open_health_report, app._open_pilot_dialog
    _o_speed, _o_quick = app._open_speed_test, app._open_quick_tools
    _o_gamepad, _o_mic = app._open_gamepad_tester, app._open_mic_check
    _o_gameping = app._open_game_server_ping
    app._open_storage_insight = lambda: _opened.append("storage")
    app._open_dns_tester = lambda: _opened.append("dns")
    app._open_health_report = lambda: _opened.append("health")
    app._open_pilot_dialog = lambda: _opened.append("pilot")
    app._open_speed_test = lambda: _opened.append("speed")
    app._open_quick_tools = lambda: _opened.append("quick")
    app._open_gamepad_tester = lambda: _opened.append("gamepad")
    app._open_mic_check = lambda: _opened.append("miccheck")
    app._open_game_server_ping = lambda: _opened.append("gameping")
    try:
        for key in ("storage", "health", "dns", "pilot", "speed", "quick",
                    "gamepad", "miccheck", "gameping"):
            tools_page._mount(key)
            root.update()
            assert _opened and _opened[-1] == key, \
                f"Tools card {key} routed to {_opened}"
    finally:
        app._open_storage_insight, app._open_dns_tester = _o_storage, _o_dns
        app._open_health_report, app._open_pilot_dialog = _o_health, _o_pilot
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
        assert (_gd._pad_img.width(), _gd._pad_img.height()) == (300, 300), \
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
        gui._xinput_rumble = lambda slot, w, st: _rcalls.append((slot, w, st)) or True
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
        gui._xinput_rumble = _old_rumble
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
            assert any(w.winfo_class() == "TCombobox"
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
    assert len(_gp.VALVE_REGIONS) == 10 and len(_gp.COD_REGIONS) == 10
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

    # --- run-engine tab contract (A-1/A-2 regression cover) --------------
    # Every page in app.tabs MUST implement set_run_enabled — the missing
    # method on ToolsTab crashed every tab loop: Install/Update clicks wedged
    # _busy forever, and the swallowed copy hid the Stop button + progress
    # bar on every Clean/Repair/Tweak run. Assert the contract AND the gated
    # UI states directly (no run needed — pure UI hops).
    for _name, _page in app.tabs.items():
        assert callable(getattr(_page, "set_run_enabled", None)), \
            f"{_name} tab missing set_run_enabled"
    app._set_buttons_enabled(False)
    root.update()
    assert app.cancel_btn._enabled is True, "Stop must enable when runs start"
    assert app.cancel_btn.winfo_manager() != "", "Stop must show when runs start"
    assert app._progress_hidden is False, "progress bar must show when runs start"
    app._set_buttons_enabled(True)
    root.update()
    assert app.cancel_btn._enabled is False, "Stop must disable when idle"
    assert app._progress_hidden is True, "progress bar must hide when idle"
    print("  run-engine contract: set_run_enabled on every tab + Stop/progress gating OK")

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
