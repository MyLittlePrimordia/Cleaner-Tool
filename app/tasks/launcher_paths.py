"""
Shared launcher cache paths used by both Clean and Games tabs.
"""

import os

LOCALAPPDATA = os.environ.get("LOCALAPPDATA", "")
APPDATA = os.environ.get("APPDATA", "")
PROGRAMDATA = os.environ.get("ProgramData", "")

def _join(base: str, *parts: str) -> str:
    # Avoid relative path trap when env var missing: os.path.join("", "X") == "X" (relative) — return "" so _clean_many skips it
    if not base or not os.path.isabs(base):
        return ""
    return os.path.join(base, *parts)

# Steam
STEAM_CACHE_PATHS = [
    _join(LOCALAPPDATA, "Steam\\htmlcache"),
    _join(LOCALAPPDATA, "Steam\\appcache"),
    _join(PROGRAMDATA, "Steam\\htmlcache"),
]

# Epic Games
EPIC_CACHE_PATHS = [
    _join(LOCALAPPDATA, "EpicGamesLauncher\\Saved\\webcache"),
    _join(LOCALAPPDATA, "EpicGamesLauncher\\Saved\\webcache_4147"),
    _join(LOCALAPPDATA, "EpicGamesLauncher\\Saved\\Logs"),
]

# EA / Origin
EA_CACHE_PATHS = [
    _join(LOCALAPPDATA, "Electronic Arts\\EA Desktop\\cache"),
    _join(LOCALAPPDATA, "EADesktop\\cache"),
    _join(LOCALAPPDATA, "Origin\\cache"),
]

# GOG Galaxy
GOG_CACHE_PATHS = [
    _join(LOCALAPPDATA, "GOG.com\\Galaxy\\webcache"),
    _join(LOCALAPPDATA, "GOG.com\\Galaxy\\logs"),
]

# Battle.net
BATTLENET_CACHE_PATHS = [
    _join(LOCALAPPDATA, "Battle.net\\Cache"),
    _join(LOCALAPPDATA, "Battle.net\\Logs"),
]

# Riot Client
RIOT_CACHE_PATHS = [
    _join(LOCALAPPDATA, "Riot Games\\Riot Client\\Cache"),
    _join(LOCALAPPDATA, "Riot Games\\Riot Client\\Logs"),
]

# Ubisoft Connect
UBISOFT_CACHE_PATHS = [
    _join(LOCALAPPDATA, "Ubisoft Game Launcher\\cache"),
    _join(LOCALAPPDATA, "Ubisoft Game Launcher\\logs"),
]

# Discord
DISCORD_CACHE_PATHS = [
    _join(APPDATA, "discord\\Cache"),
    _join(APPDATA, "discord\\GPUCache"),
    _join(APPDATA, "discord\\Code Cache"),
]

# Xbox App
XBOX_CACHE_PATHS = [
    _join(LOCALAPPDATA, "Packages\\Microsoft.GamingApp_8wekyb3d8bbwe\\LocalCache"),
    _join(LOCALAPPDATA, "Packages\\Microsoft.GamingApp_8wekyb3d8bbwe\\TempState"),
    _join(LOCALAPPDATA, "Packages\\Microsoft.GamingApp_8wekyb3d8bbwe\\AC"),
]

# Slack (Clean tab only)
SLACK_CACHE_PATHS = [
    _join(APPDATA, "Slack\\Cache"),
]

# Microsoft Teams (Clean tab only)
TEAMS_CACHE_PATHS = [
    _join(APPDATA, "Microsoft\\Teams\\Cache"),
]

# Combined launcher cache paths for Clean tab (all launchers + Slack + Teams)
ALL_LAUNCHER_CACHE_PATHS = (
    STEAM_CACHE_PATHS
    + EPIC_CACHE_PATHS
    + EA_CACHE_PATHS
    + GOG_CACHE_PATHS
    + BATTLENET_CACHE_PATHS
    + RIOT_CACHE_PATHS
    + UBISOFT_CACHE_PATHS
    + DISCORD_CACHE_PATHS
    + XBOX_CACHE_PATHS
    + SLACK_CACHE_PATHS
    + TEAMS_CACHE_PATHS
)