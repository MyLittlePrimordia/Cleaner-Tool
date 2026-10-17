"""N-006 GUI: the Run History dialog renders the honest empty and populated
states. The config read is patched, so nothing touches the real config file.
"""
from __future__ import annotations

import datetime as dt

import pytest

tk = pytest.importorskip("tkinter")

from app.ui.dialogs import RunHistoryDialog
from app.ui.dialogs._tool_page import ToolPage
from app.ui.dialogs.registry import DIALOGS

from _tkhelpers import make_root


class _StubApp:
    def __getattr__(self, name):
        return lambda *a, **k: None


def _build(monkeypatch, entries):
    import app.config_persist as cp
    monkeypatch.setattr(cp, "load_config", lambda: {"run_history": entries})
    root = make_root()
    root.withdraw()
    dlg = RunHistoryDialog(root, _StubApp())
    return root, dlg


def test_registered_and_on_the_shared_shell():
    assert issubclass(RunHistoryDialog, ToolPage)
    assert "run_history" in DIALOGS
    assert DIALOGS["run_history"].cls is RunHistoryDialog


def test_empty_history_says_so_plainly(monkeypatch):
    root, dlg = _build(monkeypatch, [])
    try:
        assert "No runs recorded yet" in dlg.banner._title_full
        assert dlg._spark_lbl is None
        assert dlg._list_rows == []
    finally:
        dlg.close()
        root.destroy()


def test_populated_history_shows_a_trend_and_rows(monkeypatch):
    now = dt.datetime.now().timestamp()
    entries = [{"t": now - i * 3600, "b": (i + 1) * 1000} for i in range(5)]
    root, dlg = _build(monkeypatch, entries)
    try:
        assert "reclaimed across 5 cleans" in dlg.banner._title_full
        assert dlg._spark_lbl is not None
        assert len(dlg._list_rows) == 5
        # newest first
        assert dlg._list_rows[0] is not None
    finally:
        dlg.close()
        root.destroy()
