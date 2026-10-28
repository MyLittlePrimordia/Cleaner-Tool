"""Optical component metrics: the sub-rung insets component geometry needs.

`theme.SPACE_*` is the page and card layout scale: a 4px grid, one rung per
layout gap. Text, chip and icon geometry does not always land on that grid -
a label needs a 2px vertical nudge, a chip needs 13px of side breathing room,
and rounding either to 12 or 16 moves pixels that were tuned on screen.

These names are deliberately NOT more SPACE rungs. Each one says what kind of
component geometry it belongs to, and a call site imports the name instead of
writing the number. Page/card layout must still choose `SPACE_*`; if a metric
here starts describing a page gap, it belongs in the scale instead.

The design guard in dev/tests/test_design_tokens.py rejects nonzero numeric
`padx`/`pady`/`ipadx`/`ipady` values and numeric GAP/PAD/INSET constants, so
adding a raw optical number fails the suite.
"""
from __future__ import annotations

#: One-pixel adjustment beside a border or inside a tight row.
HAIRLINE_NUDGE = 1

#: Two-pixel optical nudge used beside text, toggles and icons.
TEXT_NUDGE = 2

#: Three-pixel gap between tightly stacked labels.
TIGHT_GAP = 3

#: Five-pixel inset around a compact control.
COMPACT_INSET = 5

#: Six-pixel gap between adjacent controls and their labels.
CONTROL_GAP = 6

#: Seven-pixel gap between a control's icon and its label.
LABEL_GAP = 7

#: Nine-pixel vertical padding in a dense row.
ROW_PAD = 9

#: Ten-pixel gap between a group and the next, or padding inside a chip.
GROUP_GAP = 10

#: Eleven-pixel vertical padding in the search field.
FIELD_PAD = 11

#: Thirteen-pixel horizontal padding inside a chip.
CHIP_PAD = 13

#: Fourteen-pixel inset inside a card body.
CARD_BODY_INSET = 14

#: Eighteen-pixel inset for a page-like block inside a dialog.
PAGE_INSET = 18

#: Twenty-pixel break between sections.
SECTION_BREAK = 20

#: Twenty-two-pixel offset above a hero label.
HERO_OFFSET = 22

#: Thirty-pixel indent for a detail line nested under a row.
INDENT = 30

#: Seventy-pixel measure cap that keeps a reading column readable.
MEASURE_CAP = 70
