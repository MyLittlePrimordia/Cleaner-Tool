"""U-003: a category total can be opened to show what makes it up.

Storage Insight answered "how much" per category and never "what", so a 16 GB
row could not be audited: which cleaner held the bytes, and which one had
refused to look. The member rows come from the same walk that produced the
total (never a second measurement), and a declined cleaner stays unknown with
its own reason instead of a 0 it never measured.
"""
from __future__ import annotations

import pytest

from dev.tests._tkhelpers import make_root

tk = pytest.importorskip("tkinter")


def test_members_carry_their_own_numbers_and_reasons():
    from app.storage_scan import category_members

    members = category_members(
        ["office_cache", "inet_cache"],
        {"office_cache": None, "inet_cache": 2048},
        {"office_cache": "Office may be running"})
    by_key = {m["key"]: m for m in members}
    assert by_key["inet_cache"]["size"] == 2048
    assert by_key["office_cache"]["size"] is None
    assert by_key["office_cache"]["reason"] == "Office may be running"
    # Labels/descriptions come from the live task table - the same words the
    # Clean tab shows, so the breakdown cannot describe a cleaner differently.
    assert by_key["inet_cache"]["label"]
    assert by_key["inet_cache"]["description"]


def test_an_unknown_key_still_yields_a_row():
    from app.storage_scan import category_members

    (member,) = category_members(["not_a_real_task"], {}, {})
    assert member["label"] == "not_a_real_task"
    assert member["size"] is None


@pytest.fixture
def root():
    r = make_root()
    r.geometry("660x420+3000+3000")
    try:
        yield r
    finally:
        r.destroy()


MEMBERS = [
    {"key": "office_cache", "label": "Clear Office Cache",
     "description": "Clears Microsoft Office caches",
     "size": None, "reason": "Office may be running"},
    {"key": "inet_cache", "label": "Clear Internet Cache",
     "description": "Clears the Windows web cache",
     "size": 2_000_000_000, "reason": ""},
]


def test_the_breakdown_shows_sizes_and_puts_unknowns_last(root):
    from app.ui.dialogs.storage_category_dialog import StorageCategoryDialog
    from app.utils import format_bytes

    dlg = StorageCategoryDialog(root, "Browser & app caches", MEMBERS,
                                total=2_000_000_000)
    try:
        rows = dlg.table.rows
        assert [r["_key"] for r in rows] == ["inet_cache", "office_cache"]
        known, unknown = rows
        assert known["_cells"]["size"]["text"] == format_bytes(2_000_000_000)
        assert unknown["_cells"]["size"]["text"] == "?"
        # The refusal is the member's own reason, not a generic "not measured".
        assert unknown["_cells"]["member"]["sub"] == "Office may be running"
    finally:
        dlg.close()


def test_a_category_with_nothing_measured_says_so(root):
    from app.ui.dialogs.storage_category_dialog import StorageCategoryDialog

    dlg = StorageCategoryDialog(root, "Dev & package caches", [])
    try:
        assert dlg.table.rows == []
        assert dlg.banner._sub.cget("text").startswith("Nothing measurable")
    finally:
        dlg.close()


def test_the_header_quotes_the_same_total_the_row_did(root):
    from app.ui.dialogs.storage_category_dialog import StorageCategoryDialog
    from app.utils import format_bytes

    dlg = StorageCategoryDialog(root, "Browser & app caches", MEMBERS,
                                total=2_000_000_000)
    try:
        assert format_bytes(2_000_000_000) in dlg.banner._sub.cget("text")
    finally:
        dlg.close()
