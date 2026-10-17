"""Tests for the themed info / warning / error notices (design audit C-005).

The three prompts share one implementation (`_themed_notice`) and differ only
in accent colour and the native messagebox used as a last-resort fallback.
None of this needs a display: the wrappers are checked by intercepting the
shared helper, and the fallback is checked with ThemedModal forced absent.
"""
from __future__ import annotations

from app.ui import base


def _record_notice(monkeypatch):
    """Replace `_themed_notice` with a recorder and return the capture dict."""
    calls = {}

    def fake_notice(parent, title, message, accent, ok_text, fallback):
        calls.update(parent=parent, title=title, message=message,
                     accent=accent, ok_text=ok_text, fallback=fallback)

    monkeypatch.setattr(base, "_themed_notice", fake_notice)
    return calls


def test_showerror_is_red_and_falls_back_to_native_error(monkeypatch):
    calls = _record_notice(monkeypatch)
    base._themed_showerror("PARENT", "Boom", "Details")
    assert calls["parent"] == "PARENT"
    assert (calls["title"], calls["message"]) == ("Boom", "Details")
    assert calls["accent"] == base.COLORS["accent_red"]
    assert calls["fallback"] is base.messagebox.showerror
    assert calls["ok_text"] == "OK"


def test_showwarning_is_yellow_and_falls_back_to_native_warning(monkeypatch):
    calls = _record_notice(monkeypatch)
    base._themed_showwarning("PARENT", "Careful", "Details")
    assert calls["accent"] == base.COLORS["accent_yellow"]
    assert calls["fallback"] is base.messagebox.showwarning


def test_showinfo_is_green_and_falls_back_to_native_info(monkeypatch):
    calls = _record_notice(monkeypatch)
    base._themed_showinfo("PARENT", "Hello", "Details")
    assert calls["accent"] == base.COLORS["accent_green"]
    assert calls["fallback"] is base.messagebox.showinfo


def test_explicit_accent_and_ok_text_override_the_defaults(monkeypatch):
    calls = _record_notice(monkeypatch)
    base._themed_showerror("PARENT", "Boom", "Details",
                           accent="#123456", ok_text="Dismiss")
    assert calls["accent"] == "#123456"
    assert calls["ok_text"] == "Dismiss"


def test_notice_uses_native_fallback_when_themed_modal_is_unavailable(monkeypatch):
    # No ThemedModal (headless) -> the native call must still fire, so an
    # error is never silently swallowed.
    monkeypatch.setattr(base, "ThemedModal", None)
    seen = []
    monkeypatch.setattr(base.messagebox, "showerror",
                        lambda title, message: seen.append((title, message)))
    base._themed_showerror("PARENT", "Boom", "Details")
    assert seen == [("Boom", "Details")]


def test_notice_swallows_a_failing_fallback(monkeypatch):
    # A broken native messagebox must not raise out of the notice helper.
    monkeypatch.setattr(base, "ThemedModal", None)

    def _boom(title, message):
        raise RuntimeError("no display")

    monkeypatch.setattr(base.messagebox, "showerror", _boom)
    base._themed_showerror("PARENT", "Boom", "Details")  # must not raise
