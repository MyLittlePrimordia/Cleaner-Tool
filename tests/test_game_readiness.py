"""Game Readiness: read-only checks, no writes, fixes only via real tweaks."""

import pytest

import app.game_readiness as gr


def test_check_returns_every_row_with_required_fields():
    rows = gr.check()
    assert len(rows) == 7
    keys = {r["key"] for r in rows}
    assert keys == {"game_mode", "game_dvr", "power_plan", "gpu_driver",
                    "refresh", "free_space", "reboot"}
    for r in rows:
        assert r["state"] in (gr.OK, gr.WARN, gr.INFO)
        assert isinstance(r["detail"], str)


def test_game_mode_off_is_a_warning_with_the_tweak(monkeypatch):
    monkeypatch.setattr(gr, "_reg",
                        lambda path, name: 0 if name == "AutoGameModeEnabled" else None)
    row = gr._game_mode()
    assert row["state"] == gr.WARN
    assert row["fix"] == "game_mode"


def test_game_mode_on_is_ok_and_offers_nothing(monkeypatch):
    monkeypatch.setattr(gr, "_reg",
                        lambda path, name: 1 if name == "AutoGameModeEnabled" else None)
    row = gr._game_mode()
    assert row["state"] == gr.OK and row["fix"] is None


def test_unreadable_game_mode_is_unknown_not_broken(monkeypatch):
    monkeypatch.setattr(gr, "_reg", lambda path, name: None)
    row = gr._game_mode()
    assert row["state"] == gr.INFO and row["fix"] == "game_mode"


@pytest.mark.parametrize("value,state,fix", [
    (1, gr.WARN, "disable_game_dvr"),
    (0, gr.OK, None),
])
def test_game_dvr(monkeypatch, value, state, fix):
    monkeypatch.setattr(gr, "_reg", lambda path, name: value)
    row = gr._game_dvr()
    assert row["state"] == state and row["fix"] == fix


@pytest.mark.parametrize("guid,state", [
    ("8c5e7fda-e8bf-4a96-9a85-a6e23a8c635c", gr.OK),
    ("e9a42b02-d5df-448d-aa00-03f14749eb61", gr.OK),
    ("381b4222-f694-41f0-9685-ff5bb260df2e", gr.WARN),
    ("a1841308-3541-4fab-bc81-f71556f20b4a", gr.WARN),
    # A custom plan we have not read is INFO, not a nagging warning.
    ("00c34285-afb4-4c06-b63c-00d4060c7626", gr.INFO),
])
def test_power_plan_states(monkeypatch, guid, state):
    monkeypatch.setattr(gr, "active_power_plan", lambda: (guid, ""))
    row = gr._power()
    assert row["state"] == state
    assert (row["fix"] is None) == (state == gr.OK)


def test_power_plan_unreadable_is_info(monkeypatch):
    monkeypatch.setattr(gr, "active_power_plan", lambda: ("", ""))
    row = gr._power()
    assert row["state"] == gr.INFO and row["fix"] is None


def test_active_power_plan_parses_guid(monkeypatch):
    class _R:
        stdout = ("Power Scheme GUID: 381b4222-f694-41f0-9685-ff5bb260df2e  "
                  "(Balanced)")
    monkeypatch.setattr(gr.subprocess, "run", lambda *a, **k: _R())
    guid, name = gr.active_power_plan()
    assert guid == "381b4222-f694-41f0-9685-ff5bb260df2e"
    assert name == "Balanced"


def test_driver_route_is_the_drivers_tab_not_a_tweak(monkeypatch):
    class _R:
        stdout = "2020-01-01\n"
    monkeypatch.setattr(gr.subprocess, "run", lambda *a, **k: _R())
    row = gr._gpu_driver()
    assert row["state"] == gr.WARN
    assert row["fix"] == "drivers_tab"
    # Guard: the fix must never be mistaken for a tweak key.
    from app.tab_presets import TABS
    assert "drivers_tab" not in {t.key for t in TABS.get("Tweak", [])}


def test_every_tweak_fix_exists_in_the_tweak_table(monkeypatch):
    """A readiness row whose fix key is stale would be a silent dead button."""
    from app.tab_presets import TABS
    keys = {t.key for t in TABS.get("Tweak", [])}
    specials = {"drivers_tab", "storage_insight"}
    for r in gr.check():
        fix = r.get("fix")
        assert not fix or fix in keys or fix in specials, (r["key"], fix)


def test_summary_counts_only_warnings():
    rows = [{"state": gr.WARN}, {"state": gr.OK}, {"state": gr.INFO}]
    assert gr.summary(rows) == (1, 3)


def test_checks_never_raise_even_when_a_helper_blows_up(monkeypatch):
    def _boom():
        raise RuntimeError("nope")
    monkeypatch.setattr(gr, "_game_mode", _boom)
    rows = gr.check()
    assert len(rows) == 7
    bad = next(r for r in rows if "nope" in r["detail"])
    assert bad["state"] == gr.INFO and "nope" in bad["detail"]
