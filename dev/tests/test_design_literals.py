"""The design-literal gate: one report for radius, spacing, icon and font.

Four guards used to answer the same question in four places - radius, spacing
and icon scans in this suite's token file plus a font-tuple scan - each with its
own walk, its own allowlist and its own negative test. They are now one scanner
(`dev/tools/design_literals.py`) and one failing test, because "did this call site
pick a number where the design system has a name?" is one question.

The rules, so a new widget knows what it owes:

    radius    a SHAPE_*/RADIUS_* role, never `radius=16`
    spacing   a SPACE_* rung or a named metric, never `padx=10`
    icon      a rung of theme.ICON_*, never `icon_target_px(24, …)`
    font      a role from theme.TYPE, never `font=(F, 9)`

A finding names the token to reach for, so the failure is an instruction. The
negative tests below plant one module carrying every offending shape and are
what prove the gate is reading, not merely quiet.
"""
from __future__ import annotations

import dev.tools.design_literals as dl

#: A gate that scanned nothing would pass every assertion in this file. The UI
#: package is ~70 modules; the floor only has to catch a broken walk.
_UI_FILES_FLOOR = 60

#: One module with every shape the gate exists for. The expectation is by line
#: so a rule that starts or stops firing is visible in the diff.
_PLANTED = (
    "CARD_ICON_PX = 36\n"                       # 1  icon
    "\n"
    "\n"
    "class Row:\n"
    "    _ICON_SLOT_W = 32\n"                   # 5  icon
    "    PAD = 13\n"                            # 6  spacing
    "\n"
    "\n"
    "def build(parent):\n"
    "    card = RoundedCard(parent, fill=S, radius=16)\n"   # 10 radius
    "    lab = Label(parent, text=\"x\", font=(F, 9))\n"    # 11 font
    "    zero = Frame(parent, padx=0, pady=(0, 0))\n"       # 12 allowed
    "    icon = icon_target_px(24, parent)\n"              # 13 icon
    "    status = StatusIcon(parent, size=20)\n"           # 14 icon
    "    return Frame(parent, padx=10, pady=(8, 2))\n"      # 15 spacing
)

_PLANTED_EXPECTED = [
    ("icon", 1, "CARD_ICON_PX=36"),
    ("icon", 5, "_ICON_SLOT_W=32"),
    ("spacing", 6, "PAD=13"),
    ("radius", 10, "radius=16"),
    ("font", 11, "(F, 9)"),
    ("icon", 13, "icon_target_px(...)"),
    ("icon", 14, "StatusIcon(size=20)"),
    ("spacing", 15, "padx=[10]"),
    ("spacing", 15, "pady=[8, 2]"),
]


def _planted(tmp_path, text=None, name="panel.py"):
    (tmp_path / name).write_text(text or _PLANTED, encoding="utf-8")
    return dl.scan(str(tmp_path))


def test_the_gate_is_reading_the_ui_tree():
    files = list((dl.ROOT / "app" / "ui").rglob("*.py"))
    assert len(files) >= _UI_FILES_FLOOR, (
        "the gate found %d modules under app/ui; it is not looking at the "
        "product" % len(files))


def test_no_design_literal_is_written_where_a_token_exists():
    grouped = dl.report()
    total = sum(len(rows) for rows in grouped.values())
    if not total:
        return
    lines = ["%d design literal(s) that already have a name:" % total]
    for rule, rows in grouped.items():
        if not rows:
            continue
        lines.append("  %s - %s" % (rule.upper(), dl.REMEDY[rule]))
        lines.extend("    %s\n        %s" % (found, found.source)
                     for found in rows)
    raise AssertionError("\n".join(lines))


def test_every_rule_declares_the_tree_it_governs():
    """A rule with no scope would scan nothing and always pass."""
    assert set(dl.SCOPE) == set(dl.REMEDY), (
        sorted(set(dl.REMEDY) - set(dl.SCOPE)))


def test_the_gate_sees_every_rule_in_one_planted_module(tmp_path):
    found = [(f.rule, f.line, f.detail) for f in _planted(tmp_path)]
    assert found == _PLANTED_EXPECTED, found


def test_zero_is_absence_not_a_measurement(tmp_path):
    """`padx=0` and a `PAD = 0` clear a gap; they do not measure one."""
    text = "a = Frame(p, padx=0, pady=(0, 0))\n\n\nclass R:\n    PAD = 0\n"
    assert _planted(tmp_path, text) == []


def test_the_declaration_files_are_where_numbers_are_the_tokens(tmp_path):
    """A value in theme.py/metrics.py IS the token, not a bypass of one."""
    text = "RADIUS = 16\nCARD_ICON_PX = 36\nPAGE_PAD = 24\nLADDER = (F, 9)\n"
    assert _planted(tmp_path, text, name="theme.py") == []
    assert _planted(tmp_path, text, name="metrics.py") == []


def test_a_font_tuple_in_prose_is_not_a_call_site(tmp_path):
    """The font rule reads tokens, so the several docstrings that explain the
    old sizes (`(F, 9)` in a comment) are not failures while the real tuple is.
    """
    text = ('# rows used (F, 9) before the type scale existed\n'
            '"""and (FM, 8) in prose."""\n'
            'label = Label(p, text="x", font=(F, 9))\n')
    found = _planted(tmp_path, text)
    assert [(f.rule, f.line) for f in found] == [("font", 3)], found


def test_the_report_groups_by_rule_and_names_the_remedy(tmp_path):
    _planted(tmp_path)
    grouped = dl.report(str(tmp_path))
    assert set(grouped) == set(dl.REMEDY)
    assert [len(grouped[r]) for r in ("radius", "spacing", "icon", "font")] == \
        [1, 3, 4, 1]
    assert all(dl.REMEDY[r] for r in grouped), "a rule has no remedy text"
