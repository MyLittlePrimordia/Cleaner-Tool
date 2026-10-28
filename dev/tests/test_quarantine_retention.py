"""U-006: the Quarantine bin's retention window.

The window is the only path in the app that deletes files a user chose to
keep, so these tests pin the two things that make it safe: 0 means never, and
a manifest row can never widen the delete to somewhere outside the bin.
"""
from __future__ import annotations

import json
import os
import time

from app import quarantine
from app.config_schema import QUARANTINE_DAYS, apply_schema


def _bin(tmp_path):
    root = str(tmp_path / "bin")
    os.makedirs(os.path.join(root, "files"), exist_ok=True)
    return root


def _plant(root, name, age_days, body=b"x" * 10):
    """A quarantined file plus its manifest row, `age_days` days old."""
    path = os.path.join(root, "files", name)
    with open(path, "wb") as fh:
        fh.write(body)
    stamp = time.time() - age_days * 86400
    with open(os.path.join(root, "manifest.jsonl"), "a", encoding="utf-8") as fh:
        fh.write(json.dumps({"src": r"C:\Games\%s" % name, "dst": name,
                             "size": len(body), "ts": stamp}) + "\n")
    return path


def test_zero_days_means_keep_until_emptied(tmp_path):
    root = _bin(tmp_path)
    path = _plant(root, "old.dat", 400)
    assert quarantine.expire(root, days=0) == (0, 0)
    assert os.path.exists(path)


def test_expire_clears_only_the_window(tmp_path):
    root = _bin(tmp_path)
    old = _plant(root, "old.dat", 40)
    fresh = _plant(root, "fresh.dat", 2)
    count, freed = quarantine.expire(root, days=30)
    assert count == 1 and freed == 10
    assert not os.path.exists(old)
    assert os.path.exists(fresh)
    # the fresh row is still restorable; the old row is gone, not left dangling
    assert [e["dst"] for e in quarantine.list_entries(root)] == ["fresh.dat"]


def test_expire_never_follows_a_forged_row(tmp_path):
    root = _bin(tmp_path)
    outside = os.path.join(str(tmp_path), "precious.txt")
    with open(outside, "w", encoding="utf-8") as fh:
        fh.write("do not delete")
    with open(os.path.join(root, "manifest.jsonl"), "a", encoding="utf-8") as fh:
        fh.write(json.dumps({"src": r"C:\Games\x.dll",
                             "dst": r"..\precious.txt",
                             "size": 12,
                             "ts": time.time() - 400 * 86400}) + "\n")
    assert quarantine.expire(root, days=7) == (0, 0)
    assert os.path.exists(outside)


def test_expire_keeps_a_row_it_could_not_delete(tmp_path, monkeypatch):
    root = _bin(tmp_path)
    _plant(root, "locked.dat", 40)

    real_remove = os.remove

    def _locked(path, *a, **k):
        if path.endswith("locked.dat"):
            raise PermissionError("in use")
        return real_remove(path, *a, **k)

    monkeypatch.setattr(quarantine.os, "remove", _locked)
    assert quarantine.expire(root, days=7) == (0, 0)
    assert [e["dst"] for e in quarantine.list_entries(root)] == ["locked.dat"]


def test_lost_manifest_line_is_swept_by_file_age(tmp_path):
    root = _bin(tmp_path)
    stray = os.path.join(root, "files", "orphan.dat")
    with open(stray, "wb") as fh:
        fh.write(b"y" * 5)
    aged = time.time() - 40 * 86400
    os.utime(stray, (aged, aged))
    assert quarantine.expire(root, days=30) == (1, 5)
    assert not os.path.exists(stray)


def test_oldest_days_reports_the_oldest_held_file(tmp_path):
    root = _bin(tmp_path)
    assert quarantine.oldest_days(root) is None
    _plant(root, "fresh.dat", 1)
    _plant(root, "old.dat", 12)
    assert quarantine.oldest_days(root) == 12


def test_retention_setting_survives_a_hand_edit():
    defaults = {"quarantine_days": 0}
    for stored, expected in ((30, 30), ("30", 0), (True, 0), (3650, 0),
                             (-7, 0), (None, 0)):
        assert apply_schema({"quarantine_days": stored}, defaults)[
            "quarantine_days"] == expected
    assert 0 in QUARANTINE_DAYS and 7 in QUARANTINE_DAYS
