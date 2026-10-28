"""H-005: re-wrapping a TaskContext must not drop its data fields.

The Install tab re-wraps the context so winget's progress lines also reach the
status strip. That wrapper used to rebuild the dataclass with only the three
callbacks, which quietly discarded `dry_run`, `quarantine` and `notes` - so the
package names a localized update run collects (the whole point of that list)
went into a list the caller never sees.
"""
from __future__ import annotations

from app.tasks.install_tasks import _live_status_ctx
from app.utils import TaskContext


class _Quarantine:
    pass


def _ctx(**kw):
    return TaskContext(log=lambda m: None, set_status=lambda m: None, **kw)


def test_derive_replaces_the_callbacks():
    seen = []
    base = _ctx(cancelled=lambda: True)
    child = base.derive(log=seen.append, set_status=lambda m: None)
    child.log("hello")
    assert seen == ["hello"]
    assert child.cancelled() is True


def test_derive_shares_the_notes_list():
    base = _ctx()
    child = base.derive(log=lambda m: None)
    child.notes.append("Notepad++")
    assert base.notes == ["Notepad++"]


def test_derive_keeps_dry_run_and_quarantine():
    q = _Quarantine()
    base = _ctx(dry_run=True, quarantine=q)
    child = base.derive(set_status=lambda m: None)
    assert child.dry_run is True
    assert child.quarantine is q


def test_the_live_status_wrapper_keeps_everything_but_the_callbacks():
    logs, statuses = [], []
    q = _Quarantine()
    base = _ctx(dry_run=True, quarantine=q)
    base.log = logs.append
    base.set_status = statuses.append

    wrapped = _live_status_ctx(base)
    wrapped.notes.append("7-Zip")
    wrapped.log("(1/2) Downloading https://example.test/7z.exe")

    assert base.notes == ["7-Zip"]
    assert wrapped.dry_run is True and wrapped.quarantine is q
    assert logs and logs[0].startswith("(1/2) Downloading")
    assert statuses and "7-Zip" not in " ".join(statuses)
