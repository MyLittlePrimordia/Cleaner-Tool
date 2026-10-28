"""Leftover deletion must count what actually went, and say what did not.

`_after_leftover_delete` used to say "Removed N leftover item(s)" for every
selected item. The folder branch incremented N unconditionally, while
`clean_folder_contents` returns 0 for a protected root, a junction, a missing
path or a folder whose every file was locked — so five access denials could be
reported as five removals, and a total failure read "Removed 0 leftover
item(s)." with no reason and no colour.

The counting lives in `_remove_leftover_items`, which touches no Tk, so it is
exercised here directly with the deletion helpers stubbed. Nothing on disk is
touched.
"""
from __future__ import annotations

import pytest

pytest.importorskip("tkinter")

from app.ui.dialogs import uninstall_programs_dialog as mod
from app.ui.dialogs.uninstall_programs_dialog import UninstallProgramsDialog
from app.ui.theme import COLORS


# --------------------------------------------------------------- item counting

def _stub_helpers(monkeypatch, *, reg=True, startup=True, folder=None,
                  shortcut_error=None):
    """Stub the three destruction helpers the delete path imports."""
    calls = {"reg": [], "startup": [], "folder": [], "shortcut": []}

    def fake_reg(ctx, hive, path):
        calls["reg"].append((hive, path))
        return reg

    def fake_startup(item_id, enabled):
        calls["startup"].append((item_id, enabled))
        return (startup, "" if startup else "Access denied")

    def fake_folder(ctx, path, remove_root=False):
        calls["folder"].append(path)
        return folder(path) if callable(folder) else (folder or 0)

    def fake_remove(target):
        calls["shortcut"].append(target)
        if shortcut_error is not None:
            raise shortcut_error(target)

    monkeypatch.setattr("app.utils.reg_delete_key", fake_reg)
    monkeypatch.setattr("app.startup_manager.set_item_enabled", fake_startup)
    monkeypatch.setattr("app.utils.clean_folder_contents", fake_folder)
    monkeypatch.setattr(mod.os, "remove", fake_remove)
    return calls


def _run(paths):
    # The method uses no instance state, so an unbound call is enough.
    return UninstallProgramsDialog._remove_leftover_items(None, paths)


def test_a_folder_that_gave_up_bytes_counts_as_removed(monkeypatch):
    _stub_helpers(monkeypatch, folder=4096)
    monkeypatch.setattr(mod.os.path, "exists", lambda p: True)
    assert _run([r"C:\Leftovers\App"]) == (1, 0, 4096)


def test_a_folder_that_removed_nothing_is_a_failure_not_a_removal(monkeypatch):
    # The old code counted this as removed: a protected root, a junction or a
    # folder of locked files all come back from clean_folder_contents as 0.
    _stub_helpers(monkeypatch, folder=0)
    monkeypatch.setattr(mod.os.path, "exists", lambda p: True)
    assert _run([r"C:\Leftovers\Locked"]) == (0, 1, 0)


def test_an_empty_folder_that_was_actually_removed_still_counts(monkeypatch):
    _stub_helpers(monkeypatch, folder=0)
    monkeypatch.setattr(mod.os.path, "exists", lambda p: False)
    assert _run([r"C:\Leftovers\Empty"]) == (1, 0, 0)


def test_a_refused_registry_key_is_a_failure(monkeypatch):
    calls = _stub_helpers(monkeypatch, reg=False)
    assert _run(["REGKEY::HKCU::Software\\Vendor\\App"]) == (0, 1, 0)
    assert calls["reg"] == [("HKCU", "Software\\Vendor\\App")]


def test_a_refused_startup_entry_is_a_failure(monkeypatch):
    calls = _stub_helpers(monkeypatch, startup=False)
    assert _run(["STARTUP::abc123"]) == (0, 1, 0)
    assert calls["startup"] == [("abc123", False)]


def test_a_shortcut_that_cannot_be_deleted_is_a_failure(monkeypatch):
    _stub_helpers(monkeypatch, shortcut_error=PermissionError)
    assert _run([r"SHORTCUT::C:\ProgramData\...\App.lnk"]) == (0, 1, 0)


def test_a_shortcut_already_gone_is_not_a_failure(monkeypatch):
    # The uninstaller usually removed its own shortcut first: the leftover is
    # gone, so there is nothing to warn the user about.
    _stub_helpers(monkeypatch, shortcut_error=FileNotFoundError)
    assert _run([r"SHORTCUT::C:\ProgramData\...\App.lnk"]) == (1, 0, 0)


def test_mixed_batch_reports_both_sides(monkeypatch):
    _stub_helpers(monkeypatch, reg=True, startup=False, folder=2048)
    monkeypatch.setattr(mod.os.path, "exists", lambda p: True)
    assert _run([
        "REGKEY::HKCU::Software\\Vendor\\App",
        "STARTUP::abc123",
        r"C:\Leftovers\App",
    ]) == (2, 1, 2048)


def test_missing_helpers_count_every_item_as_failed(monkeypatch):
    # If the helpers cannot even be imported, nothing ran — so nothing was
    # removed, and the message must not imply otherwise.
    monkeypatch.delattr("app.utils.clean_folder_contents")
    assert _run(["a", "b", "c"]) == (0, 3, 0)


# --------------------------------------------------------------- status line

class _Status:
    def __init__(self):
        self.calls = []

    def stop(self, text=None, color=None):
        self.calls.append((text, color))


class _Fake:
    def __init__(self):
        self.status = _Status()
        self.cleared = 0
        self.refreshed = 0

    def _clear_leftover_panel(self):
        self.cleared += 1

    def _refresh(self):
        self.refreshed += 1


def _land(ok_count, failed, freed=0):
    fake = _Fake()
    UninstallProgramsDialog._after_leftover_delete(fake, ok_count, failed, freed)
    return fake, fake.status.calls[-1]


def test_total_failure_says_so_in_red_and_never_says_removed():
    fake, (text, color) = _land(0, 3)
    assert "Could not remove any of the 3" in text
    assert "Removed" not in text
    assert "administrator" in text
    assert color == COLORS["accent_red"]
    assert (fake.cleared, fake.refreshed) == (1, 1)


def test_partial_success_names_both_numbers_in_amber():
    _, (text, color) = _land(2, 1, freed=int(1.5 * 1024 * 1024))
    assert "Removed 2 of 3 leftover item(s)" in text
    assert "~1.5 MB freed" in text
    assert "1 could not be removed" in text
    assert color == COLORS["accent_yellow"]


def test_a_clean_sweep_is_green_and_reports_the_space():
    _, (text, color) = _land(4, 0, freed=2 * 1024 * 1024)
    assert text == "Removed 4 leftover item(s) (~2.0 MB freed)."
    assert color == COLORS["accent_green"]


def test_a_clean_sweep_of_empty_items_has_no_size_noise():
    _, (text, _color) = _land(1, 0)
    assert text == "Removed 1 leftover item(s)."
