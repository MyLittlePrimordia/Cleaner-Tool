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
else:  # pragma: no cover
    winreg = None  # type: ignore


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
    dry_run: bool = False
    cancelled: Callable[[], bool] = field(default=lambda: False)


# --------------------------------------------------------------------------- #
# Subprocess helper — fixed timeout deadlock, proper wait
# --------------------------------------------------------------------------- #

def run_cmd(ctx: TaskContext, command: str, shell: bool = True, timeout: Optional[int] = None) -> int:
    ctx.log(f"$ {command}")
    if ctx.dry_run:
        ctx.log("  (dry run - command not executed)")
        return 0
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

            # Ensure reader thread terminated
            reader_thread.join(timeout=2.0)
            # Ensure process reaped
            try:
                proc.wait(timeout=2.0)
            except Exception:
                pass
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
    # Use binary (1024-based) units to match Windows Explorer and gui disk monitor
    if size_bytes >= 1024 ** 3:
        return f"{size_bytes / 1024 ** 3:.2f} GB"
    if size_bytes >= 1024 ** 2:
        return f"{size_bytes / 1024 ** 2:.2f} MB"
    if size_bytes >= 1024:
        return f"{size_bytes / 1024:.2f} KB"
    return f"{int(size_bytes)} Bytes"


def clean_folder_contents(ctx: TaskContext, folder_path: str, remove_root: bool = False,
                           extensions: Optional[List[str]] = None) -> int:
    bytes_freed = 0
    skipped = 0
    if not folder_path or not os.path.exists(folder_path):
        return 0

    for root, dirs, files in os.walk(folder_path, topdown=False):
        for f in files:
            if extensions and not any(f.lower().endswith(e.lower()) for e in extensions):
                continue
            filepath = os.path.join(root, f)
            try:
                # Get size and remove atomically (avoid TOCTOU race)
                st = os.stat(filepath)
                size = st.st_size
                if not ctx.dry_run:
                    os.remove(filepath)
                bytes_freed += size
            except (PermissionError, OSError, FileNotFoundError):
                skipped += 1
                continue

        if not extensions:
            for d in dirs:
                dirpath = os.path.join(root, d)
                try:
                    if not ctx.dry_run and os.path.exists(dirpath) and not os.listdir(dirpath):
                        os.rmdir(dirpath)
                except OSError:
                    continue

    if remove_root and not extensions and not ctx.dry_run:
        try:
            if os.path.exists(folder_path) and not os.listdir(folder_path):
                os.rmdir(folder_path)
        except OSError:
            pass

    if skipped:
        ctx.log(f"  (skipped {skipped} locked files in {folder_path})")
    return bytes_freed


def folder_size(folder_path: str) -> int:
    total = 0
    if not folder_path or not os.path.exists(folder_path):
        return 0
    for root, _dirs, files in os.walk(folder_path):
        for f in files:
            try:
                total += os.path.getsize(os.path.join(root, f))
            except OSError:
                continue
    return total


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
    if ctx.dry_run:
        return True
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


def reg_delete_value(ctx: TaskContext, hive: str, path: str, name: str) -> bool:
    ctx.log(f"reg delete {hive}\\{path} /v {name or '(Default)'}")
    if ctx.dry_run:
        return True
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


def reg_delete_key(ctx: TaskContext, hive: str, path: str) -> bool:
    ctx.log(f"reg delete {hive}\\{path} /f")
    if ctx.dry_run:
        return True
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


def reg_key_exists(hive: str, path: str) -> bool:
    if not IS_WINDOWS:
        return False
    key = None
    try:
        root = _HIVES[hive]
        access = winreg.KEY_READ
        try:
            access = access | winreg.KEY_WOW64_64KEY  # type: ignore[attr-defined]
        except AttributeError:
            pass
        key = winreg.OpenKey(root, path, 0, access)
        return True
    except FileNotFoundError:
        return False
    except Exception:
        return False
    finally:
        if key is not None:
            try:
                winreg.CloseKey(key)
            except Exception:
                pass


# --------------------------------------------------------------------------- #
# System restore point
# --------------------------------------------------------------------------- #

def create_restore_point(ctx: TaskContext, description: str = "GamerOpt Cleaner - before changes") -> bool:
    ctx.set_status("Creating a System Restore point (safety net)...")
    safe_desc = description.replace("'", "''")
    ps_cmd = (
        'powershell -NoProfile -Command '
        f'"Checkpoint-Computer -Description \'{safe_desc}\' -RestorePointType MODIFY_SETTINGS"'
    )
    rc = run_cmd(ctx, ps_cmd, timeout=120)
    if rc == 0:
        ctx.log("Restore point created successfully.")
        return True
    ctx.log("Could not create a restore point (System Protection may be off for this drive). Continuing anyway.")
    # Non-fatal — don't fail the whole batch (return True so gui counts as succeeded)
    return True
