"""Design-system guard: no native (un-themed) widgets or dialogs in app/ui.

The 2026-10 design audit found a handful of places where a native Tk widget
sat inside an otherwise themed surface (a bright white error dialog, an
OptionMenu in a Material dialog, a Win95 radio in the disk tool). Those read
as rendering faults, not as choices, and are easy to reintroduce.

This mirrors the SEC-001 allowlist guard: a cheap static check that the
fixed call sites stay fixed. If a future change genuinely needs a native
control, this test is where that decision gets recorded (and justified).
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

_UI_ROOT = Path(__file__).resolve().parents[1] / "app" / "ui"

#: label -> regex that must NOT match any line under app/ui.
_BANNED = {
    "native OptionMenu (use M3Dropdown)":
        r"\btk\.OptionMenu\s*\(",
    "native Radiobutton (use SegmentedTabs / a themed control)":
        r"\btk\.Radiobutton\s*\(",
    "native tk.Button (use AnimatedButton)":
        r"\btk\.Button\s*\(",
    "native error/warning dialog (use _themed_showerror / _themed_showwarning)":
        r"\bmessagebox\.(showerror|showwarning)\s*\(",
}


def _offenders(pattern: str) -> list:
    rx = re.compile(pattern)
    hits = []
    for path in sorted(_UI_ROOT.rglob("*.py")):
        for lineno, line in enumerate(
                path.read_text(encoding="utf-8").splitlines(), 1):
            if rx.search(line):
                hits.append(
                    f"{path.relative_to(_UI_ROOT)}:{lineno}: {line.strip()}")
    return hits


@pytest.mark.parametrize("label,pattern", list(_BANNED.items()),
                         ids=list(_BANNED))
def test_no_native_widget_or_dialog(label, pattern):
    hits = _offenders(pattern)
    assert not hits, f"{label}\n  " + "\n  ".join(hits)
