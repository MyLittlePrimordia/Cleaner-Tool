"""ScorecardDialog, drive awareness, run history

H16: extracted verbatim from tools/smoke_gui.py main(), L925-1089.

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

    # --- Feature 1/4: system-drive awareness + multi-drive scorecard ----
    # Pure helpers, so these run identically on any machine.
    import os as _os
    _saved_sysdrive = _os.environ.get("SystemDrive")
    try:
        _os.environ["SystemDrive"] = "D:"
        assert gui._system_drive_root() == "D:\\", gui._system_drive_root()
        _os.environ["SystemDrive"] = "E:\\"
        assert gui._system_drive_root() == "E:\\", gui._system_drive_root()
        _os.environ.pop("SystemDrive", None)
        assert gui._system_drive_root() == "C:\\", "missing SystemDrive must fall back to C:"
    finally:
        if _saved_sysdrive is None:
            _os.environ.pop("SystemDrive", None)
        else:
            _os.environ["SystemDrive"] = _saved_sysdrive
    # real machine still answers
    assert gui._system_drive_root().endswith("\\")
    # gains: only drives that grew, biggest first, junk tolerated
    _before = {"C:\\": (100, 1000), "D:\\": (50, 500), "E:\\": (10, 100)}
    _after = {"C:\\": (150, 1000), "D:\\": (50, 500), "E:\\": (99, 100)}
    assert gui._drive_gains(_before, _after) == [("E:\\", 89), ("C:\\", 50)]
    assert gui._drive_gains(None, None) == []
    assert gui._drive_gains(_before, {}) == []
    # a live snapshot of this machine must at least see the system drive
    _snap = gui._snapshot_all_drives(gui.Application._query_drives,
                                     gui.Application._query_drive_free_total)
    assert isinstance(_snap, dict) and _snap, "no drives snapshotted"

    # scorecard renders the per-drive strip only when 2+ drives gained
    sc6 = gui.ScorecardDialog(
        root, tab_name="Clean", mode="run", cancelled=False,
        results=[("Shader caches", "ok", 139 * 1024**3)],
        total_bytes=139 * 1024**3, needs_reboot=False,
        before_drives=_before, after_drives=_after,
    )
    root.update()
    assert sc6._drive_rows is not None, "two gaining drives must show the strip"
    assert len(sc6._gains) == 2
    # Feature 3: the clipboard summary names the freed space and both drives
    _txt = sc6._summary_text()
    assert "Space freed" in _txt and "E:" in _txt and "D:" not in _txt, _txt
    assert "Shader caches" in _txt and "done" in _txt
    sc6._close()
    root.update()

    # one gaining drive = no strip (the hero number already said it)
    sc7 = gui.ScorecardDialog(
        root, tab_name="Clean", mode="run", cancelled=False,
        results=[("Temp files", "ok", 50)],
        total_bytes=50, needs_reboot=False,
        before_drives={"C:\\": (100, 1000)}, after_drives={"C:\\": (150, 1000)},
    )
    root.update()
    assert sc7._drive_rows is None, "a single drive must not get its own strip"
    sc7._close()
    root.update()

    # --- Feature 10: run history -> friendly 30-day sentence ------------
    _hist_before = config_persist.load_config().get("run_history", [])
    try:
        config_persist.record_run(0)          # nothing freed = not recorded
        _n0 = len(config_persist.load_config().get("run_history", []))
        assert _n0 == len(_hist_before), "zero-byte runs must not be recorded"
        config_persist.record_run(3 * 1024**3)
        config_persist.record_run(2 * 1024**3)
        _total, _runs = config_persist.freed_since(30)
        assert _runs >= 2 and _total >= 5 * 1024**3, (_total, _runs)
        _sc8 = gui.ScorecardDialog(
            root, tab_name="Clean", mode="run", cancelled=False,
            results=[("Temp files", "ok", 1024)], total_bytes=1024,
        )
        root.update()
        assert _sc8._month_lbl is not None, "30-day sentence missing after 2 runs"
        assert "30 days" in _sc8._month_lbl.cget("text")
        _sc8._close()
        root.update()
    finally:
        # leave the user's real history exactly as we found it
        config_persist.update_config(
            lambda cfg: cfg.__setitem__("run_history", list(_hist_before)))

