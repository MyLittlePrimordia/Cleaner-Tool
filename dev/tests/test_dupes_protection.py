"""R-010: the duplicates never-delete list is enforced on both paths.

`removable_from` decides what the dialog PROMISES and `recycle` decides what
is actually handed to the shell. The guarantee is that the second one refuses
on its own, because a UI-only filter is one rendering bug away from deleting a
file the user marked never-delete.
"""
from __future__ import annotations

import os

import pytest

from app import dupes


def _group(*paths, keep=0):
    """A scorecard-shaped group: one copy is kept, the rest are candidates."""
    return {"copies": [{"path": p, "keep": i == keep} for i, p in enumerate(paths)],
            "keep": paths[keep]}


@pytest.fixture
def recycled(monkeypatch):
    """Record every path that reaches the shell, and nothing else."""
    calls = []
    monkeypatch.setattr(dupes, "IS_WINDOWS", True)
    monkeypatch.setattr(dupes, "_recycle_one", lambda p: calls.append(p) or True)
    return calls


def test_removable_from_hides_every_protected_shape(tmp_path, monkeypatch):
    folder = tmp_path / "Photos"
    folder.mkdir()
    inside = folder / "holiday.jpg"
    inside.write_bytes(b"x")
    exact = tmp_path / "keep.txt"
    exact.write_bytes(b"x")
    globbed = tmp_path / "taxes.pdf"
    globbed.write_bytes(b"x")
    free = tmp_path / "dup.bin"
    free.write_bytes(b"x")

    protected = [str(exact), str(folder), str(tmp_path / "*.pdf")]
    # `free` is the only unprotected copy, and it is the one the group does
    # not keep — so it is the only thing removal could promise.
    group = _group(str(free), str(inside), str(exact), str(globbed), keep=1)

    assert dupes.removable_from(group, protected) == [str(free)]


def test_recycle_refuses_a_protected_file_even_without_a_list(tmp_path,
                                                              recycled,
                                                              monkeypatch):
    """The stored list is loaded by `recycle` itself: enforcement must not
    depend on the caller remembering to pass it."""
    victim = tmp_path / "keep.txt"
    victim.write_bytes(b"x")
    monkeypatch.setattr(dupes, "load_protected", lambda: [str(victim)])

    result = dupes.recycle([str(victim)])

    assert recycled == []
    assert result["moved"] == 0
    assert result["protected"] == 1
    assert result["refused"] == [str(victim)]


def test_recycle_still_moves_the_other_files_in_the_batch(tmp_path, recycled):
    protected = tmp_path / "keep.txt"
    protected.write_bytes(b"x")
    free = tmp_path / "dup.bin"
    free.write_bytes(b"y")

    result = dupes.recycle([str(protected), str(free)],
                           protected=[str(protected)])

    assert recycled == [str(free)]
    assert result["moved"] == 1
    assert result["protected"] == 1
    assert result["ok"] is True


def test_a_promise_and_a_refusal_cannot_disagree(tmp_path, recycled):
    """Whatever `removable_from` hands back, `recycle` accepts — and it refuses
    exactly the rest. This is the pairing the dialog relies on."""
    protected = tmp_path / "keep.txt"
    protected.write_bytes(b"x")
    free = tmp_path / "dup.bin"
    free.write_bytes(b"y")
    group = _group(str(free), str(protected), keep=1)
    listed = [str(protected)]

    promised = dupes.removable_from(group, listed)
    result = dupes.recycle(promised, protected=listed)

    assert promised == [str(free)]
    assert recycled == promised
    assert result["moved"] == len(promised)
    assert result["protected"] == 0      # refused earlier, by the sampler


def test_a_path_protected_after_the_list_was_built_is_refused(tmp_path, recycled):
    """Case-folded, absolute matching means a differently-cased or relative
    entry still protects the file at the last moment."""
    victim = tmp_path / "Keep.TXT"
    victim.write_bytes(b"x")
    listed = [os.path.join(str(tmp_path).upper(), "keep.txt")]

    result = dupes.recycle([str(victim)], protected=listed)

    assert recycled == []
    assert result["protected"] == 1
