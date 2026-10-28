"""A-008: a refused elevation changes the notice; it does not repeat itself.

The supplied machine log showed the same loop for an hour: "Elevation timed
out or was cancelled", and a notice still inviting the user to press a button
that had already failed. The behaviour shipped in the shared PermissionNotice,
but nothing pinned it -- so a refactor could have restored the dead end
silently. These tests are that pin, at both ends:

  * the notice itself (title, reason, retry label, why-disclosure, re-enable);
  * File Lock's landing handler, which must turn a cancelled UAC prompt into
    that state and must NOT do so after a successful elevation.
"""
from __future__ import annotations

import types

from app.ui.widgets.permission_notice import PermissionNotice

from _tkhelpers import make_root


def _notice(root, **kw):
    return PermissionNotice(root, on_action=lambda: None, **kw)


def test_a_refusal_is_a_different_state_not_a_repeat():
    root = make_root()
    try:
        notice = _notice(root)
        assert notice.title_lbl.cget("text") == "Needs administrator"
        assert notice.action()._text == "Restart as admin"

        notice.set_refused(why="a read-only scan of open handles")
        assert notice.title_lbl.cget("text") == "Administrator rights were refused"
        assert "did not grant" in notice.reason_lbl.cget("text")
        # The control that just failed must not look like one never tried.
        assert notice.action()._text == "Try again"
    finally:
        root.destroy()


def test_the_refusal_names_what_it_asked_for():
    root = make_root()
    try:
        notice = _notice(root)
        assert not notice._why_link.winfo_manager(), "hidden before a refusal"

        notice.set_refused(why="Get-CimInstance Win32_Process")
        assert notice._why_link.winfo_manager(), "disclosure appears with a why"
        notice._toggle_why()
        assert "Get-CimInstance Win32_Process" in notice._why_text.cget("text")
        notice._toggle_why()
        assert not notice._why_text.winfo_manager(), "and it collapses again"
    finally:
        root.destroy()


def test_a_refusal_without_an_action_still_reports_it():
    """A read-only tool has no button to retry; it still gets the truth."""
    root = make_root()
    try:
        notice = PermissionNotice(root)
        notice.set_refused()
        assert notice.title_lbl.cget("text") == "Administrator rights were refused"
        assert notice.action() is None
    finally:
        root.destroy()


# ------------------------------------------------------- File Lock's landing

def _land(data):
    """Drive FileLockDialog._land_elevated against a recording stand-in."""
    from app.ui.dialogs.file_lock_dialog import FileLockDialog

    calls = {"refused": [], "enabled": [], "status": []}

    class _Notice:
        def set_enabled(self, value):
            calls["enabled"].append(value)

        def set_refused(self, why=""):
            calls["refused"].append(why)

    class _Status:
        def stop(self, text, colour=None):
            calls["status"].append(text)

    fake = types.SimpleNamespace(
        _notice=_Notice(), status=_Status(), _elevated=False,
        _data={}, _limits={},
        set_busy=lambda _v: None, _paint=lambda: None,
        _apply_limits=lambda: None,
    )
    FileLockDialog._land_elevated(fake, data)
    return fake, calls


def test_a_cancelled_uac_prompt_becomes_the_refused_state():
    fake, calls = _land({"cancelled": True})
    assert calls["refused"], "the refusal must reach the notice"
    assert "open handles" in calls["refused"][0], calls["refused"][0]
    assert "refused" in calls["status"][-1].lower()
    assert fake._elevated is False
    # It was disabled while the prompt was open; a dead button is the bug.
    assert calls["enabled"] == [True]


def test_a_successful_elevation_does_not_claim_a_refusal():
    fake, calls = _land({"cancelled": False, "error": None})
    assert calls["refused"] == []
    assert fake._elevated is True


def test_an_error_is_not_reported_as_a_refusal():
    """An error is a different outcome; conflating them hides the cause."""
    fake, calls = _land({"cancelled": False, "error": "Restart Manager unavailable"})
    assert calls["refused"] == []
    assert "Restart Manager unavailable" in calls["status"][-1]
    assert fake._elevated is False
