"""N-006: the run-history/trend layer (pure, no Tk).

history.py reads the rolling run_history the pipeline already writes; these
pin the three things it promises -- validation (junk dropped, never
repaired), calendar-day bucketing, and a sparkline that shows a real zero
rather than an empty string.
"""
from __future__ import annotations

import datetime as dt

from app import history


def _patch(monkeypatch, entries):
    import app.config_persist as cp
    monkeypatch.setattr(cp, "load_config", lambda: {"run_history": entries})


def _ts(day):
    return dt.datetime.combine(day, dt.time(12, 0)).timestamp()


def test_runs_validates_sorts_and_drops_junk(monkeypatch):
    _patch(monkeypatch, [
        {"t": 200, "b": 5},
        "not a dict",
        {"t": 100, "b": 3},
        {"t": 300, "b": 0},          # zero bytes -> not a run worth keeping
        {"t": "x", "b": 1},          # unparseable time
        {"b": 9},                    # no time at all
    ])
    rs = history.runs()
    assert [e["t"] for e in rs] == [100.0, 200.0]
    assert [e["b"] for e in rs] == [3, 5]


def test_total_sums_and_counts(monkeypatch):
    _patch(monkeypatch, [{"t": 2, "b": 10}, {"t": 1, "b": 5}])
    assert history.total() == (15, 2)


def test_empty_history_is_empty_not_fabricated(monkeypatch):
    _patch(monkeypatch, [])
    assert history.runs() == []
    assert history.total() == (0, 0)


def test_daily_buckets_by_calendar_day(monkeypatch):
    today = dt.date.today()
    _patch(monkeypatch, [
        {"t": _ts(today), "b": 100},
        {"t": _ts(today - dt.timedelta(days=1)), "b": 50},
        {"t": _ts(today - dt.timedelta(days=40)), "b": 999},   # out of window
    ])
    series = history.daily(30)
    assert len(series) == 30
    assert series[-1] == 100          # today is the last bucket
    assert series[-2] == 50           # yesterday
    assert sum(series) == 150         # the 40-day-old run is excluded


def test_daily_gap_is_a_real_zero(monkeypatch):
    _patch(monkeypatch, [])
    assert history.daily(7) == [0] * 7


def test_sparkline_ramp_and_flat():
    assert history.sparkline([]) == ""
    assert history.sparkline([0]) == "\u2581"
    assert history.sparkline([0, 0]) == "\u2581\u2581"
    s = history.sparkline([0, 8])
    assert s[0] == "\u2581" and s[-1] == "\u2588"


def test_sparkline_length_matches_input(monkeypatch):
    today = dt.date.today()
    _patch(monkeypatch, [{"t": _ts(today - dt.timedelta(days=i)), "b": i * 10}
                         for i in range(10)])
    line = history.sparkline(history.daily(30))
    assert len(line) == 30


def test_reclaimed_since_is_a_pair_of_ints():
    total, count = history.reclaimed_since(30)
    assert isinstance(total, int) and isinstance(count, int)
