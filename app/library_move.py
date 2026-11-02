"""N-003: library mover audit -- which big games *can* move, and how.

Storage Insight already measures the biggest games; it deliberately has no
delete button ("uninstalling a whole game is a human decision that belongs
in its launcher"). Moving is the same kind of decision, one step further:
a Steam library *can* be moved drive-to-drive from Steam's own Storage
settings, an Epic install cannot (its manifest path is absolute -- moving
means reinstall or re-link), and a custom/portable folder has no launcher
at all, so only a careful manual move is honest.

This module is the Tk-free audit behind that honesty. `audit()` takes the
game dicts Storage Insight already measured ({launcher, name, path, bytes,
drive}) plus an optional free-space map, and returns one row per game that
is worth a conversation:

  {"name", "launcher", "path", "bytes", "drive", "verdict", "how"}

verdict is one of "steam-move" (Steam's own mover handles it),
"reinstall-only" (Epic/GOG/Ubisoft/Riot/Xbox/EA: the launcher owns the
path, so moving by hand breaks it), or "manual-only" (no launcher owns
it). A game already off the system drive is "already-off-system" -- worth
knowing, never worth nagging about. `how` is the one sentence the dialog
shows; it never claims a move the launcher cannot do.

Safety rules, matching the rest of the app:

* read-only: no registry, no moves, no directory writes -- pure function
  of the list the caller already measured;
* a path inside a reparse point / junction is "manual-only" with a warning,
  never a launcher move: a mover that follows a junction moves the wrong
  drive's bytes;
* unknown launchers are "manual-only", never "steam-move": the audit must
  not upgrade a guess into a promise;
* empty / malformed input is an empty audit, not an error.
"""
from __future__ import annotations

import os

#: Launchers whose storage the vendor's own mover understands. Steam alone:
#: Settings > Storage > Move. Every other launcher stores an absolute path
#: (Epic .item manifests, GOG/Ubisoft per-root installs) or a managed
#: package (Xbox), so a hand move orphans it -- the honest advice is
#: reinstall to the new location.
_STEAM = "steam"


def _is_steam(launcher) -> bool:
    return str(launcher or "").strip().lower() == _STEAM


def _system_drive() -> str:
    try:
        drive = os.path.splitdrive(os.path.abspath(os.sep))[0].upper()
        return drive or "C:"
    except Exception:
        return "C:"


def _norm(path) -> str:
    try:
        return os.path.normcase(os.path.abspath(str(path or "")))
    except Exception:
        return ""


def _is_link(path) -> bool:
    try:
        return bool(path) and (os.path.islink(path) or os.path.isjunction(path))
    except Exception:
        # isjunction exists only on Windows; a missing attribute is "no".
        try:
            return bool(path) and os.path.islink(path)
        except Exception:
            return False


def audit(games, *, system_drive=None, free_by_drive=None) -> list:
    """One audit row per game worth mentioning. Never raises."""
    try:
        items = list(games or ())
    except Exception:
        return []
    try:
        sys_drive = str(system_drive or _system_drive()).upper()
    except Exception:
        sys_drive = "C:"
    rows = []
    for g in items:
        try:
            if not isinstance(g, dict):
                continue
            name = str(g.get("name") or "?").strip() or "?"
            launcher = str(g.get("launcher") or "").strip()
            path = str(g.get("path") or "").strip()
            try:
                size = int(g.get("bytes") or 0)
            except (TypeError, ValueError):
                size = 0
            drive = str(g.get("drive") or "").strip().upper()
            if not path or size <= 0:
                continue
            if not drive:
                try:
                    drive = os.path.splitdrive(os.path.abspath(path))[0].upper()
                except Exception:
                    drive = ""
            off_system = bool(drive) and drive != sys_drive
            if off_system:
                verdict, how = (
                    "already-off-system",
                    "Already off the system drive -- nothing to gain by moving.",
                )
            elif _is_link(path):
                verdict, how = (
                    "manual-only",
                    "This folder is a link -- move it by hand only, "
                    "so the link target is moved, not the link.",
                )
            elif _is_steam(launcher):
                verdict, how = (
                    "steam-move",
                    "Move it inside Steam: Settings > Storage > Move. "
                    "Steam updates its library itself.",
                )
            elif launcher:
                verdict, how = (
                    "reinstall-only",
                    "This launcher stores an absolute path -- reinstall "
                    "to the new drive instead of moving the folder.",
                )
            else:
                verdict, how = (
                    "manual-only",
                    "No launcher owns this folder -- copy it, then point "
                    "the program at the new location.",
                )
            row = {"name": name, "launcher": launcher, "path": path,
                   "bytes": size, "drive": drive, "verdict": verdict,
                   "how": how}
            # Free-space context when the caller has it; absent is absent,
            # never a claim.
            try:
                free = (free_by_drive or {}).get(drive)
                if isinstance(free, (int, float)) and free >= 0:
                    row["drive_free"] = int(free)
            except Exception:
                pass
            rows.append(row)
        except Exception:
            continue
    # Biggest first, matching the Biggest games list it annotates.
    try:
        rows.sort(key=lambda r: -int(r.get("bytes") or 0))
    except Exception:
        pass
    return rows


def summary(rows) -> str:
    """One sentence for the dialog header. Never raises."""
    try:
        rows = list(rows or ())
    except Exception:
        return "No games to review."
    if not rows:
        return "No games to review."
    try:
        movable = sum(1 for r in rows
                      if isinstance(r, dict) and r.get("verdict") == "steam-move")
        total = sum(int(r.get("bytes") or 0) for r in rows
                    if isinstance(r, dict))
    except Exception:
        return "No games to review."
    from app.utils import format_bytes
    if movable:
        return ("%d of %d %s can move inside Steam (%s on the system drive)."
                % (movable, len(rows),
                   "game" if len(rows) == 1 else "games",
                   format_bytes(total)))
    return ("%d %s reviewed -- none can move inside its launcher; "
            "reinstall to a new drive instead."
            % (len(rows), "game" if len(rows) == 1 else "games"))


__all__ = ["audit", "summary"]
