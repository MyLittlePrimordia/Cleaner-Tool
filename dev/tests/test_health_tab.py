"""The Health tab: one read-only check, one score, one short list of fixes.

These tests replace the old trend/decline/Smart Care suite. That suite was
testing a page three times this size; the product now deliberately keeps only
the checkup and the route to each fix, so the tests pin exactly that.
"""
from __future__ import annotations

import inspect
import time

import pytest

tk = pytest.importorskip("tkinter")
from _tkhelpers import make_root



class _Dispatch:
    """Synchronous stand-in for Application._dispatch in the tests."""

    def post(self, fn, *args):
        fn(*args)


class _StubApp:
    def __init__(self, root):
        self.root = root
        self._dispatch = _Dispatch()
        self.badge_refreshes = 0

    def _refresh_health_badge(self):
        self.badge_refreshes += 1


def _card(grade, headline="x", detail=""):
    return {"grade": grade, "headline": headline, "detail": detail, "note": ""}


def _cards(**grades):
    return {k: _card(v) for k, v in grades.items()}


def _labels(widget):
    found = []
    stack = [widget]
    while stack:
        cur = stack.pop()
        try:
            if cur.winfo_class() == "Label":
                found.append(cur.cget("text"))
            stack.extend(cur.winfo_children())
        except Exception:
            pass
    return "\n".join(found)


def _rows(widget):
    from app.ui.widgets import FindingRow
    found = []

    def walk(cur):
        try:
            children = cur.winfo_children()
        except Exception:
            return
        for child in children:
            if isinstance(child, FindingRow):
                found.append(child)
            else:
                walk(child)

    walk(widget)
    return found


def test_health_is_a_main_destination():
    from app.tab_presets import TAB_NAMES, TABS

    assert "Health" in TAB_NAMES
    assert "Health" in TABS
    assert TABS["Health"] == []


def test_tools_no_longer_has_a_health_card():
    from app.ui.tabs.toolstab import ToolsTab

    keys = [card[0] for card in ToolsTab._CARDS]
    assert "health" not in keys
    assert "health_report" not in keys


def test_tab_satisfies_the_run_gate_contract():
    from app.ui.tabs.healthtab import HealthTab

    root = make_root()
    try:
        root.withdraw()
        tab = HealthTab(root, _StubApp(root))
        tab.set_run_enabled(False)
        assert tab._primary._enabled is False
        tab.set_run_enabled(True)
        assert tab._primary._enabled is True
    finally:
        root.destroy()


def test_opening_the_tab_does_not_start_a_scan(monkeypatch):
    from app.ui.tabs import healthtab

    calls = []
    monkeypatch.setattr(healthtab.HealthTab, "_start_scan",
                        lambda self: calls.append(True))
    root = make_root()
    try:
        root.withdraw()
        tab = healthtab.HealthTab(root, _StubApp(root))
        tab.on_show()
        assert calls == []
    finally:
        root.destroy()


def test_empty_state_names_the_single_action():
    from app.ui.tabs import healthtab

    root = make_root()
    try:
        root.withdraw()
        tab = healthtab.HealthTab(root, _StubApp(root))
        text = _labels(tab._issue_area)
        assert "Not checked yet" in text
        assert "Check PC" in text
        assert tab._primary._text == "Check PC"
    finally:
        root.destroy()


def test_priorities_render_with_one_route_action_each():
    from app.ui.tabs import healthtab

    root = make_root()
    try:
        root.withdraw()
        tab = healthtab.HealthTab(root, _StubApp(root))
        tab._cards = {
            "smart": _card("F", "Drive health is failing",
                           "SMART says replace"),
            "disk": _card("C", "C: is getting full", "Only 4 GB free"),                "startup": _card("B", "Six programs start with Windows",
                                 "You can turn them off"),
                "memory": _card("A", "Memory is fine", "Plenty free"),
            }
        tab._render()
        rows = _rows(tab._issue_area)
        assert len(rows) == 3
        assert rows[0].title_lbl.cget("text") == "Drive health is failing"
        assert rows[0].action() is not None
        assert "Open Drive Toolkit" == rows[0].action()._text
        assert rows[1].action()._text == "Open Clean"
        assert rows[2].action()._text == "Open Startup"
        assert "1 other check passed" in _labels(tab._issue_area)
    finally:
        root.destroy()


def test_unknown_cards_are_not_drawn_as_failures():
    from app.ui.tabs import healthtab

    root = make_root()
    try:
        root.withdraw()
        tab = healthtab.HealthTab(root, _StubApp(root))
        tab._cards = {"smart": _card("?", "Couldn't check drives",
                                     "SMART unavailable.")}
        tab._render()
        text = _labels(tab._issue_area)
        assert "1 check couldn't run on this PC." in text
        assert _rows(tab._issue_area) == []
    finally:
        root.destroy()


def test_a_care_action_routes_to_the_screen_that_owns_it(monkeypatch):
    from app.ui.tabs import healthtab

    opened = []
    monkeypatch.setattr("app.routes.open_route",
                        lambda app, target: opened.append(target))
    root = make_root()
    try:
        root.withdraw()
        tab = healthtab.HealthTab(root, _StubApp(root))
        tab._cards = {"disk": _card("D", "C: is full", "2 GB free")}
        tab._render()
        row = _rows(tab._issue_area)[0]
        row.action()._command()
        assert opened == ["clean"]
    finally:
        root.destroy()


def test_scan_callbacks_work_with_the_real_one_argument_dispatcher(monkeypatch):
    """Regression: TkDispatcher.post(fn) takes one callable, not fn + args."""
    from app.ui.tabs import healthtab
    from app.ui.tkdispatch import TkDispatcher

    cards = _cards(disk="A", memory="A", crashes="A", security="A",
                   startup="A", smart="A", windows="A", junk="A")

    def fake_scan(ctx, on_card=None):
        for key, card in cards.items():
            on_card(key, card)
        return cards

    monkeypatch.setattr("app.health_scan.run_health_scan", fake_scan)
    monkeypatch.setattr(healthtab.health_badge, "record", lambda *a, **k: None)
    root = make_root()
    app = _StubApp(root)
    app._dispatch = TkDispatcher(root)
    app._dispatch.start()
    try:
        root.withdraw()
        tab = healthtab.HealthTab(root, app)
        tab._start_scan()
        deadline = time.time() + 4
        while tab._scanning and time.time() < deadline:
            root.update()
            time.sleep(0.02)
        assert tab._scanning is False, "the finish callback was dropped"
        assert len(tab._cards) == 8
        assert tab._primary._text == "Check again"
    finally:
        app._dispatch.stop()
        root.destroy()


def test_finishing_a_scan_records_the_score_once(monkeypatch):
    from app.ui.tabs import healthtab

    recorded = []
    monkeypatch.setattr(healthtab.health_badge, "record",
                        lambda *a, **k: recorded.append((a, k)))
    root = make_root()
    try:
        root.withdraw()
        app = _StubApp(root)
        tab = healthtab.HealthTab(root, app)
        tab._cards = _cards(disk="A", memory="A", crashes="A", security="A",
                            startup="A", smart="A", windows="A", junk="A")
        tab._scanning = True
        tab._scan_seq = 1
        tab._finish_scan(1)
        assert recorded
        assert recorded[0][0][0] == "A"
        assert recorded[0][1]["score"] == 100
        assert tab._primary._text == "Check again"
        assert app.badge_refreshes == 1
    finally:
        root.destroy()


def test_the_page_keeps_the_removed_heavy_surfaces_out():
    """N-001 brought the trend back; it did not bring the old page back.

    The audit's complaint was three stacked panels, a decline banner, an
    Advisor button and a Smart Care runner. The trend is now ONE compact row
    with a sparkline and a sentence, so the rest stays gone.
    """
    from app.ui.tabs import healthtab

    root = make_root()
    try:
        root.withdraw()
        tab = healthtab.HealthTab(root, _StubApp(root))
        assert tab._trend_card is not None
        for gone in ("_trend_chart", "_trend_alert", "_advisor_btn",
                     "_care_bar", "_care_footer", "_shared_area",
                     "_timeline_more"):
            assert not hasattr(tab, gone), gone
        # No history in a fresh profile: the row is not on screen at all.
        assert not tab._trend_card.winfo_manager()
    finally:
        root.destroy()


def test_health_tab_reads_the_bus_instead_of_running_a_second_plan():
    """A-001 is a READER: one `active()` call and a row per finding.

    The findings bus was written by six tools and read by no screen; the fix
    was a reader, not a second plan engine (the one that existed had no
    caller and is deleted). Health does not PUBLISH either - the tools that
    measure own what the board says.
    """
    from app.ui.tabs import healthtab

    source = inspect.getsource(healthtab)
    assert "insights.active(" in source
    assert "insights.publish(" not in source
    # The ledger is the one history source: no second store may creep in.
    assert source.count("health_history") >= 1


def test_tools_findings_appear_in_the_worklist(monkeypatch):
    """A-001: the bus is written by six tools; Health is where it surfaces."""
    from app import insights
    from app.ui.tabs import healthtab

    monkeypatch.setattr(
        insights, "active",
        lambda **_k: [type("F", (), {
            "title": "172 duplicate files",
            "detail": "Worth reviewing in Downloads",
            "severity": "warn",
            "action": {"target": "duplicates"},
        })()])
    root = make_root()
    try:
        root.withdraw()
        tab = healthtab.HealthTab(root, _StubApp(root))
        tab._render()
        text = _labels(tab._issue_area)
        assert "ALSO REPORTED BY TOOLS" in text
        assert "172 duplicate files" in text
        rows = _rows(tab._issue_area)
        assert any(r.action() is not None for r in rows)
    finally:
        root.destroy()


def test_the_worklist_stops_at_five_rows(monkeypatch):
    from app.ui.tabs import healthtab

    root = make_root()
    try:
        root.withdraw()
        tab = healthtab.HealthTab(root, _StubApp(root))
        tab._cards = {k: _card("F", "bad %s" % k)
                      for k in ("a", "b", "c", "d", "e", "f", "g")}
        tab._render()
        assert len(_rows(tab._issue_area)) == tab.MAX_ROWS == 5
    finally:
        root.destroy()


def test_a_not_applicable_check_is_not_reported_as_broken():
    from app.ui.tabs import healthtab

    root = make_root()
    try:
        root.withdraw()
        tab = healthtab.HealthTab(root, _StubApp(root))
        tab._cards = {
            "battery": _card("?", "Not applicable",
                             "No battery fitted — this is a desktop."),
            "smart": _card("?", "Couldn't check drives", "SMART missing."),
        }
        tab._render()
        text = _labels(tab._issue_area)
        assert "1 check couldn't run on this PC." in text
        assert "1 check does not apply to this PC." in text
    finally:
        root.destroy()


def test_saved_history_supplies_the_headline_when_nothing_was_scanned(
        monkeypatch, tmp_path):
    """A-010: the score shown must carry the age it was actually measured."""
    from app.ui.tabs import healthtab

    now = time.time()
    monkeypatch.setattr(
        healthtab.health_history, "trend",
        lambda: {"count": 1, "last": 82, "delta": None,
                 "scores": [82], "decline": None, "last_ts": now - 3600,
                 "points": [{"t": now - 3600, "s": 82, "g": "B"}]})
    root = make_root()
    try:
        root.withdraw()
        tab = healthtab.HealthTab(root, _StubApp(root))
        assert tab._ring._score == 82
        assert tab._headline.cget("text") == "Last checkup"
        assert "saved" in tab._meta.cget("text")
        assert "hour ago" in tab._meta.cget("text")
    finally:
        root.destroy()


def test_the_trend_row_names_the_cause_of_a_decline(monkeypatch):
    from app.ui.tabs import healthtab

    now = time.time()
    trend = {
        "count": 3, "last": 71, "delta": -6, "scores": [88, 84, 71],
        "decline": {"drop": 17, "streak": 2,
                    "causes": [{"key": "disk", "from": "B", "to": "D"}]},
        "last_ts": now,
        "points": [{"t": now - 100, "s": 88, "g": "B"},
                   {"t": now - 50, "s": 84, "g": "B"},
                   {"t": now, "s": 71, "g": "C"}],
    }
    monkeypatch.setattr(healthtab.health_history, "trend", lambda: trend)
    root = make_root()
    try:
        root.withdraw()
        tab = healthtab.HealthTab(root, _StubApp(root))
        assert tab._trend_card.winfo_manager() == "pack"
        assert "−6" in tab._trend_delta.cget("text")
        assert "Disk" in tab._trend_decline.cget("text")
        assert "B → D" in tab._trend_decline.cget("text")
    finally:
        root.destroy()


def test_the_trend_row_opens_the_dated_record():
    from app.ui.tabs import healthtab

    class _App(_StubApp):
        opened = 0

        def _open_health_history(self):
            self.opened += 1

    root = make_root()
    try:
        root.withdraw()
        app = _App(root)
        tab = healthtab.HealthTab(root, app)
        tab._open_history()
        assert app.opened == 1
    finally:
        root.destroy()
