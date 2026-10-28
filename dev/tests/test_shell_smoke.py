"""Smoke test: the application shell builds and every page opens.

`test_tool_open_smoke` proves a tool WINDOW opens. Nothing proved the window:
`Application(root)` is only exercised by a human launching the exe, so a page
whose constructor raises - a renamed theme token, a widget built against a
parent that is not there, a module that stopped importing - would be logged as
"could not build the X page" and shipped. That is precisely the shape of the
report this file exists to prevent: "nothing in the tools tab opens".

Two contracts are pinned here, and one is about time rather than widgets:

  * Tools, Drivers and Settings are LAZY (AUDIT TL-013). They are factories
    until first visit, because building 19 cards, walking the driver folders
    and probing schtasks before the first paint is what made the window take
    seconds to appear. A page that starts being built eagerly is a startup
    regression that no assertion about tabs would otherwise notice.

  * A page that fails to build must not take the app down: `_ensure_tab` logs
    it and `_show_tab` says "Could not open the X page." while the previous
    page stays up. The planted case below proves that path actually runs.

Deliberately NOT exercised: the idle pre-warm chain (it builds the Install
catalog in `after()` slices ~900 ms after launch) and the catalog builder
itself. A smoke test that waits for those would be measuring winget and the
catalog, not the shell - both have their own tests. Cost: ~3.5 s.
"""
from __future__ import annotations

import pytest

tk = pytest.importorskip("tkinter")

from _tkhelpers import make_root  # noqa: E402

from app.tab_presets import TAB_NAMES  # noqa: E402

#: Built eagerly at launch: the first paint needs them.
_EAGER = ("Clean", "Repair", "Tweak", "Health", "Install")

#: Factories until the user visits them (AUDIT TL-013).
_LAZY = ("Tools", "Drivers", "Settings")


@pytest.fixture()
def shell(monkeypatch):
    """A real `Application` on a withdrawn root, torn down after the test.

    `is_admin` is forced because the app opens either the full shell or the
    Admin Gate depending on it, and the gate has its own tests.

    Three background starters are stubbed out, none of which is about page
    construction: the idle pre-warm chain and the Install catalog builder
    (both `after()`-driven, and the catalog shells out), the Install tab's
    warm scans (two daemon threads around `winget`), and the single-instance
    watcher (a thread blocked on a kernel event until the process exits).
    Leaving the last two running is what leaked a Tcl interpreter into another
    test's teardown - the warning named an unrelated file, and the fix is not
    to wait longer but to not start work this test does not examine.
    """
    import app.single_instance as single_instance
    import app.ui.app as appmod
    from app.ui.tabs.installtab import InstallTab

    monkeypatch.setattr(appmod, "is_admin", lambda: True)
    monkeypatch.setattr(appmod.Application, "_schedule_idle_prewarm",
                        lambda self: None)
    monkeypatch.setattr(InstallTab, "_start_catalog_build", lambda self: None)
    monkeypatch.setattr(InstallTab, "warm_scans", lambda self: None)
    monkeypatch.setattr(single_instance, "poll_enabled", lambda: False)

    root = make_root()
    root.withdraw()
    application = appmod.Application(root)
    try:
        yield root, application
    finally:
        # Shut down the way Quit does, not by dropping the root: the tray, disk
        # monitor, pilot watcher and the worker->UI bridge all hold timers or
        # threads, and leaving them behind is how a GUI suite turns flaky on a
        # slower runner. `_on_close` destroys the root itself.
        try:
            application._on_close(force_quit=True)
        except Exception:
            pass
        try:
            root.destroy()
        except Exception:
            pass


def test_the_shell_opens_on_clean_with_only_its_eager_pages(shell):
    _root, application = shell

    assert application.active_tab == "Clean"
    assert set(application.tabs) == set(_EAGER), (
        "the lazy pages are being built before the first visit: %s"
        % sorted(set(application.tabs) - set(_EAGER)))
    assert application._switch_labels == list(TAB_NAMES), (
        "the navigation does not list every page")


def test_every_page_builds_when_it_is_visited(shell):
    root, application = shell

    for name in TAB_NAMES:
        application._switch_to(name)
        root.update()
        assert application.active_tab == name, (
            "switching to %s left the shell on %s"
            % (name, application.active_tab))
        assert application._appbar_title.cget("text") == name, (
            "the app bar still names %r after switching to %s"
            % (application._appbar_title.cget("text"), name))
        assert name in application.tabs, (
            "the %s page did not build - see the log line above" % name)

    assert set(application.tabs) == set(TAB_NAMES)


def test_a_page_that_cannot_build_is_reported_not_crashed(shell):
    root, application = shell
    assert "Settings" not in application.tabs

    def boom():
        raise RuntimeError("planted page failure")

    application._tab_factories["Settings"] = boom
    application._switch_to("Settings")
    root.update()

    assert "Settings" not in application.tabs
    assert application.active_tab == "Clean", (
        "a failed page switch moved the shell anyway")
    assert "Could not open the Settings page." in application.status_lbl.cget(
        "text")
