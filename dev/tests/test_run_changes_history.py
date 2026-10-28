"""R-005, second half: typed changes survive the run, in run history.

The scorecard shows one run; the rolling `run_history` is what lets the app
look back. These tests pin three layers:

  * `config_persist.record_run` writing compact, display-ready change records
    (and recording a change-only run at all);
  * `config_schema._run_history` keeping that file bounded and valid, while an
    entry written before R-005 stays exactly `{t, b}`;
  * `history.runs()` exposing the records, and the PastRunsDialog opened from
    the scorecard rendering them.

No real config file is written: `update_config` and `load_config` are faked.
"""
from __future__ import annotations

import json

import pytest

from app.changes import KIND_REGISTRY, KIND_SERVICE, Change
from app.config_schema import (
    RUN_CHANGE_MAX, RUN_HISTORY_CHANGE_RUNS, _run_history,
)


def _change(kind=KIND_REGISTRY, target="HKCU\\Software\\X\\Flag", before=1,
            after=0, **kw):
    return Change(kind, target, before, after, **kw)


@pytest.fixture
def history_file(monkeypatch):
    """An in-memory stand-in for config.json's run_history."""
    import app.config_persist as cp
    state = {"run_history": []}

    def _update(fn):
        cfg = {"run_history": list(state["run_history"])}
        fn(cfg)
        state["run_history"] = list(cfg["run_history"])

    monkeypatch.setattr(cp, "update_config", _update)
    return state


# --------------------------------------------------------------------------- #
# writing
# --------------------------------------------------------------------------- #

def test_a_clean_run_is_still_recorded_as_bytes(history_file):
    from app.config_persist import record_run
    record_run(1024)
    entry = history_file["run_history"][0]
    assert entry["b"] == 1024 and entry["k"] == "clean"
    assert "c" not in entry


def test_a_run_that_freed_nothing_but_changed_something_is_recorded(history_file):
    from app.config_persist import record_run
    record_run(0, kind="tweak", changes=[_change()])
    entry = history_file["run_history"][0]
    assert entry["b"] == 0 and entry["k"] == "tweak"
    assert entry["c"] == [{"k": KIND_REGISTRY, "l": "Flag",
                           "d": "1 \u2192 0",
                           "p": "HKCU\\Software\\X\\Flag"}]


def test_a_run_with_no_bytes_and_no_changes_is_not_recorded(history_file):
    from app.config_persist import record_run
    record_run(0, kind="tweak", changes=[])
    assert history_file["run_history"] == []


def test_stored_changes_are_collapsed_and_bounded(history_file):
    from app.config_persist import record_run
    many = [_change(target="HKCU\\X\\V%d" % i) for i in range(RUN_CHANGE_MAX * 2)]
    many += [_change(target="HKCU\\X\\V0", before=0, after=2)]   # same target again
    record_run(0, kind="tweak", changes=many)
    stored = history_file["run_history"][0]["c"]
    assert len(stored) == RUN_CHANGE_MAX
    # the repeated target collapsed to its FIRST prior -> LAST value
    assert stored[0]["l"] == "V0"
    assert stored[0]["d"] == "1 \u2192 2"


def test_stored_changes_are_plain_json(history_file):
    from app.config_persist import record_run
    record_run(0, kind="tweak",
               changes=[_change(before=1, after=b"\\x00\\x01"),
                        _change(kind=KIND_SERVICE, target="DiagTrack",
                                before=None, after="disabled",
                                before_known=False)])
    json.dumps(history_file["run_history"])            # must not raise
    stored = history_file["run_history"][0]["c"]
    assert stored[1] == {"k": KIND_SERVICE, "l": "DiagTrack",
                         "d": "\u2192 disabled"}


def test_only_the_newest_runs_keep_their_change_lists(history_file):
    from app.config_persist import record_run
    for i in range(RUN_HISTORY_CHANGE_RUNS + 5):
        record_run(0, kind="tweak",
                   changes=[_change(target="HKCU\\X\\V%d" % i)])
    kept = [e for e in history_file["run_history"] if "c" in e]
    assert len(kept) == RUN_HISTORY_CHANGE_RUNS
    # and it is the NEW end that keeps them
    assert kept[-1]["c"][0]["l"] == "V%d" % (RUN_HISTORY_CHANGE_RUNS + 4)


# --------------------------------------------------------------------------- #
# the schema keeps the file bounded and valid
# --------------------------------------------------------------------------- #

def test_an_entry_written_before_r005_round_trips_unchanged():
    old = {"t": 100.0, "b": 2048}
    assert _run_history([old], []) == [old]


def test_a_stored_change_list_is_validated_and_capped():
    entry = {"t": 1, "b": 0, "k": "tweak", "c": [
        {"k": KIND_REGISTRY, "l": "Flag", "d": "1 \u2192 0", "p": "HKCU\\X\\Flag"},
        "not a record",
        {"k": "", "l": "x", "d": "y"},
        {"k": "service", "l": "x" * 500, "d": "y" * 500},
    ]}
    kept = _run_history([entry], [])[0]["c"]
    assert len(kept) == 2
    assert len(kept[1]["l"]) == 240 and len(kept[1]["d"]) == 120


def test_the_schema_also_limits_change_detail_to_the_newest_runs():
    entries = [{"t": i + 1, "b": 0, "k": "tweak",
                "c": [{"k": KIND_REGISTRY, "l": "V", "d": "1 \u2192 0"}]}
               for i in range(RUN_HISTORY_CHANGE_RUNS + 3)]
    kept = _run_history(entries, [])
    assert sum(1 for e in kept if "c" in e) == RUN_HISTORY_CHANGE_RUNS
    assert "c" not in kept[0] and "c" in kept[-1]


# --------------------------------------------------------------------------- #
# reading
# --------------------------------------------------------------------------- #

def _patch_history(monkeypatch, entries):
    import app.config_persist as cp
    monkeypatch.setattr(cp, "load_config", lambda: {"run_history": entries})


def test_runs_exposes_change_only_runs_and_labels(monkeypatch):
    from app import history
    _patch_history(monkeypatch, [
        {"t": 10, "b": 0, "k": "tweak",
         "c": [{"k": KIND_REGISTRY, "l": "Flag", "d": "1 \u2192 0"}]},
        {"t": 11, "b": 4096},
    ])
    rs = history.runs()
    assert [e["t"] for e in rs] == [10.0, 11.0]
    assert rs[0]["k"] == "tweak" and rs[0]["c"][0]["l"] == "Flag"
    assert rs[1]["k"] == "clean" and rs[1]["c"] == []
    assert history.kind_label("tweak") == "Tweak"
    assert history.kind_label("") == "Clean"


def test_runs_drops_a_malformed_change_record(monkeypatch):
    from app import history
    _patch_history(monkeypatch, [{
        "t": 10, "b": 0, "c": [
            {"k": KIND_REGISTRY},
            {"k": KIND_REGISTRY, "l": "Flag", "d": "1 \u2192 0"},
        ]}])
    rs = history.runs()
    assert [c["l"] for c in rs[0]["c"]] == ["Flag"]


# --------------------------------------------------------------------------- #
# scorecard link
# --------------------------------------------------------------------------- #

tk = pytest.importorskip("tkinter")

from app.ui.dialogs.scorecard_dialog import ScorecardDialog  # noqa: E402

from _tkhelpers import make_root  # noqa: E402


def _card(monkeypatch, entries):
    import app.history as hist
    monkeypatch.setattr(hist, "runs", lambda: entries)
    root = make_root()
    root.withdraw()
    card = ScorecardDialog(root, tab_name="Tweak", mode="run", cancelled=False,
                           results=[], total_bytes=0)
    return root, card


def test_the_card_offers_a_way_back_to_past_runs(monkeypatch):
    root, card = _card(monkeypatch, [{"t": 1, "b": 0, "k": "tweak", "c": []}])
    try:
        assert card._history_lbl is not None
        assert card._history_lbl.cget("text") == "View past runs \u203a"
    finally:
        card.close()
        root.destroy()


def test_no_history_means_no_link(monkeypatch):
    root, card = _card(monkeypatch, [])
    try:
        assert card._history_lbl is None
    finally:
        card.close()
        root.destroy()


def test_opening_past_runs_gives_the_grab_back_to_the_card(monkeypatch):
    """The viewer is a nested modal; its close releases the grab, so the
    scorecard must take it back or Escape would stop closing it."""
    import app.ui.dialogs.past_runs_dialog as mod

    root, card = _card(monkeypatch, [{"t": 1, "b": 0, "k": "tweak", "c": []}])
    seen = {}

    class _Stub:
        def __init__(self, parent, entries=None):
            seen["parent"] = parent
            seen["entries"] = entries
            self.cleared = False

        def wait(self):
            pass

    monkeypatch.setattr(mod, "PastRunsDialog", _Stub)
    try:
        card._open_past_runs()
        assert seen["parent"] is card._dlg
        assert seen["entries"] == card._history_runs, (
            "H-011: the viewer gets the card's own read, not a second one")
        root.update()
        assert card._dlg.grab_current() == card._dlg
    finally:
        card.close()
        root.destroy()


# --------------------------------------------------------------------------- #
# the viewer
# --------------------------------------------------------------------------- #

from app.ui.dialogs.past_runs_dialog import PastRunsDialog  # noqa: E402


def _viewer(monkeypatch, entries):
    import app.ui.dialogs.past_runs_dialog as mod
    monkeypatch.setattr(mod, "runs", lambda: entries)
    root = make_root()
    root.withdraw()
    return root, PastRunsDialog(root)


def test_the_viewer_lists_runs_newest_first_and_expands_changes(monkeypatch):
    root, dlg = _viewer(monkeypatch, [
        {"t": 1000, "b": 2048, "k": "clean", "c": []},
        {"t": 2000, "b": 0, "k": "tweak",
         "c": [{"k": KIND_REGISTRY, "l": "GameDVR_Enabled", "d": "1 \u2192 0",
                "p": "HKCU\\Software\\X\\GameDVR_Enabled"}]},
    ])
    try:
        assert len(dlg._rows) == 2
        newest = dlg._rows[0]
        assert hasattr(newest, "_detail"), "only the run with changes expands"
        assert not hasattr(dlg._rows[1], "_detail")

        newest.event_generate("<Button-1>")
        root.update()
        assert newest._detail.winfo_ismapped()
        texts = [w.cget("text") for w in newest._detail.winfo_children()]
        assert any("GameDVR_Enabled: 1 \u2192 0" in t for t in texts), texts
    finally:
        dlg.close()
        root.destroy()


def test_the_viewer_uses_the_list_it_is_handed(monkeypatch):
    """H-011: a caller that already read the ledger does not make it read
    again (the scorecard passes its own single read)."""
    import time as _time

    import app.ui.dialogs.past_runs_dialog as mod

    def _boom():
        raise AssertionError("the viewer must not read config when given entries")

    monkeypatch.setattr(mod, "runs", _boom)
    now = _time.time()
    root = make_root()
    root.withdraw()
    dlg = mod.PastRunsDialog(root, entries=[
        {"t": now - 10, "b": 2048, "k": "clean", "c": []},
    ])
    try:
        assert len(dlg._rows) == 1
    finally:
        dlg.close()
        root.destroy()


def test_a_run_row_can_be_expanded_from_the_keyboard(monkeypatch):
    """I-008: Tab to a run, Return (or Space) to reveal its changes."""
    import time as _time

    root, dlg = _viewer(monkeypatch, [
        {"t": _time.time(), "b": 0, "k": "tweak",
         "c": [{"k": KIND_REGISTRY, "l": "Flag", "d": "1 \u2192 0"}]},
    ])
    try:
        row = dlg._rows[0]
        assert int(row.cget("takefocus")) == 1
        row.focus_set()
        root.update()
        row.event_generate("<Return>")
        root.update()
        assert row._detail.winfo_ismapped()
        row.event_generate("<space>")
        root.update()
        assert not row._detail.winfo_ismapped()
    finally:
        dlg.close()
        root.destroy()


def test_the_viewer_says_so_when_there_is_nothing_to_show(monkeypatch):
    root, dlg = _viewer(monkeypatch, [])
    try:
        assert dlg._rows == []
        assert dlg._clear_btn is None, "nothing to clear = no clear button"
    finally:
        dlg.close()
        root.destroy()


# --------------------------------------------------------------------------- #
# clearing the ledger
# --------------------------------------------------------------------------- #


def test_clear_empties_the_ledger_and_nothing_else(history_file):
    from app import history
    from app.config_persist import record_run

    record_run(1024)
    record_run(0, kind="tweak", changes=[_change()])
    assert history.clear() is True
    assert history_file["run_history"] == []


def test_clear_reports_a_write_that_did_not_land(monkeypatch):
    """The viewer must be able to say "still there", so this one is honest
    about failure instead of swallowing it like record_run."""
    import app.config_persist as cp

    def _boom(_fn):
        raise OSError("config.json is read-only")

    monkeypatch.setattr(cp, "update_config", _boom)
    assert cp.clear_run_history() is False

    from app import history
    assert history.clear() is False


def _viewer_with_clear(monkeypatch, entries):
    import app.ui.dialogs.past_runs_dialog as mod
    monkeypatch.setattr(mod, "runs", lambda: entries)
    root = make_root()
    root.withdraw()
    return root, mod.PastRunsDialog(root), mod


_TWO_RUNS = [
    {"t": 1000, "b": 2048, "k": "clean", "c": []},
    {"t": 2000, "b": 0, "k": "tweak",
     "c": [{"k": KIND_REGISTRY, "l": "GameDVR_Enabled", "d": "1 \u2192 0"}]},
]


def test_clearing_asks_first_with_the_count_and_the_reassurance(monkeypatch):
    root, dlg, mod = _viewer_with_clear(monkeypatch, list(_TWO_RUNS))
    asked = {}

    def _ask(parent, title, message, **kw):
        asked.update(parent=parent, title=title, message=message, **kw)
        return False

    monkeypatch.setattr(mod, "_themed_askyesno", _ask)
    try:
        assert dlg._clear_btn is not None
        dlg._confirm_clear()
        root.update()
        assert asked["parent"] is dlg._dlg
        assert asked["title"] == "Clear Run History?"
        assert "all 2 recorded runs" in asked["message"]
        # the confirm promises that only the list goes
        assert "empties the list only" in asked["message"]
        assert "stay exactly as they are" in asked["message"]
        # "Keep" leaves everything untouched
        assert len(dlg._rows) == 2
        assert dlg._clear_btn is not None
        assert dlg._hint.cget("text").startswith("Newest first")
    finally:
        dlg.close()
        root.destroy()


def test_confirming_clears_the_list_and_retires_the_action(monkeypatch):
    root, dlg, mod = _viewer_with_clear(monkeypatch, list(_TWO_RUNS))
    cleared = []
    monkeypatch.setattr(mod, "_themed_askyesno", lambda *_a, **_k: True)
    monkeypatch.setattr(mod, "clear", lambda: cleared.append(True) or True)
    try:
        dlg._rows[0]._detail.pack()          # leave an expansion open
        root.update()
        dlg._confirm_clear()
        root.update()
        assert cleared == [True]
        assert dlg._rows == []
        texts = _labels(dlg._panel.inner)
        assert "Nothing to show." in texts, texts
        assert "New runs will appear here." in texts, texts
        assert dlg._clear_btn is None
        assert dlg._hint.cget("text") == (
            "History cleared. New runs will appear here.")
    finally:
        dlg.close()
        root.destroy()


def test_a_failed_clear_keeps_the_list_and_says_so(monkeypatch):
    root, dlg, mod = _viewer_with_clear(monkeypatch, list(_TWO_RUNS))
    notices = []
    monkeypatch.setattr(mod, "_themed_askyesno", lambda *_a, **_k: True)
    monkeypatch.setattr(mod, "clear", lambda: False)
    monkeypatch.setattr(mod, "_themed_showinfo",
                        lambda _p, title, msg, **_k: notices.append((title, msg)))
    try:
        dlg._confirm_clear()
        root.update()
        assert notices and notices[0][0] == "Couldn't Clear History"
        assert len(dlg._rows) == 2, "a failed write must not empty the view"
        assert dlg._clear_btn is not None
    finally:
        dlg.close()
        root.destroy()


def test_the_card_drops_its_history_lines_once_the_ledger_is_cleared(monkeypatch):
    """A clear from the viewer must not leave the card quoting runs that no
    longer exist - and must not cost another config read to find that out."""
    import time as _time

    import app.ui.dialogs.past_runs_dialog as prd

    now = _time.time()
    root, card = _card(monkeypatch, [
        {"t": now - 3600, "b": 1024, "k": "clean", "c": []},
        {"t": now - 60, "b": 0, "k": "tweak", "c": []},
    ])
    reads = {"n": 0}
    try:
        assert card._month_lbl is not None and card._month_lbl.winfo_manager()
        assert card._history_lbl.winfo_manager()

        class _ClearingStub:
            def __init__(self, parent, entries=None):
                self.cleared = True

            def wait(self):
                pass

        monkeypatch.setattr(prd, "PastRunsDialog", _ClearingStub)

        def _counting_runs():
            reads["n"] += 1
            return []

        import app.history as hist
        monkeypatch.setattr(hist, "runs", _counting_runs)
        card._open_past_runs()
        root.update()

        assert reads["n"] == 0, "the clear is reported; the card must not re-read"
        assert card._history_lbl.winfo_manager() == ""
        assert card._month_lbl.winfo_manager() == ""
    finally:
        card.close()
        root.destroy()


# --------------------------------------------------------------------------- #
# the worker wires the ledger into history
# --------------------------------------------------------------------------- #

def test_the_gui_worker_records_the_run_with_its_kind_and_changes(monkeypatch):
    from types import SimpleNamespace

    import app.config_persist as cp
    import app.ui.runflow as runflowmod
    from app.ui.app import Application

    monkeypatch.setattr(runflowmod, "_snapshot_all_drives",
                        lambda *_a, **_k: {}, raising=True)
    monkeypatch.setattr(runflowmod, "_themed_showinfo",
                        lambda *_a, **_k: None, raising=True)
    cards = []

    class _Card:
        def __init__(self, _parent, **kwargs):
            cards.append(kwargs)

        def wait(self):
            pass

    monkeypatch.setattr(runflowmod, "ScorecardDialog", _Card, raising=True)
    calls = []
    monkeypatch.setattr(cp, "record_run",
                        lambda b, **kw: calls.append((b, kw)), raising=True)

    app = object.__new__(Application)
    app.root = None
    app.log = lambda _m: None
    app.set_status = lambda _s: None
    app._cancel_requested = False
    app._query_drive_free_total = lambda _root: 0
    app._query_drives = lambda: []
    app._set_progress = lambda *_a: None
    app._stop_indeterminate = lambda: None
    app._progress_hide = lambda: None
    app.cancel_btn = SimpleNamespace(set_enabled=lambda _v: None)
    app.progress_bar = SimpleNamespace(set_indeterminate=lambda _v: None,
                                       set_fraction=lambda _v: None)
    app._release_run = lambda *_a, **_k: None
    app._set_buttons_enabled = lambda _v: None
    app._is_current = lambda _gen: True
    app._dispatch = SimpleNamespace(post=lambda fn: fn())
    app.tabs = {}

    def _invoke(ctx, _task, _mode):
        ctx.changes.append(_change(target="HKCU\\X\\Tweaked"))
        return "ok", 0, None

    app._invoke_single_task = _invoke
    app._run_tasks_worker(
        "Tweak",
        [SimpleNamespace(label="T", risk="", key="t", revert=None)],
        "run", gen=1, run_results=[])

    assert calls, "a run with changes must be remembered"
    bytes_freed, kwargs = calls[0]
    assert kwargs["kind"] == "tweak"
    assert [c.target for c in kwargs["changes"]] == ["HKCU\\X\\Tweaked"]
    # and the card was handed the same ledger it recorded
    assert cards and cards[0]["changes"][0].target == "HKCU\\X\\Tweaked"


# --------------------------------------------------------------------------- #
# U-010: kind filter, and zero-byte runs that are not failures
# --------------------------------------------------------------------------- #

def _labels(widget):
    """Every Label text under `widget`, at any depth.

    Empty states are an EmptyState frame now (C-004), so a flat scan of the
    panel's children finds a Frame, not the sentence. Walking the tree keeps
    these tests about what the reader can see.
    """
    out = []
    for child in widget.winfo_children():
        try:
            if child.winfo_class() == "Label":
                out.append(str(child.cget("text")))
        except Exception:
            pass
        out.extend(_labels(child))
    return out


def _sub_text(row):
    """The row's second line (kind + summary), as displayed."""
    left = row.winfo_children()[0]
    labels = [w for w in left.winfo_children()
              if isinstance(w, tk.Label)]
    return "\n".join(w.cget("text") for w in labels)


def test_the_viewer_filters_by_run_kind(monkeypatch):
    import time as _time

    now = _time.time()
    root, dlg = _viewer(monkeypatch, [
        {"t": now - 300, "b": 4096, "k": "clean", "c": []},
        {"t": now - 200, "b": 0, "k": "tweak",
         "c": [{"k": KIND_REGISTRY, "l": "Flag", "d": "1 \u2192 0"}]},
        {"t": now - 100, "b": 0, "k": "tweak",
         "c": [{"k": KIND_REGISTRY, "l": "Other", "d": "0 \u2192 1"}]},
    ])
    try:
        assert dlg._rows, "the ledger had rows to show"
        assert dlg._filter_row is not None, "two kinds = a filter is worth it"

        dlg._set_filter("tweak")
        assert len(dlg._rows) == 2
        dlg._set_filter("clean")
        assert len(dlg._rows) == 1
        dlg._set_filter("all")
        assert len(dlg._rows) == 3
    finally:
        dlg.close()
        root.destroy()


def test_a_filter_with_no_runs_of_that_kind_says_so(monkeypatch):
    import time as _time

    now = _time.time()
    root, dlg = _viewer(monkeypatch, [
        {"t": now - 300, "b": 4096, "k": "clean", "c": []},
        {"t": now - 200, "b": 0, "k": "tweak",
         "c": [{"k": KIND_REGISTRY, "l": "Flag", "d": "1 \u2192 0"}]},
    ])
    try:
        dlg._filter = "repair"          # not a kind this ledger holds
        dlg._fill(dlg._entries)
        assert dlg._rows == []
        texts = _labels(dlg._panel.inner)
        assert any("no repair runs" in t.lower() for t in texts), texts
    finally:
        dlg.close()
        root.destroy()


def test_one_kind_of_run_needs_no_filter_row(monkeypatch):
    import time as _time

    now = _time.time()
    root, dlg = _viewer(monkeypatch, [
        {"t": now - 1, "b": 4096, "k": "clean", "c": []},
    ])
    try:
        assert dlg._filter_row is None
        assert len(dlg._rows) == 1
    finally:
        dlg.close()
        root.destroy()


def test_a_run_that_freed_no_bytes_says_so_instead_of_showing_nothing(monkeypatch):
    import time as _time

    now = _time.time()
    root, dlg = _viewer(monkeypatch, [
        {"t": now - 300, "b": 4096, "k": "clean", "c": []},
        {"t": now - 200, "b": 0, "k": "tweak",
         "c": [{"k": KIND_REGISTRY, "l": "Flag", "d": "1 \u2192 0"}]},
    ])
    try:
        tweak = dlg._rows[0]      # newest first
        clean = dlg._rows[1]
        assert "freed no disk space" in _sub_text(tweak)
        assert "freed no disk space" not in _sub_text(clean)
    finally:
        dlg.close()
        root.destroy()
