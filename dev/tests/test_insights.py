"""AUDIT TX-002: findings outlive the window that measured them.

These tests pin the contract every tool and consumer depends on: one row per
id, bounded text, stale rows expire, a re-scan owns its namespace, and a
storage failure never breaks the tool that found something.
"""
from __future__ import annotations

import time

import pytest

from app import insights
from app.insights import DEFAULT_TTL, Finding, Insights, normalise


class Memory:
    """An in-memory config with a settable clock."""

    def __init__(self, now=1_000.0):
        self.rows = []
        self.now = float(now)

    def load(self):
        return [dict(row) for row in self.rows]

    def save(self, rows):
        self.rows = [dict(row) for row in rows]

    def clock(self):
        return self.now

    def advance(self, seconds):
        self.now += float(seconds)


@pytest.fixture
def memory():
    return Memory()


@pytest.fixture
def bus(memory):
    return Insights(load=memory.load, save=memory.save, clock=memory.clock)


def finding(fid="storage.junk", **kw):
    base = dict(
        id=fid, domain="storage", severity="warn",
        title="6.00 GB of junk can be reclaimed",
        detail="Measured across 9 categories.",
        evidence={"bytes": 6 * 1024 ** 3, "categories": 9},
        action={"kind": "route", "target": "storage", "label": "Open Storage"},
        observed_at=1_000.0,
    )
    base.update(kw)
    return Finding(**base)


# --------------------------------------------------------------------------- #
# The row
# --------------------------------------------------------------------------- #
def test_a_finding_is_bounded_and_carries_its_evidence():
    row = normalise({
        "id": "drivetoolkit.drive.corsair",
        "domain": "STORAGE",
        "severity": "CRITICAL",
        "title": "  Corsair   is failing ",
        "detail": "Back up now.",
        "evidence": {"health": "Unhealthy", "size_gb": 1.0, "ok": True,
                     "nested": {"x": 1}, "nan": float("nan")},
        "action": {"kind": "route", "target": "drivetoolkit",
                   "label": "Open Drive Toolkit"},
        "at": 1_000.0,
    }, now=1_000.0)

    assert row is not None
    assert row.domain == "storage" and row.severity == "critical"
    assert row.title == "Corsair is failing"
    assert row.evidence == {"health": "Unhealthy", "size_gb": 1.0, "ok": True}
    assert row.action["target"] == "drivetoolkit"


def test_structural_junk_is_dropped_not_repaired():
    now = 1_000.0
    assert normalise({"id": "ok.id", "domain": "storage",
                      "severity": "nope", "title": "x"}, now) is None
    assert normalise({"id": "ok.id", "domain": "elsewhere",
                      "severity": "warn", "title": "x"}, now) is None
    assert normalise({"id": "bad id", "domain": "storage",
                      "severity": "warn", "title": "x"}, now) is None
    assert normalise({"id": "ok.id", "domain": "storage",
                      "severity": "warn", "title": "  "}, now) is None
    assert normalise({"id": "ok.id", "domain": "storage",
                      "severity": "warn", "title": "x", "at": -5}, now)


def test_a_bad_action_loses_the_action_not_the_conclusion():
    row = normalise(finding(action={"kind": "teleport", "target": ""}))
    assert row is not None
    assert row.action == {}


def test_a_future_timestamp_is_not_immortal():
    row = normalise(finding(observed_at=9_999.0), now=1_000.0)
    assert row.observed_at == 1_000.0


# --------------------------------------------------------------------------- #
# The store
# --------------------------------------------------------------------------- #
def test_publish_keeps_one_row_per_id_and_only_newer_observations(bus, memory):
    bus.publish(finding(title="old"))
    bus.publish(finding(title="older", observed_at=900.0))
    assert bus.active()[0].title == "old"

    bus.publish(finding(title="new", observed_at=1_050.0))
    assert [f.title for f in bus.active()] == ["new"]
    assert len(memory.rows) == 1


def test_replace_is_authoritative_for_its_namespace(bus):
    bus.publish(finding("storage.junk"), finding("readiness.power", domain="gaming",
                                                  title="Power plan"))
    bus.replace("storage.junk", finding(title="1.00 GB", observed_at=1_100.0))

    rows = bus.active()
    assert {f.id for f in rows} == {"storage.junk", "readiness.power"}
    assert rows[0].title == "1.00 GB"

    bus.replace("storage.junk")
    assert [f.id for f in bus.active()] == ["readiness.power"]


def test_replace_needs_a_real_namespace(bus):
    with pytest.raises(ValueError):
        bus.replace("", finding())


def test_stale_findings_expire(bus, memory):
    bus.publish(finding(observed_at=1_000.0))
    memory.advance(DEFAULT_TTL + 1)
    assert bus.active() == []
    # A fresh observation replaces the stale row rather than being ignored.
    bus.publish(finding(observed_at=memory.now))
    assert len(bus.active()) == 1


def test_active_filters_and_sorts_worst_first(bus):
    bus.publish(
        finding("a", severity="info", title="info", observed_at=1_000.0),
        finding("b", severity="warn", title="warn", observed_at=1_030.0),
        finding("c", severity="critical", domain="gaming", title="critical",
                observed_at=1_010.0),
    )
    assert [f.id for f in bus.active()] == ["c", "b", "a"]
    assert [f.id for f in bus.active(min_severity="warn")] == ["c", "b"]
    assert [f.id for f in bus.active(domain="gaming")] == ["c"]
    assert [f.id for f in bus.active(domain=("storage",))] == ["b", "a"]
    with pytest.raises(ValueError):
        bus.active(domain="nonsense")
    with pytest.raises(ValueError):
        bus.active(min_severity="fatal")


def test_retract_and_clear(bus):
    bus.publish(finding("a"), finding("b", domain="gaming", title="b"))
    assert bus.retract("a", "missing") == 1
    assert [f.id for f in bus.active()] == ["b"]
    assert bus.clear() is True
    assert bus.active() == []


def test_the_board_is_capped(bus):
    for i in range(30):
        bus.publish(finding("id.%02d" % i, title="row %d" % i,
                            observed_at=970.0 + i))
    assert len(bus.active(max_age=10 * 86400)) == 24
    assert bus.active(max_age=10 * 86400)[0].id == "id.29"


def test_a_failed_save_is_not_a_failed_tool():
    def explode(_rows):
        raise OSError("disk full")

    broken = Insights(load=lambda: [], save=explode, clock=lambda: 1_000.0)
    assert broken.publish(finding()) == []
    assert broken.active() == []


# --------------------------------------------------------------------------- #
# Schema bounds and persistence
# --------------------------------------------------------------------------- #
def test_the_schema_bounds_and_dedupes_the_board():
    from app.config_schema import INSIGHTS_MAX, apply_schema

    rows = []
    for i in range(INSIGHTS_MAX + 6):
        rows.append(finding("id.%02d" % i, observed_at=1_000.0 + i).as_dict())
    rows.append({"id": "bad id", "domain": "storage", "severity": "warn",
                 "title": "x", "at": 1_000})
    rows.append(finding("no.at", observed_at=0.0).as_dict())
    rows.append(finding("id.29", title="newest", observed_at=2_000.0).as_dict())
    # A later duplicate in the file must not overwrite the newer observation.
    rows.append(finding("id.29", title="older copy", observed_at=1_500.0).as_dict())

    data = {"insights": rows}
    apply_schema(data, {"insights": []})
    kept = data["insights"]

    assert len(kept) == INSIGHTS_MAX
    assert all(row["id"] != "bad id" for row in kept)
    assert all(row["id"] != "no.at" for row in kept)
    assert kept[-1]["id"] == "id.29" and kept[-1]["title"] == "newest"


def test_the_default_bus_round_trips_through_config():
    from app.config_persist import load_config

    insights.clear()
    try:
        insights.publish(finding(observed_at=time.time()))
        stored = load_config().get("insights") or []
        assert stored and stored[0]["id"] == "storage.junk"
        assert insights.active()[0].title.startswith("6.00 GB")
    finally:
        insights.clear()


# --------------------------------------------------------------------------- #
# Publishers
# --------------------------------------------------------------------------- #
def test_drive_toolkit_publishes_bad_drives_and_clears_a_recovered_one(
        monkeypatch, bus):
    monkeypatch.setattr(insights, "_BUS", bus)
    from app import drive_toolkit

    drive_toolkit.publish_health_findings([
        {"name": "Corsair MP600", "health": "Unhealthy", "media_type": "SSD",
         "size_bytes": 1 << 30},
        {"name": "Samsung 870", "health": "Healthy", "media_type": "SSD",
         "size_bytes": 1 << 30},
    ])
    rows = bus.active()
    assert len(rows) == 1
    assert rows[0].severity == "critical"
    assert rows[0].id == "drivetoolkit.drive.corsair-mp600"

    # An empty read means "could not check", not "all healthy".
    drive_toolkit.publish_health_findings([])
    assert len(bus.active()) == 1

    drive_toolkit.publish_health_findings([
        {"name": "Corsair MP600", "health": "Healthy", "media_type": "SSD",
         "size_bytes": 1 << 30},
    ])
    assert bus.active() == []


def test_readiness_publishes_only_the_warnings_it_owns(monkeypatch, bus):
    monkeypatch.setattr(insights, "_BUS", bus)
    from app import game_readiness

    game_readiness.publish_findings([
        {"key": "power_plan", "label": "Power plan", "state": "warn",
         "detail": "Balanced."},
        {"key": "gpu_driver", "label": "Graphics driver", "state": "warn",
         "detail": "Old."},
        {"key": "game_mode", "label": "Game Mode", "state": "ok",
         "detail": "On."},
        {"key": "refresh", "label": "Refresh rate", "state": "info",
         "detail": "Unknown."},
    ])
    assert [f.id for f in bus.active()] == ["readiness.power-plan"]


def test_storage_publishes_a_complete_large_walk_and_clears_a_clean_one(
        monkeypatch, bus):
    monkeypatch.setattr(insights, "_BUS", bus)
    from app import storage_scan

    storage_scan.publish_junk_finding(None, 9, 0)
    assert bus.active() == []

    storage_scan.publish_junk_finding(6 * 1024 ** 3, 9, 2)
    row = bus.active()[0]
    assert row.confidence == "partial"
    assert row.evidence["bytes"] == 6 * 1024 ** 3

    storage_scan.publish_junk_finding(1 * 1024 ** 3, 9, 0)
    assert bus.active() == []


class _Walk:
    """One fake cleaner for the walk tests: a key and what it reports."""

    def __init__(self, key, run):
        self.key = key
        self.run = run


def _fake_walk(monkeypatch, sizes, *, refused=()):
    """Patch the walk's own internals so a real walk sees fake cleaners.

    `sizes` maps a cleaner key to the bytes it reports (None = it raised).
    `refused` names the cleaners that declined to look, in their own words.
    Returns the list of `store_estimate` calls the walk makes.
    """
    from app.utils import mark_unmeasured
    from app import storage_scan

    stored = []
    monkeypatch.setattr(storage_scan, "cached_estimate", lambda *a, **k: None)
    monkeypatch.setattr(storage_scan, "store_estimate",
                        lambda total, declined=(): stored.append(
                            (total, tuple(declined))))
    monkeypatch.setattr(storage_scan, "SCAN_ALLOWLIST", frozenset(sizes))

    def _run(key):
        def run(ctx):
            if key in refused:
                mark_unmeasured(ctx, refused[key])
                return 0
            value = sizes[key]
            if value is None:
                raise RuntimeError("planted failure")
            return value
        return run

    monkeypatch.setattr(storage_scan, "_clean_task_by_key",
                        lambda: {k: _Walk(k, _run(k)) for k in sizes})
    return stored


def _walk_result(monkeypatch, sizes, *, refused=(), cancelled=False):
    """Run a real `measure_all_cached` walk over fake cleaners."""
    from app import storage_scan

    stored = _fake_walk(monkeypatch, sizes, refused=refused)
    total = storage_scan.measure_all_cached(cancelled=lambda: cancelled)
    return total, stored


def test_a_checkup_walk_puts_its_conclusion_on_the_bus(monkeypatch, bus):
    """The checkup and the low-space nudge walk the same folders Storage
    Insight does. Their walk used to end at a card: the junk row could only
    appear (or come back after a clean invalidated it) if the user opened
    Storage, so the automatic checkup measured a problem and told nobody."""
    monkeypatch.setattr(insights, "_BUS", bus)

    total, _stored = _walk_result(monkeypatch,
                                  {"user_temp_files": 6 * 1024 ** 3})

    assert total == 6 * 1024 ** 3
    row = bus.active()[0]
    assert row.id == "storage.junk"
    assert row.evidence["bytes"] == 6 * 1024 ** 3
    assert row.confidence == "measured"
    assert row.action == {"kind": "route", "target": "storage",
                          "label": "Open Storage"}


def test_the_checkup_card_and_the_bus_quote_one_number(monkeypatch, bus):
    """The whole point, end to end: the checkup walks, the card shows the
    number, and the bus row carries the same one - so a checkup can no longer
    measure a problem, print it, and leave the rest of the app ignorant."""
    monkeypatch.setattr(insights, "_BUS", bus)
    from app import health_scan
    from app.utils import TaskContext

    _fake_walk(monkeypatch, {"user_temp_files": 6 * 1024 ** 3})

    _grade, headline, _detail, _note = health_scan.sense_junk(
        TaskContext(log=lambda _m: None, set_status=lambda _m: None))

    row = bus.active()[0]
    assert "6.00 GB" in headline, headline
    assert "6.00 GB" in row.title, row.title
    assert row.evidence["bytes"] == 6 * 1024 ** 3


def test_a_walk_that_declined_to_look_publishes_a_lower_bound(monkeypatch, bus):
    """Steam being open is a known skip, not a failure, so the walk still has
    a number - but it is a lower bound and the row has to say so."""
    monkeypatch.setattr(insights, "_BUS", bus)

    total, _stored = _walk_result(monkeypatch,
                                  {"user_temp_files": 6 * 1024 ** 3,
                                   "launcher_cache": 0},
                                  refused={"launcher_cache": "Steam is running"})

    assert total == 6 * 1024 ** 3
    row = bus.active()[0]
    assert row.confidence == "partial"
    assert row.evidence["declined"] == 1


def test_a_walk_below_the_threshold_clears_the_row_it_contradicts(monkeypatch,
                                                                 bus):
    """A fresh walk is authoritative: finding little junk must take the old
    warning down, not leave it standing next to a smaller number."""
    monkeypatch.setattr(insights, "_BUS", bus)
    from app import storage_scan
    storage_scan.publish_junk_finding(9 * 1024 ** 3, 5, 0)
    assert len(bus.active()) == 1

    total, _stored = _walk_result(monkeypatch,
                                  {"user_temp_files": 1 * 1024 ** 3})

    assert total == 1 * 1024 ** 3
    assert bus.active() == []


def test_a_cancelled_walk_concludes_nothing(monkeypatch, bus):
    """F11, at the publish end: a cancelled walk has no standing to put a
    number on the bus or in the cache. The keys it never reached are absent
    from the sum, so a partial total would look like a complete one."""
    monkeypatch.setattr(insights, "_BUS", bus)

    total, stored = _walk_result(monkeypatch, {"user_temp_files": 4 * 1024 ** 3},
                                 cancelled=True)

    assert total is None
    assert stored == []
    assert bus.active() == []


def test_a_cached_reading_is_not_republished(monkeypatch, bus):
    """A cache hit is the previous walk's conclusion, already on the bus: a
    checkup that only read the cache must not rewrite it (churn with no new
    information - and a rewrite would resurrect a row a clean retracted)."""
    monkeypatch.setattr(insights, "_BUS", bus)
    from app import storage_scan

    monkeypatch.setattr(storage_scan, "cached_estimate",
                        lambda *a, **k: (6 * 1024 ** 3, 1.0))
    monkeypatch.setattr(storage_scan, "_clean_task_by_key",
                        lambda: (_ for _ in ()).throw(
                            AssertionError("a cache hit must not walk")))

    assert storage_scan.measure_all_cached() == 6 * 1024 ** 3
    assert bus.active() == []


def test_a_clean_that_removed_bytes_takes_the_junk_estimate_down(monkeypatch):
    """The estimate is what the cleaners WOULD free, so a run that removed
    bytes from one of them makes it a pre-run reading: Health and the hub
    would otherwise point at a fix that just happened.

    The rule lives on the run that spent the estimate (it used to sit in
    app/smartcare.py, which nothing in the app ever called), so it is tested
    where the run engine and the walker meet, not inside a screen.
    """
    monkeypatch.setattr(insights, "retract",
                        lambda *ids: ids.count("storage.junk"))
    from app import storage_scan

    assert storage_scan.invalidate_junk_reading(["user_temp_files"], 4096)
    # A run of keys the estimate does not measure: emptying the Recycle Bin is
    # real cleaning, but it is not part of this number (the scan's own
    # footnote says the Bin is excluded), so the reading still stands.
    assert not storage_scan.invalidate_junk_reading(["recycle_bin"], 4096)
    # Nothing removed: everything the walk counted may still be there.
    assert not storage_scan.invalidate_junk_reading(["user_temp_files"], 0)
    # An unreadable total is not evidence that something was removed.
    assert not storage_scan.invalidate_junk_reading(["user_temp_files"], None)
    # A task that frees nothing on disk cannot invalidate a disk reading.
    assert not storage_scan.invalidate_junk_reading(["ram_purge"], 4096)


def test_the_cached_walk_goes_with_the_finding_it_was_quoted_from(monkeypatch):
    """The 5-minute estimate cache is the same reading as the finding. Left in
    place, the next checkup would quote the pre-run total the run just spent."""
    monkeypatch.setattr(insights, "retract", lambda *ids: 1)
    from app import storage_scan

    # Restored afterwards: the cache is module state, not config.
    monkeypatch.setattr(storage_scan, "_ESTIMATE_CACHE",
                        storage_scan._ESTIMATE_CACHE)
    storage_scan.store_estimate(9 * 1024 ** 3)
    assert storage_scan.cached_estimate() is not None

    assert storage_scan.invalidate_junk_reading(["user_temp_files"], 4096)
    assert storage_scan.cached_estimate() is None


def test_startup_manager_publishes_the_enabled_count_only(monkeypatch, bus):
    monkeypatch.setattr(insights, "_BUS", bus)
    from app import startup_manager
    min_items = startup_manager.STARTUP_FINDING_MIN

    items = [{"name": "app%d" % i, "enabled": True}
             for i in range(min_items - 1)]
    startup_manager.publish_startup_finding(items)
    assert bus.active() == []

    items.append({"name": "last", "enabled": True})
    startup_manager.publish_startup_finding(items)
    row = bus.active()[0]
    assert row.id == "startup.enabled"
    assert row.evidence == {"enabled": min_items, "total": min_items}

    # A failed read has no standing to clear a real observation.
    startup_manager.publish_startup_finding(None)
    assert len(bus.active()) == 1

    # Switching one off is a real change; below the threshold it clears.
    items[0]["enabled"] = False
    startup_manager.publish_startup_finding(items)
    assert bus.active() == []


def test_services_publishes_a_service_in_a_user_folder(monkeypatch, bus,
                                                       tmp_path):
    monkeypatch.setattr(insights, "_BUS", bus)
    from app import svcs

    monkeypatch.setattr(svcs, "_user_roots", lambda: [str(tmp_path)])
    exe = tmp_path / "Updater.exe"
    data = {"source": "wmi", "error": None, "services": [{
        "name": "Updater", "display": "Updater Service",
        "path": '"%s" --service' % exe, "publisher": "Acme",
        "running": True, "auto": True, "microsoft": False,
    }]}
    svcs.publish_service_findings(data)
    row = bus.active()[0]
    assert row.id == "services.user_path"
    assert row.severity == "critical"
    assert row.evidence["running"] == 1

    # A fallback read cannot see publishers or make a clean claim, so it
    # must not erase the finding.
    svcs.publish_service_findings({"source": "fallback", "error": None,
                                   "services": []})
    assert len(bus.active()) == 1

    svcs.publish_service_findings({"source": "wmi", "error": None,
                                   "services": []})
    assert len(bus.active()) == 1       # an empty list is not a clean machine

    clean = dict(data)
    clean["services"] = [{
        "name": "Safe", "display": "Safe Service",
        "path": r"C:\Program Files\Safe\safe.exe", "publisher": "Acme",
        "running": True, "auto": True, "microsoft": False,
    }]
    svcs.publish_service_findings(clean)
    assert bus.active() == []


def test_services_publishes_many_strict_third_party_services(monkeypatch, bus):
    monkeypatch.setattr(insights, "_BUS", bus)
    from app import svcs

    monkeypatch.setattr(svcs, "_user_roots", lambda: [])
    rows = [{"name": "svc%d" % i, "display": "Vendor %d" % i,
             "path": r"C:\Program Files\Vendor\v.exe",
             "publisher": "Vendor Ltd", "running": True, "auto": True}
            for i in range(svcs.SERVICE_LOAD_MIN)]
    svcs.publish_service_findings({"source": "wmi", "error": None,
                                   "services": rows})
    row = bus.active()[0]
    assert row.id == "services.background"
    assert row.evidence["count"] == svcs.SERVICE_LOAD_MIN

    # Microsoft components do not count, even when their publisher string is
    # the "Windows (R) Win 7 DDK provider" form.
    for r in rows:
        r["publisher"] = "Windows (R) Win 7 DDK provider"
    svcs.publish_service_findings({"source": "wmi", "error": None,
                                   "services": rows})
    assert bus.active() == []


def test_duplicates_publishes_and_clears_after_cleanup(monkeypatch, bus):
    monkeypatch.setattr(insights, "_BUS", bus)
    from app import dupes

    dupes.publish_reclaim_finding({"wasted": 3 * 1024 ** 3, "groups": 4})
    row = bus.active()[0]
    assert row.id == "duplicates.files"
    assert row.evidence["groups"] == 4

    dupes.publish_reclaim_finding({"wasted": 0, "groups": 0})
    assert bus.active() == []


# --------------------------------------------------------------------------- #
# Consumers
# --------------------------------------------------------------------------- #
def test_the_hub_recommends_the_tool_that_found_it(monkeypatch):
    from app import tool_hub

    monkeypatch.setattr(insights, "active", lambda **kw: [
        finding("drivetoolkit.drive.c", domain="storage", severity="critical",
                title="Corsair MP600 is failing",
                detail="Back up your files now.",
                action={"kind": "route", "target": "drivetoolkit",
                        "label": "Open Drive Toolkit"}),
        finding("readiness.power", domain="gaming", severity="warn",
                title="Power plan", detail="Balanced.",
                action={"kind": "route", "target": "readiness",
                        "label": "Open Game Readiness"}),
    ])
    monkeypatch.setattr(tool_hub, "_latest_checkup", lambda now=None: None)
    monkeypatch.setattr(tool_hub, "_drive_recommendation", lambda: None)

    recs = tool_hub.recommendations()
    assert [r[0] for r in recs] == ["drivetoolkit", "game_readiness"]
    assert recs[0][2] == "F"
    assert "Corsair" in recs[0][1]
    assert recs[1][1].startswith("Power plan")


def test_the_hub_badges_only_the_tools_that_carry_a_live_count(monkeypatch):
    from app import tool_hub

    monkeypatch.setattr(insights, "active", lambda **kw: [
        finding("services.background", domain="system", severity="warn",
                action={"kind": "route", "target": "services"}),
        finding("services.user_path", domain="system", severity="critical",
                action={"kind": "route", "target": "services"}),
        finding("startup.enabled", domain="system", severity="warn",
                action={"kind": "route", "target": "startup"}),
        finding("storage.junk",
                action={"kind": "route", "target": "storage"}),
        finding("duplicates.files",
                action={"kind": "route", "target": "duplicates"}),
        finding("readiness.power-plan", domain="gaming", severity="critical",
                action={"kind": "route", "target": "readiness"}),
    ])

    assert tool_hub.badges() == {
        "services": (2, "critical"),
        "startup": (1, "warn"),
        "storage": (1, "warn"),
        "duplicates": (1, "warn"),
        "game_readiness": (1, "critical"),
    }

    monkeypatch.setattr(insights, "active", lambda **kw: [])
    assert tool_hub.badges() == {}


def test_the_hub_maps_a_services_finding_to_its_card(monkeypatch):
    from app import tool_hub

    monkeypatch.setattr(insights, "active", lambda **kw: [
        finding("services.user_path", domain="system", severity="critical",
                title="A service runs from a user folder",
                action={"kind": "route", "target": "services"}),
    ])
    monkeypatch.setattr(tool_hub, "_latest_checkup", lambda now=None: None)
    monkeypatch.setattr(tool_hub, "_drive_recommendation", lambda: None)

    assert [r[0] for r in tool_hub.recommendations()] == ["services"]


def test_the_hub_keeps_the_worst_claim_for_a_tool(monkeypatch):
    from app import tool_hub

    monkeypatch.setattr(insights, "active", lambda **kw: [
        finding("duplicates.files", severity="warn", title="First",
                action={"kind": "route", "target": "duplicates"}),
        finding("storage.junk", severity="critical", title="Second",
                action={"kind": "route", "target": "storage"}),
    ])
    monkeypatch.setattr(tool_hub, "_latest_checkup", lambda now=None: None)
    monkeypatch.setattr(tool_hub, "_drive_recommendation", lambda: None)

    # A critical storage finding outranks a warning about duplicates.
    assert [r[0] for r in tool_hub.recommendations()] == [
        "storage", "duplicates"]


