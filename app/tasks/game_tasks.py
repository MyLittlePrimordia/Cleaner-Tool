"""
Games tab — launcher cache cleaning + per-game optimization.
Simple: one task per launcher (caches).
"""

import os

from app.utils import TaskContext, clean_folder_contents, run_cmd
from app.tasks.launcher_paths import (
    STEAM_CACHE_PATHS,
    EPIC_CACHE_PATHS,
    EA_CACHE_PATHS,
    GOG_CACHE_PATHS,
    BATTLENET_CACHE_PATHS,
    RIOT_CACHE_PATHS,
    UBISOFT_CACHE_PATHS,
    DISCORD_CACHE_PATHS,
    XBOX_CACHE_PATHS,
    LOCALAPPDATA,
    APPDATA,
    PROGRAMDATA,
)


def _clean_many(ctx: TaskContext, folders, label):
    total = 0
    for folder in folders:
        if folder and os.path.exists(folder):
            ctx.log(f"Cleaning {label}: {folder}")
            total += clean_folder_contents(ctx, folder)
    return total


def clean_steam(ctx: TaskContext):
    return _clean_many(ctx, STEAM_CACHE_PATHS, "Steam cache")


def clean_epic(ctx: TaskContext):
    return _clean_many(ctx, EPIC_CACHE_PATHS, "Epic cache")


def clean_ea(ctx: TaskContext):
    return _clean_many(ctx, EA_CACHE_PATHS, "EA/Origin cache")


def clean_gog(ctx: TaskContext):
    return _clean_many(ctx, GOG_CACHE_PATHS, "GOG cache")


def clean_battlenet(ctx: TaskContext):
    return _clean_many(ctx, BATTLENET_CACHE_PATHS, "Battle.net cache")


def clean_riot(ctx: TaskContext):
    return _clean_many(ctx, RIOT_CACHE_PATHS, "Riot cache")


def clean_ubisoft(ctx: TaskContext):
    return _clean_many(ctx, UBISOFT_CACHE_PATHS, "Ubisoft cache")


def clean_discord(ctx: TaskContext):
    return _clean_many(ctx, DISCORD_CACHE_PATHS, "Discord cache")


def clean_xbox(ctx: TaskContext):
    return _clean_many(ctx, XBOX_CACHE_PATHS, "Xbox app cache")


from app.tasks import Task  # noqa: E402

TASKS = [
    Task("steam", "Steam",
         "Clears Steam web/app cache and download temp files",
         clean_steam, default=True, admin_required=False, column=0),
    Task("epic", "Epic Games",
         "Clears Epic Games web cache and logs",
         clean_epic, default=True, admin_required=False, column=0),
    Task("ea", "EA / Origin",
         "Clears EA Desktop and Origin cache",
         clean_ea, default=True, admin_required=False, column=0),
    Task("gog", "GOG Galaxy",
         "Clears GOG web cache and logs",
         clean_gog, default=True, admin_required=False, column=0),
    Task("battlenet", "Battle.net",
         "Clears Battle.net cache and logs",
         clean_battlenet, default=True, admin_required=False, column=0),
    Task("riot", "Riot Client",
         "Clears Riot Client cache and logs",
         clean_riot, default=True, admin_required=False, column=1),
    Task("ubisoft", "Ubisoft Connect",
         "Clears Ubisoft launcher cache and logs",
         clean_ubisoft, default=True, admin_required=False, column=1),
    Task("discord", "Discord",
         "Clears Discord cache (does not touch messages/logins)",
         clean_discord, default=True, admin_required=False, column=1),
    Task("xbox", "Xbox App",
         "Clears Xbox Game Bar / PC app cache",
         clean_xbox, default=True, admin_required=False, column=1),
]