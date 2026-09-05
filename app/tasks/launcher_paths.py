"""
Shared launcher / game cache paths used by the Clean and Games tabs.

All paths are built from environment variables (no hardcoded user profiles),
and only point at disposable junk: logs, crash dumps, shader caches, and
web caches. Game saves, configs, and profiles are deliberately excluded —
paths verified against PCGamingWiki's documented data locations.
"""

import glob
import os

LOCALAPPDATA = os.environ.get("LOCALAPPDATA", "")
APPDATA = os.environ.get("APPDATA", "")
PROGRAMDATA = os.environ.get("ProgramData", "")
PROGRAMFILES = os.environ.get("ProgramFiles", "")
TEMP = os.environ.get("TEMP", "")
USERPROFILE = os.environ.get("USERPROFILE", "")
DOCUMENTS = os.path.join(USERPROFILE, "Documents") if USERPROFILE else ""
LOCALLOW = os.path.join(USERPROFILE, "AppData", "LocalLow") if USERPROFILE else ""


def _join(base: str, *parts: str) -> str:
    # Avoid relative path trap when env var missing: os.path.join("", "X") == "X" (relative)
    if not base or not os.path.isabs(base):
        return ""
    return os.path.join(base, *parts)


# --------------------------------------------------------------------------- #
# Launcher + game-store junk (safe: web caches, logs, temp download data)
# --------------------------------------------------------------------------- #

# Steam client — htmlcache/appcache are disposable web data
STEAM_CACHE_PATHS = [
    _join(LOCALAPPDATA, "Steam", "htmlcache"),
    _join(LOCALAPPDATA, "Steam", "appcache", "httpcache"),
    _join(LOCALAPPDATA, "Steam", "dumps"),
    _join(LOCALAPPDATA, "Steam", "logs"),
    _join(PROGRAMDATA, "Steam", "htmlcache"),
]

# Epic Games Launcher — webcache dirs are versioned (webcache, webcache_4430, ...), so glob them
def _epic_webcache_paths() -> list[str]:
    saved = _join(LOCALAPPDATA, "EpicGamesLauncher", "Saved")
    if not saved:
        return []
    paths = []
    try:
        for entry in glob.glob(os.path.join(saved, "webcache*")):
            paths.append(entry)
    except OSError:
        pass
    return paths

EPIC_CACHE_PATHS = [
    *_epic_webcache_paths(),
    _join(LOCALAPPDATA, "EpicGamesLauncher", "Saved", "Logs"),
]

# EA app (current) + legacy Origin / EA Desktop
EA_CACHE_PATHS = [
    _join(LOCALAPPDATA, "Electronic Arts", "EA app", "Cache"),
    _join(LOCALAPPDATA, "Electronic Arts", "EA app", "logs"),
    _join(LOCALAPPDATA, "EA app", "cache"),
    _join(LOCALAPPDATA, "EADesktop", "cache"),
    _join(LOCALAPPDATA, "Origin", "cache"),
    # EA App partial-download chunks (user request): staged installer
    # payloads that pile up after interrupted updates. Path observed live on
    # an EA App install; safe — only incomplete downloads live here.
    _join(PROGRAMDATA, "Electronic Arts", "EA App", "Downloads"),
]

# GOG Galaxy
GOG_CACHE_PATHS = [
    _join(PROGRAMDATA, "GOG.com", "Galaxy", "webcache"),
    _join(LOCALAPPDATA, "GOG.com", "Galaxy", "webcache"),
    _join(LOCALAPPDATA, "GOG.com", "Galaxy", "logs"),
]

# Battle.net
BATTLENET_CACHE_PATHS = [
    _join(LOCALAPPDATA, "Battle.net", "Cache"),
    _join(LOCALAPPDATA, "Battle.net", "Logs"),
]

# Riot Client + Valorant (logs only — saves/configs excluded)
RIOT_CACHE_PATHS = [
    _join(LOCALAPPDATA, "Riot Games", "Riot Client", "Cache"),
    _join(LOCALAPPDATA, "Riot Games", "Riot Client", "Logs"),
    _join(LOCALAPPDATA, "VALORANT", "Saved", "Logs"),
    # Riot Vanguard anti-cheat logs (user request): verbose kernel-driver
    # logs that grow without limit. Log files only — never vgtray/vgk
    # drivers or configs.
    _join(PROGRAMFILES, "Riot Vanguard", "logs"),
]

# Ubisoft Connect
UBISOFT_CACHE_PATHS = [
    _join(LOCALAPPDATA, "Ubisoft Game Launcher", "cache"),
    _join(LOCALAPPDATA, "Ubisoft Game Launcher", "logs"),
]

# Discord — stable + PTB/Canary branches; Cache_Data holds the bulk
DISCORD_CACHE_PATHS = []
for _branch in ("discord", "discordPTB", "discordCanary", "discordDevelopment"):
    DISCORD_CACHE_PATHS += [
        _join(APPDATA, _branch, "Cache"),
        _join(APPDATA, _branch, "Code Cache"),
        _join(APPDATA, _branch, "GPUCache"),
    ]

# Xbox app (UWP package)
def _xbox_package_paths() -> list[str]:
    packages_root = _join(LOCALAPPDATA, "Packages")
    if not packages_root:
        return []
    paths = []
    try:
        for name in os.listdir(packages_root):
            if name.startswith("Microsoft.GamingApp_"):
                for sub in ("LocalCache", "TempState"):
                    p = os.path.join(packages_root, name, sub)
                    if os.path.isdir(p):
                        paths.append(p)
    except OSError:
        pass
    return paths

XBOX_CACHE_PATHS = _xbox_package_paths()

# Rockstar Games Launcher (GTA V renders + launcher cache)
ROCKSTAR_CACHE_PATHS = [
    _join(LOCALAPPDATA, "Rockstar Games", "Launcher", "cache"),
    _join(APPDATA, "Rockstar Games", "Launcher", "logs"),
    # PCGW-verified: rendered Rockstar Editor videos output
    _join(LOCALAPPDATA, "Rockstar Games", "GTA V", "videos", "rendered"),
]

# Additional launchers
AMAZON_CACHE_PATHS = [
    _join(LOCALAPPDATA, "Amazon Games", "Cache"),
    _join(LOCALAPPDATA, "Amazon Games", "Logs"),
]
ITCH_CACHE_PATHS = [
    _join(APPDATA, "itch", "cache"),
    _join(APPDATA, "itch", "logs"),
]
HUMBLE_CACHE_PATHS = [
    _join(LOCALAPPDATA, "Humble Bundle", "cache"),
]
WARGAMING_CACHE_PATHS = [
    _join(APPDATA, "Wargaming.net", "GameCenter", "cache"),
    _join(APPDATA, "Wargaming.net", "GameCenter", "logs"),
]
NEXON_CACHE_PATHS = [
    _join(LOCALAPPDATA, "Nexon", "NexonPlug", "cache"),
]

# --------------------------------------------------------------------------- #
# Gamer software junk (OBS, Streamlabs, CurseForge, peripheral apps)
# --------------------------------------------------------------------------- #

OBS_CACHE_PATHS = [
    _join(APPDATA, "obs-studio", "logs"),
    _join(APPDATA, "obs-studio", "crashes"),
    _join(APPDATA, "obs-studio", "profiler_reports"),
]

STREAMLABS_CACHE_PATHS = [
    _join(APPDATA, "slobs-client", "Cache"),
    _join(APPDATA, "slobs-client", "logs"),
]

CURSEFORGE_CACHE_PATHS = [
    # Verified live: Electron caches + logs (settings/local storage excluded)
    _join(APPDATA, "CurseForge", "Cache"),
    _join(APPDATA, "CurseForge", "Code Cache"),
    _join(APPDATA, "CurseForge", "GPUCache"),
    _join(APPDATA, "CurseForge", "DawnGraphiteCache"),
    _join(APPDATA, "CurseForge", "DawnWebGPUCache"),
    _join(APPDATA, "CurseForge", "logs"),
]

PERIPHERAL_CACHE_PATHS = [
    # Logitech G Hub
    _join(LOCALAPPDATA, "LGHUB", "log"),
    _join(LOCALAPPDATA, "LGHUB", "crash_reports"),
    # Razer Synapse
    _join(APPDATA, "Razer", "Synapse", "Logs"),
    _join(APPDATA, "Razer", "Synapse3", "Logs"),
]

# --------------------------------------------------------------------------- #
# Minecraft (verified live on this machine)
# --------------------------------------------------------------------------- #

MINECRAFT_CACHE_PATHS = [
    # Java edition launcher (Microsoft Store build keeps webcache2 + launcher logs here)
    _join(APPDATA, ".minecraft", "webcache2"),
    _join(APPDATA, ".minecraft", "logs"),
    _join(APPDATA, ".minecraft", "crash-reports"),
]

def _minecraft_launcher_log_files() -> list[str]:
    # launcher_log.txt / launcher_log0.txt sit in the .minecraft root (verified 7 MB)
    mc = _join(APPDATA, ".minecraft")
    if not mc:
        return []
    files = []
    for name in ("launcher_log.txt", "launcher_log0.txt"):
        p = os.path.join(mc, name)
        if os.path.isfile(p):
            files.append(p)
    return files

MINECRAFT_LAUNCHER_LOGS = _minecraft_launcher_log_files()

# Bedrock edition (UWP) package caches
def _minecraft_bedrock_paths() -> list[str]:
    packages_root = _join(LOCALAPPDATA, "Packages")
    if not packages_root:
        return []
    paths = []
    try:
        for name in os.listdir(packages_root):
            if name.startswith("Microsoft.MinecraftUWP"):
                p = os.path.join(packages_root, name, "LocalCache")
                if os.path.isdir(p):
                    paths.append(p)
    except OSError:
        pass
    return paths

MINECRAFT_BEDROCK_CACHE_PATHS = _minecraft_bedrock_paths()

# --------------------------------------------------------------------------- #
# Top-game junk paths (Steam Most-Played Top 100, PCGamingWiki-verified)
#
# SAFETY RULES (from PCGW data):
#   - Only Logs / Crashes / CrashDumps / shader-cache / webcache subfolders.
#   - Never the game root: saves + configs live there for many titles.
#   - Excluded entirely (saves/configs share the folder, nothing safe to take):
#     Elden Ring, Terraria, Geometry Dash (.dat = saves!), Helldivers 2,
#     Black Desert, Battlefield 6, League of Legends, CS2, Dota 2, TF2,
#     Overwatch 2, War Thunder, Hunt: Showdown, Deadlock, Once Human.
# --------------------------------------------------------------------------- #

_TOP_GAME_JUNK = [
    # (base env, subpath parts...) — each becomes base + subpath
    ("LOCALAPPDATA", "DeadByDaylight", "Saved", "Logs"),          # verified live: 126 MB
    ("LOCALAPPDATA", "TslGame", "Saved", "Logs"),                 # PUBG
    ("LOCALAPPDATA", "TslGame", "Saved", "Crashes"),
    ("LOCALAPPDATA", "Pal", "Saved", "Logs"),                     # Palworld
    ("LOCALAPPDATA", "Pal", "Saved", "Crashes"),
    ("LOCALAPPDATA", "Marvel", "Saved", "Logs"),                  # Marvel Rivals
    ("LOCALAPPDATA", "FortniteGame", "Saved", "Logs"),            # Fortnite
    ("LOCALAPPDATA", "FortniteGame", "Saved", "Crashes"),
    ("LOCALAPPDATA", "Stalker2", "Saved", "Logs"),                # S.T.A.L.K.E.R. 2
    ("LOCALAPPDATA", "Discovery", "Saved", "Logs"),               # THE FINALS
    ("LOCALAPPDATA", "FactoryGame", "Saved", "Logs"),             # Satisfactory
    ("LOCALAPPDATA", "PAYDAY 2", "logs"),                          # PAYDAY 2 (saves\ excluded)
    ("LOCALAPPDATA", "Larian Studios", "Baldur's Gate 3", "CrashDumps"),  # BG3
    ("LOCALAPPDATA", "Larian Studios", "Baldur's Gate 3", "Logs"),
    ("LOCALAPPDATA", "CD Projekt Red", "Cyberpunk 2077", "ShaderCache"),  # Cyberpunk
    ("LOCALAPPDATA", "VALORANT", "Saved", "Logs"),                # Valorant
    ("LOCALAPPDATA", "Stalker2", "Saved", "Crashes"),
    ("APPDATA", "Battlestate Games", "Escape from Tarkov", "logs"),        # EFT (Settings excluded)
    ("APPDATA", "The Creative Assembly", "Warhammer3", "logs"),    # TWWH3 (save_games excluded)
    # H3 fix: removed ("APPDATA", "BrawlhallaAir") — this had no subpath, so
    # it pointed at the game's entire settings/stats root (Brawlhalla is an
    # Adobe AIR app; %APPDATA%\BrawlhallaAir IS the data root, not a
    # Logs/Crashes subfolder), directly violating this file's own rule above
    # ("Never the game root: saves + configs live there"). Clean Game Files
    # is default-on, so this could wipe a Brawlhalla player's settings/stats.
    # Not re-added with a narrowed subpath because no PCGamingWiki-verified
    # junk-only subfolder for this title was confirmed — add it back only
    # once one is verified, following the same rule as every other entry.
    ("LOCALAPPDATA", "Sports Interactive", "Football Manager 26", "logs"),  # FM26 (cloud\games = saves, excluded)
]

_DOC_GAME_JUNK = [
    # Documents-based game data (only logs/crashes subpaths)
    ("My Games", "Rocket League", "TAGame", "Logs"),               # Rocket League (SaveData excluded)
    ("My Games", "Path of Exile 2", "logs"),                       # PoE2
    ("DayZ", "logs"),                                              # DayZ
    ("Mount and Blade II Bannerlord", "crashes"),                  # Bannerlord (Game Saves excluded)
    ("Klei", "DoNotStarveTogether", "cache"),                      # DST (client_save excluded)
    ("Paradox Interactive", "Hearts of Iron IV", "logs"),          # HoI4 (save games excluded)
    ("Paradox Interactive", "Crusader Kings III", "logs"),         # CK3
]

_LOW_GAME_JUNK = [
    # AppData\LocalLow based — M3 audit fix: all three entries are single
    # FILES (Unity "Player.log"), so they must NOT be appended to the
    # directory list below (os.walk over a file path deletes nothing while
    # the log claims cleaning). They are routed to LOW_GAME_PLAYER_LOGS and
    # cleaned through game_tasks._clean_files, exactly like the Minecraft
    # launcher logs.
    ("IronGate", "Valheim", "Player.log"),                         # old log file
    ("Ludeon Studios", "RimWorld by Ludeon Studios", "Player.log"),
    ("TVGS", "Schedule I", "Player.log"),
]

_TOP_GAME_CACHE_PATHS: list[str] = []

def _build_top_game_paths() -> list[str]:
    paths: list[str] = []
    env_map = {"LOCALAPPDATA": LOCALAPPDATA, "APPDATA": APPDATA}
    for env_name, *parts in _TOP_GAME_JUNK:
        base = env_map.get(env_name, "")
        if base:
            paths.append(os.path.join(base, *parts))
    for parts in _DOC_GAME_JUNK:
        if DOCUMENTS:
            paths.append(os.path.join(DOCUMENTS, *parts))
    return [p for p in paths if p and os.path.isabs(p)]

def _build_low_game_player_logs() -> list[str]:
    """The LocalLow 'Player.log' files (Valheim / RimWorld / Schedule I)
    are single files, not folders — cleaning them through the directory
    walker silently deleted nothing (M3 audit fix). Built the same way as
    _minecraft_launcher_log_files above: only existing files are listed."""
    low = LOCALLOW
    if not low:
        return []
    files = []
    for parts in _LOW_GAME_JUNK:
        p = os.path.join(low, *parts)
        if os.path.isfile(p):
            files.append(p)
    return files

LOW_GAME_PLAYER_LOGS = _build_low_game_player_logs()

_TOP_GAME_CACHE_PATHS = _build_top_game_paths()

# DirectX / GPU shader caches (safe: auto-regenerate, fix stutter after driver updates)
DIRECTX_CACHE_PATHS = [
    _join(LOCALAPPDATA, "D3DSCache"),
    _join(LOCALAPPDATA, "Microsoft", "D3DSCache"),
]
NVIDIA_EXTENDED_CACHE_PATHS = [
    _join(LOCALAPPDATA, "NVIDIA", "DXCache"),
    _join(LOCALAPPDATA, "NVIDIA", "GLCache"),
    _join(LOCALAPPDATA, "NVIDIA", "ComputeCache"),
    _join(LOCALAPPDATA, "NVIDIA", "PerDriverVersion", "DXCache"),
    _join(LOCALAPPDATA, "NVIDIA", "PerDriverVersion", "GLCache"),
    _join(PROGRAMDATA, "NVIDIA Corporation", "NV_Cache"),
    _join(LOCALAPPDATA, "AMD", "DxCache"),
    _join(LOCALAPPDATA, "AMD", "DxcCache"),
    _join(LOCALAPPDATA, "AMD", "VkCache"),
    _join(LOCALAPPDATA, "AMD", "OglCache"),
    _join(LOCALAPPDATA, "Intel", "ShaderCache"),
]

# Steam per-game shader cache
_STEAM_PROGRAM = os.environ.get("ProgramFiles(x86)", "") or os.environ.get("ProgramFiles", "")
STEAM_SHADER_CACHE_PATHS = [
    _join(_STEAM_PROGRAM, "Steam", "steamapps", "shadercache"),
    _join(LOCALAPPDATA, "Steam", "shadercache"),
]

# Game DVR captures (user-visible, but the folder is disposable clips)
GAME_CAPTURES_PATHS = [
    _join(USERPROFILE, "Videos", "Captures"),
]

# Engine caches
UNREAL_UNITY_CACHE_PATHS = [
    _join(LOCALAPPDATA, "UnrealEngine", "Common", "DerivedDataCache"),
    _join(LOCALAPPDATA, "Unity", "cache"),
    _join(LOCALAPPDATA, "Unity", "caches"),
    _join(LOCALAPPDATA, "Temp", "UnrealEngine"),
    _join(LOCALLOW, "Unity", "WebPlayer", "Cache"),
]

GEFORCE_AMD_CACHE_PATHS = [
    _join(LOCALAPPDATA, "NVIDIA Corporation", "NVIDIA GeForce Experience", "Caches"),
    _join(LOCALAPPDATA, "AMD", "CN"),
]

ROBLOX_CACHE_PATHS = [
    _join(LOCALAPPDATA, "Roblox", "logs"),
    _join(LOCALAPPDATA, "Roblox", "Downloads"),
    _join(TEMP, "Roblox"),
]

# Slack / Teams (chat apps gamers keep open while gaming)
SLACK_CACHE_PATHS = [
    _join(APPDATA, "Slack", "Cache"),
    _join(APPDATA, "Slack", "Service Worker", "CacheStorage"),
]
TEAMS_CACHE_PATHS = [
    # New Teams (2024+, UWP) + legacy classic
    _join(LOCALAPPDATA, "Packages", "MSTeams_8wekyb3d8bbwe", "LocalCache"),
    _join(APPDATA, "Microsoft", "Teams", "Cache"),
]

# Spotify (Storage + crash dumps)
SPOTIFY_CACHE_PATHS = [
    _join(APPDATA, "Spotify", "Storage"),
    _join(LOCALAPPDATA, "Spotify", "Storage"),
    _join(APPDATA, "Spotify", "crash_reports"),
]

# --------------------------------------------------------------------------- #
# Aggregates used by task modules
# --------------------------------------------------------------------------- #

GAMER_LAUNCHER_ALL = (
    STEAM_CACHE_PATHS
    + EPIC_CACHE_PATHS
    + EA_CACHE_PATHS
    + GOG_CACHE_PATHS
    + BATTLENET_CACHE_PATHS
    + RIOT_CACHE_PATHS
    + UBISOFT_CACHE_PATHS
    + DISCORD_CACHE_PATHS
    + XBOX_CACHE_PATHS
    + ROCKSTAR_CACHE_PATHS
    + AMAZON_CACHE_PATHS
    + ITCH_CACHE_PATHS
    + HUMBLE_CACHE_PATHS
    + WARGAMING_CACHE_PATHS
    + NEXON_CACHE_PATHS
    # M5 fix: game_tasks.clean_gamer_launchers logs "+ Slack/Teams/Spotify"
    # but this list never included them — only the Clean tab's separate
    # ALL_LAUNCHER_CACHE_PATHS did. Someone using only the (now-merged)
    # Games tab got misleading output and missed cleaning. Added here so
    # the log line is accurate and both tabs clean the same things.
    + SLACK_CACHE_PATHS
    + TEAMS_CACHE_PATHS
    + SPOTIFY_CACHE_PATHS
)

GAME_FILES_ALL = (
    MINECRAFT_CACHE_PATHS
    + MINECRAFT_BEDROCK_CACHE_PATHS
    + OBS_CACHE_PATHS
    + STREAMLABS_CACHE_PATHS
    + CURSEFORGE_CACHE_PATHS
    + PERIPHERAL_CACHE_PATHS
    + ROBLOX_CACHE_PATHS
    + _TOP_GAME_CACHE_PATHS
    + UNREAL_UNITY_CACHE_PATHS
)

# M5 fix: ALL_LAUNCHER_CACHE_PATHS (Clean tab) and GAMER_LAUNCHER_ALL (Games
# tab) are now identical in coverage — both include every launcher plus
# Slack/Teams/Spotify. Kept as two names since other modules already import
# them by these names, but they intentionally point at the same list so the
# two tabs never drift apart again.
ALL_LAUNCHER_CACHE_PATHS = GAMER_LAUNCHER_ALL

# M2 fix: single source of truth for GPU shader cache paths, used by BOTH
# clean_tasks.clean_shader_cache and game_tasks.clean_gpu_shader_caches.
# Previously clean_tasks.py had its own hardcoded copy that (a) used
# APPDATA\NVIDIA\ComputeCache instead of the real LOCALAPPDATA location,
# and (b) was missing the PerDriverVersion\ paths this list already has —
# so the two "shader cache" tasks silently produced different results, and
# whichever one ran alone missed real cache locations the other one knew
# about.
GPU_SHADER_CACHE_ALL = (
    DIRECTX_CACHE_PATHS
    + NVIDIA_EXTENDED_CACHE_PATHS
    + STEAM_SHADER_CACHE_PATHS
    + GEFORCE_AMD_CACHE_PATHS
)
