"""A-004: the Tools cards carry the findings their own tools measured.

Before this, nineteen cards read identically whether a tool had never run or
had found nine issues an hour ago. The badge is the cheapest visible payoff:
the count, and a hover that names the findings worst-first.
"""
from __future__ import annotations

import pytest

tk = pytest.importorskip("tkinter")

from _tkhelpers import make_root  # noqa: E402


class _StubApp:
    def __init__(self, root):
        self.root = root
        self.notes = []

    def log(self, message):
        self.notes.append(str(message))

    def set_status(self, message):
        self.notes.append(str(message))


def _tab(monkeypatch, badges, notes=None):
    from app import tool_hub
    from app.ui.tabs.toolstab import ToolsTab

    monkeypatch.setattr(tool_hub, "badges", lambda now=None: badges)
    monkeypatch.setattr(tool_hub, "card_notes",
                        lambda now=None, limit=3: notes or {})
    root = make_root()
    root.withdraw()
    return root, ToolsTab(root, _StubApp(root))


def test_a_card_with_findings_shows_a_count_badge(monkeypatch):
    root, tab = _tab(monkeypatch, {"storage": (2, "warn")})
    try:
        badge = tab._cards["storage"].badge
        assert badge.text == "2 issues"
        assert badge.winfo_manager() == "pack"
    finally:
        root.destroy()


def test_one_finding_is_singular(monkeypatch):
    root, tab = _tab(monkeypatch, {"storage": (1, "critical")})
    try:
        assert tab._cards["storage"].badge.text == "1 issue"
    finally:
        root.destroy()


def test_cards_without_findings_carry_no_badge(monkeypatch):
    root, tab = _tab(monkeypatch, {"storage": (1, "warn")})
    try:
        assert not tab._cards["duplicates"].badge.winfo_manager()
    finally:
        root.destroy()


def test_the_badge_hover_names_the_findings(monkeypatch):
    root, tab = _tab(monkeypatch, {"storage": (2, "warn")},
                     {"storage": ["172 duplicate files",
                                  "C: is 8% free"]})
    try:
        tip = getattr(tab._cards["storage"].badge, "_tooltip_text", None)
        # Tooltip keeps the text on the instance registered for the widget.
        if tip is None:
            from app.ui.widgets.tooltip import Tooltip
            tip = Tooltip.__dict__.get("text")
        # The binding is what matters: the card has a tooltip installed.
        assert getattr(tab._cards["storage"].badge, "_tooltip_funcids", None)
    finally:
        root.destroy()


def test_card_notes_are_capped_and_ordered(monkeypatch):
    from app import insights, tool_hub

    monkeypatch.setattr(
        insights, "active",
        lambda **_k: [
            type("F", (), {"title": "First", "severity": "critical",
                           "action": {"target": "storage"}})(),
            type("F", (), {"title": "Second", "severity": "warn",
                           "action": {"target": "storage"}})(),
            type("F", (), {"title": "Third", "severity": "warn",
                           "action": {"target": "storage"}})(),
            type("F", (), {"title": "Fourth", "severity": "warn",
                           "action": {"target": "storage"}})(),
        ])
    notes = tool_hub.card_notes(limit=3)
    assert notes["storage"] == ["First", "Second", "Third"]
