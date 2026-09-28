"""Windows tool shortcuts — the Quick Tools list. Pure data, no Tk, no state.

H12: `QuickToolsDialog` was reading `ToolsTab._SHORTCUTS`, i.e. a dialog
reaching into a tab's internals. That is a layering violation (a dialog must
not know a tab exists) and it would have forced the whole ToolsTab to move
before the dialog could. Extracting the table to a leaf breaks the edge in
the allowed direction, exactly as `app/task_keys.py` does for the preset
migrator's constants.

`ToolsTab._SHORTCUTS` still resolves, to this same object, so nothing that
reads the class attribute changes.
"""
from __future__ import annotations

TOOLS_SHORTCUTS = (
    ("Windows tools",
     (("Task Manager", "taskmgr"), ("Disk Cleanup", "cleanmgr"),
      ("Reliability Monitor", "perfmon /rel"), ("System Restore", "rstrui"),
      ("Power Options", "powercfg.cpl"), ("Device Manager", "devmgmt.msc"),
      ("System Information", "msinfo32"))),
    ("Windows Settings",
     (("Startup Apps", "ms-settings:startupapps"),
      ("Uninstall Programs", "ms-settings:appsfeatures"),
      ("Storage Sense", "ms-settings:storagesense"),
      ("Windows Update", "ms-settings:windowsupdate"),
      ("Network Settings", "ms-settings:network-status"),
      ("Bluetooth & Devices", "ms-settings:bluetooth"),
      ("Sound Settings", "ms-settings:sound"),
      ("App Volume", "ms-settings:apps-volume"),
      ("Display Settings", "ms-settings:display"),
      ("Graphics Settings", "ms-settings:display-advancedgraphics"))),
)
