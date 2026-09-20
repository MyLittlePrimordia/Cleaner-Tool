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


def _log_or_swallow_fallback(msg: str, exc: BaseException | None = None) -> None:
    """REL-001: elevation.py runs before the GUI/TaskContext exist, so it
    can't use app.utils.log_or_swallow directly. This mirrors it — best
    effort, never raises — so state-changing cookie/token I/O failures
    leave a trail (stderr + the security event log) instead of vanishing.
    """
    try:
        text = f"  ! {msg}"
        if exc is not None:
            text += f" ({type(exc).__name__}: {exc})"
        print(text, file=sys.stderr)
        try:
            from app.config_persist import log_security_event
            log_security_event("ops", text.strip())
        except Exception:
            pass
    except Exception:
        pass


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


def _exclusive_write(path: str, data: str) -> None:
    """F05: predictable %TEMP% names + plain open() let a same-user attacker
    pre-create a symlink/file to force false-success/DoS. Unlink any
    pre-existing entry (removes a planted symlink) then create exclusively
    (O_CREAT|O_EXCL) with a restrictive mode — the create fails instead of
    following a raced-in link. Best-effort: falls back to a plain write
    only if exclusive create is unavailable on this platform."""
    try:
        try:
            os.unlink(path)
        except OSError:
            pass
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        try:
            fd = os.open(path, flags, 0o600)
        except AttributeError:
            raise
        try:
            os.write(fd, data.encode("utf-8"))
        finally:
            os.close(fd)
    except Exception:
        try:
            with open(path, "w", encoding="utf-8") as f:
                f.write(data)
        except Exception:
            pass


def _write_elevation_cookie(pid: int, token: str = "") -> None:
    """Write the elevated process PID and token to the cookie file."""
    try:
        _exclusive_write(_get_elevation_cookie_path(), f"{pid}:{token}" if token else str(pid))
    except Exception as exc:
        # REL-001: a failed write here means the unelevated side can never
        # recognize the elevated helper came back, silently stranding the
        # user mid-handshake. Log it (best-effort — never raises) so a
        # support session has a trail instead of a mystery hang.
        _log_or_swallow_fallback("could not write elevation cookie", exc)


def _read_elevation_cookie() -> tuple[int | None, str]:
    """Read the elevated process PID and token from the cookie file."""
    try:
        with open(_get_elevation_cookie_path(), "r") as f:
            raw = f.read().strip()
            if ":" in raw:
                pid_s, token = raw.split(":", 1)
                return int(pid_s.strip()), token.strip()
            return int(raw), ""
    except FileNotFoundError:
        # Normal — no elevation is pending. Not worth logging.
        return None, ""
    except Exception as exc:
        _log_or_swallow_fallback("could not read elevation cookie", exc)
        return None, ""


def _clear_elevation_cookie() -> None:
    """Remove the elevation cookie file."""
    try:
        os.remove(_get_elevation_cookie_path())
    except Exception:
        pass


def _write_pending_token(token: str) -> None:
    try:
        _exclusive_write(_get_elevation_token_path(), token)
    except Exception as exc:
        _log_or_swallow_fallback("could not write pending elevation token", exc)


def _read_pending_token() -> str | None:
    try:
        with open(_get_elevation_token_path(), "r") as f:
            return f.read().strip()
    except FileNotFoundError:
        return None
    except Exception as exc:
        _log_or_swallow_fallback("could not read pending elevation token", exc)
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


def process_exe_path(pid: int) -> str:
    """FULL executable path of the process under `pid` ('' when unknown).

    Phase-5 pilot overhaul: the basename-truncating twin above stays for
    the elevation handshake; this one returns the whole path so the
    Auto-Pilot can verify a process really runs from a watched game
    folder (same-name-exe-anywhere no longer triggers a session)."""
    try:
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        handle = ctypes.windll.kernel32.OpenProcess(
            PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return ""
        try:
            buf = ctypes.create_unicode_buffer(1024)
            size = ctypes.wintypes.DWORD(1024)
            ok = ctypes.windll.kernel32.QueryFullProcessImageNameW(
                handle, 0, buf, ctypes.byref(size))
            if ok:
                return buf.value or ""
        finally:
            ctypes.windll.kernel32.CloseHandle(handle)
    except Exception:
        pass
    return ""


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
            # audit fix (H10): a falsy expected_token (relaunch_as_admin
            # always writes one, but a transient read failure returned
            # None/"") used to skip the match check entirely, accepting
            # ANY cookie — including one written by an unrelated already-
            # running copy of this exe. Fail closed instead: no readable
            # pending token means we can't verify identity, so don't
            # accept this cookie as ours.
            if not expected_token:
                time.sleep(0.2)
                continue
            if token != expected_token:
                time.sleep(0.2)
                continue
            # Verify the process is still alive
            # (ctypes.wintypes import hoisted out of the poll loop — it was
            # re-executed every 200ms while waiting for UAC.)
            try:
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
                                try:
                                    from app.config_persist import log_security_event
                                    log_security_event("elevation", f"Elevation succeeded (pid {pid}).")
                                except Exception:
                                    pass
                                return True
                    finally:
                        ctypes.windll.kernel32.CloseHandle(handle)
            except Exception:
                pass
        time.sleep(0.2)
    # audit fix (H10): a timed-out wait left the pending token (and any
    # stale cookie) on disk. A later, unrelated elevation attempt reusing
    # the temp path — or a delayed cookie write arriving after we've given
    # up and moved on — could then be accepted by a *different* wait call
    # as if it were this one. Clear both on timeout so a give-up is final.
    _clear_pending_token()
    _clear_elevation_cookie()
    try:
        from app.config_persist import log_security_event
        log_security_event("elevation", "Elevation timed out or was cancelled.")
    except Exception:
        pass
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
