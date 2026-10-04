"""Find My Fastest DNS

H16: extracted verbatim from tools/smoke_gui.py main(), L1760-2313.

Nothing here was reformatted or reordered. The section banners are
kept so the original structure stays greppable, and ORDER MATTERS —
sections share live Application state, so run_all() calls these in
sequence.
"""

from tools.smoke import assert_dialog_proportional


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
    import time as _tq2          # also used by the two inline settle loops
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
    from tools.smoke import settle as _settle_impl
    def _settle(dlg):
        _settle_impl(ctx, dlg)
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
    _R = min(340 / 2.0 - 34.0, 200 - 58.0)
    assert _R >= 130, _R
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
        assert_dialog_proportional(_sd, root, "speed test")
        assert set(_sd._row_values) == {"ping", "download", "upload", "stability"}
        # Ookla-style idle stage (user call): NO auto-start — the popup
        # waits on GO. Dial hidden, GO parked where the gauge will be,
        # headline dashed, status hint centered.
        assert _sd._stage_mode == "go", _sd._stage_mode
        assert _sd._gauge.winfo_manager() == "", "dial hidden while idle"
        assert _sd._go_btn.winfo_manager() == "pack", "GO on stage while idle"
        assert _sd._phase_bar.winfo_manager() == "", "phase bar hidden while idle"
        assert _sd._big_lbl.cget("text") == "—"
        assert "GO" in _sd._status_lbl.cget("text"), _sd._status_lbl.cget("text")
        assert _sd._status_lbl.cget("anchor") == "center"
        # no description label anywhere on the popup (user call)
        for _w in _sd.body.winfo_children():
            try:
                _t = _w.cget("text")
            except Exception:
                continue
            assert "uses up to" not in _t and "Tests your connection" not in _t, _t
        # no-clipping guard (user bug: Upload row cut, Re-test unreachable):
        # content must leave 40px+ headroom in the fixed 538px body so
        # larger system fonts still fit without scrolling.
        _sd.body.update_idletasks()
        assert _sd.body.winfo_reqheight() <= _sd.body.winfo_height() - 40, \
            (_sd.body.winfo_reqheight(), _sd.body.winfo_height())
        assert hasattr(_sd, "_retest_btn") and _sd._retest_btn.winfo_exists()
        assert _sd._retest_btn is _sd._go_btn, "harness alias tracks the GO button"
        # GO starts the run: dial + phase bar take the stage (the worker
        # is orphaned instantly — real probe threads belong to manual).
        _sd._start_test()
        assert _sd._stage_mode == "gauge", _sd._stage_mode
        assert _sd._gauge.winfo_manager() == "pack", "dial on stage while running"
        assert _sd._go_btn.winfo_manager() == "", "GO hidden while running"
        assert _sd._phase_bar.winfo_manager() == "pack"
        assert "server" in _sd._status_lbl.cget("text").lower()
        _sd._stop_token[0] = True
        _sd._scan_gen += 1
        try:
            root.after_cancel(_sd._watchdog_after)
        except Exception:
            pass
        _sd._watchdog_after = None
        # phase switches: download parks needle + opens its span, upload
        # RESETS the needle with a fresh headline (two-climb behavior).
        _sd._set_phase("ping", _sd._scan_gen)
        assert _sd._phase_bar._indeterminate is True, "early legs shimmer"
        _sd._set_phase("download", _sd._scan_gen)
        assert _sd._active_leg == "download"
        assert _sd._phase_span == tuple(gui.SpeedTestDialog._SPAN_DOWN)
        assert _sd._phase_bar._indeterminate is False
        _sd._paint_live("download", 42.5, 0.5, _sd._scan_gen)
        root.update()
        assert "42" in _sd._big_lbl.cget("text"), _sd._big_lbl.cget("text")
        assert abs(_sd._phase_bar._fraction -
                   (0.12 + 0.5 * (0.60 - 0.12))) < 1e-6, _sd._phase_bar._fraction
        _sd._set_phase("upload", _sd._scan_gen)
        assert _sd._active_leg == "upload"
        assert _sd._big_lbl.cget("text") == "—", "fresh climb for upload"
        _sd._paint_live("upload", 11.9, 0.5, _sd._scan_gen)
        root.update()
        assert "11.9" in _sd._big_lbl.cget("text"), _sd._big_lbl.cget("text")
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
        # single stats row: 4 even cells, leg -> value contract intact
        assert set(_sd._row_values) == {"ping", "download", "upload", "stability"}
        _cells = _sd._rows_body.grid_slaves(row=0)
        assert len(_cells) == 4, len(_cells)
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
        # run over: dial hides, GO returns to the stage (next click
        # replays the animation); verdict stays on the headline.
        assert _sd._stage_mode == "go", _sd._stage_mode
        assert _sd._gauge.winfo_manager() == "", "dial hidden when done"
        assert _sd._go_btn.winfo_manager() == "pack", "GO back when done"
        assert _sd._phase_bar._indeterminate is False, "shimmer parked"
        _st = _sd._status_lbl.cget("text")
        # server line is centered and names the server ONLY (user call):
        # the stat cells carry the numbers, so no metrics repeat here.
        assert "Server:" in _st and "complete" in _st, _st.encode("ascii", "replace")
        assert "94" not in _st and "12.3" not in _st, _st.encode("ascii", "replace")
        assert _sd._status_lbl.cget("anchor") == "center", "server line centered"
        # numbers still land on the cards + headline, not the server line
        assert "Mbps" in _sd._row_values["download"].cget("text")
        assert "94" in _sd._big_lbl.cget("text")
        # partial honesty: one missing leg names itself, still no numbers
        _sd._finished = False
        _sd._results = {"ping": 18.0, "download": None, "upload": 12.3}
        _sd._finish(False, _sd._scan_gen)
        root.update()
        _stp = _sd._status_lbl.cget("text")
        assert "Server:" in _stp and "download unavailable" in _stp, \
            _stp.encode("ascii", "replace")
        _sd._results = {"ping": 18.0, "download": 94.5, "upload": 12.3}
        _sd._paint_result("download", 1.0, _sd._scan_gen - 99)  # stale hop ignored
        assert _sd._results["download"] == 94.5
        # all-None honesty: no fake zeros, headline back to dash
        _sd2 = gui.SpeedTestDialog(root, app)
        try:
            _sd2._stop_token[0] = True
            _settle(_sd2)
            assert _sd2._stage_mode == "go", "fresh popup waits on GO"
            assert "GO" in _sd2._status_lbl.cget("text")
            _sd2._paint_result("ping", None, _sd2._scan_gen)
            _sd2._paint_result("download", None, _sd2._scan_gen)
            _sd2._paint_result("upload", None, _sd2._scan_gen)
            _sd2._finish(False, _sd2._scan_gen)
            root.update()
            assert "Couldn't reach" in _sd2._status_lbl.cget("text")
            assert _sd2._big_lbl.cget("text") == "\u2014"
            assert _sd2._stage_mode == "go", "failed run also returns GO"
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
        assert _stm.warmup(url="http://127.0.0.1:9/nope", num_bytes=100,
                           timeout=0.2) is None
        # Ookla-style sustained rounds: 3 upload sizes with scaled gates,
        # chunk-reported upload progress, ?bytes detection by host
        assert len(_stm.UPLOAD_ROUNDS) == 3 and _stm.UPLOAD_ROUNDS[0] >= 1_000_000
        assert _stm.UP_MIN_MBPS < _stm.ROUND_MIN_MBPS
        assert _stm.UP_FAST_MBPS < _stm.ROUND_FAST_MBPS
        assert _stm._is_cloudflare_url(_stm.DOWNLOAD_URL)
        assert not _stm._is_cloudflare_url(_stm.FALLBACK_DOWNLOAD_URL)
        assert round(_stm.ESTIMATED_MAX_MB) >= 200
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

