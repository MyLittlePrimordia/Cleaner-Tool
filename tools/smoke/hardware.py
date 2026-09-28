"""Hardware Monitor

H16: extracted verbatim from tools/smoke_gui.py main(), L2314-2420.

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

    # shared with the DNS area (it was a local there in the monolith, and
    # this section reached across for it)
    from tools.smoke import settle as _settle_impl
    def _settle(dlg):
        _settle_impl(ctx, dlg)
    # --- Hardware Monitor (3x2 Task-Manager grid, user redesign) ----
    import app.hw_monitor as _hw
    # pure math + offline-safe constructors (never raise, never fake)
    assert _hw.cpu_percent_from_deltas(100, 1000, 1000) == 95.0
    assert _hw.cpu_percent_from_deltas(0, 0, 0) == 0.0
    assert _hw._phys_index_from_path(
        r"\GPU Engine(pid_1_luid_0x1_phys_2_eng_0_engtype_3D)") == 2
    assert _hw._phys_index_from_path(r"\GPU Engine(pid_1_engtype_3D)") is None
    assert _hw._is_loopback_iface("Loopback Pseudo-Interface") is True
    assert _hw._is_loopback_iface("Intel Wi-Fi 6E") is False
    _gpus = _hw.gpu_list()
    assert isinstance(_gpus, list) and _gpus, "gpu_list must enumerate adapters"
    _gs0 = _hw.gpu_static_info()
    assert _gs0["name"] == _gpus[0]["name"], "static = largest-VRAM entry"
    _nv = _hw.NvmlTemp()
    try:
        assert isinstance(_nv.available, bool) and isinstance(_nv.device_count, int)
        if not _nv.available:
            assert _nv.sample() is None and _nv.sample_at(0) is None
    finally:
        try:
            _nv.close()
        except Exception:
            pass
    _hd = gui.HardwareMonitorDialog(root, app)
    try:
        _settle(_hd)
        assert (_hd._modal_w, _hd._modal_h) == (800, 600)
        # six cards, Health-chrome (hairline outer + bg_alt inner),
        # 2 columns x 3 rows (GPU above VRAM), everything centered
        assert _hd._grid.grid_size() == (2, 3), _hd._grid.grid_size()
        assert _hd._grid.grid_slaves(row=2, column=0), "disk row present"
        assert _hd._grid.grid_slaves(row=2, column=1), "network row present"
        for _key in ("_cpu", "_ram", "_gpu", "_vram", "_disk", "_net"):
            _card = getattr(_hd, _key)
            assert set(_card) >= {"outer", "inner", "title", "val", "bar",
                                  "name", "sub", "pick"}, _key
            assert _card["outer"].cget("bg").lower() == "#232b38", _key
            assert _card["inner"].cget("bg").lower() == "#151b24", _key
            for _lbl in ("title", "val", "name", "sub"):
                assert _card[_lbl].cget("anchor") == "center", (_key, _lbl)
        # no Close button: the popup X already closes it
        _btn_texts = [getattr(_w, "_text", "")
                      for _w in _hd.body.winfo_children()
                      for _w in (_w,) + tuple(_w.winfo_children())]
        assert "Close" not in _btn_texts, _btn_texts
        # names live ON their cards (no separate spec row anywhere)
        assert _hd._cpu["name"].cget("text"), "cpu name on cpu card"
        _cpu_sub = _hd._cpu["sub"].cget("text")
        assert "threads" in _cpu_sub or _cpu_sub in ("", "warming up"), \
            "threads/warmup on cpu card: %r" % _cpu_sub
        # no Nvidia-only placeholder text on non-Nvidia rigs: temp is
        # inline-or-absent, never an explanatory line
        _gpu_sub = _hd._gpu["sub"].cget("text")
        assert "Nvidia" not in _gpu_sub and "not available" not in _gpu_sub, \
            _gpu_sub.encode("ascii", "replace")
        assert not hasattr(_hd, "_temp_lbl"), "separate temp row removed"
        # GPU ▾ arrow menu iff more than one adapter (no combobox chrome)
        assert not hasattr(_hd, "_gpu_combo"), "combobox replaced by arrow menu"
        if len(_hd._gpu_list) > 1:
            assert _hd._gpu["pick"] is not None
            assert _hd._gpu["pick"].winfo_exists()
            assert len(_hd._gpu_menu_labels()) == len(_hd._gpu_list)
            assert _hd._gpu["pick"].cget("text") == "▾"
        else:
            assert _hd._gpu["pick"] is None
        # network ▾ arrow always: Auto + every NIC (Ethernet and WiFi)
        assert _hd._net["pick"] is not None and _hd._net["pick"].winfo_exists()
        _net_labels = _hd._net_menu_labels()
        assert _net_labels and _net_labels[0] == "Auto (busiest)", _net_labels
        # six cards fit the fixed body with headroom (no clipping)
        _hd.body.update_idletasks()
        assert _hd.body.winfo_reqheight() <= _hd.body.winfo_height() - 20, \
            (_hd.body.winfo_reqheight(), _hd.body.winfo_height())
        # GPU arrow follows selection (static lines repaint, live follow)
        if _hd._gpu["pick"] is not None:
            _hd._set_gpu(len(_hd._gpu_list) - 1)
            assert _hd._gpu_sel == len(_hd._gpu_list) - 1
            _hd._set_gpu(0)
            assert _hd._gpu_sel == 0
            _hd._set_gpu(9999)
            assert _hd._gpu_sel == len(_hd._gpu_list) - 1, "clamped"
            _hd._set_gpu(0)
        # network arrow pins / releases (sampler honors the pin)
        _hd._set_net(None)
        assert _hd._net_sel is None
        _names = _hd._net_iface_names()
        assert isinstance(_names, list)
        if _names:
            _hd._set_net(_names[0])
            assert _hd._net_sel == _names[0]
            assert _hd._net_sampler._pinned == _names[0]
            _hd._set_net("no-such-nic")
            assert _hd._net_sel is None, "unknown falls back to auto"
        try:
            _hd._tick()
            root.update()
        except Exception as _e:
            raise AssertionError(f"hw tick raised: {_e}")
    finally:
        try:
            _hd._close()
        except Exception:
            pass
        root.update()
    print("  hardware monitor: 2x3 centered cards + arrow pickers + honesty OK")

