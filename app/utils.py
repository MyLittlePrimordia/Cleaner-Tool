"""
Shared low-level helpers used by the clean / repair / tweak task modules.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from dataclasses import dataclass, field
from typing import Callable, List, Optional

IS_WINDOWS = sys.platform.startswith("win")

if IS_WINDOWS:
    import winreg
    import ctypes
else:  # pragma: no cover
    winreg = None  # type: ignore
    ctypes = None  # type: ignore

# FILE_ATTRIBUTE_REPARSE_POINT — junctions & directory symlinks. os.walk
# does NOT treat Windows junctions as links (followlinks=False only guards
# POSIX symlinks), so a junction planted in a cleaned folder would redirect
# os.remove THROUGH it into arbitrary targets (verified empirically in the
# audit: a canary file behind a junction was deleted). Everything that
# walks/deletes must skip reparse points.
_FILE_ATTRIBUTE_REPARSE_POINT = 0x400


def _is_reparse_point(path: str) -> bool:
    """True if path is a junction / directory symlink / other reparse point."""
    if not IS_WINDOWS or ctypes is None:
        # POSIX: os.walk(followlinks=False) already skips symlinks for us
        return os.path.islink(path)
    try:
        attrs = ctypes.windll.kernel32.GetFileAttributesW(str(path))
        if attrs == 0xFFFFFFFF:  # INVALID_FILE_ATTRIBUTES
            return False
        return bool(attrs & _FILE_ATTRIBUTE_REPARSE_POINT)
    except Exception:
        return False


import threading
import queue

# Track the currently running subprocess per worker thread so it can be killed on user cancel.
# Use a global registry keyed by thread ident so the main (GUI) thread can cancel
# a command running in the worker thread.
_current_procs: dict[int, subprocess.Popen] = {}
_current_procs_lock = threading.Lock()


def _set_current_proc(proc):
    tid = threading.get_ident()
    with _current_procs_lock:
        if proc is None:
            _current_procs.pop(tid, None)
        else:
            _current_procs[tid] = proc


def _get_current_proc():
    tid = threading.get_ident()
    with _current_procs_lock:
        return _current_procs.get(tid)


def _get_any_proc():
    with _current_procs_lock:
        for p in _current_procs.values():
            if p is not None and p.poll() is None:
                return p
    return None


def _kill_proc(proc: subprocess.Popen):
    """Kill proc and its child tree (needed when shell=True on Windows)."""
    try:
        proc.kill()
    except Exception:
        pass
    if IS_WINDOWS:
        pid = getattr(proc, "pid", None)
        if pid is None:
            return
        # Fire-and-forget taskkill /T so it doesn't block the cancel hot path (timeout=5 would add 5s tail latency)
        def _taskkill():
            try:
                subprocess.run(
                    f"taskkill /PID {pid} /T /F",
                    shell=True,
                    capture_output=True,
                    timeout=5,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
            except Exception:
                pass
        try:
            threading.Thread(target=_taskkill, daemon=True).start()
        except Exception:
            pass


def cancel_current_command():
    """Best-effort kill of the command currently executed by run_cmd()."""
    # Try current thread first, then any worker proc (GUI thread cancelling worker)
    proc = _get_current_proc()
    if proc is None:
        proc = _get_any_proc()
    if proc is not None and proc.poll() is None:
        _kill_proc(proc)
    # Also kill any other tracked procs as fallback
    with _current_procs_lock:
        for p in list(_current_procs.values()):
            if p is not proc and p is not None and p.poll() is None:
                _kill_proc(p)


# --------------------------------------------------------------------------- #
# Task execution context
# --------------------------------------------------------------------------- #

@dataclass
class TaskContext:
    log: Callable[[str], None]
    set_status: Callable[[str], None]
    cancelled: Callable[[], bool] = field(default=lambda: False)


class TaskSkipped(RuntimeError):
    """Raised by an APPLY task when there is genuinely nothing to do on
    this machine (no SSD, no NVIDIA software, monitor already at max Hz,
    ...).

    B5 audit fix: these paths used to log a 'skipping' line and `return`
    None, which the run engine counts as SUCCESS — so the runner recorded
    the tweak as applied and the GUI badged it '✓ Active' although nothing
    changed. Runners catch TaskSkipped, log the reason to the run log and
    count the task as completed-with-skip: no applied-tweak record, no
    badge, no failure dialog. Revert functions deliberately never raise
    it — undoing a never-applied tweak keeps its existing behavior."""


class TaskCancelled(RuntimeError):
    """Raised when a long-running task was stopped (user cancel / command
    killed) instead of finishing. Runners treat it as 'stopped' — honest
    reporting, but neither a success nor a failure."""


# --------------------------------------------------------------------------- #
# Subprocess helper — fixed timeout deadlock, proper wait
# --------------------------------------------------------------------------- #

def run_cmd(ctx: TaskContext, command: str, shell: bool = True, timeout: Optional[int] = None,
            collect: "list | None" = None) -> int:
    """Run a command, streaming each output line to ctx.log.

    `collect` (optional): when given a list, every stripped non-empty output
    line is also appended to it — lets long batch runs (e.g. `winget
    upgrade --all`) build an honest per-item success/failure summary without
    a second slow invocation. Backward compatible: all existing callers
    omit it and behave exactly as before."""
    ctx.log(f"$ {command}")
    try:
        startupinfo = None
        creationflags = 0
        if IS_WINDOWS:
            startupinfo = subprocess.STARTUPINFO()
            startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            creationflags = subprocess.CREATE_NO_WINDOW

        proc = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            # H2 fix: default text-mode decoding is strict and can raise on
            # undefined bytes in localized (non-English) OEM/cp1252 output
            # (e.g. German "ü"). The reader thread's broad except swallowed
            # that exception, sent an EOF sentinel, and the outer loop fell
            # into proc.wait(timeout=None) — after which neither the
            # timeout nor cancel checks apply anymore, so a long repair
            # command (sfc/DISM, up to 1800s) could hang indefinitely with
            # its output silently cut off. errors="replace" keeps decoding
            # even on bytes with no cp1252 mapping instead of raising.
            errors="replace",
            shell=shell,
            startupinfo=startupinfo,
            creationflags=creationflags,
            bufsize=1,
        )
        _set_current_proc(proc)
        try:
            # Stream output via a reader thread so we can poll cancellation/timeout
            # without blocking on readline(). This fixes the hang where silent
            # commands (sfc, DISM) emit nothing for minutes and Cancel never fires.
            q: queue.Queue[Optional[str]] = queue.Queue()

            def _reader():
                try:
                    for raw_line in proc.stdout:  # type: ignore[union-attr]
                        q.put(raw_line)
                except Exception:
                    pass
                finally:
                    q.put(None)  # sentinel: EOF
                    try:
                        if proc.stdout:
                            proc.stdout.close()
                    except Exception:
                        pass

            reader_thread = threading.Thread(target=_reader, daemon=True)
            reader_thread.start()

            start_time = time.time()
            while True:
                if ctx.cancelled():
                    _kill_proc(proc)
                    ctx.log("  ! command cancelled")
                    # Drain reader thread briefly — kill is already async, so short join is enough
                    reader_thread.join(timeout=0.5)
                    return -1
                if timeout is not None and (time.time() - start_time) > timeout:
                    _kill_proc(proc)
                    ctx.log("  ! command timed out")
                    reader_thread.join(timeout=0.5)
                    return -1

                try:
                    item = q.get(timeout=0.1)
                except queue.Empty:
                    # No output yet — check if process already exited and reader finished
                    if proc.poll() is not None and not reader_thread.is_alive() and q.empty():
                        break
                    continue

                if item is None:
                    # EOF reached; wait briefly for process exit code to be set
                    for _ in range(20):
                        if proc.poll() is not None:
                            break
                        time.sleep(0.05)
                        if ctx.cancelled():
                            _kill_proc(proc)
                            ctx.log("  ! command cancelled")
                            return -1
                    break
                line = item.strip()
                if line:
                    ctx.log("  > " + line)
                    if collect is not None:
                        collect.append(line)

            # Ensure reader thread terminated
            reader_thread.join(timeout=2.0)
            # H2 fix: this used to be proc.wait(timeout=None) — an
            # unbounded wait with no cancel check. In the decoding-crash
            # scenario this fix's errors="replace" now prevents, EOF could
            # arrive without the process actually being done, and this call
            # would then hang forever with no way to cancel. Poll in short
            # bounded slices instead, still honoring ctx.cancelled(), and
            # give up waiting (returning -1) rather than blocking forever
            # if the process somehow never reaps.
            wait_start = time.time()
            while proc.poll() is None:
                if ctx.cancelled():
                    _kill_proc(proc)
                    ctx.log("  ! command cancelled")
                    return -1
                if time.time() - wait_start > 30:
                    ctx.log("  ! process did not exit after command completed — giving up wait")
                    # audit fix: a give-up WITHOUT a kill orphans a running
                    # process the cancel registry no longer tracks (the
                    # finally below clears it) — Stop could never reach it.
                    _kill_proc(proc)
                    return -1
                time.sleep(0.1)
            return proc.returncode if proc.returncode is not None else -1
        except Exception as exc:
            _kill_proc(proc)
            ctx.log(f"  ! ERROR running command: {exc}")
            return -1
    except Exception as exc:
        ctx.log(f"  ! ERROR running command: {exc}")
        return -1
    finally:
        _set_current_proc(None)


def run_cmd_checked(ctx: TaskContext, command: str, shell: bool = True, timeout: Optional[int] = None,
                    success_codes=(0,)):
    """Run a command and raise only if its return code is NOT in success_codes.

    Windows tools use non-zero codes that still mean success:
      * sfc /scannow returns 1 when it found AND repaired corruption.
      * DISM returns 3010 (ERROR_SUCCESS_REBOOT_REQUIRED) after a successful
        operation that needs a reboot to finish.
    Passing the appropriate success_codes keeps those from being reported as
    failures.
    """
    rc = run_cmd(ctx, command, shell=shell, timeout=timeout)
    if rc not in success_codes:
        raise RuntimeError(f"Command failed (code {rc}): {command}")
    if rc != 0 and rc in success_codes:
        ctx.log(f"  (completed successfully; exit code {rc} — a reboot may be required)")
    return rc


# --------------------------------------------------------------------------- #
# Filesystem helpers
# --------------------------------------------------------------------------- #

def format_bytes(size_bytes: float) -> str:
    if size_bytes <= 0:
        return "0 Bytes"
    # audit fix: every boundary must compare the ROUNDED-UP byte count to
    # the threshold — the old float compare rendered 1023.6 B as
    # "1024 Bytes", 1048575.9 B as "1024.00 KB" (pseudo-units: a value
    # displayed as exactly 1024 of a unit is always really 1 of the next).
    import math
    n = math.ceil(size_bytes)
    kib, mib, gib = 1024, 1024 ** 2, 1024 ** 3
    if n >= gib:
        return f"{n / gib:.2f} GB"
    if n >= mib:
        return f"{n / mib:.2f} MB"
    if n >= kib:
        return f"{n / kib:.2f} KB"
    return f"{n} Bytes"


def clean_folder_contents(ctx: TaskContext, folder_path: str, remove_root: bool = False,
                           extensions: Optional[List[str]] = None) -> int:
    bytes_freed = 0
    skipped = 0
    if not folder_path or not os.path.exists(folder_path):
        return 0

    # Security guard (audit HIGH finding): os.walk(topdown=False) cannot
    # prune and does not treat Windows junctions as links — a junction
    # anywhere under folder_path redirected deletions into its target
    # (empirically verified). Walk top-down and skip reparse points.
    for root, dirs, files in os.walk(folder_path, topdown=True, followlinks=False):
        # never descend into junctions / symlinked dirs
        dirs[:] = [d for d in dirs if not _is_reparse_point(os.path.join(root, d))]
        for f in files:
            if extensions and not any(f.lower().endswith(e.lower()) for e in extensions):
                continue
            filepath = os.path.join(root, f)
            if _is_reparse_point(filepath):
                skipped += 1
                continue
            try:
                # Get size and remove atomically (avoid TOCTOU race)
                st = os.stat(filepath)
                size = st.st_size
                os.remove(filepath)
                bytes_freed += size
            except (PermissionError, OSError, FileNotFoundError):
                skipped += 1
                continue

        if not extensions:
            for d in dirs:
                dirpath = os.path.join(root, d)
                try:
                    if os.path.exists(dirpath) and not os.listdir(dirpath):
                        os.rmdir(dirpath)
                except OSError:
                    continue

    if remove_root and not extensions and not _is_reparse_point(folder_path):
        try:
            if os.path.exists(folder_path) and not os.listdir(folder_path):
                os.rmdir(folder_path)
        except OSError:
            pass

    if skipped:
        ctx.log(f"  (skipped {skipped} locked files in {folder_path})")
    return bytes_freed


# --------------------------------------------------------------------------- #
# Bundled asset resolution (works both from source and frozen PyInstaller exe)
# --------------------------------------------------------------------------- #

def resolve_asset_path(filename: str) -> Optional[str]:
    """Find a file bundled under app/assets, whether running from source or
    from a PyInstaller-frozen exe (sys._MEIPASS). Returns None if not found."""
    import pathlib
    candidates = []
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        candidates.append(pathlib.Path(meipass) / "app" / "assets" / filename)
        candidates.append(pathlib.Path(meipass) / "assets" / filename)
    candidates.append(pathlib.Path(__file__).with_name("assets") / filename)
    candidates.append(pathlib.Path(__file__).resolve().parents[1] / "app" / "assets" / filename)
    for p in candidates:
        try:
            if p.is_file():
                return str(p)
        except Exception:
            continue
    return None


# --------------------------------------------------------------------------- #
# Registry helpers
# --------------------------------------------------------------------------- #

_HIVES = {
    "HKCU": winreg.HKEY_CURRENT_USER if IS_WINDOWS else None,
    "HKLM": winreg.HKEY_LOCAL_MACHINE if IS_WINDOWS else None,
    "HKCR": winreg.HKEY_CLASSES_ROOT if IS_WINDOWS else None,
}

_TYPE_MAP = {
    "REG_SZ": winreg.REG_SZ if IS_WINDOWS else None,
    "REG_DWORD": winreg.REG_DWORD if IS_WINDOWS else None,
    "REG_BINARY": winreg.REG_BINARY if IS_WINDOWS else None,
    "REG_EXPAND_SZ": winreg.REG_EXPAND_SZ if IS_WINDOWS else None,
}


def _reg_access(write: bool = True) -> int:
    """Registry access mask with 64-bit view fallback (avoids Wow6432Node redirect)."""
    if not IS_WINDOWS:
        return 0
    base = winreg.KEY_WRITE if write else winreg.KEY_READ
    # KEY_WOW64_64KEY ensures we touch the real 64-bit hive on 64-bit Windows
    try:
        return base | winreg.KEY_WOW64_64KEY  # type: ignore[attr-defined]
    except AttributeError:
        return base


def reg_set_value(ctx: TaskContext, hive: str, path: str, name: str, value, value_type: str = "REG_DWORD") -> bool:
    ctx.log(f"reg add {hive}\\{path} /v {name or '(Default)'} /d {value}")
    key = None
    try:
        root = _HIVES[hive]
        key = winreg.CreateKeyEx(root, path, 0, _reg_access(write=True))
        winreg.SetValueEx(key, name if name else None, 0, _TYPE_MAP[value_type], value)
        return True
    except Exception as exc:
        ctx.log(f"  ! registry write failed: {exc}")
        return False
    finally:
        if key is not None:
            try:
                winreg.CloseKey(key)
            except Exception:
                pass


def reg_set_value_checked(ctx: TaskContext, hive: str, path: str, name: str, value,
                          value_type: str = "REG_DWORD") -> None:
    """reg_set_value that RAISES on failure (F-005).

    The run/revert engine counts any task that does not raise as
    'succeeded' and marks the tweak applied (the '✓ Active' badge).
    Task functions that ignored reg_set_value's False return therefore
    reported success — with a badge and an undo entry — for tweaks that
    never actually wrote (typical case: HKLM policy values in limited
    mode). This wrapper converts a failed write into an honest task
    failure, so the runner reports it and never marks it applied.
    Reverts in this codebase write the complete target state (they are
    not incremental undo), so a partially-applied task self-heals when
    the user re-runs it or runs Undo."""
    if not reg_set_value(ctx, hive, path, name, value, value_type=value_type):
        raise RuntimeError(
            f"Could not write {hive}\\{path}"
            + (f"\\{name}" if name else "")
            + " — the change did NOT apply (Administrator rights may be "
              "required, or policy is blocking the key). Nothing was marked "
              "as applied.")


def reg_delete_value(ctx: TaskContext, hive: str, path: str, name: str) -> bool:
    ctx.log(f"reg delete {hive}\\{path} /v {name or '(Default)'}")
    key = None
    try:
        root = _HIVES[hive]
        # Use 64-bit view access if available
        access = _reg_access(write=True)
        # Ensure SET_VALUE is included (KEY_WRITE includes it, but be explicit)
        try:
            access = access | winreg.KEY_SET_VALUE
        except Exception:
            pass
        key = winreg.OpenKey(root, path, 0, access)
        winreg.DeleteValue(key, name)
        return True
    except FileNotFoundError:
        return True
    except Exception as exc:
        ctx.log(f"  ! registry delete failed: {exc}")
        return False
    finally:
        if key is not None:
            try:
                winreg.CloseKey(key)
            except Exception:
                pass


def reg_get_value(ctx: TaskContext, hive: str, path: str, name: str):
    """Read a registry value. Returns the value, or None if the key/value
    doesn't exist or can't be read. Used to snapshot a setting's real prior
    value before a tweak overwrites it, so revert can restore the exact
    value instead of a hardcoded guess."""
    key = None
    try:
        root = _HIVES[hive]
        key = winreg.OpenKey(root, path, 0, _reg_access(write=False))
        value, _ = winreg.QueryValueEx(key, name if name else None)
        return value
    except FileNotFoundError:
        return None
    except Exception:
        return None
    finally:
        if key is not None:
            try:
                winreg.CloseKey(key)
            except Exception:
                pass


# --------------------------------------------------------------------------- #
# Locale-independent system-state reads (B6 audit fix)
# --------------------------------------------------------------------------- #
# powercfg / sc / fsutil print LOCALIZED labels on non-English Windows. The
# old parsers below matched English text only ("Current AC Power Setting
# Index:", "START_TYPE"), so on localized Windows snapshots silently
# recorded nothing and reverts restored hardcoded fallbacks instead of the
# real prior values. The reads are now keyed on locale-free tokens — the
# settings' documented GUIDs and registry values — never display labels.
#
# powercfg ALIAS tokens (sub_processor, PROCTHROTTLEMAX) are English
# resources too; every read is built from the documented GUIDs instead:
#   * processor subgroup:        54533251-82be-4824-96c1-47b60b740d00
#   * PROCTHROTTLEMAX / MIN:     bc5038f7-23e0-4960-96da-33abaf5935ec /
#                                893dee8e-2bef-41e0-89c6-b55d0929964c
#     (both confirmed against this host's live `powercfg /q` output)
#   * PERFBOOSTMODE / CPMINCORES / CPMAXCORES: the other aliases the tweak
#     snapshot path queries (documented processor-setting GUIDs).
_POWERCFG_ALIAS_GUIDS = {
    "sub_processor": "54533251-82be-4824-96c1-47b60b740d00",
    "PROCTHROTTLEMAX": "bc5038f7-23e0-4960-96da-33abaf5935ec",
    "PROCTHROTTLEMIN": "893dee8e-2bef-41e0-89c6-b55d0929964c",
    "PERFBOOSTMODE": "be337238-0d82-4146-a960-4f3749d470c7",
    "CPMINCORES": "0cc5b647-c1df-4637-881a-dec4282d1fb9",
    "CPMAXCORES": "ea062031-0e34-4ff1-9b6d-eb1059334028",
}


def powercfg_query_indexes(ctx: TaskContext, subgroup: str, setting: str) -> "tuple[Optional[int], Optional[int]]":
    """(current-AC, current-DC) hex indexes for ONE power setting, read in
    a single powercfg query. Returns (None, None) — after logging a
    warning — when the setting can't be read or parsed; never a silent
    empty (a silent None used to leave reverts restoring a hardcoded
    guess).

    Locale-independent parse (B6): within a single-setting query the only
    lines ending in a hex value after ':' are the (optional) min/max/
    possible-setting lines followed by 'Current AC' then 'Current DC' —
    the LAST TWO such lines are always the AC then DC index. No label
    parsing at all (not even the word 'Index', which powercfg may
    localize). The old English-label regexes are kept as a fallback for
    exotic output shapes."""
    import re as _re
    if not IS_WINDOWS:
        return None, None
    sub = _POWERCFG_ALIAS_GUIDS.get(subgroup, subgroup)
    set_guid = _POWERCFG_ALIAS_GUIDS.get(setting, setting)
    try:
        result = subprocess.run(
            f"powercfg /q scheme_current {sub} {set_guid}",
            shell=True, capture_output=True, text=True, timeout=15,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        out = result.stdout or ""
    except Exception as exc:
        ctx.log(f"  ! could not query power setting {subgroup}:{setting}: {exc}")
        return None, None
    indexes = []
    for line in out.splitlines():
        m = _re.search(r":\s*0x([0-9A-Fa-f]+)[ \t]*$", line)
        if m:
            indexes.append(m.group(1))
    ac = dc = None
    if len(indexes) >= 2:
        ac, dc = int(indexes[-2], 16), int(indexes[-1], 16)
    else:
        # English-label fallback (the pre-B6 parser) for unusual shapes
        m = _re.search(r"Current AC Power Setting Index:\s*0x([0-9A-Fa-f]+)", out)
        if m:
            ac = int(m.group(1), 16)
        m = _re.search(r"Current DC Power Setting Index:\s*0x([0-9A-Fa-f]+)", out)
        if m:
            dc = int(m.group(1), 16)
    if ac is None or dc is None:
        # powercfg exits 0 even when the setting is not present in the
        # scheme (e.g. hidden by the 24H2 power-mode overlay) — make that
        # visible instead of silently dropping the snapshot.
        ctx.log(f"  ! could not parse the current {setting} power setting from "
                f"powercfg (rc={result.returncode}, output {out.strip()[:80]!r}) — "
                "no snapshot for this setting; its revert will fall back to a "
                "documented default.")
    return ac, dc


def powercfg_query_index(ctx: TaskContext, subgroup: str, setting: str) -> Optional[int]:
    """Read the current AC power-setting index for a powercfg alias
    (e.g. subgroup='sub_processor', setting='PROCTHROTTLEMAX'). Returns
    None if the setting can't be read (missing on this Windows edition,
    scheme not active yet, etc.) — callers should fall back to a documented
    default in that case rather than skip the revert entirely. B6: the
    query is locale-independent (GUID-based); a warning is logged when the
    value cannot be read."""
    ac, _dc = powercfg_query_indexes(ctx, subgroup, setting)
    return ac


_SC_START_TYPE_MAP = {
    "0": "boot",
    "1": "system",
    "2": "auto",
    "3": "demand",
    "4": "disabled",
}


def sc_query_start_type(ctx: TaskContext, service: str) -> Optional[str]:
    """Read a Windows service's current start type, returning one of
    boot/system/auto/demand/disabled (the same keywords `sc config
    start=` accepts), or None if it can't be read (service missing, etc.).

    B6 fix: this used to parse `sc qc` output for the English 'START_TYPE'
    label — on localized Windows the label is translated and the read
    silently returned None, so the NVIDIA-telemetry snapshot recorded the
    wrong 'demand' and its revert restored that same wrong value. The
    service's Start REG_DWORD under
    HKLM\\SYSTEM\\CurrentControlSet\\Services\\<name> is locale-free."""
    if not IS_WINDOWS:
        return None
    try:
        key = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                             f"SYSTEM\\CurrentControlSet\\Services\\{service}",
                             0, _reg_access(write=False))
        try:
            value, _ = winreg.QueryValueEx(key, "Start")
        finally:
            winreg.CloseKey(key)
    except FileNotFoundError:
        return None
    except Exception as exc:
        ctx.log(f"  ! could not read the start type of service {service} "
                f"from the registry: {exc}")
        return None
    return _SC_START_TYPE_MAP.get(str(value))


def reg_delete_key(ctx: TaskContext, hive: str, path: str) -> bool:
    ctx.log(f"reg delete {hive}\\{path} /f")
    try:
        root = _HIVES[hive]
        _reg_delete_tree(root, path)
        return True
    except FileNotFoundError:
        return True
    except Exception as exc:
        ctx.log(f"  ! registry key delete failed: {exc}")
        return False


def _reg_delete_tree(root, path: str):
    key = None
    try:
        access = winreg.KEY_ALL_ACCESS
        try:
            access = access | winreg.KEY_WOW64_64KEY  # type: ignore[attr-defined]
        except AttributeError:
            pass
        key = winreg.OpenKey(root, path, 0, access)
    except FileNotFoundError:
        return
    try:
        while True:
            try:
                subkey_name = winreg.EnumKey(key, 0)
            except OSError:
                break
            _reg_delete_tree(root, f"{path}\\{subkey_name}")
    finally:
        if key is not None:
            try:
                winreg.CloseKey(key)
            except Exception:
                pass
    winreg.DeleteKey(root, path)


# --------------------------------------------------------------------------- #
# System restore point
# --------------------------------------------------------------------------- #

# --------------------------------------------------------------------------- #
# Explorer restart — shared helper (moved here from tweak_tasks.py so
# clean_tasks.py can reuse it too; see C3 fix below)
# --------------------------------------------------------------------------- #

def _is_explorer_running() -> bool:
    """True if at least one explorer.exe process exists right now."""
    try:
        out = subprocess.check_output(
            "tasklist /fi \"imagename eq explorer.exe\" /fo csv /nh",
            shell=True, text=True, timeout=5, stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        return "explorer.exe" in out.lower()
    except Exception:
        return False


def restart_explorer(ctx: TaskContext) -> bool:
    """Restart Explorer and VERIFY the new shell actually survives.

    User-reported bug: 'sometimes the taskbar never comes back'. The old
    flow had a race — it killed explorer (graceful, then /f), spawned a new
    explorer.exe, then verified at +1s. But when explorer is killed, the
    OLD process can take 1-3s to actually exit. If the new explorer.exe
    starts while the old one is still mid-shutdown, Windows' shell
    registration tells the new one 'a shell already exists', the new
    process exits immediately, the old one finishes dying — and the user
    is left with NO taskbar/desktop. The +1s tasklist check sometimes
    caught the new process before it bailed out, so the log said success.

    Fix, in order:
      1. kill (graceful, then force)
      2. WAIT until no explorer.exe exists at all (the old one is fully
         gone — this is the step that eliminates the race), up to 10s
      3. spawn the new explorer detached
      4. verify the new process is STILL alive after 3s (long enough for a
         doomed shell-handoff exit to have happened), retrying the spawn
         up to 3 times if it died
      5. log honestly at each step; never claim success on a hunch
    Returns True only if explorer survived; False if every attempt died
    (caller logs a visible warning — no silent taskbar loss)."""
    ctx.log("Restarting Explorer...")
    # 0. Cancel fast-path: restarting Explorer takes up to ~40s; never start
    # it if the user already pressed Stop (audit: no cancelled checks here).
    if ctx.cancelled():
        ctx.log("  ! cancelled before Explorer restart.")
        return False
    # 1. Kill
    run_cmd(ctx, "taskkill /im explorer.exe", timeout=10)
    time.sleep(0.5)
    run_cmd(ctx, "taskkill /f /im explorer.exe", timeout=5)
    # 2. Wait for the old process to be fully gone (THE race fix)
    for _ in range(20):  # up to 10s
        if not _is_explorer_running():
            break
        time.sleep(0.5)
    else:
        ctx.log("  ! Old Explorer would not exit — continuing anyway.")
    # 3-4. Spawn + survival-verify, with retries
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        creationflags |= subprocess.DETACHED_PROCESS  # type: ignore[attr-defined]
    except AttributeError:
        pass
    for attempt in (1, 2, 3):
        if ctx.cancelled():
            ctx.log("  ! cancelled — leaving Explorer restart to the user "
                    "(Ctrl+Shift+Esc > Run new task > explorer.exe).")
            return False
        try:
            subprocess.Popen(["explorer.exe"], creationflags=creationflags, close_fds=True)
        except Exception as e:
            ctx.log(f"  ! Could not launch Explorer (attempt {attempt}): {e}")
            continue
        # verify survival: a doomed handoff-exit happens within ~2-3s
        for _ in range(6):
            if ctx.cancelled():
                ctx.log("  ! cancelled during verification — Explorer may need a manual start.")
                return False
            time.sleep(0.5)
            if _is_explorer_running():
                time.sleep(2.0)  # let a would-be bail-out actually bail
                if _is_explorer_running():
                    ctx.log("Explorer restarted successfully (taskbar is back).")
                    return True
                break  # it died again — retry spawn
        # not running after 3s — fall through to retry
    ctx.log("  ! Explorer did not stay running after restart. Your taskbar may be missing —")
    ctx.log("  ! press Ctrl+Shift+Esc, click File > Run new task, and type explorer.exe.")
    return False


def create_restore_point(ctx: TaskContext, description: str = "GamerOpt Cleaner - before changes") -> bool:
    ctx.set_status("Creating a System Restore point (safety net)...")
    # audit hardening: description flows into a shell=True string — pass it
    # as an argv list (shell=False) so no escaping/quoting games are needed.
    ps_script = (
        f"Checkpoint-Computer -Description '{description.replace(chr(39), chr(39)*2)}' "
        "-RestorePointType MODIFY_SETTINGS"
    )
    rc = run_cmd(ctx, ["powershell", "-NoProfile", "-Command", ps_script],
                 shell=False, timeout=120)
    if rc == 0:
        ctx.log("Restore point created successfully.")
        return True
    ctx.log("  ! Could not create a restore point (System Protection may be off for this drive).")
    ctx.log("  ! Enable it: Settings > System > About > System protection, then retry.")
    return False
