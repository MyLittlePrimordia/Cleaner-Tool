"""Installed-games / installed-apps catalog (Phase-5 pilot overhaul).

Read-only enumeration that powers the Auto-Pilot's "pick from installed
games" flow. Three sources, all headless-testable, all failure-tolerant
(a missing launcher is an empty list, never a crash):

  1. Steam — library roots from the registry + libraryfolders.vdf
     (reuse of storage_scan), then per-game appmanifest_*.acf gives the
     exact installdir; the playable exe is resolved inside it.
  2. Epic — .item manifest files carry LaunchExecutable: the EXACT exe
     name relative to InstallLocation. No heuristic needed.
  3. Registry uninstall hives (HKLM 64/32 + HKCU) — DisplayName +
     DisplayIcon for every installed program: the "installed apps"
     picker source, filtered of redists/SystemComponent noise.

Exe resolution heuristic (Steam + default roots): scan the game folder
top 2 levels for *.exe, excluding helper/crash/redist noise names, then
prefer (a) an exe whose basename echoes the folder/game name, else
(b) the largest exe — the same technique Playnite/Heroic use. Never
returns a path that doesn't exist.

Contract: every entry is {name, path (exe, may be ""), folder, launcher}
and nothing here ever writes, executes, or spawns a subprocess beyond
registry/file reads.
"""

from __future__ import annotations

import glob
import json
import os
import re

# --------------------------------------------------------------------------- #
# Steam: library roots -> per-game appmanifest -> exe resolution
# --------------------------------------------------------------------------- #

# exe basenames that are NEVER the game (helpers, launchers, redists, crash
# reporters, config tools). All lowercase.
_EXE_DENYLIST = (
    "launch.exe", "launcher.exe", "start.exe", "setup.exe", "unins000.exe",
    "unins001.exe", "uninstall.exe", "crashreport.exe", "crashreporter.exe",
    "error.exe", "bugreporter.exe", "dxsetup.exe", "oalsetup", "vc_redist",
    "dotnet", "repair.exe", "config.exe", "settings.exe", "updater.exe",
    "eula.exe", "redist.exe", "support.exe", "steamerrorreporter.exe",
    "steam_api", "cgs.exe", "cef", "unitycrashhandler64.exe",
    "unitycrashhandler32.exe", "crashhandler64.exe", "crashhandler32.exe",
)

# appmanifest "games" that are Steam infrastructure, not games
_STEAM_SKIP_NAMES = {
    "steamworks common redistributables", "steamworks sdk",
    "dedicated server",
}

# default-root folders that are launcher infrastructure, not games
_ROOT_SKIP_NAMES = {"gamesave", "_commonredist", "commonredist"}

_MIN_EXE_BYTES = 512 * 1024   # the playable exe is never a 100 KB stub


def _steam_library_roots():
    """Reuse storage_scan's proven root discovery (registry + vdf)."""
    try:
        from app.storage_scan import _steam_library_roots as _roots
        return list(_roots() or [])
    except Exception:
        return []


def _parse_acf(path):
    """Minimal VDF key/value read of a Steam appmanifest: values the
    pilot needs are flat quoted strings (name, installdir)."""
    out = {}
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            text = f.read()
        for key in ("name", "installdir"):
            m = re.search(r'"%s"\s+"([^"]+)"' % key, text)
            if m:
                out[key] = m.group(1)
    except Exception:
        pass
    return out


def _likely_game_exe(folder, name_hint=""):
    """Best playable-exe candidate inside a game folder (top 2 levels).
    Preference: basename echoes folder/game name -> largest survivor.
    Returns "" when nothing plausible exists (read-only pickers then fall
    back to watching the folder's basename set)."""
    try:
        if not folder or not os.path.isdir(folder):
            return ""
        hint = (name_hint or os.path.basename(folder) or "").lower()
        hint_words = [w for w in re.split(r"[^a-z0-9]+", hint) if len(w) > 2]
        best, best_size, best_score = "", -1, -1
        level_dirs = [folder]
        try:
            with os.scandir(folder) as it:
                level_dirs += [e.path for e in it
                               if e.is_dir(follow_symlinks=False)
                               and not e.name.startswith(("_", "."))]
        except OSError:
            pass
        for d in level_dirs[:25]:
            try:
                with os.scandir(d) as it:
                    for entry in it:
                        try:
                            if not entry.name.lower().endswith(".exe"):
                                continue
                            if not entry.is_file(follow_symlinks=False):
                                continue
                            base = entry.name.lower()
                            # F12: extension-less tokens ("cef", "steam_api",
                            # "dotnet"...) must not substring-match innocent
                            # exes (cefquest.exe); require a boundary.
                            def _denied(b: str) -> bool:
                                if "." in b:
                                    return b in base
                                stem = base[:-4] if base.endswith(".exe") else base
                                return (stem == b or stem.startswith(b + "_")
                                        or stem.startswith(b + "-") or stem.startswith(b + "."))
                            if any(_denied(bad) for bad in _EXE_DENYLIST):
                                continue
                            size = entry.stat(follow_symlinks=False).st_size
                            if size < _MIN_EXE_BYTES:
                                continue
                            score = 0
                            if hint and hint_words and \
                                    any(w in base for w in hint_words):
                                score = 2
                            elif base.startswith(hint) and hint:
                                score = 2
                            if score > best_score or \
                                    (score == best_score and size > best_size):
                                best, best_size, best_score = \
                                    entry.path, size, score
                        except OSError:
                            continue
            except OSError:
                continue
        return best
    except Exception:
        return ""


def steam_games():
    """[{name, launcher:'Steam', folder, path}] for every installed Steam
    game with a resolvable manifest. Empty on any failure."""
    out = []
    for lib in _steam_library_roots():
        apps = os.path.join(lib, "steamapps")
        try:
            manifests = glob.glob(os.path.join(apps, "appmanifest_*.acf"))
        except Exception:
            continue
        for mpath in manifests:
            data = _parse_acf(mpath)
            name = (data.get("name") or "").strip()
            installdir = (data.get("installdir") or "").strip()
            if not name or not installdir:
                continue
            if name.lower() in _STEAM_SKIP_NAMES:
                continue
            folder = os.path.join(apps, "common", installdir)
            if not os.path.isdir(folder):
                continue
            out.append({
                "name": name,
                "launcher": "Steam",
                "folder": folder,
                "path": _likely_game_exe(folder, name),
            })
    return out


# --------------------------------------------------------------------------- #
# Epic: manifest .item files (LaunchExecutable = exact exe)
# --------------------------------------------------------------------------- #

def epic_games():
    """[{name, launcher:'Epic', folder, path}] from Epic's own manifests.
    InstallLocation + LaunchExecutable give the exact exe — no guessing."""
    out = []
    progdata = os.environ.get("ProgramData", r"C:\ProgramData")
    man_dir = os.path.join(progdata, "Epic", "EpicGamesLauncher",
                           "Data", "Manifests")
    try:
        names = glob.glob(os.path.join(man_dir, "*.item"))
    except Exception:
        return out
    for fn in names:
        try:
            with open(fn, "r", encoding="utf-8", errors="replace") as f:
                data = json.load(f)
            name = (data.get("DisplayName") or "").strip()
            loc = (data.get("InstallLocation") or "").strip()
            exe = (data.get("LaunchExecutable") or "").strip()
            if not name or not loc or not os.path.isdir(loc):
                continue
            path = os.path.join(loc, exe) if exe else ""
            if path and not os.path.isfile(path):
                path = ""
            out.append({"name": name, "launcher": "Epic",
                        "folder": loc, "path": path})
        except Exception:
            continue
    return out


# --------------------------------------------------------------------------- #
# Other launchers: default install roots (folders, exe via heuristic)
# --------------------------------------------------------------------------- #

def root_games():
    """GOG / Ubisoft / Riot / Xbox / EA default-root games with the same
    {name, launcher, folder, path} shape (path may be "" when the exe
    can't be confidently identified — watching falls back to basenames
    in that case)."""
    out = []
    pf = os.environ.get("ProgramFiles", r"C:\Program Files")
    pf86 = os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")
    sysdrive = os.environ.get("SYSTEMDRIVE", "C:") + "\\"
    roots = [
        ("GOG", os.path.join(sysdrive, "GOG Games")),
        ("Ubisoft", os.path.join(pf86, "Ubisoft", "Ubisoft Game Launcher",
                                "games")),
        ("Riot", os.path.join(sysdrive, "Riot Games")),
        ("Xbox", os.path.join(sysdrive, "XboxGames")),
        ("EA", os.path.join(pf, "EA Games")),
    ]
    for launcher, root in roots:
        try:
            if not os.path.isdir(root):
                continue
            with os.scandir(root) as it:
                for entry in it:
                    try:
                        if not entry.is_dir(follow_symlinks=False):
                            continue
                        if entry.name.lower() in _ROOT_SKIP_NAMES:
                            continue
                        out.append({
                            "name": entry.name,
                            "launcher": launcher,
                            "folder": entry.path,
                            "path": _likely_game_exe(entry.path),
                        })
                    except OSError:
                        continue
        except OSError:
            continue
    return out


def installed_games():
    """All discovered games, every launcher, sorted by name. Deduped by
    folder. Entries keep their {name, launcher, folder, path} shape —
    `path` may be "" (unresolvable): watchers then use the folder's
    whole basename set instead of one exe (see session_pilot)."""
    seen = set()
    out = []
    for src in (steam_games, epic_games, root_games):
        try:
            for g in src() or []:
                key = os.path.normcase(os.path.abspath(g.get("folder", "")))
                if key in seen:
                    continue
                seen.add(key)
                out.append(g)
        except Exception:
            continue
    out.sort(key=lambda g: (g.get("name") or "").lower())
    return out


# --------------------------------------------------------------------------- #
# Installed apps: registry uninstall hives (the "apps" picker source)
# --------------------------------------------------------------------------- #

_NOISE_PATTERNS = (
    "microsoft visual c+", "vc++ redist", " redistributable",
    "dotnet", ".net framework", "runtime", "driver", "update for",
    "windows software development kit", "sdk", "language pack",
    "security update", "hotfix", "windows installer", "microsoft edge",
    "intel(r)", "nvidia", "amd ", "realtek",
    "maintenance service", "installer", "anti-cheat",
)

# exe basenames never acceptable as an app's runnable target
_APP_EXE_DENYLIST = (
    "unins000.exe", "unins001.exe", "uninstall.exe", "setup.exe",
    "update.exe", "updater.exe", "install.exe", "uninstallhelper.exe",
)

# folders whose DisplayIcons are cached installers, not runnable apps
_APP_PATH_DENYLIST = ("package cache", "windows\\installer")

_UNINSTALL_HIVES = (
    (r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall", None),
    (r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall", None),
    (r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall", "HKCU"),
)


def installed_apps():
    """[{name, launcher:'App', folder, path}] for installed programs from
    the registry uninstall hives. path comes from DisplayIcon (trimmed of
    the ,N icon-index suffix); folder from InstallLocation when present.
    Redists/driver noise filtered out. Read-only, failure-tolerant."""
    out = []
    seen = set()
    try:
        import winreg
    except Exception:
        return out
    for subkey_path, hive_host in _UNINSTALL_HIVES:
        try:
            if hive_host == "HKCU":
                hive = winreg.HKEY_CURRENT_USER
            else:
                hive = winreg.HKEY_LOCAL_MACHINE
            with winreg.OpenKey(hive, subkey_path) as hive_key:
                i = 0
                while True:
                    try:
                        sub = winreg.EnumKey(hive_key, i)
                        i += 1
                    except OSError:
                        break
                    try:
                        with winreg.OpenKey(hive_key, sub) as k:
                            def _val(name):
                                try:
                                    v, _t = winreg.QueryValueEx(k, name)
                                    return str(v or "").strip()
                                except OSError:
                                    return ""
                            name = _val("DisplayName")
                            if not name:
                                continue
                            try:
                                syscomp, _t = winreg.QueryValueEx(
                                    k, "SystemComponent")
                                if int(syscomp) == 1:
                                    continue
                            except OSError:
                                pass
                            lname = name.lower()
                            if any(p in lname for p in _NOISE_PATTERNS):
                                continue
                            icon = _val("DisplayIcon")
                            path = ""
                            if icon:
                                path = icon.split(",")[0].strip().strip('"')
                                base = os.path.basename(path).lower()
                                lp = path.lower()
                                if (not path.lower().endswith(".exe")
                                        or not os.path.isfile(path)
                                        or base in _APP_EXE_DENYLIST
                                        or any(d in lp
                                               for d in _APP_PATH_DENYLIST)):
                                    path = ""
                            if not path:
                                continue      # no runnable exe -> not pickable
                            loc = _val("InstallLocation")
                            if loc and not os.path.isdir(loc):
                                loc = ""
                            if path in seen:
                                continue
                            seen.add(path)
                            out.append({"name": name, "launcher": "App",
                                        "folder": loc or
                                        os.path.dirname(path),
                                        "path": path})
                    except OSError:
                        continue
        except OSError:
            continue
    out.sort(key=lambda g: g["name"].lower())
    return out


# --------------------------------------------------------------------------- #
# Full uninstall-capable program list (Uninstall Programs dialog + orphan
# leftover scanner). Includes entries that have no runnable DisplayIcon —
# those are still uninstallable via UninstallString.
# --------------------------------------------------------------------------- #

def list_installed_programs():
    """[{name, publisher, uninstall, quiet, location, size_kb, key_path,
    hive, icon}] for every uninstall registry entry that has a DisplayName
    and an UninstallString (or QuietUninstallString). SystemComponent=1 and
    common redist/noise names are filtered. Read-only, failure-tolerant.

    key_path is the full relative path under the hive (for leftover
    ghost-entry cleanup after a successful uninstall). hive is 'HKLM' or
    'HKCU'. size_kb is EstimatedSize when present (KB), else 0. icon is the
    raw DisplayIcon value ('path' or 'path,index') when it points at a file
    that exists on disk, else ''.
    """
    out = []
    seen_names = set()
    try:
        import winreg
    except Exception:
        return out

    hives = (
        (winreg.HKEY_LOCAL_MACHINE,
         r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall", "HKLM"),
        (winreg.HKEY_LOCAL_MACHINE,
         r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall",
         "HKLM"),
        (winreg.HKEY_CURRENT_USER,
         r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall", "HKCU"),
    )

    for hive, subkey_path, hive_label in hives:
        try:
            with winreg.OpenKey(hive, subkey_path) as hive_key:
                i = 0
                while True:
                    try:
                        sub = winreg.EnumKey(hive_key, i)
                        i += 1
                    except OSError:
                        break
                    try:
                        with winreg.OpenKey(hive_key, sub) as k:
                            def _val(name):
                                try:
                                    v, _t = winreg.QueryValueEx(k, name)
                                    return str(v or "").strip()
                                except OSError:
                                    return ""

                            name = _val("DisplayName")
                            if not name:
                                continue
                            try:
                                syscomp, _t = winreg.QueryValueEx(
                                    k, "SystemComponent")
                                if int(syscomp) == 1:
                                    continue
                            except OSError:
                                pass
                            # ParentKeyName marks nested MSI components
                            try:
                                parent = _val("ParentKeyName")
                                if parent:
                                    continue
                            except Exception:
                                pass
                            lname = name.lower()
                            if any(p in lname for p in _NOISE_PATTERNS):
                                continue
                            uninstall = _val("UninstallString")
                            quiet = _val("QuietUninstallString")
                            if not uninstall and not quiet:
                                continue
                            publisher = _val("Publisher")
                            location = _val("InstallLocation")
                            if location and not os.path.isdir(location):
                                location = ""
                            size_kb = 0
                            try:
                                raw, _t = winreg.QueryValueEx(k, "EstimatedSize")
                                size_kb = int(raw or 0)
                            except (OSError, ValueError, TypeError):
                                pass
                            # DisplayIcon (path[,index]) so icon_extract can
                            # hand it straight to ExtractIconExW — this is
                            # exactly what Control Panel itself shows, so
                            # unlike installed_apps() we do NOT filter out
                            # uninstaller exes here (Inno/NSIS uninstallers
                            # commonly reuse the app's real icon resource).
                            # DisplayIcon very often carries an unexpanded
                            # env var (%SystemRoot%\..., %ProgramFiles%\...
                            # — common for Windows Installer / system-
                            # component entries); ExtractIconExW needs a
                            # literal path, so expand + re-attach the index
                            # once here rather than at every call site.
                            icon = ""
                            raw_icon = _val("DisplayIcon")
                            if raw_icon:
                                # rpartition (not partition): when there IS
                                # an index, it's always the last comma-
                                # separated token, and a folder name can
                                # itself contain a comma (e.g. "C:\...\Foo,
                                # Inc\app.exe,0"). But that same ambiguity
                                # means a comma-in-folder-name path with NO
                                # index (e.g. "C:\...\Foo, Inc\app.exe")
                                # would wrongly look like "...\Foo" + index
                                # "Inc\app.exe" — only trust the split when
                                # the tail is actually a bare integer;
                                # otherwise there's no index and the comma
                                # was always part of the path.
                                head, _comma, tail = raw_icon.rpartition(",")
                                tail = tail.strip()
                                if _comma and tail.lstrip("-").isdigit():
                                    path_part, index_part = head, tail
                                else:
                                    path_part, index_part = raw_icon, ""
                                icon_path = os.path.expandvars(
                                    path_part.strip().strip('"'))
                                if icon_path and os.path.isfile(icon_path):
                                    icon = icon_path
                                    if index_part:
                                        icon += "," + index_part
                            # Dedup by DisplayName (prefer entry that has
                            # InstallLocation / larger size)
                            key = lname
                            if key in seen_names:
                                # keep the richer entry
                                for existing in out:
                                    if existing["name"].lower() == key:
                                        if (not existing["location"]
                                                and location):
                                            existing["location"] = location
                                        if size_kb > existing["size_kb"]:
                                            existing["size_kb"] = size_kb
                                        if not existing["quiet"] and quiet:
                                            existing["quiet"] = quiet
                                        if not existing.get("icon") and icon:
                                            existing["icon"] = icon
                                        break
                                continue
                            seen_names.add(key)
                            out.append({
                                "name": name,
                                "publisher": publisher,
                                "uninstall": uninstall,
                                "quiet": quiet,
                                "location": location,
                                "size_kb": size_kb,
                                "key_path": f"{subkey_path}\\{sub}",
                                "hive": hive_label,
                                "icon": icon,
                            })
                    except OSError:
                        continue
        except OSError:
            continue
    out.sort(key=lambda p: p["name"].lower())
    return out


def installed_display_names():
    """Lowercased DisplayName set for every uninstall entry (including
    SystemComponent / noise). Used by the orphan-leftover cleaner so we
    never treat a still-registered program folder as orphaned."""
    names = set()
    try:
        import winreg
    except Exception:
        return names
    hives = (
        (winreg.HKEY_LOCAL_MACHINE,
         r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"),
        (winreg.HKEY_LOCAL_MACHINE,
         r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall"),
        (winreg.HKEY_CURRENT_USER,
         r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"),
    )
    for hive, subkey_path in hives:
        try:
            with winreg.OpenKey(hive, subkey_path) as hive_key:
                i = 0
                while True:
                    try:
                        sub = winreg.EnumKey(hive_key, i)
                        i += 1
                    except OSError:
                        break
                    try:
                        with winreg.OpenKey(hive_key, sub) as k:
                            try:
                                v, _t = winreg.QueryValueEx(k, "DisplayName")
                                name = str(v or "").strip()
                                if name:
                                    names.add(name.lower())
                            except OSError:
                                pass
                    except OSError:
                        continue
        except OSError:
            continue
    return names
