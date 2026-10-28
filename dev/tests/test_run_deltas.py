"""N-007: a run is compared with the previous run of the SAME kind.

The ledger stored one number per run, and a number on its own answers "did
this help" only if the reader remembers the last one. These pin the three ways
that comparison can lie: comparing a Tweak run to a Clean, comparing against a
run that freed nothing, and letting the caller's order (newest-first in the
viewer, oldest-first in history.runs) change the answer.
"""
from __future__ import annotations

from app import history


def _run(t, b, kind="clean"):
    return {"t": float(t), "b": int(b), "k": kind, "c": []}


def test_a_clean_is_compared_with_the_previous_clean():
    entries = [_run(1, 1_000), _run(2, 3_000)]
    out = history.deltas(entries)
    assert out[0] is None                    # the first run of its kind
    assert out[1]["bytes"] == 2_000
    assert out[1]["previous"] is entries[0]


def test_a_tweak_is_never_compared_with_a_clean():
    entries = [_run(1, 4_000_000_000, "clean"),
               _run(2, 0, "tweak"),
               _run(3, 0, "tweak")]
    out = history.deltas(entries)
    assert out[0] is None
    assert out[1] is None                    # first tweak
    assert out[2] is None                    # a zero-byte pair has no ratio


def test_a_run_that_freed_nothing_is_not_a_regression():
    entries = [_run(1, 500), _run(2, 0, "clean")]
    assert history.deltas(entries)[1] is None


def test_the_caller_order_does_not_change_the_answer():
    entries = [_run(1, 1_000), _run(2, 3_000), _run(3, 1_500)]
    oldest_first = history.deltas(entries)
    newest_first = history.deltas(list(reversed(entries)))
    assert oldest_first[2]["bytes"] == -1_500
    assert newest_first[0]["bytes"] == -1_500
    assert newest_first[0]["previous"] is entries[1]


def test_junk_entries_are_skipped_not_raised_on():
    out = history.deltas([{}, {"t": "x", "b": "y"}, _run(1, 10)])
    assert len(out) == 3
    assert out[2] is None                    # its earlier "runs" are garbage


def test_an_empty_history_yields_no_comparisons():
    assert history.deltas([]) == []
    assert history.deltas(None) == []


# --------------------------------------------------------------------------- #
# the viewer renders the comparison as a sentence, not a fourth number
# ---------------------------------------------------------------------------
import pytest  # noqa: E402

tk = pytest.importorskip("tkinter")
from _tkhelpers import make_root  # noqa: E402


def _sub_texts(dlg):
    """Every label text inside each row, newest first (rows, then labels)."""
    out = []
    for row in dlg._rows:
        found = []

        def walk(widget):
            for child in widget.winfo_children():
                if child.winfo_class() == "Label":
                    found.append(child.cget("text"))
                else:
                    walk(child)

        walk(row)
        out.append(" | ".join(found))
    return out


def test_the_viewer_says_how_a_run_compares_with_the_last_one():
    import app.ui.dialogs.past_runs_dialog as mod

    entries = [
        {"t": 1_000, "b": 1_000_000_000, "k": "clean", "c": []},
        {"t": 2_000, "b": 3_000_000_000, "k": "clean", "c": []},
    ]
    root = make_root()
    root.withdraw()
    dlg = mod.PastRunsDialog(root, entries=entries)
    try:
        subs = _sub_texts(dlg)
        assert any("more than the previous clean" in s for s in subs), subs
        assert not any("previous" in s for s in subs[-1:]), (
            "the first run of a kind has nothing to compare against")
    finally:
        dlg.close()
        root.destroy()


def test_a_tweak_row_never_reads_as_worse_than_a_clean():
    import app.ui.dialogs.past_runs_dialog as mod

    entries = [
        {"t": 1_000, "b": 4_000_000_000, "k": "clean", "c": []},
        {"t": 2_000, "b": 0, "k": "tweak",
         "c": [{"k": "registry", "l": "Flag", "d": "1 \u2192 0"}]},
    ]
    root = make_root()
    root.withdraw()
    dlg = mod.PastRunsDialog(root, entries=entries)
    try:
        subs = _sub_texts(dlg)
        assert any("freed no disk space" in s for s in subs), subs
        assert not any("previous clean" in s for s in subs), subs
    finally:
        dlg.close()
        root.destroy()
