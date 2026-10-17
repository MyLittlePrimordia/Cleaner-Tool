"""Phase 1 regression tests (M-1..M-4 + snapshot-helper adoption).

Unit tests with boundaries monkeypatched; they never touch the real machine.
Each fails on the pre-fix code and passes on the fixed code.
"""
from __future__ import annotations

from app.utils import TaskContext


def _ctx(logs=None) -> TaskContext:
    if logs is None:
        logs = []
    return TaskContext(log=logs.append, set_status=lambda _s: None)


# --------------------------------------------------------------------------- #
# M-1 — keep_candidate must use the PATH length (shortest path wins)
# --------------------------------------------------------------------------- #

def test_keep_candidate_prefers_shortest_path():
    from app.dupes import keep_candidate

    copies = [
        {"path": r"C:\Games\SomeGame\bin\original.exe", "mtime": 1000},  # old + long
        {"path": r"C:\dl\x.exe", "mtime": 5000},                         # new + short
    ]
    idx, reason = keep_candidate(copies)
    assert copies[idx]["path"] == r"C:\dl\x.exe", "shortest path must win"
    assert "shortest" in reason


def test_keep_candidate_tie_breaks_on_oldest():
    from app.dupes import keep_candidate

    copies = [
        {"path": r"C:\aa\x.exe", "mtime": 5000},
        {"path": r"C:\bb\y.exe", "mtime": 1000},   # same length, older
    ]
    assert len(copies[0]["path"]) == len(copies[1]["path"])
    idx, _reason = keep_candidate(copies)
    assert copies[idx]["path"] == r"C:\bb\y.exe", "among equal-length paths, oldest wins"


# --------------------------------------------------------------------------- #
# M-4 — footer detection must not eat an app whose name starts with "No "
# --------------------------------------------------------------------------- #

def test_footer_detects_real_footers():
    from app.tasks.install_tasks import _is_upgrade_footer_line

    assert _is_upgrade_footer_line("no installed package found matching input criteria.")
    assert _is_upgrade_footer_line("2 upgrades available.")
    assert _is_upgrade_footer_line("no applicable update found.")
    assert _is_upgrade_footer_line("the following packages have an update:")


def test_footer_does_not_eat_app_named_no():
    from app.tasks.install_tasks import _is_upgrade_footer_line

    assert not _is_upgrade_footer_line("no man's sky 1.0 winget")


# --------------------------------------------------------------------------- #
# M-3 — _winget_silent must pass --exact
# --------------------------------------------------------------------------- #

def test_winget_silent_uses_exact(monkeypatch):
    import app.downloader as d

    captured: dict = {}
    monkeypatch.setattr(d, "resolve_exe", lambda name: name)
    monkeypatch.setattr(d, "run_cmd", lambda ctx, cmd, **k: captured.update(cmd=cmd) or 0)
    monkeypatch.setattr(d, "_norm_winget_rc", lambda rc: rc)

    d._winget_silent(_ctx(), "Microsoft.VCRedist.2015+.x64")

    assert "--exact" in captured["cmd"]


# --------------------------------------------------------------------------- #
# M-2 — Store apps get badged (product id is absent from `winget list`)
# --------------------------------------------------------------------------- #

def test_get_installed_ids_badges_store_apps(monkeypatch):
    import subprocess
    import app.tasks.install_tasks as it

    fake_out = (
        "Name                     Id                                              Version\n"
        "WhatsApp                 MSIX\\5319275A.WhatsAppDesktop_2.0_x64__abc     2.0\n"
        "Some App                 Vendor.SomeApp                                  1.0\n"
    )

    class _R:
        stdout = fake_out

    monkeypatch.setattr(subprocess, "run", lambda *a, **k: _R())
    monkeypatch.setattr(it, "resolve_exe", lambda name: name)
    monkeypatch.setattr(it, "_installed_package_names", lambda: None)
    it._INSTALLED_CACHE[0] = None

    ids = it.get_installed_ids(refresh=True)

    assert ids is not None
    assert "9nksqgp7f2nh" in ids, "WhatsApp (Store id) must be badged as installed"


# --------------------------------------------------------------------------- #
# M-6 / M-8 — Undo-integrity tweaks now snapshot before writing
# --------------------------------------------------------------------------- #

def test_shader_cache_uses_snapshot_helper(monkeypatch):
    import app.utils as u
    import app.tasks.tweak_tasks as t

    # Pretend the NVIDIA key exists so the skip guard doesn't fire.
    monkeypatch.setattr(u, "reg_get_value", lambda ctx, hive, path, name: 1)

    captured: dict = {}
    monkeypatch.setattr(
        t, "_regn_apply",
        lambda ctx, tid, writes, error="": captured.update(tid=tid, writes=writes),
    )

    t.apply_shader_cache_10gb(_ctx())

    assert captured["tid"] == "shader_cache_10gb"
    assert {w[2] for w in captured["writes"]} == {"EnableGR535", "RMShaderCacheSize"}


def test_ssd_last_access_uses_snapshot_helper(monkeypatch):
    import app.tasks.tweak_tasks as t

    monkeypatch.setattr(t, "_has_ssd", lambda: True)

    captured: dict = {}
    monkeypatch.setattr(
        t, "_regn_apply",
        lambda ctx, tid, writes, error="": captured.update(tid=tid),
    )

    t.apply_ssd_last_access(_ctx())

    assert captured["tid"] == "ssd_last_access"
