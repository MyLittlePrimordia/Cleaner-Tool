"""
Auto-elevate ("stop asking me") via an on-demand elevated scheduled task.

Mechanism: Windows cannot silently elevate a running process (that is the
whole point of UAC), but Task Scheduler can launch a program elevated
without prompting when the TASK was created with admin consent. So: one
approval at opt-in creates `CleanerTool_ElevatedLaunch` (/RL HIGHEST, /IT
interactive, /SC ONCE with a long-past trigger so it never fires on a
schedule); every later unelevated launch just `schtasks /Run`s it and
exits. The elevated copy is a normal full-GUI launch (no special flags),
so it skips the Admin Gate via the regular is_admin() path.

Design rules (same discipline as app/scheduler.py):
  * builders are pure argv lists (shell=False), unit-tested, quoting via
    subprocess.list2cmdline — paths with spaces ( portable exe on the
    Desktop) work.
  * the task pins an absolute path: a moved exe is detected by /TR
    comparison and falls back to today's Gate (never a hang, never a
    launch of a stale copy).
  * creating/deleting a HIGHEST task needs admin: creation happens in the
    reconciler whenever the process IS elevated; the unelevated side only
    ever reads (/Query) and runs (/Run) — both consent-free.
  * explicit opt-in only (config `auto_elevate`), visible off-switch
    (same flag; the reconciler deletes the task), clearly-named task.
  * never raises out of the reconcile/handoff paths: any failure means
    "launch normally", i.e. today's behavior.

Portable-first: no installer, no service, no per-machine writes. Config
stays per-user (LOCALAPPDATA); the elevated copy runs as the same user
(InteractiveToken), so HKCU and the config file just work.
"""

from __future__ import annotations

import os
import subprocess
import sys

TASK_NAME = "CleanerTool_ElevatedLaunch"
TASK_DESC = "Cleaner Tool silent elevated relaunch (opt-in: Always start as Administrator)"

# Dormant trigger: a task with no fireable trigger never runs on a
# schedule — only on explicit /Run. /SC ONCE still requires /ST, so pin
# both to a long-past instant (deterministic dormancy at any hour, no
# midnight edge the way "today at 00:00" would have).
_DORMANT_DATE = "01/01/2000"
_DORMANT_TIME = "00:00"


def _current_exe_and_args(exe_override: str | None = None):
    """(executable, [args]) the elevated task must launch: this exe, no
    special flags (an elevated normal launch skips the Gate by itself).
    `exe_override` is the test seam (paths with spaces, frozen vs source).
    Mirrors app/scheduler._get_executable_and_args."""
    if exe_override is not None:
        return exe_override, []
    if getattr(sys, "frozen", False):
        try:
            _args = ["--tray"] if "--tray" in sys.argv else []
        except Exception:
            _args = []
        return sys.executable, _args
    script = None
    try:
        script = os.path.abspath(sys.argv[0])
    except Exception:
        pass
    if not script or not os.path.isfile(script):
        try:
            script = os.path.abspath(
                os.path.join(os.path.dirname(__file__), "..", "main.py"))
        except Exception:
            script = "main.py"
    args = [script]
    # a --tray launch that hands off must stay a --tray launch on the
    # elevated side, or the helper task would pop a window the user asked
    # never to see.
    try:
        if "--tray" in sys.argv and "--tray" not in args:
            args.append("--tray")
    except Exception:
        pass
    return sys.executable, args


def build_create_cmd(exe_override: str | None = None) -> "list[str]":
    """schtasks /Create for the dormant elevated task. Pure builder."""
    from app.utils import resolve_exe
    exe, args = _current_exe_and_args(exe_override)
    task_run = subprocess.list2cmdline([exe] + args)
    return [
        resolve_exe("schtasks"), "/Create", "/TN", TASK_NAME,
        "/TR", task_run,
        "/SC", "ONCE", "/SD", _DORMANT_DATE, "/ST", _DORMANT_TIME,
        "/RL", "HIGHEST", "/IT", "/F",
    ]


def build_run_cmd() -> "list[str]":
    from app.utils import resolve_exe
    return [resolve_exe("schtasks"), "/Run", "/TN", TASK_NAME]


def build_query_cmd() -> "list[str]":
    from app.utils import resolve_exe
    return [resolve_exe("schtasks"), "/Query", "/TN", TASK_NAME,
            "/V", "/FO", "LIST"]


def build_delete_cmd() -> "list[str]":
    from app.utils import resolve_exe
    return [resolve_exe("schtasks"), "/Delete", "/TN", TASK_NAME, "/F"]


def _run(cmd) -> "tuple[bool, str]":
    """Run one schtasks command. (ok, output). Never raises."""
    try:
        result = subprocess.run(
            cmd, shell=False, capture_output=True, text=True, timeout=30,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        out = (result.stdout or "") + (result.stderr or "")
        return result.returncode == 0, out
    except Exception as exc:
        return False, str(exc)


def parse_task_run(query_output: str) -> "str | None":
    """Extract the task's command line from `schtasks /Query /V /FO LIST`
    output (the 'Task To Run:' row). None when absent/unparseable. Pure."""
    try:
        for line in (query_output or "").splitlines():
            if line.strip().lower().startswith("task to run"):
                if ":" in line:
                    return line.split(":", 1)[1].strip()
                return ""
    except Exception:
        pass
    return None


def _norm_tr(tr: str) -> str:
    try:
        return (tr or "").strip().strip('"').strip().lower()
    except Exception:
        return ""


def current_task_run() -> "str | None":
    """This exe's expected /TR string (for drift comparison)."""
    try:
        exe, args = _current_exe_and_args()
        return subprocess.list2cmdline([exe] + args)
    except Exception:
        return None


def status() -> "tuple[bool, bool, str]":
    """(exists, points_at_me, raw_output). Read-only probe (/Query) —
    safe to call unelevated, in tests, anywhere. Never raises."""
    ok, out = _run(build_query_cmd())
    if not ok:
        return False, False, out
    tr = parse_task_run(out)
    want = current_task_run()
    if tr is None or want is None:
        return True, False, out
    return True, _norm_tr(tr) == _norm_tr(want), out


def create_task() -> "tuple[bool, str]":
    """Create (or overwrite, /F) the dormant elevated task. Needs admin —
    callers ensure that (the reconciler only runs elevated)."""
    return _run(build_create_cmd())


def delete_task() -> "tuple[bool, str]":
    """Delete the task. Needs admin to succeed; best-effort otherwise."""
    return _run(build_delete_cmd())


def run_task() -> "tuple[bool, str]":
    """Launch the elevated copy on demand. Needs no admin (running your
    own task is consent-free) — this is the whole trick."""
    return _run(build_run_cmd())


def is_wanted() -> bool:
    """Config opt-in flag (single source of truth for Gate + handoff)."""
    try:
        from app.config_persist import load_config
        return bool(load_config().get("auto_elevate", False))
    except Exception:
        return False


def save_gate_prefs(auto_elevate: bool, remember_limited: bool) -> None:
    """Persist the Admin Gate checkboxes. Single-lock RMW via
    update_config (never a stale whole-dict save). auto_elevate wins on
    read paths when both are somehow set. Never raises."""
    try:
        from app.config_persist import update_config

        def _mut(cfg, _a=bool(auto_elevate), _r=bool(remember_limited)):
            cfg["auto_elevate"] = _a
            cfg["remember_limited"] = _r
        update_config(_mut)
    except Exception:
        pass


def selection_needs_admin(selected: dict, tabs) -> bool:
    """True when any selected task key (any tab) requires admin. Pure:
    unknown keys are ignored (never block the Gate on stale config).
    `tabs` is app.tab_presets.TABS (injected for tests)."""
    try:
        by_key = {}
        for _tab, _tasks in (tabs or {}).items():
            for t in _tasks or []:
                try:
                    by_key[t.key] = t
                except Exception:
                    continue
        for _tab, _keys in (selected or {}).items():
            for k in _keys or []:
                try:
                    t = by_key.get(k)
                    if t is not None and bool(getattr(t, "admin_required", False)):
                        return True
                except Exception:
                    continue
    except Exception:
        pass
    return False


def saved_selection_needs_admin() -> bool:
    """The Gate-skip check: does the user's saved selection contain any
    admin task? False (skip the Gate) on any read failure — failing open
    to limited mode is the safe direction."""
    try:
        from app.config_persist import load_config
        from app.tab_presets import TABS
        selected = load_config().get("selected_tasks", {})
        return selection_needs_admin(selected, TABS)
    except Exception:
        return False


def notice_for_gate() -> "str | None":
    """One-line Gate notice when auto-elevate is wanted but the task won't
    fire (moved exe / deleted task): tells the user exactly what approving
    once more will fix. None when all is well (or when not wanted)."""
    try:
        if not is_wanted():
            return None
        exists, matches, _out = status()
        if exists and matches:
            return None
        if not exists:
            return "Approve once more to fix auto-start."
        return "App moved. Approve once more to fix auto-start."
    except Exception:
        return None


def handoff_ready() -> "tuple[bool, str]":
    """Pure pre-check for the startup handoff: wanted + task present and
    pointing at this exe. The caller (__main__) owns the mutex dance."""
    try:
        if not is_wanted():
            return False, "not opted in"
        exists, matches, _out = status()
        if not exists:
            return False, "task missing"
        if not matches:
            return False, "task points elsewhere"
        return True, "ready"
    except Exception as exc:
        return False, f"check failed: {exc}"


def reconcile() -> "tuple[bool, str]":
    """Converge the live task with config. CALL ONLY WHEN ELEVATED (the
    caller guarantees it): creates a missing/mismatched task when wanted,
    deletes a leftover task when not. The quiet engine behind both the
    opt-in moment and the off-switch. Never raises."""
    try:
        from app.elevation import is_admin
        if not is_admin():
            return False, "reconcile needs admin (no-op)"
        wanted = is_wanted()
        exists, matches, _out = status()
        if wanted:
            if exists and matches:
                return True, "task already correct"
            if exists and not matches:
                delete_task()
            ok, msg = create_task()
            return ok, ("task created" if ok else f"create failed: {msg}")
        else:
            if exists:
                ok, msg = delete_task()
                return ok, ("leftover task removed" if ok else f"delete failed: {msg}")
            return True, "nothing to do"
    except Exception as exc:
        return False, f"reconcile failed: {exc}"
