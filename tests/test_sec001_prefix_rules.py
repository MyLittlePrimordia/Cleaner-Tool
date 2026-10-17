"""Regression tests for SEC-001 PREFIX-rule verification (roadmap 4.1).

Why this exists. One tweak cannot be allowlisted as a literal triple:
`disable_nagle` enumerates the network adapters and writes a *different* subkey
per interface GUID, so its registry path is built at run time. Its SEC-001
declaration therefore lives in `_TASK_REG_ALLOW_PREFIX` — a PREFIX rule — and
that rule is the security control: it is what stops a tampered `config.json`
from pointing a snapshot restore at a value nobody declared.

That table used to be the end of the guard's reach. The guard `continue`d past
prefix-ruled tasks (and the scanner cannot even see them, because their spec
list is built at run time), so the table was "verified by hand": add a third
value name to the write loop and nothing noticed — and that name is exactly
what a tampered snapshot could redirect.

`tools/verify_sec001_task_allowlist.py::_verify_prefix_rules` closes it by
reading the SOURCE: it finds the apply function that feeds the task id to
`_regn_apply`, requires the rule's path and hive to appear as literals there,
and requires the set of value names that function iterates to equal the set the
rule declares. It fails CLOSED — a refactor that hides the names is a failure,
not a skip, so the hole cannot silently reopen.

The Tweak preview (app/tweak_introspect.py, roadmap 4.1) reads the same table,
so the user is told what a prefix-ruled tweak will touch instead of "no
registry detail to preview".

Everything here is side-effect free: the only registry calls are reads, and the
drift cases run against a copy of the source in tmp_path, never the machine.
"""
from __future__ import annotations

import app.tweak_introspect as ti
import tools.verify_sec001_task_allowlist as guard
from app.tasks import advanced_tasks, tweak_tasks

#: The substring the guard uses for the one real prefix-ruled task. Kept in one
#: place so a rename is a one-line change here rather than three.
_PREFIX_TASK = "disable_nagle"
_PATH_LEAF = "Interfaces"
_NAME_LOOP = '("TcpAckFrequency", "TCPNoDelay")'


def _drifted_copy(tmp_path, before, after):
    """A copy of tweak_tasks.py with one exact edit; asserts it applied once."""
    src = open(tweak_tasks.__file__, encoding="utf-8").read()
    assert src.count(before) == 1, (
        "the source no longer contains %r exactly once, so this test (and the "
        "guard's assumption about the write loop) needs updating" % before)
    out = tmp_path / "tweak_tasks_drifted.py"
    out.write_text(src.replace(before, after), encoding="utf-8")
    return str(out)


# --------------------------------------------------------------------------- #
# the table is the single source of truth
# --------------------------------------------------------------------------- #

def test_every_prefix_entry_is_a_hive_path_name_triple():
    """`preview_lines` unpacks each entry as `for hive, path, name in ...`, so a
    four-element entry would raise at preview time rather than at declaration."""
    for tid, specs in tweak_tasks._TASK_REG_ALLOW_PREFIX.items():
        assert specs, f"{tid!r} has an empty prefix rule"
        for spec in specs:
            assert len(spec) == 3, f"{tid!r}: {spec!r} is not a triple"


def test_the_preview_reads_the_same_prefix_table_as_the_guard():
    ti.reset_cache()
    try:
        assert ti.prefix_specs() == {
            k: set(v) for k, v in tweak_tasks._TASK_REG_ALLOW_PREFIX.items()}
    finally:
        ti.reset_cache()


# --------------------------------------------------------------------------- #
# the real source passes the check
# --------------------------------------------------------------------------- #

def test_prefix_rules_verify_clean_against_the_real_source():
    failures = []
    verified = guard._verify_prefix_rules(
        (tweak_tasks.__file__, advanced_tasks.__file__), failures)
    assert failures == []
    expected = sum(len(v)
                   for v in tweak_tasks._TASK_REG_ALLOW_PREFIX.values())
    assert expected > 0
    assert verified == expected, (
        "every declared prefix entry must be verified, not skipped")


# --------------------------------------------------------------------------- #
# drift is caught, and the check fails closed
# --------------------------------------------------------------------------- #

def test_a_new_value_name_in_the_write_loop_is_a_failure(tmp_path):
    path = _drifted_copy(
        tmp_path, _NAME_LOOP,
        '("TcpAckFrequency", "TCPNoDelay", "Tcp1323Opts")')
    failures = []
    guard._verify_prefix_rules((path,), failures)
    joined = "\n".join(failures)
    assert "PREFIX DRIFT" in joined
    assert "Tcp1323Opts" in joined, (
        "a value name written under a run-time path must not be able to slip "
        "past the prefix rule: %r" % failures)


def test_a_stale_rule_entry_is_a_failure(tmp_path):
    """The other direction: a rule naming a value the source dropped."""
    # Remove TCPNoDelay from the write loop but leave the rule alone.
    path = _drifted_copy(tmp_path, _NAME_LOOP, '("TcpAckFrequency",)')
    failures = []
    guard._verify_prefix_rules((path,), failures)
    assert any("TCPNoDelay" in f for f in failures), failures


def test_the_check_fails_closed_if_the_name_loop_is_refactored(tmp_path):
    """Moving the names into a constant must be a hard failure, never a silent
    pass — otherwise a refactor reopens the hole the check was added to close."""
    path = _drifted_copy(tmp_path, 'for name in ' + _NAME_LOOP + ':',
                         "for name in INTERFACE_VALUE_NAMES:")
    failures = []
    guard._verify_prefix_rules((path,), failures)
    assert any("cannot be verified" in f for f in failures), failures


def test_a_rule_naming_a_path_the_source_never_mentions_is_a_failure(monkeypatch):
    monkeypatch.setitem(
        tweak_tasks._TASK_REG_ALLOW_PREFIX, _PREFIX_TASK,
        (("HKLM",
          r"SYSTEM\CurrentControlSet\Services\Tcpip\Parameters\Interfaces",
          "TcpAckFrequency"),
         ("HKLM", r"SYSTEM\CurrentControlSet\Something\Else", "TCPNoDelay")))
    failures = []
    guard._verify_prefix_rules((tweak_tasks.__file__,), failures)
    assert any("never mentions it" in f for f in failures), failures


def test_a_rule_whose_apply_function_is_gone_is_a_failure(monkeypatch):
    monkeypatch.setitem(tweak_tasks._TASK_REG_ALLOW_PREFIX, "no_such_task",
                        (("HKLM", "Anywhere", "Anything"),))
    failures = []
    guard._verify_prefix_rules((tweak_tasks.__file__,), failures)
    assert any("no_such_task" in f for f in failures), failures


# --------------------------------------------------------------------------- #
# hand-transcribed entries (the scanner cannot derive taskbar_cleanup)
# --------------------------------------------------------------------------- #

def test_hand_written_entries_verify_clean_against_the_real_source():
    failures = []
    derived = guard._scan_source()
    verified = guard._verify_manual_rules(
        (tweak_tasks.__file__, advanced_tasks.__file__), derived, failures)
    assert failures == []
    expected = sum(len(v) for tid, v in
                   tweak_tasks._TASK_REG_ALLOWLIST_MANUAL.items()
                   if tid not in derived)
    assert expected > 0, ("this test is pointless if every manual entry is "
                          "derivable; drop it if that ever becomes true")
    assert verified == expected


def test_a_hand_written_entry_authorising_an_unwritten_path_is_a_failure(
        monkeypatch):
    """Over-granting is the security-relevant direction: an entry that names a
    location the task never writes is what a tampered snapshot could aim at."""
    real = tweak_tasks._TASK_REG_ALLOWLIST_MANUAL["taskbar_cleanup"]
    monkeypatch.setitem(
        tweak_tasks._TASK_REG_ALLOWLIST_MANUAL, "taskbar_cleanup",
        tuple(real) + (("HKCU", r"Software\Evil\Anywhere", "Redirect"),
                      ))
    failures = []
    guard._verify_manual_rules((tweak_tasks.__file__,),
                               guard._scan_source(), failures)
    assert any("Evil" in f or "Redirect" in f for f in failures), failures


def test_a_hand_written_entry_stale_against_the_source_is_a_failure(tmp_path):
    """If the source stops writing a declared value, the entry must fail."""
    write_line = ('"Software\\\\Microsoft\\\\Windows\\\\CurrentVersion\\\\'
                  'Policies\\\\Explorer", "HideSCAMeetNow"')
    path = _drifted_copy(tmp_path, write_line,
                         write_line.replace("HideSCAMeetNow",
                                            "HideSCAMeetNow_v2"))
    failures = []
    guard._verify_manual_rules((path,), guard._scan_source(), failures)
    assert any("HideSCAMeetNow" in f for f in failures), failures


# --------------------------------------------------------------------------- #
# the preview reports it honestly
# --------------------------------------------------------------------------- #

def test_preview_reports_a_prefix_ruled_task_instead_of_saying_nothing():
    ti.reset_cache()
    try:
        text = "\n".join(ti.preview_lines([_PREFIX_TASK]))
    finally:
        ti.reset_cache()
    assert "no registry detail to preview" not in text, (
        "a prefix-ruled tweak does have detail to preview; the rule names it")
    assert _PATH_LEAF in text
    assert "TcpAckFrequency" in text and "TCPNoDelay" in text
    assert "<each subkey>" in text, (
        "the preview must not invent a concrete subkey — one is created per "
        "adapter at apply time")


def test_preview_still_says_nothing_for_a_task_with_no_detail(monkeypatch):
    """The honest fallback must survive: a key the scan knows nothing about."""
    ti.reset_cache()
    try:
        monkeypatch.setattr(ti, "scan_tasks", lambda: {})
        text = "\n".join(ti.preview_lines(["some_unknown_tweak"]))
    finally:
        ti.reset_cache()
    assert "no registry detail to preview" in text
