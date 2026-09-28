"""Modal registry vs Combobox popdown, PC Health Report Card

H16: extracted verbatim from tools/smoke_gui.py main(), L2856-3042.

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
    # Read the source of the class that actually holds the code, not the text
    # of app/gui.py: Application moved to app/ui/app.py (H14), and this check
    # is about what _premap_install's body does, not where the file lives.
    import inspect
    _src = inspect.getsource(gui.Application)
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
    # HealthReportDialog removed — Process Manager replaced PC Health.
    # Pure health_scan grading unit tests above are kept (module still ships).
    print("  health dialog: skipped (feature removed, Process Manager replaces it)")

