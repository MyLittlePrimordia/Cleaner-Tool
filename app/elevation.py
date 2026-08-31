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


def _get_elevation_token_path() -> str:
    """Path storing the expected random token for the pending elevation."""
    return os.path.join(tempfile.gettempdir(), "CleanerTool_elevation_token")


def _write_elevation_cookie(pid: int, token: str = "") -> None:
    """Write the elevated process PID and token to the cookie file."""
    try:
        with open(_get_elevation_cookie_path(), "w") as f:
            f.write(f"{pid}:{token}" if token else str(pid))
    except Exception:
        pass


def _read_elevation_cookie() -> tuple[int | None, str]:
    """Read the elevated process PID and token from the cookie file."""
    try:
        with open(_get_elevation_cookie_path(), "r") as f:
            raw = f.read().strip()
            if ":" in raw:
                pid_s, token = raw.split(":", 1)
                return int(pid_s.strip()), token.strip()
            return int(raw), ""
    except Exception:
        return None, ""


def _clear_elevation_cookie() -> None:
    """Remove the elevation cookie file."""
    try:
        os.remove(_get_elevation_cookie_path())
    except Exception:
        pass


def _write_pending_token(token: str) -> None:
    try:
        with open(_get_elevation_token_path(), "w") as f:
            f.write(token)
    except Exception:
        pass


def _read_pending_token() -> str | None:
    try:
        with open(_get_elevation_token_path(), "r") as f:
            return f.read().strip()
    except Exception:
        return None


def _clear_pending_token() -> None:
    try:
        os.remove(_get_elevation_token_path())
    except Exception:
        pass


def _wait_for_elevated_process(timeout: float = 10.0) -> bool:
    """
    Wait for the elevated process to signal it has started.
    Returns True if the elevated process started successfully.
    Verifies token to prevent spoofing via pre-created cookie.
    """
    expected_token = _read_pending_token()
    start_time = time.time()
    while time.time() - start_time < timeout:
        pid, token = _read_elevation_cookie()
        if pid is not None:
            # If a token was issued, the cookie must match it
            if expected_token and token != expected_token:
                time.sleep(0.2)
                continue
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
                            _clear_pending_token()
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
        import secrets
        _clear_elevation_cookie()  # Clear any stale cookie
        _clear_pending_token()
        token = secrets.token_hex(16)
        _write_pending_token(token)

        if getattr(sys, "frozen", False):
            # Running as a PyInstaller-built .exe
            executable = sys.executable
            argv = list(sys.argv[1:]) + [f"--elevation-token={token}"]
        else:
            # Running as a normal python script: relaunch python.exe with the
            # script path as the first argument.
            executable = sys.executable
            script = os.path.abspath(sys.argv[0])
            argv = [script] + list(sys.argv[1:]) + [f"--elevation-token={token}"]

        # subprocess.list2cmdline applies correct Windows argument quoting and
        # escapes any embedded quotes, preventing argument injection.
        params = subprocess.list2cmdline(argv)

        rc = ctypes.windll.shell32.ShellExecuteW(
            None, "runas", executable, params, None, 1
        )
        # ShellExecuteW returns a value > 32 on success
        if int(rc) <= 32:
            _clear_pending_token()
            return False
        return True
    except Exception:
        try:
            _clear_pending_token()
        except Exception:
            pass
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
    that it has started successfully. If --elevation-token was passed, it is
    echoed back in the cookie for verification.
    """
    token = ""
    # Extract token from argv if present
    for arg in sys.argv:
        if arg.startswith("--elevation-token="):
            token = arg.split("=", 1)[1]
            break
    # Only signal if we're actually admin and have a token (or legacy pid-only)
    # Don't write cookie on non-Windows test runs without token to avoid litter
    if not token:
        # No token — legacy / direct launch without elevation. Only write if
        # a pending token file exists (means we were launched via relaunch)
        pending = _read_pending_token()
        if pending is None:
            return
        token = pending
    _write_elevation_cookie(os.getpid(), token)
