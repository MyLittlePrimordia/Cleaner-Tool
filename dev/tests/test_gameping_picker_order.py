"""U-008: Server Ping's picker order and remembered game.

The ordering lives in `app.gameping` as pure functions so it can be pinned
without Tk: with nothing installed the list must stay exactly what it always
was, and the "● installed" marker is display-only (it must never reach the
saved pick or a target key).
"""
from __future__ import annotations

import pytest

from app import gameping as G
from app.config_schema import apply_schema

tk = pytest.importorskip("tkinter")


def test_with_nothing_installed_the_order_is_unchanged():
    titles = G.order_titles()
    expected = tuple(sorted((t for t, _k in G.GAME_TABS if t != "Custom"),
                            key=lambda t: t.casefold())) + ("Custom",)
    assert titles == expected
    assert titles[-1] == "Custom"


def test_installed_games_float_up_and_stay_recognisable():
    hits = G.installed_titles(["Counter-Strike 2", "Deadlock",
                              "Hunt: Showdown", "EA SPORTS FC 25"])
    assert "CS2, Dota 2 & Deadlock" in hits
    assert "Hunt: Showdown 1896" in hits
    assert "EA Sports FC (FIFA)" in hits
    order = G.order_titles(["Deadlock"])
    assert order[0] == "CS2, Dota 2 & Deadlock"
    assert order[-1] == "Custom"
    assert len(order) == len(G.GAME_TABS)


def test_diagnostic_rows_never_count_as_installed_games():
    # "Steam (Valve)" and the baseline must not float up just because a
    # Steam library exists on the disk.
    assert G.installed_titles(["Steam", "Internet Explorer", "Discord"]) == ()
    for title in G.installed_titles(["Steam", "Discord", "Electronic Arts"]):
        assert title not in ("Internet Baseline", "Steam (Valve)", "Custom")


def test_marker_is_display_only():
    values = G.picker_values(["Deadlock"])
    assert "● CS2, Dota 2 & Deadlock" in values
    assert G.bare_title("● CS2, Dota 2 & Deadlock") == "CS2, Dota 2 & Deadlock"
    assert G.bare_title("Custom") == "Custom"
    assert G.mark_installed("Custom", ("Custom",)) == "● Custom"


def test_saved_pick_survives_a_hand_edit():
    defaults = {"gameping_last_game": ""}
    ok = apply_schema({"gameping_last_game": "VALORANT"}, defaults)
    assert ok["gameping_last_game"] == "VALORANT"
    for junk in (7, None, "", "   ", ["x"]):
        assert apply_schema({"gameping_last_game": junk},
                            defaults)["gameping_last_game"] == ""
    long = apply_schema({"gameping_last_game": "x" * 500}, defaults)
    assert len(long["gameping_last_game"]) == 80


def test_remembered_game_falls_back_when_it_is_gone(monkeypatch):
    from app.ui.dialogs import game_server_ping_dialog as D

    monkeypatch.setattr("app.config_persist.load_config",
                        lambda: {"gameping_last_game": "VALORANT"})
    assert D.GameServerPingDialog._saved_game() == "VALORANT"
    games = ("Fortnite", "VALORANT", "Custom")
    assert D.GameServerPingDialog._initial_game(games, "VALORANT") == "VALORANT"
    # a title that no longer exists (renamed tab, hand-edited config) is not
    # handed to the picker: it falls back to the first row
    assert D.GameServerPingDialog._initial_game(("Fortnite", "Custom"),
                                                "VALORANT") == "Fortnite"
    assert D.GameServerPingDialog._initial_game((), "") == "Custom"


def test_first_scan_moves_a_first_time_open_onto_an_installed_game():
    """No saved pick means the page opened on whatever sorted first. When the
    installed-games scan lands it should start on a game this PC has, while a
    pick the user actually made is never overridden."""
    from app.ui.dialogs import game_server_ping_dialog as D

    try:
        root = tk.Tk()
    except tk.TclError as exc:                 # pragma: no cover - no display
        pytest.skip("no display for Tk: %s" % exc)

    class _Combo:
        values = ()

        def configure(self, **kw):
            self.values = tuple(kw["values"])

    def _dialog(saved):
        d = D.GameServerPingDialog.__new__(D.GameServerPingDialog)
        d._quiet = False
        d._saved_pick = saved
        d._installed = ()
        d._dlg = None
        d._modal_closed = False
        d._game_var = tk.StringVar(master=root, value="Albion Online")
        d._game_combo = _Combo()
        return d

    try:
        fresh = _dialog("")
        fresh._apply_installed_order(["Deadlock"])
        assert fresh._game_var.get() == "● CS2, Dota 2 & Deadlock"
        assert fresh._game_combo.values[0] == "● CS2, Dota 2 & Deadlock"

        chosen = _dialog("Albion Online")
        chosen._apply_installed_order(["Deadlock"])
        assert chosen._game_var.get() == "Albion Online"
        assert chosen._tab_key() == "albion"
    finally:
        root.destroy()


def test_picked_title_resolves_to_its_tab_key_with_the_marker_on():
    from app.ui.dialogs import game_server_ping_dialog as D

    try:
        root = tk.Tk()
    except tk.TclError as exc:                 # pragma: no cover - no display
        pytest.skip("no display for Tk: %s" % exc)
    dialog = D.GameServerPingDialog.__new__(D.GameServerPingDialog)
    try:
        dialog._game_var = tk.StringVar(master=root, value="● VALORANT")
        assert dialog._tab_key() == "valorant"
        dialog._game_var.set("● Custom")
        assert dialog._tab_key() == "custom"
    finally:
        root.destroy()


def test_programmatic_reorder_does_not_save_a_pick(monkeypatch):
    from app.ui.dialogs import game_server_ping_dialog as D

    try:
        root = tk.Tk()
    except tk.TclError as exc:                 # pragma: no cover - no display
        pytest.skip("no display for Tk: %s" % exc)
    dialog = D.GameServerPingDialog.__new__(D.GameServerPingDialog)
    writes = []
    monkeypatch.setattr("app.config_persist.update_config",
                        lambda mut: writes.append(mut))
    try:
        dialog._game_var = tk.StringVar(master=root, value="Fortnite")
        dialog._quiet = True
        dialog._remember_pick()          # the reorder path
        assert writes == []
        dialog._quiet = False
        dialog._remember_pick()          # a real pick
        assert writes
    finally:
        root.destroy()
