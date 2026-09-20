"""
Component capability detection — cached, honest "is X present on this PC".

Used by LTSC/missing-component installer tasks so they can:
  * skip with an honest "already installed" instead of re-running,
  * verify after install that the component actually registered,
  * never guess from the Windows EDITION (LTSC vs Pro) — because OEM
    installs vary wildly, we check the component itself. This covers
    LTSC, N editions, and debloated Pro boxes with one mechanism.

All checks are read-only, cheap (registry/AppxPackage/where), and cached
per-process since the app only installs things once per session.
"""

import functools
import subprocess
import sys
from app.utils import resolve_exe

IS_WINDOWS = sys.platform.startswith("win")
if IS_WINDOWS:
    import winreg


# audit fix (hygiene): _run_ps() had zero callers anywhere in the app —
# removed. All capability checks here use the cheaper registry/AppxPackage
# reads instead of shelling to PowerShell.


def _appx_version(package_name: str) -> "str | None":
    """Return the registry package key suffix for an installed Appx
    package (contains the version), or None if absent. Presence is all
    callers need. Uses the per-user Appx repository registry — cheaper
    and more robust than shelling to Get-AppxPackage."""
    if not IS_WINDOWS:
        return None
    path = (r"Software\Classes\Local Settings\Software\Microsoft\Windows"
            r"\CurrentVersion\AppModel\Repository\Packages")
    try:
        root = winreg.OpenKey(winreg.HKEY_CURRENT_USER, path)
    except OSError:
        return None
    suffix = None
    i = 0
    try:
        while True:
            try:
                sub = winreg.EnumKey(root, i)
                i += 1
            except OSError:
                break
            if sub.lower().startswith(package_name.lower() + "_"):
                suffix = sub
                break
    finally:
        try:
            winreg.CloseKey(root)
        except Exception:
            pass
    return suffix


@functools.lru_cache(maxsize=None)
def has_store() -> bool:
    """Microsoft Store installed (per-user Appx)."""
    return _appx_version("Microsoft.WindowsStore") is not None


@functools.lru_cache(maxsize=None)
def has_winget() -> bool:
    """winget CLI present (comes with Store's DesktopAppInstaller)."""
    if not IS_WINDOWS:
        return False
    try:
        # shell=False with a list (shell=True + list is wrong-shaped on
        # Windows and needlessly spawns cmd.exe for a PATH lookup).
        r = subprocess.run(
            [resolve_exe("where"), "winget"], shell=False, capture_output=True, text=True, timeout=10,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        return r.returncode == 0 and "winget" in (r.stdout or "").lower()
    except Exception:
        return False


@functools.lru_cache(maxsize=None)
def has_game_bar() -> bool:
    """Xbox Game Bar (Win+G overlay) installed."""
    return _appx_version("Microsoft.XboxGamingOverlay") is not None


@functools.lru_cache(maxsize=None)
def has_xbox_app() -> bool:
    """Xbox app + Gaming Services (Game Pass support)."""
    return _appx_version("Microsoft.GamingApp") is not None


@functools.lru_cache(maxsize=None)
def has_gaming_services() -> bool:
    """Microsoft Gaming Services (GamingServices — Game Pass install/auth)."""
    return _appx_version("Microsoft.GamingServices") is not None


@functools.lru_cache(maxsize=None)
def has_xbox_identity_provider() -> bool:
    """Xbox Identity Provider — the sign-in piece Game Pass games need.
    Stripped on LTSC; without it Game Pass logins fail even when the
    Xbox app and Gaming Services are present."""
    return _appx_version("Microsoft.XboxIdentityProvider") is not None


@functools.lru_cache(maxsize=None)
def has_av1_codec() -> bool:
    return _appx_version("Microsoft.AV1VideoExtension") is not None


@functools.lru_cache(maxsize=None)
def has_vp9_codec() -> bool:
    return _appx_version("Microsoft.VP9VideoExtensions") is not None


@functools.lru_cache(maxsize=None)
def has_web_media_extension() -> bool:
    """Web Media Extensions — the free OEM HEVC-compatible media pack
    (what we install instead of the $0.99 HEVC extension)."""
    return _appx_version("Microsoft.WebMediaExtension") is not None


# --------------------------------------------------------------------------- #
# LTSC everyday-app checks (2026-09): Pro-inbox Store apps that LTSC strips
# (or ships legacy-only: Notepad/Paint/Snipping Tool). Same _appx_version
# mechanism as above — never guess from the Windows edition. Every Store ID
# below was live-verified via `winget show --exact --id <id> --source
# msstore` on 2026-09-20 before being hardcoded.
# --------------------------------------------------------------------------- #

@functools.lru_cache(maxsize=None)
def has_camera_app() -> bool:
    """Windows Camera (9WZDNCRFJBBG) — webcam photos/video on stripped Windows."""
    return _appx_version("Microsoft.WindowsCamera") is not None


@functools.lru_cache(maxsize=None)
def has_photos_app() -> bool:
    """Microsoft Photos (9WZDNCRFJBH4) — screenshots won't open without it."""
    return _appx_version("Microsoft.Windows.Photos") is not None


@functools.lru_cache(maxsize=None)
def has_webp_extension() -> bool:
    """WebP Image Extension (9PG2DK419DRG) — thumbnails/photos need it."""
    return _appx_version("Microsoft.WebPImageExtension") is not None


@functools.lru_cache(maxsize=None)
def has_heif_extension() -> bool:
    """HEIF Image Extension (9PMMSR1CGPWG) — iPhone photos need it."""
    return _appx_version("Microsoft.HEIFImageExtension") is not None


@functools.lru_cache(maxsize=None)
def has_raw_image_extension() -> bool:
    """Raw Image Extension (9NCTDW2W1BH8) — camera RAWs need it."""
    return _appx_version("Microsoft.RawImageExtension") is not None


@functools.lru_cache(maxsize=None)
def has_snipping_tool() -> bool:
    """Snipping Tool, new Store build with video clipping (9MZ95KL8MR0L).
    LTSC ships the legacy build only — detected via the shared ScreenSketch
    package both builds register."""
    return _appx_version("Microsoft.ScreenSketch") is not None


@functools.lru_cache(maxsize=None)
def has_media_player() -> bool:
    """Windows Media Player, new Store build (9WZDNCRFJ3PT)."""
    return _appx_version("Microsoft.ZuneMusic") is not None


@functools.lru_cache(maxsize=None)
def has_mpeg2_extension() -> bool:
    """MPEG-2 Video Extension (9N95Q1ZZPMH4) — rare game cutscenes need it."""
    return _appx_version("Microsoft.MPEG2VideoExtension") is not None


@functools.lru_cache(maxsize=None)
def has_notepad_app() -> bool:
    """Windows Notepad, new tabbed Store build (9MSMLRH6LZF3)."""
    return _appx_version("Microsoft.WindowsNotepad") is not None


@functools.lru_cache(maxsize=None)
def has_paint_app() -> bool:
    """Paint, new Store build with layers (9PCFS5B6T72H)."""
    return _appx_version("Microsoft.Paint") is not None


@functools.lru_cache(maxsize=None)
def has_calculator_app() -> bool:
    """Windows Calculator (9WZDNCRFHVN5)."""
    return _appx_version("Microsoft.WindowsCalculator") is not None


@functools.lru_cache(maxsize=None)
def has_clock_app() -> bool:
    """Windows Clock, alarms + focus (9WZDNCRFJ3PR)."""
    return _appx_version("Microsoft.WindowsAlarms") is not None


@functools.lru_cache(maxsize=None)
def has_sticky_notes() -> bool:
    """Microsoft Sticky Notes (9NBLGGH4QGHW)."""
    return _appx_version("Microsoft.MicrosoftStickyNotes") is not None


@functools.lru_cache(maxsize=None)
def has_sound_recorder() -> bool:
    """Windows Sound Recorder (9WZDNCRFHWKN)."""
    return _appx_version("Microsoft.WindowsSoundRecorder") is not None


@functools.lru_cache(maxsize=None)
def has_phone_link() -> bool:
    """Phone Link (9NMPJ99VJBWV) — Android photos/SMS/calls on PC."""
    return _appx_version("Microsoft.YourPhone") is not None


@functools.lru_cache(maxsize=None)
def has_quick_assist() -> bool:
    """Quick Assist (9P7BP5VNWKX5) — 1-click MS remote help."""
    return _appx_version("MicrosoftCorporationII.QuickAssist") is not None


def invalidate_caches() -> None:
    """Clear capability caches — call after an installer ran, so presence
    checks reflect the new state."""
    for fn in (has_store, has_winget, has_game_bar, has_xbox_app,
                has_gaming_services, has_av1_codec, has_vp9_codec,
                has_web_media_extension, has_xbox_identity_provider,
                has_camera_app, has_photos_app, has_webp_extension,
                has_heif_extension, has_raw_image_extension,
                has_snipping_tool, has_media_player, has_mpeg2_extension,
                has_notepad_app, has_paint_app, has_calculator_app,
                has_clock_app, has_sticky_notes, has_sound_recorder,
                has_phone_link, has_quick_assist):
        fn.cache_clear()
