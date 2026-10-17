"""N-004 GUI smoke: the risk / build badges really render on a TaskTab.

The Tweak tab is used because it genuinely carries both ``ADVANCED`` and
``REBOOT REQUIRED`` tasks, so the badge path is exercised on real data, not
a fixture. Skipped gracefully when there is no display.
"""
from __future__ import annotations

import pytest

tk = pytest.importorskip("tkinter")

from app.ui.tabs.tasktab import TaskTab


class _StubApp:
    """Any attribute the tab reaches for becomes a harmless no-op callable."""

    def __getattr__(self, name):
        return lambda *a, **k: None


def _build(tab_name="Tweak"):
    try:
        root = tk.Tk()
    except tk.TclError as exc:              # pragma: no cover - no display
        pytest.skip("no display for Tk: %s" % exc)
    root.withdraw()
    page = tk.Frame(root, bg="#0d0d0d")
    page.pack(fill="both", expand=True)
    tab = TaskTab(page, _StubApp(), tab_name)
    tab.pack(fill="both", expand=True)
    _settle(root)
    return root, tab


def _settle(root):
    for _ in range(4):
        root.update_idletasks()
        root.update()


def _texts(widget):
    out = []
    stack = [widget]
    while stack:
        w = stack.pop()
        try:
            text = w.cget("text")
        except Exception:
            text = ""
        if text:
            out.append(str(text))
        try:
            stack.extend(w.winfo_children())
        except Exception:
            pass
    return out


def test_advanced_and_reboot_badges_render_in_custom_grid():
    root, tab = _build("Tweak")
    try:
        tab._enter_custom()
        _settle(root)
        texts = _texts(tab)
        assert "Advanced" in texts, "ADVANCED tasks did not get a badge"
        assert "Reboot" in texts, "REBOOT REQUIRED tasks did not get a badge"
    finally:
        root.destroy()


def test_unsupported_task_is_disabled_and_filtered_out():
    from app.tab_presets import TABS
    victim = next(t for t in TABS["Tweak"] if getattr(t, "default", False))
    victim.min_build = 999_999_999          # newer than any real Windows
    try:
        root, tab = _build("Tweak")
        try:
            tab._enter_custom()
            _settle(root)
            assert "Not for this build" in _texts(tab)
            assert tab.vars[victim.key].get() is False
            assert victim not in tab.selected_tasks()
        finally:
            root.destroy()
    finally:
        victim.min_build = 0
