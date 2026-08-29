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

# Track the currently running subprocess per thread so it can be killed on user cancel.
_current_proc_local = threading.local()


def _set_current_proc(proc):
    _current_proc_local.proc = proc


def _get_current_proc():
    return getattr(_current_proc_local, "proc", None)


def cancel_current_command():
    """Best-effort kill of the command currently executed by run_cmd()."""
    proc = _get_current_proc()
    if proc is not None and proc.poll() is None:
        try:
            proc.kill()
        except Exception:
            pass


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
            # Stream output line by line to avoid buffering large output in memory
            start_time = time.time()
            while True:
                if ctx.cancelled():
                    proc.kill()
                    ctx.log("  ! command cancelled")
                    return -1
                if timeout and (time.time() - start_time) > timeout:
                    proc.kill()
                    ctx.log("  ! command timed out")
                    return -1
                
                line = proc.stdout.readline()
                if not line and proc.poll() is not None:
                    break
                if line:
                    line = line.strip()
                    if line:
                        ctx.log("  > " + line)
                
                # Small sleep to prevent busy-waiting
                time.sleep(0.01)
            
            return proc.returncode if proc.returncode is not None else -1
        except Exception as exc:
            proc.kill()
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
    # Use decimal (1000-based) units to match Windows Explorer
    if size_bytes >= 1000 ** 3:
        return f"{size_bytes / 1000 ** 3:.2f} GB"
    if size_bytes >= 1000 ** 2:
        return f"{size_bytes / 1000 ** 2:.2f} MB"
    if size_bytes >= 1000:
        return f"{size_bytes / 1000:.2f} KB"
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


def reg_set_value(ctx: TaskContext, hive: str, path: str, name: str, value, value_type: str = "REG_DWORD") -> bool:
    ctx.log(f"reg add {hive}\\{path} /v {name or '(Default)'} /d {value}")
    if ctx.dry_run:
        return True
    key = None
    try:
        root = _HIVES[hive]
        key = winreg.CreateKeyEx(root, path, 0, winreg.KEY_WRITE)
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
        key = winreg.OpenKey(root, path, 0, winreg.KEY_SET_VALUE)
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
        key = winreg.OpenKey(root, path, 0, winreg.KEY_ALL_ACCESS)
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


def reg_get_value(hive: str, path: str, name: str, default=None):
    """Read a single registry value. Returns `default` if the key/value doesn't exist."""
    if not IS_WINDOWS:
        return default
    key = None
    try:
        root = _HIVES[hive]
        key = winreg.OpenKey(root, path)
        value, _type = winreg.QueryValueEx(key, name)
        return value
    except Exception:
        return default
    finally:
        if key is not None:
            try:
                winreg.CloseKey(key)
            except Exception:
                pass


def reg_key_exists(hive: str, path: str) -> bool:
    if not IS_WINDOWS:
        return False
    key = None
    try:
        root = _HIVES[hive]
        key = winreg.OpenKey(root, path)
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
    return False
