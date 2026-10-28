"""Stage 5 gate, one level below dev/tests/test_module_reachability.py.

That guard catches a module nothing imports (`app/smartcare.py`: 424 lines of
orchestration that kept passing its own tests for a year after its last entry
point was deleted). This one catches the same rot one level down: a function,
method or module-level constant whose last caller went with a screen or a
button. Deleting a def routinely orphans the constant it was the last reader of,
which is why both levels are read here.

`dev/tools/deadcode_report.py` does the reading - source only, never an import,
because importing a dialog here opens windows and shells out to PowerShell. This
file is what makes it a GATE: the report is run in the suite, the buckets that
mean "dead" must be empty, and the one escape hatch (a constant with a written
reason) is checked in both directions - the name must still exist, still be
unread, and still carry a reason. An exemption that hides nothing is a stale
promise and fails like any other finding.

Measured when this gate went in (2026-10-27): 3038 defs under `app/`, 0 nowhere,
0 test-only, 0 unread constants, 7 constants kept with a written reason, and the
two `set_hint` shapes below.
"""
from __future__ import annotations

import dev.tools.deadcode_report as deadcode

#: The named-shape bucket: the same name defined in two or more widgets, with no
#: call site. Two definitions is a SHAPE - an API both footers implement - and a
#: missing caller says nothing about it. Each entry needs its reason; anything
#: else landing in this bucket is a finding, not a shape.
_PROTOCOL_SHAPES = {
    "set_hint": "the same hint API on ModalFooter and PageActionBar",
}

#: A reading that saw nothing would satisfy every assertion below, so the gate
#: pins the size of the tree it read. The floor is far enough under the real
#: count (~3040) to tolerate honest churn and still catch a broken walk.
_DEF_FLOOR = 2500


def _report():
    found, total = deadcode.report()
    assert total >= _DEF_FLOOR, (
        "the report read %d defs under app/; it is not looking at the tree"
        % total)
    return found


def test_no_def_under_app_is_unreachable_or_tests_only():
    """A test that exercises a method the app never calls is coverage of dead
    API: it reads as health in every report the suite produces."""
    found = _report()
    assert found["nowhere"] == [], (
        "no Python file mentions these, so nothing can run them - delete them, "
        "or wire them up:\n  " + "\n  ".join(
            "%s:%d %s" % (rel, line, qual)
            for rel, qual, line, _kind in sorted(found["nowhere"])))
    assert found["test_only"] == [], (
        "only the tests use these; the app has no caller - delete them WITH the "
        "test that kept them alive, or wire them:\n  " + "\n  ".join(
            "%s:%d %s" % (rel, line, qual)
            for rel, qual, line, _kind in sorted(found["test_only"])))


def test_the_only_duplicated_names_are_the_documented_shapes():
    names = sorted({qual.rpartition(".")[2]
                    for _rel, qual, _line in sorted(_report()["protocol"])})
    unknown = [n for n in names if n not in _PROTOCOL_SHAPES]
    assert not unknown, (
        "these names are defined in more than one place with no caller. A shape "
        "(an API two widgets both implement) belongs in _PROTOCOL_SHAPES with "
        "its reason; a leftover belongs in the bin:\n  " + "\n  ".join(unknown))
    stale = [n for n in _PROTOCOL_SHAPES if n not in names]
    assert not stale, (
        "these are listed as shapes but no longer appear twice: %s"
        % ", ".join(sorted(stale)))


def test_no_constant_under_app_is_unread_or_read_only_by_tests():
    """The same reading one level up: `NIM_MODIFY` outlived the tooltip setter
    that read it, `CLIPBOARD_COLUMNS` outlived its exporter."""
    found = _report()
    assert found["constants"] == [], (
        "nothing reads these module-level names:\n  " + "\n  ".join(
            "%s:%d %s" % row for row in sorted(found["constants"])))
    assert found["constants_test_only"] == [], (
        "only the tests read these; a token the app paints its own literal for "
        "reads as health while nothing protects it:\n  " + "\n  ".join(
            "%s:%d %s" % row for row in sorted(found["constants_test_only"])))


def test_every_constant_exemption_exists_and_says_why():
    exempt = _report()["constants_exempt"]
    assert exempt, ("the exemption table is empty; delete it rather than leave "
                    "a hatch nobody uses")
    for rel, name, line, why in sorted(exempt):
        assert isinstance(why, str) and why.strip(), (
            "%s:%d %s is exempt without a reason" % (rel, line, name))


def test_an_exemption_covers_a_name_that_is_still_unread(monkeypatch):
    """The hatch has to expire. Emptying the table must make every exempt name
    reappear as a finding: if one does not, its key points at a file or name
    that moved (stale), or something reads it now (the exemption is hiding a
    live name)."""
    exempt = {(rel, name)
              for rel, name, _line, _why in _report()["constants_exempt"]}
    monkeypatch.setattr(deadcode, "_EXEMPT_CONSTANTS", {})
    unread = {(row[0], row[1]) for row in deadcode.report()[0]["constants"]}
    stale = sorted(exempt - unread)
    assert not stale, (
        "these exemptions no longer hide anything, so they are either stale or "
        "hiding a name that is read again:\n  " + "\n  ".join(
            "%s:%s" % pair for pair in stale))


def test_the_gate_fires_on_a_planted_dead_def_and_constant(tmp_path, monkeypatch):
    """The negative test: a tree with one dead def and one unread constant must
    land in exactly the buckets the assertions above reject."""
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "orphan.py").write_text(
        "DEAD_CONST = 1\n\n\ndef nobody_calls_me():\n    return 1\n\n\n"
        "class Panel:\n    def set_something(self, _value):\n        return 2\n",
        encoding="utf-8")
    monkeypatch.setattr(deadcode, "ROOT", str(tmp_path))
    found, total = deadcode.report()
    assert total == 2, total            # the module-level def and the method
    assert [row[1] for row in found["nowhere"]] == ["nobody_calls_me",
                                                    "Panel.set_something"]
    assert [row[1] for row in found["constants"]] == ["DEAD_CONST"]


def test_a_def_only_the_tests_use_is_a_finding_not_a_pass(tmp_path, monkeypatch):
    """"Only the tests use it" is the finding, not the excuse - the semantic
    that hid the 2026-10 orphans from every other report."""
    (tmp_path / "app").mkdir()
    (tmp_path / "dev" / "tests").mkdir(parents=True)
    (tmp_path / "app" / "helper.py").write_text(
        "def only_tests_call_me():\n    return 1\n", encoding="utf-8")
    (tmp_path / "dev" / "tests" / "test_helper.py").write_text(
        "from app.helper import only_tests_call_me\n\n\n"
        "def test_it():\n    assert only_tests_call_me() == 1\n", encoding="utf-8")
    monkeypatch.setattr(deadcode, "ROOT", str(tmp_path))
    found, _total = deadcode.report()
    assert found["nowhere"] == []
    assert [row[1] for row in found["test_only"]] == ["only_tests_call_me"]
