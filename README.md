# Cleaner Tool

A simple 1-click Windows cleaner, repair, and tweak app for gamers. Pick what
you want, hit Run, done — no need to babysit it.

Opens with an admin prompt (UAC) so everything just works the first time —
you won't have to restart the app to unlock repairs or tweaks.


<p align="center">
  <img src="preview.png" width="900" alt="Cleaner Tool Screenshot">
</p>

## What it does

**Clean** — shader caches (NVIDIA/AMD/Intel), Steam/Epic/EA/Discord/Riot/GOG/
Battle.net/Ubisoft caches, temp files, old driver leftovers, Recycle Bin,
crash dumps, browser cache, thumbnail cache, DNS cache, and more.

**Repair** — System File Checker, DISM image repair (fixes corrupted Windows
files by downloading clean copies — no reinstall needed), network reset,
Windows Update fixes, broken Start Menu/search fixes, and a one-click restore
point before anything runs.

**Tweak** (all reversible with one click) — Ultimate Performance power plan,
Game Mode, disable Xbox background recording, 1:1 mouse aim, HAGS, exclude
game folders from antivirus scanning, stop Windows Update from restarting
mid-game, and more.

**Games** — one-click cache cleanup for Steam, Epic, EA, GOG, Battle.net,
Riot, Ubisoft, Discord, and the Xbox app.

Every tweak can be undone from the same screen. There's also a Preview Mode
that shows you exactly what would happen without changing anything.

## Requirements

Windows 10 or 11. No install needed — just run the .exe.

## Running from source

```powershell
pip install -r requirements.txt   # build tools only, the app itself is stdlib-only
python main.py
```

## Building the .exe yourself

```powershell
pyinstaller --onefile --windowed --name "CleanerTool" --icon "app/assets/icon.ico" --add-data "app/assets;app/assets" --manifest "app/assets/admin_manifest.xml" app/__main__.py
```

Or just push to GitHub — `.github/workflows/build.yml` builds it for you on
every push, and tagging a release (e.g. `v2.1.0`) publishes the .exe as a
GitHub Release automatically.
