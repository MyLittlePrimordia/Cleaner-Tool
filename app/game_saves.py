"""Game saves: roots, archives, and a SAFE restore.

The Clean tab has always been able to *back up* save folders
(``backup_game_saves`` in app/tasks/game_tasks.py). Until now there was no
way back: the zips piled up in Documents and the only restore path was the
user unzipping them by hand into the right folders — a step that quietly
requires knowing what "Saved Games" maps to on a OneDrive-redirected
profile. This module adds the other half.

Design rules, because a bad restore is worse than no restore:

  * **Manifest, not guesswork.** New archives carry a ``MANIFEST`` entry
    listing every file's original absolute path and its arcname. Restore
    reads that; it never infers a destination from an arcname. Archives
    written before this change have no manifest and are reported as
    unrestorable rather than extracted on a hunch.
  * **Only inside save roots.** Every destination in the manifest must
    resolve inside one of the CURRENT save roots before a single byte is
    written. A tampered or hand-edited manifest cannot aim a restore at
    C:\\Windows.
  * **Never overwrite silently.** Files that would be replaced are first
    collected into one ``PreRestore_*.zip`` in the backup folder, so the
    restore itself has an undo.
  * **Locked means skipped.** A save file held open by a running game is
    counted, not forced; the rest of the restore still lands.
  * **Atomic per file.** Each entry is written to a temp file beside the
    destination and then ``os.replace``d, so an interrupted restore cannot
    leave a half-written save.
"""

from __future__ import annotations

import json
import os
import zipfile
from datetime import datetime

MANIFEST_NAME = "cleanertool-saves.json"
MANIFEST_VERSION = 1
BACKUP_PREFIX = "GameSavesBackup_"
PRE_RESTORE_PREFIX = "PreRestore_"

#: Extension of the archives this module writes/reads.
ARCHIVE_SUFFIX = ".zip"


# --------------------------------------------------------------------------- #
# Where saves live
# --------------------------------------------------------------------------- #

def save_roots() -> list[tuple[str, str]]:
    """[(absolute_path, tag)] for the save folders that exist right now.

    F-4: resolved through known_folder, because Documents and Saved Games
    are OneDrive-redirected by default on new Win11 profiles — a
    USERPROFILE-relative path silently misses the saves the backup exists
    to protect. Kept in this module (not game_tasks) so the restore path
    can validate destinations against exactly the same list the backup used.
    """
    from app.utils import known_folder

    home = os.environ.get("USERPROFILE", "")
    doc_fallback = os.path.join(home, "Documents")
    sources = [
        (known_folder("{4C5C32FF-BB9D-43B0-B5B4-2D72E54EAAA4}",
                      os.path.join(home, "Saved Games")), "Saved Games"),
        (os.path.join(known_folder("Personal", doc_fallback), "My Games"),
         "My Games"),
        (os.path.join(os.environ.get("APPDATA", ""), ".minecraft", "saves"),
         "Minecraft"),
    ]
    return [(p, tag) for p, tag in sources if p and os.path.isdir(p)]


def backup_folder() -> str:
    """Documents\\CleanerTool Backups\\Game Saves (created on demand)."""
    from app.utils import known_folder

    docs = known_folder("Personal",
                        os.path.join(os.environ.get("USERPROFILE", ""),
                                     "Documents"))
    dest = os.path.join(docs or os.path.join(os.environ.get("USERPROFILE", ""),
                                             "Documents"),
                        "CleanerTool Backups", "Game Saves")
    try:
        os.makedirs(dest, exist_ok=True)
    except Exception:
        pass
    return dest


# --------------------------------------------------------------------------- #
# Manifest
# --------------------------------------------------------------------------- #

def write_manifest(zf: zipfile.ZipFile, files: list[tuple[str, str]],
                   roots: list[tuple[str, str]]) -> None:
    """Record `files` as [(source_abs_path, arcname)] inside the archive.

    Written LAST by the backup so a truncated/cancelled archive simply has
    no manifest — which restore already treats as "cannot restore this
    one", the honest answer for a partial zip.
    """
    manifest = {
        "version": MANIFEST_VERSION,
        "created": datetime.now().isoformat(timespec="seconds"),
        "roots": [{"path": p, "tag": t} for p, t in roots],
        # POSIX separators: a backslash arcname is a Windows-only spelling
        # and zip readers on other tools see one odd filename per directory
        # instead of a tree. The backup writes the same normalization, so
        # the manifest and the archive can never disagree.
        "files": [{"path": os.path.abspath(src),
                   "arcname": arc.replace("\\", "/")}
                  for src, arc in files],
    }
    zf.writestr(MANIFEST_NAME, json.dumps(manifest, indent=1))


def read_manifest(zip_path: str) -> dict | None:
    """The parsed manifest, or None when this archive has none / is broken."""
    try:
        with zipfile.ZipFile(zip_path) as zf:
            raw = zf.read(MANIFEST_NAME)
    except (KeyError, OSError, zipfile.BadZipFile):
        return None
    try:
        data = json.loads(raw.decode("utf-8"))
    except Exception:
        return None
    if not isinstance(data, dict) or not isinstance(data.get("files"), list):
        return None
    return data


# --------------------------------------------------------------------------- #
# Listing
# --------------------------------------------------------------------------- #

def list_backups(folder: str | None = None) -> list[dict]:
    """Every saves archive, newest first. Never raises.

    Each entry: {path, name, mtime, size, files, restorable, reason}.
    `files` is None when the archive could not be read at all; the UI shows
    "?" rather than pretending 0 saves.
    """
    folder = folder or backup_folder()
    out: list[dict] = []
    try:
        names = os.listdir(folder)
    except OSError:
        return out
    for name in names:
        if not name.startswith(BACKUP_PREFIX) or not name.endswith(ARCHIVE_SUFFIX):
            continue
        path = os.path.join(folder, name)
        try:
            st = os.stat(path)
        except OSError:
            continue
        entry = {"path": path, "name": name, "mtime": st.st_mtime,
                 "size": st.st_size, "files": None, "restorable": False,
                 "reason": ""}
        man = read_manifest(path)
        if man is None:
            entry["reason"] = ("No manifest — made before restore support. "
                               "Unzip it by hand into your save folders.")
        else:
            entry["files"] = len(man["files"])
            entry["restorable"] = True
        out.append(entry)
    out.sort(key=lambda e: e["mtime"], reverse=True)
    return out


# --------------------------------------------------------------------------- #
# Restore
# --------------------------------------------------------------------------- #

def _inside_roots(path: str, roots: list[str]) -> bool:
    """True when `path` resolves inside one of `roots` (realpath compare)."""
    try:
        target = os.path.normcase(os.path.realpath(path))
    except Exception:
        return False
    for root in roots:
        try:
            r = os.path.normcase(os.path.realpath(root))
        except Exception:
            continue
        if not r:
            continue
        if target == r or target.startswith(r + os.sep):
            return True
    return False


def _safe_arcname(arc: str) -> bool:
    """A relative, non-traversing arcname (zip-slip defensive check)."""
    if not arc or arc.startswith(("/", "\\")):
        return False
    if os.path.isabs(arc) or ":" in arc.split("/")[0]:
        return False
    parts = arc.replace("\\", "/").split("/")
    return ".." not in parts


def plan_restore(zip_path: str) -> dict:
    """What a restore WOULD do. Read-only; used by the UI preview and tests.

    Returns {ok, error, files: [{path, arcname}], skipped: [{path, why}],
             roots, created, bytes}.
    """
    man = read_manifest(zip_path)
    if man is None:
        return {"ok": False, "files": [], "skipped": [], "roots": [],
                "bytes": 0,
                "error": "This backup has no manifest, so its original paths "
                         "are unknown. Unzip it by hand into your save folders."}
    roots = [p for p, _tag in save_roots()]
    files, skipped, total = [], [], 0
    try:
        with zipfile.ZipFile(zip_path) as zf:
            infos = {i.filename: i for i in zf.infolist()}
    except (OSError, zipfile.BadZipFile) as exc:
        return {"ok": False, "files": [], "skipped": [], "roots": roots,
                "bytes": 0, "error": f"Could not read the archive: {exc}"}

    for item in man.get("files") or []:
        if not isinstance(item, dict):
            continue
        src = str(item.get("path") or "")
        arc = str(item.get("arcname") or "").replace("\\", "/")
        if not src or not _safe_arcname(arc):
            skipped.append({"path": src or arc, "why": "bad archive entry"})
            continue
        if arc not in infos:
            skipped.append({"path": src, "why": "missing from the archive"})
            continue
        if roots and not _inside_roots(src, roots):
            skipped.append({"path": src,
                            "why": "outside the current save folders"})
            continue
        if not roots:
            skipped.append({"path": src, "why": "no save folders found"})
            continue
        files.append({"path": src, "arcname": arc,
                      "size": infos[arc].file_size})
        total += infos[arc].file_size
    return {"ok": True, "error": "", "files": files, "skipped": skipped,
            "roots": roots, "bytes": total,
            "created": man.get("created") or ""}


def _snapshot_existing(files: list[dict], folder: str,
                       log=lambda _m: None) -> str | None:
    """Zip the current versions of files about to be replaced.

    One archive, written before any restore write. Returns its path (or None
    when nothing existed). Never raises: if the snapshot cannot be made, the
    caller must stop — that is the point of having one.
    """
    existing = [f for f in files if os.path.isfile(f["path"])]
    if not existing:
        return None
    stamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")
    dest = os.path.join(folder, f"{PRE_RESTORE_PREFIX}{stamp}.zip")
    with zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED) as zf:
        count = 0
        for f in existing:
            try:
                zf.write(f["path"], f["arcname"])
                count += 1
            except OSError:
                continue
    log(f"Current versions of {count} file(s) saved to {os.path.basename(dest)}")
    _prune(folder, PRE_RESTORE_PREFIX, keep=3)
    return dest


def _prune(folder: str, prefix: str, keep: int) -> None:
    try:
        names = sorted(n for n in os.listdir(folder)
                       if n.startswith(prefix) and n.endswith(ARCHIVE_SUFFIX))
    except OSError:
        return
    for stale in names[:-keep] if len(names) > keep else []:
        try:
            os.remove(os.path.join(folder, stale))
        except OSError:
            pass


def restore_backup(zip_path: str, log=lambda _m: None,
                   cancelled=lambda: False, dry_run: bool = False) -> dict:
    """Restore one archive. Returns a result dict; never raises on bad input.

    {ok, error, restored, skipped, bytes, snapshot, cancelled}
    """
    plan = plan_restore(zip_path)
    if not plan.get("ok"):
        return {"ok": False, "error": plan.get("error") or "Cannot restore.",
                "restored": 0, "skipped": 0, "bytes": 0, "snapshot": None,
                "cancelled": False}
    files = plan["files"]
    out = {"ok": True, "error": "", "restored": 0, "skipped": len(plan["skipped"]),
           "bytes": 0, "snapshot": None, "cancelled": False}
    for s in plan["skipped"]:
        log(f"  ! skipped {s['path']} ({s['why']})")
    if not files:
        return {**out, "ok": False,
                "error": "Nothing in this backup can be restored to the "
                         "save folders that exist now."}
    if dry_run:
        log(f"[dry-run] would restore {len(files)} file(s) "
            f"({len(plan['skipped'])} skipped).")
        return out

    folder = os.path.dirname(os.path.abspath(zip_path)) or backup_folder()
    try:
        out["snapshot"] = _snapshot_existing(files, folder, log)
    except Exception as exc:                      # noqa: BLE001
        return {**out, "ok": False,
                "error": f"Could not save the current files first: {exc}"}

    with zipfile.ZipFile(zip_path) as zf:
        for item in files:
            if cancelled():
                out["cancelled"] = True
                log("  ! Restore stopped — files already written were kept.")
                break
            dest = item["path"]
            tmp = dest + ".cleanertool-restore"
            try:
                os.makedirs(os.path.dirname(dest), exist_ok=True)
                with zf.open(item["arcname"]) as src, open(tmp, "wb") as dst:
                    while True:
                        chunk = src.read(1024 * 256)
                        if not chunk:
                            break
                        dst.write(chunk)
                os.replace(tmp, dest)
                out["restored"] += 1
                out["bytes"] += item.get("size", 0)
            except (OSError, PermissionError, zipfile.BadZipFile):
                out["skipped"] += 1
                try:
                    if os.path.exists(tmp):
                        os.remove(tmp)
                except OSError:
                    pass
                continue
    if out["restored"] == 0 and not out["cancelled"]:
        out["ok"] = False
        out["error"] = ("No file could be written — the game may still be "
                        "running and holding its saves open.")
    return out
