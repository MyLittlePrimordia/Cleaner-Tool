"""The Storage Insight estimate tables must agree with each other.

`SCAN_ALLOWLIST` (what a measuring pass may invoke), `SCAN_EXCLUDED` (why each
other Clean task is not measured) and `SCAN_CATEGORIES` (the five rows the
dialog shows) are three views of one decision. Until 2026-10 only the allowlist
existed, and the only check was "an allowlisted key must be a real task" - the
direction that fails loudly.

The direction that rots silently is the other one. Drop a task from the
allowlist and the dialog just shows a smaller number; add a key to a category
that nobody measures and the row reads 0 while the total ignores it; put a key
in two categories and its bytes are counted twice. None of those raise, and
none of them look wrong on screen - they are exactly the kind of quiet
dishonesty the guard in dev/tools/verify_dryrun_scope.py exists to prevent, so
`validate_allowlist()` checks all three tables and CI fails on any disagreement.

These tests assert the real tables are in agreement AND that each check fires
when it should: a validation nobody has seen fail is a validation you cannot
trust.
"""
from __future__ import annotations

import pytest

import app.storage_scan as scan
from app.utils import mark_unmeasured


def _clean_keys() -> set:
    from app.tab_presets import TABS
    return {t.key for t in TABS.get("Clean", [])}


# --------------------------------------------------------------------------- #
# The real tables
# --------------------------------------------------------------------------- #

def test_the_declaration_tables_agree():
    ok, problems = scan.validate_allowlist()
    assert problems == []
    assert ok is True


def test_every_clean_task_is_measured_or_explained():
    clean = _clean_keys()
    measured = set(scan.SCAN_ALLOWLIST)
    excluded = set(scan.SCAN_EXCLUDED)
    assert measured <= clean
    assert excluded <= clean
    assert measured & excluded == set()
    assert measured | excluded == clean, (
        "a Clean task is in neither table: %r"
        % sorted(clean - (measured | excluded)))


def test_every_exclusion_states_a_one_line_reason():
    for key, why in scan.SCAN_EXCLUDED.items():
        assert why.strip(), "%s is excluded with no reason" % key
        assert "\n" not in why, "%s's reason is not one line" % key
        assert len(why) < 120, "%s's reason does not fit a tooltip line" % key


def test_every_measured_key_is_in_exactly_one_category():
    seen = {}
    for title, keys, tip in scan.SCAN_CATEGORIES:
        assert tip.strip(), "%s has no tooltip" % title
        for key in keys:
            assert key in scan.SCAN_ALLOWLIST, (
                "%s names %s, which is not measured" % (title, key))
            seen[key] = seen.get(key, 0) + 1
    assert set(seen) == set(scan.SCAN_ALLOWLIST), (
        "measured but invisible: %r" % sorted(set(scan.SCAN_ALLOWLIST) - set(seen)))
    assert [k for k, n in seen.items() if n > 1] == [], "a key is counted twice"


def test_the_dialog_total_is_the_sum_of_its_rows():
    """The banner sums SCAN_ALLOWLIST; the rows are the categories. If they are
    not the same set, the headline number explains itself nowhere on screen."""
    category_keys = {key for _t, keys, _tip in scan.SCAN_CATEGORIES for key in keys}
    assert category_keys == set(scan.SCAN_ALLOWLIST)


def test_the_footnote_counts_match_the_tables():
    measured = len(scan.SCAN_ALLOWLIST)
    total = measured + len(scan.SCAN_EXCLUDED)
    assert total == len(_clean_keys()), (
        "the footnote's total would not match the Clean tab")
    assert ("%d of the %d Clean tasks" % (measured, total)) in scan.SCAN_EXCLUDED_NOTE


def test_the_footnote_names_what_is_left_out():
    note = scan.SCAN_EXCLUDED_NOTE
    for phrase in ("Recycle Bin", "cleanmgr", "audits", "captures"):
        assert phrase in note, "%r is not explained in the footnote" % phrase


def test_the_footnote_promises_unknown_for_a_refusal():
    """The estimate's honesty contract, in the words the user reads: a task
    that declined to look is unknown, not an empty folder."""
    note = scan.SCAN_EXCLUDED_NOTE
    assert "declines to look" in note
    assert "never as 0" in note


def test_no_exclusion_still_blames_the_zero_reading():
    """fivem_cache and steam_depot were excluded because a skip returned 0 and
    a full cache read as empty. `mark_unmeasured` fixed that, so they are
    measured now - a reason citing the old defect would be a stale excuse for
    leaving a working estimate out."""
    for key, why in scan.SCAN_EXCLUDED.items():
        assert "reads as empty" not in why, (
            "%s is still excluded for a defect a refusal note now prevents" % key)
    for key in ("office_cache", "fivem_cache", "steam_depot"):
        assert key in scan.SCAN_ALLOWLIST


# --------------------------------------------------------------------------- #
# The check fires
# --------------------------------------------------------------------------- #

def test_a_task_dropped_from_both_tables_fails(monkeypatch):
    """The silent case: measured -> excluded -> nowhere."""
    excluded = dict(scan.SCAN_EXCLUDED)
    dropped = sorted(scan.SCAN_ALLOWLIST)[0]
    monkeypatch.setattr(scan, "SCAN_ALLOWLIST", scan.SCAN_ALLOWLIST - {dropped})
    monkeypatch.setattr(scan, "SCAN_EXCLUDED", excluded)
    ok, problems = scan.validate_allowlist()
    assert ok is False
    assert any(dropped in p and "neither measured nor excluded" in p
               for p in problems)


def test_a_key_that_is_both_measured_and_excluded_fails(monkeypatch):
    key = sorted(scan.SCAN_ALLOWLIST)[0]
    excluded = dict(scan.SCAN_EXCLUDED)
    excluded[key] = "planted for this test"
    monkeypatch.setattr(scan, "SCAN_EXCLUDED", excluded)
    ok, problems = scan.validate_allowlist()
    assert ok is False
    assert any("both measured and excluded" in p for p in problems)


def test_an_exclusion_with_no_reason_fails(monkeypatch):
    excluded = dict(scan.SCAN_EXCLUDED)
    key = sorted(excluded)[0]
    excluded[key] = "   "
    monkeypatch.setattr(scan, "SCAN_EXCLUDED", excluded)
    ok, problems = scan.validate_allowlist()
    assert ok is False
    assert any("no reason" in p for p in problems)


def test_a_category_naming_an_unmeasured_key_fails(monkeypatch):
    categories = tuple(
        ("Game files & caches", keys + ("not_measured",), tip)
        if title == "Game files & caches" else (title, keys, tip)
        for title, keys, tip in scan.SCAN_CATEGORIES)
    monkeypatch.setattr(scan, "SCAN_CATEGORIES", categories)
    ok, problems = scan.validate_allowlist()
    assert ok is False
    assert any("not_measured" in p for p in problems)


def test_a_key_in_two_categories_fails(monkeypatch):
    categories = tuple(
        ("Game files & caches", keys + (sorted(scan.SCAN_ALLOWLIST)[0],), tip)
        if title == "Game files & caches" else (title, keys, tip)
        for title, keys, tip in scan.SCAN_CATEGORIES)
    monkeypatch.setattr(scan, "SCAN_CATEGORIES", categories)
    ok, problems = scan.validate_allowlist()
    assert ok is False
    assert any("counted twice" in p for p in problems)


def test_a_measured_key_in_no_category_fails(monkeypatch):
    dropped = sorted(scan.SCAN_ALLOWLIST)[0]
    categories = tuple(
        (title, tuple(k for k in keys if k != dropped), tip)
        for title, keys, tip in scan.SCAN_CATEGORIES)
    monkeypatch.setattr(scan, "SCAN_CATEGORIES", categories)
    ok, problems = scan.validate_allowlist()
    assert ok is False
    assert any("cannot show it" in p for p in problems)


def test_the_guard_runs_this_validation(monkeypatch):
    """CI reaches it through the dry-run guard, not through a step of its own -
    so a table disagreement fails the same build a mutation would."""
    import dev.tools.verify_dryrun_scope as guard

    monkeypatch.setattr(scan, "validate_allowlist",
                        lambda: (False, ["planted table problem"]))
    report = guard.scan()
    assert any("planted table problem" in problem
               for problem in report.problems)


# --------------------------------------------------------------------------- #
# The measurement contract the allowlist depends on
# --------------------------------------------------------------------------- #

class _Task:
    def __init__(self, key, run):
        self.key = key
        self.run = run


def test_an_excluded_key_measures_zero_and_is_never_called(monkeypatch):
    """A task outside the allowlist must not be invoked at all: 0 is the
    documented contract for "not estimated", and running it would be the
    side effect the allowlist exists to prevent."""
    calls = []

    def exploded(ctx):
        calls.append("called")
        raise AssertionError("an excluded task must never run during a scan")

    excluded_key = sorted(scan.SCAN_EXCLUDED)[0]
    measured_key = sorted(scan.SCAN_ALLOWLIST)[0]
    monkeypatch.setattr(
        scan, "_clean_task_by_key",
        lambda: {excluded_key: _Task(excluded_key, exploded),
                 measured_key: _Task(measured_key, lambda ctx: 42)})

    ctx = scan.make_measure_ctx()
    sizes = scan.measure_clean_tasks(ctx, [excluded_key, measured_key])
    assert calls == []
    assert sizes[excluded_key] == 0
    assert sizes[measured_key] == 42


def test_a_task_that_declines_to_look_is_unknown_not_zero(monkeypatch):
    """The refusal case, which used to be indistinguishable from an empty
    folder: "Office may be running" returned 0 and the category read clean."""
    measured_key = "office_cache"

    def refuses(ctx):
        mark_unmeasured(ctx, "Office may be running")
        return 0

    monkeypatch.setattr(
        scan, "_clean_task_by_key",
        lambda: {measured_key: _Task(measured_key, refuses)})
    ctx = scan.make_measure_ctx()
    reasons = {}
    sizes = scan.measure_clean_tasks(ctx, [measured_key], reasons)
    assert sizes[measured_key] is None
    assert scan.sum_measured(sizes) is None
    assert scan.count_unmeasured(sizes) == 1
    assert reasons == {measured_key: "Office may be running"}


def test_a_zero_without_a_refusal_is_still_a_real_measurement(monkeypatch):
    """The other half of the contract: an empty folder genuinely is 0 bytes,
    and turning that into unknown would make every tidy PC look unmeasured."""
    measured_key = "office_cache"
    monkeypatch.setattr(
        scan, "_clean_task_by_key",
        lambda: {measured_key: _Task(measured_key, lambda ctx: 0)})
    ctx = scan.make_measure_ctx()
    sizes = scan.measure_clean_tasks(ctx, [measured_key])
    assert sizes[measured_key] == 0
    assert scan.count_unmeasured(sizes) == 0


def test_one_tasks_refusal_does_not_taint_the_next(monkeypatch):
    """The watermark is taken per task: a note left by the previous cleaner
    must not turn a real number from the next one into unknown."""
    def refuses(ctx):
        mark_unmeasured(ctx, "Steam is running")
        return 0

    def measures(ctx):
        return 4096

    monkeypatch.setattr(
        scan, "_clean_task_by_key",
        lambda: {"steam_depot": _Task("steam_depot", refuses),
                 "winget_cache": _Task("winget_cache", measures)})
    ctx = scan.make_measure_ctx()
    sizes = scan.measure_clean_tasks(ctx, ["steam_depot", "winget_cache"])
    assert sizes["steam_depot"] is None
    assert sizes["winget_cache"] == 4096


def test_sum_known_keeps_the_numbers_it_has():
    assert scan.sum_known({"a": 10, "b": None, "c": 5}) == 15
    assert scan.sum_known({"a": None}) == 0
    assert scan.sum_known({}) == 0
    # and the headline total still refuses to invent one
    assert scan.sum_measured({"a": 10, "b": None}) is None


def test_a_named_refusal_is_a_known_skip_for_the_total():
    """Strict by default; a caller that can name the skip gets the lower-bound
    total instead of losing the number because Steam happened to be open."""
    sizes = {"steam_depot": None, "inet_cache": 10}
    assert scan.sum_measured(sizes) is None
    assert scan.sum_measured(
        sizes, {"steam_depot": "Steam is running"}) == 10


def test_a_failure_poisons_the_total_even_beside_a_named_refusal():
    """The refusal allowance must not become a hole for a real failure."""
    sizes = {"steam_depot": None, "winget_cache": None, "inet_cache": 10}
    assert scan.sum_measured(
        sizes, {"steam_depot": "Steam is running"}) is None


def test_the_health_total_survives_a_refusal_but_not_a_failure(monkeypatch):
    """`measure_all_cached` feeds the Health card and the Low-Space nudge: a
    refusal must not turn its number into "?" (the card would then say "another
    scan is running", which is false), while a failure still must."""
    monkeypatch.setattr(scan, "cached_estimate", lambda *a, **k: None)
    stored = []

    def store(total, declined=()):
        stored.append((total, tuple(declined)))
    monkeypatch.setattr(scan, "store_estimate", store)
    monkeypatch.setattr(
        scan, "SCAN_ALLOWLIST", frozenset({"steam_depot", "winget_cache"}))

    def refuses(ctx):
        mark_unmeasured(ctx, "Steam is running")
        return 0

    def measures(ctx):
        return 2048

    monkeypatch.setattr(
        scan, "_clean_task_by_key",
        lambda: {"steam_depot": _Task("steam_depot", refuses),
                 "winget_cache": _Task("winget_cache", measures)})
    assert scan.measure_all_cached() == 2048
    # the skip travels WITH the total, so a cache reader can call it a lower
    # bound instead of quoting it as a complete figure
    assert stored == [(2048, ("Steam is running",))]

    def boom(ctx):
        raise RuntimeError("planted failure")

    monkeypatch.setattr(
        scan, "_clean_task_by_key",
        lambda: {"steam_depot": _Task("steam_depot", boom),
                 "winget_cache": _Task("winget_cache", measures)})
    stored.clear()
    assert scan.measure_all_cached() is None
    assert stored == []


def test_a_raised_measurement_is_unknown_not_zero(monkeypatch):
    """MED-002, the invariant the whole 0-vs-None contract rests on."""
    measured_key = sorted(scan.SCAN_ALLOWLIST)[0]

    def boom(ctx):
        raise RuntimeError("planted failure")

    monkeypatch.setattr(
        scan, "_clean_task_by_key",
        lambda: {measured_key: _Task(measured_key, boom)})
    ctx = scan.make_measure_ctx()
    sizes = scan.measure_clean_tasks(ctx, [measured_key])
    assert sizes[measured_key] is None
    assert scan.sum_measured(sizes) is None
    assert scan.count_unmeasured(sizes) == 1


@pytest.mark.parametrize("key", sorted(scan.SCAN_ALLOWLIST))
def test_every_measured_key_names_a_real_task(key):
    assert key in _clean_keys()


# --------------------------------------------------------------------------- #
# The refusal sites the scan relies on
#
# The mechanism above is only worth having if the cleaners that skip actually
# say so. Each case here is a real `return 0` a measuring pass can reach, with
# the probe that produces it - True for "the game is live", None for "the
# probe could not answer" (which fails closed and skips the same way).
# --------------------------------------------------------------------------- #

def _ctx():
    from app.utils import TaskContext
    return TaskContext(log=lambda _m: None, set_status=lambda _m: None)


@pytest.mark.parametrize("probe", [True, None])
def test_office_cache_records_the_refusal_it_returns_zero_for(monkeypatch, probe):
    from app.tasks import clean_tasks

    monkeypatch.setattr(clean_tasks, "_office_running", lambda: probe)
    ctx = _ctx()
    assert clean_tasks.clean_office_cache(ctx) == 0
    assert ctx.unmeasured, "a skipped Office cache must say why"
    assert "Office" in ctx.unmeasured[0]


def test_office_cache_does_not_note_a_genuinely_empty_cache(monkeypatch):
    from app.tasks import clean_tasks

    monkeypatch.setattr(clean_tasks, "_office_running", lambda: False)
    monkeypatch.setattr(clean_tasks, "_clean_many",
                        lambda _ctx, _folders, _label: 0)
    ctx = _ctx()
    assert clean_tasks.clean_office_cache(ctx) == 0
    assert ctx.unmeasured == []


@pytest.mark.parametrize("probe", [True, None])
def test_fivem_cache_records_the_refusal_it_returns_zero_for(monkeypatch, probe):
    from app.tasks import game_tasks

    monkeypatch.setattr(game_tasks, "_process_running", lambda _img: probe)
    ctx = _ctx()
    assert game_tasks.clean_fivem_cache(ctx) == 0
    assert ctx.unmeasured and "FiveM" in ctx.unmeasured[0]


@pytest.mark.parametrize("probe", [True, None])
def test_steam_depot_records_the_refusal_it_returns_zero_for(monkeypatch, probe):
    from app.tasks import clean_tasks

    monkeypatch.setattr(clean_tasks, "_steam_running", lambda: probe)
    ctx = _ctx()
    assert clean_tasks.clean_steam_download_cache(ctx) == 0
    assert ctx.unmeasured and "Steam" in ctx.unmeasured[0]


def test_orphan_scan_records_a_refusal_on_a_partial_installed_list(monkeypatch):
    import app.game_catalog as catalog
    from app.tasks import clean_tasks

    monkeypatch.setattr(clean_tasks, "_orphan_scan_roots", lambda: [])
    monkeypatch.setattr(catalog, "installed_display_names",
                        lambda: {"planted app %d" % i for i in range(24)})
    ctx = _ctx()
    assert clean_tasks.clean_orphan_program_leftovers(ctx) == 0
    assert ctx.unmeasured and "installed" in ctx.unmeasured[0]


def test_a_derived_context_keeps_the_refusal_notes():
    """A task that re-wraps its context must still reach the caller's list,
    for the same reason `notes` and `changes` are passed by reference."""
    inner = _ctx()
    outer = inner.derive(log=lambda _m: None)
    mark_unmeasured(outer, "planted")
    assert inner.unmeasured == ["planted"]
