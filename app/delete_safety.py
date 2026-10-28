"""The one destructive funnel, with every guard that makes it safe.

R-009: this used to live in `app/utils.py`, a 1,800-line grab bag where the
highest-risk function in the app (the single delete path behind Clean, Games
and three Repair tasks) sat between process helpers and `format_bytes`. The
guards are load-bearing and independent of each other:

  * protected container roots are refused outright (BUG-014),
  * a reparse point - junction, directory symlink - is refused at the root,
    pruned from every walk, and re-checked immediately before each removal
    (F03/F5-2: `os.walk(followlinks=False)` does NOT treat Windows junctions
    as links, and a junction planted between enumeration and `os.remove`
    redirected the delete into its target - verified empirically),
  * nothing the running app depends on is touched (`_own_runtime_*`: a
    PyInstaller build unpacks into %TEMP% and was deleting itself),
  * cancellation is polled per directory AND per file,
  * an optional Quarantine bin moves files instead of deleting them.

They now have their own module and their own test file. `app.utils` re-exports
every name here, so no import path changed.

Import direction: this module does NOT import app.utils at module load (utils
re-exports from here, which would close a cycle). `known_folder` - still a
utils helper, used by half a dozen other modules - is imported inside
`_is_protected_root`, where it is one dict lookup per call.
"""
from __future__ import annotations

import os
import sys
from typing import TYPE_CHECKING, List, Optional

if TYPE_CHECKING:                      # annotation only - never imported at runtime
    from app.utils import TaskContext

#: Same rule `app.utils.IS_WINDOWS` spells; duplicated rather than imported so
#: this leaf module can be imported BY utils without a cycle.
IS_WINDOWS = sys.platform.startswith("win")

if IS_WINDOWS:
    import ctypes
else:  # pragma: no cover
    ctypes = None  # type: ignore


# FILE_ATTRIBUTE_REPARSE_POINT — junctions & directory symlinks. os.walk
# does NOT treat Windows junctions as links (followlinks=False only guards
# POSIX symlinks), so a junction planted in a cleaned folder would redirect
# os.remove THROUGH it into arbitrary targets (verified empirically in the
# audit: a canary file behind a junction was deleted). Everything that
# walks/deletes must skip reparse points.
_FILE_ATTRIBUTE_REPARSE_POINT = 0x400


def _is_reparse_point(path: str) -> bool:
    """True if path is a junction / directory symlink / other reparse point.

    Fail-closed: any error (INVALID_FILE_ATTRIBUTES, access denied, race)
    is treated as a reparse point so cleaners refuse to walk/delete rather
    than risk following a planted junction. Callers already log and skip.
    """
    if not IS_WINDOWS or ctypes is None:
        # POSIX: os.walk(followlinks=False) already skips symlinks for us
        return os.path.islink(path)
    try:
        attrs = ctypes.windll.kernel32.GetFileAttributesW(str(path))
        if attrs == 0xFFFFFFFF:  # INVALID_FILE_ATTRIBUTES — fail closed
            return True
        return bool(attrs & _FILE_ATTRIBUTE_REPARSE_POINT)
    except Exception:
        return True  # fail closed on any API/race error


# Environment roots that must never be emptied. normcase'd env lookups, so
# the caller does not need to trust how the value was spelled.
#
# TEMP/TMP are deliberately NOT here. %TEMP% is a legitimate cleaning target
# by design -- clean_tasks.clean_user_temp_files passes it BARE (and so does
# the disk-cleanup fallback at clean_tasks.py:886), and it is part of the
# Quick/Deep Clean presets. It is scratch space whose entire purpose is to be
# emptied, it holds no user data, and protecting it would silently free 0
# bytes for a headline feature. Verified: %TEMP% bare is the ONLY protected
# container any task passes unextended; everything else is a subfolder.
_PROTECTED_ROOT_ENV = (
    "SystemRoot", "windir", "ProgramData", "ProgramFiles",
    "ProgramFiles(x86)", "ProgramW6432", "USERPROFILE", "LOCALAPPDATA",
    "APPDATA", "PUBLIC",
)

# Well-known user data folders as (User Shell Folders value name, path
# relative to %USERPROFILE%). BOTH are checked: the registry value honors
# OneDrive / known-folder redirection, and the relative path is the fallback
# for SKUs where the value is absent. That fallback is not theoretical --
# known_folder() returns "" for MyPictures/MyVideo/MyMusic on this machine, so
# a registry-only check would have left Pictures, Videos and Music UNPROTECTED.
# The Downloads entry is a GUID because that is how User Shell Folders spells
# it; Favorites is the legacy GUID.
_PROTECTED_ROOT_SHELL = (
    ("Desktop", "Desktop"),
    ("Personal", "Documents"),
    ("MyPictures", "Pictures"),
    ("MyVideo", "Videos"),
    ("MyMusic", "Music"),
    ("{374DE290-123F-4565-9164-39C4925E467B}", "Downloads"),   # Downloads
    ("{645FF040-5081-101B-9F08-00AA002F954E}", "Favorites"),   # legacy
)


def _norm_dir(path: str) -> str:
    """Absolute + normalized + case-folded directory key for comparison."""
    try:
        return os.path.normcase(os.path.abspath(os.path.normpath(path)))
    except Exception:
        return ""


def _is_protected_root(path: str) -> bool:
    """True when `path` IS a protected container root (BUG-014).

    Also refuses a bare drive root ("C:\\", "D:\\"). No caller should ever pass
    one, and if one arrives it is certainly not a cache.

    Deliberately does NOT walk up the tree. Refusing an ANCESTOR of a
    protected root is already covered, because every dangerous ancestor is
    itself in the list above: C:\\ , C:\\Windows, C:\\Users, C:\\Program Files,
    C:\\ProgramData, and the user profile.

    Fails CLOSED (returns True on any error), matching _is_reparse_point's
    posture — an unanswerable question about a delete target must stop the
    delete, not permit it."""
    try:
        if not path or not isinstance(path, str):
            return False
        norm = _norm_dir(path)
        if not norm:
            return False
        drive, rest = os.path.splitdrive(norm)
        if drive and not rest.strip("\\"):
            return True   # bare drive root
        for var in _PROTECTED_ROOT_ENV:
            raw = os.environ.get(var, "")
            if raw and norm == _norm_dir(raw):
                return True
        from app.utils import known_folder   # lazy: see the module docstring
        for shell, relative in _PROTECTED_ROOT_SHELL:
            raw = known_folder(shell)
            if raw and norm == _norm_dir(raw):
                return True
            prof = os.environ.get("USERPROFILE", "")
            if prof and relative:
                cand = os.path.join(prof, relative)
                if norm == _norm_dir(cand):
                    return True
        # the whole per-user tree: C:\Users (and D:\Users, etc.) holds every
        # local account's profile, so no single env var covers it.
        try:
            _d, tail = os.path.splitdrive(norm)
            if tail.strip("\\").lower() == "users":
                return True
        except Exception:
            pass
        return False
    except Exception:
        return True


def _own_runtime_roots() -> tuple:
    """Folders this running app depends on, which a cleaner must never touch.

    A PyInstaller one-file build unpacks itself into %TEMP%\\_MEIxxxxx and
    loads its assets and lazily-imported modules from there for as long as it
    runs. The temp-file cleaners walk %TEMP%; Windows keeps the files it has
    open locked (so they were skipped), but everything it had NOT opened yet
    was deleted out from under the running program. The next import or icon
    load after a Deep Clean then failed and the window vanished - consistent
    with a Deep Clean that "just closed the app". Nothing the app is running
    from, and nothing it saves to, is junk.
    """
    roots = []
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        roots.append(meipass)
    if getattr(sys, "frozen", False):
        try:
            roots.append(os.path.dirname(sys.executable))
        except Exception:
            pass
    try:
        from app.config_paths import CONFIG_DIR
        roots.append(str(CONFIG_DIR))             # config, logs, backups
    except Exception:
        pass
    out = []
    for r in roots:
        try:
            out.append(os.path.normcase(os.path.abspath(r)))
        except Exception:
            continue
    return tuple(out)


def _is_own_runtime(path: str, roots: Optional[tuple] = None) -> bool:
    roots = _own_runtime_roots() if roots is None else roots
    try:
        p = os.path.normcase(os.path.abspath(path))
    except Exception:
        return False
    return any(p == r or p.startswith(r + os.sep) for r in roots)


def clean_folder_contents(ctx: "TaskContext", folder_path: str, remove_root: bool = False,
                           extensions: Optional[List[str]] = None) -> int:
    bytes_freed = 0
    skipped = 0
    if not folder_path or not os.path.exists(folder_path):
        return 0
    # BUG-014: refuse protected container roots before touching anything.
    # Placed ahead of the reparse check so the refusal is unconditional, and
    # honored in dry_run too — a folder we would never clean has no
    # meaningful size to estimate.
    if _is_protected_root(folder_path):
        try:
            ctx.log(f"  ! refusing to clean a protected location: {folder_path}")
        except Exception:
            pass
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
    # 2.5: opt-in Quarantine bin. When present, files are MOVED there instead
    # of deleted (see the file loop below). None = ordinary deletes.
    quarantine = getattr(ctx, "quarantine", None)
    moved_to_quarantine = 0

    # Security guard (audit HIGH finding): os.walk(topdown=False) cannot
    # prune and does not treat Windows junctions as links — a junction
    # anywhere under folder_path redirected deletions into its target
    # (empirically verified). Walk top-down and skip reparse points.
    stopped = False
    _own = _own_runtime_roots()       # never delete what we are running from
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
        dirs[:] = [d for d in dirs if not _is_reparse_point(os.path.join(root, d))
                   and not _is_own_runtime(os.path.join(root, d), _own)]
        for f in files:
            if extensions and not any(f.lower().endswith(e.lower()) for e in extensions):
                continue
            filepath = os.path.join(root, f)
            if _is_reparse_point(filepath) or _is_own_runtime(filepath, _own):
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
                    # 2.5: with the Quarantine bin active, move the file there
                    # rather than deleting it. stash() returns False when it
                    # cannot take the file (size cap, or a move error), and we
                    # then delete as normal so the clean still cleans.
                    if quarantine is not None and quarantine.stash(filepath):
                        moved_to_quarantine += 1
                    else:
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
            # U-007: the per-folder line explains one folder; the run needs
            # the total, because "freed less than the estimate" is almost
            # always this and the summary is where a user reads the result.
            # A dry pass counts unreadable files, not held ones, so it is not
            # added to the locked total.
            try:
                ctx.skipped_locked = int(getattr(ctx, "skipped_locked", 0)) \
                    + int(skipped)
            except Exception:
                pass
    if moved_to_quarantine and not dry:
        # Honesty: these bytes were moved, not reclaimed. Say so, and say how
        # to actually get the space back, instead of letting the "freed"
        # counter imply the disk is lighter than it is.
        try:
            ctx.log(f"  moved {moved_to_quarantine} file(s) under {folder_path} "
                    "to the Quarantine bin — empty it in Settings to reclaim "
                    "the space")
        except Exception:
            pass
    return bytes_freed
