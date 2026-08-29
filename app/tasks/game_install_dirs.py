"""
Best-effort discovery of *game install* folders (not cache folders — see
launcher_paths.py for those) for known launchers. Used only by the
"Exclude Game Folders from Defender" tweak, which needs real game-content
directories, not the launcher web caches the Clean/Games tabs work with.

This is intentionally best-effort: every lookup is wrapped so a missing
launcher, registry key, or malformed file simply yields nothing rather than
raising. Only paths that actually exist on disk are ever returned.
"""

import os
import re

from app.utils import IS_WINDOWS

if IS_WINDOWS:
    import winreg

_SYSTEMDRIVE = os.environ.get("SYSTEMDRIVE", "C:")
_PROGRAM_FILES = os.environ.get("ProgramFiles", f"{_SYSTEMDRIVE}\\Program Files")
_PROGRAM_FILES_X86 = os.environ.get("ProgramFiles(x86)", f"{_SYSTEMDRIVE}\\Program Files (x86)")


def _reg_str(hive, path, name):
    if not IS_WINDOWS:
        return None
    key = None
    try:
        key = winreg.OpenKey(hive, path)
        value, _ = winreg.QueryValueEx(key, name)
        return value
    except Exception:
        return None
    finally:
        if key is not None:
            try:
                winreg.CloseKey(key)
            except Exception:
                pass


def _existing(paths):
    return [p for p in paths if p and os.path.isdir(p)]


def get_steam_library_paths():
    """Steam install dir (from registry) plus every additional library folder
    listed in steamapps/libraryfolders.vdf (simple key/value text format —
    parsed with a small regex rather than a full VDF parser)."""
    install_path = (
        _reg_str(winreg.HKEY_CURRENT_USER, "Software\\Valve\\Steam", "SteamPath")
        or _reg_str(winreg.HKEY_LOCAL_MACHINE, "SOFTWARE\\WOW6432Node\\Valve\\Steam", "InstallPath")
        or os.path.join(_PROGRAM_FILES_X86, "Steam")
    )
    install_path = install_path.replace("/", "\\")
    libraries = {install_path}

    vdf_path = os.path.join(install_path, "steamapps", "libraryfolders.vdf")
    try:
        if os.path.isfile(vdf_path):
            with open(vdf_path, "r", encoding="utf-8", errors="ignore") as f:
                content = f.read()
            # Lines look like:  "path"		"D:\\SteamLibrary"
            for match in re.finditer(r'"path"\s*"([^"]+)"', content):
                lib = match.group(1).replace("\\\\", "\\")
                libraries.add(lib)
    except OSError:
        pass

    return _existing(os.path.join(p, "steamapps", "common") for p in libraries)


def get_epic_paths():
    candidates = [
        _reg_str(winreg.HKEY_LOCAL_MACHINE,
                 "SOFTWARE\\WOW6432Node\\Epic Games\\EOS", "ModSdkMetadataDir"),
        os.path.join(_PROGRAM_FILES, "Epic Games"),
    ]
    return _existing(candidates)


def get_gog_paths():
    client_path = _reg_str(
        winreg.HKEY_LOCAL_MACHINE, "SOFTWARE\\WOW6432Node\\GOG.com\\GalaxyClient\\paths", "client"
    )
    candidates = [
        os.path.join(_PROGRAM_FILES_X86, "GOG Galaxy\\Games"),
        os.path.join(_PROGRAM_FILES, "GOG Galaxy\\Games"),
    ]
    if client_path:
        candidates.append(os.path.join(os.path.dirname(client_path), "Games"))
    return _existing(candidates)


def get_other_launcher_paths():
    """Common default install locations for launchers without a reliable
    registry lookup. Best-effort only — users who installed to a custom
    drive/folder won't be picked up automatically."""
    candidates = [
        os.path.join(_PROGRAM_FILES_X86, "Battle.net"),
        os.path.join(_PROGRAM_FILES_X86, "Riot Games"),
        os.path.join(_PROGRAM_FILES, "Riot Games"),
        os.path.join(_PROGRAM_FILES_X86, "Ubisoft\\Ubisoft Game Launcher\\games"),
        os.path.join(_PROGRAM_FILES_X86, "Origin Games"),
        os.path.join(_PROGRAM_FILES, "EA Games"),
    ]
    return _existing(candidates)


def discover_all_game_install_dirs():
    """Return a de-duplicated, sorted list of every game-content folder we
    could find. Never raises — worst case returns an empty list."""
    found = set()
    for getter in (get_steam_library_paths, get_epic_paths, get_gog_paths, get_other_launcher_paths):
        try:
            found.update(getter())
        except Exception:
            continue
    return sorted(found)
