"""Storage Insight dry-run guard and the Low-Space Nudge

H16: extracted verbatim from tools/smoke_gui.py main(), L1515-1759.

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
    # H14: Application moved to app/ui/app.py and bound notify_low_space at
    # import time, so the app.gui alias is no longer the lookup path.
    from app.ui import app as _appmod
    _orig_nudge_toast = _appmod.notify_low_space
    _appmod.notify_low_space = lambda *a: _toasts.append(a)
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
        _appmod.notify_low_space = _orig_nudge_toast
    print("  low-space nudge: fire/hysteresis/recovery/dismiss/estimate OK")

