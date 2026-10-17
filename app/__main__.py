#!/usr/bin/env python3
"""
Cleaner Tool 🧼
--------------
Entry point.
  python -m app
  python app/__main__.py
  python main.py  (shim at repo root)

Supports running as a script (app/__main__.py) or as a module (-m).

Headless flags (no window):
  --auto-clean          run the saved Clean/Repair selection now
  --auto-update         run "Update Everything" now
  --dry-run [--json]    read-only preview of what the Clean tasks would
                        reclaim; --json prints a machine-readable report
"""

import sys
import pathlib
import os
import tempfile

# Early logging to capture startup issues
def _early_log(msg):
    """Write to a log file to debug early startup crashes"""
    try:
        log_path = os.path.join(tempfile.gettempdir(), "cleaner_tool_startup.log")
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(f"[{os.getpid()}] {msg}\n")
    except Exception:
        pass

_early_log(f"Starting __main__.py, argv={sys.argv}")

# Ensure repo root is on sys.path when run as `python app/__main__.py`
# This must come before the app.dpi import below, or a direct script
# invocation cannot resolve the `app` package at all.
if __package__ in (None, ""):
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from app.dpi import enable_dpi_awareness as _enable_dpi_awareness  # noqa: E402
_early_log("About to enable DPI awareness")

# Applied at import time on every entry path (this module is imported
# before launch() by main.py / `python app/__main__.py` / `python -m app`),
# because the call only has an effect before Tk initialises. The
# implementation and its rationale now live in app/dpi.py so the smoke
# harness can share them -- it used to run DPI-unaware while this path ran
# DPI-aware, so the tests validated a layout no user ever saw.
_enable_dpi_awareness()
_early_log("DPI awareness enabled")

from app.config_persist import load_config  # noqa: E402
from app.scheduler import run_auto_clean, run_auto_update  # noqa: E402
_early_log("Config and scheduler modules imported")
# M16: gui (4785-line Tk import) used to load even for headless
# --auto-clean/--auto-update runs that never create a window. Import it
# lazily inside main() so the scheduler stays light and decoupled from Tk.
def _launch_gui():
    from app.gui import launch as _launch  # noqa: E402
    _launch()

# Signal elevated startup early (for elevation coordination), but only on Windows
# and after imports so --elevation-token is already in sys.argv.
if sys.platform.startswith("win"):
    try:
        from app.elevation import signal_elevated_startup
        _early_log("About to call signal_elevated_startup")
        print("[__main__ module] Calling signal_elevated_startup", file=sys.stderr)
        signal_elevated_startup()
        _early_log("signal_elevated_startup completed")
        print("[__main__ module] signal_elevated_startup completed", file=sys.stderr)
    except Exception as exc:
        _early_log(f"signal_elevated_startup exception: {exc}")
        print(f"[__main__ module] signal_elevated_startup exception: {exc}", file=sys.stderr)
        pass


def _selected_clean_keys() -> "list[str]":
    """Clean-tab task keys to preview: the user's saved Clean selection when
    it names any keys, else the Clean tab's default-on tasks."""
    try:
        from app.config_persist import load_config
        sel = (load_config().get("selected_tasks") or {}).get("Clean")
    except Exception:
        sel = None
    if isinstance(sel, str):
        sel = [sel]
    keys = [k for k in (sel or []) if isinstance(k, str)]
    if keys:
        return keys
    try:
        from app.tab_presets import TABS
        return [t.key for t in TABS.get("Clean", [])
                if getattr(t, "default", True)]
    except Exception:
        return []


def _run_dry_run(as_json: bool) -> int:
    """N-005: read-only "what would be freed" preview.

    Runs every selected allowlisted Clean task with a dry_run context — the
    same functions and folder lists a real run uses, but nothing is
    deleted. Honors the app's honest 0-vs-None contract: a task that
    cannot be measured reports as "unknown" (bytes null), a task with
    effects outside file cleaning reports as "skipped". Prints a human
    list, or a JSON report with --json. Always exits 0 when it produced a
    report; a crash would propagate."""
    import json as _json

    from app.storage_scan import (
        SCAN_ALLOWLIST, count_unmeasured, make_measure_ctx,
        measure_clean_tasks, sum_measured)
    from app.utils import format_bytes

    keys = _selected_clean_keys()
    ctx = make_measure_ctx()
    sizes = measure_clean_tasks(ctx, keys)
    total = sum_measured(sizes)

    def _entry(key):
        if key not in SCAN_ALLOWLIST:
            return {"key": key, "bytes": None, "status": "skipped"}
        value = sizes.get(key)
        if value is None:
            return {"key": key, "bytes": None, "status": "unknown"}
        return {"key": key, "bytes": value, "status": "measured"}

    targets = [_entry(k) for k in keys]
    skipped = sum(1 for t in targets if t["status"] == "skipped")
    unmeasured = count_unmeasured(sizes)

    if as_json:
        print(_json.dumps({
            "mode": "dry-run",
            "total_bytes": total,
            "total_human": None if total is None else format_bytes(total),
            "targets_total": len(targets),
            "skipped": skipped,
            "unmeasured": unmeasured,
            "targets": targets,
        }, indent=2))
        return 0

    print("Cleaner Tool - dry run (read-only preview)")
    if not targets:
        print("  No Clean tasks selected.")
    for t in targets:
        if t["status"] == "skipped":
            label = "skipped (has effects outside file cleaning)"
        elif t["status"] == "unknown":
            label = "unknown (could not measure)"
        else:
            label = format_bytes(t["bytes"])
        print(f"  {t['key']:<24} {label}")
    if total is None:
        print("  Total: unknown (some targets could not be measured)")
    else:
        print(f"  Total reclaimable: about {format_bytes(total)}")
    return 0


def main():
    _early_log("main() function called")
    if not sys.platform.startswith("win"):
        print("This application is designed for Windows only.")
        return

    # N-005: headless read-only preview. Handled before --auto-clean so a
    # combined invocation (`--dry-run --auto-clean`) stays read-only.
    if "--dry-run" in sys.argv:
        sys.exit(_run_dry_run(as_json="--json" in sys.argv))

    # M16: both flags present used to silently drop --auto-update (first
    # `if` won). Run both in order when combined.
    want_clean = "--auto-clean" in sys.argv
    want_update = "--auto-update" in sys.argv
    if want_clean or want_update:
        code = 0
        if want_clean:
            config = load_config()
            selected = config.get("selected_tasks", {})
            success, summary = run_auto_clean(selected)
            print(f"[AUTO] clean: {summary}")
            code |= 0 if success else 1
        if want_update:
            success, summary = run_auto_update()
            print(f"[AUTO] update: {summary}")
            code |= 0 if success else 1
        sys.exit(code)

    # Single instance (tray/auto-elevate foundation): normal GUI launches
    # exit quietly when a copy already runs (it gets focused instead).
    # The elevation-handshake child and headless runs are exempt by design
    # (see app/single_instance.should_enforce); the child claims the mutex
    # off-thread once the starter exits.
    _owner = None
    try:
        from app import single_instance as _si
        _early_log("Single instance module imported")
        if _si.is_elevation_child():
            print("[__main__] Elevation child detected, claiming mutex in background", file=sys.stderr)
            _early_log("Elevation child detected, claiming mutex in background")
            _si.claim_in_background()
        elif _si.should_enforce():
            print("[__main__] Single instance enforcement active", file=sys.stderr)
            _early_log("Single instance enforcement active")
            _owner = _si.acquire()
            if _owner is None:
                _early_log("Another instance already running, exiting")
                try:
                    _si.signal_existing()
                except Exception:
                    pass
                print("Cleaner Tool is already running.")
                # headsup for double-clickers with no visible window (tray-
                # hidden first copy): best-effort, never raises, then exit.
                try:
                    from app.toast import show_toast as _toast
                    _toast("Cleaner Tool is already running",
                           "The open copy was brought forward.")
                except Exception:
                    pass
                return
            _early_log("Single instance mutex acquired")
            _si.set_owner(_owner)
            _si.ensure_show_event()
    except Exception as exc:
        _early_log(f"Single instance exception: {exc}")
        _owner = None

    # Auto-elevate handoff ("stop asking me"): unelevated + opted in +
    # helper task points at this exe -> fire it and exit quietly. The
    # elevated copy is a normal launch (skips the Gate by is_admin), so
    # the user lands straight in the full app with zero prompts. The
    # mutex is released first so the elevated copy can claim it; a raced
    # twin converges via the same checks on its own startup. ANY failure
    # (or --no-elevate-handoff) falls through to the normal launch below.
    try:
        from app.elevation import is_admin as _is_admin
        from app import elevated_launch as _el
        from app import single_instance as _si2
        if (not _is_admin() and _owner is not None
                and "--no-elevate-handoff" not in sys.argv
                and not _si2.is_elevation_child()
                and not _si2.is_headless_run()):
            print("[__main__] Checking auto-elevate handoff", file=sys.stderr)
            _ready, _why = _el.handoff_ready()
            print(f"[__main__] Handoff ready={_ready}, why={_why}", file=sys.stderr)
            if _ready:
                try:
                    _si2.release(_owner)
                except Exception:
                    pass
                _owner = None
                _ok, _msg = _el.run_task()
                print(f"[__main__] Handoff run_task ok={_ok}, msg={_msg}", file=sys.stderr)
                if _ok:
                    return
                try:
                    _owner = _si2.acquire()
                    if _owner is None:
                        try:
                            _si2.signal_existing()
                        except Exception:
                            pass
                        return
                    _si2.set_owner(_owner)
                    _si2.ensure_show_event()
                except Exception:
                    pass
    except Exception as exc:
        print(f"[__main__] Auto-elevate handoff exception: {exc}", file=sys.stderr)
        pass

    # Reconciler (elevated only, off-thread so startup never waits on
    # schtasks): converges the live helper task with the opt-in flag in
    # both directions — creates after opt-in, deletes after opt-out.
    # Also applies the optional Windows Defender exclusion (same elevated
    # window, same off-thread so the UI is never blocked).
    try:
        from app.elevation import is_admin as _is_admin2
        if _is_admin2():
            import threading as _th

            def _rec():
                try:
                    from app import elevated_launch as _el2
                    _el2.reconcile()
                except Exception:
                    pass
                try:
                    from app.elevation import apply_defender_exclusion_if_wanted
                    apply_defender_exclusion_if_wanted()
                except Exception:
                    pass
            _th.Thread(target=_rec, daemon=True,
                       name="AutoElevateReconcile").start()
    except Exception:
        pass

    _early_log("About to launch GUI")
    _launch_gui()
    _early_log("GUI launch returned (should not happen)")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        _early_log(f"Uncaught exception in main: {exc}")
        import traceback
        _early_log(traceback.format_exc())
        # For elevation children, don't exit immediately - give time to read logs
        if "--elevation-token=" in " ".join(sys.argv):
            import time
            _early_log("Elevation child crashed, waiting 5 seconds before exit")
            time.sleep(5)
        raise
