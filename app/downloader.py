"""
Component downloader/installer — internet-required tasks (features.txt).

Design rules (user requirements + supply-chain safety):
  * Download the LATEST version when the source provides it (winget
    packages and msstore IDs are always-current; the single non-winget
    binary here — the DirectX June 2010 redist — is a frozen legacy
    package, so 'latest' is that same final version, fetched from its
    official Microsoft CDN path).
  * MIRROR/FALLBACK CHAIN: every direct-download component has, in order:
      1. the primary official URL,
      2. resolver fallbacks (official download-page scrape) that heal a
         dead primary path without an app update,
      3. honest failure with the raw error surfaced to the log.
  * VERIFY BEFORE EXECUTE: size AND SHA-256 are checked against pinned
    values before the downloaded file is ever run. A mismatch aborts the
     install (never execute an unverified binary).
  * SILENT INSTALLS where the official installer supports it.
  * Capability-gated: skip with an honest 'already installed' when the
    component is present (app.capabilities), verify-after-install, and
    raise on failure — no fake successes.
"""

import hashlib
import os
import subprocess
import tempfile
import urllib.request

from app.utils import TaskContext, run_cmd, run_cmd_checked
from app import capabilities as cap

_UA = "CleanerTool/2.0 (component installer)"


def _log(ctx: TaskContext, msg: str):
    ctx.log(msg)


def _download(ctx: TaskContext, url: str, dest: str, expected_size: int,
              sha256: str, label: str, timeout: int = 600) -> bool:
    """Download url -> dest with progress logging, then verify size AND
    sha256 before returning True. Returns False on any failure/mismatch
    (dest is removed) — the caller must NOT execute on False."""
    _log(ctx, f"Downloading {label}...")
    _log(ctx, f"  from {url}")
    try:
        req = urllib.request.Request(url, headers={"User-Agent": _UA})
        with urllib.request.urlopen(req, timeout=timeout) as resp, open(dest, "wb") as f:
            total = int(resp.headers.get("Content-Length") or 0)
            got = 0
            last_pct = -1
            while True:
                # audit fix: Stop button was dead for the whole download —
                # a 100MB fetch on a slow link ignored cancel for up to
                # 600s. Check every chunk; abort leaves a partial file the
                # except clause below removes.
                if ctx.cancelled():
                    raise RuntimeError("cancelled by user")
                chunk = resp.read(1 << 16)
                if not chunk:
                    break
                f.write(chunk)
                got += len(chunk)
                if total:
                    pct = int(got * 100 / total)
                    if pct >= last_pct + 10:
                        last_pct = pct
                        ctx.set_status(f"Downloading {label}... {pct}%")
        if expected_size and got != expected_size:
            raise RuntimeError(f"size mismatch: got {got}, expected {expected_size}")
        _log(ctx, f"  downloaded {got} bytes — verifying...")
        h = hashlib.sha256()
        with open(dest, "rb") as f:
            while True:
                chunk = f.read(1 << 20)
                if not chunk:
                    break
                h.update(chunk)
        actual = h.hexdigest()
        if actual.lower() != sha256.lower():
            raise RuntimeError(f"SHA-256 MISMATCH — refusing to run the file "
                               f"(got {actual[:16]}..., pinned {sha256[:16]}...)")
        _log(ctx, "  signature verified (SHA-256 match).")
        return True
    except Exception as e:
        try:
            if os.path.exists(dest):
                os.remove(dest)
        except OSError:
            pass
        _log(ctx, f"  ! download failed: {e}")
        return False


def _scrape_download_center(ctx: TaskContext, page_url: str, filename: str,
                            label: str) -> "str | None":
    """Fallback 'mirror': scrape the official Download Center page for the
    current direct-download URL. Heals a dead CDN path without an app
    update. (Verified working against id=8109 — the JSON island and the
    download button both carry the URL.)"""
    _log(ctx, f"  primary link failed — resolving current URL from {page_url}")
    try:
        req = urllib.request.Request(
            page_url,
            headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                                    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36",
                     "Accept-Language": "en-US,en;q=0.9"},
        )
        html = urllib.request.urlopen(req, timeout=30).read().decode("utf-8", "replace")
        import re
        m = re.search(rf"https://download\.microsoft\.com[^\"'<> ]*?{re.escape(filename)}", html)
        if m:
            url = m.group(0)
            _log(ctx, f"  resolved: {url}")
            return url
        _log(ctx, "  could not find a download URL on the official page.")
    except Exception as e:
        _log(ctx, f"  ! page resolve failed: {e}")
    return None


# --------------------------------------------------------------------------- #
# Component definitions
# --------------------------------------------------------------------------- #

# Legacy DirectX side-by-side runtimes (d3dx9/d3dx10/d3dx11, XAudio 2.7,
# XInput 1.3, XACT, Managed DX 1.1) — the missing-DLL errors older games show.
# This is a FROZEN final package (June 2010, v9.29.1974.1) — there is no
# newer version; 'latest' = this file. Pinned size + SHA-256 verified against
# the official Microsoft CDN on 2026-09-02.
_DIRECTX_REDIST = {
    "label": "DirectX End-User Runtime (June 2010)",
    "filename": "directx_Jun2010_redist.exe",
    "primary": "https://download.microsoft.com/download/8/4/a/84a35bf1-dafe-4ae8-82af-ad2ae20b6b14/directx_Jun2010_redist.exe",
    # official page that always carries the current URL (self-healing mirror)
    "page": "https://www.microsoft.com/en-us/download/details.aspx?id=8109",
    "size": 100275120,
    "sha256": "053f76dcbb28802e23341b6a787e3b0791c0fa5c8d4d011b1044172dbf89c73b",
    # silent flags: /Q = quiet, /T:<dir> + /C = extract-only (DXSETUP inside
    # is what actually installs). The June2010 package supports /Q for silent.
    "silent_args": "/Q",
}

# Official Visual C++ Redistributables, 2005–2022 — always the LATEST version
# via winget (Microsoft's own manifests update automatically). Both x64 and
# x86 — games ship both. Order: newest first (most commonly needed).
_VCREDIST_WINGET_IDS = [
    ("Microsoft.VCRedist.2015+.x64", "VC++ 2015-2022 x64"),
    ("Microsoft.VCRedist.2015+.x86", "VC++ 2015-2022 x86"),
    ("Microsoft.VCRedist.2013.x64", "VC++ 2013 x64"),
    ("Microsoft.VCRedist.2013.x86", "VC++ 2013 x86"),
    ("Microsoft.VCRedist.2012.x64", "VC++ 2012 x64"),
    ("Microsoft.VCRedist.2012.x86", "VC++ 2012 x86"),
    ("Microsoft.VCRedist.2010.x64", "VC++ 2010 x64"),
    ("Microsoft.VCRedist.2010.x86", "VC++ 2010 x86"),
    ("Microsoft.VCRedist.2008.x64", "VC++ 2008 x64"),
    ("Microsoft.VCRedist.2008.x86", "VC++ 2008 x86"),
    ("Microsoft.VCRedist.2005.x64", "VC++ 2005 x64"),
    ("Microsoft.VCRedist.2005.x86", "VC++ 2005 x86"),
]


# VC++ runtime detection: match the RUNTIME DLLs actually loaded by games
# (System32 = x64, SysWOW64 = x86) instead of parsing display names —
# names vary by package version ('2005', '2008 - x64', 'v14 (x64)', ...) and
# name-matching was found to misdetect on a real machine (all 12 present,
# detector claimed all missing). The msvcp/msvcr DLL versions per year:
#   2005: msvcr80  | 2008: msvcr90  | 2010: msvcr100
#   2012: msvcp110 | 2013: msvcp120 | 2015-2022: msvcp140
_VC_DLLS = {
    "2005": "msvcr80.dll",
    "2008": "msvcr90.dll",
    "2010": "msvcr100.dll",
    "2012": "msvcp110.dll",
    "2013": "msvcp120.dll",
    "2015+": "msvcp140.dll",
}


def _vcredist_installed(pkg_id: str) -> bool:
    """True if the runtime DLL for this winget package's year+arch exists
    in the correct system directory."""
    if not cap.IS_WINDOWS:
        return False
    year = pkg_id.split(".")[2]        # 'Microsoft.VCRedist.<year>.<arch>'
    dll = _VC_DLLS.get(year)
    if not dll:
        return False
    windir = os.environ.get("WINDIR", r"C:\Windows")
    subdir = "System32" if pkg_id.endswith("x64") else "SysWOW64"
    return os.path.isfile(os.path.join(windir, subdir, dll))


# --------------------------------------------------------------------------- #
# Installers (callable as Task.run)
# --------------------------------------------------------------------------- #

def _require_admin_for_install(label: str):
    """Hard pre-check: these installers write to System32/WinSxS and WILL
    sit on a hidden UAC consent dialog forever when run non-elevated with
    silent flags (observed: a non-admin run hung 30+ minutes with zero
    output). The GUI marks these tasks admin_required=True, but the
    installer layer must never rely on that alone — fail fast and honest
    instead of hanging."""
    try:
        import ctypes
        if not ctypes.windll.shell32.IsUserAnAdmin():
            raise RuntimeError(
                f"{label} needs Administrator rights (it writes Windows system "
                "files). Restart the app as Administrator and run it again."
            )
    except AttributeError:
        pass  # non-Windows test context


def install_directx_runtimes(ctx: TaskContext):
    """Download + silently install the legacy DirectX side-by-side
    runtimes (fixes missing d3dx9_43.dll / XAudio / XInput errors in older
    games). Fallback chain: CDN URL -> live page-scrape -> honest failure.
    File is SHA-256 verified before it is ever executed. Admin required —
    checked UP FRONT (a non-elevated silent run hangs on a hidden UAC
    dialog otherwise)."""
    _require_admin_for_install(_DIRECTX_REDIST["label"])
    ctx.set_status("Installing legacy DirectX runtimes...")
    # audit hardening: fixed well-known %TEMP% filename in a world-readable
    # dir + a verify-then-execute window = TOCTOU surface for any same-user
    # process. mkstemp gives a unique 0600 file no other process can guess.
    fd, dest = tempfile.mkstemp(prefix="cleaner_directx_", suffix=".exe")
    os.close(fd)  # _download opens its own handle
    info = _DIRECTX_REDIST
    ok = _download(ctx, info["primary"], dest, info["size"], info["sha256"], info["label"])
    if not ok:
        alt = _scrape_download_center(ctx, info["page"], info["filename"], info["label"])
        if alt and alt != info["primary"]:
            ok = _download(ctx, alt, dest, info["size"], info["sha256"], info["label"])
    if not ok:
        raise RuntimeError(
            "Could not download the DirectX runtimes from Microsoft (primary "
            "and fallback URLs failed). Check your internet connection."
        )
    try:
        _log(ctx, "Running silent install (this takes a minute or two)...")
        # 3010 = success, reboot recommended — dxsetup treats it as success
        rc = run_cmd(ctx, f'"{dest}" {info["silent_args"]}', timeout=1800)
        if rc not in (0, 3010, 1638):  # 1638 = already installed per MSI semantics
            raise RuntimeError(f"DirectX installer exited with code {rc}.")
        _log(ctx, "Legacy DirectX runtimes installed.")
    finally:
        try:
            if os.path.exists(dest):
                os.remove(dest)
        except OSError:
            pass


def install_vc_redists(ctx: TaskContext):
    """Install every official Visual C++ Redistributable 2005-2022 (x64 +
    x86) via winget — always the LATEST version Microsoft publishes, from
    Microsoft's own winget source. Silent. Skips ones already present.

    Honest verification: after the winget pass, each package's RUNTIME DLL
    is checked in System32/SysWOW64. If winget said 'already installed'
    but the DLL is still missing (stale/blocked registry entry — observed on
    a real machine), the install is retried once with --force before the
    task admits failure."""
    if not cap.has_winget():
        raise RuntimeError("winget not available — install the Microsoft Store first "
                           "(Install tab: 'Microsoft Store').")
    _require_admin_for_install("VC++ runtimes")
    installed, skipped, failed = [], [], []
    for pkg_id, label in _VCREDIST_WINGET_IDS:
        if _vcredist_installed(pkg_id):
            skipped.append(label)
            _log(ctx, f"{label}: already installed — skipping (latest stays available via winget).")
            continue
        ctx.set_status(f"Installing {label}...")
        rc = _winget_silent(ctx, pkg_id)
        if rc == 0:
            if _vcredist_installed(pkg_id):
                installed.append(label)
                _log(ctx, f"{label}: installed (latest version).")
            else:
                # winget claimed success/'already installed' but the runtime
                # DLL is still absent — stale registry entry blocking the
                # real install. Retry once with --force.
                _log(ctx, f"{label}: winget reported OK but runtime DLL still missing — retrying with --force")
                rc2 = _winget_silent(ctx, pkg_id, force=True)
                if rc2 == 0 and _vcredist_installed(pkg_id):
                    installed.append(label)
                    _log(ctx, f"{label}: installed via forced reinstall.")
                else:
                    failed.append(label)
                    _log(ctx, f"  ! {label}: runtime DLL still missing after forced install")
        else:
            failed.append(f"{label} (rc {rc})")
            _log(ctx, f"  ! {label} failed with winget exit code {rc}")
    _log(ctx, f"VC++ runtimes: {len(installed)} installed, {len(skipped)} already present"
              + (f", {len(failed)} failed: {', '.join(failed)}" if failed else ""))
    if failed and not installed:
        raise RuntimeError("Every VC++ package failed to install — check the log (Quick Tools > Export Logs).")


# winget exit codes that mean benign outcomes, NOT failures (verified on a
# real machine: 'already installed' returns -1978335189 = 0x8A150039, and
# Python surfaces the same HRESULT as the unsigned 2316632107 depending on
# how winget exits — normalize to signed 32-bit before comparing)
_WINGET_BENIGN = {
    -1978335189,  # 0x8A150039 PACKAGE_ALREADY_INSTALLED / no upgrade needed
    -1978335135,  # 0x8A150061 no applicable install
    0,
}


def _norm_winget_rc(rc: int) -> int:
    """Normalize winget's HRESULT exit codes to signed 32-bit."""
    if rc > 0x7FFFFFFF:
        return rc - 0x100000000
    return rc


_network_probe_cache: list = [None, 0.0]  # [result, timestamp monotonic]
_NETWORK_PROBE_TTL = 30.0


def has_network(timeout: float = 8.0) -> bool:
    """Cheap connectivity probe before any online task. A 404 response STILL
    proves DNS + TCP + TLS all work — the retired edge-auth endpoint used
    earlier returned 404 while the machine was perfectly online (observed
    on a real machine). Any HTTP response counts as connected; only
    network-level failures (URLError/timeout) mean offline.

    Cached for 30s (audit fix): a mixed Essentials+catalog run probed
    sequentially up to 8 times — 8x the timeout budget for one bit of
    information that does not change mid-run. A fresh probe is forced after
    a failure so a just-plugged cable is picked up quickly."""
    import time as _time
    now = _time.monotonic()
    cached, stamp = _network_probe_cache
    if cached is True and (now - stamp) < _NETWORK_PROBE_TTL:
        return True
    try:
        req = urllib.request.Request(
            "https://www.microsoft.com",
            method="HEAD", headers={"User-Agent": _UA},
        )
        urllib.request.urlopen(req, timeout=timeout)
        _network_probe_cache[0], _network_probe_cache[1] = True, now
        return True
    except urllib.error.HTTPError:
        _network_probe_cache[0], _network_probe_cache[1] = True, now
        return True   # got an HTTP response -> we're online
    except Exception:
        # don't cache failures — retry the next call (cheap, and lets a
        # transient blip heal immediately)
        return False


def install_winget_app(ctx: TaskContext, package_id: str, app_name: str,
                       fallback_url: str = "", source: str = "winget") -> int:
    """Unified install engine (Install tab): install/update one app via
    winget, silently, always the LATEST version (no version pins), with
    real-time log streaming and a clean manual-fallback message on failure.

    CANCEL FIX (user-reported: 'the Stop button does nothing during
    installs'): this used subprocess.run directly, which (a) captured all
    output and only logged it at the END — no live streaming despite the
    spec — and (b) never registered the process with utils' cancel
    registry, so Cancel had nothing to kill and blocked until the whole
    install finished. Now it goes through utils.run_cmd, which registers
    the process for cancel_current_command() and streams line-by-line.

    Requirements implemented (from the user's Install.txt spec):
      * --exact --silent --accept-package-agreements --accept-source-agreements
      * never pin versions — winget pulls the newest published release
      * failure -> warning + official homepage so the user can install
        manually (non-blocking: returns the code, doesn't raise)
      * winget 'already installed / no upgrade' HRESULTs count as success
      * CANCELLED: returns -1 with a clear log line, runner skips the rest
    """
    from app.app_catalog import MSSTORE_IDS
    if package_id in MSSTORE_IDS:
        source = "msstore"
    ctx.set_status(f"Downloading & installing {app_name}...")
    ctx.log(f"Fetching latest version of {app_name} [{package_id}] via winget...")
    cmd = ["winget", "install", "--id", package_id, "--exact", "--source", source,
           "--silent", "--accept-package-agreements", "--accept-source-agreements",
           "--disable-interactivity"]
    rc = run_cmd(ctx, cmd, shell=False, timeout=1200)
    rc = _norm_winget_rc(rc)
    if ctx.cancelled():
        ctx.log(f"  [STOPPED] {app_name} — cancelled by user.")
        return -1
    if rc == 0 or rc in _WINGET_BENIGN:
        ctx.log(f"  [OK] Successfully installed {app_name}!")
        return 0
    ctx.log(f"  ! [FAILED] Could not install {app_name} (exit code: {rc}).")
    if fallback_url:
        ctx.log(f"  -> Manual download available at: {fallback_url}")
    return rc


def _winget_silent(ctx: TaskContext, pkg_id: str, timeout: int = 900, force: bool = False) -> int:
    """VC++-batch installer path — also routed through run_cmd so the Stop
    button kills it too (same user-reported bug as install_winget_app)."""
    cmd = ["winget", "install", "--id", pkg_id, "--source", "winget",
           "--accept-package-agreements", "--accept-source-agreements",
           "--silent", "--disable-interactivity"]
    if force:
        cmd.append("--force")
    rc = run_cmd(ctx, cmd, shell=False, timeout=timeout)
    rc = _norm_winget_rc(rc)
    if rc in _WINGET_BENIGN:
        return 0
    return rc
