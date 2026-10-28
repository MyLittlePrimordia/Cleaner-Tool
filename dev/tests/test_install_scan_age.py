"""A-009: a badge count carries the time it was measured.

The green "Installed" ticks come from one winget read. The old summary line
("10 of your catalog apps are already installed") had no timestamp, so it read
as live state and then quietly disagreed with a machine where the user had
uninstalled something. Three things are pinned here:

  * the sentence names the count AND its age, and says "not checked yet"
    rather than inventing a number when the reading has not happened;
  * visiting the tab re-reads once the cached answer is past INSTALLED_TTL,
    and does NOT pay a winget call when it is fresh;
  * the re-read REPLACES the ticks, so a refresh cannot stack a second tick on
    a row (the bug a naive "paint again" would have shipped).
"""
from __future__ import annotations

import tkinter as tk
import types

from app.tasks import winget
from app.ui.tabs.installtab import InstallTab

from _tkhelpers import make_root


_UNSET = object()


def _fake(count=3, ids=_UNSET):
    fake = types.SimpleNamespace()
    fake._installed_ids = {"app.id"} if ids is _UNSET else ids
    fake._installed_catalog_count = lambda: count
    return fake


def _age(monkeypatch, seconds):
    monkeypatch.setattr(winget, "installed_scan_age", lambda: seconds)


# --------------------------------------------------------------- the sentence

def test_the_count_is_shown_with_its_age(monkeypatch):
    _age(monkeypatch, 10 * 60)
    text = InstallTab._scan_note(_fake(count=12))
    assert "12 catalog apps already on this PC" in text
    assert "checked 10 minutes ago" in text
    # The dot separator is the design system's, not a stray bullet.
    assert "\u00b7" in text


def test_one_app_is_not_twelve_apps(monkeypatch):
    _age(monkeypatch, 5)
    text = InstallTab._scan_note(_fake(count=1))
    assert "1 catalog app already" in text
    assert "1 catalog apps" not in text


def test_never_checked_does_not_invent_a_count(monkeypatch):
    _age(monkeypatch, -1)
    assert "not checked yet" in InstallTab._scan_note(_fake(count=12))
    # Measured, but no reading held: also not checked yet.
    _age(monkeypatch, 3)
    assert "not checked yet" in InstallTab._scan_note(_fake(ids=None))


def test_a_measured_zero_is_a_measurement(monkeypatch):
    """"Nothing matched" is a result; blank or "not checked" would lie."""
    _age(monkeypatch, 2)
    text = InstallTab._scan_note(_fake(count=0))
    assert "0 catalog apps already on this PC" in text
    assert "checked just now" in text


# ------------------------------------------------------------- re-reading it

def _show_fake():
    calls = {"warm": 0, "note": 0, "rescan": 0}

    def bump(key):
        calls[key] += 1

    fake = types.SimpleNamespace(
        _scans_started=True,
        warm_scans=lambda: bump("warm"),
        _update_scan_note=lambda: bump("note"),
        _rescan_installed=lambda: bump("rescan"),
    )
    return fake, calls


def test_a_stale_reading_is_re_read_on_visit(monkeypatch):
    _age(monkeypatch, winget.INSTALLED_TTL + 1)
    fake, calls = _show_fake()
    InstallTab.on_show(fake)
    assert calls == {"warm": 1, "note": 1, "rescan": 1}


def test_a_fresh_reading_costs_nothing_on_visit(monkeypatch):
    _age(monkeypatch, 30.0)
    fake, calls = _show_fake()
    InstallTab.on_show(fake)
    assert calls == {"warm": 1, "note": 1, "rescan": 0}


def test_the_first_visit_starts_the_scan_and_does_not_rescan(monkeypatch):
    """warm_scans starts the first read; that is not a stale re-read."""
    _age(monkeypatch, -1)
    fake, calls = _show_fake()
    InstallTab.on_show(fake)
    assert calls["rescan"] == 0


# ----------------------------------------------------------------- the badges

def test_a_re_read_replaces_the_tick_instead_of_stacking_one(monkeypatch):
    root = make_root()
    row = tk.Frame(root)
    try:
        _age(monkeypatch, 5.0)
        fake = types.SimpleNamespace(
            _installed_ids={"app.id"},
            _app_rows={"App.Id": row},
            _catalog_ready=True,
            _badges={},
            _pending_badges=None,
            app=types.SimpleNamespace(log=lambda *a, **k: None),
            _update_scan_note=lambda: None,
        )
        InstallTab._paint_installed_badges(fake, {"app.id"})
        InstallTab._paint_installed_badges(fake, {"app.id"})
        assert len(row.winfo_children()) == 1
        assert fake._badges != {}
    finally:
        root.destroy()
