"""The 'could not open link' fallback must actually show an error.

`_open_url` used to be a @classmethod whose failure path called
`_themed_showerror(self, ...)` — `self` is undefined there, so the last-resort
handler raised NameError instead of showing the dialog (and the link appeared
to do nothing, the exact bug it was written to fix).

The route ladder moved to `app/openlink.py` when the "Install failed" dialog
started offering the same download links (U-001); these tests patch the leaf
now, and still assert the Install tab reports a failure to the user.
"""
from __future__ import annotations

import pytest

from dev.tests._tkhelpers import make_root

tk = pytest.importorskip("tkinter")


def _break_every_route(monkeypatch):
    import app.openlink as openlink

    monkeypatch.setattr(openlink, "_resolve_default_browser_cmd",
                        lambda: None)
    monkeypatch.setattr(openlink.os, "startfile",
                        lambda _u: (_ for _ in ()).throw(
                            OSError("no handler")),
                        raising=False)

    class _Boom:
        def __init__(self, *a, **k):
            raise OSError("cannot launch")

    monkeypatch.setattr(openlink.subprocess, "Popen", _Boom)


def test_open_url_failure_reports_the_error(monkeypatch):
    import app.ui.tabs.installtab as installtab

    root = make_root()
    try:
        root.withdraw()
        tab = object.__new__(installtab.InstallTab)
        _break_every_route(monkeypatch)

        shown = []
        monkeypatch.setattr(installtab, "_themed_showerror",
                            lambda *a, **k: shown.append(a))

        tab._open_url("https://example.com")

        assert shown, "a failed URL open must surface an error dialog"
        # args are (parent, title, message); don't repr the Tk parent.
        assert "example.com" in " ".join(str(x) for x in shown[0][1:])
    finally:
        root.destroy()


def test_the_shared_opener_refuses_a_non_web_link(monkeypatch):
    """Every route ends in the shell, so a file:/javascript: URL must never
    reach it — the refusal lives in the shared opener now."""
    import app.openlink as openlink

    launched = []
    monkeypatch.setattr(openlink.subprocess, "Popen",
                        lambda *a, **k: launched.append(a))
    monkeypatch.setattr(openlink.os, "startfile",
                        lambda u: launched.append(u), raising=False)

    seen = []
    assert openlink.open_web_url("file:///C:/Windows",
                                 show_error=seen.append) is False
    assert not launched
    assert seen and "not a web address" in seen[0]


def test_the_shared_opener_uses_the_browser_route_when_it_can(monkeypatch):
    import app.openlink as openlink

    started = []

    class _Popen:
        def __init__(self, argv, **kw):
            started.append(list(argv))

    monkeypatch.setattr(openlink, "_resolve_default_browser_cmd",
                        lambda: ["C:/browser.exe"])
    monkeypatch.setattr(openlink.subprocess, "Popen", _Popen)

    assert openlink.open_web_url("https://example.com") is True
    assert started == [["C:/browser.exe", "https://example.com"]]
