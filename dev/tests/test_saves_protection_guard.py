"""A-005 guard: the Clean/Games targets must never touch game saves.

README ("Safe by Design") promises the app "Never touches game saves,
personal documents, or user profiles." Nothing enforced that claim: the
Clean path tables could drift into a saves folder — a launcher cache dir
that happens to overlap ``Documents\\My Games\\<game>\\Saves``, or a
whole-game-root entry of the BrawlhallaAir kind — and every test would
still pass.

This is the saves counterpart of dev/tools/verify_sec001_task_allowlist.py: a
cheap, fail-closed drift guard. Two layers:

  1. every RESOLVED clean path is scanned against a save deny-list, and
     checked that it does not sit at or above a known save root;
  2. every game-junk table entry must end on a known junk sub-folder (its
     leaf), never a game root.

Layer 2 is deliberately fail-closed: adding a new junk entry whose leaf is
not already in ``_SAFE_LEAVES`` fails until someone reviews it and adds
the leaf. That is the whole point — a review gate for new deletion
targets.
"""
from __future__ import annotations

import os
import re

import pytest

from app.tasks import launcher_paths as LP

# Save locations a clean path must never be, or sit above.
# Kept narrow on purpose: legit clean targets DO live under "My Games"
# (e.g. ...\My Games\Rocket League\TAGame\Logs), so a blanket "my games"
# match would be a false positive. The bare "My Games" root is matched by
# the "$" pattern below; sub-paths are judged by their leaf instead.
_FORBIDDEN = (
    re.compile(r"[/\\]saved games([/\\]|$)", re.I),   # %USERPROFILE%\Saved Games
    re.compile(r"[/\\]save ?games([/\\]|$)", re.I),   # "\save games", "\savegames"
    re.compile(r"[/\\]saves([/\\]|$)", re.I),         # a "saves" folder
    re.compile(r"\.sav$", re.I),                      # a save file
    re.compile(r"[/\\]userdata([/\\]|$)", re.I),      # Steam Cloud root
    re.compile(r"[/\\]remote([/\\]|$)", re.I),        # Steam Cloud saves
    re.compile(r"[/\\]my games$", re.I),              # the whole My Games root
)

# Junk sub-folders the game-junk tables are allowed to end on. Fail-closed:
# a new entry with a novel leaf must add it here after review.
_SAFE_LEAVES = frozenset({
    "logs", "log", "crashes", "crash", "crashdumps",
    "shadercache", "cache", "caches", "player.log", "player-prev.log",
})

_AGGREGATES = (
    "ALL_LAUNCHER_CACHE_PATHS",
    "GAMER_LAUNCHER_ALL",
    "GAME_FILES_ALL",
    "GPU_SHADER_CACHE_ALL",
)


def _forbidden(path: str) -> str:
    """The first deny-list pattern `path` matches, or ""."""
    for pat in _FORBIDDEN:
        if pat.search(path or ""):
            return pat.pattern
    return ""


def _resolved_clean_paths() -> list:
    paths = []
    for name in _AGGREGATES:
        paths.extend(getattr(LP, name, []) or [])
    return [p for p in paths if isinstance(p, str) and p.strip()]


# --------------------------------------------------------------------------- #
# 0. The guard itself must not be vacuous — prove it separates saves from junk.
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("bad", [
    r"C:\Users\me\Saved Games",
    r"C:\Users\me\Saved Games\SomeGame",
    r"C:\Users\me\Documents\My Games",
    r"D:\Steam\userdata\12345\remote",
    r"C:\Games\slot1.sav",
    r"C:\Users\me\Documents\Paradox Interactive\Stellaris\save games",
])
def test_guard_flags_known_save_locations(bad):
    assert _forbidden(bad), f"guard failed to flag a save path: {bad}"


@pytest.mark.parametrize("good", [
    r"C:\Users\me\Documents\My Games\Rocket League\TAGame\Logs",
    r"C:\Users\me\AppData\Local\Steam\logs",
    r"C:\Users\me\AppData\Local\D3DSCache",
    r"C:\Users\me\AppData\Local\EpicGamesLauncher\Saved\Logs",
])
def test_guard_allows_known_junk_locations(good):
    assert not _forbidden(good), f"guard false-positived on junk: {good}"


# --------------------------------------------------------------------------- #
# 1. No real clean target is a save location, or sits above one.
# --------------------------------------------------------------------------- #

def test_no_clean_target_is_a_save_location():
    hits = [(p, _forbidden(p)) for p in _resolved_clean_paths()]
    hits = [(p, pat) for p, pat in hits if pat]
    assert not hits, "clean target(s) point at game saves:\n" + "\n".join(
        f"  {p}  (matched {pat!r})" for p, pat in hits)


def test_no_clean_target_sits_at_or_above_a_save_root():
    home = os.environ.get("USERPROFILE", "")
    roots = []
    if home:
        roots.append(os.path.join(home, "Saved Games"))
        roots.append(os.path.join(home, "Documents", "My Games"))
    if getattr(LP, "DOCUMENTS", ""):
        roots.append(os.path.join(LP.DOCUMENTS, "My Games"))

    bad = []
    for p in _resolved_clean_paths():
        np = os.path.normcase(os.path.abspath(p))
        for r in roots:
            nr = os.path.normcase(os.path.abspath(r))
            if np == nr or nr.startswith(np + os.sep):
                bad.append((p, r))
    assert not bad, (
        "clean target(s) are at or above a save root:\n"
        + "\n".join(f"  {p}  >= {r}" for p, r in bad))


# --------------------------------------------------------------------------- #
# 2. Every game-junk entry names a junk sub-folder, never a game root.
# --------------------------------------------------------------------------- #

def test_game_junk_entries_end_on_a_known_junk_folder():
    bad = []
    for table in (LP._TOP_GAME_JUNK, LP._DOC_GAME_JUNK, LP._LOW_GAME_JUNK):
        for entry in table:
            leaf = str(entry[-1]).lower()
            if leaf not in _SAFE_LEAVES:
                bad.append(entry)
    assert not bad, (
        "game-junk entry ends on a non-junk folder (could be a game root, "
        "like the removed BrawlhallaAir entry):\n"
        + "\n".join(f"  {e}" for e in bad)
        + f"\nIf the leaf really is disposable junk, add it to _SAFE_LEAVES "
          f"in {os.path.basename(__file__)} after review.")
