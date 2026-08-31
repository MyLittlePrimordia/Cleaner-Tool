# Cleaner Tool

A lightweight, modular Windows 10/11 utility for disk cleaning, system repairs, and reversible performance tweaks. Built with Python and Tkinter using only the standard library.

---

## Features

- **Safe by Default**: Includes a dry-run preview mode and optional System Restore point creation before making changes.
- **Reversible Tweaks**: Every tweak has a dedicated one-click revert function.
- **Zero Third-Party Runtime Dependencies**: Runs entirely on Python standard library modules (`tkinter`, `ctypes`, `winreg`, `subprocess`).
- **Flexible Permissions**: Runs safe cleanups without admin privileges, with optional elevation for system-level repairs and tweaks.

---

## What It Does

### 🧹 Clean
- **Shader Caches**: DirectX, NVIDIA, AMD, Intel.
- **Game Launchers**: Steam, Epic Games, EA App, Battle.net, Riot, GOG, Discord.
- **System Junk**: Windows/user temp files, Delivery Optimization cache, crash dumps, Recycle Bin, and thumbnail caches.
- **Maintenance**: Browser HTTP caches (retains cookies/passwords/history), DNS flush, RAM working-set purge, and Windows Update cache *(Admin)*.

### 🛠️ Repair *(Admin)*
- **System File Integrity**: `sfc /scannow` and DISM (`CheckHealth`, `ScanHealth`, `RestoreHealth`).
- **Windows Components**: Component store cleanup (`/ResetBase`), Windows Update reset, and built-in app re-registration.
- **Subsystems**: Network stack reset (Winsock + TCP/IP), Search index rebuild, Print Spooler repair, and WMI repository salvage.
- **Disks**: Read-only `chkdsk /scan`.

### ⚡ Tweak *(Admin & Fully Reversible)*
- **Power & System**: Unlocks the hidden "Ultimate Performance" power plan and toggles Fast Startup.
- **Gaming**: Toggles Game Mode, Hardware-Accelerated GPU Scheduling (HAGS), and disables Game DVR background recording.
- **UI & Input**: Restores the classic Windows 11 context menu, disables mouse acceleration, and trims system animations.
- **Network**: Removes multimedia network throttling (`NetworkThrottlingIndex`) and configures Nagle's algorithm.

---

## Requirements

- **OS**: Windows 10 or Windows 11
- **Python**: 3.11+ (if running from source)

---

## Usage

### Run from Source
```powershell
python main.py
```

### Build Standalone `.exe`
```powershell
pip install -r requirements.txt
pyinstaller --onefile --windowed --name "GamerOptCleaner" --icon "assets/icon.ico" --add-data "assets;assets" --manifest "app/assets/admin_manifest.xml" main.py
```
The output file will be generated in the `dist/` directory.

---

## Project Structure

```text
Cleaner/
├── main.py                     # App entry point
├── assets/                     # Icons and application assets
├── app/
│   ├── config.py               # UI themes and window geometry
│   ├── elevation.py            # UAC detection and privilege elevation
│   ├── gui.py                  # Tkinter interface
│   ├── utils.py                # System commands, registry helpers, restore points
│   └── tasks/
│       ├── clean_tasks.py      # Cache and junk cleanup definitions
│       ├── repair_tasks.py     # System repair commands (SFC, DISM, Network)
│       └── tweak_tasks.py      # Reversible registry and system tweaks
└── .github/workflows/build.yml # CI workflow for automated builds
```

---

## Adding Custom Tasks

To add a new task, define your function in the appropriate file inside `app/tasks/` and append a new `Task` definition to the list:

```python
Task(
    name="Example Task",
    description="Brief description of what this does.",
    func=my_custom_function,
    admin_required=True,
    recommended=True
)
```
The GUI automatically registers and renders any tasks in these lists.