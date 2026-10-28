"""R-009: the destructive funnel has its own module, and its guards still hold.

`app/utils.py` re-exports every name, so the split must not change which
function the cleaners call - the first test pins exactly that.
"""
from __future__ import annotations

from app.utils import TaskContext


def _ctx(**kw):
    return TaskContext(log=lambda _m: None, set_status=lambda _s: None,
                       cancelled=lambda: False, **kw)


def test_utils_reexports_the_funnel_and_its_guards():
    import app.delete_safety as ds
    import app.utils as utils

    assert utils.clean_folder_contents is ds.clean_folder_contents
    assert utils._is_reparse_point is ds._is_reparse_point
    assert utils._is_protected_root is ds._is_protected_root
    assert utils._own_runtime_roots is ds._own_runtime_roots


def test_a_dry_run_measures_without_deleting(tmp_path):
    from app.delete_safety import clean_folder_contents

    (tmp_path / "a.bin").write_bytes(b"x" * 1024)
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "b.bin").write_bytes(b"y" * 512)

    freed = clean_folder_contents(_ctx(dry_run=True), str(tmp_path))

    assert freed == 1536
    assert (tmp_path / "a.bin").exists()
    assert (tmp_path / "sub" / "b.bin").exists()


def test_a_real_run_deletes_and_counts(tmp_path):
    from app.delete_safety import clean_folder_contents

    (tmp_path / "a.bin").write_bytes(b"x" * 64)
    freed = clean_folder_contents(_ctx(), str(tmp_path))

    assert freed == 64
    assert not (tmp_path / "a.bin").exists()


def test_a_protected_root_is_refused(monkeypatch, tmp_path):
    """The environment roots are read at call time, so pointing USERPROFILE at
    a temp tree makes that tree a protected container - and the funnel must
    leave it alone even with a real (non-dry) run."""
    from app.delete_safety import clean_folder_contents

    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    victim = tmp_path / "keep.txt"
    victim.write_text("precious", encoding="utf-8")

    assert clean_folder_contents(_ctx(), str(tmp_path)) == 0
    assert victim.read_text(encoding="utf-8") == "precious"
