"""SEC-004: removing the Defender exclusions this app added.

The removal itself is a PowerShell call, so the call is stubbed here and what
is under test is the bookkeeping around it: exactly which paths get asked for,
and what the record says afterwards.

The record is the expensive part. It is the ONLY way the app knows which
exclusions are its own — Defender cannot tell it — so a record lost on failure
means those exclusions outlive the user's decision to have them, with nothing
left that can find them. A failed removal must therefore leave the record
exactly as it was, so the next elevated launch retries it.
"""
from __future__ import annotations

import json
import os

import pytest


@pytest.fixture
def config(tmp_path, monkeypatch):
    """config.json in a temp dir, so the record is read from a real file."""
    import app.config_persist as cp

    cfg = tmp_path / "config.json"
    monkeypatch.setattr(cp, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(cp, "CONFIG_FILE", cfg)
    monkeypatch.setattr(cp, "_config_cache", None, raising=False)
    monkeypatch.setattr(cp, "_config_mtime", None, raising=False)
    return cfg


def _record(cfg):
    """The recorded exclusions, straight off disk."""
    try:
        data = json.loads(cfg.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data.get("defender_exclusions_applied")


def _set_record(paths):
    from app.config_persist import update_config

    def _mut(c):
        c["defender_exclusions_applied"] = list(paths)

    update_config(_mut)


@pytest.fixture
def shell(monkeypatch):
    """A stubbed PowerShell call. Returns (calls, state)."""
    import app.elevation as el

    calls = []
    state = {"result": (True, "")}

    def fake(script):
        calls.append(script)
        return state["result"]

    monkeypatch.setattr(el, "is_admin", lambda: True)
    monkeypatch.setattr(el, "_run_mp_script", fake)
    return calls, state


def test_it_removes_exactly_the_recorded_paths_in_one_call(config, shell):
    calls, _ = shell
    paths = [r"C:\Apps\CleanerTool.exe", r"D:\Portable\CleanerTool.exe"]
    _set_record(paths)

    from app.elevation import remove_defender_exclusions
    ok, msg = remove_defender_exclusions()

    assert ok is True
    assert msg == "removed 2 exclusion(s)"
    assert len(calls) == 1, "one command, not one per path"
    assert calls[0].count("Remove-MpPreference") == 2
    for path in paths:
        assert f"-ExclusionPath '{path}'" in calls[0]
    # never the whole list: only what this app recorded
    assert "Add-MpPreference" not in calls[0]
    assert _record(config) == [], "a successful removal clears the record"


def test_a_failed_removal_keeps_the_record_and_is_retried(config, shell):
    """The leftover-record retry: Defender absent, a policy block, or a
    permission problem must all leave the record intact so the next elevated
    launch tries again."""
    calls, state = shell
    path = r"C:\Apps\CleanerTool.exe"
    _set_record([path])

    from app.elevation import remove_defender_exclusions

    state["result"] = (False, "Remove-MpPreference failed: access denied")
    ok, msg = remove_defender_exclusions()
    assert ok is False
    assert "access denied" in msg
    assert _record(config) == [path], "a failure must not forget the path"

    # next launch: the shell call works this time
    state["result"] = (True, "")
    ok, msg = remove_defender_exclusions()
    assert ok is True
    assert msg == "removed 1 exclusion(s)"
    assert len(calls) == 2
    assert path in calls[1]
    assert _record(config) == []


def test_a_failure_with_no_output_still_says_something(config, shell):
    _, state = shell
    _set_record([r"C:\Apps\CleanerTool.exe"])
    state["result"] = (False, "")

    from app.elevation import remove_defender_exclusions
    ok, msg = remove_defender_exclusions()

    assert ok is False
    assert msg == "Remove-MpPreference failed"


def test_a_raising_shell_call_is_reported_and_not_propagated(config, shell,
                                                            monkeypatch):
    calls, _ = shell
    path = r"C:\Apps\CleanerTool.exe"
    _set_record([path])

    import app.elevation as el

    def boom(script):
        calls.append(script)
        raise RuntimeError("powershell went away")

    monkeypatch.setattr(el, "_run_mp_script", boom)

    ok, msg = el.remove_defender_exclusions()          # must not raise
    assert ok is False
    assert msg == "RuntimeError: powershell went away"
    assert _record(config) == [path]


def test_nothing_recorded_means_no_shell_call(config, shell):
    calls, _ = shell
    _set_record([])

    from app.elevation import remove_defender_exclusions
    ok, msg = remove_defender_exclusions()

    assert ok is True
    assert msg == "no exclusions recorded by this app"
    assert calls == []


def test_without_admin_it_touches_nothing(config, shell, monkeypatch):
    calls, _ = shell
    _set_record([r"C:\Apps\CleanerTool.exe"])

    import app.elevation as el
    monkeypatch.setattr(el, "is_admin", lambda: False)

    ok, msg = el.remove_defender_exclusions()

    assert ok is False
    assert msg == "not running as administrator"
    assert calls == []
    assert _record(config) == [r"C:\Apps\CleanerTool.exe"]


def test_removing_a_subset_leaves_the_other_paths_recorded(config, shell,
                                                          tmp_path):
    """Only some paths asked for: the rest were NOT removed, so they must stay
    in the record or nothing will ever remove them."""
    calls, _ = shell
    asked = str(tmp_path / "CleanerTool.exe")
    kept = str(tmp_path / "old" / "CleanerTool.exe")
    _set_record([asked, kept])

    from app.elevation import remove_defender_exclusions
    ok, msg = remove_defender_exclusions(paths=[asked])

    assert ok is True
    assert msg == "removed 1 exclusion(s)"
    assert calls[0].count("Remove-MpPreference") == 1
    assert asked in calls[0]
    assert kept not in calls[0]
    assert _record(config) == [kept], "the path we did not remove is still ours"


def test_an_explicit_path_that_was_not_recorded_is_refused(config, shell,
                                                           tmp_path):
    """The record is what makes removal safe: an explicit path the app does not
    own must be ignored, so a bug elsewhere cannot delete someone else's
    exclusion."""
    calls, _ = shell
    mine = str(tmp_path / "CleanerTool.exe")
    theirs = str(tmp_path / "SomeOtherTool.exe")
    _set_record([mine])

    from app.elevation import remove_defender_exclusions
    ok, msg = remove_defender_exclusions(paths=[theirs])

    assert ok is True
    assert msg == "no exclusions recorded by this app"
    assert calls == []
    assert _record(config) == [mine]


def test_an_explicit_path_is_accepted_when_nothing_is_recorded(config, shell,
                                                              tmp_path):
    """Installs predating the record: the only exclusion the app ever added was
    its own binary, so an explicit path is allowed when the record is empty —
    and an empty record must stay empty, not gain the path."""
    calls, _ = shell
    legacy = str(tmp_path / "CleanerTool.exe")
    _set_record([])

    from app.elevation import remove_defender_exclusions
    ok, msg = remove_defender_exclusions(paths=[legacy])

    assert ok is True
    assert msg == "removed 1 exclusion(s)"
    assert legacy in calls[0]
    assert _record(config) == []


# --------------------------------------------------------------------------- #
# remove_path_exclusion: the one-path removal the migration notice uses. It
# deliberately does NOT require the record (the folder it removes predates it),
# but it must keep the record honest when the path happens to be in there.
# --------------------------------------------------------------------------- #

def test_removing_one_path_by_name_prunes_it_from_the_record(config, shell,
                                                             tmp_path):
    calls, _ = shell
    gone = str(tmp_path / "CleanerTool.exe")
    other = str(tmp_path / "old" / "CleanerTool.exe")
    _set_record([gone, other])

    from app.elevation import remove_path_exclusion
    ok, msg = remove_path_exclusion(gone)

    assert ok is True
    assert "Removed the exclusion" in msg
    assert calls[0].count("Remove-MpPreference") == 1
    assert gone in calls[0]
    assert _record(config) == [other], "the removed path must not still be ours"


def test_a_failed_named_removal_keeps_the_record(config, shell, tmp_path):
    _, state = shell
    path = str(tmp_path / "CleanerTool.exe")
    _set_record([path])
    state["result"] = (False, "access denied")

    from app.elevation import remove_path_exclusion
    ok, msg = remove_path_exclusion(path)

    assert ok is False
    assert "access denied" in msg
    assert _record(config) == [path], "a failure must stay recorded for retry"


def test_a_path_never_recorded_is_still_removed_by_name(config, shell,
                                                        tmp_path):
    """The migration notice removes a FOLDER an older build excluded, which the
    record never contained. That must still work, and the record must be left
    exactly as it was.
    """
    calls, _ = shell
    mine = str(tmp_path / "CleanerTool.exe")
    folder = str(tmp_path / "OldFolder")
    _set_record([mine])

    from app.elevation import remove_path_exclusion
    ok, _msg = remove_path_exclusion(folder)

    assert ok is True
    assert folder in calls[0]
    assert _record(config) == [mine]


def test_the_record_match_ignores_a_trailing_separator(config, shell,
                                                       tmp_path):
    """`C:\\Apps` and `C:\\Apps\\` name the same exclusion, and the caller's
    spelling need not be the one the record was written with."""
    _, _state = shell
    folder = str(tmp_path / "Apps")
    _set_record([folder])

    from app.elevation import remove_path_exclusion
    assert remove_path_exclusion(folder + os.sep)[0] is True
    assert _record(config) == []


# --------------------------------------------------------------------------- #
# ensure_defender_exclusion: the add path. What it asks Defender for, and what
# the record says afterwards — a record that is wrong here is what makes the
# removal path unable to undo the user's decision.
# --------------------------------------------------------------------------- #

def test_the_default_add_excludes_the_exe_and_never_its_folder(config, shell,
                                                              monkeypatch,
                                                              tmp_path):
    """SEC-004: an older build excluded the parent folder, which for a portable
    app run from Desktop or Downloads quietly stopped Defender scanning every
    OTHER file in that folder."""
    calls, _ = shell
    exe = str(tmp_path / "Apps" / "CleanerTool.exe")
    folder = os.path.dirname(exe)

    import app.elevation as el
    monkeypatch.setattr(el, "_current_exe_path", lambda: exe)

    ok, msg = el.ensure_defender_exclusion()

    assert ok is True
    assert msg == "exclusion added for 1 path(s)"
    assert calls[0].count("Add-MpPreference") == 1
    assert f"-ExclusionPath '{exe}'" in calls[0]
    assert f"-ExclusionPath '{folder}'" not in calls[0], "never the folder"
    assert _record(config) == [exe]


def test_a_failed_add_records_nothing(config, shell, monkeypatch, tmp_path):
    _, state = shell
    old = str(tmp_path / "old" / "CleanerTool.exe")
    _set_record([old])
    state["result"] = (False, "Defender is not installed")

    import app.elevation as el
    monkeypatch.setattr(el, "_current_exe_path",
                        lambda: str(tmp_path / "new" / "CleanerTool.exe"))

    ok, msg = el.ensure_defender_exclusion()

    assert ok is False
    assert "Defender is not installed" in msg
    assert _record(config) == [old], "nothing was added, so nothing to record"


def test_a_raising_add_is_reported_and_records_nothing(config, shell,
                                                      monkeypatch, tmp_path):
    calls, _state = shell
    _set_record([])

    import app.elevation as el

    def boom(script):
        calls.append(script)
        raise RuntimeError("powershell went away")

    monkeypatch.setattr(el, "_run_mp_script", boom)
    monkeypatch.setattr(el, "_current_exe_path",
                        lambda: str(tmp_path / "CleanerTool.exe"))

    ok, msg = el.ensure_defender_exclusion()          # must not raise
    assert ok is False
    assert msg == "RuntimeError: powershell went away"
    assert _record(config) == []


def test_explicit_paths_are_absolutised_and_deduplicated(config, shell,
                                                         tmp_path):
    calls, _ = shell
    _set_record([])
    exe = str(tmp_path / "Tools" / "CleanerTool.exe")
    same = str(tmp_path / "Tools" / "." / "CleanerTool.exe")

    from app.elevation import ensure_defender_exclusion
    ok, msg = ensure_defender_exclusion([exe, same, "", None])

    assert ok is True
    assert msg == "exclusion added for 1 path(s)"
    assert calls[0].count("Add-MpPreference") == 1
    assert _record(config) == [os.path.abspath(exe)]


def test_adding_a_second_path_keeps_the_one_already_recorded(config, shell,
                                                            tmp_path):
    """A portable copy run from another folder adds a SECOND exclusion while
    the first still exists. Replacing the record here would orphan the earlier
    path: the record is the only thing that can name it again."""
    calls, _ = shell
    old = str(tmp_path / "old" / "CleanerTool.exe")
    new = str(tmp_path / "new" / "CleanerTool.exe")
    _set_record([old])

    from app.elevation import ensure_defender_exclusion
    assert ensure_defender_exclusion([new])[0] is True

    assert _record(config) == sorted([old, new])
    assert new in calls[0] and old not in calls[0], (
        "only the new path is asked for; the old one is left alone")


def test_nothing_to_exclude_means_no_shell_call(config, shell, monkeypatch):
    calls, _ = shell
    _set_record([])

    import app.elevation as el
    monkeypatch.setattr(el, "_current_exe_path", lambda: "")

    ok, msg = el.ensure_defender_exclusion()

    assert ok is False
    assert msg == "no path to exclude"
    assert calls == []


def test_without_admin_the_add_touches_nothing(config, shell, monkeypatch,
                                              tmp_path):
    calls, _ = shell
    _set_record([])

    import app.elevation as el
    monkeypatch.setattr(el, "is_admin", lambda: False)
    monkeypatch.setattr(el, "_current_exe_path",
                        lambda: str(tmp_path / "CleanerTool.exe"))

    ok, msg = el.ensure_defender_exclusion()

    assert ok is False
    assert msg == "not running as administrator"
    assert calls == []
    assert _record(config) == []


def test_a_quote_in_a_path_cannot_break_out_of_the_script(config, shell):
    """The path is embedded in a PowerShell string literal; `_ps_quote` doubles
    the quote so a folder named `It's` cannot end the literal early."""
    calls, _ = shell
    _set_record([r"C:\It's\Tool.exe"])

    from app.elevation import remove_defender_exclusions
    assert remove_defender_exclusions()[0] is True

    assert "'C:\\It''s\\Tool.exe'" in calls[0]


def test_the_real_exe_resolution_decides_what_the_add_excludes(config, shell,
                                                             monkeypatch, tmp_path):
    """No stub on `_current_exe_path` here: this is the seam every other test in
    this file replaces, so it is the one place the real resolution meets the
    real exclusion. A frozen build reports its own .exe, and that — not the
    folder it happens to sit in — is what is asked for and recorded."""
    calls, _ = shell
    _set_record([])
    exe = str(tmp_path / "Apps" / "CleanerTool.exe")
    folder = os.path.dirname(exe)

    import app.elevation as el
    monkeypatch.setattr(el.sys, "frozen", True, raising=False)
    monkeypatch.setattr(el.sys, "executable", exe)
    monkeypatch.setattr(el.sys, "argv", [str(tmp_path / "main.py")])

    ok, msg = el.ensure_defender_exclusion()

    assert ok is True
    assert msg == "exclusion added for 1 path(s)"
    assert calls[0].count("Add-MpPreference") == 1
    assert f"-ExclusionPath '{exe}'" in calls[0]
    assert f"-ExclusionPath '{folder}'" not in calls[0], "never the folder"
    assert _record(config) == [exe]


def test_what_the_add_records_is_what_the_removal_asks_for(config, shell,
                                                         monkeypatch, tmp_path):
    """Add, then turn the preference off — both through the real path
    resolution. The string the add records has to be the string the removal
    then targets, or unopting leaves an exclusion nothing can name again."""
    calls, _ = shell
    _set_record([])
    exe = str(tmp_path / "Apps" / "CleanerTool.exe")

    import app.elevation as el
    monkeypatch.setattr(el.sys, "frozen", True, raising=False)
    monkeypatch.setattr(el.sys, "executable", exe)
    monkeypatch.setattr(el.sys, "argv", [str(tmp_path / "main.py")])

    assert el.ensure_defender_exclusion()[0] is True
    _set_pref(False)
    el.apply_defender_exclusion_if_wanted()

    assert len(calls) == 2, "one Add-, then one Remove-"
    assert "Remove-MpPreference" in calls[1]
    assert f"-ExclusionPath '{exe}'" in calls[1], "the same path came back"
    assert _record(config) == []


# --------------------------------------------------------------------------- #
# apply_defender_exclusion_if_wanted: the preference, end to end. This is the
# only caller of both paths, so what it does here is what "untick the box"
# actually means to the user.
# --------------------------------------------------------------------------- #

def _set_pref(on):
    from app.config_persist import update_config

    def _mut(c):
        c["add_defender_exclusion"] = on

    update_config(_mut)


def test_turning_the_preference_off_removes_what_was_recorded(config, shell):
    calls, _ = shell
    paths = [r"C:\Apps\CleanerTool.exe", r"D:\Portable\CleanerTool.exe"]
    _set_record(paths)
    _set_pref(False)

    from app.elevation import apply_defender_exclusion_if_wanted
    assert apply_defender_exclusion_if_wanted() is None

    assert len(calls) == 1, "one command for both paths"
    assert calls[0].count("Remove-MpPreference") == 2
    assert "Add-MpPreference" not in calls[0]
    assert _record(config) == [], "the decision is now fully reversed"


def test_turning_the_preference_off_with_nothing_recorded_runs_nothing(config,
                                                                     shell):
    """Nothing of ours to undo: no PowerShell, no record churn."""
    calls, _ = shell
    _set_record([])
    _set_pref(False)

    from app.elevation import apply_defender_exclusion_if_wanted
    apply_defender_exclusion_if_wanted()

    assert calls == []
    assert _record(config) == []


def test_turning_the_preference_off_leaves_an_unrecorded_folder_alone(
        config, shell, monkeypatch, tmp_path):
    """The folder an older build excluded is not in the record, so this must
    not delete it: only the migration notice may, and only after the user
    confirms. Unticking the box is not consent to clear a folder-wide
    exclusion the app cannot prove it made."""
    calls, _ = shell
    folder = str(tmp_path / "Apps")
    _set_record([])
    _set_pref(False)

    import app.elevation as el
    monkeypatch.setattr(el, "list_defender_exclusions", lambda: [folder])

    el.apply_defender_exclusion_if_wanted()

    assert calls == []
    assert _record(config) == []


def test_turning_the_preference_on_excludes_the_exe_and_never_the_folder(
        config, shell, monkeypatch, tmp_path):
    """Opting in excludes this app's binary — one path — even when Defender
    still holds the whole-folder exclusion an older build added. The old folder
    is neither re-added nor removed; it is the migration notice's question."""
    calls, _ = shell
    exe = str(tmp_path / "Apps" / "CleanerTool.exe")
    folder = os.path.dirname(exe)
    _set_record([])
    _set_pref(True)

    import app.elevation as el
    monkeypatch.setattr(el, "_current_exe_path", lambda: exe)
    monkeypatch.setattr(el, "list_defender_exclusions", lambda: [folder])

    el.apply_defender_exclusion_if_wanted()

    assert len(calls) == 1
    assert calls[0].count("Add-MpPreference") == 1
    assert f"-ExclusionPath '{exe}'" in calls[0]
    assert f"-ExclusionPath '{folder}'" not in calls[0], "never the folder"
    assert "Remove-MpPreference" not in calls[0], "opting in removes nothing"
    assert _record(config) == [exe]


def test_a_failed_removal_is_retried_on_the_next_elevated_launch(config,
                                                                shell):
    """Defender blocked the removal this time; the record must survive so the
    next launch tries again, and the retry must then clear it."""
    calls, state = shell
    path = r"C:\Apps\CleanerTool.exe"
    _set_record([path])
    _set_pref(False)

    import app.elevation as el

    state["result"] = (False, "access denied")
    el.apply_defender_exclusion_if_wanted()
    assert _record(config) == [path], "still ours to remove"

    state["result"] = (True, "")
    el.apply_defender_exclusion_if_wanted()
    assert len(calls) == 2, "the second launch tried again"
    assert _record(config) == []


def test_a_raising_removal_does_not_escape_into_the_launch(config, shell,
                                                           monkeypatch):
    calls, _ = shell
    path = r"C:\Apps\CleanerTool.exe"
    _set_record([path])
    _set_pref(False)

    import app.elevation as el

    def boom(script):
        calls.append(script)
        raise RuntimeError("powershell went away")

    monkeypatch.setattr(el, "_run_mp_script", boom)

    el.apply_defender_exclusion_if_wanted()          # must not raise
    assert len(calls) == 1
    assert _record(config) == [path]


def test_without_admin_the_preference_changes_nothing(config, shell, monkeypatch,
                                                     tmp_path):
    """A normal (unelevated) launch cannot touch Defender either way, and must
    not corrupt the record while failing to act on it."""
    calls, _ = shell
    old = r"C:\Apps\CleanerTool.exe"
    _set_record([old])
    _set_pref(True)

    import app.elevation as el
    monkeypatch.setattr(el, "is_admin", lambda: False)
    monkeypatch.setattr(el, "_current_exe_path",
                        lambda: str(tmp_path / "CleanerTool.exe"))

    el.apply_defender_exclusion_if_wanted()

    assert calls == []
    assert _record(config) == [old], "the pending removal is still pending"


def test_a_hand_edited_value_cannot_switch_the_preference_on(config, shell,
                                                            monkeypatch,
                                                            tmp_path):
    """`config_schema` coerces a non-bool to the default (off), so hand-editing
    `"yes"` into config.json must not quietly recreate an exclusion the user
    never opted into."""
    calls, _ = shell
    config.write_text(json.dumps({"add_defender_exclusion": "yes"}),
                      encoding="utf-8")

    import app.elevation as el
    monkeypatch.setattr(el, "_current_exe_path",
                        lambda: str(tmp_path / "CleanerTool.exe"))

    el.apply_defender_exclusion_if_wanted()

    assert calls == [], "a string is not a boolean opt-in"
    assert _record(config) is None


# --------------------------------------------------------------------------- #
# find_stale_folder_exclusion: the SEC-004 migration notice. It only ever
# REPORTS an exclusion an older build applied; the removal is a separate call
# the user makes by clicking. Reporting must therefore be harmless, because it
# runs on an ordinary gate screen before anything is elevated.
# --------------------------------------------------------------------------- #

def test_it_reports_the_parent_folder_defender_has_excluded(config, shell,
                                                           monkeypatch, tmp_path):
    calls, state = shell
    exe = str(tmp_path / "Apps" / "CleanerTool.exe")
    folder = os.path.dirname(exe)
    _set_record([])
    state["result"] = (True, folder)          # what Get-MpPreference prints

    import app.elevation as el
    monkeypatch.setattr(el, "_current_exe_path", lambda: exe)

    assert el.find_stale_folder_exclusion() == folder
    assert len(calls) == 1


def test_it_never_removes_the_folder_it_reports(config, shell, monkeypatch,
                                                tmp_path):
    """The whole point of the notice: report, do not act. Even with the folder
    sitting in our own record, finding it must leave Defender and the record
    exactly as they were."""
    calls, state = shell
    exe = str(tmp_path / "Apps" / "CleanerTool.exe")
    folder = os.path.dirname(exe)
    _set_record([folder])
    state["result"] = (True, folder)

    import app.elevation as el
    monkeypatch.setattr(el, "_current_exe_path", lambda: exe)

    assert el.find_stale_folder_exclusion() == folder

    assert len(calls) == 1, "one read, nothing else"
    assert "Get-MpPreference" in calls[0]
    assert "Remove-MpPreference" not in calls[0], "reporting must not delete"
    assert "Add-MpPreference" not in calls[0]
    assert _record(config) == [folder], "nothing was removed, so nothing is pruned"


def test_only_the_exe_excluded_is_not_a_stale_folder(config, shell,
                                                    monkeypatch, tmp_path):
    """The common, already-correct install: the app's binary is excluded and
    its folder is not. Nothing to report, so nobody is asked."""
    calls, state = shell
    exe = str(tmp_path / "Apps" / "CleanerTool.exe")
    _set_record([exe])
    state["result"] = (True, exe)

    import app.elevation as el
    monkeypatch.setattr(el, "_current_exe_path", lambda: exe)

    assert el.find_stale_folder_exclusion() == ""
    assert "Remove-MpPreference" not in calls[0]


def test_a_trailing_separator_in_defenders_list_still_matches(config, shell,
                                                            monkeypatch, tmp_path):
    """Defender happily stores `C:\\Apps\\`; that is the same exclusion as
    `C:\\Apps`, and it must not go unnoticed."""
    _, state = shell
    exe = str(tmp_path / "Apps" / "CleanerTool.exe")
    folder = os.path.dirname(exe)
    _set_record([])
    state["result"] = (True, folder + os.sep)

    import app.elevation as el
    monkeypatch.setattr(el, "_current_exe_path", lambda: exe)

    assert el.find_stale_folder_exclusion() == folder


def test_a_parent_of_the_parent_or_a_sibling_is_not_reported(config, shell,
                                                            monkeypatch, tmp_path):
    """Only the exact containing folder counts. Excluding `C:\\Apps` when the
    app lives in `C:\\Apps\\Cleaner` is a different, broader decision the app
    must not offer to undo on the user's behalf."""
    _, state = shell
    exe = str(tmp_path / "Apps" / "Cleaner" / "CleanerTool.exe")
    outside = [str(tmp_path / "Apps"), str(tmp_path / "Apps" / "Other")]
    _set_record([])
    state["result"] = (True, "\n".join(outside))

    import app.elevation as el
    monkeypatch.setattr(el, "_current_exe_path", lambda: exe)

    assert el.find_stale_folder_exclusion() == ""


def test_a_root_folder_is_never_reported(config, shell, monkeypatch):
    """Portable copies get dropped next to a drive root. Offering to remove an
    exclusion for the whole drive would be catastrophic, so the root is never
    a candidate — even though Defender really does list it."""
    calls, state = shell
    root = os.path.abspath(os.sep)
    exe = os.path.join(root, "CleanerTool.exe")
    _set_record([])
    state["result"] = (True, root)

    import app.elevation as el
    monkeypatch.setattr(el, "_current_exe_path", lambda: exe)

    assert el.find_stale_folder_exclusion() == ""
    assert calls == [], "not even a needless Defender read"


def test_no_exe_path_means_nothing_to_report(config, shell, monkeypatch):
    calls, _ = shell
    _set_record([])

    import app.elevation as el
    monkeypatch.setattr(el, "_current_exe_path", lambda: "")

    assert el.find_stale_folder_exclusion() == ""
    assert calls == []


def test_an_unreadable_exclusion_list_reports_nothing(config, shell,
                                                     monkeypatch, tmp_path):
    """Get-MpPreference failing (Defender off, policy, no admin) must read as
    "nothing to report", not as an error on a gate screen."""
    _, state = shell
    _set_record([])
    state["result"] = (False, "Get-MpPreference : Access is denied")

    import app.elevation as el
    monkeypatch.setattr(el, "_current_exe_path",
                        lambda: str(tmp_path / "Apps" / "CleanerTool.exe"))

    assert el.find_stale_folder_exclusion() == ""


def test_a_raising_list_reports_nothing(config, shell, monkeypatch, tmp_path):
    _set_record([])

    import app.elevation as el

    def boom():
        raise RuntimeError("powershell went away")

    monkeypatch.setattr(el, "_current_exe_path",
                        lambda: str(tmp_path / "Apps" / "CleanerTool.exe"))
    monkeypatch.setattr(el, "list_defender_exclusions", boom)

    assert el.find_stale_folder_exclusion() == ""       # must not raise
