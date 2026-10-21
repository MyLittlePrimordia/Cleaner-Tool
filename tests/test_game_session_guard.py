"""2026-10 audit: no game-cache task may touch a live session.

Deleting a shader/engine cache under a running game is the one realistic way
this app can crash a session, and Deep Clean's thumbnail task must never kill
the shell. These tests pin both behaviours.
"""

import pytest

import app.tasks.game_tasks as gt
from app.utils import TaskContext, TaskSkipped


def _ctx(log):
    return TaskContext(log=log.append, set_status=lambda _s: None)


GUARDED = [
    (gt.clean_gamer_launchers, "launcher caches"),
    (gt.clean_game_files, "game caches"),
    (gt.clean_gpu_shader_caches, "shader caches"),
    (gt.clean_game_captures, "game captures"),
]


@pytest.mark.parametrize("func,label", GUARDED)
def test_running_game_skips_task(monkeypatch, func, label):
    monkeypatch.setattr(gt, "_session_processes",
                        lambda: (["eldenring.exe"], []))
    log = []
    with pytest.raises(TaskSkipped) as exc:
        func(_ctx(log))
    assert "eldenring.exe" in str(exc.value)
    assert any("skipping" in line and "eldenring.exe" in line for line in log)


@pytest.mark.parametrize("func,label", GUARDED)
def test_running_launcher_skips_task(monkeypatch, func, label):
    monkeypatch.setattr(gt, "_session_processes",
                        lambda: ([], ["steam.exe"]))
    with pytest.raises(TaskSkipped):
        func(_ctx([]))


@pytest.mark.parametrize("func,label", GUARDED)
def test_failed_probe_skips_instead_of_cleaning(monkeypatch, func, label):
    """An unanswerable probe must never read as 'nothing is running'."""
    monkeypatch.setattr(gt, "_session_processes", lambda: None)
    log = []
    with pytest.raises(TaskSkipped):
        func(_ctx(log))
    assert any("Could not list running processes" in line for line in log)


def test_dry_run_is_not_blocked_by_a_running_game(monkeypatch):
    """Measure-only passes delete nothing, so they must still estimate."""
    monkeypatch.setattr(gt, "_session_processes",
                        lambda: (["eldenring.exe"], []))
    monkeypatch.setattr(gt, "_clean_many", lambda ctx, folders, label: 0)
    ctx = _ctx([])
    ctx.dry_run = True
    # Cleanest observable: the guard itself returns without raising.
    gt._skip_if_session_running(ctx, "shader caches")


def test_session_processes_classifies_games_and_launchers(monkeypatch):
    import app.session_pilot as sp
    monkeypatch.setattr(
        sp, "default_get_processes",
        lambda wanted=None, timeout=10: [
            ("eldenring.exe", ""), ("steam.exe", ""), ("notepad.exe", "")])
    games, launchers = gt._session_processes()
    assert games == ["eldenring.exe"]
    assert launchers == ["steam.exe"]


# --------------------------------------------------------------------------- #
# B9 — thumbnail/icon cache clean leaves the shell alone
# --------------------------------------------------------------------------- #

def test_thumbnail_cache_never_kills_explorer(monkeypatch, tmp_path):
    import app.tasks.clean_tasks as ct

    killed = []

    def _fake_run_cmd(ctx, cmd, **kw):
        killed.append(cmd)
        return 0

    def _fake_restart(ctx):
        killed.append("restart")
        return True

    monkeypatch.setattr(ct, "run_cmd", _fake_run_cmd)
    monkeypatch.setattr(ct, "restart_explorer", _fake_restart, raising=False)
    monkeypatch.setattr(ct, "_LOCALAPPDATA", str(tmp_path), raising=False)
    monkeypatch.setattr(ct, "clean_folder_contents",
                        lambda ctx, path, **kw: 1234)

    log = []
    freed = ct.clean_thumbnail_icon_cache(_ctx(log))

    assert freed == 1234
    assert killed == [], "Explorer must not be killed or restarted by a clean task"


def test_thumbnail_cache_reports_locked_files(monkeypatch, tmp_path):
    import app.tasks.clean_tasks as ct

    (tmp_path / "Microsoft" / "Windows" / "Explorer").mkdir(parents=True)
    (tmp_path / "Microsoft" / "Windows" / "Explorer" / "thumbcache_96.db").write_bytes(b"x")
    monkeypatch.setattr(ct, "_LOCALAPPDATA", str(tmp_path), raising=False)
    monkeypatch.setattr(ct, "clean_folder_contents", lambda ctx, path, **kw: 0)

    log = []
    ct.clean_thumbnail_icon_cache(_ctx(log))
    assert any("locked by the running shell" in line for line in log)
