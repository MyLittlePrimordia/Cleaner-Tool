"""U-002: the Install tab says where its catalog came from.

The catalog is a bundled Python list, not a live index, and its entries were
verified on a specific date. Saying so is the honest half of shipping a static
list: when a publisher retires a package between releases, the failed install
is visibly a stale entry rather than looking like this app's bug.
"""
from __future__ import annotations

import re

import pytest

from app import app_catalog as C

tk = pytest.importorskip("tkinter")


def test_provenance_counts_the_catalog_itself():
    info = C.provenance()
    assert info["apps"] == len(C.APP_CATALOG) > 0
    assert info["link_only"] == len(C.MANUAL_ONLY_APPS)
    assert info["categories"] == len(C.CATEGORY_ORDER)


def test_line_names_it_bundled_and_dates_it():
    line = C.provenance_line()
    assert line.startswith("Bundled list")
    assert "%d apps" % len(C.APP_CATALOG) in line
    assert "16 Sep 2026" in line          # CATALOG_CHECKED, human-readable
    assert len(line) <= 90                # stays one line beside the buttons


def test_checked_date_matches_the_newest_changelog_round():
    """The date is only worth printing if it moves with the data: every
    curation round above logs the day it was verified live, so the newest one
    and CATALOG_CHECKED must be the same day. Adding entries without updating
    the stamp fails here instead of shipping a caption that lies."""
    rounds = re.findall(r"^(\d{4}-\d{2}-\d{2})\b", C.__doc__ or "", re.M)
    assert rounds, "no dated curation rounds found in the catalog docstring"
    assert C.CATALOG_CHECKED == max(rounds)


class _StubApp:
    def log(self, *_a, **_k):
        pass


def test_install_tab_shows_the_line(monkeypatch):
    try:
        root = tk.Tk()
    except tk.TclError as exc:                 # pragma: no cover - no display
        pytest.skip("no display for Tk: %s" % exc)
    # The constructor kicks off read-only winget scans for the update count;
    # this test only cares about the caption.
    from app.ui.tabs import installtab as I
    monkeypatch.setattr(I.InstallTab, "_refresh_install_count", lambda self: None)
    try:
        tab = I.InstallTab(root, _StubApp())
        root.update_idletasks()
        assert tab._prov_lbl.cget("text") == C.provenance_line()
        assert tab._prov_lbl.winfo_reqwidth() <= 520   # fits the 640 floor
    finally:
        root.destroy()
