"""
Games tab — merged mega-tasks: one checkbox per concern, giant verified path lists.

Tasks:
  * Clean Gamer Launchers — every launcher store + chat clients (web caches, logs)
  * Clean Game Files — the giant per-game junk table (logs/crashes/shader caches
    for the Steam Most-Played Top 100, Minecraft, CurseForge, OBS, peripherals)
  * Clean GPU Shader Caches — DirectX/NVIDIA/AMD/Intel/Steam shader caches
  * Clean Game Captures — Game DVR clip folder
"""

import os

from app.utils import TaskContext, clean_folder_contents
from app.tasks.launcher_paths import (
    GAMER_LAUNCHER_ALL,
    GAME_FILES_ALL,
    GPU_SHADER_CACHE_ALL,
    GAME_CAPTURES_PATHS,
    MINECRAFT_LAUNCHER_LOGS,
    LOW_GAME_PLAYER_LOGS,
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
        (os.path.join(os.environ.get("USERPROFILE", ""), "Saved Games"), "Saved Games"),
        (os.path.join(os.environ.get("USERPROFILE", ""), "Documents", "My Games"), "My Games"),
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
    total = 0
    for f in files:
        if not f or not os.path.isabs(f) or not os.path.isfile(f):
            continue
        try:
            st = os.stat(f)
            os.remove(f)
            total += st.st_size
            ctx.log(f"Cleaning {label}: {f}")
        except OSError:
            continue
    return total


def clean_gamer_launchers(ctx: TaskContext):
    # M5 fix: log line now matches GAMER_LAUNCHER_ALL's real contents
    # (Slack/Teams/Spotify are actually included now, see launcher_paths.py).
    ctx.log("Cleaning gamer launchers (Steam, Epic, EA, GOG, Battle.net, Riot, Ubisoft,")
    ctx.log("Discord, Xbox, Rockstar, Amazon, itch, Humble, Wargaming, Nexon, Slack, Teams, Spotify)")
    return _clean_many(ctx, GAMER_LAUNCHER_ALL, "launcher cache")


def clean_game_files(ctx: TaskContext):
    """The mega task: junk from 100+ top games + Minecraft + CurseForge + OBS + peripherals.

    Only touches logs, crash dumps, web caches and shader caches — never saves.
    """
    ctx.log("Cleaning game files (Steam Top-100 titles, Minecraft, CurseForge, OBS,")
    ctx.log("Streamlabs, Logitech G Hub, Razer, Roblox, Unreal/Unity engine caches)")
    total = _clean_many(ctx, GAME_FILES_ALL, "game files")
    # Minecraft launcher logs are loose files in the .minecraft root
    total += _clean_files(ctx, MINECRAFT_LAUNCHER_LOGS, "Minecraft launcher log")
    # M3 audit fix: the Valheim/RimWorld/Schedule I 'Player.log' entries
    # are single files too (see launcher_paths.LOW_GAME_PLAYER_LOGS) — they
    # used to ride in the directory list above, where os.walk over a file
    # path matched nothing and the log claimed cleaning anyway. Route them
    # through the file-based cleaner like the Minecraft launcher logs.
    total += _clean_files(ctx, LOW_GAME_PLAYER_LOGS, "game Player.log")
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
    Task("game_files", "Clean Game Files", "Removes logs, crashes and junk from 100+ top games like Fortnite, PUBG, BG3; keeps saves", clean_game_files, default=True, admin_required=False, column=1),
    Task("steam_stuck", "Clear Stuck Steam Downloads", "Removes huge leftover files from failed Steam updates; frees lots of space", clean_steam_stuck_downloads, default=False, admin_required=False, column=1),
    Task("gpu_shader_caches", "Clean Shader Caches", "Rebuilds DirectX/NVIDIA/AMD/Intel shader caches to fix game stutter", clean_gpu_shader_caches, default=False, admin_required=False, column=0),
    Task("game_captures", "Clean Game Captures", "Removes old Xbox Game Bar clips to free disk space", clean_game_captures, default=False, admin_required=False, column=1),
    # default=False ON PURPOSE: an always-on backup in the weekly scheduler
    # would pile up timestamped zips on the Desktop forever.
    Task("backup_saves", "Back Up Game Saves", "Zips your save folders (Saved Games, My Games, Minecraft) to a timestamped file on your Desktop", backup_game_saves, default=False, admin_required=False, column=0),
]
