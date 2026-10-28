"""
Shared low-level helpers used by the clean / repair / tweak task modules.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from dataclasses import dataclass, field
from typing import Callable, Optional

from app.changes import record_command, record_registry

IS_WINDOWS = sys.platform.startswith("win")

from app.delete_safety import (  # noqa: F401  (re-exported on purpose)
    _is_own_runtime,
    _is_protected_root,
    _is_reparse_point,
    _own_runtime_roots,
    clean_folder_contents,
)

# R-008: the exe lookup and the PowerShell argv are leaf modules now, so this
# module can use the same builder every task uses. Both are imported at module
# scope BECAUSE neither imports utils back (that is the whole point of the
# split): exe_paths -> os, powershell -> exe_paths.
from app.exe_paths import resolve_exe  # noqa: F401  (re-exported on purpose)
from app.powershell import ps_argv


if IS_WINDOWS:
    import winreg
    import ctypes
else:  # pragma: no cover
    winreg = None  # type: ignore
    ctypes = None  # type: ignore

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


def atomic_write_text(dest: str, content: str, newline: str = "\n",
                      *, restore_readonly: bool = False,
                      errors: str = "strict") -> None:
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
        with os.fdopen(fd, "w", encoding="utf-8", newline=newline,
                       errors=errors) as fh:
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


#: Schemes a link from the UI may be handed to the shell with. Everything
#: else — `file:`, `javascript:`, `ms-settings:` outside its own allowlist,
#: a bare `C:\...`, or a typo — is refused by `is_web_url()`.
WEB_URL_SCHEMES = frozenset({"http", "https"})


def is_web_url(url: str) -> bool:
    """True only for a well-formed http(s) URL.

    Callers hand the result to the OS shell (os.startfile, `cmd /c start`,
    ShellExecute), which resolves ANY scheme it knows — including `file:`
    (opens a local path) and registered protocol handlers. Catalog metadata
    is data, not code, so it gets validated at the boundary instead of being
    trusted.
    """
    try:
        from urllib.parse import urlsplit
        text = (url or "").strip()
        if not text or len(text) > 2048:
            return False
        parts = urlsplit(text)
        if parts.scheme.lower() not in WEB_URL_SCHEMES:
            return False
        return bool(parts.hostname)
    except Exception:
        return False


def exclusive_create_text(dest: str, content: str, newline: str = "\n",
                          errors: str = "strict") -> bool:
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
    # BUG-005: the guard could never succeed. `_is_reparse_point(dest) or
    # os.path.exists(dest)` -- the reparse test is deliberately fail-closed, and
    # GetFileAttributesW returns INVALID_FILE_ATTRIBUTES for a path that does
    # NOT exist, which maps to True. So for every fresh path the first operand
    # was True, `or` short-circuited, and this returned False without ever
    # reaching os.open. Measured: a new path returned False and created no
    # file; bypassing only that operand made the same call work.
    #
    # Consequence: apply_ad_blocker's one-time backup of the Windows hosts file
    # never happened, silently, while the apply still reported success.
    #
    # `os.path.lexists`, not `os.path.exists`: exists() FOLLOWS symlinks, so a
    # dangling one answers False and we would happily write through it.
    # lexists() is the "is anything here at all" test this actually wants.
    #
    # The reparse check is then unnecessary rather than merely reordered:
    # os.open with O_CREAT|O_EXCL already refuses to follow a symlink and
    # refuses to clobber an existing file, atomically. That is a stronger
    # guarantee than a check-then-act test, which has a TOCTOU window anyway.
    if os.path.lexists(dest):
        return False
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
    try:
        fd = os.open(dest, flags, 0o600)
    except FileExistsError:
        return False
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline=newline,
                       errors=errors) as fh:
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
    """Kill proc AND its child tree (needed when shell=True on Windows).

    BUG-006: the order was wrong. `proc.kill()` ran FIRST and synchronously,
    and only then was the `taskkill /T` tree walk fired off in a thread. With
    a shell=True run, `proc` is cmd.exe and the real tool (DISM.exe,
    sfc.exe, winget.exe) is its child -- so by the time taskkill ran, the
    parent was already gone and /T had no tree left to enumerate. Measured:
    taskkill exited 128, "The process "N" not found.", and the child was still
    alive two seconds later. Nothing stopped it: the `finally` in run_cmd
    clears the cancel registry, so a later Stop could never reach it either.
    The user was told "command cancelled" while an elevated DISM kept running.

    So: walk the tree FIRST, while the parent still exists to anchor it, and
    fall back to proc.kill() for anything taskkill missed (a non-Windows
    platform, or a process that exited between the two calls).
    """
    if IS_WINDOWS:
        pid = getattr(proc, "pid", None)
        if pid is not None:
            # Fire-and-forget so this does not block the cancel hot path
            # (timeout=5 would add 5s of tail latency to every Stop).
            def _taskkill():
                try:
                    subprocess.run(
                        ["taskkill", "/PID", str(pid), "/T", "/F"],
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
    try:
        proc.kill()
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

    progress: Optional[Callable[[float, str], None]] = None
    """Optional `(percent, raw_line)` callback for a determinate progress bar.

    Only commands whose output carries a real percentage use it (DISM, SFC,
    chkdsk, defrag). `percent` is 0-100. The raw line is passed along so a
    caller can show the tool's own stage text, but the app deliberately shows
    its own task label instead: "DISM RestoreHealth 61%" is useful, a wall of
    bracket characters is not. Default None keeps every existing caller and
    the headless scheduler byte-identical.
    """

    quarantine: Optional[object] = None
    """Optional app.quarantine.Quarantine handle (roadmap 2.5). When set, the
    clean funnel (clean_folder_contents) MOVES matched files into the bin
    instead of deleting them. Default None = ordinary deletes, so every
    existing caller is unchanged. Typed `object` to keep utils free of an
    import on the quarantine module.
    """

    notes: "list[str]" = field(default_factory=list)
    """Lines a task wants its caller to show beside the outcome: facts only
    the task knows, such as which update results a localized winget could not
    report. NOT transcript lines - the task has already logged those.
    The windowed install runner repeats them in the completion card, whose
    counts alone cannot carry a list; headless callers have no card and keep
    reading them out of the log they already capture.
    """

    changes: "list" = field(default_factory=list)
    """Typed records of what the run actually changed (R-005).

    Filled by the shared write layer (`reg_set_value*`, `reg_delete_value`,
    `run_cmd*`) at the moment of the write, where the prior and new value are
    both in hand - not reconstructed later from the transcript. The runner
    slices it per task (`TaskOutcome.changes`) so the scorecard can group and
    print real before -> after lines. Shared by reference in `derive()` for the
    same reason `notes` is: a task that re-wraps the context must still land in
    the run's ledger.
    """

    #: Count of files a clean had to leave alone because something held
    #: them open (U-007). Collected by the shared delete walker, so the run
    #: summary can explain a result smaller than the estimate instead of
    #: presenting it as the whole answer. 0 when nothing was locked.
    skipped_locked: int = 0

    unmeasured: "list[str]" = field(default_factory=list)
    """Reasons a task DECLINED TO LOOK, in its own words (`mark_unmeasured`).

    Several cleaners refuse to run while the thing they clean is live - Office
    holding unsynced edits, Steam mid-download, FiveM in a session - or when the
    list they judge against is untrustworthy (orphan scan with a partial
    installed-programs set). They return 0, which is the right answer for a real
    run (nothing was freed either way) and a lie for an estimate: a dry pass
    cannot tell "empty" from "did not look", so Storage Insight painted the
    category as clean and graded it fine. The refusal is recorded here instead,
    and `app.storage_scan` reports that task's size as unknown. Empty in the
    ordinary case, so every other caller sees exactly what it saw before.
    Shared by reference in `derive()` like `notes`, so a task that re-wraps the
    context still reaches the caller's list.
    """
    def derive(self, *, log=None, set_status=None,
               cancelled=None) -> "TaskContext":
        """A copy with the given CALLBACKS replaced and the data fields kept.

        H-005: a runner that wanted to reword the live status line rebuilt the
        dataclass from scratch with `TaskContext(log=..., set_status=...,
        cancelled=...)`. That silently dropped `dry_run`, `quarantine` and —
        the one that mattered — `notes`, so the package names a localized
        update run collected went into a list nobody read. Anything that
        re-wraps a context goes through here instead, and `notes` is passed by
        reference on purpose: a task appending to `ctx.notes` must land in the
        caller's list.
        """
        return TaskContext(
            log=log if log is not None else self.log,
            set_status=(set_status if set_status is not None
                        else self.set_status),
            cancelled=(cancelled if cancelled is not None
                       else self.cancelled),
            dry_run=self.dry_run,
            progress=self.progress,
            quarantine=self.quarantine,
            notes=self.notes,
            changes=self.changes,
            skipped_locked=self.skipped_locked,
            unmeasured=self.unmeasured,
        )


def mark_unmeasured(ctx, reason: str) -> None:
    """Record that this task declined to look, so its 0 is not a measurement.

    Call this beside the `return 0` of every refusal-to-run ("Office may be
    running", "Steam is running", "could not read the installed list"), never
    beside a genuine empty result: the point is to separate 0 bytes from "no
    answer", and a caller's estimate turns this into unknown.

    Tolerant by design - a stub context without the field, or one that refuses
    the append, must not turn a clean refusal into a crash.
    """
    try:
        bucket = getattr(ctx, "unmeasured", None)
        if bucket is None:
            return
        bucket.append(str(reason))
    except Exception:
        pass


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
            # H2: imported from the leaf module, not from app.config_persist.
            # That was the last edge of the repository's only import cycle
            # (utils -> config_persist -> tab_presets -> tasks -> utils);
            # the event log is observability and needs nothing from the
            # config loader, so it no longer has to drag it in.
            from app.config_events import log_security_event
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

# BUG-021: a `cmd_arg()` helper used to live here. It was documented as making
# `&`, `|`, `>`, `%`, `^` and spaces "travel as data, never as command
# separators" inside a shell=True string, and it did NONE of that except
# whitespace. Its body was `subprocess.list2cmdline([str(value)])`, which quotes
# only for whitespace, embedded quotes and empty strings — it performs no
# metacharacter escaping at all:
#
#     'C:\a&b' -> 'C:\a&b'    # not quoted
#     'a|b'    -> 'a|b'        # not quoted
#     'C:\a b' -> '"C:\a b"'  # quoted (whitespace only)
#
# So a caller who trusted the docstring got a false sense of safety. It is
# DELETED rather than fixed, because there is no correct version of it: escaping
# metacharacters for cmd.exe inside a shell string is a losing game. Pass an
# argv list with shell=False instead (see run_cmd), where no shell exists to
# parse them. That is the pattern the rest of this codebase already uses.


import re as _re

#: DISM prints "[=====   8.8%   ]", SFC prints "Verification 45% complete.",
#: chkdsk prints "Progress: 118932 of 2000896 done; Stage: 5%; Total: 3%" and
#: defrag prints "Retrim:  20% complete...". Hundreds of lines per run; in a
#: deep repair that was ~600 of the log's 970 lines, drowning the lines that
#: matter. One line per 10% is enough to show progress in the LOG; the run
#: strip gets every 1% through the progress callback below, without printing.
_PROGRESS_RES = (
    _re.compile(r"^\[[= ]*\s*(\d+(?:\.\d+)?)%\s*[= ]*\]$"),       # DISM
    _re.compile(r"^Verification\s+(\d+)%\s+complete\.?$", _re.I),   # SFC
    _re.compile(r"^Retrim:\s*(\d+(?:\.\d+)?)%\s+complete", _re.I),
)

#: chkdsk's "Total" is NOT cumulative: it drops from ~37% back to ~6% when
#: stage 2 starts (measured on the reference machine), so feeding it to a
#: progress bar would move it backwards. It is still progress NOISE, though:
#: hundreds of repeated lines. Recognise it so the log can drop it, and leave
#: the bar indeterminate for that one command.
_CHKDSK_PROGRESS_RE = _re.compile(
    r"^Progress:\s*\d+\s+of\s+\d+\s+done;", _re.I)


def _progress_percent(line: str):
    """The percentage in a DISM/SFC/defrag progress line, else None."""
    for rx in _PROGRESS_RES:
        m = rx.match(line)
        if m:
            try:
                return float(m.group(1))
            except (TypeError, ValueError):
                return None
    return None


def _emit_progress(ctx, pct, line: str) -> None:
    """Tell a UI (if one is attached) how far a command has got.

    `TaskContext.progress` is optional so headless runs are byte-identical to
    before. The callback must never be able to break a repair: a failing UI
    hop is swallowed, exactly like the log callback.
    """
    cb = getattr(ctx, "progress", None)
    if cb is None:
        return
    try:
        cb(float(pct), str(line))
    except Exception:
        pass


def store_available() -> bool:
    """True only when the Microsoft Store app is installed for this user.

    LTSC, IoT, Server and N/KN-stripped images ship without it. On those,
    `wsreset.exe` has nothing to reset and instead opens an
    "ms-windows-store:" link that no app owns, so Windows pops a "Get an app
    to open this link" / "not found" prompt in the middle of a clean. A
    cleanup tool must not trigger prompts on the user's desktop, so Store
    tasks check first and skip.

    When the check itself cannot run (PowerShell blocked, timeout) this
    returns False: a skipped Store-cache reset costs nothing, a stray popup
    does.
    """
    if not IS_WINDOWS:
        return False
    try:
        out = subprocess.check_output(
            ps_argv("if (Get-AppxPackage -Name Microsoft.WindowsStore "
                    "-ErrorAction SilentlyContinue) { 'STORE_YES' } "
                    "else { 'STORE_NO' }", non_interactive=True),
            text=True, timeout=25, stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        return "STORE_YES" in (out or "")
    except Exception:
        return False


def _popen_command(command, shell: bool):
    """What to hand subprocess.Popen for `command` WITHOUT involving a shell.

    A list/tuple is already an argv and passes through. A STRING with
    shell=False is the common legacy form ("sfc /scannow", "net stop X"):
      * on Windows, Popen hands the string straight to CreateProcess, which
        parses it with the C runtime's quoting rules and runs the program
        directly - there is no cmd.exe, so `& | > ^ %` are ordinary
        characters. This is exactly how the argv form behaves, with the
        quoting already written out.
      * elsewhere (the Linux test harness) the string is split with shlex,
        because Popen would otherwise try to execute the whole string as one
        program name.
    shell=True is still honoured for a caller that explicitly asks for
    cmd.exe features, but nothing in the app does (dev/tools/verify_run_cmd_no_shell
    fails the build if one appears).
    """
    if shell or isinstance(command, (list, tuple)):
        return command
    if IS_WINDOWS:
        return command
    import shlex
    return shlex.split(command)


def run_cmd(ctx: TaskContext, command, shell: bool = False, timeout: Optional[int] = None,
            collect: "list | None" = None) -> int:
    """Run a command, streaming each output line to ctx.log.

    `command` is an argv SEQUENCE (preferred) or a command string. The default
    is `shell=False` (BUG-RES-001): there is no cmd.exe, so `&`, `|`, `>`, `^`
    and `%` are just characters in an argument - the only construction that
    makes them inert (BUG-021). A string is run directly by CreateProcess on
    Windows (see `_popen_command`), so existing "tool /switch" strings keep
    working. Pass `shell=True` only for a command that genuinely needs
    cmd.exe built-ins; the build fails if one is added.

    `collect` (optional): when given a list, every stripped non-empty output
    line is also appended to it — lets long batch runs (e.g. `winget
    upgrade --all`) build an honest per-item success/failure summary without
    a second slow invocation. Backward compatible: all existing callers
    omit it and behave exactly as before."""
    if isinstance(command, (list, tuple)):
        # Render the way the user would recognise it, not as a raw repr.
        ctx.log("$ " + subprocess.list2cmdline([str(c) for c in command]))
    else:
        ctx.log(f"$ {command}")
    try:
        startupinfo = None
        creationflags = 0
        if IS_WINDOWS:
            startupinfo = subprocess.STARTUPINFO()
            startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            creationflags = subprocess.CREATE_NO_WINDOW

        proc = subprocess.Popen(
            _popen_command(command, shell),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            # A GUI process has no console stdin. Commands that ask a
            # question (chkdsk's "schedule at next restart? (Y/N)") would
            # block or fail on the inherited handle; give them a closed
            # input so they take their default and exit.
            stdin=subprocess.DEVNULL,
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
            _last_bucket = [-1]       # last 10% step logged (progress lines)
            _last_pct = [-1.0]        # last 1% step sent to the progress bar

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
                # SFC writes UTF-16: read as an 8-bit codepage every character
                # is followed by a NUL ("B\x00e\x00g\x00i\x00n..."). That filled
                # the log with garbage AND made every substring check on SFC's
                # verdict ("did not find any integrity violations") fail.
                # Dropping the NULs recovers the ASCII text exactly.
                line = item.replace("\x00", "").strip()
                if line:
                    # H-007: the 10% bucket check exists so the LOG does not
                    # print the same progress line ten times. It used to
                    # `continue` before the collect branch, so every repeat was
                    # dropped from `collect` too - and `collect` is what the
                    # winget upgrade summary parses. Progress lines are output
                    # like any other; only their repetition is boring.
                    log_line = True
                    pct = _progress_percent(line)
                    if pct is not None:
                        bucket = int(pct // 10)
                        if bucket == _last_bucket[0]:
                            log_line = False
                        else:
                            _last_bucket[0] = bucket
                        # The strip is a different consumer from the log: it
                        # wants whole percent steps, not 10% buckets.
                        whole = int(pct)
                        if whole != int(_last_pct[0]):
                            _last_pct[0] = pct
                            _emit_progress(ctx, pct, line)
                    elif _CHKDSK_PROGRESS_RE.match(line):
                        # Dropped from the log as repeated noise, and never
                        # sent to the bar (see _CHKDSK_PROGRESS_RE).
                        log_line = False
                    if log_line:
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
            rc = proc.returncode if proc.returncode is not None else -1
            # R-005: a recognised service/task/power/boot command that
            # succeeded is a typed change. The classifier ignores everything
            # else (and the volatile net start/stop pair) by design.
            if rc == 0:
                record_command(ctx, command, ok=True)
            return rc
        except Exception as exc:
            _kill_proc(proc)
            ctx.log(f"  ! ERROR running command: {exc}")
            return -1
    except Exception as exc:
        ctx.log(f"  ! ERROR running command: {exc}")
        return -1
    finally:
        _set_current_proc(None)


def run_capture(command, timeout: float = 30) -> "tuple[int | None, str]":
    """Run a console command and return (returncode, combined output text).

    I-001: the same five-line wrapper was written by hand in the scheduler, in
    the elevated-launch helper and in a dozen task modules, and the copies
    drifted in ways that only show up on a real machine:

      * some omitted CREATE_NO_WINDOW, so every probe flashed a console in the
        middle of a GUI run;
      * some omitted `errors="replace"`, so a localized (non-UTF-8) Windows
        raised UnicodeDecodeError and the probe looked like "the command
        failed";
      * some inherited the GUI's stdin, so a tool that asks a question
        blocked;
      * timeouts were chosen per copy, and a few were missing entirely.

    returncode is None (and the text is the error) when the command could not
    be run at all - never a fake 0, so a caller cannot mistake a launch
    failure for success. Never raises. For long jobs with a transcript, use
    run_cmd instead: this one buffers, so keep it for probes and status
    queries.
    """
    try:
        kwargs = {}
        if IS_WINDOWS:
            kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        result = subprocess.run(
            _popen_command(command, False),
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL, text=True, errors="replace",
            timeout=timeout, **kwargs)
        return result.returncode, result.stdout or ""
    except Exception as exc:
        return None, str(exc)


def run_cmd_checked(ctx: TaskContext, command, shell: bool = False, timeout: Optional[int] = None,
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
        # rc 0 already recorded itself inside run_cmd; a non-zero success
        # (sfc's 1, DISM's 3010) did not, so record it here.
        record_command(ctx, command, ok=True)
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


def format_bytes(size_bytes: float, compact: bool = False) -> str:
    """Human byte count.

    `compact=True` gives the short form a stat tile can hold - "1.5 GB",
    "512 B" - with at most one decimal and no trailing ".00". Six Tools
    popups had each grown their own byte formatter, which is why the same
    number rendered differently in different windows; the second mode lives
    here so there is still exactly one implementation.
    """
    if size_bytes <= 0:
        return "0 B" if compact else "0 Bytes"
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

    def out(value, unit):
        if not compact:
            return f"{value:.2f} {unit}"
        if value >= 100:
            return f"{value:.0f} {unit}"
        if value >= 10:
            return f"{value:.0f} {unit}"
        return (f"{value:.1f} {unit}").replace(".0 ", " ")

    if n >= gib:
        v = n / gib
        if round(v, 2) < 1024:
            return out(v, "GB")
        return out(n / (gib * 1024), "TB")
    if n >= mib:
        v = n / mib
        if round(v, 2) < 1024:
            return out(v, "MB")
        return out(n / 1024, "GB")
    if n >= kib:
        v = n / kib
        if round(v, 2) < 1024:
            return out(v, "KB")
        return out(n / 1024, "MB")
    return (f"{n} B") if compact else (f"{n} Bytes")


# --------------------------------------------------------------------------- #
# BUG-014 protected-root guard
# --------------------------------------------------------------------------- #
# The audit finding: clean_folder_contents() is the single destructive funnel
# for the Clean tab, the Games tab and four Repair-tab tasks, and it had NO
# guard on WHICH directory it was pointed at. Any caller bug therefore became
# an arbitrary recursive delete. game_tasks.clean_vrchat_cache turned that into
# a live data-loss bug (a user-writable config.json supplying the delete root).
#
# This guard is the last line of defence for every caller at once.
#
# DESIGN — EXACT normalized match, never a prefix/containment test. That
# distinction is the whole point: we deny the CONTAINER and allow the subtree.
# Roughly 25 legitimate call sites clean things that live strictly INSIDE
# these very roots — C:\Windows\Prefetch, C:\Windows\SoftwareDistribution\
# Download, DeliveryOptimization, the print spooler, %LOCALAPPDATA%\...\
# Cache, Program Files app shader caches. A containment check would have
# broken core functionality; an exact check leaves all of them working.

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


def _describe_reg_write_error(exc: BaseException) -> str:
    """Turn a registry-write exception into an honest, specific reason.

    3.3: every failure used to be reported as "Administrator rights may be
    required", which sent users (and the audit) chasing permissions for causes
    that had nothing to do with access — a KeyError on an unknown value type is
    the exact bug that masked H-3. Split the common cases so the message names
    the real cause.
    """
    if isinstance(exc, KeyError):
        return (f"unknown registry value type {exc!r} "
                "(internal error — NOT a permissions problem)")
    if isinstance(exc, PermissionError) or getattr(exc, "winerror", None) == 5:
        return ("access denied — run as Administrator, or a policy/ACL "
                "protects this key")
    if getattr(exc, "winerror", None) in (2, 3):
        return "the key path could not be created (it does not exist)"
    return f"{type(exc).__name__}: {exc}"


def _change_prior(ctx: TaskContext, hive: str, path: str, name: str):
    """(prior value, known) for the change ledger - read only when recording.

    A context whose `changes` list is missing (an old caller, a test double)
    pays no extra registry read at all. A denied read is NOT "absent": it is
    reported as unknown so the card never prints a false "not set -> 0".
    """
    if not isinstance(getattr(ctx, "changes", None), list):
        return None, False
    try:
        prior, _type = reg_get_value_typed(ctx, hive, path, name)
    except Exception:                                        # pragma: no cover
        return None, False
    if prior is _REG_DENIED:
        return None, False
    return prior, True


def _reg_set_value_ex(ctx: TaskContext, hive: str, path: str, name: str, value,
                      value_type: str = "REG_DWORD"):
    """Write a registry value. Returns (ok, reason) — `reason` is a specific,
    user-facing explanation when ok is False, "" when it succeeded."""
    # H-3 (HIGH) fix: callers may pass value_type=None (the Advanced tab's write
    # specs and _VISUAL_EFFECTS_DEFAULTS use None to mean "the default DWORD").
    # Because the parameter has a default, an EXPLICIT None bypassed it, and
    # _TYPE_MAP[None] raised KeyError — which the except below logged as the
    # misleading "Administrator rights may be required" message. Normalise
    # falsy to the documented default here.
    if not value_type:
        value_type = "REG_DWORD"
    prior, prior_known = _change_prior(ctx, hive, path, name)
    ctx.log(f"reg add {hive}\\{path} /v {name or '(Default)'} /d {value}")
    key = None
    try:
        root = _HIVES[hive]
        key = winreg.CreateKeyEx(root, path, 0, _reg_access(write=True))
        winreg.SetValueEx(key, name if name else None, 0, _TYPE_MAP[value_type], value)
        # R-005: record at the write, not in the transcript afterwards.
        record_registry(ctx, hive, path, name, prior, value,
                        before_known=prior_known, type_name=value_type)
        return True, ""
    except Exception as exc:
        ctx.log(f"  ! registry write failed: {exc}")
        return False, _describe_reg_write_error(exc)
    finally:
        if key is not None:
            try:
                winreg.CloseKey(key)
            except Exception:
                pass


def reg_set_value(ctx: TaskContext, hive: str, path: str, name: str, value, value_type: str = "REG_DWORD") -> bool:
    return _reg_set_value_ex(ctx, hive, path, name, value, value_type=value_type)[0]


def reg_set_value_checked(ctx: TaskContext, hive: str, path: str, name: str, value,
                          value_type: str = "REG_DWORD") -> None:
    """reg_set_value that RAISES on failure (F-005 / F-023).

    The run/revert engine counts any task that does not raise as
    'succeeded' and marks the tweak applied (the '✓ Active' badge).
    Task functions that ignored reg_set_value's False return therefore
    reported success — with a badge and an undo entry — for tweaks that
    never actually wrote (typical case: HKLM policy values in limited
    mode). This wrapper converts a failed write into an honest task
    failure, so the runner reports it and never marks it applied.

    3.3: the message now carries the specific reason from the write
    (_describe_reg_write_error) instead of always blaming Administrator
    rights, so an unknown-type or missing-key failure is no longer
    misreported as a permissions problem.

    Reverts in this codebase write the complete target state (they are
    not incremental undo), so a partially-applied task self-heals when
    the user re-runs it or runs Undo."""
    _ok, reason = _reg_set_value_ex(ctx, hive, path, name, value, value_type=value_type)
    if not _ok:
        raise RuntimeError(
            f"Could not write {hive}\\{path}"
            + (f"\\{name}" if name else "")
            + f" — {reason}. The change did NOT apply; nothing was marked "
              "as applied.")


def reg_delete_value(ctx: TaskContext, hive: str, path: str, name: str) -> bool:
    prior, prior_known = _change_prior(ctx, hive, path, name)
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
        record_registry(ctx, hive, path, name, prior, None,
                        before_known=prior_known)
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
    is None ONLY if the key/value genuinely does not exist; _REG_DENIED if it
    may exist but could not be read (F13: missing vs denied must not
    collapse - a denied prior snapshotted as absent made revert DELETE a
    pre-existing value; MED-008 widened that: ANY read failure that is not a
    definitive not-found now counts as denied, because a sharing violation or
    a busy hive is not evidence of absence). In both of those cases type_name
    is None (nothing was queried)."""
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
        # MED-008: only a DEFINITIVE "it is not there" may be reported as
        # absent. Everything else is UNKNOWN, and unknown must never become
        # "absent" here, because the caller cannot tell the difference:
        # _snap_reg_values records `present: False` and _restore_reg_values
        # then DELETES a value that actually existed.
        #
        # The old code only mapped winerror 5 / errno 5,13 to _REG_DENIED and
        # returned "absent" for everything else - so an ordinary sharing
        # violation (32, file open by another process), a hive-busy error
        # (33 / 234, another process is using the hive) or a transient I/O
        # error was enough to make Undo delete a real user setting.
        #
        # So: the two error codes that genuinely mean "no such key/value" are
        # absent; everything else is denied/unknown.
        if getattr(exc, "winerror", None) in (2, 3) or getattr(exc, "errno", None) in (2, 3):
            return None, None
        return _REG_DENIED, None
    except Exception:
        # Same reasoning, and this catch-all had the identical flaw: an
        # unexpected exception was reported as "absent", so it too could make a
        # revert delete a value the user had. Unknown is not absent.
        return _REG_DENIED, None
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
            [powercfg_exe, "/q", "scheme_current", str(sub), str(set_guid)],
            capture_output=True, text=True, timeout=15,
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

def _is_explorer_running():
    """True if at least one explorer.exe exists, False if definitely none,
    None if the probe itself failed.

    MED-006: this returned False on any error, which is indistinguishable
    from "Explorer is gone". restart_explorer waits for the old shell to
    exit BEFORE spawning a replacement, so a tasklist timeout used to break
    that wait instantly and launch a SECOND explorer.exe while the first was
    still alive - the shell-handoff race the function's own docstring says it
    exists to prevent, and the source of the reported "sometimes the taskbar
    never comes back". Callers now only act on a definitive answer."""
    try:
        tasklist_exe = resolve_exe("tasklist")
        out = subprocess.check_output(
            [tasklist_exe, "/fi", "imagename eq explorer.exe", "/fo", "csv",
             "/nh"],
            text=True, timeout=5, stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        return "explorer.exe" in out.lower()
    except Exception:
        return None


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
    # MED-006: break ONLY on a definitive "gone". None means the probe failed,
    # which is not evidence of exit - keep waiting rather than spawning a
    # second shell alongside the first. Worst case we fall through to the
    # timeout log below, which is the safe direction.
    for _ in range(20):  # up to 10s
        if _is_explorer_running() is False:
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
            # MED-006: only a definitive False means "it died, retry the
            # spawn", and only a definitive True may claim success. None
            # (probe unavailable) keeps polling instead of guessing either way.
            _alive = _is_explorer_running()
            if _alive is False:
                break
            if _alive is True:
                time.sleep(2.0)  # let a would-be bail-out actually bail
                if _is_explorer_running() is True:
                    ctx.log("Explorer restarted successfully (taskbar is back).")
                    return True
                break  # it died again — retry spawn
        # not running after 3s — fall through to retry
    ctx.log("  ! Explorer did not stay running after restart. Your taskbar may be missing —")
    ctx.log("  ! press Ctrl+Shift+Esc, click File > Run new task, and type explorer.exe.")
    return False


def restore_point_create_ps(description: str = "Cleaner Tool - safety point") -> str:
    """PowerShell that creates one System Restore checkpoint and prints a
    LANGUAGE-NEUTRAL status token.

    B-056: Windows returns a LOCALIZED error when the 24-hour throttle trips
    ("... 1440 minutes ..." exists only in English), so matching that text
    broke on non-English systems — a throttled create was reported as a hard
    failure. Instead of parsing text, the script asks the API how old the
    newest point is and prints RESTORE_THROTTLED itself; on success it prints
    RESTORE_OK. Callers classify on those tokens first and only fall back to
    text matching for genuinely unexpected errors.
    """
    safe = (description or "Cleaner Tool - safety point").replace("'", "''")
    return (
        "$ErrorActionPreference='Stop';"
        "[Console]::OutputEncoding=[Text.Encoding]::UTF8;"
        # A disabled System Restore / VSS service is the common reason a
        # checkpoint cannot be made (Windows 10/11 ships with System
        # Protection OFF on many machines, and LTSC images often have the
        # service disabled outright). Detect it as its own state BEFORE the
        # 24-hour check, so the caller can report a skip with instructions
        # instead of an unexplained failure.
        "foreach ($n in @('SDRSVC','VSS')) {"
        "$s = Get-Service -Name $n -ErrorAction SilentlyContinue;"
        "if ($s -and $s.StartType -eq 'Disabled') "
        "{ Write-Output 'RESTORE_DISABLED'; return }};"
        "try {"
        "$last = Get-ComputerRestorePoint | Sort-Object CreationTime "
        "-Descending | Select-Object -First 1;"
        "if ($last -and ((Get-Date) - $last.CreationTime).TotalMinutes -lt 1440) "
        "{ Write-Output 'RESTORE_THROTTLED'; return };"
        "Checkpoint-Computer -Description '%s' -RestorePointType MODIFY_SETTINGS;"
        "Write-Output 'RESTORE_OK'"
        "} catch {"
        "$m = [string]$_.Exception.Message;"
        "if ($m -match 'service cannot be started|ServiceDisabled|disabled') "
        "{ Write-Output 'RESTORE_DISABLED'; return };"
        "throw }" % safe
    )


def create_restore_point(ctx: TaskContext, description: str = "GamerOpt Cleaner - before changes") -> bool:
    ctx.set_status("Creating a System Restore point (safety net)...")
    # audit hardening: description flows into a shell=True string — pass it
    # as an argv list (shell=False) so no escaping/quoting games are needed.
    ps_script = restore_point_create_ps(description)
    out: list = []
    rc = run_cmd(ctx, ps_argv(ps_script),
                 shell=False, timeout=120, collect=out)
    blob = "\n".join(out)
    # BUG-013 / B-056: Windows allows only ONE System Restore checkpoint per 24
    # hours. The script prints RESTORE_THROTTLED (language-neutral) when the
    # newest point is younger than that, so a throttled create is a skip, not a
    # failure. The English phrasings are kept only as a fallback.
    low = blob.lower()
    # System Protection off / the restore service disabled. This is a
    # machine-state answer, not a task failure: "Safety Checkpoint - failed:
    # Task returned False" told the user nothing they could act on. Classify
    # it as a skip, with the exact place to turn it on.
    if ("restore_disabled" in low or "servicedisabled" in low
            or "service cannot be started" in low):
        ctx.log("  i Skipped: System Protection is off for this drive (or its "
                "restore service is disabled), so Windows cannot make a "
                "restore point.")
        ctx.log("  i Turn it on: Start > type 'Create a restore point' > "
                "Configure > Turn on system protection. Then run this again.")
        raise TaskSkipped(
            "System Protection is off for this drive, so Windows cannot "
            "create restore points until it is turned on")
    if "restore_throttled" in low or "1440 minutes" in low or \
            "restore point cannot be created" in low:
        ctx.log("  i Skipped: Windows allows only one restore point per 24 hours,")
        ctx.log("  i and one was already created today. Nothing to do.")
        raise TaskSkipped("a restore point was already created in the last 24h")
    if rc == 0 or "restore_ok" in low:
        ctx.log("Restore point created successfully.")
        return True
    ctx.log("  ! Could not create a restore point for another reason (exit code %s)." % rc)
    ctx.log("  ! Check that System Protection is on for this drive, then retry.")
    return False
