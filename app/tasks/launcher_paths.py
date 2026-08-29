"""
Shared launcher cache paths used by both Clean and Games tabs.
"""

import os

LOCALAPPDATA = os.environ.get("LOCALAPPDATA", "")
APPDATA = os.environ.get("APPDATA", "")
PROGRAMDATA = os.environ.get("ProgramData", "")

# Steam
STEAM_CACHE_PATHS = [
    os.path.join(LOCALAPPDATA, "Steam\\htmlcache"),
    os.path.join(LOCALAPPDATA, "Steam\\appcache"),
    os.path.join(PROGRAMDATA, "Steam\\htmlcache"),
]

# Epic Games
EPIC_CACHE_PATHS = [
    os.path.join(LOCALAPPDATA, "EpicGamesLauncher\\Saved\\webcache"),
    os.path.join(LOCALAPPDATA, "EpicGamesLauncher\\Saved\\webcache_4147"),
    os.path.join(LOCALAPPDATA, "EpicGamesLauncher\\Saved\\Logs"),
]

# EA / Origin
EA_CACHE_PATHS = [
    os.path.join(LOCALAPPDATA, "Electronic Arts\\EA Desktop\\cache"),
    os.path.join(LOCALAPPDATA, "EADesktop\\cache"),
    os.path.join(LOCALAPPDATA, "Origin\\cache"),
]

# GOG Galaxy
GOG_CACHE_PATHS = [
    os.path.join(LOCALAPPDATA, "GOG.com\\Galaxy\\webcache"),
    os.path.join(LOCALAPPDATA, "GOG.com\\Galaxy\\logs"),
]

# Battle.net
BATTLENET_CACHE_PATHS = [
    os.path.join(LOCALAPPDATA, "Battle.net\\Cache"),
    os.path.join(LOCALAPPDATA, "Battle.net\\Logs"),
]

# Riot Client
RIOT_CACHE_PATHS = [
    os.path.join(LOCALAPPDATA, "Riot Games\\Riot Client\\Cache"),
    os.path.join(LOCALAPPDATA, "Riot Games\\Riot Client\\Logs"),
]

# Ubisoft Connect
UBISOFT_CACHE_PATHS = [
    os.path.join(LOCALAPPDATA, "Ubisoft Game Launcher\\cache"),
    os.path.join(LOCALAPPDATA, "Ubisoft Game Launcher\\logs"),
]

# Discord
DISCORD_CACHE_PATHS = [
    os.path.join(APPDATA, "discord\\Cache"),
    os.path.join(APPDATA, "discord\\GPUCache"),
    os.path.join(APPDATA, "discord\\Code Cache"),
]

# Xbox App
XBOX_CACHE_PATHS = [
    os.path.join(LOCALAPPDATA, "Packages\\Microsoft.GamingApp_8wekyb3d8bbwe\\LocalCache"),
    os.path.join(LOCALAPPDATA, "Packages\\Microsoft.GamingApp_8wekyb3d8bbwe\\TempState"),
    os.path.join(LOCALAPPDATA, "Packages\\Microsoft.GamingApp_8wekyb3d8bbwe\\AC"),
]

# Slack (Clean tab only)
SLACK_CACHE_PATHS = [
    os.path.join(APPDATA, "Slack\\Cache"),
]

# Microsoft Teams (Clean tab only)
TEAMS_CACHE_PATHS = [
    os.path.join(APPDATA, "Microsoft\\Teams\\Cache"),
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