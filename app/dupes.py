"""Duplicate and large-file finder. READ-ONLY scan; deletes go to the Recycle Bin.

Tk-free by design (matches app/netconn.py, app/svcs.py, app/filelocks.py):
this is a sampler, the dialog that shows it lives in app/ui/dialogs/.

The safety position, up front
-----------------------------
This is the only Tools popup that can destroy data, so three decisions are
load-bearing and none of them are negotiable:

1. NOTHING IS DELETED BY THE SCAN. A scan only ever reads. The dialog opens
   with every group collapsed and no deletion armed.

2. DELETION GOES TO THE RECYCLE BIN, never a hard unlink. Verified on this
   machine with SHFileOperationW + FOF_ALLOWUNDO: the file disappears from
   its folder and a `$R...` entry appears under C:\\$Recycle.Bin with the
   original bytes intact. "I deleted the wrong duplicate" becomes a two-click
   undo instead of a bad afternoon. This is the single most valuable thing a
   duplicates tool can do for someone who does not understand what it found.

3. THE SAFETY RULES ARE NOT REINVENTED HERE. The walk and the delete guard
   come from app/infra/fs_safe.py (`safe_walk`, `is_safe_to_delete`), which
   is the single place those rules live. BUG-014 exists because two copies of
   "is this safe to delete" drifted apart; adding a third would repeat it.

How a duplicate is proven
-------------------------
Two files are only ever called duplicates when they have the SAME SIZE and
the SAME SHA-256 of their contents. Same name is not evidence and same size
alone is not evidence - a 4 KB config and a 4 KB keyring fragment collide by
size constantly. Files are grouped by size first and only hashed inside a
group, so a folder of unique files costs almost no I/O.

Scan bounds are reported, never silently applied. Hitting the file cap or the
byte cap sets a flag the dialog shows, because "no duplicates found" and
"found none in the first 20,000 files" are very different statements.
"""
from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import hashlib
import os
import sys

from app.infra.fs_safe import is_safe_to_delete, safe_walk

IS_WINDOWS = sys.platform.startswith("win")

#: Files smaller than this are ignored. Duplicates of tiny files are almost
#: always config fragments where several copies are correct, and offering to
#: delete them is noise at best.
MIN_DUPE_SIZE = 64 * 1024

#: Scan bounds. Both are reported when hit, never applied silently.
MAX_FILES = 40000
MAX_HASH_BYTES = 6 * 1024 * 1024 * 1024      # 6 GB of actual hashing
HASH_CHUNK = 1024 * 1024

#: How many of each to report. Enough to be useful, few enough to read.
TOP_GROUPS = 60
TOP_LARGE = 40

_CHUNK_CACHE = {}


class _SHFILEOPSTRUCTW(ctypes.Structure):
    _fields_ = [
        ("hwnd", wt.HWND), ("wFunc", wt.UINT),
        ("pFrom", wt.LPCWSTR), ("pTo", wt.LPWSTR),
        ("fFlags", ctypes.c_ushort),
        ("fAnyOperationsAborted", wt.BOOL),
        ("hNameMappings", ctypes.c_void_p),
        ("lpszProgressTitle", wt.LPCWSTR),
    ]


_FO_DELETE = 0x0003
_FOF_ALLOWUNDO = 0x0040
_FOF_NOCONFIRMATION = 0x0010
_FOF_SILENT = 0x0004
_FOF_NOERRORUI = 0x0400


def sha256(path: str):
    """File digest, or None when it cannot be read (locked, gone, denied).

    A file that cannot be hashed is skipped, never guessed: an unreadable file
    must not be able to become a "duplicate" of something.
    """
    try:
        st = os.stat(path)
    except OSError:
        return None
    key = (st.st_size, int(st.st_mtime), int(st.st_ino))
    hit = _CHUNK_CACHE.get(key)
    if hit is not None:
        return hit
    h = hashlib.sha256()
    try:
        with open(path, "rb") as fh:
            while True:
                b = fh.read(HASH_CHUNK)
                if not b:
                    break
                h.update(b)
    except OSError:
        return None
    digest = h.hexdigest()
    if len(_CHUNK_CACHE) > 4096:
        _CHUNK_CACHE.clear()
    _CHUNK_CACHE[key] = digest
    return digest


def _collect(root: str, min_size: int, max_files: int, progress=None,
             extensions=None):
    """(reps, truncated) - one entry per REAL file, hardlinks collapsed.

    BUG-038: this used to return every path, so three links to one file were
    reported as three identical copies. Measured on a folder holding one file
    hardlinked three times plus one genuine copy: the tool claimed 4 copies
    and 600 KB reclaimable, when the truth was 2 files and 200 KB.

    That is not only a wrong number. Deleting two of the three links frees
    NOTHING - the inode survives with nlink=1 - so the tool would report
    success and success that never happened, while the copy it told you to
    keep is the one that actually mattered.

    A hardlinked pair is one file with several names, so identity is
    (st_dev, st_ino). `st_ino` is 0 on some filesystems (network shares, some
    FAT volumes); there it is not trustworthy, so those files are NOT
    collapsed - a false duplicate is better than a missed one.

    `progress`, when given, is called as progress(seen) every 256 files so a
    multi-minute walk can say where it is. Without it the UI showed one
    frozen line of text for the whole scan, which reads as a hang.
    `extensions`, when given, is a set of lower-case suffixes with the dot
    (" .jpg"); anything else is skipped.
    """
    reps = []
    by_inode = {}
    truncated = False
    seen = 0
    try:
        for dirpath, _dirs, names in safe_walk(root):
            for n in names:
                if seen >= max_files:
                    return reps, True
                seen += 1
                if progress is not None and seen % 256 == 0:
                    progress(seen)
                if extensions is not None:
                    ext = os.path.splitext(n)[1].lower()
                    if ext not in extensions:
                        continue
                p = os.path.join(dirpath, n)
                try:
                    if os.path.islink(p) or not os.path.isfile(p):
                        continue
                    st = os.stat(p)
                    if st.st_size < min_size:
                        continue
                except OSError:
                    continue
                entry = {"path": p, "size": st.st_size, "mtime": st.st_mtime,
                         "ino": int(getattr(st, "st_ino", 0) or 0),
                         "dev": int(getattr(st, "st_dev", 0) or 0),
                         "nlink": int(getattr(st, "st_nlink", 1) or 1),
                         "aliases": []}
                if entry["ino"]:
                    key = (entry["dev"], entry["ino"])
                    first = by_inode.get(key)
                    if first is not None:
                        # Another name for a file we already have. Keep the
                        # SHORTEST name as the one we display: directory
                        # enumeration order decided this before, so the tool
                        # would show "link1.dat" as the file to keep while
                        # "original.dat" sat in the alias list. All names
                        # share one inode, so size and mtime cannot break the
                        # tie - path length is the only honest signal there is.
                        first["aliases"].append(p)
                        first["nlink"] = max(first["nlink"], entry["nlink"])
                        if len(p) < len(first["path"]):
                            first["aliases"].append(first["path"])
                            first["path"] = p
                        continue
                    by_inode[key] = entry
                reps.append(entry)
    except Exception:
        return reps, truncated
    return reps, truncated


#: The size steps the dialog's filter offers. The floor is the historical
#: MIN_DUPE_SIZE, so the default view is unchanged; everything above it is
#: the control every other duplicate finder has and this one lacked.
SIZE_STEPS = (
    ("64 KB - anything", 64 * 1024),
    ("1 MB and up", 1024 * 1024),
    ("10 MB and up", 10 * 1024 * 1024),
    ("100 MB and up", 100 * 1024 * 1024),
)


def keep_candidate(copies):
    """Which copy to keep. Must be explainable in one sentence.

    Shortest path first: for a file duplicated across a program folder, a
    documents folder and Downloads, the short path is nearly always the
    original install rather than the copy someone made. Ties break on oldest
    modified time, which is the same intuition.

    Returns (index_into_copies, reason).
    """
    if not copies:
        return 0, "nothing to keep"
    # M-1 fix: measure the PATH length, not len(dict) — the old key was the
    # record's key COUNT (a constant), so the shortest-path rule never ran and
    # the "oldest copy" tie-break decided everything regardless of path.
    order = sorted(
        range(len(copies)),
        key=lambda i: (len(copies[i]["path"]), -copies[i]["mtime"], copies[i]["path"]))
    best = order[0]
    same = [i for i in order
            if len(copies[i]["path"]) == len(copies[best]["path"])]
    if len(same) > 1:
        best = min(same, key=lambda i: copies[i]["mtime"])
        reason = "oldest of the copies with the shortest path"
    else:
        reason = "shortest path"
    return best, reason


def scan(roots, min_size: int = MIN_DUPE_SIZE, max_files: int = MAX_FILES,
         top_large: int = TOP_LARGE, should_stop=None, progress=None,
         extensions=None) -> dict:
    """Find duplicates and large files under `roots`.

    Read-only. Returns {"groups", "large", "stats", "error", "all_groups"}.

    `progress` is called as progress(phase, done, total) where phase is one of
    "walking" / "hashing"; the UI uses it to show a real bar instead of one
    frozen line for a multi-minute scan.

    `stats` reports the TRUE totals alongside the truncated lists
    (`groups`, `large_total`), so the caller can say "+N more" rather than
    silently showing a shorter list than it found.
    """
    if not IS_WINDOWS:
        return {"groups": [], "large": [], "stats": {}, "error":
                "This scan is Windows-only."}
    if isinstance(roots, str):
        roots = [roots]
    roots = [r for r in (roots or []) if r and os.path.exists(r)]
    if not roots:
        return {"groups": [], "large": [], "stats": {},
                "error": "Choose a folder to look in."}

    all_files = []
    truncated = False
    hardlink_groups = 0
    for r in roots:
        if should_stop is not None and should_stop():
            truncated = True
            break
        found, t = _collect(r, min_size, max_files - len(all_files),
                            progress=(lambda n: progress("walking", n, 0))
                            if progress else None,
                            extensions=extensions)
        hardlink_groups += len([f for f in found if f["aliases"]])
        all_files.extend(found)
        if t:
            truncated = True
            break

    # Hardlinks already collapsed by _collect, so every entry here is a real,
    # separate file. Sizes and digests below are therefore honest.
    info = all_files

    # Large files first: cheap, and useful even when hashing is cut short.
    info.sort(key=lambda e: -e["size"])
    large_total = len(info)
    large = [{"path": e["path"], "size": e["size"], "mtime": e["mtime"]}
             for e in info[:top_large]]

    by_size = {}
    for e in info:
        by_size.setdefault(e["size"], []).append(e)

    hashed_bytes = 0
    hash_capped = False
    groups = []
    hashable = sum(len(v) for v in by_size.values() if len(v) > 1)
    done = 0
    for size, entries in by_size.items():
        if len(entries) < 2:
            continue
        if should_stop is not None and should_stop():
            break
        if hashed_bytes + size * len(entries) > MAX_HASH_BYTES:
            hash_capped = True
            continue
        hashed_bytes += size * len(entries)
        by_digest = {}
        for e in entries:
            d = sha256(e["path"])
            done += 1
            if progress is not None and done % 8 == 0:
                progress("hashing", done, hashable)
            if d is None:
                continue
            by_digest.setdefault(d, []).append(e)
        for digest, copies in by_digest.items():
            if len(copies) < 2:
                continue
            keep_i, reason = keep_candidate(copies)
            keep = copies[keep_i]
            for i, c in enumerate(copies):
                c["keep"] = (i == keep_i)
            groups.append({
                "digest": digest,
                "size": size,
                "copies": copies,
                "keep": keep["path"],
                "reason": reason,
                "wasted": size * (len(copies) - 1),
                # Hardlink aliases are recorded but NOT removable: they are
                # other names for a file already in the group.
                "hardlinked": any(c.get("aliases") for c in copies),
            })

    groups.sort(key=lambda g: -g["wasted"])
    total_groups = len(groups)
    stats = {
        "files": len(all_files),
        "roots": len(roots),
        "groups": total_groups,
        "groups_shown": min(TOP_GROUPS, total_groups),
        "large_total": large_total,
        "large_shown": len(large),
        "copies": sum(len(g["copies"]) for g in groups),
        "wasted": sum(g["wasted"] for g in groups),
        "truncated": truncated,
        "hash_capped": hash_capped,
        "hashed_bytes": hashed_bytes,
        "hardlink_groups": hardlink_groups,
        "min_size": min_size,
    }
    if progress is not None:
        progress("hashing", hashable, hashable)
    return {"groups": groups[:TOP_GROUPS], "large": large,
            "all_groups": groups, "stats": stats, "error": None}


def verdict_for(data: dict) -> str:
    """One plain sentence. Never claims the scan was exhaustive."""
    if not data:
        return "Choose a folder to look in."
    if data.get("error"):
        return data["error"]
    st = data.get("stats") or {}
    g = int(st.get("groups") or 0)
    if g == 0:
        if st.get("truncated") or st.get("hash_capped"):
            return ("No duplicates found in the part of the folder that was "
                    "checked.")
        return "No duplicate files found."
    n = int(st.get("copies") or 0) - g
    if g == 1:
        return "1 set of identical files. %d could be removed." % max(0, n)
    return "%d sets of identical files. %d could be removed." % (g, max(0, n))


def removable_from(group: dict, protected=None):
    """The copies a "remove the duplicates" action would send away.

    Derived from the group's own `keep` flags, so the number the dialog
    promises and the files it deletes cannot drift apart.

    `protected` (a list of paths/patterns the user marked never-delete) is
    filtered here as well as in `recycle()`. Hiding a protected file in the
    UI is a courtesy; refusing it in the sampler is the guarantee. Both exist
    because a UI-only filter is one rendering bug away from data loss.
    """
    out = []
    for c in group.get("copies", []):
        if c.get("keep"):
            continue
        p = c.get("path") or ""
        if protected and is_protected(p, protected):
            continue
        out.append(p)
    return out


def promote_keeper(group: dict, path: str) -> bool:
    """Make `path` the copy this group keeps. False if it is not in the group.

    The alternative - one button that removes everything except an automatic
    pick - is why the tool felt thin: the user could see four copies and
    still not say "that one, keep that one".
    """
    copies = group.get("copies") or []
    if not any(c.get("path") == path for c in copies):
        return False
    for c in copies:
        c["keep"] = (c.get("path") == path)
    group["keep"] = path
    return True


# ------------------------------------------------------- never-delete list
_CONFIG_KEY = "duplicate_protected_paths"


def load_protected() -> list:
    """The user's never-delete list. Never raises; bad data reads as empty."""
    try:
        from app.config_persist import load_config
        raw = load_config().get(_CONFIG_KEY) or []
    except Exception:
        return []
    if not isinstance(raw, (list, tuple)):
        return []
    return [str(x).strip() for x in raw if str(x).strip()]


# I-006: there used to be save_protected()/add_protected()/remove_protected()
# here, and nothing called any of them - the 2026-10 UI pass removed the
# affordance that would have (see duplicates_dialog's module docstring: "it
# just has no button here") but left the writers behind. Deleting them makes
# the truth explicit: this list is CONFIG-ONLY, maintained by hand in
# config.json, and the app only ever reads it. `load_protected`
# + `normalise_protected` + `is_protected` are the whole live surface, and
# `recycle()` re-checks it itself so a filter bug upstream cannot become a
# deletion. If the affordance ever comes back, its writer belongs here again -
# capped by `config_schema.PROTECTED_PATHS_MAX` on the way out, not only on
# load.


def normalise_protected(protected) -> tuple:
    """Pre-normalise the list once, so is_protected() stays cheap per file."""
    exact, folders, globs = set(), [], []
    for p in protected or []:
        p = str(p or "").strip()
        if not p:
            continue
        if any(ch in p for ch in "*?["):
            globs.append(p.lower())
        else:
            full = os.path.normcase(os.path.abspath(p))
            exact.add(full)
            if os.path.isdir(p):
                folders.append(full.rstrip("\\/") + os.sep)
    return exact, folders, globs


def is_protected(path: str, protected=None) -> bool:
    """True when `path` is covered by the never-delete list.

    Three shapes, because "never touch this" means different things:
      * an exact file path;
      * a FOLDER, which protects everything beneath it;
      * a glob such as `*.pdf` or `C:\\Photos\\*`.

    Matching is done on absolute, case-folded paths so a relative entry and
    an absolute one still agree.
    """
    if not protected or not path:
        return False
    if isinstance(protected, tuple) and len(protected) == 3 \
            and all(isinstance(x, (set, list)) for x in protected):
        exact, folders, globs = protected
    else:
        exact, folders, globs = normalise_protected(protected)
    if not exact and not folders and not globs:
        return False
    try:
        full = os.path.normcase(os.path.abspath(path))
    except Exception:
        return False
    if full in exact:
        return True
    for prefix in folders:
        if full.startswith(prefix):
            return True
    low = full.lower()
    for pat in globs:
        import fnmatch
        if fnmatch.fnmatch(low, pat):
            return True
    return False


def recycle(paths, timeout: int = 60, protected=None, should_stop=None,
            progress=None) -> dict:
    """Send files to the RECYCLE BIN. Never a hard delete.

    Refuses anything fs_safe says is unsafe, re-checks immediately before the
    call, and reports per-file outcomes rather than one blanket success.

    `protected` is the user's never-delete list. When it is None the stored
    list is loaded, so enforcement does not depend on the caller remembering
    to pass it - a filter bug upstream must not become data loss here.

    AUDIT E6: `should_stop` and `progress` are for the BATCH remove, which is
    the one genuinely long operation in this dialog - SHFileOperationW is
    called once per file, so 200 files is minutes of a dialog that looks hung.
    The progress strip said "Scan finished." for the whole of it, because the
    remove path never started it.

    Stopping is checked BETWEEN files and never mid-file: a half-executed
    SHFileOperationW cannot be recalled, and a user who asked to stop did not
    ask for a partial copy operation. `cancelled` is reported separately from
    `ok` so the UI can say what actually happened - files already moved stay
    moved and stay in the Recycle Bin, which the caller must tell the user.
    """
    paths = [p for p in (paths or []) if p]
    if not paths:
        return {"ok": True, "moved": 0, "refused": [], "failed": [],
                "freed": 0, "protected": 0, "cancelled": False}
    if not IS_WINDOWS:
        return {"ok": False, "moved": 0, "refused": [], "failed": paths,
                "freed": 0, "error": "Recycle Bin is Windows-only.",
                "cancelled": False}

    if protected is None:
        protected = load_protected()
    norm = normalise_protected(protected)

    refused, failed, moved, freed = [], [], 0, 0
    n_protected = 0
    cancelled = False
    for _i, p in enumerate(paths):
        if should_stop is not None and should_stop():
            cancelled = True
            break
        # Progress cadence: every file for a small batch, every 8th for a big
        # one. A fixed every-8th throttle reported ONCE for a six-file batch
        # (measured) and then went silent for the rest of the work, which is
        # the same "looks hung" problem the strip is meant to solve.
        if progress is not None:
            if len(paths) <= 20 or _i % 8 == 0 or _i == len(paths) - 1:
                progress(_i, len(paths))
        # Defense in depth: checked here, not only when the list was built, so
        # a path that became protected in between is still refused.
        if not is_safe_to_delete(p):
            refused.append(p)
            continue
        if norm and is_protected(p, norm):
            n_protected += 1
            refused.append(p)
            continue
        try:
            freed += os.path.getsize(p)
        except OSError:
            pass
        if _recycle_one(p):
            moved += 1
        else:
            failed.append(p)

    # OUTSIDE the loop. This return was indented one level too deep, so it
    # fired after the FIRST file and the function then returned None for every
    # multi-file call - which is why verify_duplicates.py died with
    # 'NoneType' object is not subscriptable the moment a batch had two files.
    # A single-file call still passed, which is exactly why this survived:
    # every earlier test moved one file at a time.
    #
    # `ok` deliberately ignores `cancelled`: the files that DID move moved
    # successfully, and cancelling is not a failure. `cancelled` carries that
    # half of the story so the caller can report both.
    return {"ok": not failed, "moved": moved, "refused": refused,
            "failed": failed, "freed": freed, "protected": n_protected,
            "cancelled": cancelled,
            "error": None if not failed else
            "Windows could not move %d file(s)." % len(failed)}


def _recycle_one(path: str) -> bool:
    """One file to the Recycle Bin via SHFileOperationW + FOF_ALLOWUNDO.

    BUG-037: the success test was wrong in a way that reported failure for
    files it had actually moved. `SHFileOperationW` returns **0 on success**
    (unusual for a Win32 call), and with `restype = c_void_p` ctypes hands
    back `None` for that 0 - so `rc == 0` was never true and every recycle
    was reported as "Windows could not move it", while the file had in fact
    gone to the Recycle Bin. An earlier probe hid this by printing
    `rc or 0`, which quietly turned None into 0.

    `c_int` restype is used deliberately: it makes the documented 0 land as
    the integer 0, so the comparison means what it says.
    """
    try:
        shell32 = ctypes.WinDLL("shell32", use_last_error=True)
        shell32.SHFileOperationW.restype = ctypes.c_int
        shell32.SHFileOperationW.argtypes = [ctypes.c_void_p]
        op = _SHFILEOPSTRUCTW()
        op.wFunc = _FO_DELETE
        # Double NUL terminated, as the API requires for a list.
        op.pFrom = path + "\x00\x00"
        op.pTo = None
        op.fFlags = (_FOF_ALLOWUNDO | _FOF_NOCONFIRMATION | _FOF_SILENT
                     | _FOF_NOERRORUI)
        rc = shell32.SHFileOperationW(ctypes.byref(op))
        return rc == 0 and not op.fAnyOperationsAborted
    except Exception:
        return False


#: Below this the scan is not a conclusion worth carrying to another screen.
DUPLICATE_FINDING_BYTES = 2 * (1024 ** 3)


def publish_reclaim_finding(stats) -> list:
    """Publish what a completed duplicate scan found (AUDIT TX-002).

    The scan is authoritative for its namespace: a later scan with nothing
    over the threshold clears the old row, and so does cleaning the copies
    down. Only a caller that actually completed a scan may invoke this - an
    error or a cancelled walk has no standing to claim "nothing left".
    """
    try:
        from app import insights
        from app.insights import Finding
        from app.utils import format_bytes
        import time as _time
    except Exception:
        return []
    try:
        wasted = int((stats or {}).get("wasted") or 0)
    except (TypeError, ValueError, OverflowError):
        wasted = 0
    if wasted < DUPLICATE_FINDING_BYTES:
        try:
            return insights.replace("duplicates.")
        except Exception:
            return []
    try:
        groups = int((stats or {}).get("groups") or 0)
    except (TypeError, ValueError, OverflowError):
        groups = 0
    partial = bool((stats or {}).get("truncated")
                   or (stats or {}).get("hash_capped"))
    detail = ("%d set%s of copies. One copy of each is always kept, and "
              "every removal goes to the Recycle Bin."
              % (groups, "" if groups == 1 else "s"))
    if partial:
        detail += " The folder is huge, so this scan is partial."
    finding = Finding(
        id="duplicates.files",
        domain="storage",
        severity="warn",
        title="%s of duplicate files" % format_bytes(wasted, compact=True),
        detail=detail,
        evidence={"bytes": wasted, "groups": groups, "partial": partial},
        action={"kind": "route", "target": "duplicates",
                "label": "Open Duplicates"},
        observed_at=_time.time(),
        confidence="partial" if partial else "measured",
    )
    try:
        return insights.replace("duplicates.", finding)
    except Exception:
        return []
