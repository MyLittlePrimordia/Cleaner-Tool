"""Verified in-app update (U-006).

The app has always been able to *detect* a new build and hand the browser a
link to it. This adds the option to fetch and apply it in place, so a user
does not have to find the file, close the app, and replace it by hand. That
is a self-modifying, security-sensitive operation, so everything here fails
closed:

  * nothing is downloaded without a SHA-256 to check it against -- either the
    digest GitHub computes for the asset, or the published ``.sha256``
    sibling. No digest, no download;
  * a downloaded file is refused unless the bytes match the digest, the length
    is sane, AND it begins with the ``MZ`` magic every Windows executable
    starts with. The staged copy is re-verified before it is ever swapped in;
  * the swap is refused entirely unless the app is a frozen build running from
    a writable location. A source checkout, a read-only install, or a Program
    Files install (which needs Administrator) all fall back to the browser;
  * the running ``.exe`` cannot replace itself -- Windows locks it -- so a tiny
    detached ``.cmd`` waits for this process to exit, swaps the file with
    bounded retries, relaunches, and deletes itself. If the swap fails halfway
    it rolls the original back rather than leaving no exe at all.

Nothing here touches Tk (the dialog owns the pixels, this owns the truth), so
every rule above is unit-testable without a display.
"""
from __future__ import annotations

import hashlib
import os
import subprocess
import sys
import tempfile
import urllib.request

#: The release repository the app checks. Canonical here (not in app.ui.app)
#: so both the GUI and this module can name it without an upward import.
UPDATE_REPO = "MyLittlePrimordia/Cleaner-Tool"

#: Asset names published by .github/workflows/build.yml. The exe name is
#: pinned: app/update_checker.py only recognises exactly this asset.
ASSET_NAME = "Cleaner-Tool.exe"
SHA_ASSET_NAME = "Cleaner-Tool.exe.sha256"

_UA = "CleanerTool/2.0 (self-update)"

#: Sanity bounds. The real gate is the SHA-256; these only stop a wrong
#: response (an HTML error page, a multi-GB body) from being written at all.
_MIN_BYTES = 1 * 1024 * 1024
_MAX_BYTES = 400 * 1024 * 1024
_MZ = b"MZ"


# --------------------------------------------------------------------------- #
# Identity / preconditions
# --------------------------------------------------------------------------- #

def is_frozen() -> bool:
    """True when running as the packaged PyInstaller build."""
    return bool(getattr(sys, "frozen", False))


def current_exe() -> str:
    """The running executable's path (or the script path in a source run)."""
    from app.update_checker import get_current_exe_path
    return get_current_exe_path()


def _dir_writable(path: str) -> bool:
    """True if a file can really be created in `path`.

    Checked by doing it -- os.access lies on network shares and with UAC
    virtualisation, and this decision gates a file being replaced.
    """
    try:
        os.makedirs(path, exist_ok=True)
        fd, probe = tempfile.mkstemp(prefix=".ct_upd_", dir=path)
        os.close(fd)
        os.remove(probe)
        return True
    except Exception:
        return False


def can_self_update(exe_path: str = "") -> "tuple[bool, str]":
    """(True, "") when an in-place update is safe to attempt; otherwise
    (False, reason) in plain words. Never raises, never touches the network."""
    if not is_frozen():
        return False, ("This is a source checkout, not the packaged app. "
                       "Download the new build from the release page.")
    exe = exe_path or current_exe()
    if not exe or not str(exe).lower().endswith(".exe"):
        return False, "The running program is not a Windows .exe."
    if not os.path.isfile(exe):
        return False, "The running .exe could not be found on disk."
    folder = os.path.dirname(os.path.abspath(exe))
    if not _dir_writable(folder):
        return False, ("The folder that holds the app is not writable (a "
                       "Program Files install needs Administrator). Download "
                       "the new build from the release page instead.")
    return True, ""


# --------------------------------------------------------------------------- #
# Resolving the asset + its checksum
# --------------------------------------------------------------------------- #

def _asset(release_info: dict, repo: str, name: str) -> dict:
    """The release asset called `name` with a trusted download URL, or {}.

    The URL is server-controlled, so it is only accepted when
    update_checker._trusted_release_url vouches for it (https + a GitHub
    release host + the repo we asked about). 'sha256' is "" when the release
    did not publish a digest for this asset.
    """
    from app.update_checker import _trusted_release_url
    if not isinstance(release_info, dict):
        return {}
    for asset in (release_info.get("assets") or []):
        if not isinstance(asset, dict) or asset.get("name") != name:
            continue
        url = _trusted_release_url(asset.get("browser_download_url", ""), repo)
        if not url:
            return {}
        digest = str(asset.get("digest") or "")
        sha = digest.split(":", 1)[1].lower() \
            if digest.startswith("sha256:") else ""
        try:
            size = int(asset.get("size") or 0)
        except (TypeError, ValueError):
            size = 0
        return {"url": url, "sha256": sha, "size": size, "name": name}
    return {}


def parse_sha256(text: str) -> str:
    """The SHA-256 hex digest in a checksum file, or "".

    Accepts the ``shasum`` format the build publishes (``<hash>  <name>``)
    and a bare hash; never guesses. A value that is not exactly 64 lowercase
    hex characters is not a digest.
    """
    if not text:
        return ""
    for token in str(text).replace("\r", "\n").split():
        t = token.strip().lower()
        if len(t) == 64 and all(c in "0123456789abcdef" for c in t):
            return t
    return ""


def fetch_sha256(url: str, timeout: int = 20, max_bytes: int = 4096) -> str:
    """Download a ``.sha256`` file and parse it. "" on any failure."""
    try:
        req = urllib.request.Request(url, headers={"User-Agent": _UA})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read(max_bytes + 1)
    except Exception as exc:
        from app.utils import log_or_swallow
        log_or_swallow(None, "self_update.sha256_fetch", exc)
        return ""
    if len(raw) > max_bytes:
        return ""
    return parse_sha256(raw.decode("utf-8", "replace"))


def resolve_update(release_info: dict, repo: str, timeout: int = 20) -> dict:
    """Everything needed to fetch and verify the new build, or {} if it cannot
    be resolved safely.

    Prefers the digest GitHub computes for the asset; falls back to the
    published ``.sha256`` sibling. Returns {} rather than a dict with no
    checksum -- a download with nothing to verify against must never happen.
    """
    exe = _asset(release_info, repo, ASSET_NAME)
    if not exe:
        return {}
    if not exe["sha256"]:
        sha_asset = _asset(release_info, repo, SHA_ASSET_NAME)
        if sha_asset:
            exe["sha256"] = fetch_sha256(sha_asset["url"], timeout=timeout)
    if not parse_sha256(exe["sha256"]):
        return {}
    exe["sha256"] = parse_sha256(exe["sha256"])
    return exe


# --------------------------------------------------------------------------- #
# Downloading + staging
# --------------------------------------------------------------------------- #

def _quiet_remove(path: str) -> None:
    try:
        if path and os.path.exists(path):
            os.remove(path)
    except OSError:
        pass


def download_verified(url: str, sha256: str, size: int, dest: str, *,
                      on_progress=None, cancelled=None,
                      timeout: int = 120) -> "tuple[bool, str]":
    """Stream `url` to `dest`, verify it, and return (ok, message).

    Fail-closed: `dest` is removed unless the length matches `size` (when
    given), the bytes match `sha256`, and the file begins with the MZ magic.
    Mirrors app.downloader._download's guarantees without needing a
    TaskContext, so it can be exercised directly.
    """
    expected = int(size or 0)
    ceiling = expected if expected else _MAX_BYTES
    try:
        folder = os.path.dirname(os.path.abspath(dest))
        os.makedirs(folder, exist_ok=True)
        req = urllib.request.Request(url, headers={"User-Agent": _UA})
        h = hashlib.sha256()
        got = 0
        with urllib.request.urlopen(req, timeout=timeout) as resp, \
                open(dest, "wb") as f:
            while True:
                if cancelled is not None and cancelled():
                    raise RuntimeError("cancelled by user")
                chunk = resp.read(1 << 16)
                if not chunk:
                    break
                if got + len(chunk) > ceiling:
                    raise RuntimeError("the download is larger than expected")
                f.write(chunk)
                h.update(chunk)
                got += len(chunk)
                if on_progress is not None:
                    try:
                        on_progress(got, expected)
                    except Exception:
                        pass
        if expected and got != expected:
            raise RuntimeError("size mismatch (got %d, expected %d)"
                               % (got, expected))
        if got < _MIN_BYTES:
            raise RuntimeError("the download is too small to be the app")
        if h.hexdigest().lower() != parse_sha256(sha256):
            raise RuntimeError(
                "the download did not match its published SHA-256")
        with open(dest, "rb") as f:
            if f.read(2) != _MZ:
                raise RuntimeError("the download is not a Windows executable")
        return True, ""
    except Exception as exc:
        _quiet_remove(dest)
        return False, str(exc) or "download failed"


def staging_dir() -> str:
    from app.config_paths import CONFIG_DIR
    return os.path.join(str(CONFIG_DIR), "update")


def staged_path(name: str = ASSET_NAME) -> str:
    return os.path.join(staging_dir(), name)


def cleanup_staged() -> None:
    """Remove any staged update (exe + its checksum). Best-effort."""
    _quiet_remove(staged_path())
    _quiet_remove(staged_path() + ".sha256")


def _hash_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def staged_ready() -> str:
    """Path of a staged, still-verified update exe, or "" if there is none.

    Re-verified every time it is asked, so a staged file that rotted or was
    tampered with after download is not treated as ready.
    """
    exe = staged_path()
    sha_file = exe + ".sha256"
    if not (os.path.isfile(exe) and os.path.isfile(sha_file)):
        return ""
    try:
        with open(sha_file, "r", encoding="utf-8") as f:
            expected = parse_sha256(f.read())
    except Exception:
        return ""
    if not expected:
        return ""
    try:
        return exe if _hash_file(exe).lower() == expected else ""
    except Exception:
        return ""


def stage_download(url: str, sha256: str, size: int, *,
                   on_progress=None, cancelled=None,
                   timeout: int = 120) -> "tuple[bool, str]":
    """Download+verify the build into the staging folder. Returns (ok, msg).

    On success the expected digest is written beside the exe so staged_ready()
    can re-verify it later. Any failure leaves nothing staged.
    """
    cleanup_staged()
    dest = staged_path()
    ok, msg = download_verified(url, sha256, size, dest,
                                on_progress=on_progress,
                                cancelled=cancelled, timeout=timeout)
    if not ok:
        return False, msg
    try:
        with open(dest + ".sha256", "w", encoding="utf-8") as f:
            f.write("%s  %s\n" % (parse_sha256(sha256), ASSET_NAME))
    except Exception as exc:
        cleanup_staged()
        return False, "could not record the update checksum: %s" % exc
    return True, ""


# --------------------------------------------------------------------------- #
# Swapping the running exe on exit
# --------------------------------------------------------------------------- #

def build_swap_script(target_exe: str, staged_exe: str,
                      relaunch: bool = True) -> str:
    """The detached ``.cmd`` that applies a staged update.

    It waits (up to ~4 minutes) for this process to release the running exe,
    moves the old one aside, moves the new one in, relaunches, and deletes
    itself. If the move-in fails it puts the original back, so a failed update
    never leaves the user with no exe at all.
    """
    lines = [
        "@echo off",
        'set "TARGET=%s"' % target_exe,
        'set "NEW=%s"' % staged_exe,
        'set "OLD=%s.old"' % target_exe,
        "set /a n=0",
        ":wait",
        "set /a n+=1",
        'move /y "%TARGET%" "%OLD%" >nul 2>&1',
        "if not errorlevel 1 goto swap",
        "if %n% GEQ 120 goto quit",
        "ping -n 2 127.0.0.1 >nul",
        "goto wait",
        ":swap",
        'move /y "%NEW%" "%TARGET%" >nul 2>&1',
        "if errorlevel 1 goto rollback",
    ]
    if relaunch:
        lines.append('start "" "%TARGET%"')
    lines += [
        'del "%OLD%" >nul 2>&1',
        "goto quit",
        ":rollback",
        'move /y "%OLD%" "%TARGET%" >nul 2>&1',
        ":quit",
        'del "%~f0" >nul 2>&1',
    ]
    return "\r\n".join(lines) + "\r\n"


def schedule_swap(target_exe: str, staged_exe: str, *, relaunch: bool = True,
                  spawn=None) -> "tuple[bool, str]":
    """Write and launch the swap helper detached. Returns (ok, message).

    Refuses (never raises) unless the app is the frozen build and both files
    exist. `spawn` is injectable so tests can assert the launch without
    running a helper.
    """
    if not is_frozen():
        return False, "Only the packaged app can replace itself in place."
    if not os.path.isfile(target_exe) or not os.path.isfile(staged_exe):
        return False, "The files needed for the update are missing."
    try:
        script = build_swap_script(target_exe, staged_exe, relaunch=relaunch)
        fd, path = tempfile.mkstemp(prefix="ct_update_", suffix=".cmd")
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as f:
            f.write(script)
    except Exception as exc:
        return False, "Could not prepare the update: %s" % exc
    flags = 0
    for name in ("CREATE_NO_WINDOW", "DETACHED_PROCESS"):
        flags |= int(getattr(subprocess, name, 0))
    try:
        (spawn or subprocess.Popen)(
            ["cmd", "/c", path], creationflags=flags, close_fds=True,
            cwd=os.path.dirname(os.path.abspath(path)))
    except Exception as exc:
        return False, "Could not start the updater: %s" % exc
    return True, path
