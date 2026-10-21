"""Regression guards for two 2026-10 fixes.

1. `run_tasks` must persist the user's *pre-filter* selection. A refactor
   moved the save block into `_start_claimed_run` but never passed
   `all_selected` through, so the block raised NameError on every manual run
   (swallowed by a broad except). Scheduled Auto Maintenance then had
   nothing to run on a fresh config.
2. Tab switches must not flush layout synchronously. Pages are stacked and
   switched with tkraise(); the old update_idletasks() calls cost 42-89 ms
   per click (96-100% of the switch).
"""
from __future__ import annotations

import threading

import pytest

from tests._tkhelpers import make_root

tk = pytest.importorskip("tkinter")


# --------------------------------------------------------------------------- #
# 1. selection persistence
# --------------------------------------------------------------------------- #

class _StopAfterSave(Exception):
    """Raised from the first post-save call so the test never starts a worker."""


def _bare_app(root):
    from app.ui.app import Application
    from app.runner import RunState

    app = object.__new__(Application)
    app.root = root
    app._busy = False
    app._busy_lock = threading.Lock()
    app._state = RunState.IDLE
    app._cancel_requested = False
    app._run_gen = 0
    app._current_gen = 0
    app.log = lambda _msg: None
    app._claim_run = lambda: 1
    app._abort_unstarted_run = lambda exc: None
    app._set_buttons_enabled = lambda _enabled: (_ for _ in ()).throw(
        _StopAfterSave())
    return app


def test_manual_run_persists_prefilter_selection(monkeypatch):
    """The saved list is the selection BEFORE limited-mode admin filtering."""
    import app.ui.app as uiapp
    from app.tasks import Task

    root = make_root()
    try:
        root.withdraw()
        app = _bare_app(root)

        admin_task = Task(key="admin_only", label="Admin", description="",
                          run=lambda ctx: 0, admin_required=True, default=False)
        user_task = Task(key="user_task", label="User", description="",
                         run=lambda ctx: 0, admin_required=False, default=False)

        # Limited mode: the messagebox is UI, not the thing under test.
        monkeypatch.setattr(uiapp, "is_admin", lambda: False)
        monkeypatch.setattr(uiapp, "_themed_showinfo",
                            lambda *a, **k: None)
        # No hazards / no preflight prompts in this path.
        app._preflight_check = lambda _tasks: []
        monkeypatch.setattr(uiapp, "check_info_notices", lambda _tasks: [])
        monkeypatch.setattr(uiapp, "check_dangerous_combos", lambda _tasks: [])

        saved = {}

        def fake_update_config(mutator):
            cfg = {}
            mutator(cfg)          # a NameError here is swallowed by run_tasks
            saved.update(cfg)

        import app.config_persist as cp
        monkeypatch.setattr(cp, "update_config", fake_update_config)

        app.run_tasks("Clean", [admin_task, user_task], mode="run")

        keys = saved.get("selected_tasks", {}).get("Clean")
        assert keys == ["admin_only", "user_task"], (
            "manual runs must save the full pre-filter selection; got %r" % (keys,)
        )
    finally:
        root.destroy()


# --------------------------------------------------------------------------- #
# 2. no synchronous layout flush on a tab switch
# --------------------------------------------------------------------------- #

class _Progress:
    _accent = None

    def _draw(self):
        pass


def _switch_app(root, page):
    from app.ui.app import Application
    from app.ui.theme import _TAB_STATUS

    app = object.__new__(Application)
    app.root = root
    app.active_tab = "Clean"
    app._busy = False
    app._rail_t = [0.0] * 7
    app._switch_anim = None
    app._switch_anim_after = None
    app._hover_seg = None
    app._hover_t = 0.0
    app._appbar_title = tk.Label(root)
    app._appbar_sub = tk.Label(root)
    app._wheel_bound = True
    app.progress_bar = _Progress()
    app.set_status = lambda _text: None
    app.log = lambda _msg: None
    app.tabs = {"Clean": page, "Repair": page}
    # Isolate the geometry question from the rail animation.
    app._paint_pills = lambda: None
    app._update_tab_icon = lambda: None
    app._style_switch_labels = lambda: None
    app._tick_switch = lambda: None
    return app


def test_tab_switch_does_not_flush_layout_synchronously():
    root = make_root()
    try:
        root.withdraw()
        page = tk.Frame(root)
        page.place(relx=0, rely=0, relwidth=1, relheight=1)
        app = _switch_app(root, page)

        calls = []
        real = root.update_idletasks

        def counted(*a, **k):
            calls.append(1)
            return real(*a, **k)

        root.update_idletasks = counted
        app._switch_to("Repair")
        assert calls == [], (
            "tab switching must not force a synchronous layout flush; "
            "it cost 42-89 ms per click"
        )
    finally:
        root.destroy()


def test_show_tab_does_not_flush_layout_synchronously():
    root = make_root()
    try:
        root.withdraw()
        page = tk.Frame(root)
        app = _switch_app(root, page)

        calls = []
        real = root.update_idletasks

        def counted(*a, **k):
            calls.append(1)
            return real(*a, **k)

        root.update_idletasks = counted
        app._show_tab("Clean")
        assert calls == [], "showing a page must not flush layout on the click path"
    finally:
        root.destroy()
