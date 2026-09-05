# Cleaner Tool

A lightweight, modular Windows 10/11 utility for disk cleaning, system repairs, and reversible performance tweaks. Built with Python and Tkinter using only the standard library.

---

## Features

- **Safe by Default**: Optional System Restore point creation before making changes, plus warning checks on risky task combinations.
- **Reversible Tweaks**: Every tweak has a dedicated one-click revert — Undo restores your machine's real prior values, not hardcoded guesses.
- **Made for Non-Tech Gamers**: Three tabs (Clean / Repair / Tweak), one big Run button, preset cards instead of checkbox walls, and a progress bar instead of a console log. The full log stays in memory and can be exported to a .txt via **Quick Tools > Export Logs**.
- **Zero Third-Party Runtime Dependencies**: Runs entirely on Python standard library modules (`tkinter`, `ctypes`, `winreg`, `subprocess`).
- **Flexible Permissions**: Runs safe cleanups without admin privileges, with optional in-app elevation for system-level repairs and tweaks.
- **Real Game Support**: Cleans logs, crash dumps, shader caches and web junk for the Steam Most-Played Top 100 titles (paths verified against PCGamingWiki) — plus Minecraft, CurseForge, OBS, Discord, Logitech G Hub and more. Game saves are never touched.
- **Auto Maintenance**: Optional scheduled runs (daily/weekly/monthly) that execute your saved task selection headlessly.

---

## What It Does

### Clean (includes the former Games tab)
- **Presets**: Quick Clean (everyday junk, fast and safe) / Deep Clean (everything, including browsers, Store, bloat) / Custom.
- **Shader & GPU Caches**: DirectX, NVIDIA, AMD, Intel, Steam per-game.
- **Launchers & Chat**: Steam, Epic, EA app, GOG, Battle.net, Riot, Ubisoft, Xbox, Rockstar, Discord, Slack, Teams, Spotify.
- **Game Files**: One mega-task covering the Steam Top-100 by player count — PUBG, Palworld, Marvel Rivals, Fortnite, S.T.A.L.K.E.R. 2, THE FINALS, Valorant, PAYDAY 2, BG3, Cyberpunk, Dead by Daylight, EFT, Total War: Warhammer 3, Football Manager, Rocket League, DayZ, Bannerlord, DST, Paradox titles, Valheim, RimWorld, Schedule I, Minecraft (Java + Bedrock), Roblox, and more. Only junk subfolders (Logs/Crashes/webcache/shader cache) are touched — saves and configs are excluded by design.
- **System Junk**: Windows/user temp files, Delivery Optimization cache, crash dumps, Recycle Bin, thumbnail caches, Windows Update leftovers (Windows.old, $WinREAgent...), and activity traces.
- **Maintenance**: Browser HTTP caches (retains cookies/passwords/history), DNS flush, real RAM working-set trim, and Windows Update cache *(Admin)*.
- **Debloat**: Removes verified bloatware package names (Clipchamp, MSN apps, TikTok, 3D Viewer, Solitaire, and more) — keeps all Xbox/Game Pass and codec packages safe.

### Repair
- **Presets**: Quick Repair (fast checks) / Deep Repair (full repair stack) / Custom.
- **System File Integrity**: `sfc /scannow` and DISM (`CheckHealth`, `ScanHealth`, `RestoreHealth`).
- **Windows Components**: Component store cleanup (safe — keeps your ability to roll back updates), Windows Update reset (with safe backups, no data-destroying races).
- **Gamer Fixes**: Xbox / Game Pass app re-registration, SSD retrim + health report, VSS (restore point) repair, network stack reset to Microsoft defaults.
- **Subsystems**: Search index rebuild, Print Spooler repair, WMI repository salvage, BITS queue reset, clock sync, Group Policy refresh.
- **Disks**: Read-only `chkdsk /scan`.

### Tweak *(Reversible — includes the former Advanced tab)*
- **Presets**: Minimal (safe speed basics) / Recommended (best all-round setup) / Custom / Undo Tweaks.
- **Power & CPU**: Hidden Ultimate Performance plan (idempotent — no duplicate schemes), aggressive CPU boost, core parking off.
- **Gaming**: Game Mode, HAGS, Game DVR off, windowed-game optimizations, game priority boost, fullscreen optimizations off.
- **UI & Input**: Classic context menu, mouse acceleration off, animation trims, taskbar cleanup (Widgets/Chat/search highlights off).
- **Network**: Network throttling removal, Nagle's algorithm, USB selective suspend off (correct GUID form), drive timeout off.
- **Privacy (one-click, fully reversible)**: Privacy Baseline (ad ID, tracking, typing data, speech opt-outs), Stop Windows Ads & Tips, Fast Local Search (no Bing), Stop Telemetry (services + tasks), NVIDIA telemetry opt-out, System-Wide Ad Blocker (~78,000 known ad/tracker domains via the hosts file, one-click revert).
- **Advanced (Custom-only)**: Memory compression, hibernation, Copilot/Recall/AI policies. (Memory Integrity, Hyper-V and WPBT tweaks were removed from the app — low/no gaming benefit relative to the risk.)

---

## Requirements

- **OS**: Windows 10 or Windows 11
- **Python**: 3.10+ (if running from source)

---

## Usage

### Run from Source
```powershell
python main.py
```

### Build Standalone `.exe`
```powershell
pip install -r requirements.txt
pyinstaller --onefile --windowed --name "CleanerTool" --icon "app/assets/icon.ico" --add-data "app/assets;assets" --manifest "app/assets/app_manifest.xml" app/__main__.py
```
The output file will be generated in the `dist/` directory. The manifest is `asInvoker` — the app asks for admin rights itself via its in-app Admin Gate screen, so it never forces a UAC prompt at launch and stays usable in limited (clean-only) mode.

---

## Project Structure

```text
Cleaner Tool/
├── main.py                     # App entry point (shim)
├── write_manifest.py           # Regenerates app/assets/app_manifest.xml
├── tools/
│   ├── smoke_gui.py            # Headless GUI smoke test (drives the real app)
│   └── capture_ui.py           # Screenshots of the real running UI
├── app/
│   ├── __main__.py             # Real entry point (python -m app)
│   ├── config.py               # Theme + window geometry
│   ├── config_persist.py       # Atomic JSON config + tweak snapshots/applied registry
│   ├── elevation.py            # UAC detection and opt-in elevation
│   ├── gui.py                  # Tkinter interface (3 tabs, presets, animated widgets)
│   ├── scheduler.py            # Headless --auto-clean runs
│   ├── tab_presets.py          # 3-tab layout + preset tiers (single source of truth)
│   ├── toast.py                # Windows toast notifications
│   ├── utils.py                # Command runner, registry helpers, restore points
│   ├── warnings.py             # Dangerous-combination warnings
│   └── tasks/
│       ├── launcher_paths.py   # Verified launcher/game junk paths (env-var built)
│       ├── clean_tasks.py       # Clean tab tasks
│       ├── repair_tasks.py     # Repair tab tasks
│       ├── tweak_tasks.py       # Tweak tab tasks
│       ├── game_tasks.py        # Games tasks (merged into Clean by tab_presets)
│       └── advanced_tasks.py    # Advanced tasks (merged into Tweak by tab_presets)
└── .github/workflows/build.yml  # CI workflow for automated builds
```

---

## Testing

```powershell
python tools/smoke_gui.py      # headless: builds the real UI, clicks every preset
python tools/capture_ui.py     # screenshots into shots/
```

---

## Adding Custom Tasks

To add a new task, define your function in the appropriate file inside `app/tasks/` and append a new `Task` definition to the list:

```python
Task(
    key="example_task",          # unique id used by the scheduler
    label="Example Task",
    description="Brief description shown in the hover tooltip.",
    func=my_custom_function,
    admin_required=True,
    default=False,
    revert=my_revert_function,   # optional — enables one-click Undo
)
```

The GUI automatically registers and renders any tasks in these lists — add new keys to a preset in `app/tab_presets.py` too if the task belongs in a curated tier. Revert functions are only required for tweaks — cleaning tasks don't need them.

### Adding games to "Clean Game Files"

Add the game's junk subfolder to `_TOP_GAME_JUNK` / `_DOC_GAME_JUNK` / `_LOW_GAME_JUNK` in `app/tasks/launcher_paths.py` using environment-variable bases (`LOCALAPPDATA`, `APPDATA`, Documents, LocalLow). **Only ever add Logs / Crashes / CrashDumps / webcache / shader-cache subfolders** — saves and configs live in the same roots for many games (see the exclusion notes in that file for titles where nothing is safe to clean).
