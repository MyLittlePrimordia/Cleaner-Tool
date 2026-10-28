"""AUDIT TX-003: one routing table, used by Health, tools and the Advisor."""
from __future__ import annotations

from app import health_model, routes


class App:
    def __init__(self):
        self.calls = []

    def _switch_to(self, name):
        self.calls.append(("tab", name))

    def _open_services(self):
        self.calls.append(("services",))

    def _open_storage_insight(self):
        self.calls.append(("storage",))


def test_every_health_card_route_resolves():
    for action in health_model.CARD_ACTION.values():
        if action.get("kind") != "route":
            continue
        assert routes.get(action["target"]) is not None, action


def test_every_publisher_target_resolves():
    """The targets tools actually publish today, plus their older aliases."""
    for target in ("storage", "drivetoolkit", "duplicates", "readiness",
                   "startup", "services", "procmgr", "drivers"):
        assert routes.get(target) is not None, target


def test_the_removed_screens_have_no_rows_left_behind():
    """2026-10: the Advisor and What Changed were unreachable, so the rows
    went with the windows, and the peripheral testers followed them.

    This replaces the old "every Ask topic has a destination" check: the Ask
    layer (app/ask.py) fed the Advisor window and nothing else, so its
    "timeline" target is now a string with no screen behind it. If any of
    these rows comes back, that has to be deliberate - which is the moment to
    decide whether the window comes back too.
    """
    gone = ("timeline", "advisor", "gamepad", "keyboard", "mouse", "monitor",
            "webcam", "miccheck", "speaker")
    for target in gone:
        assert routes.get(target) is None, target
    # ...and no ALIAS is left pointing at one of them: an entry whose target is
    # not in ROUTES only spells a missing screen a second way, which is how the
    # peripheral spellings outlived the windows by a release.
    for spelling in ("gamepad_tester", "keyboard_tester", "mouse_tester",
                     "monitor_test", "webcam_test", "mic", "microphone",
                     "speaker_test"):
        assert routes.normalise(spelling) == spelling, spelling
        assert routes.get(spelling) is None, spelling


def test_dispatch_uses_the_application_method():
    app = App()
    assert routes.open_route(app, "services") is True
    assert routes.open_route(app, "storage") is True
    assert routes.open_route(app, "health") is True
    assert app.calls == [("services",), ("storage",), ("tab", "Health")]


def test_aliases_and_labels_are_one_destination():
    assert routes.normalise("drivetoolkit") == "drivetool"
    assert routes.get("drivetoolkit") is routes.get("drivetool")
    assert routes.get("game_readiness") is routes.get("readiness")
    assert routes.label("drivetoolkit") == "Open Drive Toolkit"


def test_unknown_targets_fail_closed():
    app = App()
    assert routes.get("not-a-screen") is None
    assert routes.open_route(app, "not-a-screen") is False
    assert app.calls == []


def test_reliability_is_an_external_route(monkeypatch):
    opened = []
    monkeypatch.setattr("app.health_scan.open_reliability_monitor",
                        lambda: opened.append(True))
    assert routes.open_route(App(), "reliability") is True
    assert opened == [True]
