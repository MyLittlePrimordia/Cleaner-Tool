"""
Install tab — Master App Catalog data.

2026-09-12 addition (user request): OpenRGB (OpenRGB.OpenRGB, verified
FOUND on winget, homepage openrgb.org live 200) in Hardware Diagnostics
& Tuning, next to Fan Control. Fan Control itself (Rem0o.FanControl,
verified FOUND) was already catalogued — no duplicate added.

2026-09-12 addition (user request: EarTrumpet, Sunshine, CapFrameX only —
no other picks): EarTrumpet (File-New-Project.EarTrumpet, verified FOUND
v2.3.0.0, eartrumpet.app live 200) in Media, Streaming & Audio; Sunshine
(LizardByte.Sunshine, verified FOUND, project docs live 200) in Gaming
Tools & Emulators next to Moonlight; CapFrameX (CXWorld.CapFrameX —
note the publisher prefix, verified FOUND v1.9.0.8, capframex.com live
200; the Beta variant CXWorld.CapFrameX.Beta deliberately not added) in
Hardware Diagnostics & Tuning. All three FOSS, no accounts, no tiers.

2026-09-08 round-8 curation (user request: extend the APO+Peace verified
download method to more non-winget apps + FluidEQ bundle). IDs/URLs for
every NEW or CHANGED entry were live-verified via `winget search
--exact` / `winget show`, the GitHub API and HTTP probes on 2026-09-08:
  * Xenia Canary: NOW ON WINGET — Xenia.XeniaCanary resolves (verified
    FOUND v9132035, portable zip with VC++ dependency, homepage
    xenia-canary-releases). The old manual: entry is REMOVED; a winget
    entry under that ID replaces it.
  * Lime3DS -> Azahar: the project renamed and moved to
    azahar-emu/azahar; Lime3DS/Lime3DS now only hosts source + the
    Azahar succession. Azahar IS on winget — AzaharEmu.Azahar (verified
    FOUND v2126.0, nullsoft installer, homepage azahar-emu.org, live
    200). The old manual:lime3ds entry is REPLACED by a winget entry for
    Azahar (honest name: the 3DS emulator's real current project).
  * RustDesk: REMOVED from MANUAL_ONLY_APPS — installed by the new
    verified-download RUSTDESK_TASK (embedded Utilities row; verified
    NOT on winget anymore, so winget id RustDesk.RustDesk does not
    resolve and the GitHub-releases method is the honest automated path).
  * FreeFileSync: REMOVED from MANUAL_ONLY_APPS — installed by the new
    FREEFILESYNC_TASK (embedded Utilities row; author distributes only
    via freefilesync.org, no winget package, installer Authenticode-
    signed by the author — signature-gated like Peace).
  * DS4Windows: repo MOVED — Ryochan7/DS4Windows is 404 (verified
    live); the project lives at github.com/ds4windowsapp/DS4Windows
    now. Link-only manual entry stays (portable zip, needs ViGEmBus
    driver — no honest silent-install path), with the corrected URL.
  * New embedded bundle task "Equalizer APO + FluidEQ" lives in
    install_tasks.py (APO_FLUIDEQ_TASK) next to the Peace bundle — the
    user picks either GUI; both render in the Media category.

2026-09-06 round-7 curation (user request: consolidated master list —
remove 55 apps, add 13, add 2 bundle items, tag 3 OEM apps, move
LosslessCut + Xenia Canary to manual-only). IDs/URLs for every NEW or
CHANGED entry were live-verified via `winget search --exact` / `winget
show` and an HTTP probe on 2026-09-06; untouched entries keep their
previously verified state.

ID/URL adjudications (evidence-based, deviations from the blueprint's
stated IDs — the blueprint's literal IDs were tested first and FAILED):
  * Macro Deck: SuchByte.MacroDeck does NOT resolve on winget. The live
    package is MacroDeck.MacroDeck (verified FOUND v2.15.1; publisher
    SuchByte, homepage macrodeck.org) -> winget entry under that ID.
  * Soundux: Soundux.Soundux does NOT resolve; the live package is
    Soundux.Soundux.Portable (verified FOUND v0.2.7) -> winget entry
    under that ID. NOTE: it is a portable build that needs the WebView2
    Runtime (already an Essentials checkbox) and a VB-CABLE audio sink
    for game/Discord output — documented in the description.
  * MusicBee: MusicBee.MusicBee does NOT resolve on winget. The author
    ships the installer himself (getmusicbee.com -> a Mega mirror; the
    Store edition is a separate paid-Store-style listing 9P4CLT2RJ1RS
    that IS winget-installable and auto-updates) -> kept as a winget
    entry under the Store ID (added to MSSTORE_IDS), official site URL.
  * FreeFileSync: GmbH.FreeFileSync does NOT resolve on winget. The
    author (Zenju) distributes only via freefilesync.org (verified
    live 200; no winget package, no GitHub releases) -> MANUAL entry,
    honest link-only, no fake installer ID (the same rule the catalog
    has always followed).
  * LightCrosshair: PrimeBuild.LightCrosshair resolves (verified FOUND
    v1.7.0), but the project's real repo moved to the PrimeBuild-pc
    org — github.com/PrimeBuild/LightCrosshair is 404 (verified live)
    -> winget entry with the winget-published homepage
    https://github.com/PrimeBuild-pc/LightCrosshair (200).
  * Xenia Canary: user asked for a manual GitHub link entry; the
    releases live in the xenia-canary-releases repo (the xenia-canary
    repo itself only links out), so the fallback_url points at the
    repo that actually hosts the downloads. The old winget entry
    Xenia.Xenia (the frozen master-branch build) was REMOVED per the
    user's emulator consolidation (RetroArch covers retro cores).
  * LosslessCut: per the user's ruling, the Store build is PAID while
    mifi's own GitHub releases are the free official FOSS build ->
    moved to MANUAL_ONLY_APPS with the GitHub releases page as the
    primary link (the old winget id ch.LosslessCut was a third-party
    repack — dropped with it).
  * Microsoft.DirectX (new bundle entry): resolves on winget (verified
    FOUND v9.29.1974.0, Microsoft Corporation, the legacy d3dx9/
    d3dx10/d3dx11/XAudio/XInput side-by-side libraries) — see the
    DIRECTX BUNDLE task note in tasks/install_tasks.py.
  * Bundles: the "Visual C++ All-in-One" + "DirectX End-User Runtimes"
    items the user named map onto the Essentials VC++ bundle (the
    winget id Microsoft.VCRedist.2015+.x64 the user gave is one of
    the 12 the bundle already installs) and the new winget-based
    DIRECTX BUNDLE task respectively — no catalog changes needed
    beyond the DirectX task note.

OEM-EXCLUSIVE HARDWARE TAGS (user request): G-Helper, Lenovo Legion
Toolkit and Dell/Alienware Command Update keep their catalog entries
but carry a `hardware` tag so non-matching users are warned BEFORE
they install (rendered as an amber chip next to the name in the GUI).

2026-09-05 round-5 curation (user request: remove 18 named bloat/
duplicate/unwanted entries, add 6 replacements). IDs verified live
against winget/GitHub on 2026-09-05:
  * Removed (winget entries): CurseForge Launcher, Azahar, WeMod,
    PotPlayer, CapCut, Streamlabs, OP Auto Clicker, SignalRGB, NanaZip
    (round-7 note: NanaZip is BACK per the user's 7-Zip replacement
    ruling), PeaZip, Netflix, Hulu, Crunchyroll, Reddit, Pandora,
    SoundCloud, Tubi TV, Pluto TV, Vesktop (Vencord itself was never a
    separate catalog entry — only the Vesktop client existed).
  * Removed (manual-only): Pinokio, Reddit.
  * Added: LosslessCut, Microsoft PowerToys, Flameshot (round-7: both
    REMOVED again per the consolidated master list), MPC-HC (clsid2
    fork), Blur-AutoClicker (round-7: REMOVED), Flycast (round-7:
    moved to REMOVED — RetroArch covers it).
  * MPC-HC: the original mpc-hc.org project is unmaintained; clsid2/
    mpc-hc is the actively maintained continuation and is what
    `clsid2.mpc-hc` on winget actually ships (verified live, v2.7.4).

2026-09-05 round-4 curation (user blueprint: bloatware purge + verified
replacements). IDs/URLs for every NEW or CHANGED entry were live-verified
via `winget search --exact` and HTTP probe on 2026-09-05; untouched
entries keep their previously verified state.

Standing rules (unchanged):
  * winget install for every catalog app; MANUAL_ONLY_APPS are link-only.
  * "fallback_url": official/vendor page, project GitHub releases, or a
    mirror the project itself endorses. NEVER third-party aggregators.
  * A-Z within each category (validated on import).
"""

MSSTORE_IDS = {"9MVZQVXJBQ9V", "9N4D0MSMP0PT", "9MV0B5HZVK9Z", "9NZKPSTSNW4P",
               "9N5TDP8VCMHS", "XP8CLZL93F5Z4P", "9NBLGGH30XJ3",
               "9P4CLT2RJ1RS", "9NKSQGP7F2NH"}

# Apps with NO winget package — shown in the catalog as manual-download
# links only (honest: we refuse to ship a fake installer entry).
MANUAL_ONLY_APPS = [
    {"id": 'manual:amd-auto-detect', "name": 'AMD Auto-Detect Installer', "category": 'Driver Installers & Hardware',
     "description": 'AMD installer is not on winget - opens the official AMD page', "foss": False,
     "url": 'https://www.amd.com/en/support/download/drivers.html'},
    {"id": 'manual:logitech-omm', "name": 'Logitech Onboard Memory Manager (OMM)', "category": 'Driver Installers & Hardware',
     "description": 'Official 5 MB portable utility to tune Logitech mouse DPI, binds, and polling without G HUB bloat', "foss": False,
     "url": 'https://support.logi.com/hc/en-us/articles/360059641133'},
    {"id": 'manual:hoyoplay', "name": 'HoyoPlay Launcher', "category": 'Game Launchers, Stores & Mod Managers',
     "description": 'HoYoverse launcher for Genshin Impact, Star Rail and ZZZ', "foss": False,
     "url": 'https://hoyoplay.hoyoverse.com/'},
    {"id": 'manual:riot-client', "name": 'Riot Client', "category": 'Game Launchers, Stores & Mod Managers',
     "description": 'Riot launcher for Valorant and League of Legends', "foss": False,
     "url": 'https://www.riotgames.com/'},
    {"id": 'manual:borderless-gaming', "name": 'Borderless Gaming', "category": 'Gaming Tools & Emulators',
     "description": 'Forces older games into borderless fullscreen mode', "foss": True,
     "url": 'https://github.com/andrewmd5/Borderless-Gaming/releases/latest'},
    {"id": 'manual:ds4windows', "name": 'DS4Windows', "category": 'Gaming Tools & Emulators',
     "description": 'Use PlayStation controllers on PC with gyro support (portable zip — needs the ViGEmBus driver)',
     "foss": False,
     "url": 'https://github.com/ds4windowsapp/DS4Windows/releases/latest'},
    {"id": 'manual:google-play-games', "name": 'Google Play Games for PC', "category": 'Gaming Tools & Emulators',
     "description": 'Official Google Android gaming platform on PC with keyboard/mouse mapping and zero adware', "foss": False,
     "url": 'https://play.google.com/googleplaygames'},
    {"id": 'manual:rpcs3', 'name': 'RPCS3', 'category': 'Gaming Tools & Emulators',
     'description': 'PS3 emulator with 4K support - official build (no winget package)',
     'foss': True, 'url': 'https://rpcs3.net/'},
    {"id": 'manual:nvidia-broadcast', "name": 'NVIDIA Broadcast', "category": 'Media, Streaming & Audio',
     "description": 'AI mic noise removal and webcam effects (needs RTX)', "foss": False,
     "url": 'https://www.nvidia.com/en-us/geforce/broadcasting/broadcast-app/'},
    {"id": 'manual:touch-portal', "name": 'Touch Portal', "category": 'Media, Streaming & Audio',
     "description": 'Remote-control deck for OBS, stream macros and scenes', "foss": False,
     "url": 'https://www.touch-portal.com/'},
    {"id": 'manual:davinci-resolve', "name": 'DaVinci Resolve', "category": 'Creative & Productivity',
     "description": 'Hollywood-grade free video editor and color grading suite', "foss": False,
     "url": 'https://www.blackmagicdesign.com/products/davinciresolve'},
    {"id": 'manual:losslesscut', "name": 'LosslessCut', "category": 'Media, Streaming & Audio',
     "description": 'Lossless video/audio trimmer and joiner — free official FOSS build (no winget package)', "foss": True,
     "url": 'https://github.com/mifi/lossless-cut/releases', "fallback_url": 'https://mifi.no/losslesscut/'},
    {"id": 'manual:tiktok-live-studio', "name": 'TikTok LIVE Studio', "category": 'Media, Streaming & Audio',
     "description": 'Official TikTok desktop app for going live (no winget package)', "foss": False,
     "url": 'https://www.tiktok.com/live/studio'},

]


APP_CATALOG = [
    # A. Driver Installers & Hardware
    {"id": 'Dell.CommandUpdate.Universal', "name": 'Dell / Alienware Command Update', "category": 'Driver Installers & Hardware',
     "description": 'Auto BIOS and driver updater for Dell/Alienware', "foss": False,
     "hardware": 'Dell / Alienware systems only',
     "url": 'https://www.dell.com/support/'},
    {"id": 'Elgato.4KCaptureUtility', "name": 'Elgato 4K Capture Utility', "category": 'Driver Installers & Hardware',
     "description": 'Firmware updater for Elgato capture devices', "foss": False,
     "url": 'https://www.elgato.com/downloads'},
    {"id": 'seerge.g-helper', "name": 'G-Helper (ASUS ROG/TUF)', "category": 'Driver Installers & Hardware',
     "description": 'FOSS lightweight replacement for Armoury Crate', "foss": True,
     "hardware": 'ASUS ROG / TUF laptops only',
     "url": 'https://github.com/seerge/g-helper'},
    {"id": 'Intel.IntelDriverAndSupportAssistant', "name": 'Intel Driver & Support Assistant', "category": 'Driver Installers & Hardware',
     "description": 'Auto-detects Intel CPUs, Arc/Iris GPUs, chipsets, Wi-Fi, and Bluetooth', "foss": False,
     "url": 'https://www.intel.com/content/www/us/en/support/detect.html'},
    {"id": 'BartoszCichecki.LenovoLegionToolkit', "name": 'Lenovo Legion Toolkit', "category": 'Driver Installers & Hardware',
     "description": 'FOSS Lenovo Legion driver and power manager', "foss": True,
     "hardware": 'Lenovo Legion laptops only',
     "url": 'https://github.com/BartoszCichecki/LenovoLegionToolkit'},
    {"id": 'XP8CLZL93F5Z4P', "name": 'NVIDIA App', "category": 'Driver Installers & Hardware',
     "description": 'GeForce driver installer with ShadowPlay', "foss": False,
     "url": 'https://www.nvidia.com/software/nvidia-app/'},
    {"id": 'GlennDelahoy.SnappyDriverInstallerOrigin', "name": 'Snappy Driver Installer Origin', "category": 'Driver Installers & Hardware',
     "description": 'FOSS offline driver updater with driverpacks for any PC', "foss": True,
     "url": 'https://www.snappy-driver-installer.org/'},
    {"id": '9NBLGGH30XJ3', "name": 'Xbox Accessories', "category": 'Driver Installers & Hardware',
     "description": 'Calibrate and update firmware on Xbox controllers', "foss": False,
     "url": 'https://apps.microsoft.com/detail/9NBLGGH30XJ3'},
    # B. Game Launchers, Stores & Mod Managers
    {"id": 'Amazon.Games', "name": 'Amazon Games App', "category": 'Game Launchers, Stores & Mod Managers',
     "description": "Amazon's launcher for Prime Gaming freebies and its own titles", "foss": False,
     "url": 'https://gaming.amazon.com/'},
    {"id": 'Blizzard.BattleNet', "name": 'Battle.net', "category": 'Game Launchers, Stores & Mod Managers',
     "description": 'Blizzard launcher for Diablo, Overwatch, Call of Duty and WoW', "foss": False,
     "url": 'https://www.blizzard.com/'},
    {"id": 'ElectronicArts.EADesktop', "name": 'EA App', "category": 'Game Launchers, Stores & Mod Managers',
     "description": 'EA platform for Apex, Battlefield, EA Play', "foss": False,
     "url": 'https://www.ea.com/ea-app'},
    {"id": 'EpicGames.EpicGamesLauncher', "name": 'Epic Games Launcher', "category": 'Game Launchers, Stores & Mod Managers',
     "description": 'Required for Fortnite, Unreal Engine, and weekly free games', "foss": False,
     "url": 'https://store.epicgames.com/'},
    {"id": 'GOG.Galaxy', "name": 'GOG Galaxy', "category": 'Game Launchers, Stores & Mod Managers',
     "description": 'DRM-free gaming platform and multi-store library aggregator', "foss": False,
     "url": 'https://www.gog.com/galaxy'},
    {"id": 'HeroicGamesLauncher.HeroicGamesLauncher', "name": 'Heroic Games Launcher', "category": 'Game Launchers, Stores & Mod Managers',
     "description": 'FOSS launcher for Epic Games, GOG, and Amazon Games', "foss": True,
     "url": 'https://heroicgameslauncher.com/'},
    {"id": 'ItchIo.Itch', "name": 'itch.io', "category": 'Game Launchers, Stores & Mod Managers',
     "description": 'Indie game marketplace client', "foss": True,
     "url": 'https://itch.io/app'},
    {"id": 'Mojang.MinecraftLauncher', "name": 'Minecraft Launcher', "category": 'Game Launchers, Stores & Mod Managers',
     "description": 'Official launcher for Minecraft Java and Bedrock editions', "foss": False,
     "url": 'https://www.minecraft.net/'},
    {"id": 'ModOrganizer2.modorganizer', "name": 'Mod Organizer 2', "category": 'Game Launchers, Stores & Mod Managers',
     "description": 'Best mod manager for Skyrim and Fallout', "foss": True,
     "url": 'https://modorganizer.org/'},
    {"id": 'Playnite.Playnite', "name": 'Playnite', "category": 'Game Launchers, Stores & Mod Managers',
     "description": 'Unified FOSS game library manager with full emulator and store integration', "foss": True,
     "url": 'https://playnite.link/'},
    {"id": 'PrismLauncher.PrismLauncher', "name": 'Prism Launcher', "category": 'Game Launchers, Stores & Mod Managers',
     "description": 'FOSS Minecraft launcher with multi-instance and modpack management', "foss": True,
     "url": 'https://prismlauncher.org/'},
    {"id": 'ebkr.r2modman', "name": 'r2modman', "category": 'Game Launchers, Stores & Mod Managers',
     "description": 'Clean FOSS Thunderstore mod manager for Lethal Company, Valheim, and RoR2', "foss": True,
     "url": 'https://thunderstore.io/package/ebkr/r2modman/'},
    {"id": 'Roblox.Roblox', "name": 'Roblox Player', "category": 'Game Launchers, Stores & Mod Managers',
     "description": 'Official desktop client for Roblox games', "foss": False,
     "url": 'https://www.roblox.com/'},
    {"id": 'RockstarGames.Launcher', "name": 'Rockstar Games Launcher', "category": 'Game Launchers, Stores & Mod Managers',
     "description": 'PC launcher for GTA and Red Dead games', "foss": False,
     "url": 'https://www.rockstargames.com/'},
    {"id": 'Valve.Steam', "name": 'Steam', "category": 'Game Launchers, Stores & Mod Managers',
     "description": 'The primary digital PC gaming storefront and community', "foss": False,
     "url": 'https://store.steampowered.com/'},
    {"id": 'Ubisoft.Connect', "name": 'Ubisoft Connect', "category": 'Game Launchers, Stores & Mod Managers',
     "description": "Ubisoft's launcher for Assassin's Creed, Far Cry and Rainbow Six", "foss": False,
     "url": 'https://ubisoftconnect.com/'},
    {"id": 'NexusMods.Vortex', "name": 'Vortex Mod Manager', "category": 'Game Launchers, Stores & Mod Managers',
     "description": 'Nexus mod manager for Cyberpunk, BG3 and more', "foss": True,
     "url": 'https://www.nexusmods.com/about/vortex/'},
    # C. Gaming Tools & Emulators
    {"id": 'AntiMicroX.antimicrox', "name": 'AntiMicroX', "category": 'Gaming Tools & Emulators',
     "description": 'Maps controller inputs to keyboard/mouse with virtual gamepad emulation', "foss": True,
     "url": 'https://github.com/AntiMicroX/antimicrox'},
    {"id": 'AzaharEmu.Azahar', "name": 'Azahar (3DS Emulator)', "category": 'Gaming Tools & Emulators',
     "description": 'Open-source Nintendo 3DS emulator with 4K upscaling (the maintained Citra/Lime3DS continuation)', "foss": True,
     "url": 'https://azahar-emu.org/', "fallback_url": 'https://github.com/azahar-emu/azahar'},
    {"id": 'Cemu.Cemu', "name": 'Cemu (Wii U Emulator)', "category": 'Gaming Tools & Emulators',
     "description": 'Wii U emulator (plays Zelda: Breath of the Wild at 4K/60+ FPS)', "foss": True,
     "url": 'https://cemu.info/'},
    {"id": 'DolphinEmulator.Dolphin', "name": 'Dolphin Emulator', "category": 'Gaming Tools & Emulators',
     "description": 'GameCube & Wii emulator with 4K resolution upscaling and texture packs', "foss": True,
     "url": 'https://dolphin-emu.org/'},
    {"id": 'Stenzek.DuckStation', "name": 'DuckStation', "category": 'Gaming Tools & Emulators',
     "description": 'PlayStation 1 emulator featuring PGXP geometry correction and widescreen', "foss": True,
     "url": 'https://github.com/stenzek/duckstation'},
    {"id": 'ES-DE.EmulationStation-DE', "name": 'EmulationStation Desktop Edition', "category": 'Gaming Tools & Emulators',
     "description": 'Couch-friendly emulation frontend — simpler than RetroArch for TV play', "foss": False,
     "url": 'https://es-de.org/'},
    {"id": 'PrimeBuild.LightCrosshair', "name": 'LightCrosshair', "category": 'Gaming Tools & Emulators',
     "description": 'FOSS custom crosshair overlay for borderless games — anti-cheat safe', "foss": True,
     "url": 'https://github.com/PrimeBuild-pc/LightCrosshair'},
    {"id": 'mtkennerly.ludusavi', "name": 'Ludusavi (Game Save Backup)', "category": 'Gaming Tools & Emulators',
     "description": 'Auto backup for 10,000+ PC game saves', "foss": True,
     "url": 'https://github.com/mtkennerly/ludusavi'},
    {"id": 'Blinue.Magpie', "name": 'Magpie (FSR Upscaler)', "category": 'Gaming Tools & Emulators',
     "description": 'Adds FSR, integer scaling, and Lanczos upscaling to any windowed game', "foss": True,
     "url": 'https://github.com/Blinue/Magpie'},
    {"id": 'JeffreyPfau.mGBA', "name": 'mGBA (GBA Emulator)', "category": 'Gaming Tools & Emulators',
     "description": 'Standalone Game Boy Advance emulator — simpler than RetroArch for casual play', "foss": True,
     "url": 'https://mgba.io/'},
    {"id": 'MoonlightGameStreamingProject.Moonlight', "name": 'Moonlight Game Streaming', "category": 'Gaming Tools & Emulators',
     "description": 'FOSS client to stream PC games to laptops, TVs, or Steam Deck at 120 FPS', "foss": True,
     "url": 'https://moonlight-stream.org/'},
    {"id": 'Parsec.Parsec', "name": 'Parsec', "category": 'Gaming Tools & Emulators',
     "description": 'Low-latency remote play for split-screen games', "foss": False,
     "url": 'https://parsec.app/'},
    {"id": 'PCSX2Team.PCSX2', "name": 'PCSX2', "category": 'Gaming Tools & Emulators',
     "description": 'PlayStation 2 emulator with widescreen patches and HD rendering', "foss": True,
     "url": 'https://pcsx2.net/'},
    {"id": 'PPSSPPTeam.PPSSPP', "name": 'PPSSPP', "category": 'Gaming Tools & Emulators',
     "description": 'PlayStation Portable emulator with HD resolution and texture upscaling', "foss": True,
     "url": 'https://www.ppsspp.org/'},
    {"id": 'Reshade.Setup', "name": 'ReShade', "category": 'Gaming Tools & Emulators',
     "description": 'One-click graphics injector with a per-game setup wizard', "foss": True,
     "url": 'https://reshade.me/'},
    {"id": 'Libretro.RetroArch', "name": 'RetroArch', "category": 'Gaming Tools & Emulators',
     "description": 'All-in-one frontend for retro consoles (GBA, DS, Dreamcast and more built in)', "foss": True,
     "url": 'https://www.retroarch.com/'},
    {"id": 'SteamGridDB.RomManager', "name": 'Steam ROM Manager', "category": 'Gaming Tools & Emulators',
     "description": 'Adds emulated ROMs to Steam with cover art (pairs with RetroArch)', "foss": True,
     "url": 'https://github.com/SteamGridDB/steam-rom-manager'},
    {"id": 'LizardByte.Sunshine', "name": 'Sunshine', "category": 'Gaming Tools & Emulators',
     "description": 'Stream your PC games to a TV, laptop or handheld (pairs with Moonlight)', "foss": True,
     "url": 'https://docs.lizardbyte.dev/projects/sunshine/'},
    {"id": 'xemu-project.xemu', "name": 'xemu (Original Xbox Emulator)', "category": 'Gaming Tools & Emulators',
     "description": 'Original Xbox emulator with higher resolutions', "foss": True,
     "url": 'https://xemu.app/'},
    {"id": 'Xenia.XeniaCanary', "name": 'Xenia Canary (Xbox 360 Emulator)', "category": 'Gaming Tools & Emulators',
     "description": 'Actively maintained Xbox 360 emulator — now on winget (portable build; auto-installs the VC++ runtime it needs)', "foss": True,
     "url": 'https://github.com/xenia-canary/xenia-canary-releases',
     "fallback_url": 'https://github.com/xenia-canary/xenia-canary-releases/releases'},
    # D. Web Browsers
    {"id": 'Brave.Brave', "name": 'Brave Browser', "category": 'Web Browsers',
     "description": 'Chromium-based browser with built-in ad/tracker blocking and low RAM usage', "foss": True,
     "url": 'https://brave.com/'},
    {"id": 'DuckDuckGo.DesktopBrowser', "name": 'DuckDuckGo Privacy Browser', "category": 'Web Browsers',
     "description": 'Private browser with tracker blocking', "foss": True,
     "url": 'https://duckduckgo.com/windows'},
    {"id": 'Google.Chrome', "name": 'Google Chrome', "category": 'Web Browsers',
     "description": 'Standard Chromium browser for Google account syncing and ecosystem tools', "foss": False,
     "url": 'https://www.google.com/chrome/'},
    {"id": 'Mozilla.Firefox', "name": 'Mozilla Firefox', "category": 'Web Browsers',
     "description": 'Independent open-source browser with rich privacy and extension support', "foss": True,
     "url": 'https://www.mozilla.org/firefox/'},
    {"id": 'Opera.OperaGX', "name": 'Opera GX', "category": 'Web Browsers',
     "description": 'Gamer browser with RAM/CPU limiters and a Twitch sidebar', "foss": False,
     "url": 'https://www.opera.com/gx'},
    {"id": 'Vivaldi.Vivaldi', "name": 'Vivaldi', "category": 'Web Browsers',
     "description": 'Power browser with built-in adblock, mail and tab stacks', "foss": False,
     "url": 'https://vivaldi.com/'},
    {"id": 'Zen-Team.Zen-Browser', "name": 'Zen Browser', "category": 'Web Browsers',
     "description": 'High-speed modern Firefox fork with native vertical tabs and split view', "foss": True,
     "url": 'https://zen-browser.app/'},
    # E. Media, Streaming & Audio
    {"id": 'Audacity.Audacity', "name": 'Audacity', "category": 'Media, Streaming & Audio',
     "description": 'Multi-track audio recorder, editor, and mic noise remover', "foss": True,
     "url": 'https://www.audacityteam.org/'},
    {"id": 'ChatterinoTeam.Chatterino', "name": 'Chatterino', "category": 'Media, Streaming & Audio',
     "description": 'Lightweight Twitch chat with 7TV/BTTV emotes', "foss": True,
     "url": 'https://chatterino.com/'},
    {"id": 'Discord.Discord', "name": 'Discord', "category": 'Media, Streaming & Audio',
     "description": 'Standard gaming voice, chat, and community platform', "foss": False,
     "url": 'https://discord.com/'},
    {"id": 'Digimezzo.Dopamine.3', "name": 'Dopamine', "category": 'Media, Streaming & Audio',
     "description": 'Clean minimalist local music player', "foss": True,
     "url": 'https://github.com/digimezzo/dopamine-windows/releases/latest'},
    {"id": 'File-New-Project.EarTrumpet', "name": 'EarTrumpet', "category": 'Media, Streaming & Audio',
     "description": 'Per-app volume mixer that lives in your taskbar', "foss": True,
     "url": 'https://eartrumpet.app/'},
    {"id": 'Elgato.CameraHub', "name": 'Elgato Camera Hub', "category": 'Media, Streaming & Audio',
     "description": 'One-screen webcam tuning for streams and calls', "foss": False,
     "url": 'https://www.elgato.com/downloads'},
    {"id": 'Elgato.WaveLink', "name": 'Elgato Wave Link', "category": 'Media, Streaming & Audio',
     "description": 'Stream audio mixer for mic, game and Discord levels', "foss": False,
     "url": 'https://www.elgato.com/downloads'},
    {"id": 'PeterPawlowski.foobar2000', "name": 'foobar2000', "category": 'Media, Streaming & Audio',
     "description": 'Tiny veteran music player with gapless playback', "foss": False,
     "url": 'https://www.foobar2000.org/'},
    {"id": 'FreeTube.FreeTube', "name": 'FreeTube', "category": 'Media, Streaming & Audio',
     "description": 'Private ad-free YouTube client with SponsorBlock and local subscriptions', "foss": True,
     "url": 'https://freetubeapp.io/'},
    {"id": 'FxSound.FxSound', "name": 'FxSound', "category": 'Media, Streaming & Audio',
     "description": 'Real-time audio booster and equalizer — hear footsteps clearly in FPS games', "foss": True,
     "url": 'https://www.fxsound.com/'},
    {"id": 'HandBrake.HandBrake', "name": 'HandBrake', "category": 'Media, Streaming & Audio',
     "description": 'GPU-accelerated video compressor and clip transcoder', "foss": True,
     "url": 'https://handbrake.fr/'},
    {"id": 'Apple.iTunes', "name": 'iTunes', "category": 'Media, Streaming & Audio',
     "description": 'Apple music library plus iPhone sync and local backups', "foss": False,
     "url": 'https://www.apple.com/itunes/'},
    {"id": 'Jellyfin.JellyfinMediaPlayer', "name": 'Jellyfin Media Player', "category": 'Media, Streaming & Audio',
     "description": 'TV-style player for a Jellyfin home server', "foss": True,
     "url": 'https://jellyfin.org/'},
    {"id": 'Jellyfin.Server', "name": 'Jellyfin Server', "category": 'Media, Streaming & Audio',
     "description": 'Self-hosted media server — your own Netflix for the house', "foss": True,
     "url": 'https://jellyfin.org/'},
    {"id": 'XBMCFoundation.Kodi', "name": 'Kodi', "category": 'Media, Streaming & Audio',
     "description": 'Living-room TV interface for local and streaming media', "foss": True,
     "url": 'https://kodi.tv/'},
    {"id": 'LMMS.LMMS', "name": 'LMMS', "category": 'Media, Streaming & Audio',
     "description": 'Free music studio for making beats, chiptunes, and game soundtracks', "foss": True,
     "url": 'https://lmms.io/'},
    {"id": 'FlorianHeidenreich.Mp3tag', "name": 'Mp3tag', "category": 'Media, Streaming & Audio',
     "description": 'Batch metadata and cover-art editor for audio libraries', "foss": False,
     "url": 'https://www.mp3tag.de/en/'},
    {"id": 'clsid2.mpc-hc', "name": 'MPC-HC', "category": 'Media, Streaming & Audio',
     "description": 'Hardware-accelerated media player — maintained community fork of Media Player Classic', "foss": True,
     "url": 'https://github.com/clsid2/mpc-hc'},
    {"id": '9P4CLT2RJ1RS', "name": 'MusicBee', "category": 'Media, Streaming & Audio',
     "description": 'Full-featured local music player and library manager', "foss": False,
     "url": 'https://getmusicbee.com/'},
    {"id": 'MusicBrainz.Picard', "name": 'MusicBrainz Picard', "category": 'Media, Streaming & Audio',
     "description": 'Auto-tagger that fingerprints songs to fix missing tags and album artwork', "foss": True,
     "url": 'https://picard.musicbrainz.org/'},
    {"id": 'OBSProject.OBSStudio', "name": 'OBS Studio', "category": 'Media, Streaming & Audio',
     "description": 'Open-source live streaming, screen recording, and broadcasting software', "foss": True,
     "url": 'https://obsproject.com/'},
    {"id": 'Nickvision.Parabolic', "name": 'Parabolic', "category": 'Media, Streaming & Audio',
     "description": 'Modern video and audio downloader (yt-dlp frontend)', "foss": True,
     "url": 'https://nickvision.org/', "fallback_url": 'https://github.com/NickvisionApps/Parabolic'},
    {"id": 'Plex.Plex', "name": 'Plex', "category": 'Media, Streaming & Audio',
     "description": 'Watch your Plex server media anywhere, with downloads', "foss": False,
     "url": 'https://www.plex.tv/'},
    {"id": 'Plex.PlexMediaServer', "name": 'Plex Media Server', "category": 'Media, Streaming & Audio',
     "description": 'Self-hosted streaming server with remote watch and downloads', "foss": False,
     "url": 'https://www.plex.tv/'},
    {"id": 'Meltytech.Shotcut', "name": 'Shotcut', "category": 'Media, Streaming & Audio',
     "description": 'Lightweight FOSS video editor that runs great on low-spec PCs', "foss": True,
     "url": 'https://shotcut.org/'},
    {"id": 'PaulPacifico.ShutterEncoder', "name": 'Shutter Encoder', "category": 'Media, Streaming & Audio',
     "description": 'All-in-one lossless trimmer, converter and MKV-to-MP4 rewrapper', "foss": True,
     "url": 'https://www.shutterencoder.com/'},
    {"id": 'OpenWhisperSystems.Signal', "name": 'Signal', "category": 'Media, Streaming & Audio',
     "description": 'Private messenger with dead-simple setup', "foss": True,
     "url": 'https://signal.org/'},
    {"id": 'Soundux.Soundux.Portable', "name": 'Soundux', "category": 'Media, Streaming & Audio',
     "description": 'FOSS soundboard for in-game and Discord voice (Soundpad alternative; portable build)', "foss": True,
     "url": 'https://soundux.rocks/', "fallback_url": 'https://github.com/Soundux/Soundux/releases'},
    {"id": 'Spotify.Spotify', "name": 'Spotify', "category": 'Media, Streaming & Audio',
     "description": 'Desktop music and podcast streaming client', "foss": False,
     "url": 'https://www.spotify.com/'},
    {"id": 'KRTirtho.Spotube', "name": 'Spotube', "category": 'Media, Streaming & Audio',
     "description": 'Spotify client that plays and downloads tracks as MP3/FLAC', "foss": True,
     "url": 'https://spotube.cc/'},
    {"id": 'SteelSeries.GG', "name": 'SteelSeries GG', "category": 'Media, Streaming & Audio',
     "description": 'Free headset EQ (Sonar) plus automatic game-clip capture', "foss": False,
     "url": 'https://steelseries.com/gg'},
    {"id": 'Stremio.Stremio', "name": 'Stremio', "category": 'Media, Streaming & Audio',
     "description": 'All-in-one media aggregator for videos, series, and channels', "foss": True,
     "url": 'https://www.stremio.com/'},
    {"id": 'TeamSpeakSystems.TeamSpeakClient', "name": 'TeamSpeak 3', "category": 'Media, Streaming & Audio',
     "description": 'Low-latency tactical voice chat for sim-racing, ARMA, and competitive teams', "foss": False,
     "url": 'https://www.teamspeak.com/'},
    {"id": 'Telegram.TelegramDesktop', "name": 'Telegram', "category": 'Media, Streaming & Audio',
     "description": 'Fast casual messenger with huge group chats', "foss": True,
     "url": 'https://desktop.telegram.org/'},
    {"id": 'VideoLAN.VLC', "name": 'VLC Media Player', "category": 'Media, Streaming & Audio',
     "description": 'Versatile open-source media player supporting all audio and video formats', "foss": True,
     "url": 'https://www.videolan.org/vlc/'},
    {"id": '9NKSQGP7F2NH', "name": 'WhatsApp', "category": 'Media, Streaming & Audio',
     "description": 'The default messenger for non-techie family and friends', "foss": False,
     "url": 'https://www.whatsapp.com/'},
    {"id": 'th-ch.YouTubeMusic', "name": 'YouTube Music', "category": 'Media, Streaming & Audio',
     "description": 'YouTube Music desktop app with adblock and SponsorBlock', "foss": True,
     "url": 'https://github.com/th-ch/youtube-music'},
    {"id": 'Zoom.Zoom', "name": 'Zoom Workplace', "category": 'Media, Streaming & Audio',
     "description": 'Everyone-has-it video calls (40-minute limit on free group calls)', "foss": False,
     "url": 'https://www.zoom.com/'},
    # F. Creative & Productivity
    {"id": 'BlenderFoundation.Blender', "name": 'Blender', "category": 'Creative & Productivity',
     "description": 'Professional 3D modeling, animation, VFX, and game asset creation suite', "foss": True,
     "url": 'https://www.blender.org/'},
    {"id": 'calibre.calibre', "name": 'calibre', "category": 'Creative & Productivity',
     "description": 'Ebook library manager and converter for Kindle and Kobo', "foss": True,
     "url": 'https://calibre-ebook.com/'},
    {"id": 'GIMP.GIMP', "name": 'GIMP', "category": 'Creative & Productivity',
     "description": 'Open-source image editor', "foss": True,
     "url": 'https://www.gimp.org/'},
    {"id": 'Greenshot.Greenshot', "name": 'Greenshot', "category": 'Creative & Productivity',
     "description": 'One-key screenshots with instant crop, arrows and blur', "foss": True,
     "url": 'https://getgreenshot.org/'},
    {"id": 'Inkscape.Inkscape', "name": 'Inkscape', "category": 'Creative & Productivity',
     "description": 'FOSS vector graphics editor (Illustrator alternative)', "foss": True,
     "url": 'https://inkscape.org/'},
    {"id": 'KDE.Kdenlive', "name": 'Kdenlive', "category": 'Creative & Productivity',
     "description": 'Full FOSS video editor for game montages and YouTube (Premiere alternative)', "foss": True,
     "url": 'https://kdenlive.org/'},
    {"id": 'KDE.Krita', "name": 'Krita', "category": 'Creative & Productivity',
     "description": 'Professional FOSS digital painting, sketching, and concept art studio', "foss": True,
     "url": 'https://krita.org/'},
    {"id": 'TheDocumentFoundation.LibreOffice', "name": 'LibreOffice', "category": 'Creative & Productivity',
     "description": 'Complete open-source office suite', "foss": True,
     "url": 'https://www.libreoffice.org/'},
    {"id": 'Notepad++.Notepad++', "name": 'Notepad++', "category": 'Creative & Productivity',
     "description": 'Fast, tabbed text editor for tweaking game configs (.ini/.json) and code', "foss": True,
     "url": 'https://notepad-plus-plus.org/'},
    {"id": 'Obsidian.Obsidian', "name": 'Obsidian', "category": 'Creative & Productivity',
     "description": 'Private local Markdown notes with graph view', "foss": False,
     "url": 'https://obsidian.md/'},
    {"id": 'KDE.Okular', "name": 'Okular', "category": 'Creative & Productivity',
     "description": 'Tabbed PDF reader with annotations and dark mode', "foss": True,
     "url": 'https://okular.kde.org/'},
    {"id": 'ONLYOFFICE.DesktopEditors', "name": 'ONLYOFFICE Desktop Editors', "category": 'Creative & Productivity',
     "description": 'FOSS office suite with true MS Office fidelity', "foss": True,
     "url": 'https://www.onlyoffice.com/'},
    {"id": 'OpenShot.OpenShot', "name": 'OpenShot Video Editor', "category": 'Creative & Productivity',
     "description": 'Beginner-friendly FOSS video editor with a simple drag-and-drop timeline', "foss": True,
     "url": 'https://www.openshot.org/'},
    {"id": 'dotPDN.PaintDotNet', "name": 'Paint.NET', "category": 'Creative & Productivity',
     "description": 'Fast, layer-based image and photo editor for Windows', "foss": False,
     "url": 'https://www.getpaint.net/'},
    {"id": 'geeksoftwareGmbH.PDF24Creator', "name": 'PDF24 Creator', "category": 'Creative & Productivity',
     "description": 'Free offline toolbox to merge, split, compress, and edit PDF documents', "foss": False,
     "url": 'https://www.pdf24.org/'},
    {"id": 'Pinta.Pinta', "name": 'Pinta', "category": 'Creative & Productivity',
     "description": 'Simple layer-based image editor — an easy Paint.NET alternative', "foss": True,
     "url": 'https://www.pinta-project.com/', "fallback_url": 'https://github.com/PintaProject/Pinta'},
    {"id": 'Proton.ProtonMail', "name": 'Proton Mail', "category": 'Creative & Productivity',
     "description": 'Encrypted email app that works with a free Proton account', "foss": False,
     "url": 'https://proton.me/mail'},
    {"id": 'jurplel.qView', "name": 'qView', "category": 'Creative & Productivity',
     "description": 'Ultra-minimal fast image viewer', "foss": True,
     "url": 'https://interversehq.com/qview/'},
    {"id": 'NickeManarin.ScreenToGif', "name": 'ScreenToGif', "category": 'Creative & Productivity',
     "description": 'FOSS screen recorder with GIF editor', "foss": True,
     "url": 'https://www.screentogif.com/'},
    {"id": 'SumatraPDF.SumatraPDF', "name": 'SumatraPDF', "category": 'Creative & Productivity',
     "description": 'Ultra-fast, lightweight open-source PDF, EPUB, MOBI, and comic reader', "foss": True,
     "url": 'https://www.sumatrapdfreader.org/'},
    {"id": 'Mozilla.Thunderbird', "name": 'Thunderbird', "category": 'Creative & Productivity',
     "description": 'Simple private desktop email that just works', "foss": True,
     "url": 'https://www.thunderbird.net/'},
    # G. Utilities & Cleaners
    {"id": 'amir1376.ABDownloadManager', "name": 'AB Download Manager', "category": 'Utilities & Cleaners',
     "description": 'Fast multi-threaded download accelerator', "foss": True,
     "url": 'https://abdownloadmanager.com/'},
    {"id": 'Malwarebytes.AdwCleaner', "name": 'AdwCleaner', "category": 'Utilities & Cleaners',
     "description": 'Free adware and junkware remover', "foss": False,
     "url": 'https://www.malwarebytes.com/adwcleaner'},
    {"id": 'AnyDesk.AnyDesk', "name": 'AnyDesk', "category": 'Utilities & Cleaners',
     "description": 'Remote into family PCs to fix things from your couch', "foss": False,
     "url": 'https://anydesk.com/'},
    {"id": 'AutoHotkey.AutoHotkey', "name": 'AutoHotkey', "category": 'Utilities & Cleaners',
     "description": 'One-key macros and hotkeys for games and apps', "foss": True,
     "url": 'https://www.autohotkey.com/'},
    {"id": 'Bitwarden.Bitwarden', "name": 'Bitwarden', "category": 'Utilities & Cleaners',
     "description": 'Cloud password manager that syncs every device', "foss": True,
     "url": 'https://bitwarden.com/'},
    {"id": 'Klocman.BulkCrapUninstaller', "name": 'Bulk Crap Uninstaller (BCU)', "category": 'Utilities & Cleaners',
     "description": 'Batch uninstaller that cleans leftover files', "foss": True,
     "url": 'https://www.bcuninstaller.com/'},
    {"id": 'Cloudflare.Warp', "name": 'Cloudflare WARP (1.1.1.1)', "category": 'Utilities & Cleaners',
     "description": 'Fast free encrypted tunnel for safer browsing', "foss": False,
     "url": 'https://one.one.one.one/'},
    {"id": 'Ditto.Ditto', "name": 'Ditto', "category": 'Utilities & Cleaners',
     "description": 'Persistent searchable multi-clipboard history', "foss": True,
     "url": 'https://ditto-cp.sourceforge.io/'},
    {"id": 'Dropbox.Dropbox', "name": 'Dropbox', "category": 'Utilities & Cleaners',
     "description": 'Simple cloud sync for docs and clips', "foss": False,
     "url": 'https://www.dropbox.com/'},
    {"id": 'voidtools.Everything', "name": 'Everything Search', "category": 'Utilities & Cleaners',
     "description": 'Locates any file or folder across your entire PC in milliseconds', "foss": False,
     "url": 'https://www.voidtools.com/'},
    {"id": 'flux.flux', "name": 'F.lux', "category": 'Utilities & Cleaners',
     "description": 'Warm screen tint after dark — easier eyes for late sessions', "foss": False,
     "url": 'https://justgetflux.com/'},
    {"id": 'Flow-Launcher.Flow-Launcher', "name": 'Flow Launcher', "category": 'Utilities & Cleaners',
     "description": 'Alt+Space launcher for files, games and math', "foss": True,
     "url": 'https://www.flowlauncher.com/'},
    {"id": 'Google.GoogleDrive', "name": 'Google Drive', "category": 'Utilities & Cleaners',
     "description": 'Cloud backup that keeps game clips off your SSD', "foss": False,
     "url": 'https://www.google.com/drive/'},
    {"id": 'KeePassXCTeam.KeePassXC', "name": 'KeePassXC', "category": 'Utilities & Cleaners',
     "description": 'FOSS offline password manager for game and app logins', "foss": True,
     "url": 'https://keepassxc.org/'},
    {"id": 'rocksdanister.LivelyWallpaper', "name": 'Lively Wallpaper', "category": 'Utilities & Cleaners',
     "description": 'Free FOSS animated wallpaper engine (auto-pauses while gaming)', "foss": True,
     "url": 'https://www.rocksdanister.com/lively/'},
    {"id": 'LocalSend.LocalSend', "name": 'LocalSend', "category": 'Utilities & Cleaners',
     "description": 'FOSS AirDrop alternative for PC and phones', "foss": True,
     "url": 'https://localsend.org/'},
    {"id": 'MacroDeck.MacroDeck', "name": 'Macro Deck', "category": 'Utilities & Cleaners',
     "description": 'FOSS Stream Deck alternative that turns a phone or tablet into macro buttons', "foss": True,
     "url": 'https://macrodeck.org/'},
    {"id": 'Mega.MEGASync', "name": 'MEGA', "category": 'Utilities & Cleaners',
     "description": 'Generous free encrypted cloud storage', "foss": False,
     "url": 'https://mega.io/'},
    {"id": 'Microsoft.PowerToys', "name": 'Microsoft PowerToys', "category": 'Utilities & Cleaners',
     "description": 'Official Microsoft utility set: window manager, color picker, bulk renamer, and more', "foss": True,
     "url": 'https://learn.microsoft.com/en-us/windows/powertoys/'},
    {"id": 'M2Team.NanaZip', "name": 'NanaZip', "category": 'Utilities & Cleaners',
     "description": 'Modernized 7-Zip fork with native Windows 11 context menus', "foss": True,
     "url": 'https://github.com/M2Team/NanaZip'},
    {"id": 'Cyanfish.NAPS2', "name": 'NAPS2', "category": 'Utilities & Cleaners',
     "description": 'Simple scan-to-PDF tool that works with almost any scanner', "foss": True,
     "url": 'https://www.naps2.com/'},
    {"id": 'OO-Software.ShutUp10', "name": 'O&O ShutUp10++', "category": 'Utilities & Cleaners',
     "description": 'Free one-click Windows privacy switches', "foss": False,
     "url": 'https://www.oo-software.com/en/shutup10'},
    {"id": 'OpenVPNTechnologies.OpenVPN', "name": 'OpenVPN', "category": 'Utilities & Cleaners',
     "description": 'Connect to any OpenVPN server or privacy provider', "foss": True,
     "url": 'https://openvpn.net/'},
    {"id": 'Proton.ProtonDrive', "name": 'Proton Drive', "category": 'Utilities & Cleaners',
     "description": 'Encrypted cloud drive that pairs with Proton Mail', "foss": False,
     "url": 'https://proton.me/drive'},
    {"id": 'Proton.ProtonVPN', "name": 'Proton VPN', "category": 'Utilities & Cleaners',
     "description": 'Audited Swiss VPN with unlimited free data', "foss": True,
     "url": 'https://protonvpn.com/'},
    {"id": 'QL-Win.QuickLook', "name": 'QuickLook', "category": 'Utilities & Cleaners',
     "description": 'Press Spacebar to preview files in Explorer', "foss": True,
     "url": 'https://github.com/QL-Win/QuickLook'},
    {"id": 'ShareX.ShareX', "name": 'ShareX', "category": 'Utilities & Cleaners',
     "description": 'Screen capture, active-window recorder, GIF generator, and OCR utility', "foss": True,
     "url": 'https://getsharex.com/'},
    {"id": 'TeamViewer.TeamViewer', "name": 'TeamViewer', "category": 'Utilities & Cleaners',
     "description": 'Remote help for family PCs (free for personal use)', "foss": False,
     "url": 'https://www.teamviewer.com/'},
    {"id": 'JosephFinney.Text-Grab', "name": 'Text Grab', "category": 'Utilities & Cleaners',
     "description": 'Copy any text on screen — even from images, videos and games', "foss": True,
     "url": 'https://github.com/TheJoeFin/Text-Grab'},
    {"id": 'zhongyang219.TrafficMonitor.Full', "name": 'TrafficMonitor', "category": 'Utilities & Cleaners',
     "description": 'Live network, CPU, GPU and RAM monitor', "foss": True,
     "url": 'https://github.com/zhongyang219/TrafficMonitor'},
    {"id": 'CharlesMilette.TranslucentTB', "name": 'TranslucentTB', "category": 'Utilities & Cleaners',
     "description": 'Makes Windows taskbar completely transparent or frosted glass with zero lag', "foss": True,
     "url": 'https://github.com/TranslucentTB/TranslucentTB'},
    {"id": 'xanderfrangos.twinkletray', "name": 'Twinkle Tray', "category": 'Utilities & Cleaners',
     "description": 'FOSS brightness slider for all monitors', "foss": True,
     "url": 'https://github.com/xanderfrangos/twinkle-tray'},
    {"id": 'Devolutions.UniGetUI', "name": 'UniGetUI (Winget GUI)', "category": 'Utilities & Cleaners',
     "description": 'Graphical updater for all your apps', "foss": True,
     "url": 'https://unigetui.com/'},
    {"id": 'Upscayl.Upscayl', "name": 'Upscayl', "category": 'Utilities & Cleaners',
     "description": 'Free AI upscaler: screenshots to 4K/8K', "foss": True,
     "url": 'https://www.upscayl.org/'},
    {"id": 'memstechtips.Winhance', "name": 'Winhance', "category": 'Utilities & Cleaners',
     "description": 'Windows optimizer and customizer for power users', "foss": False,
     "url": 'https://memstechtips.com/my-projects/winhance/', "fallback_url": 'https://github.com/memstechtips/Winhance'},
    {"id": 'AntibodySoftware.WizTree', "name": 'WizTree', "category": 'Utilities & Cleaners',
     "description": 'Instant visual disk space scanner — finds huge game folders in seconds', "foss": False,
     "url": 'https://diskanalyzer.com/'},
    # H. Hardware Diagnostics & Tuning
    {"id": 'CXWorld.CapFrameX', "name": 'CapFrameX', "category": 'Hardware Diagnostics & Tuning',
     "description": 'Measure FPS and frametimes to see how smoothly games really run', "foss": True,
     "url": 'https://www.capframex.com/'},
    {"id": 'CPUID.CPU-Z', "name": 'CPU-Z', "category": 'Hardware Diagnostics & Tuning',
     "description": 'Live CPU, memory and motherboard specs', "foss": False,
     "url": 'https://www.cpuid.com/softwares/cpu-z.html'},
    {"id": 'CrystalDewWorld.CrystalDiskInfo', "name": 'CrystalDiskInfo', "category": 'Hardware Diagnostics & Tuning',
     "description": 'SSD and HDD health and temperature monitor', "foss": True,
     "url": 'https://crystalmark.info/'},
    {"id": 'CrystalDewWorld.CrystalDiskMark', "name": 'CrystalDiskMark', "category": 'Hardware Diagnostics & Tuning',
     "description": 'Standard SSD and HDD speed benchmark', "foss": True,
     "url": 'https://crystalmark.info/'},
    {"id": 'Wagnardsoft.DisplayDriverUninstaller', "name": 'Display Driver Uninstaller (DDU)', "category": 'Hardware Diagnostics & Tuning',
     "description": 'Completely removes GPU drivers and leftover registry keys in Safe Mode', "foss": True,
     "url": 'https://www.wagnardsoft.com/'},
    {"id": 'Rem0o.FanControl', "name": 'Fan Control', "category": 'Hardware Diagnostics & Tuning',
     "description": 'Control all CPU, GPU and case fan curves', "foss": False,
     "url": 'https://getfancontrol.com/'},
    {"id": 'TechPowerUp.GPU-Z', "name": 'GPU-Z', "category": 'Hardware Diagnostics & Tuning',
     "description": 'Detailed graphics card information, VRAM usage, and GPU sensor monitoring', "foss": False,
     "url": 'https://www.techpowerup.com/gpuz/'},
    {"id": 'REALiX.HWiNFO', "name": 'HWiNFO64', "category": 'Hardware Diagnostics & Tuning',
     "description": 'Accurate hardware temps and voltage logging', "foss": False,
     "url": 'https://www.hwinfo.com/'},
    {"id": 'Guru3D.Afterburner', "name": 'MSI Afterburner', "category": 'Hardware Diagnostics & Tuning',
     "description": 'GPU overclocking, undervolting, and in-game FPS/temp overlay (via RTSS)', "foss": False,
     "url": 'https://www.msi.com/Landing/afterburner'},
    {"id": 'OpenRGB.OpenRGB', "name": 'OpenRGB', "category": 'Hardware Diagnostics & Tuning',
     "description": 'Open-source RGB lighting control for keyboards, mice, RAM, fans and more', "foss": True,
     "url": 'https://openrgb.org/'},
    # I. Disk Imaging, VMs & Torrents
    {"id": 'Balena.Etcher', "name": 'balenaEtcher', "category": 'Disk Imaging, VMs & Torrents',
     "description": 'Dead-simple USB flasher for ISOs — Rufus without the options maze', "foss": True,
     "url": 'https://etcher.balena.io/'},
    {"id": 'qBittorrent.qBittorrent', "name": 'qBittorrent', "category": 'Disk Imaging, VMs & Torrents',
     "description": 'Ad-free, open-source BitTorrent client with integrated search engine', "foss": True,
     "url": 'https://www.qbittorrent.org/'},
    {"id": 'Rufus.Rufus', "name": 'Rufus', "category": 'Disk Imaging, VMs & Torrents',
     "description": 'Fast utility to format and create bootable USB flash drives (Windows/Linux)', "foss": True,
     "url": 'https://rufus.ie/'},
    {"id": 'Transmission.Transmission', "name": 'Transmission', "category": 'Disk Imaging, VMs & Torrents',
     "description": 'Lightweight FOSS BitTorrent client with no ads', "foss": True,
     "url": 'https://transmissionbt.com/'},
    {"id": 'Ventoy.Ventoy', "name": 'Ventoy', "category": 'Disk Imaging, VMs & Torrents',
     "description": 'Boot multiple ISOs from one USB stick', "foss": True,
     "url": 'https://www.ventoy.net/'},
    # J. Local AI & GPU Model Runners
    {"id": 'nomic.gpt4all', "name": 'GPT4All', "category": 'Local AI & GPU Model Runners',
     "description": 'Offline private AI assistant that chats with your local documents and PDFs', "foss": True,
     "url": 'https://gpt4all.io/'},
    {"id": 'Jan.Jan', "name": 'Jan AI', "category": 'Local AI & GPU Model Runners',
     "description": '100% offline, privacy-first local ChatGPT alternative powered by your GPU', "foss": True,
     "url": 'https://jan.ai/'},
    {"id": 'ElementLabs.LMStudio', "name": 'LM Studio', "category": 'Local AI & GPU Model Runners',
     "description": 'Discover and run local LLMs on your GPU', "foss": False,
     "url": 'https://lmstudio.ai/'},
    {"id": 'SST.opencode', "name": 'OpenCode', "category": 'Local AI & GPU Model Runners',
     "description": 'Open-source AI coding agent for the terminal', "foss": True,
     "url": 'https://opencode.ai/'},
]

CATEGORY_ORDER = [
    "Driver Installers & Hardware",
    "Game Launchers, Stores & Mod Managers",
    "Gaming Tools & Emulators",
    "Web Browsers",
    "Media, Streaming & Audio",
    "Creative & Productivity",
    "Utilities & Cleaners",
    "Hardware Diagnostics & Tuning",
    "Disk Imaging, VMs & Torrents",
    "Local AI & GPU Model Runners",
]


def _validate():
    ids = [a["id"] for a in APP_CATALOG]
    assert len(ids) == len(set(ids)), "duplicate winget ids in catalog"
    for a in APP_CATALOG:
        assert a["category"] in CATEGORY_ORDER, "unknown category: " + a["category"]
        assert set(a) >= {"id", "name", "category", "description", "foss", "url"}, a["id"]
    # retired categories must stay gone
    assert "Runtimes & Dependencies" not in CATEGORY_ORDER
    assert "FOSS Games & Source Ports" not in CATEGORY_ORDER
    # A-Z within each category (user request)
    for cat in CATEGORY_ORDER:
        names = [a["name"].lower() for a in APP_CATALOG if a["category"] == cat]
        assert names == sorted(names), "category not alphabetical: " + cat
    for m in MANUAL_ONLY_APPS:
        assert m["category"] in CATEGORY_ORDER, "unknown manual category: " + m["category"]
        if "fallback_url" in m:
            assert m["fallback_url"] and m["fallback_url"] != m["url"], m["id"]


_validate()
