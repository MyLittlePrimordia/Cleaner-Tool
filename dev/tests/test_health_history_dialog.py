"""N-001: the dated checkup record behind the Health trend.

The ledger was already written on every checkup and read by nothing. These
tests pin the window that finally reads it: one row per checkup, newest first,
each naming its own change, and a Clear action that empties ONLY the record.
"""
from __future__ import annotations

import time

import pytest

tk = pytest.importorskip("tkinter")

from _tkhelpers import make_root  # noqa: E402


def _no_wait(self):
    return None


def _trend():
    now = time.time()
    return {
        "count": 3,
        "scores": [88, 84, 71],
        "first": 88, "last": 71, "best": 88, "worst": 71,
        "delta": -13, "overall": -17, "decline": None,
        "span_days": 4.0,
        "first_ts": now - 3 * 86400,
        "last_ts": now,
        "points": [
            {"t": now - 3 * 86400, "s": 88, "g": "B",
             "f": {"disk": "B", "smart": "A"}},
            {"t": now - 2 * 86400, "s": 84, "g": "B",
             "f": {"disk": "B", "smart": "A"}},
            {"t": now, "s": 71, "g": "C",
             "f": {"disk": "D", "smart": "A"}},
        ],
    }


@pytest.fixture
def window(monkeypatch):
    from app.ui.dialogs import health_history_dialog as mod

    monkeypatch.setattr(mod.HealthHistoryDialog, "wait", _no_wait)
    monkeypatch.setattr("app.health_history.trend", _trend)
    root = make_root()
    root.geometry("520x240+3000+3000")
    root.update()
    app = _StubApp(root)
    dialog = mod.HealthHistoryDialog(root, app)
    _settle(root, app, dialog)
    try:
        yield dialog
    finally:
        try:
            dialog.close()
        except Exception:
            pass
        root.destroy()


class _Dispatch:
    """Queue worker callbacks, drained on the Tk thread by `_settle`.

    Calling them inline from the worker would mutate Tk from a non-main
    thread, which the real TkDispatcher exists to avoid.
    """

    def __init__(self):
        self.queue = []

    def post(self, fn, *args):
        self.queue.append(lambda: fn(*args))

    def drain(self):
        while self.queue:
            self.queue.pop(0)()


class _StubApp:
    def __init__(self, root):
        self.root = root
        self._dispatch = _Dispatch()
        self.notes = []

    def log(self, message):
        self.notes.append(str(message))

    def set_status(self, message):
        self.notes.append(str(message))


def _settle(root, app, dialog, expect=3, timeout=3.0):
    """Pump the queue (and Tk) until the worker's result has landed."""
    deadline = time.time() + timeout
    while len(dialog._points) < expect and time.time() < deadline:
        app._dispatch.drain()
        root.update()
        time.sleep(0.01)
    app._dispatch.drain()
    root.update()
    assert len(dialog._points) == expect, "the worker result never landed"


def _text(widget):
    out = []
    stack = [widget]
    while stack:
        cur = stack.pop()
        try:
            if cur.winfo_class() == "Label":
                out.append(cur.cget("text"))
            stack.extend(cur.winfo_children())
        except Exception:
            pass
    return "\n".join(out)


def test_every_checkup_gets_a_row_newest_first(window):
    rows = [w for w in window._list.winfo_children()
            if hasattr(w, "title_lbl")]
    titles = [r.title_lbl.cget("text") for r in rows]
    assert len(titles) == 3
    assert "71 (C)" in titles[0]
    assert "84 (B)" in titles[1]
    assert "88 (B)" in titles[2]


def test_a_row_names_the_change_and_its_cause(window):
    text = _text(window._list)
    assert "Down 13 from 84 (71)." in text
    assert "Disk" in text and "B \u2192 D" in text
    assert "First recorded checkup" in text


def test_the_banner_counts_the_record(window):
    assert "3 checkups" in window.banner._title_full
    assert "71 to 88" in window.banner._title_full


def test_clear_empties_only_the_record(monkeypatch, window):
    from app import health_history
    from app.ui.dialogs import health_history_dialog as mod

    cleared = []
    monkeypatch.setattr(health_history, "clear", lambda: cleared.append(True))
    monkeypatch.setattr(mod, "_themed_askyesno", lambda *a, **k: True)
    window._clear()
    assert cleared == [True]
    assert window._points == []
    assert "Nothing recorded yet" in _text(window._list)
    assert "Nothing on your PC" in window.status._raw or \
        "touched" in window.status._raw


def test_a_refused_confirmation_clears_nothing(monkeypatch, window):
    from app import health_history
    from app.ui.dialogs import health_history_dialog as mod

    cleared = []
    monkeypatch.setattr(health_history, "clear", lambda: cleared.append(True))
    monkeypatch.setattr(mod, "_themed_askyesno", lambda *a, **k: False)
    window._clear()
    assert cleared == []
    assert len(window._points) == 3
