"""Phase 2.5 regression tests — the opt-in Quarantine bin.

Everything runs in pytest's tmp_path, so no real user file, no real registry
value and no real config file is touched. The clean-path tests call the real
`clean_folder_contents` on a temp tree.
"""
from __future__ import annotations

from app.quarantine import (
    Quarantine, empty, list_entries, restore_all, stats,
)
from app.utils import TaskContext, clean_folder_contents


def _ctx(path, quarantine=None, logs=None):
    return TaskContext(log=(logs.append if logs is not None else (lambda m: None)),
                       set_status=lambda m: None,
                       cancelled=lambda: False,
                       quarantine=quarantine)


# --------------------------------------------------------------------------- #
# store mechanics
# --------------------------------------------------------------------------- #

def test_stash_moves_file_and_records(tmp_path):
    src = tmp_path / "a.bin"
    src.write_bytes(b"x" * 100)
    q = Quarantine(root=str(tmp_path / "q"))
    assert q.stash(str(src)) is True
    assert not src.exists()               # moved, not copied
    assert q.moved_files == 1 and q.moved_bytes == 100
    assert stats(str(tmp_path / "q")) == (1, 100)
    assert len(list_entries(str(tmp_path / "q"))) == 1


def test_restore_all_puts_files_back(tmp_path):
    src = tmp_path / "sub" / "b.bin"
    src.parent.mkdir()
    src.write_bytes(b"y" * 42)
    q = Quarantine(root=str(tmp_path / "q"))
    q.stash(str(src))
    assert not src.exists()
    restored, failed = restore_all(str(tmp_path / "q"))
    assert (restored, failed) == (1, 0)
    assert src.read_bytes() == b"y" * 42
    assert stats(str(tmp_path / "q")) == (0, 0)


def test_restore_all_never_clobbers(tmp_path):
    src = tmp_path / "f.txt"
    src.write_text("original")
    q = Quarantine(root=str(tmp_path / "q"))
    q.stash(str(src))
    src.write_text("new file created in the meantime")
    restored, failed = restore_all(str(tmp_path / "q"))
    assert (restored, failed) == (0, 1)
    assert src.read_text() == "new file created in the meantime"


def test_size_cap_refuses_and_leaves_file(tmp_path):
    f = tmp_path / "big.bin"
    f.write_bytes(b"z" * 100)
    q = Quarantine(root=str(tmp_path / "q"), max_bytes=10)
    assert q.stash(str(f)) is False
    assert f.exists()
    assert q.refused == 1


def test_cap_counts_what_is_already_held(tmp_path):
    qroot = tmp_path / "q"
    f1 = tmp_path / "a.bin"
    f1.write_bytes(b"a" * 60)
    assert Quarantine(root=str(qroot), max_bytes=100).stash(str(f1)) is True
    f2 = tmp_path / "b.bin"
    f2.write_bytes(b"b" * 60)
    assert Quarantine(root=str(qroot), max_bytes=100).stash(str(f2)) is False
    assert f2.exists()


def test_empty_bin(tmp_path):
    src = tmp_path / "f.txt"
    src.write_text("hello")
    q = Quarantine(root=str(tmp_path / "q"))
    q.stash(str(src))
    n, total = empty(str(tmp_path / "q"))
    assert (n, total) == (1, 5)
    assert stats(str(tmp_path / "q")) == (0, 0)


def test_enabled_reflects_config(monkeypatch):
    import app.config_persist as cp
    from app import quarantine

    monkeypatch.setattr(cp, "load_config", lambda: {})
    assert quarantine.enabled() is False
    monkeypatch.setattr(cp, "load_config",
                        lambda: {"quarantine_enabled": True})
    assert quarantine.enabled() is True


# --------------------------------------------------------------------------- #
# clean funnel integration
# --------------------------------------------------------------------------- #

def test_clean_folder_contents_quarantines(tmp_path):
    src = tmp_path / "cleanme"
    src.mkdir()
    (src / "a.tmp").write_bytes(b"x" * 100)
    (src / "b.tmp").write_bytes(b"y" * 50)
    logs = []
    q = Quarantine(root=str(tmp_path / "q"))
    freed = clean_folder_contents(_ctx(src, quarantine=q, logs=logs), str(src))
    assert freed == 150
    assert not (src / "a.tmp").exists()
    assert not (src / "b.tmp").exists()
    assert stats(str(tmp_path / "q")) == (2, 150)
    # and it is honestly logged as moved, not reclaimed
    assert any("Quarantine bin" in m for m in logs)
    # the files really are recoverable
    restored, failed = restore_all(str(tmp_path / "q"))
    assert (restored, failed) == (2, 0)
    assert (src / "a.tmp").read_bytes() == b"x" * 100


def test_clean_folder_contents_deletes_when_bin_off(tmp_path):
    src = tmp_path / "cleanme"
    src.mkdir()
    (src / "a.tmp").write_bytes(b"x" * 100)
    freed = clean_folder_contents(_ctx(src), str(src))   # no quarantine
    assert freed == 100
    assert not (src / "a.tmp").exists()
    assert stats(str(tmp_path / "q")) == (0, 0)
