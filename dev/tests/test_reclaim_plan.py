"""N-002: one ordered plan, built only from numbers that were measured.

The plan is the answer to "what first". These pin the three things that would
make it untrustworthy: an unmeasured category claimed as 0, a step that cannot
run pretending it can, and an order that reshuffles between two scans of the
same machine.
"""
from __future__ import annotations

import pytest

from app import reclaim


def _cat(title, size, keys=("a",), unknown=0):
    return {"title": title, "bytes": size, "keys": list(keys),
            "unknown": unknown}


def test_largest_measured_category_comes_first():
    plan = reclaim.build_plan([
        _cat("Windows temp & logs", 300_000_000),
        _cat("Game files & caches", 4_000_000_000),
        _cat("Dev & package caches", 900_000_000),
    ])
    titles = [s["title"] for s in plan["steps"]][:3]
    assert titles == ["Game files & caches", "Dev & package caches",
                      "Windows temp & logs"]


def test_equal_sizes_keep_the_caller_order():
    first = reclaim.build_plan([_cat("A", 10), _cat("B", 10)])
    second = reclaim.build_plan([_cat("A", 10), _cat("B", 10)])
    assert [s["title"] for s in first["steps"]][:2] == ["A", "B"]
    assert first == second


def test_an_unmeasured_category_is_never_zero():
    plan = reclaim.build_plan([_cat("Browser & app caches", None),
                               _cat("Game files & caches", 1000)])
    unknown = [s for s in plan["steps"] if s["title"] == "Browser & app caches"]
    assert unknown and unknown[0]["bytes"] is None
    assert "Not measured" in unknown[0]["detail"]
    # The measured total counts only what was measured, and `partial` says so
    # instead of presenting it as the whole answer.
    assert plan["measured_total"] == 1000
    assert plan["partial"] is True


def test_a_measured_category_with_no_keys_cannot_offer_clean():
    plan = reclaim.build_plan([_cat("Windows temp & logs", 500, keys=())])
    step = plan["steps"][0]
    assert step["action"] == ""
    assert reclaim.runnable_keys(step) == []


def test_unknown_member_cleaners_are_stated_in_the_detail():
    plan = reclaim.build_plan([
        _cat("Game files & caches", 2_048, keys=("a", "b"), unknown=2)])
    assert "2 did not look" in plan["steps"][0]["detail"]


def test_the_cross_tool_steps_come_after_the_measurements():
    plan = reclaim.build_plan([_cat("Game files & caches", 4_000_000_000)])
    ids = [s["id"] for s in plan["steps"]]
    assert ids[0] == "cat:Game files & caches"
    assert "extra:duplicates" in ids
    for step in plan["steps"]:
        if step["id"].startswith("extra:"):
            assert step["action"] == "Open"
            assert step["target"]
            assert reclaim.runnable_keys(step) == []


def test_extras_can_be_left_out():
    plan = reclaim.build_plan([_cat("Game files & caches", 10)],
                              extras=False)
    assert all(not s["id"].startswith("extra:") for s in plan["steps"])


def test_a_plan_with_nothing_measured_says_nothing_measured():
    plan = reclaim.build_plan([])
    assert not [s for s in plan["steps"] if s["id"].startswith("cat:")]
    assert plan["measured_total"] == 0
    assert reclaim.headline(plan) == "Nothing measured"


def test_headline_shows_the_partial_marker_only_when_partial():
    clean = reclaim.build_plan([_cat("A", 1024 * 1024)])
    assert reclaim.headline(clean).endswith("MB"), reclaim.headline(clean)
    partial = reclaim.build_plan([_cat("A", 1024 * 1024),
                                  _cat("B", None)])
    assert reclaim.headline(partial).endswith("+ ?")


def test_junk_input_is_skipped_not_raised_on():
    plan = reclaim.build_plan([None, {}, {"title": ""}, _cat("A", 5),
                               {"title": "B", "bytes": "lots"}])
    titles = [s["title"] for s in plan["steps"] if s["id"].startswith("cat:")]
    assert titles == ["A", "B"]
    # "lots" is not a measurement - it becomes a 'did not measure' step, never
    # a step claiming zero bytes.
    assert [s for s in plan["steps"] if s["title"] == "B"][0]["bytes"] is None


tk = pytest.importorskip("tkinter")


def _stub_app(root):
    class _Dispatch:
        def post(self, fn, *args):
            fn(*args)

    class _App:
        _dispatch = _Dispatch()

        def __init__(self):
            self.root = root
            self.runs = []

        def run_tasks(self, tab, tasks, mode="run"):
            self.runs.append((tab, [t.key for t in tasks], mode))

        def log(self, *_a):
            pass

    return _App()


def test_the_dialog_draws_one_row_per_step_and_runs_the_picked_one():
    from _tkhelpers import make_root
    from app.ui.dialogs.reclaim_plan_dialog import ReclaimPlanDialog
    from app.ui.widgets import FindingRow

    plan = reclaim.build_plan([_cat("Game files & caches", 4_000_000_000,
                                    keys=("shader_cache",))])
    root = make_root()
    try:
        root.withdraw()
        app = _stub_app(root)
        dlg = ReclaimPlanDialog(root, app, plan)
        rows = []
        # ToolPage wraps a Toplevel; the rows live inside it.

        def walk(widget):
            for child in widget.winfo_children():
                if isinstance(child, FindingRow):
                    rows.append(child)
                else:
                    walk(child)

        walk(dlg._dlg)
        assert len(rows) == len(plan["steps"])
        assert dlg.banner._title_full.startswith("3.7")
        rows[0].action()._fire()
        assert app.runs and app.runs[0][0] == "Clean"
        assert app.runs[0][1] == ["shader_cache"]
        assert app.runs[0][2] == "run"
        dlg.close()
    finally:
        root.destroy()


def test_storage_insight_hands_its_own_measurements_to_the_plan(monkeypatch):
    """The plan never rescans: it is built from what that walk measured."""
    from _tkhelpers import make_root
    from app.ui.dialogs import storage_insight_dialog as sid
    from app.ui.dialogs import reclaim_plan_dialog as rpd

    seen = {}

    class _FakePlan:
        def __init__(self, parent, app, plan):
            seen.update(plan)

        def wait(self):
            pass

    monkeypatch.setattr(rpd, "ReclaimPlanDialog", _FakePlan)
    root = make_root()
    try:
        root.withdraw()
        dlg = sid.StorageInsightDialog(root, _stub_app(root))
        dlg._cancel_scan()
        dlg._cat_sizes = {"Game files & caches": 4_000_000_000}
        dlg._cat_keys = {"Game files & caches": ["shader_cache"]}
        dlg._cat_unknown = {"Game files & caches": 1}
        dlg._open_reclaim_plan()
        assert seen["measured_total"] == 4_000_000_000
        top = seen["steps"][0]
        assert top["title"] == "Game files & caches"
        assert top["keys"] == ["shader_cache"]
        assert "1 did not look" in top["detail"]
    finally:
        try:
            dlg.close()
        except Exception:
            pass
        root.destroy()


def test_the_dialog_with_a_empty_plan_has_no_rows(monkeypatch):
    from _tkhelpers import make_root
    from app.ui.dialogs.reclaim_plan_dialog import ReclaimPlanDialog
    from app.ui.widgets import FindingRow

    root = make_root()
    try:
        root.withdraw()
        dlg = ReclaimPlanDialog(root, _stub_app(root),
                                reclaim.build_plan([], extras=False))
        found = []

        def walk(widget):
            for child in widget.winfo_children():
                if isinstance(child, FindingRow):
                    found.append(child)
                else:
                    walk(child)

        walk(dlg._dlg)
        assert found == []
        dlg.close()
    finally:
        root.destroy()
