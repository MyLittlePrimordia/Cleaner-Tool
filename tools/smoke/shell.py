"""Window shell: init flow, admin gate, branding, tab icons

H16: extracted verbatim from tools/smoke_gui.py main(), L526-585.

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
    # --- real init flow: pass the admin gate if it's showing ---
    if not is_admin():
        gate = None
        for w in root.winfo_children():
            if isinstance(w, gui.AdminGateFrame):
                gate = w
                break
        assert gate is not None, "Non-admin but AdminGateFrame not shown"
        gate._continue_limited()

    # hand the user's own selection back now that the Gate is behind us
    # the driver's gate precondition, consumed here (see Ctx.gate_sel_saved)
    _gate_sel_saved = ctx.gate_sel_saved
    ctx.gate_sel_saved = None
    if _gate_sel_saved is not None:
        try:
            from app import config_persist as _cp0
            _cp0.update_config(
                lambda c: c.__setitem__("selected_tasks", _gate_sel_saved))
            _cp0._config_cache = None
        except Exception:
            pass

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

