"""Run-engine tab contract and the legacy config migration

H16: extracted verbatim from tools/smoke_gui.py main(), L3347-3391.

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

    # H16: the root teardown and the pass/fail report used to live at the end
    # of main() and were swept into this module with the last section. They
    # belong to the driver, which owns the Tk root, and they live there now.
