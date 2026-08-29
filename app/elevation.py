"""
Administrator elevation helpers.

Design goal (per spec): the app must NOT silently relaunch itself as admin the
instant it starts (that is what the original decompiled app did). Instead, the
GUI shows the user a clear screen explaining *why* admin rights are needed and
lets them click a button to relaunch elevated (UAC prompt) -- or continue in a
read-only / limited mode if they decline.
"""

import ctypes
import os
import subprocess
import sys
import tempfile
import time


def is_admin() -> bool:
    """Return True if the current process is running with administrator rights."""
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def _get_elevation_cookie_path() -> str:
    """Get path to a temporary file used for elevation coordination."""
    return os.path.join(tempfile.gettempdir(), "CleanerTool_elevation_cookie")


def _write_elevation_cookie(pid: int) -> None:
    """Write the elevated process PID to the cookie file."""
    try:
        with open(_get_elevation_cookie_path(), "w") as f:
            f.write(str(pid))
    except Exception:
        pass


def _read_elevation_cookie() -> int | None:
    """Read the elevated process PID from the cookie file."""
    try:
        with open(_get_elevation_cookie_path(), "r") as f:
            return int(f.read().strip())
    except Exception:
        return None


def _clear_elevation_cookie() -> None:
    """Remove the elevation cookie file."""
    try:
        os.remove(_get_elevation_cookie_path())
    except Exception:
        pass


def _wait_for_elevated_process(timeout: float = 10.0) -> bool:
    """
    Wait for the elevated process to signal it has started.
    Returns True if the elevated process started successfully.
    """
    start_time = time.time()
    while time.time() - start_time < timeout:
        pid = _read_elevation_cookie()
        if pid is not None:
            # Verify the process is still alive
            try:
                import ctypes.wintypes
                PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
                handle = ctypes.windll.kernel32.OpenProcess(
                    PROCESS_QUERY_LIMITED_INFORMATION, False, pid
                )
                if handle:
                    exit_code = ctypes.wintypes.DWORD()
                    if ctypes.windll.kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
                        ctypes.windll.kernel32.CloseHandle(handle)
                        if exit_code.value == 259:  # STILL_ACTIVE
                            _clear_elevation_cookie()
                            return True
                    ctypes.windll.kernel32.CloseHandle(handle)
            except Exception:
                pass
        time.sleep(0.2)
    return False


def relaunch_as_admin() -> bool:
    """
    Re-launch the current program (frozen .exe or `python main.py`) with a UAC
    elevation prompt. Returns True if the relaunch was *requested* successfully
    (Windows will show the UAC dialog next -- the user can still cancel there).
    
    Note: The caller should call wait_for_elevated_process() after this returns True
    to ensure the elevated process actually started before exiting.
    """
    try:
        _clear_elevation_cookie()  # Clear any stale cookie
        
        if getattr(sys, "frozen", False):
            # Running as a PyInstaller-built .exe
            executable = sys.executable
            argv = list(sys.argv[1:])
        else:
            # Running as a normal python script: relaunch python.exe with the
            # script path as the first argument.
            executable = sys.executable
            script = os.path.abspath(sys.argv[0])
            argv = [script] + list(sys.argv[1:])

        # subprocess.list2cmdline applies correct Windows argument quoting and
        # escapes any embedded quotes, preventing argument injection.
        params = subprocess.list2cmdline(argv)

        rc = ctypes.windll.shell32.ShellExecuteW(
            None, "runas", executable, params, None, 1
        )
        # ShellExecuteW returns a value > 32 on success
        return int(rc) > 32
    except Exception:
        return False


def wait_for_elevated_process(timeout: float = 10.0) -> bool:
    """
    Wait for the elevated process to signal it has started.
    Should be called after relaunch_as_admin() returns True.
    Returns True if the elevated process started successfully.
    """
    return _wait_for_elevated_process(timeout)


def signal_elevated_startup() -> None:
    """
    Called by the elevated process on startup to signal the original process
    that it has started successfully.
    """
    _write_elevation_cookie(os.getpid())
