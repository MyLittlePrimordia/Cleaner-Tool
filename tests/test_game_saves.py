"""Game Save Manager: manifests and the root-confined restore.

A restore that writes the wrong file over the wrong path is worse than no
restore at all, so these tests are about refusals as much as successes.
"""

import json
import os
import zipfile

import pytest

import app.game_saves as gs


@pytest.fixture()
def roots(tmp_path):
    saved = tmp_path / "Saved Games"
    games = tmp_path / "My Games"
    (saved / "Elden Ring").mkdir(parents=True)
    (games / "Skyrim").mkdir(parents=True)
    (saved / "Elden Ring" / "slot1.sav").write_bytes(b"OLD-ELDEN")
    (games / "Skyrim" / "save1.ess").write_bytes(b"OLD-SKYRIM")
    return [(str(saved), "Saved Games"), (str(games), "My Games")]


@pytest.fixture()
def backup_dir(tmp_path, monkeypatch):
    folder = tmp_path / "backups"
    folder.mkdir()
    monkeypatch.setattr(gs, "backup_folder", lambda: str(folder))
    return folder


def _make_backup(roots, dest, mutate=None):
    """Write a valid archive mirroring backup_game_saves' format."""
    files = []
    for root, tag in roots:
        for base, _dirs, names in os.walk(root):
            for name in names:
                fp = os.path.join(base, name)
                files.append((fp, os.path.join(
                    tag, os.path.relpath(fp, root)).replace("\\", "/")))
    with zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED) as zf:
        for fp, arc in files:
            zf.write(fp, arc)
        gs.write_manifest(zf, mutate(files) if mutate else files, roots)
    return dest


def test_manifest_roundtrip_and_plan(roots, backup_dir, monkeypatch):
    monkeypatch.setattr(gs, "save_roots", lambda: roots)
    path = _make_backup(roots, str(backup_dir / "GameSavesBackup_x.zip"))
    man = gs.read_manifest(path)
    assert man and len(man["files"]) == 2
    plan = gs.plan_restore(path)
    assert plan["ok"], plan["error"]
    assert {os.path.basename(f["path"]) for f in plan["files"]} == {
        "slot1.sav", "save1.ess"}
    assert plan["skipped"] == []


def test_list_backups_marks_legacy_archives(roots, backup_dir, monkeypatch):
    monkeypatch.setattr(gs, "save_roots", lambda: roots)
    legacy = backup_dir / "GameSavesBackup_old.zip"
    with zipfile.ZipFile(legacy, "w") as zf:
        zf.writestr("Saved Games/x.sav", b"x")
    _make_backup(roots, str(backup_dir / "GameSavesBackup_new.zip"))
    rows = gs.list_backups()
    by_name = {r["name"]: r for r in rows}
    assert by_name["GameSavesBackup_new.zip"]["restorable"] is True
    assert by_name["GameSavesBackup_old.zip"]["restorable"] is False
    assert "manifest" in by_name["GameSavesBackup_old.zip"]["reason"].lower()


def test_restore_refuses_archive_without_manifest(backup_dir):
    legacy = backup_dir / "GameSavesBackup_old.zip"
    with zipfile.ZipFile(legacy, "w") as zf:
        zf.writestr("Saved Games/x.sav", b"x")
    res = gs.restore_backup(str(legacy))
    assert res["ok"] is False
    assert "manifest" in res["error"].lower()
    assert res["restored"] == 0


def test_restore_refuses_paths_outside_save_roots(roots, backup_dir,
                                                  monkeypatch, tmp_path):
    monkeypatch.setattr(gs, "save_roots", lambda: roots)
    outside = tmp_path / "not-saves" / "evil.sav"
    outside.parent.mkdir()
    outside.write_bytes(b"EVIL")
    path = str(backup_dir / "GameSavesBackup_evil.zip")
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("Saved Games/evil.sav", b"EVIL")
        gs.write_manifest(zf, [(str(outside), "Saved Games/evil.sav")], roots)

    plan = gs.plan_restore(path)
    assert plan["ok"] is True
    assert plan["files"] == []
    assert plan["skipped"] and "outside" in plan["skipped"][0]["why"]
    res = gs.restore_backup(path)
    assert res["ok"] is False and res["restored"] == 0
    assert not outside.exists() or outside.read_bytes() == b"EVIL"


def test_restore_refuses_traversing_arcnames(roots, backup_dir, monkeypatch):
    monkeypatch.setattr(gs, "save_roots", lambda: roots)
    target = os.path.join(roots[0][0], "Elden Ring", "slot1.sav")
    path = str(backup_dir / "GameSavesBackup_slip.zip")
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("Saved Games/ok.sav", b"x")
        gs.write_manifest(zf, [(target, "../../etc/passwd")], roots)
    plan = gs.plan_restore(path)
    assert plan["files"] == []
    assert plan["skipped"][0]["why"] == "bad archive entry"


def test_restore_writes_files_and_snapshots_current_versions(
        roots, backup_dir, monkeypatch):
    monkeypatch.setattr(gs, "save_roots", lambda: roots)
    path = str(backup_dir / "GameSavesBackup_x.zip")
    _make_backup(roots, path)
    # Change a save, then restore the archived version.
    live = os.path.join(roots[0][0], "Elden Ring", "slot1.sav")
    with open(live, "wb") as fh:
        fh.write(b"NEWER-PROGRESS")

    logs = []
    res = gs.restore_backup(path, log=logs.append)
    assert res["ok"] and res["restored"] == 2
    assert open(live, "rb").read() == b"OLD-ELDEN"
    # The overwritten version is recoverable from the pre-restore snapshot.
    assert res["snapshot"] and os.path.isfile(res["snapshot"])
    with zipfile.ZipFile(res["snapshot"]) as zf:
        names = zf.namelist()
        snap = [n for n in names if n.endswith("slot1.sav")][0]
        assert zf.read(snap) == b"NEWER-PROGRESS"


def test_locked_file_is_skipped_not_forced(roots, backup_dir, monkeypatch):
    monkeypatch.setattr(gs, "save_roots", lambda: roots)
    path = str(backup_dir / "GameSavesBackup_x.zip")
    _make_backup(roots, path)
    locked = os.path.join(roots[0][0], "Elden Ring", "slot1.sav")

    real_replace = os.replace

    def _replace(src, dst):
        if os.path.normcase(dst) == os.path.normcase(locked):
            raise PermissionError("held open by the game")
        return real_replace(src, dst)

    monkeypatch.setattr(gs.os, "replace", _replace)
    res = gs.restore_backup(path)
    assert res["skipped"] == 1
    assert res["restored"] == 1
    assert not os.path.exists(locked + ".cleanertool-restore")


def test_cancel_leaves_written_files_in_place(roots, backup_dir, monkeypatch):
    monkeypatch.setattr(gs, "save_roots", lambda: roots)
    path = str(backup_dir / "GameSavesBackup_x.zip")
    _make_backup(roots, path)
    res = gs.restore_backup(path, cancelled=lambda: True)
    assert res["cancelled"] is True and res["restored"] == 0


def test_dry_run_writes_nothing(roots, backup_dir, monkeypatch):
    monkeypatch.setattr(gs, "save_roots", lambda: roots)
    path = str(backup_dir / "GameSavesBackup_x.zip")
    _make_backup(roots, path)
    live = os.path.join(roots[1][0], "Skyrim", "save1.ess")
    with open(live, "wb") as fh:
        fh.write(b"CURRENT")
    res = gs.restore_backup(path, dry_run=True)
    assert res["restored"] == 0 and res["snapshot"] is None
    assert open(live, "rb").read() == b"CURRENT"


def test_backup_task_writes_restorable_manifest(roots, backup_dir, monkeypatch):
    """The Clean-tab backup and the restore path must agree on the format."""
    import app.tasks.game_tasks as gt
    from app.utils import TaskContext

    monkeypatch.setattr(gs, "save_roots", lambda: roots)
    monkeypatch.setattr(gt, "_gs", gs)
    monkeypatch.setattr(gt, "_game_saves_backup_dir", lambda: str(backup_dir))
    monkeypatch.setattr(gt, "_prune_game_saves_backups", lambda *a, **k: None)

    logs = []
    ctx = TaskContext(log=logs.append, set_status=lambda _s: None)
    gt.backup_game_saves(ctx)

    zips = [n for n in os.listdir(backup_dir) if n.endswith(".zip")]
    assert len(zips) == 1
    man = gs.read_manifest(os.path.join(backup_dir, zips[0]))
    assert man and man["version"] == gs.MANIFEST_VERSION
    # Normalize the roots for this test's temp paths, then restore for real.
    assert {r["tag"] for r in man["roots"]} == {"Saved Games", "My Games"}
    plan = gs.plan_restore(os.path.join(backup_dir, zips[0]))
    assert plan["ok"] and len(plan["files"]) == 2


def test_manifest_json_is_plain_data(backup_dir):
    path = str(backup_dir / "GameSavesBackup_x.zip")
    with zipfile.ZipFile(path, "w") as zf:
        gs.write_manifest(zf, [("C:/a/b.sav", "Saved Games/b.sav")], [])
        raw = zf.read(gs.MANIFEST_NAME)
    data = json.loads(raw.decode("utf-8"))
    assert data["files"][0]["arcname"] == "Saved Games/b.sav"
