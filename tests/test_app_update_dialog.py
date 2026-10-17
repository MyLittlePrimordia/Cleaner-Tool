"""U-006 GUI: the app-update dialog renders the offer / ready / refused states
and routes its two actions. The update engine is stubbed, so nothing here
downloads, swaps or opens a browser.
"""
from __future__ import annotations

import pytest

tk = pytest.importorskip("tkinter")

from app import self_update
from app.ui.dialogs import AppUpdateDialog
from app.ui.dialogs._tool_page import ToolPage
from app.ui.dialogs.registry import DIALOGS


class _StubApp:
    _update_info = {
        "latest_version": "2.1.0",
        "current_version": "2.0.0",
        "download_url": ("https://github.com/MyLittlePrimordia/Cleaner-Tool/"
                         "releases/download/latest/Cleaner-Tool.exe"),
        "release_notes": "Fixes a crash and a slow scan.",
    }

    def __getattr__(self, name):
        return lambda *a, **k: None


def _build(monkeypatch, *, can=True, staged=""):
    monkeypatch.setattr(self_update, "can_self_update",
                        lambda exe_path="": ((True, "") if can
                                             else (False, "source checkout")))
    monkeypatch.setattr(self_update, "staged_ready", lambda: staged)
    root = tk.Tk()
    root.withdraw()
    return root, AppUpdateDialog(root, _StubApp())


def test_registered_on_the_shared_shell():
    assert issubclass(AppUpdateDialog, ToolPage)
    assert "app_update" in DIALOGS
    assert DIALOGS["app_update"].cls is AppUpdateDialog
    assert DIALOGS["app_update"].guard_busy is True


def test_offer_state_when_automatic_is_available(monkeypatch):
    root, dlg = _build(monkeypatch, can=True)
    try:
        assert dlg._state == "offer"
        assert "2.1.0" in dlg.banner._title_full
        assert dlg._auto_btn._text == "Update automatically"
        assert dlg._auto_btn._enabled is True
    finally:
        dlg.close()
        root.destroy()


def test_offer_state_disables_automatic_when_unavailable(monkeypatch):
    root, dlg = _build(monkeypatch, can=False)
    try:
        assert dlg._auto_btn._enabled is False
        texts = _walk_texts(dlg.content)
        assert any("source checkout" in t for t in texts), texts
    finally:
        dlg.close()
        root.destroy()


def test_ready_state_from_a_staged_build(monkeypatch):
    root, dlg = _build(monkeypatch, staged=r"C:\tmp\Cleaner-Tool.exe")
    try:
        assert dlg._state == "ready"
        assert "Ready to install" in dlg.banner._title_full
        assert dlg._auto_btn._text == "Restart to finish"
    finally:
        dlg.close()
        root.destroy()


def test_land_auto_failure_keeps_the_offer_and_explains(monkeypatch):
    root, dlg = _build(monkeypatch)
    try:
        dlg._land_auto({"ok": False, "error": "no checksum published"})
        assert dlg._state == "offer"
        assert "no checksum published" in dlg.status._raw
    finally:
        dlg.close()
        root.destroy()


def test_land_auto_success_moves_to_ready(monkeypatch):
    root, dlg = _build(monkeypatch, staged=r"C:\tmp\Cleaner-Tool.exe")
    try:
        dlg._land_auto({"ok": True, "error": ""})
        assert dlg._state == "ready"
        assert dlg._auto_btn._text == "Restart to finish"
    finally:
        dlg.close()
        root.destroy()


def test_open_browser_refuses_an_untrusted_link(monkeypatch):
    root, dlg = _build(monkeypatch)
    try:
        dlg._update["download_url"] = "http://evil.example/x.exe"
        dlg._open_browser()
        assert "nothing was opened" in dlg.status._raw
    finally:
        dlg.close()
        root.destroy()


def test_restart_now_schedules_the_swap_and_closes(monkeypatch):
    root, dlg = _build(monkeypatch, staged=r"C:\tmp\Cleaner-Tool.exe")
    seen = {}
    monkeypatch.setattr(self_update, "current_exe", lambda: r"C:\app\ct.exe")
    monkeypatch.setattr(
        self_update, "schedule_swap",
        lambda target, staged, **k: (seen.update(target=target, staged=staged),
                                     (True, "ok"))[1])
    try:
        dlg._restart_now()
        assert seen["target"] == r"C:\app\ct.exe"
        assert seen["staged"] == r"C:\tmp\Cleaner-Tool.exe"
    finally:
        try:
            dlg.close()
        except Exception:
            pass
        root.destroy()


def test_prepare_reports_an_unreachable_release(monkeypatch):
    root, dlg = _build(monkeypatch)
    try:
        monkeypatch.setattr("app.update_checker.fetch_latest_release_info",
                            lambda repo, timeout=20: None)
        res = dlg._prepare()
        assert res["ok"] is False
        assert "release page" in res["error"]
    finally:
        dlg.close()
        root.destroy()


def test_prepare_stages_the_verified_build(monkeypatch):
    from app import self_update as su
    root, dlg = _build(monkeypatch)
    digest = "ab" * 32
    release = {"assets": [{
        "name": "Cleaner-Tool.exe",
        "browser_download_url": ("https://github.com/MyLittlePrimordia/"
                                 "Cleaner-Tool/releases/download/latest/"
                                 "Cleaner-Tool.exe"),
        "digest": "sha256:" + digest, "size": 42}]}
    monkeypatch.setattr("app.update_checker.fetch_latest_release_info",
                        lambda repo, timeout=20: release)
    seen = {}

    def stage(url, sha, size, **kw):
        seen.update(url=url, sha=sha, size=size)
        return (True, "")

    monkeypatch.setattr(su, "stage_download", stage)
    try:
        assert dlg._prepare() == {"ok": True, "error": ""}
        assert seen["sha"] == digest and seen["size"] == 42
        assert seen["url"].endswith("Cleaner-Tool.exe")
    finally:
        dlg.close()
        root.destroy()


# --------------------------------------------------------------- small helper
def _walk_texts(widget):
    out = []
    try:
        text = widget.cget("text")
    except Exception:
        text = None
    if text:
        out.append(str(text))
    for child in widget.winfo_children():
        out.extend(_walk_texts(child))
    return out
