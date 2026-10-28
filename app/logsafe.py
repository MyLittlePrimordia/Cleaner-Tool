"""Redaction and link-safe I/O for anything that leaves the process.

Two jobs, both from the 2026-10 audit (B-003, H-001, H-003):

  * `redact()` is the ONE place secrets are removed from text that becomes a
    log line, a %TEMP% trail or an exported support bundle. The elevation
    handshake's token used to be written verbatim (the whole `argv`, the
    command line and the PowerShell string all carry
    ``--elevation-token=<secret>``), and `export_logs()` appends the event-log
    tail to a file users are asked to attach to public issues.
  * `read_text_no_follow()` / `append_text_no_follow()` close the other half
    of the %TEMP% handshake surface: the write side already used
    ``O_CREAT|O_EXCL`` (`elevation._exclusive_write`), but the readers and the
    debug-log writers still followed a link planted at the fixed, predictable
    paths. Reads now refuse links outright; appends open with
    ``O_NOFOLLOW`` where the platform has it, otherwise they check first.

Kept dependency-free (stdlib only, no app imports) so `app.elevation` can use
it before the config layer exists. Tests: dev/tests/test_logsafe.py.
"""
from __future__ import annotations

import contextlib
import os
import re
import time

#: `--elevation-token=<anything>` -> value replaced, flag kept legible.
_ELEVATION_ARG = re.compile(r"(--elevation-token\s*=?\s*)([^\s'\"]+)",
                            re.IGNORECASE)
#: `token=<hex>` / `token: <hex>` -> value replaced (wait-loop diagnostics).
_TOKEN_PAIR = re.compile(r"\b(token|secret)(\s*[=:]\s*)([0-9a-f]{16,})",
                         re.IGNORECASE)
#: `C:\Users\<name>\...` -> the user's name is an identifier, not a fact a
#: public bug report needs. Drive, separator and the rest are kept.
_USER_DIR = re.compile(r"([A-Za-z]:[\\/]Users[\\/])([^\\/\s\"']+)",
                       re.IGNORECASE)

#: Default cap for the small debug trails; they are append-only and had no
#: bound at all (I-003). 512 KB matches the event log's ceiling.
_TRAIL_MAX_BYTES = 512_000
_TRAIL_KEEP_LINES = 2000


def redact(text: str) -> str:
    """`text` with credentials and the user's name removed. Never raises.

    Deliberately narrow: a SHA-256 or MD5 digest stays readable, because a
    support bundle is where those are checked against a release. Only the
    elevation token (the handshake's anti-spoof secret) and the account name
    are scrubbed.
    """
    try:
        out = str(text if text is not None else "")
    except Exception:
        return ""
    out = _ELEVATION_ARG.sub(lambda m: m.group(1) + "<redacted>", out)
    out = _TOKEN_PAIR.sub(lambda m: m.group(1) + m.group(2) + "<redacted>",
                          out)
    out = _USER_DIR.sub(lambda m: m.group(1) + "<user>", out)
    return out


def read_text_no_follow(path: str, encoding: str = "utf-8") -> str:
    """Read a file, refusing to follow a link at `path`.

    Used for the elevation cookie/token: those live at fixed names in %TEMP%,
    which any same-user process can pre-create. A link there would feed
    attacker-chosen text into the wait loop, so it is refused (the caller
    treats the refusal like any other unreadable file).
    """
    if os.path.islink(path):
        raise OSError("refusing to read through a link: %s" % path)
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags)
    with os.fdopen(fd, "r", encoding=encoding) as fh:
        return fh.read()


def append_text_no_follow(path: str, text: str, encoding: str = "utf-8",
                          max_bytes: int = _TRAIL_MAX_BYTES) -> None:
    """Append one chunk to a debug trail, bounded and link-safe.

    The trail files are diagnostics, so they are capped (oldest lines go) and
    written with ``O_NOFOLLOW`` so a planted link cannot redirect the append
    into another file the user can write. Raises on failure; every caller
    handles that already.
    """
    if os.path.islink(path):
        raise OSError("refusing to append through a link: %s" % path)
    with file_lock(path):
        flags = (os.O_WRONLY | os.O_CREAT | os.O_APPEND
                 | getattr(os, "O_NOFOLLOW", 0))
        fd = os.open(path, flags, 0o600)
        try:
            os.write(fd, str(text).encode(encoding, "replace"))
        finally:
            os.close(fd)
        try:
            if max_bytes and os.path.getsize(path) > max_bytes:
                _trim_to_tail(path, encoding, max_bytes)
        except OSError:
            pass


@contextlib.contextmanager
def file_lock(path: str, timeout: float = 2.0, lock_suffix: str = ".lock"):
    """An advisory lock held ACROSS PROCESSES for `path`, best-effort.

    H-002: the event log was serialized with a threading.Lock, which is enough
    for one process and no help at all for the two that actually write it (the
    GUI and its elevated twin, plus a headless scheduled run). Concurrent
    appends there could interleave half-lines, and the size trim rewrites the
    whole file, so a second writer's line could be dropped entirely.

    Yields True when the lock was taken and False when it could not be
    (unsupported platform, unwritable sidecar, timeout). Callers proceed
    either way: this is observability, and a log line is never worth blocking
    the operation the user asked for. `msvcrt` on Windows, `fcntl` elsewhere.
    """
    lock_path = str(path) + lock_suffix
    try:
        fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    except OSError:
        yield False
        return
    acquired = False
    try:
        deadline = time.monotonic() + max(0.0, timeout)
        while True:
            if _try_lock(fd):
                acquired = True
                break
            if time.monotonic() >= deadline:
                break
            time.sleep(0.01)
        yield acquired
    finally:
        if acquired:
            _release_lock(fd)
        try:
            os.close(fd)
        except OSError:
            pass


def _try_lock(fd: int) -> bool:
    """One non-blocking attempt at an exclusive lock. True if held."""
    try:
        import msvcrt
    except ImportError:
        msvcrt = None
    if msvcrt is not None:
        try:
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            return True
        except OSError:
            return False
    try:
        import fcntl
    except ImportError:
        return True          # no primitive available: caller's own lock only
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except OSError:
        return False


def _release_lock(fd: int) -> None:
    try:
        import msvcrt
    except ImportError:
        msvcrt = None
    try:
        if msvcrt is not None:
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(fd, fcntl.LOCK_UN)
    except OSError:
        pass


def _trim_to_tail(path: str, encoding: str, max_bytes: int) -> None:
    """Rewrite `path` with its last lines, atomically, in place."""
    try:
        with open(path, "r", encoding=encoding, errors="ignore") as fh:
            lines = fh.readlines()
    except OSError:
        return
    keep = lines[-_TRAIL_KEEP_LINES:]
    if len(keep) == len(lines) and sum(len(x) for x in keep) <= max_bytes:
        return
    tmp = str(path) + ".trim.tmp"
    with open(tmp, "w", encoding=encoding) as fh:
        fh.writelines(keep)
        try:
            fh.flush()
            os.fsync(fh.fileno())
        except OSError:
            pass
    os.replace(tmp, path)
