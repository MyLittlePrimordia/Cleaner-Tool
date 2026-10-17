"""
Game Night (feature 9) — one press to get the PC ready for a session,
one press to put everything back.

This module deliberately owns almost no behaviour. Game Night is a
COMPOSITION of things the app already does safely:

  * the tweaks are the existing "Game Session" preset from tab_presets —
    not a new list, so anything the Tweak tab can apply and undo, Game
    Night can apply and undo, through the identical run engine
  * apply / revert go through Application.run_tasks() exactly like a
    normal Tweak run, so cancel, the Admin Gate, snapshots, applied-tweak
    bookkeeping and the scorecard all behave as usual
  * the "only undo what we turned on" rule is the same fresh-keys rule
    Session Pilot already uses (see Application._pilot_revert): tweaks
    the user had already applied before pressing Game Night are left
    alone, because reverting those would undo the user's own setup
    rather than Game Night's work

What lives here is the small amount that is genuinely new: the list of
background apps the user may optionally have closed, and a graceful way
to close them.

Zero dependency, Windows-only at the edges (taskkill), stdlib throughout.
"""

from __future__ import annotations

import sys

# The tweak set. Reusing the existing preset name rather than a private
# copy means a change to the preset automatically applies here too — and
# it can never drift out of sync with what the Tweak tab shows.
PRESET_NAME = "Game Session"


def preset_task_keys():
    """Task keys Game Night applies, straight from tab_presets.

    Returns [] if the preset ever disappears; callers treat an empty list
    as "nothing to do" rather than inventing a fallback set, because a
    guessed list of registry tweaks is exactly the thing this module is
    supposed to avoid."""
    try:
        from app.tab_presets import PRESETS
        return list(PRESETS.get("Tweak", {}).get(PRESET_NAME, []) or [])
    except Exception:
        return []


# --------------------------------------------------------------------------- #
# Optional background-app quieting
# --------------------------------------------------------------------------- #
# Every entry is OFF by default and only ever acted on when the user has
# explicitly ticked it. The list is deliberately short and boring: things
# that are safe to shut for an hour and obviously restartable. Nothing
# game-related (Steam, Epic, launchers) and nothing driver/peripheral
# related is listed — closing those breaks the session it is meant to
# help. (Overwolf/CurseForge are overlays and mod managers, not launchers:
# closing them mid-game just drops the overlay and they reopen in a click.)
#
# This is a CANDIDATE pool, not the on-screen list: the Auto-Pilot dialog
# shows only the entries whose app actually exists on the machine (see
# PilotDialog._rebuild_close_rows), so the box never offers an option for
# something that is not installed — and every row it does show has a real
# icon.
#
# (key, friendly name, plain-English note, (exe names...))
CLOSEABLE_APPS = (
    ("browsers", "Web browsers",
     "Chrome, Edge, Firefox — usually the biggest memory user",
     ("chrome.exe", "msedge.exe", "firefox.exe", "brave.exe", "opera.exe",
      "vivaldi.exe")),
    ("discord", "Discord",
     "Only if you are not using it for voice chat",
     ("discord.exe",)),
    ("spotify", "Spotify",
     "Music app and its helper",
     ("spotify.exe",)),
    ("onedrive", "OneDrive",
     "Pauses cloud syncing until you restart it",
     ("onedrive.exe",)),
    ("clouddrives", "Dropbox and Google Drive",
     "They pause, then resync when you restart them",
     ("dropbox.exe", "googledrivefs.exe")),
    ("chat", "Chat and work apps",
     "Teams, Slack and Zoom — not needed for the game itself",
     ("ms-teams.exe", "teams.exe", "slack.exe", "zoom.exe", "skype.exe")),
    ("wallpaper", "Wallpaper Engine",
     "Animated wallpapers quietly use GPU and RAM",
     ("wallpaper64.exe", "wallpaper32.exe")),
    ("overlay", "Overwolf and CurseForge",
     "Game overlays and mod managers you can reopen later",
     ("overwolf.exe", "curseforge.exe")),
    ("creative", "Adobe Creative Cloud",
     "Background updater and syncing",
     ("creative cloud.exe", "adobe desktop service.exe",
      "creative cloud helper.exe")),
)

APP_KEYS = tuple(a[0] for a in CLOSEABLE_APPS)


# --------------------------------------------------------------------------- #
# User-picked apps
# --------------------------------------------------------------------------- #
# The user can add their own programs to the close list (Auto-Pilot → Game
# Mode → Add app). Those are stored as `custom:<exe name>` keys beside the
# built-in ones, and the only thing ever kept or acted on is a plain process
# NAME — never a path, never a command line. close_apps hands that name to
# taskkill as a single argv element with shell=False, so even a hand-edited
# config can only ask us to close a process whose name is a real exe name;
# it cannot name an executable for us to run.
CUSTOM_PREFIX = "custom:"

#: Characters that can never appear in a process name we accept: the path
#: separators would turn a name into a location, the rest are shell quirks.
_UNSAFE_IN_EXE = '\\/:*?"<>|'


def custom_exe_name(value):
    """Validate one custom app: 'custom:steam.exe' or 'Steam.exe' -> the exe
    name as given; anything else -> None.

    Fail-closed on purpose: a name carrying a path separator, a (drive)
    colon, a quote, a control character, or no `.exe` suffix is refused, so
    nothing that could name a *location* or a command line survives."""
    s = str(value or "").strip()
    if s.lower().startswith(CUSTOM_PREFIX):
        s = s[len(CUSTOM_PREFIX):].strip()
    if not s or len(s) > 96 or not s.lower().endswith(".exe"):
        return None
    if s[0] in "-." or any(ch in _UNSAFE_IN_EXE or ord(ch) < 32 for ch in s):
        return None
    return s


def custom_key(name):
    """Storage key for a user-picked app: 'Steam.exe' -> 'custom:steam.exe'.
    None when the name is not a plain exe name."""
    n = custom_exe_name(name)
    return (CUSTOM_PREFIX + n.lower()) if n else None


def clean_custom_apps(names):
    """Sanitised, de-duplicated, lowercase custom-app list (config hygiene)."""
    out, seen = [], set()
    for n in (names or []):
        c = custom_exe_name(n)
        if c is None:
            continue
        low = c.lower()
        if low not in seen:
            seen.add(low)
            out.append(low)
    return out[:25]


def custom_label(name):
    """Display name for a custom app: exe stem with the first letter up."""
    s = str(name or "").strip()
    if s.lower().endswith(".exe"):
        s = s[:-4]
    s = s.strip() or str(name or "").strip()
    return (s[:1].upper() + s[1:]) if s else "?"


def exe_names_for(keys):
    """Flat tuple of exe names for the ticked app keys. Unknown keys are
    ignored — a stale config can never ask us to close something that is
    not on the reviewed list above — and a `custom:` key only contributes a
    name that passes custom_exe_name (a plain exe basename, picked by the
    user in the Auto-Pilot dialog)."""
    wanted = set(str(k) for k in (keys or []))
    names, seen = [], set()

    def _add(n):
        if n.lower() not in seen:
            seen.add(n.lower())
            names.append(n)

    for key, _label, _note, exes in CLOSEABLE_APPS:
        if key in wanted:
            for exe in exes:
                _add(exe)
    for key in sorted(wanted):
        exe = custom_exe_name(key)
        if exe:
            _add(exe)
    return tuple(names)


def close_apps(keys, log=None):
    """Politely ask the ticked apps to close. Returns [closed friendly names].

    Built-in keys come from the reviewed table above; `custom:` keys are
    apps the user picked themselves (see custom_exe_name for what is
    accepted).

    Uses `taskkill /IM <exe>` WITHOUT /F on purpose: that posts WM_CLOSE,
    which is the same thing clicking the window's X does. The app gets to
    prompt about unsaved work and shut down cleanly. /F would terminate
    it outright and could lose a half-written message or document, which
    is not an acceptable trade for a few hundred MB of RAM.

    An app that is not running simply returns a non-zero exit code and is
    reported as "not running" rather than a failure. Never raises."""
    if not sys.platform.startswith("win"):
        return []
    wanted = set(str(k) for k in (keys or []))
    closed = []
    for key, label, _note, exes in CLOSEABLE_APPS:
        if key not in wanted:
            continue
        hit = False
        for exe in exes:
            if _taskkill(exe, log):
                hit = True
        if hit:
            closed.append(label)
        elif log is not None:
            try:
                log(f"  · {label} was not running.")
            except Exception:
                pass
    for key in sorted(wanted):
        exe = custom_exe_name(key)
        if not exe:
            continue
        label = custom_label(exe)
        if _taskkill(exe, log):
            closed.append(label)
        elif log is not None:
            try:
                log(f"  · {label} was not running.")
            except Exception:
                pass
    return closed


def _taskkill(exe_name: str, log=None) -> bool:
    """True when taskkill reported it closed at least one instance.

    argv list + shell=False: the exe names come from the frozen table
    above, but the project's rule is that nothing reaches a shell, so
    this follows it regardless."""
    try:
        import subprocess as _sp
        from app.utils import resolve_exe
        proc = _sp.run(
            [resolve_exe("taskkill"), "/IM", exe_name],
            capture_output=True, text=True, timeout=15, errors="replace",
            creationflags=getattr(_sp, "CREATE_NO_WINDOW", 0),
        )
        return proc.returncode == 0
    except Exception as exc:
        if log is not None:
            try:
                log(f"  ! could not close {exe_name} ({exc}).")
            except Exception:
                pass
        return False
