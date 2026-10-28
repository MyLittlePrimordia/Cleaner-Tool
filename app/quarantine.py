"""Opt-in Quarantine bin for the Clean funnel (roadmap 2.5).

When "Quarantine deleted files" is on, the single destructive funnel
(`utils.clean_folder_contents`, used by the Clean tab, the Games tab and three
Repair tasks) MOVES matched files into a quarantine folder instead of deleting
them, so a mistaken clean can be undone from Settings.

It is OFF by default: with it off, nothing here runs and every existing number
and behaviour is unchanged.

Only *interactive* runs attach a bin. The headless scheduler (`--auto-clean`)
still deletes as before — an unattended run should not quietly fill the drive —
and the size cap bounds the whole bin, so even interactive runs stop
quarantining (and delete instead) once the bin is full.

Honest note about the free-space counter: a quarantined file still occupies
disk. While the bin holds files, a run's "freed" total therefore counts bytes
that were moved *aside*, not reclaimed — the per-folder log line says so, the
Settings section shows the bin's size, and emptying the bin reclaims it. A size
cap keeps the bin from filling the drive: past the cap, files are deleted as
before (and that is logged).

Layout under <root> (%LOCALAPPDATA%\\CleanerTool\\Quarantine, with a temp-dir
fallback):

    manifest.jsonl       one JSON line per quarantined file
    files/<hash>_<name>  the moved files
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import sys
import tempfile
import threading
import contextlib
import time
from typing import Callable, List, Optional, Tuple

#: Keep at most this much in the bin before falling back to real deletes.
DEFAULT_MAX_BYTES = 2 * 1024 ** 3  # 2 GB

_LOCK = threading.Lock()

# H-024: in-process guard + reentrancy counter for the cross-process lock,
# mirroring config_persist._config_file_lock. The threading.Lock alone only
# serialises threads inside ONE process; the GUI and a headless run (or a
# future path that attaches a bin to headless runs) both append to and rewrite
# the SAME manifest.jsonl, so two interleaved writers could interleave JSONL
# lines or truncate mid-rewrite and destroy restore records.
_manifest_lock_guard = threading.RLock()
_manifest_lock_depth = 0


@contextlib.contextmanager
def _manifest_file_lock(root: str):
    """Cross-process advisory lock around manifest read-modify-write (H-024).

    Fails open: a locking failure never blocks the bin (the file is still
    moved and each write is small), it only leaves the rare cross-process
    lost-update window unguarded.
    """
    global _manifest_lock_depth
    with _manifest_lock_guard:
        if _manifest_lock_depth > 0:
            _manifest_lock_depth += 1
            try:
                yield
            finally:
                _manifest_lock_depth -= 1
            return
        handle = None
        locked = False
        try:
            os.makedirs(root, exist_ok=True)
            lock_path = _manifest_path(root) + ".lock"
            handle = open(lock_path, "a+")
            if sys.platform.startswith("win"):
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
            locked = True
        except Exception as exc:
            from app.utils import log_or_swallow
            log_or_swallow(None, "quarantine.manifest_lock", exc)
            locked = False
        _manifest_lock_depth = 1
        try:
            yield
        finally:
            _manifest_lock_depth = 0
            if handle is not None:
                try:
                    if locked and sys.platform.startswith("win"):
                        import msvcrt
                        handle.seek(0)
                        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                except Exception:
                    pass
                try:
                    handle.close()
                except Exception:
                    pass


def default_root() -> str:
    base = os.environ.get("LOCALAPPDATA") or tempfile.gettempdir()
    return os.path.join(base, "CleanerTool", "Quarantine")


def enabled() -> bool:
    """Read the opt-in flag (missing/unreadable config => off, the safe default)."""
    try:
        from app.config_persist import load_config
        return bool(load_config().get("quarantine_enabled", False))
    except Exception as exc:
        from app.utils import log_or_swallow
        log_or_swallow(None, "quarantine.enabled", exc)
        return False


def _stored_name(path: str, salt: str = "") -> str:
    digest = hashlib.sha1((path + salt).encode("utf-8", "replace")).hexdigest()[:20]
    return digest + "_" + os.path.basename(path)


def _manifest_path(root: str) -> str:
    return os.path.join(root, "manifest.jsonl")


def _files_dir(root: str) -> str:
    return os.path.join(root, "files")


# --------------------------------------------------------------------------- #
# Per-run store
# --------------------------------------------------------------------------- #

class Quarantine:
    """One run's view of the bin. Cheap; safe to create per run.

    `stash(path)` moves a file in and returns True on success; returns False when
    the file cannot be moved OR the size cap would be exceeded (the caller then
    deletes as usual, so "Clean" still cleans)."""

    def __init__(self, root: Optional[str] = None,
                 max_bytes: int = DEFAULT_MAX_BYTES):
        self.root = root or default_root()
        self.max_bytes = int(max_bytes)
        self.moved_files = 0
        self.moved_bytes = 0
        self.refused = 0
        # The cap bounds the WHOLE bin, not just this run: start from what is
        # already held so repeated cleans cannot creep past the limit.
        try:
            self._bin_bytes = stats(self.root)[1]
        except Exception:
            self._bin_bytes = 0

    @property
    def manifest_path(self) -> str:
        return _manifest_path(self.root)

    @property
    def files_dir(self) -> str:
        return _files_dir(self.root)

    def stash(self, path: str) -> bool:
        try:
            size = os.path.getsize(path)
        except OSError:
            return False
        if self.max_bytes and (self._bin_bytes + size) > self.max_bytes:
            self.refused += 1
            return False
        name = _stored_name(path)
        dest = os.path.join(self.files_dir, name)
        try:
            os.makedirs(self.files_dir, exist_ok=True)
            if os.path.exists(dest):
                name = _stored_name(path, salt=repr(time.time()))
                dest = os.path.join(self.files_dir, name)
            try:
                os.replace(path, dest)
            except OSError:
                shutil.move(path, dest)   # cross-volume
        except Exception:
            return False
        try:
            with _LOCK:
                with _manifest_file_lock(self.root):
                    with open(self.manifest_path, "a", encoding="utf-8") as fh:
                        fh.write(json.dumps({
                            "src": os.path.abspath(path),
                            "dst": name,
                            "size": size,
                            "ts": time.time(),
                        }) + "\n")
        except Exception:
            # the file is safely moved; a lost manifest line only costs a
            # restore, never data — keep the run going.
            pass
        self.moved_files += 1
        self.moved_bytes += size
        self._bin_bytes += size
        return True


# --------------------------------------------------------------------------- #
# Bin-wide operations (Settings)
# --------------------------------------------------------------------------- #

def list_entries(root: Optional[str] = None) -> List[dict]:
    root = root or default_root()
    out: List[dict] = []
    try:
        with open(_manifest_path(root), "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                if isinstance(rec, dict) and rec.get("src") and rec.get("dst"):
                    out.append(rec)
    except OSError:
        pass
    return out


def _write_manifest(root: str, entries: List[dict]) -> None:
    try:
        with _LOCK:
            with _manifest_file_lock(root):
                if not entries:
                    try:
                        os.remove(_manifest_path(root))
                    except OSError:
                        pass
                    return
                with open(_manifest_path(root), "w", encoding="utf-8") as fh:
                    for rec in entries:
                        fh.write(json.dumps(rec) + "\n")
    except OSError:
        pass


def split_reclaimed(total_bytes: int, moved_bytes: int = 0) -> Tuple[int, int]:
    """(actually reclaimed, moved aside) for an honest run total.

    A file moved into the Quarantine bin still occupies disk. The task
    functions count it in their own totals (they measure what left the target
    folder), so the run-level number must subtract what was only relocated -
    otherwise the scorecard, toast and run history report space that was
    never freed. Clamped, and tolerant of junk input.
    """
    try:
        total = max(0, int(total_bytes or 0))
    except (TypeError, ValueError):
        total = 0
    try:
        moved = max(0, int(moved_bytes or 0))
    except (TypeError, ValueError):
        moved = 0
    moved = min(moved, total)
    return total - moved, moved


def stats(root: Optional[str] = None) -> Tuple[int, int]:
    """(file count, total bytes) currently in the bin, walked from disk so it
    stays truthful even if the manifest was lost."""
    root = root or default_root()
    count = 0
    total = 0
    for base, _dirs, names in os.walk(_files_dir(root)):
        for n in names:
            try:
                total += os.path.getsize(os.path.join(base, n))
                count += 1
            except OSError:
                pass
    return count, total


def retention_days() -> int:
    """How long the bin keeps a file, in days (0 = until the user empties it).

    Validated against the schema's own set rather than trusted: a hand-edited
    ``3650`` would otherwise mean "ten years" while Settings shows the first
    option, which is the one shape of setting that must never guess.
    """
    try:
        from app.config_persist import load_config
        raw = load_config().get("quarantine_days", 0)
    except Exception as exc:
        from app.utils import log_or_swallow
        log_or_swallow(None, "quarantine.retention_days", exc)
        return 0
    try:
        from app.config_schema import QUARANTINE_DAYS
    except Exception:
        return 0
    if isinstance(raw, bool) or not isinstance(raw, int) or raw not in QUARANTINE_DAYS:
        return 0
    return raw


def _bare_name(name) -> str:
    """A stored name, or "" when it could point outside the bin."""
    name = str(name or "")
    if not name or name in (".", ".."):
        return ""
    if "/" in name or "\\" in name or ":" in name:
        return ""
    return name if os.path.basename(name) == name else ""


def _entry_ts(entry: dict) -> float:
    try:
        return float(entry.get("ts") or 0)
    except (TypeError, ValueError):
        return 0.0


def oldest_days(root: Optional[str] = None, now: Optional[float] = None) -> Optional[int]:
    """Age of the oldest thing in the bin, in whole days (None when empty).

    Reads the filesystem as well as the manifest: the Settings line has to be
    right even when a manifest line was lost, which is the same reason
    :func:`stats` walks the folder.
    """
    root = root or default_root()
    now = time.time() if now is None else float(now)
    stamps = [_entry_ts(e) for e in list_entries(root)]
    files_dir = _files_dir(root)
    try:
        for name in os.listdir(files_dir):
            try:
                stamps.append(os.path.getmtime(os.path.join(files_dir, name)))
            except OSError:
                pass
    except OSError:
        pass
    stamps = [s for s in stamps if s > 0]
    if not stamps:
        return None
    return max(0, int((now - min(stamps)) // 86400))


def expire(root: Optional[str] = None, days: Optional[int] = None,
           now: Optional[float] = None) -> Tuple[int, int]:
    """Delete bin entries older than the retention window. (files, bytes).

    Called at launch, never mid-session: shortening the window in Settings
    must not delete files the moment it is saved.

    Only ever touches files the bin itself holds - entry names must be bare
    filenames, exactly as in :func:`restore_all`, so a forged ``dst`` cannot
    turn retention into a delete-anything primitive. A row with no timestamp
    is kept (never guess an age), a file that cannot be deleted keeps its row
    so a lock is retried next launch, and `days` 0 reads the saved setting and
    does nothing.
    """
    root = root or default_root()
    if days is None:
        days = retention_days()
    try:
        days = int(days)
    except (TypeError, ValueError):
        days = 0
    if days <= 0:
        return 0, 0
    now = time.time() if now is None else float(now)
    cutoff = now - days * 86400
    files_dir = _files_dir(root)
    entries = list_entries(root)
    kept: List[dict] = []
    removed = 0
    freed = 0
    for entry in entries:
        name = _bare_name(entry.get("dst"))
        stamp = _entry_ts(entry)
        if not name or not stamp or stamp >= cutoff:
            kept.append(entry)
            continue
        path = os.path.join(files_dir, name)
        try:
            size = os.path.getsize(path)
        except OSError:
            continue                     # file already gone: drop the row
        try:
            os.remove(path)
        except OSError:
            kept.append(entry)           # locked: try again next launch
            continue
        removed += 1
        freed += size
    # Files a lost manifest line no longer accounts for: age comes from the
    # filesystem, which is the only thing left to ask.
    referenced = {_bare_name(e.get("dst")) for e in kept}
    try:
        stray = os.listdir(files_dir)
    except OSError:
        stray = []
    for name in stray:
        if name in referenced or not _bare_name(name):
            continue
        path = os.path.join(files_dir, name)
        try:
            if os.path.getmtime(path) >= cutoff:
                continue
            size = os.path.getsize(path)
            os.remove(path)
        except OSError:
            continue
        removed += 1
        freed += size
    if removed or len(kept) != len(entries):
        _write_manifest(root, kept)
    return removed, freed


def _entry_is_confined(root: str, entry: dict) -> bool:
    """True when a manifest row can be acted on without leaving the sandbox.

    The manifest is a plaintext file in the user's profile that anything can
    edit, and Restore All is a WRITE: a forged `dst` of `..\\..\\x.dll` would
    read and write outside the bin, and a forged `src` would drop a file into
    any folder on the machine. A row is only honoured when its stored name is
    a bare filename inside the bin and its original path is a normal absolute
    path that is neither inside the bin nor a protected root.
    """
    name = str(entry.get("dst") or "")
    src = str(entry.get("src") or "")
    if not name or not src:
        return False
    # dst: a bare filename, nothing else (no separators, no drive, no ..)
    if name in (".", "..") or "/" in name or "\\" in name or ":" in name:
        return False
    if os.path.basename(name) != name:
        return False
    try:
        target = os.path.normcase(os.path.abspath(src))
        bin_root = os.path.normcase(os.path.abspath(root))
        bin_files = os.path.normcase(os.path.abspath(_files_dir(root)))
    except Exception:
        return False
    if not os.path.isabs(src):
        return False
    if ".." in str(src).replace("/", "\\").split("\\"):
        return False
    for inside in (bin_root, bin_files):
        if target == inside or target.startswith(inside + os.sep):
            return False
    from app.utils import _is_protected_root
    return not _is_protected_root(target)


def restore_all(root: Optional[str] = None,
                on_progress: Optional[Callable[[int, int], None]] = None
                ) -> Tuple[int, int]:
    """Move every quarantined file back to where it came from.

    Never overwrites an existing file at the original location (that is treated
    as already-restored/skipped) and never raises. Returns (restored, failed)."""
    root = root or default_root()
    entries = list_entries(root)
    files_dir = _files_dir(root)
    restored = 0
    failed: List[dict] = []
    total = len(entries)
    for i, e in enumerate(reversed(entries)):
        src = e.get("src")
        name = e.get("dst")
        if not src or not name:
            continue
        if not _entry_is_confined(root, e):
            failed.append(e)          # unreadable/forged row: keep it, never act
            continue
        dst = os.path.join(files_dir, name)
        if not os.path.exists(dst):
            continue
        if os.path.exists(src):
            failed.append(e)          # do not clobber whatever is there now
            continue
        try:
            parent = os.path.dirname(src)
            if parent:
                os.makedirs(parent, exist_ok=True)
            try:
                os.replace(dst, src)
            except OSError:
                shutil.move(dst, src)
            restored += 1
        except Exception:
            failed.append(e)
        if on_progress is not None:
            try:
                on_progress(i + 1, total)
            except Exception:
                pass
    _write_manifest(root, failed)
    return restored, len(failed)


def empty(root: Optional[str] = None) -> Tuple[int, int]:
    """Permanently delete everything in the bin. Returns (files, bytes) freed."""
    root = root or default_root()
    count, total = stats(root)
    try:
        shutil.rmtree(root, ignore_errors=True)
    except Exception:
        pass
    return count, total
