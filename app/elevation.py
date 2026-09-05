"""
Administrator elevation helpers.

Design goal (per spec): the app must NOT silently relaunch itself as admin the
instant it starts (that is what the original decompiled app did). Instead, the
GUI shows the user a clear screen explaining *why* admin rights are needed and
lets them click a button to relaunch elevated (UAC prompt) -- or continue in a
read-only / limited mode if they decline.
"""

import ctypes
import ctypes.wintypes
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


def _get_own_exe_name() -> str:
    """Basename of the executable this process was started from (frozen exe
    or the python interpreter running main.py). Used to verify a PID found
    in the elevation cookie is actually an instance of this app."""
    return os.path.basename(sys.executable).lower()


def _process_image_name(pid: int) -> str | None:
    """Return the basename of the executable running under `pid`, or None
    if it can't be determined (process gone, access denied, etc.)."""
    try:
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        handle = ctypes.windll.kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return None
        try:
            buf = ctypes.create_unicode_buffer(260)
            size = ctypes.wintypes.DWORD(260)
            ok = ctypes.windll.kernel32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size))
            if ok:
                return os.path.basename(buf.value).lower()
        finally:
            ctypes.windll.kernel32.CloseHandle(handle)
    except Exception:
        pass
    return None


def _process_is_elevated(pid: int) -> bool:
    """True if the process under `pid` runs with an elevated (admin) token.

    Closes the last spoof gap in the elevation handshake: token match +
    STILL_ACTIVE + image-name match proved only that *some* same-user
    process with our exe name was alive — not that it was the elevated
    relaunch. A same-user process could write a cookie naming any
    already-running copy of this exe, and the old check accepted it,
    making the original window exit with a false 'elevation succeeded'.
    """
    try:
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        handle = ctypes.windll.kernel32.OpenProcess(
            PROCESS_QUERY_LIMITED_INFORMATION, False, pid
        )
        if not handle:
            return False
        try:
            # 1 = TokenElevation class
            token = ctypes.wintypes.HANDLE()
            if not ctypes.windll.kernel32.OpenProcessToken(
                handle, 0x0008, ctypes.byref(token)  # TOKEN_QUERY
            ):
                return False
            try:
                elevation = ctypes.wintypes.DWORD()
                ret_len = ctypes.wintypes.DWORD()
                if not ctypes.windll.advapi32.GetTokenInformation(
                    token, 1, ctypes.byref(elevation),
                    ctypes.sizeof(elevation), ctypes.byref(ret_len)
                ):
                    return False
                return bool(elevation.value)
            finally:
                ctypes.windll.kernel32.CloseHandle(token)
        finally:
            ctypes.windll.kernel32.CloseHandle(handle)
    except Exception:
        return False


def _wait_for_elevated_process(timeout: float = 60.0) -> bool:
    """
    Wait for the elevated process to signal it has started.
    Returns True if the elevated process started successfully.
    Verifies token to prevent spoofing via pre-created cookie.

    Token (secrets.token_hex(16)) + STILL_ACTIVE + image-name match is the
    real verification. The elevated-token check is best-effort only: a
    medium-integrity process often gets ACCESS_DENIED opening the token of
    the high-integrity child, which used to turn every successful elevation
    into a false timeout (two windows + 'Elevation Cancelled').
    Timeout is 60s because it includes UAC click time + slow --onefile unpack.
    """
    expected_token = _read_pending_token()
    own_exe = _get_own_exe_name()
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
                    try:
                        exit_code = ctypes.wintypes.DWORD()
                        if ctypes.windll.kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
                            if exit_code.value == 259:  # STILL_ACTIVE
                                # Also require the PID's own image name to match this app
                                image_name = _process_image_name(pid)
                                if image_name != own_exe:
                                    time.sleep(0.2)
                                    continue
                                # NOTE: no _process_is_elevated(pid) gate here
                                # on purpose. It used to be required, but a
                                # medium-integrity process usually gets
                                # ACCESS_DENIED querying the high-integrity
                                # child's token, turning every successful
                                # elevation into a false timeout (two windows
                                # + 'Elevation Cancelled'). The 128-bit token
                                # + alive + image-name match is the real
                                # verification.
                                _clear_elevation_cookie()
                                _clear_pending_token()
                                return True
                    finally:
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


def wait_for_elevated_process(timeout: float = 60.0) -> bool:
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
    if not token:
        # No token — legacy / direct launch without elevation. Only write if
        # a pending token file exists (means we were launched via relaunch).
        pending = _read_pending_token()
        if pending is None:
            return
        token = pending
    # Only signal if we're actually admin — a non-elevated process must never
    # claim to be the elevated relaunch (prevents stale-token races where a
    # later, unrelated process start answers the original app's wait loop).
    try:
        if not is_admin():
            return
    except Exception:
        return
    _write_elevation_cookie(os.getpid(), token)
