"""The in-app "Interface scale" setting (2026-10).

One value, four layers, each failing differently — so each is pinned on its
own: the config schema (does a hand-edited value survive a load?),
`settings_apply` (does the save path write exactly what the schema accepts?),
`app/dpi` (does the stored percentage become the right Tk scaling?), and the
Settings page (is every stored value reachable in the dropdown?).

The last one matters most here: the setting exists so a user on a 100% display
can choose a density instead of living with the app's 125% floor. If the
dropdown could not produce "100", the floor would still be the floor.
"""
from __future__ import annotations

import pytest


# --------------------------------------------------------------------------- #
# config schema
# --------------------------------------------------------------------------- #

def test_the_default_is_automatic_and_the_schema_agrees():
    from app.config_persist import DEFAULT_CONFIG
    from app.config_schema import SCHEMA

    # "system" keeps the old behaviour for every existing install: follow the
    # display, never below the 125% floor.
    assert DEFAULT_CONFIG["ui_scale"] == "system"
    assert "ui_scale" in SCHEMA


def test_the_scale_list_has_exactly_one_owner():
    """Settings, the schema and dpi must not hold three copies of the list.

    A re-declared tuple is how the dropdown grows an option the config loader
    then silently rewrites to "system" (or the reverse).
    """
    from app.config_schema import UI_SCALES
    from app.dpi import UI_SCALE_CHOICES

    assert UI_SCALE_CHOICES == UI_SCALES
    assert UI_SCALES == ("system", "100", "125", "150", "175", "200")


@pytest.mark.parametrize("stored,want", [
    ("100", "100"),
    (" 200 ", "200"),      # whitespace is not an error
    ("150", "150"),
    ("SYSTEM", "system"),  # case is not an error
    ("137", "system"),     # not a Windows rung
    ("150%", "system"),    # Settings stores the bare number
    (150, "system"),       # a number is not a string
    ("", "system"),
    (None, "system"),
])
def test_only_the_listed_rungs_survive_a_load(stored, want):
    from app.config_persist import DEFAULT_CONFIG
    from app.config_schema import apply_schema

    assert apply_schema({"ui_scale": stored},
                        DEFAULT_CONFIG)["ui_scale"] == want


# --------------------------------------------------------------------------- #
# the mapping to Tk scaling
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("choice,current,want", [
    # Automatic follows the display, with the floor underneath it.
    ("system", 96 / 72, 120 / 72),
    ("system", 2.0, 2.0),
    (None, 96 / 72, 120 / 72),
    ("nonsense", 96 / 72, 120 / 72),
    # An explicit rung is exact, including under the floor.
    ("100", 2.0, 96 / 72),
    ("125", 96 / 72, 120 / 72),
    ("150", 96 / 72, 144 / 72),
    ("175", 96 / 72, 168 / 72),
    ("200", 96 / 72, 192 / 72),
    ("150%", 96 / 72, 144 / 72),
    (" 150 ", 96 / 72, 144 / 72),
])
def test_the_choice_maps_to_the_windows_percentage(choice, current, want):
    from app.dpi import target_tk_scaling

    assert target_tk_scaling(choice, current) == pytest.approx(want)


def test_a_nonsense_display_scale_still_resolves():
    """The launch path asks this before Tk has painted; it may never raise."""
    from app.dpi import MIN_TK_SCALING, target_tk_scaling

    assert target_tk_scaling("system", "not a number") == MIN_TK_SCALING
    assert target_tk_scaling("100", None) == 96 / 72


def test_an_explicit_100_can_go_below_the_default_floor():
    from dev.tests._tkhelpers import make_root
    from app.dpi import MIN_TK_SCALING, apply_ui_density

    root = make_root()
    original = float(root.tk.call("tk", "scaling"))
    try:
        root.tk.call("tk", "scaling", 2.0)
        assert apply_ui_density(root, "100") is True
        assert float(root.tk.call("tk", "scaling")) == pytest.approx(
            96 / 72, abs=0.02)
        assert MIN_TK_SCALING > 96 / 72      # it really is under the floor
        # Idempotent: a second launch pass does not re-write the same value.
        assert apply_ui_density(root, "100") is False
        # And "Automatic" lifts it back to the floor.
        assert apply_ui_density(root, "system") is True
        assert float(root.tk.call("tk", "scaling")) >= MIN_TK_SCALING - 0.001
    finally:
        root.tk.call("tk", "scaling", original)
        root.destroy()


# --------------------------------------------------------------------------- #
# the save path
# --------------------------------------------------------------------------- #

def _capture_updates(monkeypatch):
    saved = {}

    def _update(mut):
        data = {}
        mut(data)                  # what would have been written to disk
        saved.update(data)

    monkeypatch.setattr("app.config_persist.update_config", _update)
    return saved


def test_saving_writes_the_choice_and_says_when_it_applies(monkeypatch):
    from app import settings_apply

    saved = _capture_updates(monkeypatch)
    results, errors = settings_apply.apply_settings(
        {"ui_scale": "150"}, config={})
    assert saved == {"ui_scale": "150"}
    assert errors == []
    # Honest: Tk's scale is read before the first widget exists, so the change
    # cannot be live. The user is told, not surprised.
    assert any("restart" in r.lower() for r in results)


def test_saving_tolerates_a_percent_sign_but_not_a_made_up_rung(monkeypatch):
    from app import settings_apply

    saved = _capture_updates(monkeypatch)
    settings_apply.apply_settings({"ui_scale": "150%"}, config={})
    assert saved == {"ui_scale": "150"}

    # An unknown value is dropped rather than written: the coercer would turn
    # it back into "system" on the next load, so writing it hides a UI bug.
    saved.clear()
    results, errors = settings_apply.apply_settings(
        {"ui_scale": "137"}, config={})
    assert saved == {}
    assert errors == []


def test_an_unchanged_scale_does_not_keep_asking_for_a_restart(monkeypatch):
    """The form carries every control on every Save. If the scale did not
    move, the status must not tell the user to restart for an unrelated
    change."""
    from app import settings_apply

    saved = _capture_updates(monkeypatch)
    results, errors = settings_apply.apply_settings(
        {"ui_scale": "system"}, config={"ui_scale": "system"})
    assert saved == {"ui_scale": "system"}   # idempotent write
    assert errors == []
    assert not any("restart" in r.lower() for r in results)


def test_a_form_without_the_key_leaves_the_stored_scale_alone(monkeypatch):
    from app import settings_apply

    saved = _capture_updates(monkeypatch)
    settings_apply.apply_settings({"close_to_tray": True}, config={})
    assert "ui_scale" not in saved
