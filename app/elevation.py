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

from app.logsafe import append_text_no_follow, read_text_no_follow, redact
from app.powershell import ps_argv


#: Sinks inside `_log_or_swallow_fallback` that have already complained. It is
#: the last-resort logger, so a broken sink is reported ONCE per process: a
#: read-only config dir makes the event log fail on every single call, and
#: repeating that line would bury the message it exists to carry.
_reported_sinks: set[str] = set()


def _report_broken_sink(name: str, exc: BaseException) -> None:
    """Say once, on stderr, that one of the fallback logger's sinks failed.

    Deliberately not routed through `_log_or_swallow_fallback`: that would
    recurse straight back into the sink that just failed. The `pass` below is
    the true last resort — if stderr itself is broken there is nowhere left to
    report to, and raising here would mask the caller's original exception.
    """
    if name in _reported_sinks:
        return
    _reported_sinks.add(name)
    try:
        print(f"  ! elevation log sink '{name}' is failing "
              f"({type(exc).__name__}: {exc})", file=sys.stderr)
    except Exception:
        pass


def _log_or_swallow_fallback(msg: str, exc: BaseException | None = None) -> None:
    """REL-001: elevation.py runs before the GUI/TaskContext exist, so it
    can't use app.utils.log_or_swallow directly. This mirrors it — best
    effort, never raises — so state-changing cookie/token I/O failures
    leave a trail (stderr + the security event log) instead of vanishing.
    """
    try:
        # B-003: every sink below is a file the user may share. The message
        # and the exception are redacted HERE, once, so a future call site that
        # interpolates a token cannot leak it through any of them.
        text = f"  ! {redact(msg)}"
        if exc is not None:
            text += f" ({type(exc).__name__}: {redact(exc)})"
        print(text, file=sys.stderr)
        # Also write to temp file for debugging fast closes
        try:
            log_path = os.path.join(tempfile.gettempdir(), "cleaner_tool_elevation.log")
            # H-001/I-003: no link-following append, and the trail is bounded.
            append_text_no_follow(log_path, f"[{os.getpid()}] {text}\n")
        except Exception as exc:
            # A sink failing used to be dropped here. Report which one, so a
            # support session can tell "nothing was written" apart from "the
            # trail could not be written" — the two need different fixes.
            _report_broken_sink("tempfile", exc)
        try:
            from app.config_persist import log_security_event
            log_security_event("ops", text.strip())
        except Exception as exc:
            _report_broken_sink("security-event", exc)
        try:
            # I-003: the crash log is the ONE file a support session asks for,
            # and a handshake failure that only reached %TEMP% was invisible
            # there. diagnostics.log never raises, and importing it here (not
            # at module scope) keeps elevation's early-startup import cost.
            from app.diagnostics import log as _diag_log
            _diag_log(text)
        except Exception as exc:
            _report_broken_sink("diagnostics", exc)
    except Exception:
        # Genuine end of the line: building or printing the message itself
        # failed. Logging a logging failure would recurse, so this is the one
        # swallow left — and it is asserted never to raise.
        pass


#: Failure sites that have already left a trail. The ctypes probes below are
#: called from hot paths — the wait loop polls at 5 Hz, the pilot sweeps every
#: running process — so a permanently broken query would otherwise write
#: hundreds of identical lines per wait. Report the first failure per site: a
#: ctypes call that fails on this machine fails the same way every time.
_reported_failures: set[str] = set()


def _log_once(key: str, msg: str, exc: BaseException | None = None) -> None:
    """`_log_or_swallow_fallback`, at most once per process per failure site."""
    if key in _reported_failures:
        return
    _reported_failures.add(key)
    _log_or_swallow_fallback(msg, exc)


def is_admin() -> bool:
    """Return True if the current process is running with administrator rights."""
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception as exc:
        # False is the safe answer (callers fall back to limited mode), but it
        # is indistinguishable from a real "not admin" — and it is what every
        # "could not check elevation" message to the user rests on. Record the
        # cause once, here, rather than at each of those three call sites.
        _log_or_swallow_fallback("could not query admin status", exc)
        return False


def _get_elevation_cookie_path() -> str:
    """Get path to a temporary file used for elevation coordination."""
    return os.path.join(tempfile.gettempdir(), "CleanerTool_elevation_cookie")


def _get_elevation_token_path() -> str:
    """Path storing the expected random token for the pending elevation."""
    return os.path.join(tempfile.gettempdir(), "CleanerTool_elevation_token")


def _exclusive_write(path: str, data: str) -> None:
    """F05 / SEC-002: predictable %TEMP% names + plain open() let a
    same-user attacker pre-create a symlink/file to force false-success
    or DoS. Unlink any pre-existing entry (removes a planted symlink)
    then create exclusively (O_CREAT|O_EXCL). On failure we raise so the
    caller aborts the handshake and falls through to a normal (unelevated)
    launch — never follow a raced-in link via open(path, "w")."""
    try:
        os.unlink(path)
    except OSError:
        pass
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    fd = os.open(path, flags, 0o600)
    try:
        os.write(fd, data.encode("utf-8"))
    finally:
        os.close(fd)


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
        # H-001: fixed name in %TEMP% — never read through a planted link.
        raw = read_text_no_follow(_get_elevation_cookie_path()).strip()
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
    except FileNotFoundError:
        pass        # nothing pending — the normal case, not worth a trail
    except Exception as exc:
        # A file that exists but cannot be removed leaves a stale cookie that
        # a LATER wait could accept as its own, so the cause is worth keeping.
        _log_or_swallow_fallback("could not clear elevation cookie", exc)


def _write_pending_token(token: str) -> None:
    try:
        _exclusive_write(_get_elevation_token_path(), token)
    except Exception as exc:
        _log_or_swallow_fallback("could not write pending elevation token", exc)


def _read_pending_token() -> str | None:
    try:
        # H-001: same rule as the cookie reader.
        return read_text_no_follow(_get_elevation_token_path()).strip()
    except FileNotFoundError:
        return None
    except Exception as exc:
        _log_or_swallow_fallback("could not read pending elevation token", exc)
        return None


def _clear_pending_token() -> None:
    try:
        os.remove(_get_elevation_token_path())
    except FileNotFoundError:
        pass        # nothing pending — the normal case, not worth a trail
    except Exception as exc:
        _log_or_swallow_fallback("could not clear pending elevation token", exc)


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
    except Exception as exc:
        # The wait loop turns this into "image name mismatch, got=None" five
        # times a second: the consequence, never the cause.
        _log_once("image-name",
                  f"could not query the image name for pid {pid}", exc)
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
    except Exception as exc:
        # Auto-Pilot folds "" into "not under a watched folder", so without
        # this a broken query looks like a game the user never configured.
        _log_once("exe-path",
                  f"could not query the full path for pid {pid}", exc)
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
    except Exception as exc:
        # "False" here is indistinguishable from a genuine limited token, so
        # the caller's answer would be wrong with nothing to show why.
        _log_once("is-elevated",
                  f"could not check whether pid {pid} is elevated", exc)
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
    # B-003: presence, never the value or a prefix of it.
    _log_or_swallow_fallback(
        "_wait_for_elevated_process: starting wait, pending token "
        + ("present" if expected_token else "missing"))
    # B-014: monotonic, not wall clock. This wait runs during logon/relaunch,
    # exactly when the Windows Time service may step the clock; a backward step
    # stretched the 60 s budget while the parent sat with two windows up, and a
    # forward step ended it early and reported a successful elevation as
    # "Elevation Cancelled".
    start_time = time.monotonic()
    while time.monotonic() - start_time < timeout:
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
                _log_or_swallow_fallback(
                    "_wait_for_elevated_process: token mismatch")
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
                                    _log_or_swallow_fallback(f"_wait_for_elevated_process: image name mismatch, got={image_name}, expected={own_exe}")
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
                                _log_or_swallow_fallback(f"_wait_for_elevated_process: elevation succeeded (pid {pid})")
                                try:
                                    from app.config_persist import log_security_event
                                    log_security_event("elevation", f"Elevation succeeded (pid {pid}).")
                                except Exception as exc:
                                    _log_or_swallow_fallback(
                                        "could not record the elevation success event", exc)
                                return True
                    finally:
                        ctypes.windll.kernel32.CloseHandle(handle)
            except Exception as exc:
                _log_or_swallow_fallback("_wait_for_elevated_process: exception checking process", exc)
                pass
        time.sleep(0.2)
    # audit fix (H10): a timed-out wait left the pending token (and any
    # stale cookie) on disk. A later, unrelated elevation attempt reusing
    # the temp path — or a delayed cookie write arriving after we've given
    # up and moved on — could then be accepted by a *different* wait call
    # as if it were this one. Clear both on timeout so a give-up is final.
    _log_or_swallow_fallback("_wait_for_elevated_process: timed out")
    _clear_pending_token()
    _clear_elevation_cookie()
    try:
        from app.config_persist import log_security_event
        log_security_event("elevation", "Elevation timed out or was cancelled.")
    except Exception as exc:
        _log_or_swallow_fallback(
            "could not record the elevation timeout event", exc)
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
            argv = [executable] + list(sys.argv[1:]) + [f"--elevation-token={token}"]
            # B-003: the argv carries the token, so the trail names the exe and
            # the argument count instead of the command line.
            _log_or_swallow_fallback(
                f"relaunch_as_admin: frozen mode, exe={executable}, "
                f"args={len(argv)}")
        else:
            # Running as a normal python script: relaunch python.exe with the
            # script path as the first argument.
            executable = sys.executable
            script = os.path.abspath(sys.argv[0])
            argv = [executable, script] + list(sys.argv[1:]) + [f"--elevation-token={token}"]
            _log_or_swallow_fallback(
                f"relaunch_as_admin: script mode, exe={executable}, "
                f"script={script}, args={len(argv)}")

        # Build the full command line
        cmd_line = subprocess.list2cmdline(argv)
        _log_or_swallow_fallback(
            f"relaunch_as_admin: cmd_line={redact(cmd_line)}")

        # Use PowerShell to run the command elevated - this is more reliable
        # for passing arguments to Python scripts than ShellExecuteW directly.
        # NOTE: the entire argument list (and the executable path) must be
        # single-quoted as one PS string. Leaving it bare makes PowerShell
        # parse parentheses / spaces in the path as syntax (e.g. folders
        # named "...(3)"), which failed elevation in real installs.
        _ps_exe = executable.replace("'", "''")
        _ps_args = subprocess.list2cmdline(argv[1:]).replace("'", "''")
        ps_cmd = f"Start-Process -FilePath '{_ps_exe}' -ArgumentList '{_ps_args}' -Verb RunAs"
        _log_or_swallow_fallback(f"relaunch_as_admin: ps_cmd={redact(ps_cmd)}")

        # R-008: `resolve_exe` now pins the same WindowsPowerShell\v1.0 path
        # this used to build by hand (see app/exe_paths.py), so the shared
        # runner gives the identical exe and the identical flags.
        result = subprocess.run(
            ps_argv(ps_cmd, non_interactive=True),
            shell=False,
            capture_output=True,
            text=True,
            timeout=30,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )

        if result.returncode != 0:
            _log_or_swallow_fallback(f"relaunch_as_admin: PowerShell failed with rc={result.returncode}, stderr={result.stderr}")
            _clear_pending_token()
            return False

        _log_or_swallow_fallback("relaunch_as_admin: PowerShell succeeded")
        return True
    except Exception as exc:
        _log_or_swallow_fallback("relaunch_as_admin: exception", exc)
        try:
            _clear_pending_token()
        except Exception as exc2:
            # `_clear_pending_token` swallows its own errors, so this only
            # fires if it is ever changed not to. Still worth a trail: a token
            # left behind is what makes the NEXT elevation attempt ambiguous.
            _log_or_swallow_fallback(
                "relaunch_as_admin: could not clear the pending token", exc2)
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
            _log_or_swallow_fallback("signal_elevated_startup: no token and no pending token")
            return
        token = pending
    # Only signal if we're actually admin — a non-elevated process must never
    # claim to be the elevated relaunch (prevents stale-token races where a
    # later, unrelated process start answers the original app's wait loop).
    try:
        if not is_admin():
            _log_or_swallow_fallback("signal_elevated_startup: not admin, cannot signal")
            return
    except Exception as exc:
        _log_or_swallow_fallback("signal_elevated_startup: admin check failed", exc)
        return
    _write_elevation_cookie(os.getpid(), token)


def _current_exe_path() -> str:
    """Absolute path of the running executable (frozen .exe or script)."""
    try:
        if getattr(sys, "frozen", False):
            return os.path.abspath(sys.executable)
        return os.path.abspath(sys.argv[0] or sys.executable)
    except Exception:
        try:
            return os.path.abspath(sys.executable)
        except Exception:
            return ""


# SEC-004: the record of exclusions THIS application added, so that turning
# the setting off can undo exactly those and nothing else.
_EXCLUSION_KEY = "defender_exclusions_applied"


def _ps_quote(s: str) -> str:
    """Quote a path for embedding in a PowerShell command string.

    Single-quote it and double any embedded single quotes, so spaces and
    apostrophes in a user path cannot break out of the string literal.
    """
    return "'" + (s or "").replace("'", "''") + "'"


def _run_mp_script(script: str) -> tuple[bool, str]:
    """Run a Defender preference script. Returns (ok, combined output)."""
    # Built locally ON PURPOSE, and pinned harder than the shared runner: this
    # is the only place a Defender preference script runs, and its contract is
    # tested (dev/tests/test_elevation_psrunner.py) - the %SystemRoot% path wins so
    # a PATH entry cannot shadow it, the bare name is used ONLY when that file
    # is absent (never a which() lookup), and -ExecutionPolicy Bypass is part
    # of the command line callers rely on. Hence the documented exceptions
    # below rather than ps_argv.
    ps_exe = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"),
                          "System32", "WindowsPowerShell", "v1.0",
                          "powershell.exe")   # ps-ok: pinned SystemRoot path (PSRUNNER contract)
    if not os.path.isfile(ps_exe):
        ps_exe = "powershell.exe"        # ps-ok: bare-name fallback when that file is absent
    result = subprocess.run(
        [
            ps_exe,
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy", "Bypass",
            "-Command", script,
        ],
        shell=False,
        capture_output=True,
        text=True,
        timeout=30,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    out = ((result.stdout or "") + (result.stderr or "")).strip()
    return result.returncode == 0, out


def _applied_exclusions() -> list[str]:
    """The exclusions this app believes it added. Never raises."""
    try:
        from app.config_persist import load_config
        raw = load_config().get(_EXCLUSION_KEY)
        if not isinstance(raw, list):
            return []
        return [p for p in raw if isinstance(p, str) and p]
    except Exception as exc:
        # [] means "this app has no record", which makes the removal path act
        # on nothing. A read failure looks identical, so it needs a trail.
        _log_or_swallow_fallback("could not read the applied-exclusion record", exc)
        return []


def _record_applied_exclusions(paths=(), remove=(), merge=False) -> None:
    """Remember what we added so the removal path can undo exactly it.

    `paths` REPLACES the record unless `merge=True`, in which case it is added
    to whatever is already there. The removal paths want replace — they hand in
    the complete new record — while the add path wants merge, because a
    portable copy run from a second folder adds an exclusion while the first
    one still exists, and only this record can name that first one again.

    `remove` names entries to forget. The record is only pruned after
    Remove-MpPreference actually succeeded, so a failed removal stays
    recorded and is retried on the next elevated launch. Never raises.
    """
    try:
        from app.config_persist import update_config

        def _mut(cfg):
            # Read and write inside the mutator: update_config holds the config
            # lock for both, so merging cannot lose a concurrent write.
            entries = set()
            if merge:
                entries = {p for p in (cfg.get(_EXCLUSION_KEY) or [])
                           if isinstance(p, str) and p}
            for p in paths or ():
                if isinstance(p, str) and p:
                    entries.add(p)
            for p in remove or ():
                if isinstance(p, str):
                    entries.discard(p)
            cfg[_EXCLUSION_KEY] = sorted(entries)

        # NOTE: update_config takes the MUTATOR, so this must be `_mut`, not
        # `_mut()`. It shipped as `_mut()` - which calls _mut() with no `cfg`
        # argument, raises TypeError, and was then swallowed by the bare
        # except below, so the record was never written and the whole removal
        # path silently had nothing to remove. The bare except is kept (this is
        # best-effort bookkeeping and must never break a launch), but a failure
        # here means removal cannot work, which is worth knowing.
        update_config(_mut)
    except Exception as exc:
        # The most consequential swallow in this module: a failed write means
        # the removal path has nothing to find, so an exclusion outlives the
        # decision to have it. Still never raises — but never silent either.
        _log_or_swallow_fallback("could not record the applied exclusions", exc)


def remove_defender_exclusions(paths=None) -> tuple[bool, str]:
    """SEC-004: remove the exclusions THIS APPLICATION added.

    Only recorded paths are removed, and it never clears Defender's whole
    exclusion list - doing that would silently drop exclusions the user or
    another tool set deliberately, which is a far worse surprise than a
    leftover entry. Returns (ok, human-readable message). Never raises.
    """
    try:
        if not is_admin():
            return False, "not running as administrator"
    except Exception as exc:
        _log_or_swallow_fallback("could not check elevation", exc)
        return False, "could not check elevation"

    recorded = _applied_exclusions()
    if paths is None:
        targets = list(recorded)
    else:
        allow = set(recorded)
        targets = []
        for p in paths:
            if not isinstance(p, str) or not p:
                continue
            try:
                ap = os.path.abspath(p)
            except Exception as exc:
                _log_or_swallow_fallback(
                    f"could not resolve exclusion path {p!r}", exc)
                continue
            # An explicit path must still be one we recorded. The `not allow`
            # case is for installs predating the record, where the only
            # exclusion this app ever added was its own binary.
            if (ap in allow or not allow) and ap not in targets:
                targets.append(ap)

    if not targets:
        return True, "no exclusions recorded by this app"

    ps_script = "; ".join(
        f"Remove-MpPreference -ExclusionPath {_ps_quote(p)} -ErrorAction SilentlyContinue"
        for p in targets)
    try:
        ok, out = _run_mp_script(ps_script)
    except Exception as exc:
        return False, f"{type(exc).__name__}: {exc}"
    if ok:
        # Hand the CURRENT record back in, then subtract what was removed.
        # `_record_applied_exclusions` REPLACES the record with what it is
        # given, so passing `()` (the "add nothing" form) wrote the remainder
        # of an empty set: removing one path wiped the paths that were never
        # asked for, and nothing could ever find them again. Harmless while
        # the only caller asks for everything, wrong the moment one doesn't.
        _record_applied_exclusions(recorded, remove=targets)
        return True, f"removed {len(targets)} exclusion(s)"
    return False, out or "Remove-MpPreference failed"


def list_defender_exclusions() -> list[str]:
    """Current Defender ExclusionPath entries. [] if unavailable. Never raises.

    Read-only: used by the migration notice to spot an exclusion an OLDER
    build applied, which is not in our record because the record did not exist
    then.
    """
    try:
        script = ("(Get-MpPreference).ExclusionPath | "
                  "ForEach-Object { $_.ToString() }")
        ok, out = _run_mp_script(script)
    except Exception as exc:
        # [] is indistinguishable from "Defender is not installed", which is
        # exactly what the migration notice reasons about. Say why.
        _log_or_swallow_fallback("could not list Defender exclusions", exc)
        return []
    if not ok:
        return []
    return [ln.strip() for ln in (out or "").splitlines() if ln.strip()]


def find_stale_folder_exclusion() -> str:
    """The parent FOLDER excluded by an older build, or "" if there is none.

    SEC-004 migration. Older versions defaulted this ON and excluded the
    executable's entire parent directory, not just the binary. That exclusion is
    NOT in `defender_exclusions_applied` - the record did not exist when it was
    added - so `remove_defender_exclusions()` deliberately ignores it. Leaving
    it would mean a portable copy keeps its whole folder un scanned forever.

    It is only ever REPORTED, never removed automatically: the app cannot tell
    an exclusion it added from one the user added on purpose, and silently
    deleting a security setting is worse than asking. The caller shows a
    one-time notice and the user decides.
    """
    try:
        exe = _current_exe_path()
        if not exe:
            return ""
        parent = os.path.dirname(os.path.abspath(exe))
        if not parent or parent == os.path.dirname(parent):
            return ""            # never a drive root
        have = {os.path.normcase(os.path.normpath(p)) for p in list_defender_exclusions()}
        if os.path.normcase(os.path.normpath(parent)) in have:
            return parent
    except Exception:
        pass
    return ""


def remove_path_exclusion(path: str) -> tuple[bool, str]:
    """Remove ONE explicitly-confirmed exclusion path. Never raises.

    Separate from remove_defender_exclusions() on purpose: that one only ever
    touches paths it recorded, and this one exists so the migration notice
    requires the user to name the exact path they are agreeing to remove.

    On success the path is also dropped from the SEC-004 record when it is in
    there. Usually it is not — the migration notice removes a folder an OLDER
    build excluded, from before the record existed — but a path that IS
    recorded must not be left behind after being removed by name, or the
    record keeps claiming an exclusion Defender no longer has and every later
    launch asks it to remove something that is already gone.
    """
    try:
        if not is_admin():
            return False, "not running as administrator"
    except Exception as exc:
        _log_or_swallow_fallback("could not check elevation", exc)
        return False, "could not check elevation"
    if not path or not isinstance(path, str):
        return False, "no path given"
    try:
        ok, out = _run_mp_script(
            "Remove-MpPreference -ExclusionPath %s -ErrorAction SilentlyContinue"
            % _ps_quote(path))
    except Exception as exc:
        return False, "%s: %s" % (type(exc).__name__, exc)
    if ok:
        _forget_exclusion(path)
        return True, "Removed the exclusion for %s" % path
    return False, out or "Remove-MpPreference failed"


def _forget_exclusion(path: str) -> None:
    """Drop one already-removed path from the SEC-004 record. Never raises.

    Compared with normcase/normpath because the record holds the string this
    app recorded at add time while the caller passes whatever the user or the
    migration notice named: `C:\\Apps` and `C:\\Apps\\` are the same exclusion,
    and on Windows so are two spellings that differ only in case.

    Only a genuine match rewrites the record, so a removal that has nothing to
    do with this app (an exclusion the user added) leaves it untouched.
    """
    try:
        recorded = _applied_exclusions()
        if not recorded:
            return
        try:
            key = os.path.normcase(os.path.normpath(path))
        except Exception as exc:
            _log_or_swallow_fallback(
                f"could not normalise exclusion path {path!r}", exc)
            return
        keep = [p for p in recorded
                if os.path.normcase(os.path.normpath(p)) != key]
        if keep != recorded:
            # `keep` is the whole new record: passing it as the paths to add
            # replaces the record, which is what a subtraction is here.
            _record_applied_exclusions(keep)
    except Exception as exc:
        _log_or_swallow_fallback("could not forget a removed exclusion", exc)


def ensure_defender_exclusion(paths: list[str] | None = None) -> tuple[bool, str]:
    """
    Add Windows Defender exclusion(s) for this app.

    Requires administrator rights. Safe to call repeatedly (Add-MpPreference
    is idempotent for the same path). Never raises.

    Returns (ok, message). ok is False when not admin, Defender is absent,
    or the command failed; the message is always human-readable.
    """
    try:
        if not is_admin():
            return False, "not running as administrator"
    except Exception as exc:
        _log_or_swallow_fallback("could not check elevation", exc)
        return False, "could not check elevation"

    targets: list[str] = []
    if paths:
        for p in paths:
            if p and isinstance(p, str):
                try:
                    ap = os.path.abspath(p)
                    if ap and ap not in targets:
                        targets.append(ap)
                except Exception as exc:
                    _log_or_swallow_fallback(
                        f"could not resolve exclusion path {p!r}", exc)
                    continue
    else:
        exe = _current_exe_path()
        if exe:
            targets.append(exe)
            # SEC-004: the parent directory is deliberately NOT excluded.
            # It used to be, and for a portable app that means running from
            # Desktop, Downloads or any shared folder excluded everything ELSE
            # in that folder from Defender scanning - files the user did not
            # agree to trust, on a directory they share with other things. The
            # exclusion the app actually needs is for ITS OWN binary, so that
            # is all it takes.

    if not targets:
        return False, "no path to exclude"

    ps_script = "; ".join(
        f"Add-MpPreference -ExclusionPath {_ps_quote(p)} -ErrorAction SilentlyContinue"
        for p in targets)

    try:
        ok, out = _run_mp_script(ps_script)
    except Exception as exc:
        return False, f"{type(exc).__name__}: {exc}"
    if ok:
        # SEC-004: remember exactly what we added. Without this there was no
        # removal path at all, so an exclusion could outlive the user's
        # decision to stop having it.
        #
        # MERGE, not replace: running the app from a second location (a
        # portable copy, a moved folder) excludes a new path while the old
        # exclusion still exists, and replacing the record with only the new
        # one left the old path unrecorded — nothing could ever remove it.
        _record_applied_exclusions(targets, merge=True)
        return True, f"exclusion added for {len(targets)} path(s)"
    # Non-zero can still mean partial success (Defender not installed,
    # policy blocked, etc.). Surface the message for diagnostics.
    return False, out or "Add-MpPreference failed"


def apply_defender_exclusion_if_wanted() -> None:
    """SEC-004: this used to apply a default-ON, parent-directory-wide
    Defender exclusion on every elevated launch, with no way to undo it.

    The preference is now opt-in. If the user has turned it OFF but we still
    have exclusions on record - from an earlier opt-in, or from a version
    that predates the record - those are removed, so flipping the checkbox
    back off genuinely reverses it and an older install repairs itself on its
    next elevated launch. Best-effort; never raises, never blocks for long.
    """
    try:
        if not is_admin():
            return
        from app.config_persist import load_config
        if not bool(load_config().get("add_defender_exclusion", False)):
            if _applied_exclusions():
                ok, msg = remove_defender_exclusions()
                if not ok:
                    # Discarding this was the silent half of the feature: the
                    # user unticked the box, the removal was blocked, and the
                    # exclusion stayed with nothing anywhere saying so.
                    _log_or_swallow_fallback(
                        f"could not remove the recorded exclusions: {msg}")
            return
        ok, msg = ensure_defender_exclusion()
        if not ok:
            # Same on the way in: a policy block or a third-party AV means no
            # exclusion was ever created, and the user is left believing their
            # portable exe is excluded while Defender can still quarantine it.
            _log_or_swallow_fallback(
                f"could not add the Defender exclusion: {msg}")
    except Exception as exc:
        _log_or_swallow_fallback(
            "could not apply the Defender exclusion setting", exc)
