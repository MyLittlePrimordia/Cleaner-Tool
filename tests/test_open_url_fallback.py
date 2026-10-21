"""The 'could not open link' fallback must actually show an error.

`_open_url` used to be a @classmethod whose failure path called
`_themed_showerror(self, ...)` — `self` is undefined there, so the last-resort
handler raised NameError instead of showing the dialog (and the link appeared
to do nothing, the exact bug it was written to fix).
"""
from __future__ import annotations

import pytest

from tests._tkhelpers import make_root

tk = pytest.importorskip("tkinter")


def test_open_url_failure_reports_the_error(monkeypatch):
    import app.ui.tabs.installtab as installtab

    root = make_root()
    try:
        root.withdraw()
        tab = object.__new__(installtab.InstallTab)

        monkeypatch.setattr(tab, "_resolve_default_browser_cmd",
                            lambda: None, raising=False)
        monkeypatch.setattr(installtab.os, "startfile",
                            lambda _u: (_ for _ in ()).throw(OSError("no handler")),
                            raising=False)

        class _Boom:
            def __init__(self, *a, **k):
                raise OSError("cannot launch")

        monkeypatch.setattr(installtab.subprocess, "Popen", _Boom)

        shown = []
        monkeypatch.setattr(installtab, "_themed_showerror",
                            lambda *a, **k: shown.append(a))

        tab._open_url("https://example.com")

        assert shown, "a failed URL open must surface an error dialog"
        # args are (parent, title, message); don't repr the Tk parent.
        assert "example.com" in " ".join(str(x) for x in shown[0][1:])
    finally:
        root.destroy()
