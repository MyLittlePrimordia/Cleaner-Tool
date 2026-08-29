"""
Cleaner Tool 🧼
--------------
A 1-click Windows cleaning, repair, and tweaking GUI utility.

Package layout:
    app/__main__.py          - Entry point (python -m app)
    app/config.py            - App-wide constants, theme, metadata
    app/elevation.py         - Administrator privilege detection / relaunch
    app/utils.py             - Shared helpers (command runner, registry helpers,
                               folder cleaning, byte formatting, restore points)
    app/tasks/clean_tasks.py - Clean tab tasks
    app/tasks/repair_tasks.py- Repair tab tasks
    app/tasks/tweak_tasks.py - Tweak tab tasks
    app/gui.py               - Tkinter GUI (3 tabs, shared log/console)
"""

__version__ = "2.0.0"
