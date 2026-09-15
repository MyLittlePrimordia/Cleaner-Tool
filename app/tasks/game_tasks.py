"""
Games tab — merged mega-tasks: one checkbox per concern, giant verified path lists.

Tasks:
  * Clean Gamer Launchers — every launcher store + chat clients (web caches, logs)
   * Clean Game Files — verified per-game junk paths (logs/crashes/shader caches
     for top titles, Minecraft, CurseForge, OBS, peripherals) plus a generalized
     Unity Player.log sweep covering the indie long tail
  * Clean GPU Shader Caches — DirectX/NVIDIA/AMD/Intel/Steam shader caches
  * Clean Game Captures — Game DVR clip folder
"""

import os

from app.utils import TaskContext, clean_folder_contents
from app.tasks import launcher_paths as _lp
from app.tasks.launcher_paths import (
    GAMER_LAUNCHER_ALL,
    GAME_FILES_ALL,
    GPU_SHADER_CACHE_ALL,
    GAME_CAPTURES_PATHS,
    MINECRAFT_LAUNCHER_LOGS,
    LOW_GAME_PLAYER_LOGS,
    UNITY_PLAYER_LOGS,
)


def _desktop_dir() -> str:
    """Real Desktop path, respecting OneDrive/known-folder redirection.
    USERPROFILE\\Desktop is wrong on OneDrive-redirected machines — read the
    shell folder value and expandvars it instead."""
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                            r"Software\Microsoft\Windows\CurrentVersion\Explorer\User Shell Folders") as k:
            raw, _ = winreg.QueryValueEx(k, "Desktop")
            return os.path.expandvars(raw)
    except OSError:
        return os.path.join(os.environ.get("USERPROFILE", ""), "Desktop")


def _shell_dir(name: str, fallback: str) -> str:
    """Known-folder path with OneDrive redirection (F-4): Documents is
    OneDrive-redirected by default on new Win11 profiles, so a saves backup
    built from USERPROFILE\\Documents would silently miss My Games. Same
    technique as _desktop_dir, with the given profile-relative fallback."""
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                            r"Software\Microsoft\Windows\CurrentVersion\Explorer\User Shell Folders") as k:
            raw, _ = winreg.QueryValueEx(k, name)
            resolved = os.path.expandvars(raw)
            if resolved and os.path.isabs(resolved):
                return resolved
    except OSError:
        pass
    return fallback


def backup_game_saves(ctx: TaskContext):
    """Zip known save locations to the Desktop, timestamped. Read-only on
    sources; creates ONE file; deletes nothing — the safety net that pairs
    with the app's 'never touch saves' cleaning rule.

    Returns None (not bytes) on purpose: the Clean-tab runner sums int
    returns as 'space freed', and a backup CREATES bytes — counting it in
    would corrupt the freed-space total."""
    import zipfile
    import shutil
    from datetime import datetime

    sources = [
        # F-4: resolve the real known-folder paths — OneDrive-redirected
        # profiles keep Saved Games / Documents under OneDrive, and a
        # USERPROFILE-relative path would silently miss the saves this
        # backup exists to protect.
        (_shell_dir("{4C5C32FF-BB9D-43B0-B5B4-2D72E54EAAA4}",
                    os.path.join(os.environ.get("USERPROFILE", ""), "Saved Games")), "Saved Games"),
        (os.path.join(_shell_dir("Personal",
                                 os.path.join(os.environ.get("USERPROFILE", ""), "Documents")),
                      "My Games"), "My Games"),
        (os.path.join(os.environ.get("APPDATA", ""), ".minecraft", "saves"), "Minecraft"),
        # extend with verified per-game roots only, per launcher_paths.py's
        # 'verified live' policy: a wrong folder just bloats the zip, but a
        # missing folder means silently unprotected saves.
    ]
    sources = [(p, tag) for p, tag in sources if p and os.path.isdir(p)]
    if not sources:
        raise RuntimeError("No save folders found — nothing to back up.")

    # 1) size estimate + free-space guard (uncompressed size = safe upper bound)
    total = 0
    for path, _tag in sources:
        for root, _dirs, files in os.walk(path):
            for f in files:
                try:
                    total += os.path.getsize(os.path.join(root, f))
                except OSError:
                    pass
    desktop = _desktop_dir()
    try:
        free = shutil.disk_usage(desktop).free
    except OSError as exc:
        raise RuntimeError(f"Could not check free space on your Desktop drive: {exc}")
    if free < total:
        from app.utils import format_bytes
        raise RuntimeError(
            f"Not enough space on your Desktop drive (need ~{format_bytes(total)}, "
            f"have {format_bytes(free)})."
        )

    # 2) zip
    stamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")
    dest = os.path.join(desktop, f"GameSavesBackup_{stamp}.zip")
    ctx.set_status(f"Backing up game saves to {os.path.basename(dest)}...")
    count, skipped = 0, 0
    with zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED) as zf:
        for path, tag in sources:
            ctx.log(f"  Adding: {path}")
            for root, _dirs, files in os.walk(path):
                if ctx.cancelled():
                    ctx.log("  ! Cancelled — partial zip kept on Desktop.")
                    return None
                for f in files:
                    fp = os.path.join(root, f)
                    try:
                        zf.write(fp, os.path.join(tag, os.path.relpath(fp, path)))
                        count += 1
                    except OSError:
                        skipped += 1
    if skipped:
        ctx.log(f"  (skipped {skipped} locked/inaccessible files — close running games for a full backup)")
    ctx.log(f"Backup complete: {count} files -> {dest}")
    return None


def _clean_many(ctx: TaskContext, folders, label):
    total = 0
    for folder in folders:
        if not folder or not os.path.isabs(folder):
            continue
        if os.path.exists(folder):
            ctx.log(f"Cleaning {label}: {folder}")
            total += clean_folder_contents(ctx, folder)
    return total


def _clean_files(ctx: TaskContext, files, label):
    """Remove individual files (e.g. launcher_log.txt) and count freed bytes."""
    from app.utils import _is_reparse_point as _is_rp
    dry = bool(getattr(ctx, "dry_run", False))
    total = 0
    for f in files:
        # F-3: honor Stop here too — the Unity Player.log sweep can list
        # hundreds of files on Unity-heavy machines; leave the rest in place.
        if ctx.cancelled():
            break
        if not f or not os.path.isabs(f) or not os.path.isfile(f):
            continue
        # F04 guard: never delete through a reparse point (junction/symlink
        # file behind an attacker-planted link).
        try:
            if _is_rp(f):
                continue
        except Exception:
            continue
        try:
            st = os.stat(f)
            if not dry:
                os.remove(f)
            total += st.st_size
            if not dry:
                ctx.log(f"Cleaning {label}: {f}")
        except OSError:
            continue
    return total


def clean_gamer_launchers(ctx: TaskContext):
    # M5 fix: log line now matches GAMER_LAUNCHER_ALL's real contents
    # (Slack/Teams/Spotify are actually included now, see launcher_paths.py).
    # M12: refresh glob/listdir discoveries at clean time (lists mutate in
    # place, so the imported names below observe the refresh).
    _lp.refresh_dynamic_paths()
    ctx.log("Cleaning gamer launchers (Steam, Epic, EA, GOG, Battle.net, Riot, Ubisoft,")
    ctx.log("Discord, Xbox, Rockstar, Amazon, itch, Humble, Wargaming, Nexon, Slack, Teams, Spotify)")
    return _clean_many(ctx, GAMER_LAUNCHER_ALL, "launcher cache")


def clean_game_files(ctx: TaskContext):
    """The mega task: junk from verified top titles + Minecraft + CurseForge
    + OBS + peripherals + a generalized Unity-engine log sweep.

    Only touches logs, crash dumps, web caches and shader caches — never saves.
    """
    # M12: fresh Unity/LocalLow + Minecraft discoveries (in-place refresh).
    _lp.refresh_dynamic_paths()
    ctx.log("Cleaning game files (verified top titles, Minecraft, CurseForge, OBS,")
    ctx.log("Streamlabs, Logitech G Hub, Razer, Roblox, Unreal/Unity engine caches,")
    ctx.log("Unity Player.log sweep)")
    total = _clean_many(ctx, GAME_FILES_ALL, "game files")
    # Minecraft launcher logs are loose files in the .minecraft root
    total += _clean_files(ctx, MINECRAFT_LAUNCHER_LOGS, "Minecraft launcher log")
    # M3 audit fix: the Valheim/RimWorld/Schedule I 'Player.log' entries
    # are single files too (see launcher_paths.LOW_GAME_PLAYER_LOGS) — they
    # used to ride in the directory list above, where os.walk over a file
    # path matched nothing and the log claimed cleaning anyway. Route them
    # through the file-based cleaner like the Minecraft launcher logs.
    total += _clean_files(ctx, LOW_GAME_PLAYER_LOGS, "game Player.log")
    # Generalized Unity sweep (launcher_paths.UNITY_PLAYER_LOGS): every
    # Player.log / Player-prev.log under LocalLow — the indie long tail
    # with no per-game table rows. Overlaps LOW_GAME_PLAYER_LOGS for the
    # three titles above; _clean_files skips already-removed files quietly.
    total += _clean_files(ctx, UNITY_PLAYER_LOGS, "Unity Player.log")
    return total


def clean_gpu_shader_caches(ctx: TaskContext):
    """All GPU vendor shader caches + Steam per-game shader cache + GeForce/AMD app caches.

    M2 fix: now shares launcher_paths.GPU_SHADER_CACHE_ALL with
    clean_tasks.clean_shader_cache instead of assembling its own slightly
    different path list, so the two "shader cache" tasks can no longer
    silently drift apart.
    """
    ctx.log("Cleaning shader caches (DirectX, NVIDIA, AMD, Intel, Steam per-game)")
    return _clean_many(ctx, GPU_SHADER_CACHE_ALL, "shader cache")


def clean_game_captures(ctx: TaskContext):
    """Game DVR / Xbox Game Bar recorded clips."""
    ctx.log("Cleaning game captures (Videos\\Captures)")
    return _clean_many(ctx, GAME_CAPTURES_PATHS, "game captures")


def _process_running(image: str) -> bool:
    """True if a process with this image name is currently running
    (active game/launcher files may be mid-write — callers skip instead of
    touching live data; same shape as the Steam guard below)."""
    try:
        import subprocess as _sp
        out = _sp.check_output(
            f'tasklist /fi "imagename eq {image}" /fo csv /nh',
            shell=True, text=True, timeout=10, stderr=_sp.DEVNULL,
            creationflags=getattr(_sp, "CREATE_NO_WINDOW", 0),
        )
        return image.lower() in (out or "").lower()
    except Exception:
        return False


def clean_vrchat_cache(ctx: TaskContext):
    """Clear VRChat's downloaded-content cache (avatars, worlds, images —
    default 20 GB cap, the classic VRChat disk hog).

    Source: VRChat's own local-storage docs (docs.vrchat.com,
    'Local VRChat Storage'): `%LocalLow%\\VRChat\\VRChat\\Cache-WindowsPlayer`
    is the asset download cache; `config.json`, `LocalAvatarData`,
    `LocalPlayerModerations` (saves/moderation data), `Avatars` (local test
    avatars) and `OSC` are settings/user data and are NEVER touched — only
    the cache folder goes. A relocated cache (`cache_directory` in the
    game's own config.json, read-only parse) is honored too.
    Redownloads on demand; safe to run any time."""
    import json as _json
    local_low = _lp.LOCALLOW if hasattr(_lp, "LOCALLOW") else ""
    if not local_low:
        userprofile = os.environ.get("USERPROFILE", "")
        local_low = os.path.join(userprofile, "AppData", "LocalLow") if userprofile else ""
    base = os.path.join(local_low, "VRChat", "VRChat") if local_low else ""
    if not base or not os.path.isdir(base):
        ctx.log("VRChat data folder not found — nothing to do.")
        return 0
    targets = [os.path.join(base, "Cache-WindowsPlayer")]
    # Relocated cache (the game's own config may point anywhere, even another
    # drive): read-only parse, absolute dirs only, junctions still skipped by
    # the walker (a symlinked custom dir is left alone, never followed).
    try:
        with open(os.path.join(base, "config.json"), "r", encoding="utf-8") as f:
            custom = (_json.load(f) or {}).get("cache_directory", "")
        if isinstance(custom, str) and custom.strip() and os.path.isabs(custom):
            norm = os.path.normpath(custom.strip())
            # F16: cache_directory is game-writable — refuse system roots
            # (drive root / Windows / profile root); entry-junction guard
            # in clean_folder_contents covers the symlink case.
            try:
                croot = os.path.splitdrive(norm)[0] + os.sep
                cwindir = os.environ.get("WINDIR", "")
                cprofile = os.environ.get("USERPROFILE", "")
                if norm.lower() in (croot.lower(), cwindir.lower() if cwindir else "", cprofile.lower() if cprofile else ""):
                    ctx.log(f"VRChat custom cache looks unsafe — skipping: {norm}")
                elif norm not in targets:
                    targets.append(norm)
                    ctx.log(f"VRChat uses a relocated cache: {norm}")
            except Exception:
                pass
    except (OSError, ValueError):
        pass
    total = _clean_many(ctx, [t for t in targets if os.path.isdir(t)],
                        "VRChat cache")
    if total:
        ctx.log("VRChat cache cleared — avatars and worlds redownload as you visit them.")
    return total


def clean_fivem_cache(ctx: TaskContext):
    """Clear FiveM's client download caches (server assets that pile up to
    tens of GB and the classic fix for broken textures / loading loops).

    Community-verified layout (Cfx.re forums + client guides):
    `%LOCALAPPDATA%\\FiveM\\FiveM.app\\data\\` holds `cache`,
    `server-cache`, `server-cache-priv` (and the newer `server-cache-priv-cl2`)
    — downloaded server assets, safe to empty. `game-storage` (the downloaded
    game build — GBs to refetch), `CitizenFX.ini` and everything else are
    deliberately left alone. Characters and saves live server-side, so
    clearing never loses progress.

    Safety: skips entirely while FiveM is running (locked cache databases,
    active session). Default FiveM location only — a portable install
    elsewhere is left alone with an honest log line."""
    ctx.set_status("Cleaning FiveM download caches...")
    if _process_running("FiveM.exe"):
        ctx.log("FiveM is running — skipping to avoid touching the live session.")
        return 0
    localappdata = os.environ.get("LOCALAPPDATA", "")
    data = os.path.join(localappdata, "FiveM", "FiveM.app", "data") if localappdata else ""
    if not data or not os.path.isdir(data):
        ctx.log("FiveM data folder not found at its default location — nothing to do.")
        return 0
    total = 0
    for sub in ("cache", "server-cache", "server-cache-priv", "server-cache-priv-cl2"):
        folder = os.path.join(data, sub)
        if os.path.isdir(folder):
            ctx.log(f"Cleaning FiveM cache: {folder}")
            total += clean_folder_contents(ctx, folder)
    if total:
        ctx.log("FiveM caches cleared — servers redownload their assets on next join "
                "(first join takes longer; characters and saves are server-side and safe).")
    else:
        ctx.log("FiveM caches already empty.")
    return total


# Star Citizen per-channel USER preserves (first-party RSI launcher
# "Delete Local Settings" contract): keybindings, settings profile, custom
# characters and screenshots survive; everything else under USER (shaders,
# logs, stale state — the patch-day crash fix) goes.
_SC_USER_PRESERVE_DIRS = ("controls", "profiles", "customcharacters")


def _sc_user_clean_targets(user_dir: str):
    """Yield (path, is_dir) entries under a Star Citizen USER folder that are
    safe to clean: everything EXCEPT the preserved names below. Pure function
    of the directory listing (unit-tested with fixtures) — the caller does
    the deleting through the junction-guarded cleaners.

    Preserved (never yielded):
      * Controls / Profiles / CustomCharacters (keybindings, settings,
        custom characters — the launcher's own preservation list)
      * any directory whose name contains 'screenshot' (player captures)
      * any file named user.cfg (custom graphics/settings overrides)
    """
    try:
        entries = sorted(os.listdir(user_dir))
    except OSError:
        return
    for entry in entries:
        full = os.path.join(user_dir, entry)
        try:
            if os.path.isdir(full) and not os.path.islink(full):
                low = entry.lower()
                if low in _SC_USER_PRESERVE_DIRS:
                    continue
                if "screenshot" in low:
                    continue
                yield full, True
            elif os.path.isfile(full):
                if entry.lower() == "user.cfg":
                    continue
                yield full, False
        except OSError:
            continue


def clean_starcitizen_cache(ctx: TaskContext):
    """Clear Star Citizen's shader + USER caches — RSI's own patch-day fix
    for crashes and broken graphics after updates.

    Sources (first-party): the RSI launcher's 'Delete Local Settings'
    definition + support KB 'delete the USER folder' guidance:
      * `%LOCALAPPDATA%\\Star Citizen` shaders, EXCEPT the `Crashes`
        subfolder (crash dumps belong to support tickets — never touched)
      * `%APPDATA%\\rsilauncher\\Cache` + `GPUCache` (launcher web caches,
        per the launcher-troubleshooting KB)
      * `<install>\\<LIVE|PTU|EPTU>\\USER` with the preserved set above
        (keybindings, settings profile, custom characters, screenshots,
        user.cfg all survive)

    Default install location only — a custom library drive is left alone
    with an honest log line (no guessing). Needs admin (Program Files)."""
    from app.utils import _is_reparse_point
    ctx.set_status("Cleaning Star Citizen caches...")
    total = 0
    appdata = os.environ.get("APPDATA", "")
    localappdata = os.environ.get("LOCALAPPDATA", "")
    # 1) launcher web caches (per-user, no admin needed for these two)
    if appdata:
        for sub in (os.path.join("rsilauncher", "Cache"),
                    os.path.join("rsilauncher", "GPUCache")):
            folder = os.path.join(appdata, sub)
            if os.path.isdir(folder):
                ctx.log(f"Cleaning RSI Launcher cache: {folder}")
                total += clean_folder_contents(ctx, folder)
    # 2) local shader cache — everything except Crashes (CIG's own carve-out)
    if localappdata:
        shader_root = os.path.join(localappdata, "Star Citizen")
        if os.path.isdir(shader_root):
            try:
                names = sorted(os.listdir(shader_root))
            except OSError:
                names = []
            for name in names:
                if ctx.cancelled():
                    break
                if name.lower() == "crashes":
                    continue  # crash dumps stay for support tickets
                full = os.path.join(shader_root, name)
                if os.path.isdir(full) and not _is_reparse_point(full):
                    ctx.log(f"Cleaning Star Citizen shaders: {full}")
                    total += clean_folder_contents(ctx, full)
    # 3) per-channel USER folders (default install location)
    pf = os.environ.get("ProgramFiles", r"C:\Program Files")
    game_root = os.path.join(pf, "Roberts Space Industries", "StarCitizen")
    if not os.path.isdir(game_root):
        ctx.log(f"Star Citizen install not found at {game_root} — "
                "custom library drives are left alone.")
    else:
        for channel in ("LIVE", "PTU", "EPTU"):
            user_dir = os.path.join(game_root, channel, "USER")
            if not os.path.isdir(user_dir):
                continue
            ctx.log(f"Cleaning Star Citizen {channel} USER cache "
                    "(keybindings, settings, characters and screenshots kept)...")
            for path, is_dir in _sc_user_clean_targets(user_dir):
                if ctx.cancelled():
                    break
                if _is_reparse_point(path):
                    continue
                try:
                    if is_dir:
                        total += clean_folder_contents(ctx, path, remove_root=True)
                    else:
                        size = os.path.getsize(path)
                        os.remove(path)
                        total += size
                except OSError:
                    continue
    if total:
        ctx.log("Star Citizen caches cleared — shaders rebuild on next launch.")
    else:
        ctx.log("No Star Citizen caches found to clean.")
    return total


def _localappdata() -> str:
    """%LOCALAPPDATA% with a profile-relative fallback (same shape as the
    VRChat helper below — env-var-first, never a hardcoded profile)."""
    la = os.environ.get("LOCALAPPDATA", "")
    if la:
        return la
    up = os.environ.get("USERPROFILE", "")
    return os.path.join(up, "AppData", "Local") if up else ""


def clean_riot_logs(ctx: TaskContext):
    """Clear VALORANT + Riot Client diagnostic logs (Unreal crash logs,
    ShooterGame.log archives, timestamped Riot Client logs — GBs of
    append-only text the games rewrite on every launch).

    Sources: Riot support's log-collection paths
    (%LOCALAPPDATA%\\VALORANT\\Saved\\Logs, %LOCALAPPDATA%\\Riot Games\\
    Riot Client\\Logs). Only *.log/*.dmp inside those log folders —
    Saved\\Config (keybinds, crosshair, settings) and
    RiotGamesPrivateSettings.yaml (login tokens) are never touched."""
    ctx.set_status("Cleaning Riot Games logs...")
    la = _localappdata()
    if not la:
        ctx.log("LocalAppData not found — nothing to do.")
        return 0
    roots = [
        os.path.join(la, "VALORANT", "Saved", "Logs"),
        os.path.join(la, "VALORANT", "Saved", "Crashes"),
        os.path.join(la, "Riot Games", "Riot Client", "Logs"),
    ]
    total = 0
    found = False
    for folder in roots:
        if os.path.isdir(folder):
            found = True
            ctx.log(f"Cleaning Riot logs: {folder}")
            total += clean_folder_contents(ctx, folder, extensions=[".log", ".dmp"])
    if not found:
        ctx.log("No Riot Games logs found — nothing to do.")
    elif total:
        ctx.log("Riot logs cleared — launchers rewrite fresh ones next start.")
    return total


def clean_hoyoverse_logs(ctx: TaskContext):
    """Clear HoYoverse crash/output logs (output_log.txt + crash dumps
    under the vendors' LocalLow folders — HoYoverse support's own
    requested files when Genshin crashes; the game rewrites them).

    Only those exact log/dump names — screenshots, configs and game
    data next to them are never touched. (Player.log here is already
    covered by the Unity sweep; this adds output_log.txt + dumps.)"""
    import glob as _glob
    ctx.set_status("Cleaning HoYoverse logs...")
    userprofile = os.environ.get("USERPROFILE", "")
    low = os.path.join(userprofile, "AppData", "LocalLow") if userprofile else ""
    if not low or not os.path.isdir(low):
        ctx.log("LocalLow folder not found — nothing to do.")
        return 0
    found: list[str] = []
    for vendor in ("miHoYo", "Cognosphere", "HoYoverse"):
        vdir = os.path.join(low, vendor)
        if not os.path.isdir(vdir):
            continue
        for pat in ("output_log*.txt", "*.dmp", "crash*.log"):
            try:
                for hit in _glob.glob(os.path.join(vdir, "**", pat), recursive=True):
                    if os.path.isfile(hit):
                        found.append(hit)
            except Exception:
                continue
    found = list(dict.fromkeys(f for f in found if f))
    if not found:
        ctx.log("No HoYoverse logs found — nothing to do.")
        return 0
    return _clean_files(ctx, found, "HoYoverse logs")


def clean_rockstar_logs(ctx: TaskContext):
    """Clear Rockstar Games Launcher web cache + logs (%LOCALAPPDATA%\\
    Rockstar Games\\Launcher\\{cache,logs} — Chromium/HTTP/web-asset
    debris plus append-only launcher logs; the launcher rebuilds them).

    Only those two subfolders — settings_*.dat (account/launcher
    settings), CrashLogs state and game installs are never touched.
    Documents\\Rockstar Games is deliberately excluded (game settings
    live there)."""
    ctx.set_status("Cleaning Rockstar Launcher cache...")
    la = _localappdata()
    if not la:
        ctx.log("LocalAppData not found — nothing to do.")
        return 0
    base = os.path.join(la, "Rockstar Games", "Launcher")
    if not os.path.isdir(base):
        ctx.log("Rockstar Games Launcher data not found — nothing to do.")
        return 0
    total = _clean_many(ctx, [os.path.join(base, "cache"),
                              os.path.join(base, "logs")],
                        "Rockstar Launcher cache")
    if total:
        ctx.log("Rockstar Launcher cache cleared — it rebuilds on next launch.")
    return total


def clean_roblox_logs(ctx: TaskContext):
    """Clear Roblox client logs (%LOCALAPPDATA%\\Roblox\\logs\\*.log —
    famously chatty per-session logs; the client opens fresh ones).

    Only *.log inside logs\\ — versions\\ (player executables) and
    LocalStorage (login/session data) are never touched."""
    ctx.set_status("Cleaning Roblox logs...")
    la = _localappdata()
    if not la:
        ctx.log("LocalAppData not found — nothing to do.")
        return 0
    folder = os.path.join(la, "Roblox", "logs")
    if not os.path.isdir(folder):
        ctx.log("Roblox logs not found — nothing to do.")
        return 0
    ctx.log(f"Cleaning Roblox logs: {folder}")
    total = clean_folder_contents(ctx, folder, extensions=[".log"])
    if total:
        ctx.log("Roblox logs cleared — fresh ones open with the next session.")
    return total


def clean_steam_stuck_downloads(ctx: TaskContext):
    """Remove Steam's orphaned staging files from failed/paused/cancelled
    updates (steamapps\\downloading + steamapps\\temp). These can pile up
    to tens of GB and Steam never cleans them on its own.

    Safety: skips entirely if Steam is running, so an active download is
    never corrupted; finds the install root from Steam's own registry key;
    touches ONLY those two staging folders — never games or saves.
    (Lives here in game_tasks because it's gamer-specific; surfaced on the
    merged Clean tab via tab_presets.)"""
    import subprocess
    import winreg
    ctx.set_status("Cleaning stuck Steam download files...")
    # Steam running? -> active downloads could be writing right now
    try:
        out = subprocess.check_output(
            'tasklist /fi "imagename eq steam.exe" /fo csv /nh',
            shell=True, text=True, timeout=10, stderr=subprocess.DEVNULL,
        )
        if "steam.exe" in out.lower():
            ctx.log("Steam is running — skipping to avoid touching active downloads.")
            return 0
    except Exception:
        pass
    steam_path = None
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Software\\Valve\\Steam") as k:
            steam_path = winreg.QueryValueEx(k, "SteamPath")[0]
    except OSError:
        ctx.log("Steam not found in the registry — nothing to do.")
        return 0
    # F16: HKCU SteamPath is user-writable — same fingerprint check as
    # clean_tasks._steam_root before deleting through it while elevated.
    try:
        norm = os.path.normpath(steam_path or "")
        root = os.path.splitdrive(norm)[0] + os.sep
        windir = os.environ.get("WINDIR", "")
        profile = os.environ.get("USERPROFILE", "")
        bad = norm.lower() in (root.lower(), windir.lower() if windir else "", profile.lower() if profile else "")
        looks = (os.path.isfile(os.path.join(norm, "steam.exe"))
                 or os.path.isdir(os.path.join(norm, "steamapps")))
        if bad or not looks:
            ctx.log(f"Steam path looks invalid — skipping: {steam_path}")
            return 0
    except Exception:
        ctx.log(f"Steam path looks invalid — skipping: {steam_path}")
        return 0
    total = 0
    for sub in ("downloading", "temp"):
        folder = os.path.join(steam_path, "steamapps", sub)
        if os.path.isdir(folder):
            ctx.log(f"Cleaning Steam staging: {folder}")
            total += clean_folder_contents(ctx, folder)
    return total


from app.tasks import Task  # noqa: E402

TASKS = [
    Task("gamer_launchers", "Clean Launchers & Chat", "Clears every game store + Discord, Slack, Teams, Spotify web junk; keeps logins", clean_gamer_launchers, default=True, admin_required=False, column=0),
    Task("game_files", "Clean Game Files", "Removes logs, crashes and junk from top games like Fortnite, PUBG, BG3 plus a Unity log sweep for indies; keeps saves", clean_game_files, default=True, admin_required=False, column=1),
    Task("steam_stuck", "Clear Stuck Steam Downloads", "Removes huge leftover files from failed Steam updates; frees lots of space", clean_steam_stuck_downloads, default=False, admin_required=False, column=1),
    Task("gpu_shader_caches", "Clean Shader Caches", "Rebuilds DirectX/NVIDIA/AMD/Intel shader caches to fix game stutter", clean_gpu_shader_caches, default=False, admin_required=False, column=0),
    Task("game_captures", "Clean Game Captures", "Removes old Xbox Game Bar clips to free disk space", clean_game_captures, default=False, admin_required=False, column=1),
    # default=False ON PURPOSE: an always-on backup in the weekly scheduler
    # would pile up timestamped zips on the Desktop forever.
    Task("backup_saves", "Back Up Game Saves", "Zips your save folders (Saved Games, My Games, Minecraft) to a timestamped file on your Desktop", backup_game_saves, default=False, admin_required=False, column=0),
    # Per-game mega-caches (user request 2026-09-12): separate opt-in rows so
    # a layman sees WHAT will redownload. All default=False (redownload cost
    # or game-specific), all skip honestly when the game isn't installed.
    Task("vrchat_cache", "Clear VRChat Cache", "Frees gigabytes of downloaded avatars and worlds; they redownload as you visit them", clean_vrchat_cache, default=False, admin_required=False, column=1),
    Task("fivem_cache", "Clear FiveM Cache", "Fixes broken textures and loading loops on roleplay servers; servers resend their files on next join", clean_fivem_cache, default=False, admin_required=False, column=1),
    Task("starcitizen_cache", "Clean Star Citizen Files", "Clears shader and user caches behind patch-day crashes; keeps keybindings, settings, characters and screenshots", clean_starcitizen_cache, default=False, admin_required=True, column=0),
    # Log-only sweeps for the biggest free-to-play launchers (user request):
    # diagnostics the games rewrite every launch; saves, configs, tokens
    # and executables are never touched. All default=False (opt-in).
    Task("riot_logs", "Clear Riot Games Logs", "Removes VALORANT + Riot Client diagnostic logs that pile up for gigabytes", clean_riot_logs, default=False, admin_required=False, column=1),
    Task("hoyoverse_logs", "Clear HoYoverse Logs", "Removes Genshin/Star Rail/ZZZ crash and output logs; games rewrite them", clean_hoyoverse_logs, default=False, admin_required=False, column=1),
    Task("rockstar_logs", "Clear Rockstar Launcher Cache", "Removes launcher web cache and logs; settings and games untouched", clean_rockstar_logs, default=False, admin_required=False, column=1),
    Task("roblox_logs", "Clear Roblox Logs", "Removes chatty per-session client logs; fresh ones open next launch", clean_roblox_logs, default=False, admin_required=False, column=1),
]
