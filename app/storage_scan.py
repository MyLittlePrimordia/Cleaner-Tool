"""
Storage Insight scan engine (user-approved feature 3, 2026-09).

Two read-only scanners, no UI here (the dialog lives in gui.py):

1. measure_clean_tasks() — invokes real Clean-tab task functions with a
   dry_run TaskContext, so estimates come from the SAME folder lists a
   real run would clean (no duplicated path knowledge to drift).
   ONLY tasks on SCAN_ALLOWLIST are invocable: each was individually
   audited (2026-09) to call nothing but clean_folder_contents /
   _clean_many / read-only helpers (tasklist, listdir, registry reads).
   Everything with side effects — service stop/start (win_update_cache,
   delivery_optimization, font_cache), taskkill (thumbnail_cache),
   wsreset/cleanmgr/ipconfig/appx/schtasks/wevtutil/powershell-removes,
   direct os.remove calls (chk_fragments, recycle_bin Memory.dmp,
   steam appinfo, terminal history), registry deletes
   (activity_traces) — is EXCLUDED. An excluded task is never estimated;
   the dialog says so honestly instead of inventing numbers.

2. find_game_libraries() — sizes installed games per launcher for the
   "Biggest games" column: Steam libraryfolders.vdf, Epic manifest
   .item files, plus default install roots for GOG/Ubisoft/Riot/Xbox.
   READ-ONLY directory walks (no deletion path exists in this module).

Both scanners honor ctx.cancelled()/cancelled() so closing the dialog
stops the walk promptly, and both skip reparse points (same guard as
the cleaner — a junction must never inflate another drive's total).
"""

from __future__ import annotations

import glob
import json
import os
import re

from app.utils import TaskContext

# --------------------------------------------------------------------------- #
# 1. Junk measurement — audited allowlist + display categories
# --------------------------------------------------------------------------- #

# Every key here was read line-by-line (2026-09 audit): the task function
# touches NOTHING but clean_folder_contents / _clean_many / _clean_files
# (all dry_run-aware) plus read-only probes. Anything else is excluded —
# see the module docstring for the excluded list and why.
SCAN_ALLOWLIST = frozenset({
    "shader_cache", "launcher_cache", "engine_cache", "driver_junk",
    "user_temp_files", "system_temp_files", "inet_cache", "error_reports",
    "gpu_watchdog_dumps", "old_logs", "browser_cache", "office_cache",
    "uwp_cache", "winget_cache", "dev_caches", "pkg_caches",
    "update_leftovers", "defender_history", "prefetch",
    "game_files", "game_captures",
})

# Display groups: (title, member keys in measure order, tooltip text).
# 5 rows fit the dialog with no scrolling; member task labels ride each
# row's tooltip so users can trace an estimate back to real tasks.
SCAN_CATEGORIES = (
    ("Game files & caches",
     ("shader_cache", "launcher_cache", "engine_cache", "driver_junk",
      "game_files", "game_captures"),
     "Shader caches, launcher web caches, engine caches, driver leftovers, "
     "per-game logs/dumps and Game Bar captures."),
    ("Windows temp & logs",
     ("user_temp_files", "system_temp_files", "old_logs", "error_reports",
      "gpu_watchdog_dumps", "prefetch"),
     "Temp folders, setup/event logs, error reports, GPU watchdog dumps "
     "and Prefetch data."),
    ("Browser & app caches",
     ("browser_cache", "inet_cache", "office_cache", "uwp_cache"),
     "Chrome/Edge/Firefox caches, Windows web cache, Office and Store-app caches."),
    ("Update leftovers",
     ("update_leftovers",),
     "Windows.old, $Windows.~BT and other upgrade debris (when present)."),
    ("Dev & package caches",
     ("winget_cache", "dev_caches", "pkg_caches", "defender_history"),
     "winget/npm/pip/NuGet/Cargo/Gradle caches and Defender history."),
)

# What the scan deliberately does NOT estimate (shown as the dialog's
# honest footnote): tasks with side effects outside folder cleaning.
SCAN_EXCLUDED_NOTE = (
    "Estimates cover file-based cleaners only."
)


def _clean_task_by_key() -> dict:
    """Live task objects for the merged Clean tab, keyed by task key.
    Resolved at call time (not import) so the scan always tracks the
    current task table — a renamed key degrades to 'skipped', never a
    crash (same tolerant-resolver philosophy as the scheduler)."""
    from app.tab_presets import TABS
    return {t.key: t for t in TABS.get("Clean", [])}


def make_measure_ctx(set_status=None, cancelled=None) -> TaskContext:
    """A TaskContext for scanning: silent log, live status, dry_run on.
    set_status/cancelled are wired to the dialog (both optional)."""
    return TaskContext(
        log=lambda _m: None,
        set_status=(set_status or (lambda _m: None)),
        cancelled=(cancelled or (lambda: False)),
        dry_run=True,
    )


def measure_clean_tasks(ctx: TaskContext, keys) -> dict:
    """Run the given allowlisted task functions with the dry-run ctx and
    return {key: estimated bytes}. Unknown keys and raising tasks yield
    0 (logged via ctx.set_status, never raised) — a scan must degrade,
    not die. Honors ctx.cancelled() between tasks."""
    by_key = _clean_task_by_key()
    out = {}
    for key in keys:
        if ctx.cancelled():
            break
        task = by_key.get(key)
        if task is None or key not in SCAN_ALLOWLIST:
            out[key] = 0
            continue
        try:
            out[key] = task.run(ctx) or 0
        except Exception:
            out[key] = 0
    return out


def validate_allowlist() -> "tuple[bool, list[str]]":
    """(ok, unknown_keys): the allowlist must stay a subset of the live
    Clean task table — called by the smoke harness on every build."""
    by_key = _clean_task_by_key()
    unknown = sorted(k for k in SCAN_ALLOWLIST if k not in by_key)
    return (not unknown, unknown)


# --------------------------------------------------------------------------- #
# 1b. Scan coordinator (Phase-6 orchestration) — one walk serves all three
#     consumers: Storage dialog, Health's junk sensor, the Low-Space nudge.
#     A process-wide mutex prevents concurrent disk walks; a 5-minute TTL
#     cache lets Health/Nudge quote a fresh walk instead of re-walking.
# --------------------------------------------------------------------------- #

_SCAN_MUTEX = None          # lazy: module import stays cheap for tests
_ESTIMATE_CACHE = None      # (total_bytes, timestamp) or None
_ESTIMATE_TTL_S = 300.0


def _scan_mutex():
    global _SCAN_MUTEX
    if _SCAN_MUTEX is None:
        import threading
        _SCAN_MUTEX = threading.Lock()
    return _SCAN_MUTEX


def acquire_scan(cancelled=None, timeout=30.0):
    """Cooperative scan slot: blocks up to `timeout` for a prior walk to
    finish (Storage closing often cancels its walk mid-flight, so the
    wait is short), then gives up and returns False — callers degrade
    to 'no estimate', never queue up three walks behind each other."""
    import time as _time
    m = _scan_mutex()
    deadline = _time.time() + max(0.0, timeout)
    while True:
        got = m.acquire(timeout=0.25)
        if got:
            return True
        try:
            if cancelled is not None and cancelled():
                return False
        except Exception:
            return False
        try:
            if _time.time() >= deadline:
                return False
        except Exception:
            return False


def release_scan():
    try:
        _scan_mutex().release()
    except Exception:
        pass


def cached_estimate(max_age_s=None):
    """(total_bytes, timestamp) of the last full-allowlist walk when
    fresh enough, else None. Pure read; never walks."""
    import time as _time
    try:
        total, stamp = _ESTIMATE_CACHE
        ttl = _ESTIMATE_TTL_S if max_age_s is None else max_age_s
        if total is not None and (_time.time() - stamp) <= ttl:
            return (total, stamp)
    except Exception:
        pass
    return None


def store_estimate(total_bytes):
    """Publish a finished walk's total (called by the walker itself)."""
    global _ESTIMATE_CACHE
    import time as _time
    try:
        _ESTIMATE_CACHE = (total_bytes, _time.time())
    except Exception:
        pass


def measure_all_cached(set_status=None, cancelled=None):
    """Full-allowlist estimate with cache + coordination (Health/Nudge
    path). Fresh cache hit: instant, zero disk. Otherwise: takes the
    scan slot (brief wait, honest give-up), walks once, publishes the
    cache, releases. Returns total bytes (0 when it couldn't run — the
    honest 'unknown', never a stale number dressed as fresh)."""
    hit = cached_estimate()
    if hit is not None:
        return hit[0]
    if not acquire_scan(cancelled=cancelled):
        return 0
    try:
        if cancelled is not None and cancelled():
            return 0
        ctx = make_measure_ctx(set_status=set_status, cancelled=cancelled)
        total = sum(measure_clean_tasks(ctx, sorted(SCAN_ALLOWLIST)).values())
        store_estimate(total)
        return total
    finally:
        release_scan()


# --------------------------------------------------------------------------- #
# 2. Biggest-games measurement — read-only size walks
# --------------------------------------------------------------------------- #

def _is_link(path: str) -> bool:
    """Reparse-point/junction guard for the size walk (mirrors the
    cleaner's _is_reparse_point without importing a private helper)."""
    try:
        if os.path.islink(path):
            return True
    except Exception:
        return True
    try:
        import ctypes
        attrs = ctypes.windll.kernel32.GetFileAttributesW(str(path))
        if attrs == 0xFFFFFFFF:
            return False
        return bool(attrs & 0x400)  # FILE_ATTRIBUTE_REPARSE_POINT
    except Exception:
        return False


def measure_folder_tree(path: str, cancelled=None, _budget: int = 0) -> int:
    """Sum of file bytes under path (iterative, no recursion limit risk).
    Skips reparse points; checks cancelled() every 256 dirs to stay
    responsive without per-file overhead. Returns the partial total on
    cancel — the dialog labels partial scans honestly."""
    total = 0
    if not path or not os.path.isdir(path) or _is_link(path):
        return 0
    is_cancelled = cancelled or (lambda: False)
    stack = [path]
    checked = 0
    while stack:
        if checked % 256 == 0 and is_cancelled():
            break
        checked += 1
        cur = stack.pop()
        try:
            with os.scandir(cur) as it:
                for entry in it:
                    try:
                        if entry.is_symlink():
                            continue
                        if entry.is_dir(follow_symlinks=False):
                            p = entry.path
                            if not _is_link(p):
                                stack.append(p)
                        elif entry.is_file(follow_symlinks=False):
                            try:
                                total += entry.stat(follow_symlinks=False).st_size
                            except OSError:
                                continue
                    except OSError:
                        continue
        except OSError:
            continue
    return total


def _steam_library_roots() -> "list[str]":
    """All Steam library roots: install root + libraryfolders.vdf extras.
    VDF parsed with a narrow regex (only "path" lines) — no VDF parser
    dependency for one field."""
    roots = []
    steam_root = ""
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                            r"Software\Valve\Steam") as k:
            steam_root, _ = winreg.QueryValueEx(k, "SteamPath")
    except Exception:
        steam_root = ""
    # Steam writes SteamPath with forward slashes — normalize so every
    # downstream join produces clean Windows paths
    steam_root = (steam_root or "").replace("/", "\\")
    if not steam_root or not os.path.isdir(steam_root):
        pf86 = os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")
        steam_root = os.path.join(pf86, "Steam")
    if os.path.isdir(steam_root):
        roots.append(steam_root)
        vdf = os.path.join(steam_root, "steamapps", "libraryfolders.vdf")
        try:
            with open(vdf, "r", encoding="utf-8", errors="replace") as f:
                for m in re.finditer(r'"path"\s+"([^"]+)"', f.read()):
                    p = m.group(1).replace("\\\\", "\\")
                    # normalize any stray forward slashes so downstream
                    # joins produce clean Windows paths (tooltips read
                    # these strings directly)
                    p = p.replace("/", "\\")
                    if os.path.isdir(p) and p not in roots:
                        roots.append(p)
        except OSError:
            pass
    return roots


def _epic_installs() -> "list[tuple[str, str]]":
    """[(display name, install path)] from Epic manifest .item files
    (JSON, first-party format). Unparseable/missing dir -> skipped."""
    out = []
    progdata = os.environ.get("ProgramData", r"C:\ProgramData")
    man_dir = os.path.join(progdata, "Epic", "EpicGamesLauncher",
                           "Data", "Manifests")
    try:
        names = glob.glob(os.path.join(man_dir, "*.item"))
    except OSError:
        names = []
    for fn in names:
        try:
            with open(fn, "r", encoding="utf-8", errors="replace") as f:
                data = json.load(f)
            loc = data.get("InstallLocation", "")
            name = data.get("DisplayName", "") or os.path.basename(loc)
            if loc and os.path.isdir(loc):
                out.append((name, loc))
        except (OSError, ValueError, AttributeError):
            continue
    return out


def _default_root_games() -> "list[tuple[str, str, str]]":
    """[(launcher, game, path)] from well-known default install roots.
    Only top-level directories that exist; each entry guarded."""
    out = []
    pf = os.environ.get("ProgramFiles", r"C:\Program Files")
    pf86 = os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")
    sysdrive = os.environ.get("SYSTEMDRIVE", "C:") + "\\"
    roots = [
        ("GOG", os.path.join(sysdrive, "GOG Games")),
        ("Ubisoft", os.path.join(pf86, "Ubisoft", "Ubisoft Game Launcher", "games")),
        ("Riot", os.path.join(sysdrive, "Riot Games")),
        ("Xbox", os.path.join(sysdrive, "XboxGames")),
        ("EA", os.path.join(pf, "EA Games")),
    ]
    for launcher, root in roots:
        try:
            if not os.path.isdir(root):
                continue
            with os.scandir(root) as it:
                for entry in it:
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            out.append((launcher, entry.name, entry.path))
                    except OSError:
                        continue
        except OSError:
            continue
    return out


def find_game_libraries(cancelled=None, on_game=None) -> list:
    """Ordered-biggest-first list of dicts {launcher, name, path, bytes,
    drive}. Steam common dirs + Epic manifests + default roots. Each
    game measured with measure_folder_tree (cancellable, partial-safe).
    on_game(name) is called per game for live dialog status (optional)."""
    is_cancelled = cancelled or (lambda: False)
    games = []
    seen_paths = set()

    def _add(launcher, name, path):
        if is_cancelled():
            return False
        norm = os.path.normcase(os.path.abspath(path))
        if norm in seen_paths or not os.path.isdir(path):
            return True
        seen_paths.add(norm)
        if on_game is not None:
            try:
                on_game(name)
            except Exception:
                pass
        size = measure_folder_tree(path, cancelled=is_cancelled)
        drive = os.path.splitdrive(path)[0].upper() or "C:"
        games.append({"launcher": launcher, "name": name, "path": path,
                      "bytes": size, "drive": drive})
        return True

    # Steam (per-library common dirs)
    for lib in _steam_library_roots():
        if is_cancelled():
            break
        common = os.path.join(lib, "steamapps", "common")
        try:
            if not os.path.isdir(common):
                continue
            with os.scandir(common) as it:
                entries = sorted(
                    (e.name, e.path) for e in it
                    if e.is_dir(follow_symlinks=False))
        except OSError:
            continue
        for name, path in entries:
            if not _add("Steam", name, path):
                break

    # Epic (manifest installs)
    if not is_cancelled():
        for name, path in _epic_installs():
            if not _add("Epic", name, path):
                break

    # Default roots (GOG / Ubisoft / Riot / Xbox / EA)
    if not is_cancelled():
        for launcher, name, path in _default_root_games():
            if not _add(launcher, name, path):
                break

    games.sort(key=lambda g: g["bytes"], reverse=True)
    return games
