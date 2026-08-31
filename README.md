# Windows Gamer Maintenance & Optimizer Suite (v2.0.0)

A 1-click Windows cleaning, repair, and performance-tweak GUI, rebuilt from
scratch after the original source was lost (recovered only as a decompiled
`.exe`) and substantially expanded with new tasks and a proper 3-tab layout.

## What changed vs. the original app

The original was a single window with one big "Run Selected" button mixing
cleaning and repair tasks together, and it force-relaunched itself as admin
the instant it opened (no explanation, no choice). This rebuild:

- Splits everything into **three tabs**: **Clean**, **Repair**, **Tweak** —
  each with its own checkboxes, Select All / Deselect All / Recommended
  Defaults buttons, and its own Run button.
- Shows an **admin-gate screen** on launch instead of silently elevating:
  it explains why admin is useful and lets the user click a button to
  relaunch elevated, or continue in a limited (Clean-only) mode.
- Adds a global **Preview Mode (dry run)** checkbox — see exactly what a run
  would delete/change and how much space it would free, without touching
  anything.
- Adds a **Revert Selected** button on the Tweak tab — every tweak is a
  documented, reversible registry/`powercfg` change with a matching revert
  function, not a one-way trip.
- Adds an optional **"Create a System Restore Point first"** task at the top
  of Repair and Tweak (checked by default) as a safety net.
- Roughly triples the number of tasks (see below) based on research into
  what popular cleaner/tweaker tools (CCleaner, BleachBit, and community
  Windows gaming-tweak guides) do, filtered down to only what's genuinely
  safe and non-destructive.
- Adds a **Quick Tools** menu (Task Manager, Disk Cleanup, Reliability
  Monitor, Event Viewer, System Restore, Power Options) for one-click access
  to native Windows tools that don't need to be reinvented.

## Tabs & tasks

### 🧹 Clean (no admin required, except the two marked)
GPU shader caches (NVIDIA/AMD/Intel/DirectX) • game launcher caches
(Steam/Epic/EA/Discord/Riot/GOG/Battle.net) • Unreal/Unity engine caches •
leftover extracted driver folders • Windows & user temp files • Windows
Update download cache *(admin)* • Delivery Optimization cache • Recycle Bin
& crash dumps • thumbnail/icon cache • browser HTTP caches (never
history/cookies/passwords) • font cache rebuild *(admin)* • Microsoft Store
cache reset • old CBS/DISM logs • DNS flush • RAM working-set purge •
Prefetch (off by default, low value, included for completeness).

**Deliberately excluded:** `Windows.old` removal (requires the official
Disk Cleanup/`cleanmgr` sageset mechanism to handle `TrustedInstaller`
permissions correctly — doing it by hand risks partial deletion) and
anything touching `C:\Windows\Installer` (orphaned-looking entries there
are frequently still referenced by installed apps' uninstallers/repairs).

### 🛠️ Repair (admin required)
Restore point checkpoint • SFC (`sfc /scannow`) • DISM quick/full health
scan • **DISM RestoreHealth** (this is the "fix Windows files without
reinstalling" option, pulling replacement files from Windows Update) •
DISM component-store cleanup • DISM `/ResetBase` deep cleanup *(Advanced —
permanently removes the ability to uninstall currently installed updates)*
• read-only `chkdsk /scan` • Windows Update component reset (the standard
Microsoft-documented stuck-update fix) • network stack reset (Winsock +
TCP/IP) • Windows Search index rebuild • print spooler / stuck print job
fix • WMI repository verify & salvage • re-register built-in Windows apps
(fixes a broken Start Menu without reinstalling Windows).

### ⚡ Tweak (admin required, all reversible via "Revert Selected")
Unlock & activate the **hidden "Ultimate Performance" power plan**
(`powercfg -duplicatescheme e9a42b02-d5df-448d-aa00-03f14749eb61` — this is
the same hidden plan Microsoft normally only exposes on Workstation SKUs) •
restore the **full/classic right-click context menu** (skips the "Show more
options" step) • disable Xbox Game Bar background recording (Game DVR) •
ensure Game Mode is on • Hardware-Accelerated GPU Scheduling (HAGS,
*reboot required*) • trim visual effects (transparency/animations/menu
delay) • disable mouse pointer acceleration • remove the multimedia
network-throttling cap (`NetworkThrottlingIndex` / `SystemResponsiveness`)
• disable Nagle's Algorithm per network adapter *(Advanced)* • disable Fast
Startup (helps some driver/dual-boot issues) • limit diagnostic data to
"Required/Basic".

**Deliberately excluded:** anything that disables Windows Defender/security
features, BCD/boot-clock hacks (`bcdedit useplatformclock`), full telemetry
kill switches beyond the Microsoft-supported "Basic" level, and any
one-way/irreversible registry surgery. If a tweak doesn't have a clean,
documented revert path, it isn't in this app.

## Requirements

- Windows 10 or 11 (some tweaks target Windows 11's context menu behavior
  specifically; they simply won't apply anything on Windows 10 where that
  key doesn't exist).
- Python 3.11+ if running from source. No third-party runtime dependencies —
  the app only uses the standard library (`tkinter`, `ctypes`, `winreg`,
  `subprocess`).

## Running from source

```powershell
python main.py
```

## Building the standalone .exe yourself

```powershell
pip install -r requirements.txt
pyinstaller --onefile --windowed --name "GamerOptCleaner" --icon "assets/icon.ico" --add-data "assets;assets" --manifest "app/assets/admin_manifest.xml" main.py
```

The built exe will be in `dist/GamerOptCleaner.exe`.

## Building automatically via GitHub Actions

Push this folder to a GitHub repo and the included
`.github/workflows/build.yml` will build `GamerOptCleaner.exe` on
`windows-latest` runners automatically:

- **Every push/PR to `main`** → builds and uploads the exe as a workflow
  artifact (Actions tab → the run → Artifacts).
- **Pushing a tag like `v2.0.0`** → also creates a GitHub Release with the
  exe attached.
- You can also trigger it manually from the **Actions** tab
  ("Run workflow" button — `workflow_dispatch`).

## Project layout

```
Cleaner/
├── main.py                     Entry point
├── requirements.txt
├── assets/
│   └── icon.ico
├── app/
│   ├── config.py                 Theme/colors/window sizing
│   ├── elevation.py               Admin detection + relaunch
│   ├── utils.py                    Command runner, registry helpers,
│   │                                folder cleaning, restore points
│   ├── gui.py                       Tkinter UI (admin gate + 3-tab window)
│   └── tasks/
│       ├── __init__.py               Shared Task dataclass
│       ├── clean_tasks.py
│       ├── repair_tasks.py
│       └── tweak_tasks.py
└── .github/workflows/build.yml    CI build → .exe artifact / release
```

## Adding a new task

Each tab is just a Python list of `Task(...)` objects — the GUI builds its
checkboxes automatically from whatever is in that list. To add a new
cleaning task, repair routine, or tweak, write a function that takes a
`TaskContext` (see `app/utils.py`) and append one `Task(...)` line to the
relevant file in `app/tasks/`. No GUI code needs to change.
