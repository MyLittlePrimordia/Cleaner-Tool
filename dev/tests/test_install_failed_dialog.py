"""U-001: a failed install ends with a page you can open, not a URL in a log.

The install engine already knew every catalog entry's vendor page and logged it
on failure — into an in-memory log this app has no window for. These tests pin
the two halves of the repair: mapping failed names back to their catalog entry,
and a dialog that turns each one into a clickable action.
"""
from __future__ import annotations

import pytest

from dev.tests._tkhelpers import make_root

tk = pytest.importorskip("tkinter")


def _apps():
    return [
        {"id": "Foo.Bar", "name": "Foo Bar", "url": "https://foo.example/dl"},
        {"id": "Baz.Qux", "name": "Baz Qux", "url": "https://baz.example/"},
        {"id": "No.Url", "name": "No Url", "url": ""},
    ]


def test_failed_names_map_back_to_their_catalog_page():
    from app.ui.runflow import failed_app_rows

    rows = failed_app_rows(_apps(), ["Foo Bar", "No Url"])
    assert rows == [
        {"name": "Foo Bar", "url": "https://foo.example/dl"},
        {"name": "No Url", "url": ""},
    ]


def test_an_unknown_name_still_yields_a_row():
    """The dialog must list what failed even when the catalog moved on; a
    missing URL means "no button", never "drop the row"."""
    from app.ui.runflow import failed_app_rows

    assert failed_app_rows(_apps(), ["Gone App"]) == [
        {"name": "Gone App", "url": ""}]


@pytest.fixture
def root():
    r = make_root()
    r.geometry("640x420+3000+3000")
    try:
        yield r
    finally:
        r.destroy()


def test_the_dialog_lists_each_failure_and_only_links_the_known_pages(root):
    from app.ui.dialogs.install_failed_dialog import InstallFailedDialog

    dlg = InstallFailedDialog(root, [
        {"name": "Foo Bar", "url": "https://foo.example/dl"},
        {"name": "No Url", "url": ""},
    ], summary="Install complete: 1 succeeded, 2 failed")
    try:
        rows = dlg.table.rows
        assert [r["_key"] for r in rows] == ["Foo Bar", "No Url"]
        assert rows[0]["_actions"] == [{"id": "open", "text": "Open page"}]
        assert rows[1]["_actions"] == []
        # The count in the header is the number of failures, not the run size.
        assert dlg.banner._title.cget("text") == "2 apps didn't install"
    finally:
        dlg.close()


def test_opening_a_row_uses_the_shared_browser_raiser(root, monkeypatch):
    from app.ui.dialogs.install_failed_dialog import InstallFailedDialog
    import app.openlink as openlink

    opened = []
    monkeypatch.setattr(openlink, "open_web_url",
                        lambda url, show_error=None: opened.append(url) or True)

    dlg = InstallFailedDialog(root, [
        {"name": "Foo Bar", "url": "https://foo.example/dl"}])
    try:
        row = dlg.table.rows[0]
        dlg._on_action(row, "open")
        assert opened == ["https://foo.example/dl"]
    finally:
        dlg.close()


def test_a_row_without_a_page_does_nothing(root, monkeypatch):
    from app.ui.dialogs.install_failed_dialog import InstallFailedDialog
    import app.openlink as openlink

    opened = []
    monkeypatch.setattr(openlink, "open_web_url",
                        lambda url, show_error=None: opened.append(url) or True)

    dlg = InstallFailedDialog(root, [{"name": "Gone App", "url": ""}])
    try:
        dlg._on_action(dlg.table.rows[0], "open")
        assert opened == []
    finally:
        dlg.close()
