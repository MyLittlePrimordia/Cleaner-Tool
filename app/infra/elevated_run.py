"""Run a short PowerShell snippet with administrator rights and capture stdout.

Shared, because two Tools popups needed it and each had grown its own copy:
app/restorepts.py (listing and creating restore points, both admin-gated) and
app/svcs.py (starting, stopping and reconfiguring services). A second copy
would be a second set of bugs.

Why ShellExecuteW with the "runas" verb
---------------------------------------
The app already knows how to relaunch ITSELF elevated
(app/elevated_launch.py, via a scheduled task), but that is the wrong tool
when one operation needs permission. Relaunching the whole app to change one
service start type is heavy-handed and changes what the user is looking at.

`ShellExecuteW(NULL, "runas", "powershell.exe", ...)` raises a UAC prompt for
that one process and nothing else. Measured working for restore-point
operations on this machine.

The stdout problem, and why this is not a one-liner
--------------------------------------------------
A process launched through ShellExecuteW has no console attached, so its
stdout does not come back to us. The snippet therefore redirects its own
output to a temp file, which the parent polls until it is non-empty or the
timeout expires. Three consequences worth knowing:

* RETURNING IS AMBIGUOUS WHEN THE SCRIPT PRINTS NOTHING. A script that fails
  silently and one that succeeds quietly look identical, so callers pass a
  sentinel to print on success (see the svcs module) rather than guessing.
* The UAC prompt is a real outcome. rc <= 32 means the user declined or the
  shell refused, and that is reported as "cancelled", never as a failure.
* The script and its stdout file are created with tempfile.mkstemp (O_EXCL,
  random name), so two concurrent operations cannot collide and a same-user
  process cannot pre-plant or swap the file before it is run elevated.
"""
from __future__ import annotations

import ctypes
import os
import sys
import tempfile
import time

IS_WINDOWS = sys.platform.startswith("win")

#: ShellExecuteW returns >32 on success. 5 is ERROR_CANCELLED, i.e. the user
#: said no to the UAC prompt - a normal outcome, not an error.
SE_ERR_CANCELLED = 5
_MIN_OK = 32


def is_elevated() -> bool:
    """True when this process already holds administrator rights."""
    if not IS_WINDOWS:
        return True
    try:
        from app.elevation import _process_is_elevated
        return bool(_process_is_elevated(os.getpid()))
    except Exception:
        pass
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def run_elevated(script: str, timeout: int = 120):
    """Run `script` elevated. Returns (ok, stdout_text).

    `ok` is False when the user declined the prompt, the shell refused, or
    the script never produced output within `timeout`.
    """
    if not IS_WINDOWS:
        return False, ""
    script_path = None
    out_path = None
    try:
        # B-003: a predictable pid+ms name under %TEMP%, opened with a plain
        # "w", is a symlink-swap / pre-plant race: any same-user process can
        # plant a symlink or replace the script between our write and the
        # ELEVATED -File execution, running arbitrary code at high integrity.
        # mkstemp gives an O_CREAT|O_EXCL random name no one can pre-plant.
        fd, script_path = tempfile.mkstemp(prefix="_cleaner_elev_", suffix=".ps1")
        os.close(fd)
        fd, out_path = tempfile.mkstemp(prefix="_cleaner_elev_", suffix=".txt")
        os.close(fd)
        # Remove the pre-created empty stdout file so its existence again
        # means "PowerShell started writing", not "mkstemp ran".
        try:
            os.remove(out_path)
        except OSError:
            pass
        # utf-8-sig: Windows PowerShell 5.1 reads a BOM-less .ps1 as ANSI, so
        # non-ASCII values interpolated by the caller (service names, paths)
        # become mojibake and the operation silently targets the wrong name.
        with open(script_path, "w", encoding="utf-8-sig") as fh:
            fh.write("%s *> '%s'" % (script, out_path))
        params = ('-NoProfile -NonInteractive -ExecutionPolicy Bypass '
                  '-File "%s"' % script_path)
        rc = ctypes.windll.shell32.ShellExecuteW(None, "runas",
                                                 "powershell.exe", params,
                                                 None, 0)
        if rc is None or rc <= _MIN_OK:
            return False, ""
        deadline = time.time() + timeout
        while time.time() < deadline:
            if os.path.exists(out_path):
                try:
                    with open(out_path, "r", encoding="utf-8",
                              errors="replace") as fh:
                        text = fh.read()
                    # B-057: require actual output. The file is created by the
                    # "*> file" redirect when the script STARTS, so an empty
                    # file just means it is still running OR died before
                    # printing anything. Accepting empty after ~3 s reported a
                    # failed / no-op script as success; every caller prints a
                    # sentinel (RESTORE_OK, RESTORE_LIST_DONE, CLEANER_OK,
                    # OK|, ...), so a run that never prints is a failure.
                    if text.strip():
                        return True, text
                except OSError:
                    pass
            time.sleep(0.15)
        return False, ""
    except Exception:
        return False, ""
    finally:
        for p in (script_path, out_path):
            try:
                os.remove(p)
            except OSError:
                pass


def run_elevated_exe(exe: str, params: str = ""):
    """Launch `exe` elevated through the same "runas" verb. No stdout.

    The sibling of run_elevated for the case where the thing being launched
    is a GUI and there is nothing to capture. Measured on this machine:
    SystemPropertiesProtection.exe refuses to start at all without
    elevation (ShellExecute ERROR_ELEVATION_REQUIRED, 740), so a plain
    subprocess.Popen from an unelevated app cannot open it at all - which
    is why the Restore Point Manager needs this rather than a Popen.

    Returns (ok, reason) where reason is "cancelled" when the user declined
    the prompt. A declined prompt is a normal outcome, never a failure.
    """
    if not IS_WINDOWS:
        return False, "unsupported"
    try:
        rc = ctypes.windll.shell32.ShellExecuteW(None, "runas", str(exe),
                                                 str(params or ""), None, 1)
    except Exception:
        return False, "failed"
    if rc is None:
        return False, "failed"
    try:
        code = int(rc)
    except (TypeError, ValueError):
        return True, ""
    if code <= _MIN_OK:
        return False, ("cancelled" if code == SE_ERR_CANCELLED else "failed")
    return True, ""


def quote_ps(value: str) -> str:
    """Single-quote a value for PowerShell, doubling any internal quote.

    Every caller interpolates a service name or a path into a script string,
    so this is a security boundary, not a formatting convenience.
    """
    return "'%s'" % str(value or "").replace("'", "''")