"""I-001: one capture wrapper instead of a dozen hand-rolled copies.

The copies drifted: some flashed a console, some crashed decoding localized
output, some inherited the GUI's stdin, and a few had no timeout. `run_capture`
is the one place those decisions are made now.
"""
from __future__ import annotations

import sys

from app.utils import run_capture


def test_it_returns_the_code_and_the_output():
    rc, out = run_capture([sys.executable, "-c", "print('hello from probe')"],
                          timeout=60)
    assert rc == 0
    assert "hello from probe" in out


def test_a_missing_program_is_none_not_zero():
    rc, out = run_capture(["definitely-not-a-real-program-ct"], timeout=10)
    assert rc is None
    assert out, "the reason belongs in the output, not swallowed"


def test_a_failing_command_keeps_its_code():
    rc, _out = run_capture([sys.executable, "-c", "raise SystemExit(3)"],
                           timeout=60)
    assert rc == 3


def test_it_never_raises_on_a_bad_timeout():
    rc, out = run_capture([sys.executable, "-c", "print('x')"], timeout=0.001)
    assert rc is None or rc == 0      # timing-dependent; it must simply not raise
    assert isinstance(out, str)
