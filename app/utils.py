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


def known_folder(name: str, fallback: str = "", *, strict: bool = False) -> str:
    """Resolve a Windows known folder from HKCU\\...\\User Shell Folders,
    honoring OneDrive/known-folder redirection (the F-4 fix, shared by the
    three formerly-duplicated resolvers in launcher_paths / game_tasks /
    repair_tasks). Returns the expandvars'd value only when it is absolute;
    otherwise the fallback. With strict=True the value must also be free of
    cmd metacharacters (&|<>^%\" and friends) — used where the result is
    embedded into a shell command string (netsh/dism) as a trusted path."""
    if not IS_WINDOWS or winreg is None:
        return fallback
    try:
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Explorer\User Shell Folders",
        ) as key:
            raw, _ = winreg.QueryValueEx(key, name)
            resolved = os.path.expandvars(raw)
            if resolved and os.path.isabs(resolved):
                if strict and any(c in resolved for c in '"&|<>^%'):
                    return fallback
                return resolved
    except OSError:
        pass
    except Exception:
        pass
    return fallback


# SEC-002 hardening: known inbox-tool absolute paths, tried before a bare
# name so a same-user-writable app/CWD directory can't shadow the real
# tool for a process that sometimes runs elevated (install/repair/tweak
# flows). Falls back to shutil.which(), then the bare name as a last
# resort (logged) so this never breaks LTSC/Server SKUs where a tool
# might live somewhere unexpected.
_EXE_CACHE: dict = {}


def resolve_exe(name: str) -> str:
    """Absolute path for a well-known Windows inbox tool (winget,
    powershell, schtasks, powercfg, tasklist, wevtutil, ping, net, ...).
    Cached per-process. Never raises — worst case returns `name` itself,
    identical to the old bare-name behavior."""
    if name in _EXE_CACHE:
        return _EXE_CACHE[name]
    result = name
    try:
        system_root = os.environ.get("SystemRoot", r"C:\Windows")
        sys32 = os.path.join(system_root, "System32")
        candidates = [os.path.join(sys32, f"{name}.exe")]
        if name == "winget":
            # winget ships via the App Installer MSIX package, not System32 —
            # WindowsApps holds a versioned real exe behind an alias stub;
            # shutil.which() finds the per-user alias correctly.
            candidates = []
        for cand in candidates:
            if os.path.isfile(cand):
                result = cand
                break
        else:
            import shutil as _shutil
            which = _shutil.which(name)
            if which:
                result = which
    except Exception:
        result = name
    _EXE_CACHE[name] = result
    return result


def atomic_write_text(dest: str, content: str, newline: str = "\n",
                      *, restore_readonly: bool = False) -> None:
    """Write `content` to `dest` atomically via a unique temp file in the
    SAME directory + os.replace — readers never observe a half-written file,
    and a crash/lock mid-write leaves the original untouched. Written with
    explicit newline control (no open() translation surprises).

    `restore_readonly` (hosts-file writers): the destination's read-only
    attribute (flipped by some AV/security tools) is cleared so os.replace
    can overwrite it, then re-applied to the new file — replicating the
    hand-rolled dance the tweak tab used to do inline."""
    import stat as _stat
    import tempfile
    dest = os.path.normpath(dest)
    parent = os.path.dirname(dest) or "."
    fd, tmp = tempfile.mkstemp(prefix=".cleanertool.tmp.", dir=parent)
    was_readonly = False
    if restore_readonly:
        try:
            mode = os.stat(dest).st_mode
            if not (mode & _stat.S_IWRITE):
                was_readonly = True
                os.chmod(dest, mode | _stat.S_IWRITE)
        except Exception:
            pass
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline=newline) as fh:
            fh.write(content)
            fh.flush()
            try:
                os.fsync(fh.fileno())
            except Exception:
                pass
        os.replace(tmp, dest)
        if was_readonly:
            try:
                os.chmod(dest, os.stat(dest).st_mode & ~_stat.S_IWRITE)
            except Exception:
                pass
    except Exception:
        try:
            os.remove(tmp)
        except Exception:
            pass
        raise


import threading
import queue


def exclusive_create_text(dest: str, content: str, newline: str = "\n") -> bool:
    """Create `dest` with `content` ONLY if nothing already exists there,
    atomically (SEC-001 audit fix). The old hosts-backup call site did
    `if not os.path.isfile(path): open(path, "w")` — a plain check-then-write
    with a window between the two, and a plain "w" open follows a symlink if
    one is already sitting at that path rather than refusing it. A low-priv
    local process (this tool runs elevated) could plant a symlink at the
    backup path pointing at an arbitrary protected file; either race would
    make the "one-time backup" write clobber that target instead. This uses
    O_CREAT|O_EXCL (a single atomic syscall — no separate check to race) plus
    an explicit _is_reparse_point rejection for a clear, logged failure
    reason, consistent with how the rest of this module already refuses to
    walk/delete through junctions and symlinks.

    Returns True if created; False if something was already there (an
    existing real backup, or someone else's file/symlink) — the caller
    should treat "already existed" as "nothing to do", not an error.
    Raises on any other failure (permissions, disk full, etc.)."""
    dest = os.path.normpath(dest)
    if _is_reparse_point(dest) or os.path.exists(dest):
        return False
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
    try:
        fd = os.open(dest, flags, 0o600)
    except FileExistsError:
        return False
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline=newline) as fh:
            fh.write(content)
            fh.flush()
            try:
                os.fsync(fh.fileno())
            except Exception:
                pass
        return True
    except Exception:
        try:
            os.remove(dest)
        except Exception:
            pass
        raise

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
    dry_run: bool = False
    """Measure-only mode (Storage Insight scan, 2026-09): walkers stat
    and sum but never delete. Default False keeps every existing caller
    byte-identical; the scan engine passes dry_run=True so all pure
    folder-cleaning tasks can be invoked directly for honest estimates
    with zero changes to any task module."""


def log_or_swallow(ctx: "TaskContext | None", msg: str, exc: BaseException | None = None, *, level: str = "!") -> None:
    """Best-effort log for state-changing paths. Never raises.
    level '!' = important failure the user should see in the run log.

    REL-001: several state-changing paths (snapshot/restore, deletions,
    elevation cookie I/O, restore-point creation, install verification)
    used to swallow exceptions completely silently, so the UI/log could
    claim success while the underlying action partially failed. This
    helper gives those paths a consistent, non-fatal way to surface the
    failure without changing any control flow or success/failure
    semantics.
    """
    try:
        text = f"  {level} {msg}"
        if exc is not None:
            text += f" ({type(exc).__name__}: {exc})"
        if ctx is not None and hasattr(ctx, "log"):
            ctx.log(text)
        else:
            # fallback when no context (elevation, early init)
            print(text, file=sys.stderr)
        # also feed the existing security event log when available
        try:
            from app.config_persist import log_security_event
            log_security_event("ops", text.strip())
        except Exception:
            pass
    except Exception:
        pass


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

def cmd_arg(value) -> str:
    """Quote one token for use inside a run_cmd(command, shell=True) string.
    Windows cmd.exe quoting is genuinely broken, so this uses
    subprocess.list2cmdline — the same rules subprocess applies when given
    a list — meaning `&`, `|`, `>`, `%`, spaces etc. inside a value travel
    as data, never as command separators. Callers that embed user- or
    registry-derived values into shell command strings should route them
    through this (F5-1: shell=True stays the default everywhere for
    backward compatibility; the helper makes it safe)."""
    try:
        return subprocess.list2cmdline([str(value)])
    except Exception:
        return str(value)


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
            # REL-001 audit fix: this used to give up (and kill) exactly 30s
            # after EOF unconditionally, ignoring whatever `timeout=` the
            # caller had already agreed to — silently overriding a longer
            # budget with a shorter, unrelated one. sfc/DISM run with
            # timeout=600-1800 specifically because they're slow; a build
            # that finishes printing output at t=1750s but takes a further
            # 31s for Windows to actually reap the process (final log
            # writes, handle release) got force-killed and reported FAILED
            # at t=1780s, inside its own 1800s budget. Continue counting
            # against that SAME budget (elapsed since the command started,
            # not a fresh clock since EOF) when the caller gave one; only
            # fall back to the original 30s cap when they didn't, since
            # nothing else would ever bound an unbounded call in that case.
            # Never LESS patient than the old flat 30s, only ever more.
            if timeout is not None:
                deadline = start_time + max(timeout, 30)
            else:
                deadline = wait_start + 30
            while proc.poll() is None:
                if ctx.cancelled():
                    _kill_proc(proc)
                    ctx.log("  ! command cancelled")
                    return -1
                if time.time() > deadline:
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
    # H6: rc -1 means cancelled/timed-out/gave-up (see run_cmd). A user
    # Stop must surface as TaskCancelled ("stopped") — never as a plain
    # RuntimeError failure. Timeouts (cancelled() False) stay failures.
    if rc == -1 and ctx.cancelled():
        raise TaskCancelled(f"Command cancelled by user: {command}")
    if rc not in success_codes:
        raise RuntimeError(f"Command failed (code {rc}): {command}")
    if rc != 0 and rc in success_codes:
        ctx.log(f"  (completed successfully; exit code {rc} — a reboot may be required)")
    return rc


# --------------------------------------------------------------------------- #
# Filesystem helpers
# --------------------------------------------------------------------------- #

def format_run_summary(succeeded: int, failed: int, skipped: int, freed_bytes: int = 0) -> "tuple[str, str]":
    """Return (title, body) for the run-complete toast.

    UX-001: single source of truth for "what just happened" copy, so the
    toast and the scorecard chips never disagree, and an all-skipped run
    (nothing needed cleaning) doesn't get told it "finished" like real
    work happened. Pure function — no side effects, easy to unit-test.
    """
    freed = format_bytes(freed_bytes) if freed_bytes else None

    if failed == 0 and succeeded == 0 and skipped > 0:
        title = "Cleaner Tool - Nothing to Clean"
        body = f"All {skipped} selected item(s) were already clean."
    elif failed == 0 and succeeded > 0:
        title = "Cleaner Tool - Clean Complete"
        body = f"{succeeded} task(s) finished"
        if freed:
            body += f". Freed {freed}"
        if skipped:
            body += f" ({skipped} already clean)"
        body += "."
    elif failed > 0 and succeeded > 0:
        title = "Cleaner Tool - Finished With Errors"
        body = f"{succeeded} succeeded, {failed} failed"
        if freed:
            body += f". Freed {freed}"
        body += ". See the run log."
    elif failed > 0 and succeeded == 0:
        title = "Cleaner Tool - Clean Failed"
        body = f"All {failed} task(s) failed — see the run log for details."
    else:
        title = "Cleaner Tool - Run Finished"
        body = "No tasks ran."
    return title, body


def format_bytes(size_bytes: float) -> str:
    if size_bytes <= 0:
        return "0 Bytes"
    # audit fix: every boundary must compare the ROUNDED-UP byte count to
    # the threshold — the old float compare rendered 1023.6 B as
    # "1024 Bytes", 1048575.9 B as "1024.00 KB" (pseudo-units: a value
    # displayed as exactly 1024 of a unit is always really 1 of the next).
    # M1 residual: ceil fixed the threshold pick but `:.2f` rounding can
    # still promote (1048575 B -> 1024.00 KB). Promote when the ROUNDED
    # display value reaches 1024.
    import math
    n = math.ceil(size_bytes)
    kib, mib, gib = 1024, 1024 ** 2, 1024 ** 3
    if n >= gib:
        v = n / gib
        if round(v, 2) < 1024:
            return f"{v:.2f} GB"
        return f"{n / (gib * 1024):.2f} TB"
    if n >= mib:
        v = n / mib
        if round(v, 2) < 1024:
            return f"{v:.2f} MB"
        return f"{v / 1024:.2f} GB"
    if n >= kib:
        v = n / kib
        if round(v, 2) < 1024:
            return f"{v:.2f} KB"
        return f"{v / 1024:.2f} MB"
    return f"{n} Bytes"


def clean_folder_contents(ctx: TaskContext, folder_path: str, remove_root: bool = False,
                           extensions: Optional[List[str]] = None) -> int:
    bytes_freed = 0
    skipped = 0
    if not folder_path or not os.path.exists(folder_path):
        return 0
    # F03 entry guard: os.walk(top) enumerates THROUGH a junction root even
    # with followlinks=False (only subdirs are pruned). Refuse to walk when
    # the root itself is a reparse point — both _clean_many funnels land here.
    if _is_reparse_point(folder_path):
        try:
            ctx.log(f"  ! skipping junction/symlink root: {folder_path}")
        except Exception:
            pass
        return 0
    # Storage Insight scan (2026-09): ctx.dry_run walks the exact same
    # tree with the exact same guards (junctions, cancel checks) but
    # never deletes — the returned total is what a real run WOULD free.
    # The flag is ctx-carried (not a parameter) so every pure task
    # function (_clean_many callers) measures correctly with no edits.
    dry = bool(getattr(ctx, "dry_run", False))

    # Security guard (audit HIGH finding): os.walk(topdown=False) cannot
    # prune and does not treat Windows junctions as links — a junction
    # anywhere under folder_path redirected deletions into its target
    # (empirically verified). Walk top-down and skip reparse points.
    stopped = False
    for root, dirs, files in os.walk(folder_path, topdown=True, followlinks=False):
        # F-3 audit fix: the Stop button previously only killed the tracked
        # SUBPROCESS — this pure-Python walker kept deleting for minutes on
        # big trees (shader caches: tens of thousands of files) after the
        # user pressed Stop. Poll cancellation here (per directory) and per
        # file below; a stop leaves the remaining files in place and returns
        # the partial total, exactly like the run_cmd kill path.
        if ctx.cancelled():
            stopped = True
            break
        # never descend into junctions / symlinked dirs
        dirs[:] = [d for d in dirs if not _is_reparse_point(os.path.join(root, d))]
        for f in files:
            if extensions and not any(f.lower().endswith(e.lower()) for e in extensions):
                continue
            filepath = os.path.join(root, f)
            if _is_reparse_point(filepath):
                skipped += 1
                continue
            if ctx.cancelled():
                stopped = True
                break
            try:
                # Get size and remove atomically (avoid TOCTOU race).
                # Dry-run: stat only, never remove — same accounting.
                st = os.stat(filepath)
                size = st.st_size
                if not dry:
                    # F5-2: re-verify IMMEDIATELY before removal — a path
                    # that became a junction/symlink since enumeration would
                    # redirect os.remove through it into the target.
                    if _is_reparse_point(filepath):
                        skipped += 1
                        continue
                    os.remove(filepath)
                bytes_freed += size
            except (PermissionError, OSError, FileNotFoundError):
                skipped += 1
                continue
        if stopped:
            break

        if not extensions and not dry:
            for d in dirs:
                if ctx.cancelled():
                    stopped = True
                    break
                dirpath = os.path.join(root, d)
                try:
                    if os.path.exists(dirpath) and not os.listdir(dirpath):
                        # F5-2: re-check before rmdir — a junction planted
                        # between enumeration and here must not be removed.
                        if _is_reparse_point(dirpath):
                            continue
                        os.rmdir(dirpath)
                except OSError:
                    continue
            if stopped:
                break

    if remove_root and not extensions and not dry and not _is_reparse_point(folder_path) and not stopped:
        try:
            if os.path.exists(folder_path) and not os.listdir(folder_path):
                os.rmdir(folder_path)
        except OSError:
            pass

    if stopped:
        if dry:
            ctx.log(f"  ! scan stopped early — sizes under {folder_path} are partial")
        else:
            ctx.log(f"  ! stopped early — some files were left in place under {folder_path}")
    if skipped:
        if dry:
            ctx.log(f"  (skipped {skipped} unreadable files in {folder_path})")
        else:
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
    "REG_QWORD": winreg.REG_QWORD if IS_WINDOWS else None,
    "REG_MULTI_SZ": winreg.REG_MULTI_SZ if IS_WINDOWS else None,
}

# CT-001 audit fix: reverse lookup so a snapshot can record the ACTUAL prior
# winreg type (from QueryValueEx) instead of guessing. Without this, reverting
# a value that was never given an explicit spec type got stamped REG_DWORD by
# default even when the real prior value was REG_SZ, and restoring a string
# under REG_DWORD raises inside winreg.SetValueEx (revert fails outright).
_TYPE_MAP_REV = {v: k for k, v in _TYPE_MAP.items() if v is not None}

# F13 sentinel: value exists but is unreadable (access denied). Distinct
# from None (absent) so snapshots never record "absent" for denied keys.
_REG_DENIED = object()


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


def reg_get_value_typed(ctx: TaskContext, hive: str, path: str, name: str):
    """Read a registry value AND its real winreg type (CT-001 audit fix).
    Returns (value, type_name) where type_name is one of _TYPE_MAP's keys
    (e.g. "REG_SZ"), or None if the type has no name in _TYPE_MAP. Value
    is None if the key/value doesn't exist; _REG_DENIED if it exists but
    can't be read (F13: missing vs denied must not collapse — a denied
    prior snapshotted as absent made revert DELETE a pre-existing value).
    In both of those cases type_name is None (nothing was queried)."""
    key = None
    try:
        root = _HIVES[hive]
        key = winreg.OpenKey(root, path, 0, _reg_access(write=False))
        value, vtype = winreg.QueryValueEx(key, name if name else None)
        return value, _TYPE_MAP_REV.get(vtype)
    except FileNotFoundError:
        return None, None
    except PermissionError:
        return _REG_DENIED, None
    except OSError as exc:
        # ERROR_ACCESS_DENIED == 5 (winerror); errnos vary by SKU.
        if getattr(exc, "winerror", None) == 5 or getattr(exc, "errno", None) in (5, 13):
            return _REG_DENIED, None
        return None, None
    except Exception:
        return None, None
    finally:
        if key is not None:
            try:
                winreg.CloseKey(key)
            except Exception:
                pass


def reg_get_value(ctx: TaskContext, hive: str, path: str, name: str):
    """Read a registry value. Returns the value; None if the key/value
    doesn't exist; _REG_DENIED if it exists but can't be read (F13:
    missing vs denied must not collapse — a denied prior snapshotted as
    absent made revert DELETE a pre-existing value)."""
    value, _vtype = reg_get_value_typed(ctx, hive, path, name)
    return value


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
        powercfg_exe = resolve_exe("powercfg")
        result = subprocess.run(
            f'"{powercfg_exe}" /q scheme_current {sub} {set_guid}',
            shell=True, capture_output=True, text=True, timeout=15,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        out = result.stdout or ""
    except Exception as exc:
        ctx.log(f"  ! could not query power setting {subgroup}:{setting}: {exc}")
        return None, None
    # M2: the old order tried the last-two-hex heuristic first, so on
    # English systems a Minimum/Maximum/AC triple (or any extra trailing
    # hex line) silently misread max-as-AC. Try the precise English labels
    # first (exact when they match); use the label-free last-two heuristic
    # only where labels are localized away, warning when the shape is
    # ambiguous (>2 candidates).
    ac = dc = None
    m = _re.search(r"Current AC Power Setting Index:\s*0x([0-9A-Fa-f]+)", out)
    if m:
        ac = int(m.group(1), 16)
    m = _re.search(r"Current DC Power Setting Index:\s*0x([0-9A-Fa-f]+)", out)
    if m:
        dc = int(m.group(1), 16)
    if ac is None or dc is None:
        indexes = []
        for line in out.splitlines():
            m = _re.search(r":\s*0x([0-9A-Fa-f]+)[ \t]*$", line)
            if m:
                indexes.append(m.group(1))
        if len(indexes) >= 2:
            if len(indexes) > 2:
                ctx.log(f"  ! powercfg output for {setting} has an unexpected shape "
                        f"({len(indexes)} hex values) — taking the last two as AC/DC.")
            if ac is None:
                ac = int(indexes[-2], 16)
            if dc is None:
                dc = int(indexes[-1], 16)
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
        tasklist_exe = resolve_exe("tasklist")
        out = subprocess.check_output(
            f'"{tasklist_exe}" /fi "imagename eq explorer.exe" /fo csv /nh',
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
