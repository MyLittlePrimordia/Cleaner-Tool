r"""Where driver files live.

Default: a "Drivers" folder next to the .exe, because that is where a
portable tool's user looks. A .exe in Program Files (or on a read-only share)
cannot write there, so the default is only used if a probe write succeeds;
otherwise it falls back to <CleanerTool config dir>\Drivers. A folder the user
picked in Settings wins over both, if it is writable.

    <root>\downloads   .cab files exactly as downloaded
    <root>\extracted   one subfolder per driver, ready for pnputil
    <root>\backup      pnputil exports of the drivers that were replaced
"""
from __future__ import annotations

import os
import sys
import tempfile
from typing import Optional

SUBDIRS = ("downloads", "extracted", "backup")


def _exe_dir() -> str:
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    # source run: the project folder (parent of app/)
    return os.path.dirname(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))))


def writable(path: str) -> bool:
    """True if a file can really be created in `path` (creating it if
    needed). Checked by doing it - os.access lies on network shares and with
    UAC virtualisation."""
    try:
        os.makedirs(path, exist_ok=True)
        fd, probe = tempfile.mkstemp(prefix=".ct_write_", dir=path)
        os.close(fd)
        os.remove(probe)
        return True
    except Exception:
        return False


def fallback_folder() -> str:
    try:
        from app.config_paths import CONFIG_DIR
        return os.path.join(str(CONFIG_DIR), "Drivers")
    except Exception:
        return os.path.join(os.path.expanduser("~"), "CleanerTool", "Drivers")


def default_folder() -> str:
    return os.path.join(_exe_dir(), "Drivers")


def resolve(custom: Optional[str] = None) -> str:
    """The folder to use right now: custom (if writable) -> next to the exe
    (if writable) -> the fallback. Always returns something usable."""
    for cand in (str(custom or "").strip(), default_folder()):
        if cand and writable(cand):
            return os.path.abspath(cand)
    fb = fallback_folder()
    writable(fb)
    return os.path.abspath(fb)


def ensure_layout(root: str) -> dict:
    """Create the three subfolders; return {name: path}."""
    out = {}
    for name in SUBDIRS:
        p = os.path.join(root, name)
        os.makedirs(p, exist_ok=True)
        out[name] = p
    return out


def configured() -> str:
    """resolve() with the folder from the saved config."""
    try:
        from app.config_persist import load_config
        custom = load_config().get("drivers_folder", "")
    except Exception:
        custom = ""
    return resolve(custom)
