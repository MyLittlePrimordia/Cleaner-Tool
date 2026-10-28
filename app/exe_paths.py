"""Absolute paths for Windows inbox tools — a leaf module on purpose.

`resolve_exe` moved here from `app/utils.py` (audit 2026-10, R-008 third slice)
so that a leaf can resolve the same tools without importing the 1500-line
utils module. The concrete reason: `app/powershell.py` builds every PowerShell
argv in the app, and `app/utils.py` needs to import IT (its restore-point call
is one of those argv). utils -> powershell -> utils would be a cycle;
utils -> powershell -> exe_paths is not.

`app/utils.py` re-exports this name, so `from app.utils import resolve_exe`
still works everywhere. Nothing else moved: this module imports `os` and has no
app dependencies of its own.
"""
from __future__ import annotations

import os

# SEC-002 hardening: known inbox-tool absolute paths, tried before a bare
# name so a same-user-writable app/CWD directory can't shadow the real
# tool for a process that sometimes runs elevated (install/repair/tweak
# flows). Falls back to shutil.which(), then the bare name as a last
# resort (logged) so this never breaks LTSC/Server SKUs where a tool
# might live somewhere unexpected.
_EXE_CACHE: dict = {}


def resolve_exe(name: str) -> str:
    """Absolute path for a well-known Windows inbox tool (winget,
    powershell, schtasks, powercfg, tasklist, wevtutil, ping, net, ...).
    Cached per-process. Never raises — worst case returns `name` itself,
    identical to the old bare-name behavior."""
    if name in _EXE_CACHE:
        return _EXE_CACHE[name]
    result = name
    try:
        system_root = os.environ.get("SystemRoot", r"C:\Windows")
        sys32 = os.path.join(system_root, "System32")
        candidates = [os.path.join(sys32, f"{name}.exe")]
        if name == "powershell":
            # powershell.exe is NOT in System32 - it lives one level down, in
            # the versioned engine folder. Without this candidate the lookup
            # fell through to shutil.which() and took whatever PATH said, which
            # is exactly the shadowing this function exists to prevent - and
            # the app relaunches itself ELEVATED through that path. The two
            # elevation helpers used to pin this path by hand for that reason;
            # pinning it here means every caller gets the strong behaviour
            # instead of just the two that remembered.
            candidates.insert(0, os.path.join(sys32, "WindowsPowerShell",
                                              "v1.0", "powershell.exe"))
        if name == "winget":
            # winget ships via the App Installer MSIX package, not System32 —
            # WindowsApps holds a versioned real exe behind an alias stub;
            # shutil.which() finds the per-user alias correctly.
            candidates = []
        for cand in candidates:
            if os.path.isfile(cand):
                result = cand
                break
        else:
            import shutil as _shutil
            which = _shutil.which(name)
            if which:
                result = which
    except Exception:
        result = name
    _EXE_CACHE[name] = result
    return result
