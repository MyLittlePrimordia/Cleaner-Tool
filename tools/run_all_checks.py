"""Run the whole verification suite and report a single pass/fail.

Uses subprocess exit codes, not text matching. An earlier shell runner
grepped the output for the string "FAIL" and reported five false positives,
because several test LABELS contain the word "fail" (e.g. "the fail path",
"cancels on FAIL"). Only the exit code is trustworthy.

Also hashes the user's real config.json before and after the whole run, so a
suite that pollutes it is caught here as well as by
tools/verify_no_config_pollution.py.
"""

from __future__ import annotations

import hashlib
import io
import os
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _config_guard import config_path  # noqa: E402

SUITE = [
    ("compileall", [sys.executable, "-m", "compileall", "-q", "app", "tools"]),
    ("no-globals", [sys.executable, "-u", "tools/verify_no_globals.py"]),
    ("architecture", [sys.executable, "tools/check_architecture.py"]),
    ("single-instance", [sys.executable, "-u", "tools/verify_single_instance.py"]),
    ("config-schema", [sys.executable, "-u", "tools/verify_config_schema.py"]),    ("run-state", [sys.executable, "-u", "tools/verify_run_state.py"]),
    ("run-state-e2e", [sys.executable, "-u", "tools/verify_run_state_e2e.py"]),
    ("run-generation", [sys.executable, "-u", "tools/verify_run_generation.py"]),
    ("tweak-state-view", [sys.executable, "-u", "tools/verify_tweak_state_view.py"]),
    ("session", [sys.executable, "-u", "tools/verify_session_orchestrator.py"]),
    ("preflight-stop", [sys.executable, "-u", "tools/verify_preflight_stop.py"]),
    ("tweak-rollback", [sys.executable, "-u", "tools/verify_tweak_rollback.py"]),
    ("sec001-allowlist", [sys.executable, "-u",
                        "tools/verify_sec001_task_allowlist.py"]),
    ("sec004-migration", [sys.executable, "-u",
                       "tools/verify_sec004_migration.py"]),
    ("prior004-defender", [sys.executable, "-u",
                        "tools/verify_prior004_defender_exclusion.py"]),
    ("prior002-startup", [sys.executable, "-u",
                        "tools/verify_prior002_startup_containment.py"]),
    ("telemetry-undo", [sys.executable, "-u", "tools/verify_telemetry_undo.py"]),
    ("taskbar-undo", [sys.executable, "-u", "tools/verify_taskbar_undo.py"]),
    ("prior010-sched-drift", [sys.executable, "-u",
                             "tools/verify_prior010_scheduler_drift.py"]),
    ("bug022-023-pilot", [sys.executable, "-u",
                          "tools/verify_bug022_023_session_pilot.py"]),
    ("bug018-worker", [sys.executable, "-u",
                       "tools/verify_bug018_worker_release.py"]),
    ("bug021-cmd-arg", [sys.executable, "-u",
                        "tools/verify_bug021_cmd_arg.py"]),
    ("med009-telemetry", [sys.executable, "-u",
                          "tools/verify_med009_telemetry_snapshot.py"]),
    ("med007-undo", [sys.executable, "-u",
                     "tools/verify_med007_snapshotted_undo.py"]),
    ("med010-011-powercfg", [sys.executable, "-u",
                             "tools/verify_med010_011_powercfg_revert.py"]),
    ("med002-004-scan-gates", [sys.executable, "-u",
                               "tools/verify_med002_004_scan_gates.py"]),
    ("med008-denied", [sys.executable, "-u",
                       "tools/verify_med008_denied_vs_absent.py"]),
    ("med014-cross-thread", [sys.executable, "-u",
                             "tools/verify_med014_cross_thread_hops.py"]),
    ("med012-013-url-config", [sys.executable, "-u",
                               "tools/verify_med012_013_url_and_config.py"]),
    ("low005-008-limits", [sys.executable, "-u",
                           "tools/verify_low005_008_resource_limits.py"]),
    ("low001-014-batch", [sys.executable, "-u",
                          "tools/verify_low001_014_batch.py"]),
    ("low003-004-dead", [sys.executable, "-u",
                         "tools/verify_low003_004_dead_code.py"]),
    ("low009-010-info006", [sys.executable, "-u",
                           "tools/verify_low009_010_info006.py"]),
    ("low002-health-scan", [sys.executable, "-u",
                            "tools/verify_health_scan.py"]),
    ("icon-parity", [sys.executable, "-u",
                     "tools/verify_icon_parity.py"]),
    ("app-icons", [sys.executable, "-u", "tools/verify_app_icons.py"]),
    ("install-row-alignment", [sys.executable, "-u",
                               "tools/verify_install_row_alignment.py"]),
    ("scorecard", [sys.executable, "-u", "tools/verify_scorecard_layout.py"]),
    ("pcspecs-dialog", [sys.executable, "-u",
                     "tools/verify_pcspecs_dialog_perf.py"]),
    ("pcspecs-perf", [sys.executable, "-u",
                   "tools/verify_pcspecs_perf.py"]),
    ("procmgr-cpu", [sys.executable, "-u",
                   "tools/verify_procmgr_cpu_scale.py"]),
    ("procmgr-icons", [sys.executable, "-u",
                    "tools/verify_procmgr_icons.py"]),
    ("catalog-chain", [sys.executable, "-u",
                    "tools/verify_catalog_chain.py"]),
    ("procmgr-flicker", [sys.executable, "-u",
                      "tools/verify_procmgr_flicker.py"]),
    ("procmgr-perf", [sys.executable, "-u",
                    "tools/verify_procmgr_perf.py"]),
    ("smoke-gui", [sys.executable, "-u", "tools/smoke_gui.py"]),
    ("smoke-async", [sys.executable, "-u", "tools/smoke_async.py"]),
    ("no-config-pollution", [sys.executable, "-u",
                             "tools/verify_no_config_pollution.py"]),
]


def digest() -> str:
    path = config_path()
    if not os.path.exists(path):
        return "<absent>"
    with io.open(path, "rb") as handle:
        return hashlib.sha256(handle.read()).hexdigest()


def main() -> int:
    before = digest()
    print("  real config sha256 before: %s" % before[:32])
    print()
    width = max(len(n) for n, _ in SUITE)
    results = []
    for name, cmd in SUITE:
        t0 = time.time()
        proc = subprocess.run(cmd, capture_output=True, text=True,
                              encoding="utf-8", errors="replace")
        dt = time.time() - t0
        tail = ""
        for line in reversed((proc.stdout or "").splitlines()):
            if line.strip():
                tail = line.strip()
                break
        ok = proc.returncode == 0
        results.append((name, ok, proc.returncode, dt, tail))
        print("  %s  %-*s  %5.1fs  %s"
              % ("ok  " if ok else "FAIL", width, name, dt, tail[:60]))
        if not ok:
            print("        exit=%d" % proc.returncode)
            for line in (proc.stdout or "").splitlines():
                if line.strip().startswith("FAIL") or "Traceback" in line:
                    print("        %s" % line.strip()[:110])
            for line in (proc.stderr or "").splitlines()[-6:]:
                print("        stderr: %s" % line.strip()[:110])

    after = digest()
    print()
    print("  real config sha256 after : %s" % after[:32])
    clean = after == before
    print("  %s  user config untouched by the whole suite"
          % ("ok  " if clean else "FAIL"))
    if not clean:
        print("        %s -> %s" % (before[:16], after[:16]))

    failed = [n for n, ok, *_ in results if not ok]
    print()
    if failed or not clean:
        print("SUITE: FAILED -> %s" % ", ".join(failed + ([] if clean else ["config-pollution"])))
        return 1
    print("SUITE: ALL GREEN (%d checks, config verified byte-identical)"
          % len(SUITE))
    return 0


if __name__ == "__main__":
    sys.exit(main())
