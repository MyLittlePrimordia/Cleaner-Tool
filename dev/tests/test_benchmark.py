"""N-002: the lightweight before/after reading and its honest compare().

Pure, no Tk: compare() is the whole contract, and it has to be honest in
three directions -- report a real move, call a sub-floor wiggle "no
measurable change", and stay silent entirely when a reading never happened.
"""
from __future__ import annotations

from app import benchmark


def _snap(mem_avail=None, mem_total=None, restart=None):
    return {"mem_total": mem_total, "mem_avail": mem_avail,
            "restart_pending": restart}


def test_compare_reports_a_real_memory_gain():
    rows = benchmark.compare(
        _snap(1_000_000_000, 16_000_000_000, False),
        _snap(3_000_000_000, 16_000_000_000, False))
    assert len(rows) == 1
    assert rows[0]["label"] == "Available memory"
    assert rows[0]["text"].startswith("+")
    assert rows[0]["tone"] == "good"


def test_compare_reports_a_memory_loss():
    rows = benchmark.compare(
        _snap(3_000_000_000, 16_000_000_000),
        _snap(1_000_000_000, 16_000_000_000))
    assert rows[0]["tone"] == "bad"
    assert rows[0]["text"].startswith("\u2212")


def test_small_memory_wiggle_is_no_measurable_change():
    # A 4 MiB move -- below the 16 MiB floor -- is background noise.
    rows = benchmark.compare(
        _snap(1_000_000_000, 16_000_000_000),
        _snap(1_004_000_000, 16_000_000_000))
    assert rows == []


def test_noise_floor_scales_with_installed_ram():
    # 0.5% of 64 GiB is ~320 MiB, so a 200 MiB move must still read as noise.
    total = 64 * 1024 ** 3
    rows = benchmark.compare(
        _snap(10_000_000_000, total),
        _snap(10_200_000_000, total))
    assert rows == []


def test_restart_flag_transitions_are_reported_with_the_right_tone():
    pending = benchmark.compare(_snap(1, 2, False), _snap(1, 2, True))
    assert pending and pending[0]["label"] == "Restart"
    assert pending[0]["tone"] == "warn"
    cleared = benchmark.compare(_snap(1, 2, True), _snap(1, 2, False))
    assert cleared and cleared[0]["tone"] == "good"


def test_unreadable_snapshots_say_nothing():
    assert benchmark.compare(None, None) is None
    assert benchmark.compare({}, "not a snapshot") is None
    # both present but every reading None -> nothing usable, so stay silent
    assert benchmark.compare(_snap(None, None, None),
                             _snap(None, None, None)) is None


def test_measured_but_unchanged_is_an_empty_list_not_none():
    # memory unusable, only the restart flag known and unchanged: that IS a
    # measurement, so the caller may say "no measurable change" -- not silence.
    assert benchmark.compare(_snap(None, None, False),
                             _snap(None, None, False)) == []


def test_snapshot_shape_never_raises():
    snap = benchmark.snapshot()
    assert set(snap) == {"mem_total", "mem_avail", "restart_pending"}
    assert snap["mem_avail"] is None or isinstance(snap["mem_avail"], int)
    assert snap["mem_total"] is None or isinstance(snap["mem_total"], int)
    assert snap["restart_pending"] in (True, False, None)


def test_restart_pending_is_tristate():
    assert benchmark.restart_pending() in (True, False, None)
