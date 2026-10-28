"""R-005: typed \"what changed\" records, from the write layer to the card.

The point of the item is that the scorecard should not have to infer a run's
effect from a memory reading and the transcript: the code that performs each
write knows the prior and the new value, so it records them. These tests pin
the three hops:

  * the classifier, which turns a successfully-run command into a typed change
    (and refuses to guess at ``net start/stop`` or unrecognised tools);
  * the shared registry writers, which record a real before -> after;
  * `invoke_task`/`RunSummary`, which slice the run ledger per task, and the
    scorecard, which renders and copies the collapsed list.

No real registry, process or run is touched.
"""
from __future__ import annotations

import pytest

from app.changes import (
    KIND_BOOT, KIND_POWER, KIND_REGISTRY, KIND_SCHEDULED_TASK, KIND_SERVICE,
    Change, change_detail, classify_command, collapse, counts_text,
)
from app.runner.task_runner import RunSummary, invoke_task
from app.tasks import Task
from app.utils import TaskContext


def _ctx() -> TaskContext:
    return TaskContext(log=lambda _m: None, set_status=lambda _s: None)


# --------------------------------------------------------------------------- #
# command classification
# --------------------------------------------------------------------------- #

_CLASSIFY_CASES = [
    # (command, kind, target, after)
    (["sc", "config", "DiagTrack", "start=", "disabled"],
     KIND_SERVICE, "DiagTrack", "disabled"),
    ("sc config SysMain start= disabled",
     KIND_SERVICE, "SysMain", "disabled"),
    (["schtasks", "/change", "/tn", r"\Microsoft\Windows\Foo", "/disable"],
     KIND_SCHEDULED_TASK, r"\Microsoft\Windows\Foo", "disabled"),
    (["schtasks", "/change", "/tn", r"\Microsoft\Windows\Foo", "/enable"],
     KIND_SCHEDULED_TASK, r"\Microsoft\Windows\Foo", "enabled"),
    ("powercfg /setacvalueindex scheme_current sub_processor PROCTHROTTLEMAX 100",
     KIND_POWER, "sub_processor\\PROCTHROTTLEMAX", "100"),
    ("powercfg -change disk-timeout-ac 0",
     KIND_POWER, "disk-timeout-ac", "0"),
    ("powercfg /setactive scheme_current",
     KIND_POWER, "active plan", "scheme_current"),
    ("bcdedit /set disabledynamictick yes",
     KIND_BOOT, "disabledynamictick", "yes"),
    ("bcdedit /set {current} useplatformclock false",
     KIND_BOOT, "useplatformclock", "false"),
    ("bcdedit /deletevalue disabledynamictick",
     KIND_BOOT, "disabledynamictick", None),
]


@pytest.mark.parametrize("command,kind,target,after", _CLASSIFY_CASES)
def test_a_recognised_command_becomes_a_typed_change(command, kind, target, after):
    change = classify_command(command)
    assert change is not None, command
    assert (change.kind, change.target, change.after) == (kind, target, after)
    assert change.before_known is False, "a command states no prior value"


@pytest.mark.parametrize("command", [
    "net stop DiagTrack",                 # volatile; sc config carries the fact
    "net start DiagTrack",
    "winget upgrade --all",
    "sfc /scannow",
    "",
])
def test_unknown_and_volatile_commands_are_not_recorded(command):
    assert classify_command(command) is None


def test_a_failed_command_is_never_a_change():
    assert classify_command("sc config DiagTrack start= disabled", ok=False) is None


def test_only_a_whole_value_counts_as_unchanged():
    assert change_detail(Change(KIND_REGISTRY, "X", 1, 1)) == "1 \u2192 1"
    # 1 == True and 1 == "1" in Python; a type change is still a change.
    assert collapse([Change(KIND_REGISTRY, "X", 1, "1")]) != []
    assert collapse([Change(KIND_REGISTRY, "X", True, 1)]) != []


# --------------------------------------------------------------------------- #
# collapsing a run
# --------------------------------------------------------------------------- #

def test_the_same_target_keeps_the_first_prior_and_the_last_value():
    merged = collapse([
        Change(KIND_REGISTRY, "HKCU\\A\\Flag", 1, 0),
        Change(KIND_REGISTRY, "HKCU\\A\\Flag", 0, 2),
    ])
    assert merged == [Change(KIND_REGISTRY, "HKCU\\A\\Flag", 1, 2)]


def test_a_round_trip_that_ends_where_it_started_is_not_a_change():
    assert collapse([
        Change(KIND_REGISTRY, "HKCU\\A\\Flag", 1, 0),
        Change(KIND_REGISTRY, "HKCU\\A\\Flag", 0, 1),
    ]) == []


def test_different_targets_and_kinds_stay_separate():
    changes = [Change(KIND_REGISTRY, "HKCU\\A\\Flag", 1, 0),
               Change(KIND_SERVICE, "Flag", None, "disabled", before_known=False),
               Change(KIND_SCHEDULED_TASK, "Flag", None, "disabled", before_known=False)]
    assert len(collapse(changes)) == 3
    assert counts_text(changes) == ("1 registry value \u00b7 1 service \u00b7 "
                                    "1 scheduled task")


def test_detail_lines_say_removed_and_unknown_prior_honestly():
    assert change_detail(Change(KIND_REGISTRY, "X", 1, None)) == "removed"
    assert change_detail(Change(KIND_SERVICE, "X", None, "disabled",
                                before_known=False)) == "\u2192 disabled"
    assert change_detail(Change(KIND_REGISTRY, "X", 1, 0)) == "1 \u2192 0"


# --------------------------------------------------------------------------- #
# the shared write layer records
# --------------------------------------------------------------------------- #

class _FakeWinreg:
    KEY_WRITE = 0x20006
    KEY_READ = 0x20019
    KEY_SET_VALUE = 0x0002
    KEY_WOW64_64KEY = 0x0100
    REG_SZ = 1
    REG_DWORD = 4

    def CreateKeyEx(self, *_a, **_k):
        return object()

    def OpenKey(self, *_a, **_k):
        return object()

    def SetValueEx(self, *_a, **_k):
        return None

    def DeleteValue(self, *_a, **_k):
        return None

    def CloseKey(self, *_a, **_k):
        return None


@pytest.fixture
def fake_registry(monkeypatch):
    import app.utils as u
    monkeypatch.setattr(u, "winreg", _FakeWinreg())
    monkeypatch.setattr(u, "_HIVES", {"HKCU": object()})
    return u


def test_a_registry_write_records_the_real_prior(fake_registry, monkeypatch):
    monkeypatch.setattr(fake_registry, "reg_get_value_typed",
                        lambda *_a: (1, "REG_DWORD"))
    ctx = _ctx()
    fake_registry.reg_set_value_checked(ctx, "HKCU", "Software\\X", "Flag", 0)

    assert ctx.changes == [Change(KIND_REGISTRY, "HKCU\\Software\\X\\Flag",
                                  1, 0, type_name="REG_DWORD")]
    assert change_detail(ctx.changes[0]) == "1 \u2192 0"


def test_an_absent_value_records_not_set_rather_than_none(fake_registry, monkeypatch):
    monkeypatch.setattr(fake_registry, "reg_get_value_typed", lambda *_a: (None, None))
    ctx = _ctx()
    fake_registry.reg_set_value(ctx, "HKCU", "Software\\X", "Flag", 1)
    assert change_detail(ctx.changes[0]) == "not set \u2192 1"


def test_a_denied_prior_is_unknown_not_absent(fake_registry, monkeypatch):
    import app.utils as u
    monkeypatch.setattr(fake_registry, "reg_get_value_typed",
                        lambda *_a: (u._REG_DENIED, None))
    ctx = _ctx()
    fake_registry.reg_set_value(ctx, "HKCU", "Software\\X", "Flag", 1)
    assert ctx.changes[0].before_known is False
    assert change_detail(ctx.changes[0]) == "\u2192 1"


def test_a_delete_records_removal_but_not_a_no_op(fake_registry, monkeypatch):
    monkeypatch.setattr(fake_registry, "reg_get_value_typed",
                        lambda *_a: ("GB", "REG_SZ"))
    ctx = _ctx()
    assert fake_registry.reg_delete_value(ctx, "HKCU", "Software\\X", "Locale")
    assert ctx.changes == [Change(KIND_REGISTRY, "HKCU\\Software\\X\\Locale",
                                  "GB", None)]

    ctx = _ctx()
    monkeypatch.setattr(fake_registry, "reg_get_value_typed", lambda *_a: (None, None))
    assert fake_registry.reg_delete_value(ctx, "HKCU", "Software\\X", "Missing")
    assert ctx.changes == []


def test_a_write_that_changes_nothing_is_not_recorded(fake_registry, monkeypatch):
    monkeypatch.setattr(fake_registry, "reg_get_value_typed",
                        lambda *_a: (0, "REG_DWORD"))
    ctx = _ctx()
    fake_registry.reg_set_value(ctx, "HKCU", "Software\\X", "Flag", 0)
    assert ctx.changes == []


def test_a_context_without_a_ledger_pays_no_extra_read(fake_registry, monkeypatch):
    def _boom(*_a):
        raise AssertionError("no ledger -> no prior read")

    monkeypatch.setattr(fake_registry, "reg_get_value_typed", _boom)
    ctx = object.__new__(TaskContext)          # no `changes` attribute at all
    ctx.log = lambda _m: None
    ctx.set_status = lambda _s: None
    fake_registry.reg_set_value(ctx, "HKCU", "Software\\X", "Flag", 1)


# --------------------------------------------------------------------------- #
# the runner slices the ledger per task
# --------------------------------------------------------------------------- #

def _task(key, fn):
    return Task(key=key, label=key, description="", run=fn)


def test_invoke_task_gets_only_its_own_changes():
    ctx = _ctx()

    def first(c):
        c.changes.append(Change(KIND_REGISTRY, "A", 1, 0))

    def second(c):
        c.changes.append(Change(KIND_SERVICE, "B", None, "disabled",
                                before_known=False))

    one = invoke_task(ctx, _task("one", first))
    two = invoke_task(ctx, _task("two", second))

    assert [c.target for c in one.changes] == ["A"]
    assert [c.target for c in two.changes] == ["B"]


def test_a_task_that_fails_after_a_write_keeps_the_write_it_made():
    ctx = _ctx()

    def half_way(c):
        c.changes.append(Change(KIND_REGISTRY, "A", 1, 0))
        raise RuntimeError("denied on the second value")

    outcome = invoke_task(ctx, _task("half", half_way))
    assert outcome.failed
    assert [c.target for c in outcome.changes] == ["A"]


def test_run_summary_aggregates_the_run_ledger():
    summary = RunSummary()
    summary.add(invoke_task(_ctx(), _task("one", lambda c: c.changes.append(
        Change(KIND_REGISTRY, "A", 1, 0)))))
    summary.add(invoke_task(_ctx(), _task("two", lambda c: c.changes.append(
        Change(KIND_REGISTRY, "B", 1, 0)))))
    assert [c.target for c in summary.changes] == ["A", "B"]


# --------------------------------------------------------------------------- #
# scorecard
# --------------------------------------------------------------------------- #

tk = pytest.importorskip("tkinter")

from app.ui.dialogs.scorecard_dialog import ScorecardDialog  # noqa: E402

from _tkhelpers import make_root  # noqa: E402


def _build(changes):
    root = make_root()
    root.withdraw()
    card = ScorecardDialog(
        root, tab_name="Tweak", mode="run", cancelled=False,
        results=[("Disable telemetry", "ok", 0, None)],
        total_bytes=0, changes=changes)
    return root, card


def test_the_card_shows_the_typed_change_list_and_copies_all_of_it():
    changes = [
        Change(KIND_REGISTRY, "HKCU\\Software\\X\\GameDVR_Enabled", 1, 0),
        Change(KIND_SERVICE, "DiagTrack", None, "disabled", before_known=False),
    ]
    root, card = _build(changes)
    try:
        assert card._change_rows and len(card._change_rows) == 2
        assert "1 registry value" in card._change_lbl.cget("text")
        assert "1 service" in card._change_lbl.cget("text")
        text = card._summary_text()
        assert "What changed (1 registry value \u00b7 1 service):" in text
        assert "GameDVR_Enabled: 1 \u2192 0" in text
        assert "DiagTrack: \u2192 disabled" in text
    finally:
        card.close()
        root.destroy()


def test_the_card_caps_rows_but_the_copied_text_keeps_every_change():
    changes = [Change(KIND_REGISTRY, "HKCU\\Software\\X\\V%d" % i, 1, 0)
               for i in range(12)]
    root, card = _build(changes)
    try:
        assert len(card._change_rows) == ScorecardDialog._CHANGE_ROWS
        text = card._summary_text()
        assert text.count("\u2192 0") == 12
    finally:
        card.close()
        root.destroy()


def test_no_recorded_changes_means_no_section_at_all():
    root, card = _build([])
    try:
        assert card._change_rows is None
        assert card._change_lbl is None
        assert "What changed" not in card._summary_text()
    finally:
        card.close()
        root.destroy()


# --------------------------------------------------------------------------- #
# per-task reveal
# --------------------------------------------------------------------------- #

def _card_with_row_changes(task_changes):
    root = make_root()
    root.withdraw()
    card = ScorecardDialog(
        root, tab_name="Tweak", mode="run", cancelled=False,
        results=[("Disable telemetry", "ok", 0, None, task_changes)],
        total_bytes=0)
    return root, card


def test_a_task_row_reveals_only_its_own_changes_on_click():
    change = Change(KIND_REGISTRY, "HKCU\\Software\\X\\GameDVR_Enabled", 1, 0)
    root, card = _card_with_row_changes((change,))
    try:
        inner = card._rows_panel.inner
        row, detail = inner.winfo_children()[:2]
        assert not detail.winfo_ismapped(), "detail starts hidden"

        row.event_generate("<Button-1>")
        root.update()
        assert detail.winfo_ismapped(), "a click reveals the task's changes"
        texts = [w.cget("text") for w in detail.winfo_children()]
        assert any("GameDVR_Enabled: 1 \u2192 0" in t for t in texts), texts

        row.event_generate("<Button-1>")
        root.update()
        assert not detail.winfo_ismapped(), "a second click hides it again"
    finally:
        card.close()
        root.destroy()


def test_a_row_without_changes_has_no_reveal_affordance():
    root, card = _card_with_row_changes(())
    try:
        inner = card._rows_panel.inner
        children = inner.winfo_children()
        assert len(children) == 1, "no hidden detail frame is built"
        texts = [w.cget("text") for w in children[0].winfo_children()]
        assert not any(t.startswith("\u25b8") for t in texts), texts
    finally:
        card.close()
        root.destroy()


def test_copy_results_lists_each_tasks_own_changes_under_its_row():
    change = Change(KIND_SERVICE, "DiagTrack", None, "disabled", before_known=False)
    root, card = _card_with_row_changes((change,))
    try:
        text = card._summary_text()
        assert "Disable telemetry \u2014 done" in text
        assert "      DiagTrack: \u2192 disabled" in text
    finally:
        card.close()
        root.destroy()


# --------------------------------------------------------------------------- #
# the GUI worker stores the slice on the row
# --------------------------------------------------------------------------- #

def test_the_worker_puts_each_tasks_changes_on_its_own_result_row(monkeypatch):
    from types import SimpleNamespace

    import app.ui.runflow as runflowmod
    from app.ui.app import Application

    monkeypatch.setattr(runflowmod, "_snapshot_all_drives",
                        lambda *_a, **_k: {}, raising=True)
    app = object.__new__(Application)
    app.log = lambda _m: None
    app.set_status = lambda _s: None
    app._cancel_requested = False
    app._query_drive_free_total = lambda _root: 1000
    app._query_drives = lambda: []
    app._set_progress = lambda *_a: None
    app._stop_indeterminate = lambda: None
    app._progress_hide = lambda: None
    app.cancel_btn = SimpleNamespace(set_enabled=lambda _v: None)
    app.progress_bar = SimpleNamespace(set_indeterminate=lambda _v: None,
                                       set_fraction=lambda _v: None)
    app._release_run = lambda *_a, **_k: None
    app._set_buttons_enabled = lambda _v: None
    app._is_current = lambda _gen: True
    app._dispatch = SimpleNamespace(post=lambda _fn: None)
    app.tabs = {}

    def _invoke(ctx, task, _mode):
        ctx.changes.append(Change(KIND_REGISTRY, "HKCU\\X\\" + task.label, 1, 0))
        return "ok", 0, None

    app._invoke_single_task = _invoke
    tasks = [SimpleNamespace(label="A", risk="", key="a", revert=None),
             SimpleNamespace(label="B", risk="", key="b", revert=None)]
    results = []
    app._run_tasks_worker("Tweak", tasks, "run", gen=1, run_results=results)

    assert [r[4][0].target for r in results] == ["HKCU\\X\\A", "HKCU\\X\\B"]
