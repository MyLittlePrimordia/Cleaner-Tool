"""The saved-setup Delete button must ask before it deletes.

"My Setups" was the one destructive surface in the app with no confirmation:
one click on a row's Delete removed the setup outright, while Process
Manager, Uninstall leftovers and service changes all ask first. These tests
pin the question - the row's name in the title, the app count in the body -
and the honest report afterwards, so a refused or failed delete cannot read
as success.

The dialog is built on a real Tk root; the confirm helper and the config
store are stubbed, so nothing is written anywhere.
"""
from __future__ import annotations

import pytest

tk = pytest.importorskip("tkinter")

from app.ui.dialogs import install_profiles_dialog as mod
from app.ui.dialogs.install_profiles_dialog import InstallProfilesDialog
from app.ui.widgets import AnimatedButton

from _tkhelpers import make_root


class _StubTab:
    """Just enough of the Install tab for the dialog to build."""

    def __init__(self, ids=()):
        self._ids = list(ids)

    def profile_ids(self):
        return list(self._ids)

    def apply_profile_ids(self, ids):
        return (len(ids), 0)


def _build(monkeypatch, profiles=None, *, answer=True, boom=False):
    """Return (root, dialog, calls) with the confirm and the store stubbed."""
    profiles = {"My Setup": ["x"], "Work": ["a", "b", "c"]} if profiles is None \
        else profiles
    calls = {"ask": [], "deleted": []}

    def fake_ask(parent, title, message, accent=None, yes_text="Continue",
                 no_text="Go Back"):
        calls["ask"].append(dict(parent=parent, title=title, message=message,
                                 accent=accent, yes_text=yes_text,
                                 no_text=no_text))
        if boom:
            raise RuntimeError("dialog failed")
        return answer

    def fake_delete(name):
        calls["deleted"].append(name)
        return True

    monkeypatch.setattr(mod, "_themed_askyesno", fake_ask)
    monkeypatch.setattr("app.config_persist.get_install_profiles",
                        lambda: dict(profiles))
    monkeypatch.setattr("app.config_persist.delete_install_profile",
                        fake_delete)
    root = make_root()
    root.withdraw()
    return root, InstallProfilesDialog(root, _StubTab()), calls


def _delete_buttons(dialog):
    """[(label, button)] for every row Delete button, in build order."""
    found = []

    def walk(widget):
        for child in widget.winfo_children():
            if isinstance(child, AnimatedButton) and child._text == "Delete":
                found.append(child)
            walk(child)

    walk(dialog._rows_holder)
    return found


def test_a_declined_confirm_keeps_the_setup(monkeypatch):
    root, dlg, calls = _build(monkeypatch, answer=False)
    try:
        assert _delete_buttons(dlg), "the rows should offer Delete"
        _delete_buttons(dlg)[0]._command()
        assert len(calls["ask"]) == 1
        assert calls["deleted"] == []
        assert "Deleted" not in dlg._status.cget("text")
        # Rows are untouched: both setups still listed.
        assert len(_delete_buttons(dlg)) == 2
    finally:
        dlg.close()
        root.destroy()


def test_the_question_names_the_setup_and_how_many_apps_go(monkeypatch):
    root, dlg, calls = _build(monkeypatch)
    try:
        # Rows are sorted case-insensitively: "My Setup" then "Work".
        _delete_buttons(dlg)[1]._command()
        ask = calls["ask"][0]
        # the question itself is the title; the body says how much it holds
        assert ask["title"] == "Delete 'Work'?"
        assert "3 apps" in ask["message"]
        assert "only the saved setup is removed" in ask["message"]
        assert ask["yes_text"] == "Delete"
        assert ask["no_text"] == "Keep it"
        assert ask["accent"] == mod.TAB_ACCENTS["Install"]
    finally:
        dlg.close()
        root.destroy()


def test_confirming_deletes_exactly_that_setup_and_says_so(monkeypatch):
    root, dlg, calls = _build(monkeypatch)
    try:
        _delete_buttons(dlg)[1]._command()
        assert calls["deleted"] == ["Work"]
        assert dlg._status.cget("text") == "Deleted 'Work'."
    finally:
        dlg.close()
        root.destroy()


def test_a_delete_that_reports_failure_does_not_claim_success(monkeypatch):
    root, dlg, calls = _build(monkeypatch)
    monkeypatch.setattr("app.config_persist.delete_install_profile",
                        lambda name: calls["deleted"].append(name) or False)
    try:
        _delete_buttons(dlg)[0]._command()
        assert calls["deleted"] == ["My Setup"]
        text = dlg._status.cget("text")
        assert "Deleted" not in text
        assert "Couldn't delete 'My Setup'" in text
    finally:
        dlg.close()
        root.destroy()


def test_a_delete_that_raises_does_not_claim_success(monkeypatch):
    root, dlg, calls = _build(monkeypatch)

    def _boom(name):
        raise OSError("config file locked")

    monkeypatch.setattr("app.config_persist.delete_install_profile", _boom)
    try:
        _delete_buttons(dlg)[0]._command()
        text = dlg._status.cget("text")
        assert "Deleted" not in text
        assert "Couldn't delete" in text
    finally:
        dlg.close()
        root.destroy()


def test_a_confirm_that_cannot_be_shown_keeps_the_setup(monkeypatch):
    # Fail safe: a broken dialog must never be read as consent.
    root, dlg, calls = _build(monkeypatch, boom=True)
    try:
        _delete_buttons(dlg)[0]._command()
        assert calls["deleted"] == []
        assert dlg._status.cget("text") == ""
    finally:
        dlg.close()
        root.destroy()
