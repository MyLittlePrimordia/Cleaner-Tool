"""Storage runway: how long until a drive fills at the pace it is going.

This is a real trend, not a guess dressed as one. The app records a free-space
sample at most every six hours (from Storage Insight or the existing Quiet
Watch tick), keeps the samples in a small private ledger, and fits a straight
line through the readings for one drive. A forecast is only produced when
there are at least two readings covering at least half a day, so a single
fresh install cannot pretend to know the future.

The wording follows the same rule as the rest of the app:

    forecast     a measured downward trend; says "about N days"
    stable       the trend is flat or rising; says it is not filling
    collecting   fewer than two readings / too short a span; says so
    no_data      no reading for that drive; says so, never invents 0 days

The ledger is separate from config.json because it is machine-generated and
rewritten by a background tick. It contains drive letters, byte counts and
timestamps only - no paths, file names or account data.
"""
from __future__ import annotations

import os
import threading
import time

#: Do not write a second sample inside this window. Six hours is long enough
#: for a real day-over-day slope and short enough that a user who opens the
#: app morning and evening gets a usable baseline the next day.
MIN_GAP = 6 * 3600
#: Two readings this far apart are the minimum for a forecast.
MIN_SPAN = 12 * 3600
#: Enough history to see a month of behaviour without an unbounded file.
KEEP_DAYS = 120
MAX_SAMPLES = 480

_LOCK = threading.Lock()


def state_file():
    """The ledger path, resolved lazily so a portable run follows its home."""
    try:
        from app.config_paths import get_config_dir
        return get_config_dir() / "storage_runway.json"
    except Exception:
        from app.config_paths import CONFIG_DIR
        return CONFIG_DIR / "storage_runway.json"


def _empty():
    return {"v": 1, "samples": []}


def _read(path=None):
    path = path or state_file()
    try:
        import json
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except Exception:
        return _empty()
    if not isinstance(data, dict) or not isinstance(data.get("samples"), list):
        return _empty()
    return data


def _write(path, data):
    try:
        import json
        path = str(path)
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(data, fh)
        os.replace(tmp, path)
        return True
    except Exception:
        return False


def list_drives():
    """[(letter, root, free_bytes, total_bytes)] for ready fixed drives.

    Uses the same GetLogicalDrives + GetDiskFreeSpaceExW pair as the status
    row: both are instant and need no WMI. Empty on non-Windows or when the
    probe is unavailable; callers treat that as "no reading", not "no space".
    """
    try:
        import ctypes
        bitmask = ctypes.windll.kernel32.GetLogicalDrives()
    except Exception:
        return []
    out = []
    for i in range(26):
        if not (bitmask >> i) & 1:
            continue
        letter = chr(ord("A") + i)
        root = "%s:\\" % letter
        free = ctypes.c_ulonglong(0)
        total = ctypes.c_ulonglong(0)
        avail = ctypes.c_ulonglong(0)
        try:
            ok = ctypes.windll.kernel32.GetDiskFreeSpaceExW(
                ctypes.c_wchar_p(root), ctypes.byref(avail),
                ctypes.byref(total), ctypes.byref(free))
        except Exception:
            continue
        if ok and total.value > 0:
            out.append((letter, root, int(free.value), int(total.value)))
    out.sort(key=lambda d: (d[0] != "C", d[0]))
    return out


def _normalise_drives(drives):
    out = {}
    for item in drives or ():
        try:
            letter = str(item[0]).split(":", 1)[0].strip().upper()
            free = int(item[2])
            total = int(item[3])
        except (IndexError, TypeError, ValueError):
            continue
        if not letter or total <= 0 or free < 0:
            continue
        out[letter] = [free, total]
    return out


def record(drives=None, now=None, path=None):
    """Append one sample, at most once per MIN_GAP. True when written.

    `drives` is injectable for tests (same shape as `list_drives`). Never
    raises: losing a sample costs a later forecast, not the app.
    """
    path = path or state_file()
    now = time.time() if now is None else float(now)
    if drives is None:
        drives = list_drives()
    stamp = _normalise_drives(drives)
    if not stamp:
        return False
    with _LOCK:
        data = _read(path)
        samples = data.get("samples") if isinstance(data, dict) else []
        if not isinstance(samples, list):
            samples = []
        last = samples[-1].get("t") if samples and isinstance(samples[-1], dict) else None
        try:
            if last is not None and now - float(last) < MIN_GAP:
                return False
        except (TypeError, ValueError):
            pass
        samples.append({"t": now, "d": stamp})
        cutoff = now - KEEP_DAYS * 86400
        kept = []
        for sample in samples:
            try:
                if float(sample.get("t") or 0) >= cutoff:
                    kept.append(sample)
            except Exception:
                continue
        data = {"v": 1, "samples": kept[-MAX_SAMPLES:]}
        return _write(path, data)


def _points(letter, samples):
    letter = str(letter or "").strip().upper().split(":", 1)[0]
    out = []
    for sample in samples or ():
        if not isinstance(sample, dict):
            continue
        drives = sample.get("d")
        if not isinstance(drives, dict):
            continue
        value = drives.get(letter)
        if not isinstance(value, (list, tuple)) or len(value) < 2:
            continue
        raw_t = sample.get("t")
        if raw_t is None:
            continue
        try:
            free = float(value[0])
            total = float(value[1])
            t = float(raw_t)
        except (TypeError, ValueError):
            continue
        # 0 is a valid timestamp for a ledger written by a test or an import;
        # only a negative or unusable reading is skipped. Rejecting 0 dropped
        # the baseline and made every trend look like "collecting".
        if t < 0 or total <= 0 or free < 0:
            continue
        out.append((t, free, total))
    out.sort(key=lambda p: p[0])
    return out


def _fit(points):
    """Least-squares slope in bytes/day and the latest point, or None."""
    if len(points) < 2:
        return None
    n = len(points)
    mean_t = sum(p[0] for p in points) / n
    mean_f = sum(p[1] for p in points) / n
    denom = sum((p[0] - mean_t) ** 2 for p in points)
    if denom <= 0:
        return None
    slope_sec = sum((p[0] - mean_t) * (p[1] - mean_f)
                    for p in points) / denom
    return slope_sec * 86400.0, points[-1]


def forecast(letter, samples=None, now=None):
    """Forecast one drive. A dict with a state; never a fabricated date."""
    if samples is None:
        samples = _read().get("samples") or []
    points = _points(letter, samples)
    if not points:
        return {"letter": str(letter), "state": "no_data", "readings": 0}
    if len(points) < 2:
        return {"letter": str(letter), "state": "collecting",
                "readings": len(points), "needed": 2}
    span = points[-1][0] - points[0][0]
    if span < MIN_SPAN:
        return {"letter": str(letter), "state": "collecting",
                "readings": len(points), "needed": 2}
    fitted = _fit(points)
    if fitted is None:
        return {"letter": str(letter), "state": "collecting",
                "readings": len(points), "needed": 2}
    rate, latest = fitted
    free_now, total = latest[1], latest[2]
    # A tiny drift is maintenance, not a countdown. The floor scales with
    # drive size so a 4 TB archive drive is not called "stable" while losing
    # 4 GB a day, and a 128 GB stick is not called "filling" over 40 MB.
    floor = max(50 * 1024 * 1024, total * 0.0002)
    if rate >= -floor:
        return {"letter": str(letter), "state": "stable",
                "bytes_per_day": rate, "free": free_now, "total": total,
                "readings": len(points), "span_days": span / 86400.0}
    days = max(0.0, free_now / (-rate))
    return {"letter": str(letter), "state": "forecast",
            "days": days, "bytes_per_day": rate, "free": free_now,
            "total": total, "readings": len(points),
            "span_days": span / 86400.0}


def summaries(samples=None, now=None):
    """One forecast per drive present in the newest sample."""
    if samples is None:
        samples = _read().get("samples") or []
    latest = {}
    for sample in samples or ():
        if isinstance(sample, dict) and isinstance(sample.get("d"), dict):
            latest = sample["d"]
    return [forecast(letter, samples, now=now)
            for letter in sorted(latest, key=lambda s: (s != "C", s))]


def describe(info):
    """One honest sentence for a forecast dict; pure and testable."""
    info = info if isinstance(info, dict) else {}
    letter = str(info.get("letter") or "?")
    state = info.get("state")
    if state == "forecast":
        try:
            days = max(0, int(round(float(info.get("days") or 0))))
        except (TypeError, ValueError):
            days = 0
        if days <= 0:
            return "%s: is full now." % letter
        return ("%s: about %d day%s left at the current pace."
                % (letter, days, "" if days == 1 else "s"))
    if state == "stable":
        return "%s: is not filling at the current pace." % letter
    if state == "collecting":
        return "%s: collecting a baseline - runway needs more readings." % letter
    return "%s: no runway reading yet." % letter
