"""The run transcript says each ending once.

`self.log` writes the in-memory transcript that "Export Logs" turns into a
support file, so its structure matters more than most copy: a reader scans the
`=====` banners, then the block at the end. Two defects lived there.

The banner was written before the worker knew a Stop had landed in pre-flight,
so a run that never attempted a task still opened with "Running 5 Clean
task(s)" - a header for a run the next line called cancelled.

The closing block composed its headline from the worker's PROGRESS verb, so a
clean run read "Running complete: 5 succeeded, 0 failed." (contradiction, and
not the vocabulary of the scorecard above it), and it printed the reboot
sentence twice - once inside the summary it had just appended, once on its own
line beneath.
"""
from __future__ import annotations

import ast
import pathlib
import re
from types import SimpleNamespace

import pytest

import app.scheduler as schedmod
import app.ui.app as appmod
from app.ui import runflow as runflowmod
from app.ui.app import (Application, _log_block, _run_header_line,
                        _run_summary_lines)


def _lines(**over):
    base = dict(tab_name="Clean", mode="run", cancelled=False, crashed=None,
                completed=5, skipped_n=0, failed=0, attempted=5,
                reclaimed_bytes=0, moved_bytes=0, needs_reboot=False)
    base.update(over)
    return _run_summary_lines(**base)


def test_the_headline_names_the_run_not_the_progress_verb():
    lines = _lines()
    assert lines[0] == "Clean complete: 5 succeeded, 0 failed."
    assert not any("Running complete" in line for line in lines)
    # a revert is the Undo run, the same word its scorecard uses
    assert _lines(tab_name="Tweak", mode="revert")[0].startswith("Undo complete:")
    assert _lines(tab_name="Repair")[0].startswith("Repair complete:")


def test_a_crashed_run_is_not_called_complete():
    lines = _lines(crashed=RuntimeError("boom"))
    assert lines[0].startswith("Clean ended early:")
    assert "complete" not in lines[0].lower()
    assert any("The run ended early: RuntimeError('boom')" in line
               for line in lines)


def test_a_skipped_task_keeps_its_own_count():
    line = _lines(completed=3, skipped_n=2)[0]
    assert "3 succeeded" in line
    assert "2 skipped (nothing to change)" in line


def test_a_stop_before_the_first_task_does_not_invent_counts():
    lines = _lines(cancelled=True, attempted=0, completed=0, failed=0)
    assert lines == ["Clean stopped early \u2014 no task was attempted."]


def test_a_stop_mid_run_reports_the_counts_the_loop_already_explained():
    # the loop logs "Cancelled by user - remaining tasks were skipped." at the
    # moment of the stop; the block is the verdict and must not repeat it
    lines = _lines(cancelled=True, attempted=3, completed=2, failed=1)
    assert lines == ["Clean stopped early: 2 succeeded, 1 failed."]


def test_the_reboot_requirement_is_stated_once():
    lines = _lines(needs_reboot=True, reclaimed_bytes=1024 ** 3,
                   moved_bytes=2 * 1024 ** 2)
    assert sum("Reboot system" in line for line in lines) == 1
    assert sum(line.startswith("Disk space freed:") for line in lines) == 1
    assert sum("Quarantine" in line for line in lines) == 1


@pytest.mark.parametrize("state", [
    dict(),
    dict(cancelled=True, attempted=0, completed=0, failed=0),
    dict(cancelled=True, attempted=4, completed=2, failed=1, skipped_n=1),
    dict(crashed=RuntimeError("boom")),
    dict(needs_reboot=True, reclaimed_bytes=4096, moved_bytes=4096),
    dict(tab_name="Tweak", mode="revert", needs_reboot=True),
])
def test_no_line_is_repeated_in_the_block(state):
    lines = _lines(**state)
    assert len(lines) == len(set(lines))
    assert all(line.strip() for line in lines)


def test_the_closing_block_has_one_shape_for_every_run():
    out = []
    _log_block(out.append, "Clean complete: 2 succeeded, 0 failed.")
    assert out == ["=" * 48, "Clean complete: 2 succeeded, 0 failed.",
                   "=" * 48]


def _hand_rolled_blocks(path):
    """Line numbers of any `x.log("=" * 48)` outside the shared helper."""
    tree = ast.parse(pathlib.Path(path).read_text(encoding="utf-8"))
    offenders = []
    for fn in ast.walk(tree):
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if fn.name in {"_log_block", "log_block"}:
            continue
        for node in ast.walk(fn):
            if not (isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "log" and node.args):
                continue
            arg = node.args[0]
            if (isinstance(arg, ast.BinOp) and isinstance(arg.op, ast.Mult)
                    and isinstance(arg.right, ast.Constant)
                    and arg.right.value == 48):
                offenders.append(node.lineno)
    return offenders


def test_only_the_shared_helper_writes_the_block():
    """A second, hand-rolled copy of the block is how the transcripts drift
    apart again - the install runner, the task runner and the headless
    scheduler must all call the same helper."""
    assert not _hand_rolled_blocks(appmod.__file__), appmod.__file__
    assert not _hand_rolled_blocks(schedmod.__file__), schedmod.__file__


def test_the_banner_is_not_written_for_a_run_that_never_starts():
    assert _run_header_line("Running", 5, "Clean", False) == \
        "===== Running 5 Clean task(s) ====="
    assert _run_header_line("Undoing", 3, "Tweak", False) == \
        "===== Undoing 3 Tweak task(s) ====="
    assert _run_header_line("Running", 5, "Clean", True) is None


# ---- the real worker, with the widgets stubbed out ---------------------- #

def _task(label, risk=""):
    return SimpleNamespace(label=label, risk=risk, key=label.lower(),
                           revert=None)


def _fake_app(monkeypatch):
    """An Application whose UI is a list of log lines."""
    monkeypatch.setattr(runflowmod, "_snapshot_all_drives",
                        lambda *a, **k: {}, raising=True)
    app = object.__new__(Application)
    lines = []
    app.log = lines.append
    app.statuses = []
    app.set_status = app.statuses.append
    app._cancel_requested = False
    app._query_drive_free_total = lambda root: 1000
    app._query_drives = lambda: []
    app._set_progress = lambda *a: None
    app._stop_indeterminate = lambda: None
    app._progress_hide = lambda: None
    app.cancel_btn = SimpleNamespace(set_enabled=lambda v: None)
    app.progress_bar = SimpleNamespace(set_indeterminate=lambda v: None,
                                       set_fraction=lambda v: None)
    app._release_run = lambda *a, **k: None
    app._set_buttons_enabled = lambda v: None
    app._is_current = lambda gen: True
    app._dispatch = SimpleNamespace(post=lambda fn: None)
    app.tabs = {}
    app._invoke_single_task = lambda ctx, task, mode: ("ok", 0, None)
    return app, lines


def test_the_worker_writes_one_banner_and_one_block(monkeypatch):
    app, lines = _fake_app(monkeypatch)
    app._run_tasks_worker("Clean", [_task("Task A"), _task("Task B")],
                          "run", gen=1, run_results=[])
    assert lines[0] == "===== Running 2 Clean task(s) ====="
    assert "Clean complete: 2 succeeded, 0 failed." in lines
    # the closing block is banner / summary / banner, not four statements
    assert sum(line == "=" * 48 for line in lines) == 2
    assert len([line for line in lines if line.startswith("Clean ")]) == 1


def test_a_run_stopped_before_it_starts_writes_no_banner(monkeypatch):
    app, lines = _fake_app(monkeypatch)
    app._cancel_requested = True
    app._run_tasks_worker("Clean", [_task("Task A"), _task("Task B")],
                          "run", gen=2, run_results=[])
    # the only "=====" lines are the block's rules; no banner was written
    assert not any(line.startswith("===== ") for line in lines)
    assert "Clean stopped early \u2014 no task was attempted." in lines
    assert not any("remaining tasks were skipped" in line.lower()
                   for line in lines)


def test_the_worker_states_the_reboot_once(monkeypatch):
    app, lines = _fake_app(monkeypatch)
    app._run_tasks_worker("Tweak", [_task("Task A", risk="REBOOT REQUIRED")],
                          "run", gen=3, run_results=[])
    assert sum("Reboot system" in line for line in lines) == 1


# ---- the install runner, same stub approach --------------------------- #

def _start_windowed(monkeypatch, tasks, apps):
    """Run `_start_claimed_install` with the widgets stubbed. Returns
    (transcript lines, the cards the run showed)."""
    app, lines = _fake_app(monkeypatch)
    cards = []
    monkeypatch.setattr(runflowmod, "_themed_showinfo",
                        lambda *a, **k: cards.append(a), raising=True)
    monkeypatch.setattr(runflowmod, "is_admin", lambda: True, raising=True)
    app._state = appmod.RunState.IDLE
    app.root = None
    app._progress_show = lambda: None
    app._dispatch = SimpleNamespace(post=lambda fn: fn())
    threads = []
    app._mark_running = threads.append
    app._invoke_single_task = (lambda ctx, task, mode, strict_ok=True:
                               appmod.Application._invoke_single_task(
                                   ctx, task, mode, strict_ok))

    app._start_claimed_install(1, tasks, apps)
    for thread in threads:
        thread.join(10)
    return app, lines, cards


def _stub_winget(monkeypatch, output):
    import app.tasks.install_tasks as install_tasks

    def _run_cmd(ctx, cmd, **kw):
        kw["collect"].extend(output)
        return 0

    monkeypatch.setattr(install_tasks, "has_network", lambda: True,
                        raising=True)
    monkeypatch.setattr(install_tasks, "_ensure_winget", lambda ctx: None,
                        raising=True)
    monkeypatch.setattr(install_tasks, "run_cmd", _run_cmd, raising=True)


def test_the_windowed_install_card_carries_the_unreadable_names(monkeypatch):
    """A run started from the Install tab shows its result in a completion
    card, not in the transcript - so the packages a localized winget could not
    report have to reach that card. Drives the real update task, the real
    per-task outcome mapping and the real card copy."""
    import app.tasks.install_tasks as install_tasks

    _stub_winget(monkeypatch, [
        "(1/2) Gefunden Notepad++ [Notepad++.Notepad++]",
        "Erfolgreich installiert",
        "(2/2) Gefunden VLC media player [VideoLAN.VLC]",
        "Erfolgreich installiert",
    ])

    app, lines, cards = _start_windowed(
        monkeypatch, [install_tasks.UPDATE_ALL_TASK], [])

    assert len(cards) == 1
    # the run opens as an update too, not as "Installing 1 item(s)"
    assert "===== Updating all apps =====" in lines
    assert app.statuses[0] == "Updating installed apps..."
    assert not any("Installing" in line for line in lines)
    # the job's own line names what winget is doing, not the button's label
    assert install_tasks.UPDATE_ALL_STATUS in app.statuses
    assert not any(s.startswith("Running Update Everything")
                   for s in app.statuses)
    _root, title, detail = cards[0]
    # an upgrade run did not install anything, and its card says so
    assert title == "Update Done"
    assert detail.startswith("1 succeeded, 0 failed.")
    assert "2 update results could not be read" in detail
    assert "  - Notepad++" in detail and "  - VLC media player" in detail
    # the block the transcript ends with uses the same noun
    assert "Update finished: 1 succeeded, 0 failed." in lines


def test_the_update_sets_one_status_line_not_two_near_duplicates(monkeypatch):
    """The Install tab sets a line before the job starts and `update_all_apps`
    sets one as winget begins; two sentences there would flicker past each
    other, so both read the same string."""
    import app.tasks.install_tasks as install_tasks
    from app.utils import TaskContext

    _stub_winget(monkeypatch, [])
    statuses = []
    ctx = TaskContext(log=lambda text: None, set_status=statuses.append,
                      cancelled=lambda: False)

    install_tasks.update_all_apps(ctx)

    assert statuses == [install_tasks.UPDATE_ALL_STATUS]
    assert (appmod._install_task_status(install_tasks.UPDATE_ALL_TASK)
            == install_tasks.UPDATE_ALL_STATUS)


def test_a_mixed_run_that_includes_the_update_stays_an_install(monkeypatch):
    """The update noun belongs to a run that ONLY updates. Check the update
    row next to an app and the card still reports an install - otherwise the
    copy would describe a run that installed something as an upgrade."""
    import app.tasks.install_tasks as install_tasks

    _stub_winget(monkeypatch, [])
    monkeypatch.setattr(install_tasks, "install_selected_apps",
                        lambda ctx, apps: ([], []), raising=True)

    _app, lines, cards = _start_windowed(
        monkeypatch, [install_tasks.UPDATE_ALL_TASK], [{"name": "App A"}])

    assert cards[0][1] == "Install Done"
    assert "Install finished: 1 succeeded, 0 failed." in lines


def test_an_install_run_ends_with_the_same_block(monkeypatch):
    app, lines = _fake_app(monkeypatch)
    monkeypatch.setattr(runflowmod, "is_admin", lambda: True, raising=True)
    app._state = appmod.RunState.IDLE
    app.cancel_btn = SimpleNamespace(set_enabled=lambda v: None)
    app.progress_bar = SimpleNamespace(set_fraction=lambda v: None,
                                       set_indeterminate=lambda v: None)
    app._progress_show = lambda: None
    threads = []
    app._mark_running = threads.append
    app._invoke_single_task = (lambda ctx, task, mode, strict_ok=True:
                               ("ok", 0, None))

    app._start_claimed_install(1, [_task("Essentials A")], [])
    for thread in threads:
        thread.join(10)

    assert "===== Installing 1 selected item(s) =====" in lines
    assert "Install finished: 1 succeeded, 0 failed." in lines
    # the block is the last thing in the transcript: rule, verdict, rule
    assert lines[-3:] == ["=" * 48, "Install finished: 1 succeeded, 0 failed.",
                          "=" * 48]


# ---- the headless scheduler, same block ------------------------------- #

def _stdout_lines(capsys):
    return [line for line in capsys.readouterr().out.splitlines()
            if line.strip()]


def test_the_block_helper_is_one_tk_free_definition():
    """The scheduler runs with no Tk, so the helper cannot live in
    app/ui/app.py - and two definitions would drift."""
    from app.runner import task_runner

    assert appmod._log_block is task_runner.log_block
    assert "tkinter" not in pathlib.Path(
        task_runner.__file__).read_text(encoding="utf-8")


def test_a_scheduled_clean_ends_with_the_block(monkeypatch, capsys):
    import app.toast as toast
    from app.runner.task_runner import RunSummary
    from app.tasks import clean_tasks

    monkeypatch.setattr(schedmod, "is_admin", lambda: True, raising=True)
    monkeypatch.setattr(schedmod, "run_batch",
                        lambda *a, **k: RunSummary(succeeded=2), raising=True)
    monkeypatch.setattr(toast, "notify_scheduled_run", lambda: None,
                        raising=True)

    ok, summary = schedmod.run_auto_clean({"Clean": [clean_tasks.TASKS[0].key]})

    assert ok is True
    lines = _stdout_lines(capsys)
    assert lines[-3:] == ["[AUTO] " + "=" * 48, "[AUTO] " + summary,
                          "[AUTO] " + "=" * 48]
    # the verdict is the block's, not a line printed again after it
    assert sum(summary in line for line in lines) == 1


def test_a_scheduled_clean_with_nothing_selected_says_so(capsys):
    ok, summary = schedmod.run_auto_clean({})

    assert ok is False
    assert "no tasks were selected" in summary
    lines = _stdout_lines(capsys)
    assert lines == ["[AUTO] " + "=" * 48, "[AUTO] " + summary,
                     "[AUTO] " + "=" * 48]


def test_a_scheduled_update_ends_with_the_block(monkeypatch, capsys):
    import app.tasks.install_tasks as install_tasks

    monkeypatch.setattr(
        install_tasks, "update_all_apps",
        lambda ctx: ctx.log("  [OK] All apps updated.")
        or install_tasks.UpgradeSummary([], [], 0), raising=True)

    ok, summary = schedmod.run_auto_update()

    assert ok is True
    lines = _stdout_lines(capsys)
    assert lines[-3:] == ["[AUTO-UPD] " + "=" * 48, "[AUTO-UPD] " + summary,
                          "[AUTO-UPD] " + "=" * 48]
    assert sum(summary in line for line in lines) == 1


@pytest.mark.parametrize("updated, failed, verdict", [
    (0, 0, "Auto-update complete: all apps current."),
    (7, 0, "Auto-update complete: 7 updated, 0 failed."),
    (7, 2, "Auto-update complete with warnings: 7 updated, 2 did not update."),
    (0, 3, "Auto-update complete with warnings: 0 updated, 3 did not update."),
])
def test_the_update_verdict_counts_what_happened(updated, failed, verdict):
    assert schedmod._auto_update_verdict(updated, failed) == verdict


@pytest.mark.parametrize("updated, failed, unreadable, verdict", [
    (0, 0, 2, "Auto-update complete: 2 results could not be read "
              "(winget's output is localized)."),
    (0, 0, 1, "Auto-update complete: 1 result could not be read "
              "(winget's output is localized)."),
    (2, 0, 1, "Auto-update complete: 2 updated, 0 did not update, "
              "1 result could not be read (winget's output is localized)."),
])
def test_a_localized_update_says_what_it_could_not_read(updated, failed,
                                                        unreadable, verdict):
    """winget prints its per-app lines in the display language. A run whose
    results could not be read must never fall back to "all apps current" -
    that sentence was the bug a localized run hit."""
    got = schedmod._auto_update_verdict(updated, failed, unreadable)

    assert got == verdict
    assert "all apps current" not in got


def test_a_scheduled_update_puts_its_counts_in_the_block(monkeypatch, capsys):
    """The counts are `update_all_apps`' parsed per-package result, not a
    guess - a run that upgraded apps must not read "all apps current"."""
    import app.tasks.install_tasks as install_tasks

    monkeypatch.setattr(
        install_tasks, "update_all_apps",
        lambda ctx: install_tasks.UpgradeSummary(["App A", "App B"], ["App C"]),
        raising=True)

    ok, summary = schedmod.run_auto_update()

    assert ok is True
    assert summary == ("Auto-update complete with warnings: 2 updated, "
                       "1 did not update.")
    lines = _stdout_lines(capsys)
    assert lines[-2] == "[AUTO-UPD] " + summary


def test_a_cancelled_update_states_the_cause_inside_the_block(monkeypatch,
                                                             capsys):
    import app.tasks.install_tasks as install_tasks
    from app.utils import TaskCancelled

    def _stop(ctx):
        raise TaskCancelled("update run was stopped before finishing")

    monkeypatch.setattr(install_tasks, "update_all_apps", _stop, raising=True)

    ok, summary = schedmod.run_auto_update()

    assert ok is False
    lines = _stdout_lines(capsys)
    assert lines[-2] == "[AUTO-UPD] " + summary
    # one statement of the stop, not a cause line plus the same verdict again
    assert sum("cancelled" in line.lower() for line in lines) == 1
    assert "stopped before finishing" in summary


# ---- the counts come from winget's own output ------------------------- #

def _update_run(monkeypatch, rc, lines):
    """Drive the real `update_all_apps` with only winget itself stubbed."""
    import app.tasks.install_tasks as install_tasks
    from app.utils import TaskContext

    def _run_cmd(ctx, cmd, **kw):
        kw["collect"].extend(lines)
        return rc

    monkeypatch.setattr(install_tasks, "has_network", lambda: True,
                        raising=True)
    monkeypatch.setattr(install_tasks, "_ensure_winget", lambda ctx: None,
                        raising=True)
    monkeypatch.setattr(install_tasks, "run_cmd", _run_cmd, raising=True)
    logged = []
    ctx = TaskContext(log=logged.append, set_status=lambda s: None,
                      cancelled=lambda: False)
    return install_tasks.update_all_apps(ctx), logged


def test_an_update_returns_what_winget_says_it_updated(monkeypatch):
    result, logged = _update_run(monkeypatch, 0, [
        "(1/2) Found App A [Pub.A]",
        "Successfully installed",
        "(2/2) Found App B [Pub.B]",
        "Successfully installed",
    ])
    assert result.updated == ["App A", "App B"]
    assert result.failed == []
    assert result.unreadable == 0
    # rc 0 is the normal success case, so its log line may not contradict the
    # counts: "already current" is only true when the output named no package
    assert any("Updated 2 app(s): App A, App B." in line for line in logged)
    assert not any("already current" in line for line in logged)


def test_an_update_counts_a_package_that_failed_on_its_own_installer(monkeypatch):
    result, _logged = _update_run(monkeypatch, 1, [
        "(1/2) Found App A [Pub.A]",
        "Successfully installed",
        "(2/2) Found App B [Pub.B]",
        "Installer failed with exit code 1603",
    ])
    assert result.updated == ["App A"]
    assert result.failed and result.failed[0].startswith("App B")
    assert result.unreadable == 0


def test_an_update_with_nothing_to_do_returns_no_counts(monkeypatch):
    result, logged = _update_run(monkeypatch, 0, ["No applicable update found."])
    assert (result.updated, result.failed, result.unreadable) == ([], [], 0)
    assert any("already current" in line for line in logged)


def test_a_localized_update_does_not_pass_for_all_apps_current(monkeypatch):
    """German winget: the '(N/M)' counter, the name and the [ID] are still
    there, but "Successfully installed" is "Erfolgreich installiert". Before
    this was fixed those two packages parsed as 0/0 and the verdict said the
    system was already current after upgrading them."""
    result, logged = _update_run(monkeypatch, 0, [
        "(1/2) Gefunden Notepad++ [Notepad++.Notepad++]",
        "Erfolgreich installiert",
        "(2/2) Gefunden VLC media player [VideoLAN.VLC]",
        "Erfolgreich installiert",
    ])

    assert result.updated == []
    assert result.failed == []
    assert result.unreadable == 2
    assert not any("already current" in line for line in logged)
    assert any("2 package(s) went through winget" in line for line in logged)


def test_a_localized_update_names_the_packages_it_could_not_read(monkeypatch):
    """The count alone leaves the user unable to match the report to the log
    above it; the list is what makes "could not be read" actionable."""
    result, logged = _update_run(monkeypatch, 0, [
        "(1/2) Gefunden Notepad++ [Notepad++.Notepad++]",
        "Erfolgreich installiert",
        "(2/2) Gefunden VLC media player [VideoLAN.VLC]",
        "Erfolgreich installiert",
    ])

    # the translated verb is dropped because the untranslated ID still
    # identifies what follows it - no translation table involved
    assert result.unreadable_names == ("Notepad++", "VLC media player")
    assert "    - Notepad++" in logged
    assert "    - VLC media player" in logged


def test_a_localized_update_leaves_the_names_for_the_ui_card(monkeypatch):
    """The windowed install runner's card is built from counts, so the names
    have to travel with the run (`ctx.notes`) instead of being re-parsed out
    of a transcript that is not on screen during an install."""
    import app.tasks.install_tasks as install_tasks
    from app.utils import TaskContext

    def _run_cmd(ctx, cmd, **kw):
        kw["collect"].extend([
            "(1/2) Gefunden Notepad++ [Notepad++.Notepad++]",
            "Erfolgreich installiert",
            "(2/2) Gefunden VLC media player [VideoLAN.VLC]",
            "Erfolgreich installiert",
        ])
        return 0

    monkeypatch.setattr(install_tasks, "has_network", lambda: True,
                        raising=True)
    monkeypatch.setattr(install_tasks, "_ensure_winget", lambda ctx: None,
                        raising=True)
    monkeypatch.setattr(install_tasks, "run_cmd", _run_cmd, raising=True)

    ctx = TaskContext(log=lambda text: None, set_status=lambda text: None,
                      cancelled=lambda: False)
    install_tasks.update_all_apps(ctx)

    assert ctx.notes == [
        "2 update results could not be read — winget reports in the Windows "
        "display language:\n  - Notepad++\n  - VLC media player"
    ]


def test_a_name_the_id_cannot_confirm_keeps_windows_own_wording(monkeypatch):
    """When the ID shares nothing with the display name there is no safe way
    to tell a translated verb from a first name word, so the header is kept
    as winget wrote it rather than guessed at."""
    result, _logged = _update_run(monkeypatch, 0, [
        "(1/1) Gefunden Wunderlist [6Wunderkinder.WunderlistDesktop]",
        "Erfolgreich installiert",
    ])

    assert result.unreadable_names == ("Gefunden Wunderlist",)


def test_an_update_counts_the_mix_of_read_and_unread_packages(monkeypatch):
    result, _logged = _update_run(monkeypatch, 0, [
        "(1/2) Found App A [Pub.A]",
        "Successfully installed",
        "(2/2) Gefunden App B [Pub.B]",
        "Erfolgreich installiert",
    ])

    assert result.updated == ["App A"]
    assert result.unreadable == 1


def test_a_localized_update_losing_its_headers_still_counts_packages(monkeypatch):
    """Even when not one header survives the parse, winget's own '(N/M)'
    counter says it worked through packages - which is not "nothing to do"."""
    result, logged = _update_run(monkeypatch, 0, [
        "(1/2) Installation wird gestartet",
        "(2/2) Installation wird gestartet",
    ])

    assert result.unreadable == 2
    assert result.unreadable_names == ()  # no header, so no name to give
    assert any("the lines above are the record." in line for line in logged)
    assert not any("already current" in line for line in logged)


def test_the_live_status_states_how_many_packages_winget_planned():
    """winget's '(3/7)' is the only total it ever prints - `upgrade --all` has
    no dry-run summary - so the update's status line says it in words instead
    of leaving the counter to be decoded."""
    import app.tasks.install_tasks as install_tasks
    from app.utils import TaskContext

    statuses = []
    inner = TaskContext(log=lambda text: None, set_status=statuses.append,
                        cancelled=lambda: False)
    live = install_tasks._live_status_ctx(inner)

    live.log("(1/3) Found Notepad++ [Notepad++.Notepad++]")
    live.log("Successfully installed")
    live.log("(2/3) Gefunden VLC media player [VideoLAN.VLC]")
    live.log("(3/3) Found Steam [Valve.Steam]")

    # the count stays on the lines between headers, so the plan never
    # disappears mid-run
    assert statuses == [
        "Updating 1 of 3 apps: Notepad++",
        "Updating 1 of 3 apps: Successfully installed",
        "Updating 2 of 3 apps: VLC media player",
        "Updating 3 of 3 apps: Steam",
    ]


#: One row per SHAPE of winget body the strip has to render: a line that
#: fits, a carried plan, a header with its count, a localized header, a name
#: too long, a URL too long (with a host that fits, a deep path, a file name
#: wider than the strip, or winget's progress figures after it), and a
#: package that is starting. The expected status is the render, so every
#: trimming rule is visible as a row.
_STATUS_TRIM_CASES = [
    # a URL that fits is quoted as winget printed it, scheme included: the
    # eight characters "https://" costs buy nothing on these lines, and the
    # scheme is the one part of the URL the strip cannot get back once it
    # shortens (sixty-eighth pass)
    ("line fits, no plan yet",
     "Downloading https://example.test/x.exe", (0, 0),
     "Downloading https://example.test/x.exe"),
    ("line fits, plan carried",
     "Downloading https://example.test/x.exe", (1, 3),
     "Updating 1 of 3 apps: Downloading https://example.test/x.exe"),
    ("installed line, plan carried",
     "Successfully installed", (2, 3),
     "Updating 2 of 3 apps: Successfully installed"),
    ("header names the plan, singular total",
     "(1/1) Found Steam [Valve.Steam]", (0, 0),
     "Updating 1 of 1 app: Steam"),
    ("header, name fits",
     "(2/3) Found VLC media player [VideoLAN.VLC]", (0, 0),
     "Updating 2 of 3 apps: VLC media player"),
    ("header, localized verb dropped on the ID",
     "(2/3) Gefunden VLC media player [VideoLAN.VLC]", (0, 0),
     "Updating 2 of 3 apps: VLC media player"),
    ("header, localized verb kept without the ID",
     "(2/3) Gefunden Wunderlist [6Wunderkinder.WunderlistDesktop]", (0, 0),
     "Updating 2 of 3 apps: Gefunden Wunderlist"),
    ("package name too long: end trimmed",
     "(2/5) Found Microsoft Visual C++ 2015-2022 Redistributable (x64) "
     "- 14.38.33130 (Preview Build) [Microsoft.VCRedist.2015+.x64]", (0, 0),
     "Updating 2 of 5 apps: Microsoft Visual C++ 2015-2022 Redistributable "
     "(x64) - 14.38.33130…"),
    ("URL too long: middle cut at a separator",
     "Downloading https://github.com/notepad-plus-plus/notepad-plus-plus/"
     "releases/download/v8.6.9/npp.8.6.9.Installer.x64.exe", (2, 7),
     "Updating 2 of 7 apps: Downloading https://github.com"
     "/…/npp.8.6.9.Installer.x64.exe"),
    ("URL too long behind a long word: host and file name",
     "Wird heruntergeladen https://github.com/notepad-plus-plus/"
     "notepad-plus-plus/releases/download/v8.6.9/npp.8.6.9.Installer.x64.exe",
     (2, 7),
     "Updating 2 of 7 apps: Wird heruntergeladen "
     "github.com/…/npp.8.6.9.Installer.x64.exe"),
    ("URL too long, deep path: last directory kept",
     "Downloading https://cdn.example.test/a/b/c/d/e/f/verylongfilename-1.0.exe",
     (9, 12),
     "Updating 9 of 12 apps: Downloading https://cdn.example.test/a"
     "/…/verylongfilename-1.0.exe"),
    # winget prints its progress bar while the file downloads, and a captured
    # stream can carry those figures on the same line as the URL (the noise
    # microsoft/winget-cli#2582 describes). The moving "done" figure goes; the
    # size the pair ends on stays, as the file's size, and the caller paints it
    # once per package (see test_the_download_size_is_shown_once_per_package).
    # The size costs the scheme, not the host: "github.com/…/npp…" still says
    # where the file comes from, and the file name names it.
    ("URL followed by winget's progress figures",
     "Downloading https://github.com/notepad-plus-plus/notepad-plus-plus/"
     "releases/download/v8.6.9/npp.8.6.9.Installer.x64.exe 1.61 MB / 1.61 MB",
     (2, 7),
     "Updating 2 of 7 apps: Downloading "
     "github.com/…/npp.8.6.9.Installer.x64.exe (1.61 MB)"),
    # the left-hand figure is how much has arrived, so a line caught mid-file
    # states the file's size on the right - not the counter it happens to read.
    ("mid-download line: the size, not the counter",
     "Downloading https://mirror.test/vlc-3.0.20.exe 0.40 MB / 44.2 MB",
     (2, 7),
     "Updating 2 of 7 apps: Downloading https://mirror.test/vlc-3.0.20.exe "
     "(44.2 MB)"),
    ("file name wider than the strip: its tail kept",
     "Downloading https://mirror.test/dl/" + "a" * 120 + "-8.6.9-x64.exe",
     (2, 7),
     "Updating 2 of 7 apps: Downloading …" + "a" * 41 + "-8.6.9-x64.exe"),
    ("package starting",
     "Starting package install...", (3, 12),
     "Updating 3 of 12 apps: Starting package install..."),
]


_URL_RE = re.compile(r"https?://\S+")


def _url_fragment_start(rendered: str, lead: str,
                        url: str) -> "int | None":
    """Where the kept part of `url` begins in `rendered`, or None.

    Preferred: just after the words that precede the URL in winget's line,
    which puts the start inside the render even when the URL was cut. Failing
    that the whole URL is looked for, and failing that the ellipsis that
    replaced everything before the kept tail.
    """
    if lead and lead in rendered:
        return rendered.index(lead) + len(lead)
    if url in rendered:
        return rendered.index(url)
    at = rendered.find("…")
    return at if at >= 0 else None


def _assert_url_fragments_are_whole(line: str, rendered: str) -> None:
    """Every URL fragment the table row keeps is a whole path segment.

    The strip shortens a download URL by removing its MIDDLE, so whatever
    survives on either side of the ellipsis has to line up with separators in
    the source. Two cuts fail that and are asserted against here: a head that
    ends mid-directory (".../notepad-") and a tail that starts mid-directory,
    where the ellipsis hides the rest of a name the user is meant to read.

    The fragment is located by the words winget printed in FRONT of the URL
    and runs to the next whitespace, so whatever a future row puts after the
    URL - a mirror, a size, a percentage - is left out instead of becoming
    the fragment under test. Locating it positionally also means a render
    that has lost the URL fails here rather than being skipped: a row the
    check cannot read must not pass silently.

    A head is whole at the start of the URL or at the start of its host: when
    the strip has room for a host but not for "https://" as well, the scheme
    is dropped and "github.com/…/npp…" keeps what identifies the download.
    """
    match = _URL_RE.search(line)
    if match is None:                 # a shape with no URL to fragment
        return
    url, lead = match.group(0), line[:match.start()]
    start = _url_fragment_start(rendered, lead, url)
    assert start is not None, f"render shows no URL to check: {rendered!r}"
    shown = rendered[start:].split(None, 1)[0]

    if "…" not in shown:              # fits, so nothing was cut
        assert shown == url
        return

    head, tail = shown.split("…")
    kept = head.rstrip("/")           # the head is joined on with a "/"
    if kept:
        site = url.find("://")
        host_at = site + 3 if site >= 0 else 0
        at = (0 if url.startswith(kept)
              else host_at if url[host_at:].startswith(kept) else -1)
        assert at >= 0, f"head is not a whole prefix: {head!r}"
        at += len(kept)
        assert url[at] == "/", f"head cut mid-segment: {head!r}"
        # ...and a real one: reaching the host is the floor, so a head that
        # stops inside the scheme ("https:/") or inside the host fails
        after_host = url.find("/", host_at)
        assert at >= after_host, f"head cut inside the host: {head!r}"

    assert url.endswith(tail)
    at = len(url) - len(tail)
    if tail and not tail.startswith("/"):
        # permitted only for the file name, which keeps its end when the
        # whole of it cannot fit - a directory fragment never starts here
        assert at > url.rfind("/"), f"tail starts mid-segment: {tail!r}"


@pytest.mark.parametrize("shape, line, plan, expected", _STATUS_TRIM_CASES,
                         ids=[case[0] for case in _STATUS_TRIM_CASES])
def test_each_body_shape_renders_as_the_strip_shows_it(shape, line, plan,
                                                       expected):
    """The trimming rules, one row each: a body that fits is shown whole; a
    stated plan is prefixed and never trimmed; a name too long loses its END
    (its first words identify it); a download URL too long loses its MIDDLE,
    at a separator, so the file name survives; and a file name wider than the
    strip keeps its own end.

    The render is pinned exactly, and every row that shows a URL is also held
    to the rule behind those renders: no fragment of it is cut through the
    middle of a directory.
    """
    import app.tasks.install_tasks as install_tasks

    status, _plan = install_tasks._winget_status_line(line, plan)

    assert status == expected
    assert len(status) <= 90
    if plan[1]:                       # a stated count never leaves the line
        assert status.startswith(f"Updating {plan[0]} of {plan[1]} app")
    _assert_url_fragments_are_whole(line, status)


@pytest.mark.parametrize("line, rendered", [
    # head ends inside "notepad-plus-plus"
    ("Downloading https://github.com/notepad-plus-plus/notepad-plus-plus/"
     "releases/download/v8.6.9/npp.8.6.9.Installer.x64.exe",
     "Updating 2 of 7 apps: Downloading https://github.com/notepad-"
     "…/npp.8.6.9.Installer.x64.exe"),
    # head ends inside the scheme, which is not a path
    ("Downloading https://github.com/notepad-plus-plus/notepad-plus-plus/"
     "releases/download/v8.6.9/npp.8.6.9.Installer.x64.exe",
     "Updating 2 of 7 apps: Downloading https:/…/npp.8.6.9.Installer.x64.exe"),
    # head ends inside the HOST - the new floor a scheme-less head must meet
    ("Downloading https://github.com/notepad-plus-plus/notepad-plus-plus/"
     "releases/download/v8.6.9/npp.8.6.9.Installer.x64.exe",
     "Updating 2 of 7 apps: Downloading "
     "github.co…/npp.8.6.9.Installer.x64.exe"),
    # the ellipsis hides the rest of a DIRECTORY, not of the file name
    ("Downloading https://files.example.test/verylongdirectories/name/file.zip",
     "Updating 2 of 7 apps: Downloading "
     "…rylongdirectories/name/file.zip"),
])
def test_the_url_fragment_rule_rejects_a_cut_through_a_name(line, rendered):
    """The table's rule must bite: each of these keeps the file name and fits
    the strip, so only the fragment check can tell them from a good render."""
    with pytest.raises(AssertionError):
        _assert_url_fragments_are_whole(line, rendered)


@pytest.mark.parametrize("line, rendered", [
    # winget's words after the URL, beside a good middle cut
    ("Downloading https://github.com/notepad-plus-plus/notepad-plus-plus/"
     "releases/download/v8.6.9/npp.8.6.9.Installer.x64.exe",
     "Updating 2 of 7 apps: Downloading https://github.com"
     "/…/npp.8.6.9.Installer.x64.exe from the mirror"),
    # ...and beside a URL that fits untouched
    ("Downloading https://example.test/x.exe",
     "Updating 2 of 7 apps: Downloading https://example.test/x.exe 12.4 MB"),
    # no words in front of the URL, so the front comes from the ellipsis
    ("https://github.com/notepad-plus-plus/notepad-plus-plus/releases/"
     "download/v8.6.9/npp.8.6.9.Installer.x64.exe",
     "Updating 2 of 3 apps: …/npp.8.6.9.Installer.x64.exe done"),
])
def test_the_url_fragment_rule_ignores_words_after_the_url(line, rendered):
    """A future row may print winget text after the URL. The fragment ends at
    the next whitespace, so that text never becomes the fragment under test."""
    _assert_url_fragments_are_whole(line, rendered)      # must not raise


def test_the_download_size_winget_states_is_the_file_not_the_counter():
    """Only the right-hand figure of "<done> / <total>" is the size of the
    file; a percentage states none, and no URL means no download to size."""
    import app.tasks.install_tasks as install_tasks

    size = install_tasks._download_size

    assert size("Downloading https://example.test/x.exe 0.40 MB / 44.2 MB") \
        == "44.2 MB"
    assert size("Downloading https://example.test/x.exe 1.61 MB / 1.61 MB") \
        == "1.61 MB"
    assert size("Downloading https://example.test/x.exe 45%") is None
    # a name ending in a size is not a download in progress
    assert size("Starting package install... 12 MB") is None


def test_a_localized_download_line_reaches_the_strip():
    """H-010: winget's verbs are translated, its URLs are not. Before this, a
    German build's download line was dropped by the strip entirely, taking the
    file name and the size with it - the two things that line is for."""
    import app.tasks.install_tasks as install_tasks
    from app.utils import TaskContext

    statuses = []
    inner = TaskContext(log=lambda text: None, set_status=statuses.append,
                        cancelled=lambda: False)
    live = install_tasks._live_status_ctx(inner)

    live.log("(1/2) Gefunden VLC media player [VideoLAN.VLC]")
    live.log("Wird heruntergeladen https://x.test/vlc.exe 0.40 MB / 44.2 MB")

    assert statuses == [
        "Updating 1 of 2 apps: VLC media player",
        "Updating 1 of 2 apps: Wird heruntergeladen https://x.test/vlc.exe "
        "(44.2 MB)",
    ]


def test_the_download_size_is_shown_once_per_package():
    """winget repeats the download line with a moved counter. The strip paints
    the file's size on the first of them and leaves the repeats to the log:
    repainting them would churn the line and drop the size on the next tick.
    The next package starts its own once."""
    import app.tasks.install_tasks as install_tasks
    from app.utils import TaskContext

    statuses, logged = [], []
    inner = TaskContext(log=logged.append, set_status=statuses.append,
                        cancelled=lambda: False)
    live = install_tasks._live_status_ctx(inner)

    live.log("(2/3) Found VLC media player [VideoLAN.VLC]")
    live.log("Downloading https://mirror.test/vlc-3.0.20.exe 0.40 MB / 44.2 MB")
    live.log("Downloading https://mirror.test/vlc-3.0.20.exe 12.1 MB / 44.2 MB")
    live.log("Downloading https://mirror.test/vlc-3.0.20.exe 44.2 MB / 44.2 MB")
    live.log("Starting package install...")
    live.log("(3/3) Found Steam [Valve.Steam]")
    live.log("Downloading https://cdn.test/steam.exe 2.0 MB / 2.0 MB")
    live.log("Downloading https://cdn.test/steam.exe 2.0 MB / 2.0 MB")

    assert statuses == [
        "Updating 2 of 3 apps: VLC media player",
        "Updating 2 of 3 apps: Downloading "
        "https://mirror.test/vlc-3.0.20.exe (44.2 MB)",
        "Updating 2 of 3 apps: Starting package install...",
        "Updating 3 of 3 apps: Steam",
        "Updating 3 of 3 apps: Downloading https://cdn.test/steam.exe (2.0 MB)",
    ]
    # ...and every one of them is still in the log the run reads
    assert len(logged) == 8


def test_only_the_progress_figures_are_dropped_from_a_download_line():
    """The strip sheds winget's progress figures - the "done" half is stale
    before it is read - but nothing else winget prints there. The log keeps the
    whole line either way."""
    import app.tasks.install_tasks as install_tasks

    kept = install_tasks._without_progress_tail

    assert kept("Downloading https://example.test/x.exe 1.61 MB / 1.61 MB") \
        == "Downloading https://example.test/x.exe"
    assert kept("Downloading https://example.test/x.exe 45%") \
        == "Downloading https://example.test/x.exe"
    assert kept("Downloading https://example.test/x.exe from the mirror") \
        == "Downloading https://example.test/x.exe from the mirror"
    # a line with no URL is left alone: a name ending in a size is not a
    # download in progress
    assert kept("Starting package install... 12 MB") \
        == "Starting package install... 12 MB"


def test_a_render_that_lost_its_url_is_not_skipped_silently():
    """A row the check cannot read has to fail: a skip would look like a pass
    for exactly the rows nobody has written yet."""
    line = "Downloading https://example.test/x.exe"

    with pytest.raises(AssertionError, match="no URL to check"):
        _assert_url_fragments_are_whole(line,
                                        "Updating 1 of 3 apps: Downloading")


def test_a_localized_update_with_a_nonzero_exit_does_not_claim_nothing_updated(
        monkeypatch):
    """rc != 0 plus unreadable results: the run failed, but "nothing updated"
    would be a guess about output this parser could not read."""
    with pytest.raises(RuntimeError) as excinfo:
        _update_run(monkeypatch, 1, [
            "(1/1) Gefunden App A [Pub.A]",
            "Erfolgreich installiert",
        ])

    assert "could not be read" in str(excinfo.value)
    assert "nothing updated" not in str(excinfo.value)


def test_a_scheduled_localized_update_puts_that_in_the_block(monkeypatch,
                                                            capsys):
    import app.tasks.install_tasks as install_tasks

    def _localized(ctx):
        summary = install_tasks.UpgradeSummary(
            [], [], 3, ("Notepad++", "VLC media player", "Steam"))
        install_tasks._log_unreadable(ctx, summary)
        return summary

    monkeypatch.setattr(install_tasks, "update_all_apps", _localized,
                        raising=True)

    ok, summary = schedmod.run_auto_update()

    assert ok is True
    assert summary == ("Auto-update complete: 3 results could not be read "
                       "(winget's output is localized).")
    lines = _stdout_lines(capsys)
    assert lines[-2] == "[AUTO-UPD] " + summary
    assert "all apps current" not in "\n".join(lines)
    # the names ride in the log the block closes, not in the verdict itself
    assert "[AUTO-UPD]     - Steam" in lines


def test_the_cli_does_not_repeat_the_verdict_after_the_block():
    """`__main__` used to echo the summary it had just logged, so a scheduled
    run ended with the same sentence twice, one line under the block."""
    src = pathlib.Path(appmod.__file__).parents[1].joinpath(
        "__main__.py").read_text(encoding="utf-8")
    assert "[AUTO] clean:" not in src
    assert "[AUTO] update:" not in src
