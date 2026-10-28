"""The Health junk card must not present a lower bound as a complete number.

A cleaner that skips while the app it cleans is live (Office/Steam/FiveM) is a
known skip, so the shared walk still returns a total - but one that omits a
folder nobody looked at. `sense_junk` is the only place that total becomes a
claim, and the report row renders only the headline, which is why the marker
lives there. These tests pin both halves: the reasons riding the cached total,
and the card's wording for each case.
"""
import time

import app.health_scan as health
import app.storage_scan as scan
from app.utils import mark_unmeasured


def _ctx():
    from app.utils import TaskContext
    return TaskContext(log=lambda _m: None, set_status=lambda _m: None)


def _cached(monkeypatch, total, declined=()):
    """Plant a fresh cache record: what a just-finished walk publishes."""
    monkeypatch.setattr(scan, "_ESTIMATE_CACHE",
                        (total, time.time(), tuple(declined)))


class _Task:
    def __init__(self, key, run):
        self.key = key
        self.run = run


# --------------------------------------------------------------------------- #
# The reasons travel with the total
# --------------------------------------------------------------------------- #

def test_the_cache_keeps_the_reasons_a_walk_skipped_for(monkeypatch):
    monkeypatch.setattr(scan, "_ESTIMATE_CACHE", None)
    scan.store_estimate(
        4096, ["Steam is running, so the depot cache was not looked at"])
    hit = scan.cached_estimate()
    assert hit is not None and hit[0] == 4096
    assert scan.cached_declined() == (
        "Steam is running, so the depot cache was not looked at",)


def test_a_complete_walk_caches_no_reasons(monkeypatch):
    monkeypatch.setattr(scan, "_ESTIMATE_CACHE", None)
    scan.store_estimate(4096)
    assert scan.cached_declined() == ()


def test_a_fresh_walk_hands_its_skips_to_the_caller(monkeypatch):
    """The out-parameter is the fresh-walk half of the contract: a caller that
    shows the number has to be able to say what the walk skipped."""
    monkeypatch.setattr(scan, "cached_estimate", lambda *a, **k: None)
    monkeypatch.setattr(scan, "store_estimate", lambda *a, **k: None)
    monkeypatch.setattr(
        scan, "SCAN_ALLOWLIST", frozenset({"office_cache", "winget_cache"}))

    def refuses(ctx):
        mark_unmeasured(ctx, "Office may be running")
        return 0

    def measures(ctx):
        return 2048

    monkeypatch.setattr(
        scan, "_clean_task_by_key",
        lambda: {"office_cache": _Task("office_cache", refuses),
                 "winget_cache": _Task("winget_cache", measures)})
    declined = []
    assert scan.measure_all_cached(declined=declined) == 2048
    assert declined == ["Office may be running"]


def test_a_cache_hit_reports_the_skips_that_walk_had(monkeypatch):
    """Quoting the cached number without its skips would turn a lower bound
    back into a complete-looking total."""
    _cached(monkeypatch, 4096, ["Steam is running"])

    def walked(_ctx=None):
        raise AssertionError("a fresh cache hit must not walk the disk")

    monkeypatch.setattr(scan, "_clean_task_by_key", walked)
    declined = []
    assert scan.measure_all_cached(declined=declined) == 4096
    assert declined == ["Steam is running"]


# --------------------------------------------------------------------------- #
# The card's wording
# --------------------------------------------------------------------------- #

def test_the_card_calls_a_total_with_a_skip_a_lower_bound(monkeypatch):
    from app.utils import format_bytes
    _cached(monkeypatch, 4096,
            ["Office may be running, so the Office cache was not looked at"])
    grade, headline, detail, _note = health.sense_junk(_ctx())
    assert grade == health.grade_junk(4096)
    assert headline == "At least %s can be cleaned" % format_bytes(4096)
    assert "Office" in detail


def test_two_skips_are_both_named(monkeypatch):
    _cached(monkeypatch, 1024,
            ["Office may be running", "Steam is running"])
    _grade, _headline, detail, _note = health.sense_junk(_ctx())
    assert "Office" in detail and "Steam" in detail


def test_a_complete_walk_still_says_about(monkeypatch):
    from app.utils import format_bytes
    _cached(monkeypatch, 4096)
    _grade, headline, _detail, _note = health.sense_junk(_ctx())
    assert headline == "About %s can be cleaned" % format_bytes(4096)
    assert "At least" not in headline


def test_zero_with_a_skip_is_not_already_empty(monkeypatch):
    """"Already empty" would be a claim about a folder nobody looked at, so
    this case is unknown - matching the dialog's "? unknown" pill."""
    _cached(monkeypatch, 0,
            ["FiveM is running, so the cache was not looked at"])
    grade, _headline, detail, _note = health.sense_junk(_ctx())
    assert grade == "?"
    assert "already empty" not in detail
    assert "FiveM" in detail


def test_zero_without_a_skip_is_still_an_honest_all_clear(monkeypatch):
    _cached(monkeypatch, 0)
    grade, headline, detail, _note = health.sense_junk(_ctx())
    assert grade == "A"
    assert headline == "Nothing to clean"
    assert "already empty" in detail


def test_the_card_reports_a_fresh_walks_skips_too(monkeypatch):
    """Health is often the first surface to walk; the marker must not depend
    on Storage Insight having cached the same fact first."""
    monkeypatch.setattr(scan, "cached_estimate", lambda *a, **k: None)

    def walk(set_status=None, cancelled=None, declined=None):
        declined.append(
            "Steam is running, so the depot cache was not looked at")
        return 8192

    monkeypatch.setattr(scan, "measure_all_cached", walk)
    _grade, headline, detail, _note = health.sense_junk(_ctx())
    assert headline.startswith("At least ")
    assert "Steam" in detail
