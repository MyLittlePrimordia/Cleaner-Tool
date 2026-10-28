"""The SEC-001 non-task ids are declared in place, not guessed from spelling.

`dev/tools/verify_sec001_task_allowlist.py` reports every allowlist id that is not a
Tweak task key, because those are the entries a reviewer has to be able to
explain. It used to decide which ones were "expected" by looking at the id's
SUFFIX - `_regs`, `_pmz`, `_specs` - which is a naming convention pretending to
be a security judgement: a typo'd or invented entry that happened to end in one
of those was silently fine.

Now each such id is declared beside the allowlist table with a one-sentence
reason, and the guard prints it on every run. These tests keep both halves
honest: the declarations must be complete and reasoned, and the tool must fail
on a declaration that has gone stale.
"""
from __future__ import annotations

import pytest

import dev.tools.verify_sec001_task_allowlist as guard
from app.tasks import tweak_tasks as T


@pytest.fixture
def declared():
    table = getattr(T, "_TASK_REG_ALLOWLIST_NOT_A_TASK", None)
    assert table, "the declarations table is missing"
    return table


# --------------------------------------------------------------------------- #
# The declarations themselves
# --------------------------------------------------------------------------- #

def test_every_declared_id_is_in_the_allowlist(declared):
    stray = sorted(set(declared) - set(T._TASK_REG_ALLOWED_NORM))
    assert stray == [], (
        "a declaration outlived its allowlist entry; the guard fails on this")


def test_every_declaration_says_why(declared):
    empty = sorted(k for k, reason in declared.items()
                   if not str(reason or "").strip())
    assert empty == [], "an exception without a reason is not a documented one"


def test_every_declared_id_is_really_not_a_task_key(declared):
    """A declaration for something that IS a task key is dead weight - it would
    never be printed, so it would never be reviewed."""
    assert set(declared) & guard._task_keys() == set()


def test_each_declaration_is_at_most_one_sentence(declared):
    for tid, reason in declared.items():
        assert len(reason) <= 120, f"{tid}: keep the reason to one line"


# --------------------------------------------------------------------------- #
# The guard uses them
# --------------------------------------------------------------------------- #

def test_the_report_prints_each_reason(declared, capsys):
    assert guard.main() == 0
    out = capsys.readouterr().out
    for tid, reason in declared.items():
        assert tid in out and reason in out, f"{tid} was not reported with its reason"


def test_an_undeclared_non_task_id_is_a_warning(monkeypatch, capsys):
    """Drop a declaration: the id must not vanish quietly, it must be flagged
    for review - and it must not become a note just because it has a suffix.

    The three inputs that would otherwise explain it away are stubbed out: a
    real task key, a spec set the scanner derives, and the cut-task list."""
    table = dict(getattr(T, "_TASK_REG_ALLOWLIST_NOT_A_TASK"))
    dropped = sorted(table)[0]
    table.pop(dropped)
    monkeypatch.setattr(T, "_TASK_REG_ALLOWLIST_NOT_A_TASK", table)
    monkeypatch.setattr(guard, "_task_keys",
                        lambda: set(T._TASK_REG_ALLOWED_NORM) - {dropped})
    monkeypatch.setattr(guard, "_scan_source", lambda: {})
    monkeypatch.setattr(guard, "CUT_TASK_KEYS", set())

    assert guard.main() == 0
    out = capsys.readouterr().out
    assert f"allowlist id '{dropped}'" in out
    assert "declare it in _TASK_REG_ALLOWLIST_NOT_A_TASK" in out


def test_a_stale_declaration_fails_the_check(monkeypatch, capsys):
    table = dict(getattr(T, "_TASK_REG_ALLOWLIST_NOT_A_TASK"))
    table["no_such_id_regs"] = "a typo nobody noticed"
    monkeypatch.setattr(T, "_TASK_REG_ALLOWLIST_NOT_A_TASK", table)

    assert guard.main() == 1
    assert "not in the allowlist any more" in capsys.readouterr().out


def test_a_declaration_without_a_reason_fails_the_check(monkeypatch, capsys):
    table = dict(getattr(T, "_TASK_REG_ALLOWLIST_NOT_A_TASK"))
    first = sorted(table)[0]
    table[first] = "   "
    monkeypatch.setattr(T, "_TASK_REG_ALLOWLIST_NOT_A_TASK", table)

    assert guard.main() == 1
    assert "has no reason" in capsys.readouterr().out


def test_the_suffix_heuristic_is_gone():
    """The old rule lived in this module as a tuple of suffixes. If it comes
    back, so does the hole: a new id ending in `_regs` would be 'expected'."""
    assert not hasattr(guard, "_INTERNAL_ID_SUFFIXES")
