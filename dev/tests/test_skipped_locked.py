"""U-007: a clean that freed less than the estimate must say why.

Locked files were already counted per folder in the log. The gap was the run
summary: it reported a number smaller than the estimate with no explanation,
which reads as the tool failing rather than as a consequence of running apps.
"""
from __future__ import annotations

import sys

import pytest

from app.ui.app import _run_summary_lines
from app.utils import TaskContext

WINDOWS = sys.platform.startswith("win")


def _lines(**over):
    base = dict(tab_name="Clean", mode="run", cancelled=False, crashed=None,
                completed=4, skipped_n=0, failed=0, attempted=4,
                reclaimed_bytes=0, moved_bytes=0, needs_reboot=False)
    base.update(over)
    return _run_summary_lines(**base)


def test_the_summary_counts_locked_files_and_says_what_to_do():
    lines = _lines(locked_files=148)
    line = next(x for x in lines if "locked" in x)
    assert "148 file(s)" in line
    assert "Close them and run again" in line
    assert "log names each folder" in line


def test_a_run_with_no_locked_files_does_not_add_the_line():
    assert not any("locked" in x for x in _lines())


def test_derive_keeps_the_locked_total():
    """A task that re-wraps the context must not lose the count.

    `derive()` exists because rebuilding the dataclass dropped fields before
    (notes, dry_run). This is the same trap, so it is pinned the same way.
    """
    ctx = TaskContext(log=lambda _m: None, set_status=lambda _m: None)
    ctx.skipped_locked = 7
    copy = ctx.derive(log=lambda _m: None)
    assert copy.skipped_locked == 7


@pytest.mark.skipif(not WINDOWS, reason="Windows share-mode locking")
def test_a_file_held_open_is_skipped_and_counted(tmp_path, monkeypatch):
    """The real path: a held file is left in place and increments the total."""
    import ctypes
    from ctypes import wintypes

    from app.delete_safety import clean_folder_contents

    folder = tmp_path / "cache"
    folder.mkdir()
    free = folder / "free.bin"
    held = folder / "held.bin"
    free.write_bytes(b"x" * 64)
    held.write_bytes(b"y" * 64)

    kernel32 = ctypes.windll.kernel32
    kernel32.CreateFileW.restype = wintypes.HANDLE
    kernel32.CreateFileW.argtypes = [
        wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
        wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE,
    ]
    GENERIC_READ, OPEN_EXISTING = 0x80000000, 3
    handle = kernel32.CreateFileW(str(held), GENERIC_READ, 0, None,
                                  OPEN_EXISTING, 0, None)
    if handle in (None, wintypes.HANDLE(-1).value):
        pytest.skip("could not take an exclusive handle")
    ctx = TaskContext(log=lambda _m: None, set_status=lambda _m: None)
    try:
        freed = clean_folder_contents(ctx, str(folder))
    finally:
        kernel32.CloseHandle(handle)
    assert ctx.skipped_locked == 1
    assert not free.exists(), "the unlocked file should have been removed"
    assert held.exists(), "the held file must be left alone"
    assert freed == 64, "only the file that was actually removed counts"
