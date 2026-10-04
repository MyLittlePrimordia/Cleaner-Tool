"""Meta-test: the verification suite must not modify the user's real config.

This exists because it already went wrong twice. ``verify_run_generation.py``
left ``h10_probe`` in ``applied_tweaks``; an earlier version of
``verify_telemetry_undo.py`` wrote a whole ``stop_telemetry`` snapshot into
the real ``config.json`` because ``_merge_tweak_snapshot`` re-imported the
config writers and so bypassed the seam the test was patching.

Individual tests are supposed to restore the file themselves. This checks that
claim from the outside, where it cannot be faked: hash the real config, run
every test that touches config or the run pipeline, hash it again, and compare.

The restore is also enforced here, so a leak found this way is cleaned up
rather than left for the user to find.
"""

from __future__ import annotations

import hashlib
import io
import os
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _config_guard import ConfigGuard, config_path  # noqa: E402

FAILS = 0
CHECKS = 0

# Tests that legitimately need the real config present. Tests that only read
# config, or that fake the writers entirely, are excluded to keep this quick.
RUNNERS = [
    ("verify_run_generation.py", "drives the real run_tasks"),
    ("verify_run_state_e2e.py", "drives the real run_tasks"),
    ("verify_tweak_rollback.py", "drives the real tweak registry"),
    ("verify_telemetry_undo.py", "drives the real config writers"),
    ("verify_tweak_state_view.py", "writes probe keys to the registry"),
    ("verify_config_schema.py", "loads and re-saves the real config"),
    ("verify_preflight_stop.py", "drives the real run pipeline"),
    ("verify_bug018_worker_release.py", "drives the real install/run workers"),
    ("smoke_gui.py", "full GUI smoke over the real config"),
    ("smoke_async.py", "async/process-manager smoke over the real config"),
]


def ck(label: str, cond: bool, detail: object = "") -> None:
    global FAILS, CHECKS
    CHECKS += 1
    if cond:
        print("  PASS  %s" % label)
    else:
        FAILS += 1
        print("  FAIL  %s   <- %s" % (label, detail))


def digest() -> str:
    path = config_path()
    if not os.path.exists(path):
        return "<absent>"
    with io.open(path, "rb") as handle:
        return hashlib.sha256(handle.read()).hexdigest()


def main() -> int:
    if not os.path.exists(config_path()):
        print("  SKIP  no real config.json yet at %s" % config_path())
        print()
        print("NO-POLLUTION VERIFY: SKIPPED")
        return 0

    # The outer guard is the safety net: whatever a test does, the user's file
    # goes back to exactly these bytes when this script exits.
    outer = ConfigGuard("no_pollution")
    outer.__enter__()
    before = digest()
    print("  real config sha256: %s" % before[:32])
    print()

    print("[1] every config-touching test leaves config.json byte-identical")
    for name, why in RUNNERS:
        path = os.path.join("tools", name)
        if not os.path.exists(path):
            ck("%s exists" % name, False, path)
            continue
        t0 = time.time()
        proc = subprocess.run(
            [sys.executable, "-u", os.path.join("tools", name)],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
        )
        elapsed = time.time() - t0
        after = digest()
        ok = after == before
        status = "restored" if ok else "MODIFIED THE USER'S CONFIG"
        ck("%-28s %s" % (name, status), ok,
           "sha %s -> %s" % (before[:12], after[:12]))
        print("        %-26s exit=%d  %.1fs  (%s)" % ("", proc.returncode, elapsed, why))
        if not ok:
            leaked = _diff(before, after)
            for line in leaked[:6]:
                print("          %s" % line)
        outer.__exit__(None, None, None)   # undo whatever it did, right now
        outer.__enter__()                 # re-snapshot the pristine bytes

    print()
    print("[2] the config is exactly as we found it")
    ck("final sha256 matches the initial snapshot", digest() == before,
       "%s -> %s" % (before[:16], digest()[:16]))

    outer.__exit__(None, None, None)

    # Hash equality only proves THIS RUN changed nothing. It cannot see a leak
    # a previous run left behind: that is exactly how a stop_telemetry
    # snapshot survived here for several runs, because every run faithfully
    # preserved the already-polluted file and reported clean. This check looks
    # for the SHAPE of a test leak -- snapshot/applied keys for tweaks nobody
    # applied -- so the damage is reported rather than inherited.
    print()
    print("[3] no stray tweak state from an earlier leaking run")
    try:
        import json
        with io.open(config_path(), encoding="utf-8") as handle:
            live = json.load(handle)
    except Exception as exc:
        live = {}
        ck("config.json is readable", False, repr(exc))
    snaps = live.get("tweak_snapshots") or {}
    applied = live.get("applied_tweaks") or []
    ck("no tweak snapshots are recorded",
       not snaps, "stray snapshot keys: %s" % sorted(snaps)[:6])
    ck("no tweaks are marked applied",
       not applied, "stray applied_tweaks: %s" % sorted(applied)[:6])

    print()
    if FAILS:
        print("NO-POLLUTION VERIFY: %d/%d FAILED" % (FAILS, CHECKS))
        return 1
    print("NO-POLLUTION VERIFY: ALL PASS (%d checks)" % CHECKS)
    return 0


def _diff(before: str, after: str) -> list[str]:
    """Best-effort report of what a leaking test changed."""
    import json
    try:
        sys.path.insert(0, ".")
        from app.config_persist import get_config_dir  # noqa: F401
        with io.open(config_path(), encoding="utf-8") as handle:
            now = json.load(handle)
    except Exception as exc:
        return ["(could not parse config: %r)" % (exc,)]
    lines: list[str] = []
    for key in sorted(set(now) | {"applied_tweaks", "selected_tasks",
                                  "tweak_snapshots", "seen_tips"}):
        val = now.get(key)
        if key in ("applied_tweaks", "tweak_snapshots", "seen_tips") and val:
            lines.append("%s = %r" % (key, val))
    return lines or ["(differs but no obvious culprit key)"]


if __name__ == "__main__":
    sys.exit(main())
