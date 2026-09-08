"""
Install tab — tasks that install curated apps via the unified winget engine.

The catalog lives in app_catalog.py (verified IDs). This module exposes:
  * one Task per catalog app is NOT built here — the Install tab works on
    user selections from the catalog UI, funneled through one runner
    (install_selected_apps) that the GUI calls with the chosen app dicts.
  * the LTSC prerequisite installers (Store / winget+UniGetUI / Xbox /
    Game Bar / codecs / WebView2 / VC++ / DirectX / .NET / Java / classic
    runtimes) are MOVED here from repair_tasks so every internet-required
    task lives on the Install tab (user request).
  * there is deliberately NO "Runtimes & Dependencies" catalog category:
    every runtime lives in an Essentials bundle below, so non-technical
    users just check the bundles instead of guessing versions.

Round-8 (2026-09-08, user request — "bring the Equalizer APO + Peace
download method to more apps"): three new verified-download installers
for apps winget does NOT carry (all verified live on 2026-09-08):
  * Equalizer APO + FluidEQ — second GUI choice for the APO engine,
    resolved 'latest' from the developer's electron-builder update feed
    (exact size + SHA-512 base64), self-healing on every release.
  * RustDesk — GitHub 'latest' release asset, SHA-256 verified against
    GitHub's own API digest (self-healing), WiX Burn silent install.
  * FreeFileSync — versioned URL resolved from the author's official
    page, Authenticode signature REQUIRED (Peace-style moving pin),
    Inno Setup silent install.
These render as embedded checkbox rows via EMBEDDED_TASKS_BY_CATEGORY.
"""

from app.utils import TaskContext, TaskCancelled, run_cmd, run_cmd_checked
from app.downloader import (
    has_network, install_winget_app, install_vc_redists, install_directx_runtimes,
    _require_admin_for_install, _norm_winget_rc, _WINGET_BENIGN,
)
from app import capabilities as cap
import os


def _ensure_winget(ctx: TaskContext) -> None:
    """winget gate for every installer below. On stripped Windows (LTSC)
    winget itself is missing — try the automatic LTSC bootstrapper first
    (user-provided engine); only if that fails, point at the Store task."""
    if cap.has_winget():
        return
    if ensure_winget_installed(ctx):
        cap.invalidate_caches()
        if cap.has_winget():
            return
    raise RuntimeError(
        "winget is not available on this PC. Install the Microsoft Store "
        "first (Install tab: 'Microsoft Store'), then try again."
    )


def ensure_winget_installed(ctx: TaskContext) -> bool:
    """LTSC WINGET BOOTSTRAPPER (user-provided engine): winget ships with
    the Store's App Installer, so clean LTSC has neither. This pulls the
    official winget CLI (latest winget-cli release .msixbundle from
    Microsoft's GitHub) plus its VCLibs / UI.Xaml dependencies and
    registers them with Add-AppxPackage — all Microsoft-official sources,
    per-user (no Store needed). Returns True when winget is usable.

    F-004a fix (supply-chain hardening): every downloaded artifact is now
    verified BEFORE Add-AppxPackage runs, matching downloader.py's stated
    'verify before execute' rule that this path previously skipped:
      * VCLibs + UI.Xaml are frozen, versioned artifacts -> pinned SHA-256
        (computed from the official files on 2026-09-04). A mismatch
        aborts (fail-closed) — if Microsoft ever retargets the aka.ms
        alias, the bootstrap fails honestly and the Store-install fallback
        path takes over instead of installing an unverified binary.
      * the msixbundle is a moving 'latest' (no stable hash to pin) -> its
        Authenticode signature must chain (Status 'Valid') AND the signer
        must be Microsoft Corporation. The resolved release tag is logged
        so a bad 'latest' is visible in the exported log.
      * the bundle asset is matched by its canonical name (a future second
        .msixbundle asset would make the old EndsWith filter return an
        array and break asset resolution)."""
    if cap.has_winget():
        return True
    if not has_network():
        ctx.log("  ! No internet — cannot bootstrap winget automatically.")
        return False
    ctx.log("winget not found in PATH (typical on fresh LTSC). Bootstrapping official winget binaries...")
    ctx.set_status("Bootstrapping winget + dependencies (a few minutes)...")
    ps_bootstrap = """
    $ErrorActionPreference = 'Stop'
    $ProgressPreference = 'SilentlyContinue'

    function Assert-Sha256([string]$Path, [string]$Expected, [string]$Label) {
        $actual = (Get-FileHash -Path $Path -Algorithm SHA256).Hash.ToLower()
        if ($actual -ne $Expected.ToLower()) {
            throw "SHA-256 MISMATCH for $Label - expected $Expected, got $actual. Refusing to install (download corrupt, or the source changed - use the 'Install Microsoft Store' task instead)."
        }
        Write-Output "  $Label hash verified ($($actual.Substring(0,16))...)"
    }

    $releasesUrl = "https://api.github.com/repos/microsoft/winget-cli/releases/latest"
    $release = Invoke-RestMethod -Uri $releasesUrl
    Write-Output ("Latest winget-cli release: " + $release.tag_name)

    $appInstallerAsset = @($release.assets | Where-Object { $_.name -eq "Microsoft.DesktopAppInstaller_8wekyb3d8bbwe.msixbundle" })
    if (-not $appInstallerAsset) { $appInstallerAsset = @($release.assets | Where-Object { $_.name.EndsWith(".msixbundle") }) }
    if (-not $appInstallerAsset) { throw "No msixbundle asset found in winget-cli release $($release.tag_name)." }
    $appInstallerUrl = $appInstallerAsset[0].browser_download_url

    $vclibsUrl = "https://aka.ms/Microsoft.VCLibs.x64.14.00.Desktop.appx"
    $uiXamlUrl = "https://github.com/microsoft/microsoft-ui-xaml/releases/download/v2.8.6/Microsoft.UI.Xaml.2.8.x64.appx"

    $tempDir = "$env:TEMP\\WingetBootstrap"
    New-Item -ItemType Directory -Force -Path $tempDir | Out-Null

    try {
        Invoke-WebRequest -Uri $vclibsUrl -OutFile "$tempDir\\VCLibs.appx"
        Invoke-WebRequest -Uri $uiXamlUrl -OutFile "$tempDir\\UIXaml.appx"
        Invoke-WebRequest -Uri $appInstallerUrl -OutFile "$tempDir\\AppInstaller.msixbundle"

        Assert-Sha256 "$tempDir\\VCLibs.appx" "b56a9101f706f9d95f815f5b7fa6efbac972e86573d378b96a07cff5540c5961" "VCLibs x64 14.00"
        Assert-Sha256 "$tempDir\\UIXaml.appx" "249d2afb41cc009494841372bd6dd2df46f87386d535ddf8d9f32c97226d2e46" "UI.Xaml 2.8.6 x64"

        $sig = Get-AuthenticodeSignature -FilePath "$tempDir\\AppInstaller.msixbundle"
        if ($sig.Status -ne 'Valid') { throw "winget msixbundle signature invalid ($($sig.Status)) - refusing to install." }
        if ($sig.SignerCertificate.Subject -notlike '*CN=Microsoft Corporation*') { throw "winget msixbundle signer is not Microsoft Corporation ($($sig.SignerCertificate.Subject)) - refusing to install." }
        Write-Output ("  msixbundle signature verified: " + $sig.SignerCertificate.Subject)

        Add-AppxPackage -Path "$tempDir\\VCLibs.appx"
        Add-AppxPackage -Path "$tempDir\\UIXaml.appx"
        Add-AppxPackage -Path "$tempDir\\AppInstaller.msixbundle"
    }
    finally {
        Remove-Item -Recurse -Force $tempDir -ErrorAction SilentlyContinue
    }
    """
    # run_cmd (not bare subprocess): streams progress to the hidden log
    # and registers the process so the Stop button can kill it.
    rc = run_cmd(ctx, ["powershell", "-NoProfile", "-Command", ps_bootstrap],
                 shell=False, timeout=900)
    cap.invalidate_caches()
    if rc == 0 and cap.has_winget():
        ctx.log("winget bootstrapped and verified — ready.")
        return True
    ctx.log(f"  ! winget bootstrap failed (exit {rc}) — install the Microsoft Store instead.")
    return False


def install_selected_apps(ctx: TaskContext, apps: list):
    """Runner for the Install tab's 'Install Selected Apps'. `apps` is a
    list of catalog dicts (id/name/url/foss). Sequential, non-blocking on
    individual failures, with a final summary report (Install.txt spec).

    audit fix: now RETURNS (ok_names, failed_names) so the GUI can count
    per-app outcomes honestly — the old caller did 'ok_n += len(apps)'
    whenever this function didn't raise, counting failed apps as installed.
    The all-failed raise is kept for the strict Essentials contract."""
    if not apps:
        raise RuntimeError("No apps selected.")
    ctx.set_status("Checking internet connection...")
    if not has_network():
        raise RuntimeError(
            "No internet connection detected — the Install tab needs "
            "internet to download apps. Connect and try again."
        )
    _ensure_winget(ctx)
    ctx.log(f"Installing {len(apps)} selected app(s)...")
    ok, failed = [], []
    for app in apps:
        if ctx.cancelled():
            ctx.log("Stopped — remaining apps were skipped.")
            break
        rc = install_winget_app(ctx, app["id"], app["name"], fallback_url=app.get("url", ""))
        # H6: a cancelled install (rc -1) must not be recorded as "failed" —
        # the run was stopped, and the caller reports Stopped vs Complete.
        if rc == -1 or ctx.cancelled():
            ctx.log(f"  [STOPPED] {app['name']} — cancelled by user; remaining apps skipped.")
            break
        (ok if rc == 0 else failed).append(app["name"])
    ctx.log("=" * 48)
    ctx.log(f"Install complete: {len(ok)} succeeded, {len(failed)} failed"
            + (f" ({', '.join(failed)})" if failed else "") + ".")
    if failed and not ok:
        raise RuntimeError("Every selected app failed to install — check the log.")
    return ok, failed


# --------------------------------------------------------------------------- #
# LTSC / missing-component installers (moved from Repair tab — all of these
# need internet, so they belong on Install per the user's request)
# --------------------------------------------------------------------------- #

# audit fix (M8): missing --exact risked a prefix match landing on the
# wrong package (winget picks the "best" match, not necessarily the exact
# id), and missing --disable-interactivity let an unexpected winget prompt
# hang the call for the full timeout (up to 900s) instead of failing fast.
# downloader.install_winget_app already carried both flags; this path
# (msstore installs) did not.
_WINGET_ARGS = ["--exact", "--accept-package-agreements", "--accept-source-agreements",
                "--silent", "--disable-interactivity"]


def _winget_install(ctx: TaskContext, package_id: str, label: str, timeout: int = 900) -> None:
    """Install an msstore package by winget ID. Logs to the hidden log;
    honest failure — the caller verifies presence afterwards.

    audit fix (regression of the documented CANCEL FIX): this used
    subprocess.run directly — no cancel registration (Stop button dead
    for Xbox/Game Bar/codec installs), no output streaming (everything
    appeared only after the install finished). Routed through utils.run_cmd
    now, exactly like downloader.install_winget_app and _winget_silent.

    audit fix (A2): winget's benign HRESULTs ('already installed',
    0x8A150039, the same set downloader.py classifies as success) used to
    raise here — so the common "component already present" state failed
    the whole bundle task with a misleading error. Normalize the exit
    code and treat the benign set as success, exactly like downloader.py."""
    cmd = ["winget", "install", "--id", package_id, "--source", "msstore"] + _WINGET_ARGS
    ctx.log(f"Installing {label} ({package_id})...")
    rc = run_cmd(ctx, cmd, shell=False, timeout=timeout)
    rc = _norm_winget_rc(rc)
    # H6: a stop is TaskCancelled ("stopped"), never a RuntimeError failure.
    if ctx.cancelled() or rc == -1:
        raise TaskCancelled(f"winget install of {label} cancelled.")
    if rc == 0:
        return
    if rc in _WINGET_BENIGN:
        ctx.log(f"  {label}: already installed (winget code {rc}) — treated as success.")
        return
    raise RuntimeError(f"winget install of {label} failed (exit {rc}).")


def install_microsoft_store(ctx: TaskContext):
    """Bring the Microsoft Store (and with it winget) to a stripped
    Windows install via `wsreset -i`. Takes 5-10 minutes on first run."""
    if not has_network():
        raise RuntimeError("No internet connection — the Store install needs to download.")
    if cap.has_store():
        ctx.log("Microsoft Store already installed — nothing to do.")
        return
    ctx.set_status("Installing Microsoft Store (can take 5-10 minutes)...")
    run_cmd_checked(ctx, "wsreset -i", timeout=900, success_codes=(0,))
    cap.invalidate_caches()
    if not cap.has_store():
        raise RuntimeError(
            "Store install did not complete (wsreset -i). It can need several "
            "minutes plus a reboot on slow connections — try again after rebooting."
        )
    ctx.log("Microsoft Store installed and verified.")


def install_xbox_stack(ctx: TaskContext):
    """Xbox app + Gaming Services + Identity Provider — what Game Pass
    and Xbox-network games (Minecraft cross-play, Forza, SoT) need. The
    Identity Provider is the sign-in piece LTSC strips: without it Game
    Pass logins fail even when the app and services are present."""
    if not has_network():
        raise RuntimeError("No internet connection — the Xbox stack needs to download.")
    _ensure_winget(ctx)
    attempted = []  # components THIS run actually tried to install
    if not cap.has_xbox_app():
        _winget_install(ctx, "9MV0B5HZVK9Z", "Xbox app")
        attempted.append("Xbox app")
    else:
        ctx.log("Xbox app already installed.")
    if not cap.has_gaming_services():
        # A2 fix: this used the Xbox app's Store ID (9MV0B5HZVK9Z), which
        # winget resolves to the "XBOX" app — verified via `winget show`:
        # 9MV0B5HZVK9Z -> "XBOX", 9MWPM2CQNLHN -> "Gaming Services"
        # (Microsoft Corporation). The old ID reinstalled the already-
        # present Xbox app and never installed Gaming Services.
        _winget_install(ctx, "9MWPM2CQNLHN", "Microsoft Gaming Services")
        attempted.append("Gaming Services")
    else:
        ctx.log("Gaming Services already installed.")
    if not cap.has_xbox_identity_provider():
        _winget_install(ctx, "9WZDNCRD1HKW", "Xbox Identity Provider")
        attempted.append("Xbox Identity Provider")
    else:
        ctx.log("Xbox Identity Provider already installed.")
    cap.invalidate_caches()
    if not attempted:
        return
    # A2 fix (per-component verification, install_video_codecs pattern): the
    # old check was an OR over the three components, so one arrival of three
    # logged "installed and verified". Verify each component this run
    # attempted individually; invalidate_caches() above ran so these checks
    # see the post-install state.
    missing = [label for label, check in (
        ("Xbox app", cap.has_xbox_app),
        ("Gaming Services", cap.has_gaming_services),
        ("Xbox Identity Provider", cap.has_xbox_identity_provider),
    ) if label in attempted and not check()]
    if missing:
        raise RuntimeError(
            f"Xbox stack install did not verify — still missing: {', '.join(missing)}. "
            "Try rebooting and running again.")
    ctx.log(f"Installed and verified: {', '.join(attempted)}.")


def install_game_bar(ctx: TaskContext):
    """Xbox Game Bar — the Win+G overlay (clips, screenshots, perf stats)."""
    if not has_network():
        raise RuntimeError("No internet connection — Game Bar needs to download.")
    if cap.has_game_bar():
        ctx.log("Game Bar already installed — nothing to do.")
        return
    _ensure_winget(ctx)
    _winget_install(ctx, "9NZKPSTSNW4P", "Xbox Game Bar")
    cap.invalidate_caches()
    if not cap.has_game_bar():
        raise RuntimeError("Game Bar install did not verify — try rebooting and running again.")
    ctx.log("Game Bar installed and verified.")


def install_video_codecs(ctx: TaskContext):
    """AV1 + VP9 video extensions plus the free Web Media pack — the
    missing codecs behind frozen/black in-game cutscenes on stripped
    Windows. (HEVC's official extension is paid; Web Media Extensions is
    the free OEM pack covering the same media path — no paid installs.)"""
    if not has_network():
        raise RuntimeError("No internet connection — codecs need to download.")
    _ensure_winget(ctx)
    did = []
    if not cap.has_av1_codec():
        _winget_install(ctx, "9MVZQVXJBQ9V", "AV1 Video Extension")
        did.append("AV1")
    if not cap.has_vp9_codec():
        _winget_install(ctx, "9N4D0MSMP0PT", "VP9 Video Extensions")
        did.append("VP9")
    if not cap.has_web_media_extension():
        _winget_install(ctx, "9N5TDP8VCMHS", "Web Media Extensions")
        did.append("Web Media")
    cap.invalidate_caches()
    if not did:
        ctx.log("All video codecs already installed — nothing to do.")
        return
    # audit fix (probe-confirmed bypass): the old boolean used
    # ('AV1' in did) or has_av1_codec() — a codec WE attempted to install
    # was never re-verified, so a failed install still reported success.
    # invalidate_caches() above ran specifically so these checks see the
    # post-install state; verify every codec unconditionally.
    ok = cap.has_av1_codec() and cap.has_vp9_codec() and cap.has_web_media_extension()
    if not ok:
        raise RuntimeError("One or more codec installs did not verify — export the log for details.")
    ctx.log(f"Installed and verified: {', '.join(did)}.")


def install_legacy_runtimes(ctx: TaskContext):
    """Enable the optional Windows features DirectPlay and .NET Framework
    3.5 — needed by many older/retro games. (Not internet-dependent in the
    download sense, but grouped here as a 'missing component' installer.)"""
    ctx.set_status("Enabling DirectPlay and .NET 3.5 (DISM)...")
    rc1 = run_cmd(ctx, "dism /online /enable-feature /featurename:DirectPlay /all /norestart", timeout=900)
    rc2 = run_cmd(ctx, "dism /online /enable-feature /featurename:NetFx3 /all /norestart", timeout=1800)
    if rc1 not in (0, 3010) or rc2 not in (0, 3010):
        raise RuntimeError(
            "DISM could not enable DirectPlay/.NET 3.5 — LTSC may need the "
            "install media's source files (run from an elevated prompt to see details)."
        )
    ctx.log("DirectPlay and .NET 3.5 enabled (a reboot finishes installing them).")


def task_install_vc_redists(ctx: TaskContext):
    """All 12 official VC++ Redistributables (2005-2022) — thin wrapper so
    the Task list can point at the downloader implementation."""
    install_vc_redists(ctx)


def install_vcredist_allinone(ctx: TaskContext):
    """MICROSOFT VISUAL C++ ALL-IN-ONE BUNDLE (user request 2026-09-06) —
    the single winget package `Microsoft.VCRedist.2015+.x64` (Microsoft's
    own "Visual C++ v14 Redistributable (x64)", verified live 2026-09-06,
    v14.51.36247.0) — the latest VC++ 2015-2022 runtime every modern game
    and app needs, in one click.

    Deliberately SEPARATE from the "ALL VC++ Runtimes (2005-2022)" bundle:
    that one installs all 12 packages (both arches, every year) for full
    retro coverage with admin rights; this one is the quick single-package
    install for users who just want the one runtime modern software asks
    for. No admin gate — the winget MSI elevation is handled by winget."""
    if not has_network():
        raise RuntimeError("No internet connection — VC++ All-in-One needs to download.")
    _ensure_winget(ctx)
    rc = install_winget_app(ctx, "Microsoft.VCRedist.2015+.x64", "Microsoft Visual C++ All-in-One (x64)",
                             fallback_url="https://learn.microsoft.com/en-us/cpp/windows/latest-supported-vc-redist")
    # H6: a cancelled install (rc -1) must not be recorded as "failed" —
    # the run was stopped, and the caller reports Stopped vs Complete.
    if rc == -1 or ctx.cancelled():
        raise TaskCancelled("VC++ All-in-One install cancelled by user.")
    if rc != 0:
        raise RuntimeError("VC++ All-in-One install failed — see the log.")
    ctx.log("VC++ All-in-One (2015-2022 x64) installed.")


def install_directx_runtimes_winget(ctx: TaskContext):
    """DIRECTX END-USER RUNTIMES BUNDLE (user request 2026-09-06) — the
    single winget package `Microsoft.DirectX`: Microsoft's own winget
    package carrying the legacy DirectX SDK side-by-side libraries
    (d3dx9/d3dx10/d3dx11, XAudio 2.7, XInput 1.3, XACT, Managed DX 1.1 —
    verified live 2026-09-06, v9.29.1974.0). Fixes the classic
    missing-DLL game errors (d3dx9_43.dll and friends) in one click.

    Deliberately SEPARATE from the "ALL DirectX Runtimes" bundle: that one
    downloads and runs the June 2010 web-installer EXE (admin required);
    this one is the lightweight winget MSIX path — no admin gate, silent,
    self-updating via winget source."""
    if not has_network():
        raise RuntimeError("No internet connection — DirectX runtimes need to download.")
    _ensure_winget(ctx)
    rc = install_winget_app(ctx, "Microsoft.DirectX", "DirectX End-User Runtimes",
                             fallback_url="https://www.microsoft.com/en-us/download/details.aspx?id=8109")
    if rc == -1 or ctx.cancelled():
        raise TaskCancelled("DirectX runtimes install cancelled by user.")
    if rc != 0:
        raise RuntimeError("DirectX runtimes install failed — see the log.")
    ctx.log("DirectX End-User Runtimes (legacy d3dx9/d3dx10/d3dx11/XAudio/XInput) installed.")


def install_java_bundle(ctx: TaskContext):
    """JAVA BUNDLE (user request: one click, no version guessing) —
    installs everything Java a game can need, silently, latest versions:
      * Temurin 17 JRE  — modern Minecraft/mods
      * Temurin 17 JDK  — mod development / gradle builds
      * Temurin 8 JRE   — legacy Minecraft (1.12 and older) & old Java games
    Already-installed parts are skipped by winget's own detection."""
    if not has_network():
        raise RuntimeError("No internet connection — Java needs to download.")
    _ensure_winget(ctx)
    parts = [
        ("EclipseAdoptium.Temurin.17.JRE", "Java 17 JRE (modern Minecraft)"),
        ("EclipseAdoptium.Temurin.17.JDK", "Java 17 JDK (mod development)"),
        ("EclipseAdoptium.Temurin.8.JRE", "Java 8 JRE (legacy Minecraft / old games)"),
    ]
    ctx.log("Installing the full Java suite (17 JRE + 17 JDK + 8 JRE)...")
    failed = []
    for pid, label in parts:
        # H6: a cancelled install (rc -1) is "stopped", not "failed" —
        # report it as TaskCancelled and stop the batch immediately.
        if ctx.cancelled():
            raise TaskCancelled("Java install cancelled by user.")
        rc = install_winget_app(ctx, pid, label, fallback_url="https://adoptium.net/")
        if rc == -1 or ctx.cancelled():
            raise TaskCancelled("Java install cancelled by user.")
        if rc != 0:
            failed.append(label)
    if failed:
        raise RuntimeError(f"Java suite partially failed: {', '.join(failed)} — see the log.")
    ctx.log("Java suite complete — every Minecraft era and Java game is covered.")


def install_dotnet_bundle(ctx: TaskContext):
    """.NET BUNDLE (user request) — every .NET Desktop Runtime a game or
    tool can ask for, silently, latest versions:
      * .NET 8  — current mainstream
      * .NET 6  — older games/tools still pinned to it
      * .NET 3.5 + DirectPlay via DISM — legacy/retro (part of this bundle
        so 'everything old' is one click; needs admin for the DISM part)
    """
    if not has_network():
        raise RuntimeError("No internet connection — .NET needs to download.")
    _ensure_winget(ctx)
    ctx.log("Installing .NET Desktop Runtimes 8 and 6...")
    failed = []
    for pid, label in (
        ("Microsoft.DotNet.DesktopRuntime.8", ".NET Desktop Runtime 8"),
        ("Microsoft.DotNet.DesktopRuntime.6", ".NET Desktop Runtime 6 (older games)"),
    ):
        # H6: cancelled (rc -1) is "stopped", not "failed".
        if ctx.cancelled():
            raise TaskCancelled(".NET install cancelled by user.")
        rc = install_winget_app(ctx, pid, label, fallback_url="https://dotnet.microsoft.com/download/dotnet")
        if rc == -1 or ctx.cancelled():
            raise TaskCancelled(".NET install cancelled by user.")
        if rc != 0:
            failed.append(label)
    # legacy DISM features (best-effort — needs admin; honest skip if not)
    import ctypes
    try:
        if ctypes.windll.shell32.IsUserAnAdmin():
            install_legacy_runtimes(ctx)
        else:
            ctx.log("  (.NET 3.5 / DirectPlay skipped — needs admin; run the app as Administrator for the full bundle)")
    except AttributeError:
        pass
    if failed:
        raise RuntimeError(f".NET bundle partially failed: {', '.join(failed)} — see the log.")
    ctx.log(".NET bundle complete — modern and legacy runtimes installed.")


def install_directx_bundle(ctx: TaskContext):
    """DIRECTX BUNDLE (user request) — the legacy DirectX stack games can
    ask for, in one click:
      * DirectX End-User Runtimes (June 2010) — d3dx9/d3dx10/d3dx11, XAudio
        2.7, XInput 1.3 side-by-side libraries (the missing-DLL fix),
        downloaded from Microsoft's CDN, SHA-256 verified, silent.
      * Microsoft.DirectX (winget) — Microsoft's own winget package for
        the same legacy DirectX SDK side-by-side libraries (added per user
        request 2026-09-06; verified FOUND v9.29.1974.0, the June 2010
        redist republished as a signed MSIX). Run AFTER the web-installer
        path so the DLLs land even when the CDN scrape fails — winget's
        offline MSIX install needs no elevation dance.
    Note: DirectX 12 itself is built into Windows 10/11 and updated via
    Windows Update — there is no separate installer to run. The AV1/VP9/
    Web Media codec packs live in their own 'Install Windows Codecs'
    bundle now (user request: codecs bundled as windows codecs)."""
    install_directx_runtimes(ctx)      # legacy d3dx9/d3dx10/d3dx11/XAudio/XInput
    if ctx.cancelled():
        raise TaskCancelled("DirectX install cancelled by user.")
    # Winget path for the same legacy runtimes (self-healing second chance)
    _ensure_winget(ctx)
    rc = install_winget_app(ctx, "Microsoft.DirectX", "DirectX End-User Runtimes",
                             fallback_url="https://www.microsoft.com/en-us/download/details.aspx?id=8109")
    if rc == -1 or ctx.cancelled():
        raise TaskCancelled("DirectX install cancelled by user.")
    if rc != 0:
        ctx.log("  ! Microsoft.DirectX winget install failed — the CDN install above may still have landed.")


def install_codecs_bundle(ctx: TaskContext):
    """WINDOWS CODECS BUNDLE (user request: 'bundled as windows codecs') —
    AV1 + VP9 + Web Media Extensions in one click; the media components
    LTSC strips that break/black in-game cutscenes."""
    install_video_codecs(ctx)          # skips present ones, verifies the rest


def install_classic_runtimes(ctx: TaskContext):
    """CLASSIC GAME RUNTIMES BUNDLE — OpenAL 3D audio + Microsoft XNA
    Framework 4.0 + legacy PhysX in one click (user request: bundle what
    goes together). Required by S.T.A.L.K.E.R., Amnesia and classic 3D
    games (OpenAL), original Terraria, Fez and Bastion (XNA), and
    Mirror's Edge, Batman AA and Borderlands 2 (PhysX). Silently installs
    the latest winget versions; already-installed parts are skipped by
    winget's own detection."""
    if not has_network():
        raise RuntimeError("No internet connection — classic runtimes need to download.")
    _ensure_winget(ctx)
    parts = [
        ("CreativeTechnology.OpenAL", "OpenAL 3D Audio API", "https://www.openal.org/"),
        ("Microsoft.XNARedist", "XNA Framework 4.0",
         "https://www.microsoft.com/download/details.aspx?id=20914"),
        ("Nvidia.PhysX", "Legacy PhysX System Software", "https://www.nvidia.com/Download/index.aspx"),
    ]
    ctx.log("Installing classic game runtimes (OpenAL + XNA + PhysX)...")
    failed = []
    for pid, label, url in parts:
        # H6: cancelled (rc -1) is "stopped", not "failed".
        if ctx.cancelled():
            raise TaskCancelled("Classic runtimes install cancelled by user.")
        rc = install_winget_app(ctx, pid, label, fallback_url=url)
        if rc == -1 or ctx.cancelled():
            raise TaskCancelled("Classic runtimes install cancelled by user.")
        if rc != 0:
            failed.append(label)
    if failed:
        raise RuntimeError(f"Classic runtimes partially failed: {', '.join(failed)} — see the log.")
    ctx.log("Classic runtimes complete — OpenAL, XNA and PhysX installed.")


# Equalizer APO + Peace GUI, bundled as ONE task (user request): the engine
# (APO) has no winget package, so this downloads both official installers
# from SourceForge and runs them silently, APO first (Peace needs it).
#
# F-004b fix (supply-chain hardening): these are the app's ONLY downloads
# that get executed with no OS signature gate (Add-AppxPackage validates
# signatures for the winget bootstrap; these are plain unsigned-in-transit
# EXEs from SourceForge run ELEVATED), so they now follow downloader.py's
# 'verify before execute' rule with pinned integrity values. Every pin
# below is corroborated by at least two independent first-party sources
# (verified 2026-09-04):
#   * EqualizerAPO-x64-1.4.2.exe: SHA-256 pinned from the Chocolatey
#     community package's moderated manifest (which pins the EXACT same
#     SourceForge URL we download), cross-checked against SourceForge's
#     own RSS metadata (size 11980366, MD5 410aab97...). Author's frozen
#     1.4.2 release (published 2025-03-21).
#   * PeaceSetup.exe: versionless URL serving a moving file, so a SHA-256
#     pin would break on every Peace update. Instead: MD5 + exact size
#     from SourceForge's own RSS feed, PLUS the installer must carry a
#     VALID Authenticode signature (the author documents all Peace
#     executables are code-signed via Certum since 1.5.0.1, and documents
#     this exact byte size as his tamper check). A mismatch aborts.
# If a future Peace release changes the file, the pin updates with a
# normal app release — deliberate fail-closed per the audit contract.
_APO_PARTS = [
    {"label": "Equalizer APO 1.4.2 (system-wide EQ engine)",
     "url": "https://sourceforge.net/projects/equalizerapo/files/1.4.2/EqualizerAPO-x64-1.4.2.exe/download",
     "silent": "/S", "min_bytes": 5_000_000,
     "sha256": "7403be7427bbe1936a40dded082829b6e217fc4f5990fee5cba501f0ae055afa",
     "exact_size": 11_980_366,
     "manual": "https://sourceforge.net/projects/equalizerapo/files/"},
    {"label": "Peace GUI 1.6.9.11 (equalizer interface)",
     "url": "https://sourceforge.net/projects/peace-equalizer-apo-extension/files/PeaceSetup.exe/download",
     "silent": "/SILENT", "min_bytes": 20_000_000,
     "md5": "4536c170b52023028723fd1c26da223c",
     "exact_size": 55_657_784,
     "require_signature": True,
     "manual": "https://sourceforge.net/projects/peace-equalizer-apo-extension/files/"},
]


def _is_hex(s: str) -> bool:
    """True when s is a plain hexadecimal digest (vs electron-builder's
    base64 sha512 form)."""
    try:
        int(s, 16)
        return True
    except ValueError:
        return False


def _verify_download_integrity(ctx: TaskContext, dest: str, label: str, part: dict) -> None:
    """Pin-based integrity gate for the APO/Peace installers (F-004b).

    Verifies whatever pins the part defines: exact size, SHA-256, MD5, and
    (optionally) a valid Authenticode signature. Any mismatch removes the
    file and raises — the caller must NOT execute it. Keeps the existing
    min_bytes + MZ-magic checks as cheap first-pass gates."""
    import hashlib
    # exact size pin
    expected_size = part.get("exact_size")
    if expected_size is not None:
        actual_size = os.path.getsize(dest)
        if actual_size != expected_size:
            raise RuntimeError(
                f"{label} size mismatch: got {actual_size:,} bytes, pinned "
                f"{expected_size:,} — the download is not the verified file "
                f"(corrupt transfer, or SourceForge changed/redirected it). "
                f"Refusing to run it.")
    # hash pins (streamed, so a 200MB file is not loaded whole). sha512
    # pins are electron-builder BASE64 (the format the update feed itself
    # publishes) — decoded to raw bytes and compared as hex.
    for algo, key in (("sha256", "sha256"), ("md5", "md5"), ("sha512", "sha512")):
        expected = part.get(key)
        if expected is None:
            continue
        h = getattr(hashlib, algo)()
        with open(dest, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                h.update(chunk)
        actual = h.hexdigest()
        if algo == "sha512" and not _is_hex(expected):
            # base64 form (electron-builder latest.yml): compare b64-to-b64
            import base64 as _b64
            actual = _b64.b64encode(h.digest()).decode("ascii")
        if actual.lower() != expected.lower():
            raise RuntimeError(
                f"{label} {algo.upper()} MISMATCH — expected {expected[:16]}..., "
                f"got {actual[:16]}... Refusing to run the file (download "
                f"corrupt, or the source changed — use the manual link).")
        ctx.log(f"  {label}: {algo.upper()} verified ({actual[:16]}...).")
    # signature gate (Peace: author code-signs every release via Certum)
    if part.get("require_signature"):
        import subprocess as _sp
        try:
            out = _sp.run(
                ["powershell", "-NoProfile", "-Command",
                 f"(Get-AuthenticodeSignature -FilePath '{dest}').Status"],
                capture_output=True, text=True, timeout=30,
                creationflags=getattr(_sp, "CREATE_NO_WINDOW", 0),
            )
            status = (out.stdout or "").strip()
        except Exception as exc:
            raise RuntimeError(f"{label}: could not check Authenticode signature: {exc}")
        if status != "Valid":
            raise RuntimeError(
                f"{label} signature check failed (status: {status or 'unknown'}) — "
                f"the author signs every official release, so an invalid "
                f"signature means the file is not the official build. "
                f"Refusing to run it. Manual download: {part.get('manual', 'see project page')}")
        ctx.log(f"  {label}: Authenticode signature valid.")


def _download_binary(ctx: TaskContext, url: str, dest: str, label: str, min_bytes: int,
                     part: "dict | None" = None) -> None:
    """Download url -> dest with a browser UA (SourceForge file mirrors
    reject bare script clients). Verifies before anyone may execute it:
    must exist, meet min_bytes, serve a binary content-type, and start with
    the MZ executable magic — an HTML challenge/error page fails loudly
    instead of ever running. With `part` (F-004b), the pinned-integrity
    gate (_verify_download_integrity) also runs: exact size + hash pins +
    optional Authenticode requirement, fail-closed before execution."""
    import urllib.request as _urllib
    ctx.log(f"Downloading {label}...")
    req = _urllib.Request(url, headers={
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                      "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36",
    })
    try:
        with _urllib.urlopen(req, timeout=600) as resp, open(dest, "wb") as f:
            ctype = (resp.headers.get("Content-Type") or "").lower()
            if "text/html" in ctype:
                raise RuntimeError(f"server returned an HTML page, not a file (blocked/challenge?)")
            while True:
                if ctx.cancelled():
                    # F-7: a user Stop is 'stopped', not a download failure —
                    # raise TaskCancelled (downloader._download's H6 contract)
                    # so install_selected_mixed reports Stopped instead of a
                    # failed task with a misleading download-error message.
                    raise TaskCancelled("download cancelled by user")
                chunk = resp.read(1 << 16)
                if not chunk:
                    break
                f.write(chunk)
    except TaskCancelled:
        # same partial-file cleanup as failures, but keep the 'stopped'
        # classification — do not wrap it into a RuntimeError
        try:
            if os.path.exists(dest):
                os.remove(dest)
        except OSError:
            pass
        raise
    except Exception as exc:
        try:
            if os.path.exists(dest):
                os.remove(dest)
        except OSError:
            pass
        raise RuntimeError(f"download failed for {label}: {exc}")
    try:
        size = os.path.getsize(dest)
        with open(dest, "rb") as f:
            magic = f.read(2)
    except OSError as exc:
        raise RuntimeError(f"could not read downloaded {label}: {exc}")
    # first-pass magic gate: EXE = MZ, MSI = the OLE compound-file signature
    # (verified: rustdesk's .msi starts D0 CF 11 E0 — an HTML challenge
    # page served instead fails loudly here, never at execution)
    expected_magic = part.get("magic") if part else None
    if expected_magic is None:
        expected_magic = b"MZ"
    if size < min_bytes or magic != expected_magic[:2]:
        try:
            os.remove(dest)
        except OSError:
            pass
        raise RuntimeError(
            f"{label} failed verification (size {size}, magic {magic!r}) — "
            f"not a real installer, refusing to run it.")
    # F-004b: pinned integrity gate — raises (and the caller's finally
    # removes the partial file) on any mismatch, BEFORE execution.
    if part is not None:
        _verify_download_integrity(ctx, dest, label, part)


def _apo_engine_present() -> bool:
    """True when the Equalizer APO engine is already installed (either
    bundle's APO part). APO 1.4.x installs to 'EqualizerAPO' under Program
    Files and registers an 'Equalizer APO' uninstall entry, so either
    sentinel proves presence. Used so checking BOTH bundle rows (or re-
    running a bundle) skips the second APO install instead of running its
    installer twice (its exit codes already tolerate 1638, but an honest
    'already installed — skipping' log line is better than a 3-minute
    silent installer rerun)."""
    if not cap.IS_WINDOWS:
        return False
    for env_var, sub in (("ProgramFiles", "EqualizerAPO"), ("ProgramFiles(x86)", "EqualizerAPO")):
        base = os.environ.get(env_var)
        if base and os.path.isdir(os.path.join(base, sub)):
            return True
    # registry sentinel: the APO uninstall key (name matches regardless of
    # version; quiet query, no output)
    import subprocess as _sp
    try:
        out = _sp.run(
            ["reg", "query", r"HKLM\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall",
             "/f", "Equalizer APO", "/reg:64"],
            capture_output=True, text=True, timeout=15,
            creationflags=getattr(_sp, "CREATE_NO_WINDOW", 0),
        )
        if "Equalizer APO" in (out.stdout or ""):
            return True
    except Exception:
        pass
    return False


def install_apo_peace_bundle(ctx: TaskContext):
    """APO + PEACE BUNDLE: system-wide parametric EQ (hear footsteps) with
    the Peace graphical interface, in one click. Admin required (audio
    driver install). Honest notes: a reboot finishes the APO driver; Peace
    may trip antivirus heuristics (documented false positive by its author);
    on first run, pick your output device in Peace once."""
    _require_admin_for_install("Equalizer APO + Peace")
    if not has_network():
        raise RuntimeError("No internet connection — APO + Peace need to download.")
    import tempfile as _tf
    ctx.log("Installing Equalizer APO + Peace (audio EQ stack)...")
    _ran_apo = False
    for part in _APO_PARTS:
        if part is _APO_PARTS[0] and _apo_engine_present():
            ctx.log("  Equalizer APO engine already installed — skipping (each bundle "
                    "installs it once; the GUI part below still installs).")
            continue
        fd, dest = _tf.mkstemp(prefix="cleaner_apo_", suffix=".exe")
        os.close(fd)
        try:
            try:
                _download_binary(ctx, part["url"], dest, part["label"], part["min_bytes"],
                                part=part)  # F-004b: pinned integrity gate
            except TaskCancelled:
                # F-7: keep the 'stopped' classification — no manual-download
                # failure text for a user Stop; the runner reports Stopped.
                raise
            except RuntimeError as exc:
                raise RuntimeError(f"{exc} Manual download: {part['manual']}")
            ctx.log(f"Running silent install: {part['label']}...")
            rc = run_cmd(ctx, f'"{dest}" {part["silent"]}', shell=True, timeout=900)
            if rc not in (0, 3010, 1638):  # 3010=reboot-needed success, 1638=already installed
                raise RuntimeError(
                    f"{part['label']} installer exited with code {rc}. "
                    f"Manual download: {part['manual']}")
            _ran_apo = _ran_apo or part is _APO_PARTS[0]
            ctx.log(f"  [OK] {part['label']} installed.")
        finally:
            try:
                if os.path.exists(dest):
                    os.remove(dest)
            except OSError:
                pass
        if ctx.cancelled():
            ctx.log("Stopped — remaining parts were skipped.")
            return
    ctx.log("APO + Peace complete — reboot to finish the audio driver, then open Peace and pick your output device.")


# --------------------------------------------------------------------------- #
# Latest-version resolvers (round-8, user request: bring the APO+Peace
# one-click download method to more apps that have NO winget package).
#
# F-004b supply-chain contract per source type:
#   * MOVING 'latest' with a machine-readable feed (FluidEQ's electron-
#     builder latest.yml, RustDesk's GitHub API digest) -> download the
#     hash FROM THE DEVELOPER'S OWN FEED at run time and verify against
#     it (self-healing: a new release updates the pin automatically —
#     NO app release needed, nothing to go stale).
#   * Static versioned URL, no published hashes (FreeFileSync) -> the
#     Authenticode signature MUST be valid (author code-signs every
#     release), exactly like the Peace pin design.
#   * Every download still passes the MZ magic + min_bytes + (MSI magic
#     for .msi) first-pass gates, and runs only via the shared
#     _download_binary / _verify_download_integrity fail-closed path.
# --------------------------------------------------------------------------- #

_ELECTRON_YML_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) " \
                   "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"


def _resolve_github_latest_asset(ctx: TaskContext, repo: str, asset_pattern: str,
                                 label: str) -> "tuple[str, str, int] | None":
    """Resolve the CURRENT latest-release asset on GitHub for `repo`
    matching `asset_pattern` (case-insensitive substring). Returns
    (download_url, sha256_hex, size_bytes) from GitHub's own API — the
    digest is computed and published by GitHub, so verifying against it is
    a first-party gate that self-heals on every release (verified live:
    rustdesk 1.4.9 ships sha256 digests per asset). None on failure, with
    honest log lines (caller decides whether to raise or fall back)."""
    import json as _json
    import urllib.request as _urllib
    url = f"https://api.github.com/repos/{repo}/releases/latest"
    ctx.log(f"  resolving latest {label} from {repo} (GitHub API)...")
    req = _urllib.Request(url, headers={"User-Agent": _ELECTRON_YML_UA,
                                        "Accept": "application/vnd.github+json"})
    try:
        with _urllib.urlopen(req, timeout=30) as resp:
            data = _json.loads(resp.read().decode("utf-8", "replace"))
    except Exception as exc:
        ctx.log(f"  ! GitHub API resolve failed for {repo}: {exc}")
        return None
    tag = data.get("tag_name", "?")
    for asset in data.get("assets", []):
        name = (asset.get("name") or "")
        if asset_pattern.lower() in name.lower():
            digest = (asset.get("digest") or "")
            if digest.startswith("sha256:"):
                sha = digest[len("sha256:"):].lower()
                size = int(asset.get("size") or 0)
                url = asset.get("browser_download_url") or ""
                if url and sha and size:
                    ctx.log(f"  latest {label}: {tag} ({name}, {size:,} bytes, "
                            f"sha256 {sha[:12]}...)")
                    return url, sha, size
    ctx.log(f"  ! no asset matching '{asset_pattern}' (with a sha256 digest) "
            f"in {repo} latest release {tag}.")
    return None


def _resolve_electron_latest_yml(ctx: TaskContext, yml_url: str, label: str,
                                 min_bytes: int = 5_000_000) -> "tuple[str, str, int, str] | None":
    """Resolve the CURRENT release from an electron-builder `latest.yml`
    update feed (a stable, versionless URL the developer publishes). The
    feed carries the installer filename, exact size and SHA-512 (base64)
    — computed by the developer's own build pipeline, so verifying
    against it is a first-party gate that self-heals on every release.
    Returns (installer_url, sha512_b64, exact_size, version) or None.
    (Verified live: StartSWest/FluidEQ serves this feed — v1.6.4,
    FluidEQ-Setup-1.6.4.exe, 206,667,756 bytes, sha512 base64 matching
    both the fluideq.com download page and the GitHub release digest.)"""
    import base64 as _b64
    import re as _re
    import urllib.request as _urllib
    ctx.log(f"  resolving latest {label} from its update feed...")
    req = _urllib.Request(yml_url, headers={"User-Agent": _ELECTRON_YML_UA})
    try:
        with _urllib.urlopen(req, timeout=30) as resp:
            yml = resp.read().decode("utf-8", "replace")
    except Exception as exc:
        ctx.log(f"  ! update feed resolve failed ({yml_url}): {exc}")
        return None
    version = ""
    m = _re.search(r"^version:\s*(\S+)", yml, _re.MULTILINE)
    if m:
        version = m.group(1)
    # the fields live under a YAML list item ('  - url: …'), so the dash
    # is optional whitespace-adjacent prefix on each line
    url_m = _re.search(r"^\s*(?:-\s+)?url:\s*(\S+)", yml, _re.MULTILINE)
    sha_m = _re.search(r"^\s*(?:-\s+)?sha512:\s*(\S+)", yml, _re.MULTILINE)
    size_m = _re.search(r"^\s*(?:-\s+)?size:\s*(\d+)", yml, _re.MULTILINE)
    if not (url_m and sha_m and size_m):
        ctx.log("  ! update feed did not carry url/sha512/size — refusing to guess.")
        return None
    fname, sha512, size = url_m.group(1), sha_m.group(1), int(size_m.group(1))
    try:
        digest_bytes = _b64.b64decode(sha512, validate=True)
    except Exception:
        ctx.log("  ! update feed sha512 is not valid base64 — refusing.")
        return None
    if size < min_bytes or not digest_bytes:
        ctx.log("  ! update feed values failed sanity checks — refusing.")
        return None
    # the feed's relative filename resolves against the same directory as
    # the yml itself (electron-builder convention: .../releases/latest/download/)
    installer_url = yml_url.rsplit("/", 1)[0] + "/" + fname
    ctx.log(f"  latest {label}: v{version} ({fname}, {size:,} bytes, sha512 feed-pinned).")
    return installer_url, sha512, size, version


def _run_verified_installer(ctx: TaskContext, label: str, dest: str, silent: str,
                            manual: str, ok_codes: "tuple[int, ...]" = (0, 3010, 1638)) -> None:
    """Silently execute a just-downloaded installer with the same honest
    contract as the APO+Peace bundle: cancel-tolerant run via run_cmd,
    exit-code gate (0 / 3010 reboot-needed / 1638 already-installed),
    temp file always removed. Raises RuntimeError pointing at the manual
    link on failure."""
    ctx.log(f"Running silent install: {label}...")
    rc = run_cmd(ctx, f'"{dest}" {silent}', shell=True, timeout=1200)
    if rc not in ok_codes:
        raise RuntimeError(f"{label} installer exited with code {rc}. "
                           f"Manual download: {manual}")
    ctx.log(f"  [OK] {label} installed.")


def install_apo_fluideq_bundle(ctx: TaskContext):
    """APO + FLUIDEQ BUNDLE (user request: pick between the two Equalizer
    APO interfaces): Equalizer APO (the engine) + FluidEQ, the modern
    GUI (128-band EQ, per-output profiles, headphone correction library,
    convolution, karaoke). Same shape as the Peace bundle: admin required,
    APO first (skipped when already present), GUI second — FluidEQ's own
    docs confirm it leaves an existing APO completely alone.

    Integrity (F-004b): FluidEQ's installer is UNSIGNED, so there is no
    Authenticode gate — instead the exact file is pinned by the developer's
    OWN electron-builder update feed (latest.yml: exact size + SHA-512
    base64), resolved fresh at run time, cross-published on fluideq.com's
    download page and the GitHub release digest (verified matching on
    2026-09-08). A mismatch aborts before execution. New FluidEQ releases
    heal automatically — the feed moves, nothing in this app is pinned.

    Honest notes: the 197MB download dwarfs Peace's 55MB; Windows
    SmartScreen warns on manual runs of the unsigned build (the app's
    silent path bypasses that dialog); FluidEQ also carries APO itself
    and offers to install it on first run, but installing our verified
    APO first keeps both bundles identical in shape. FOSS (GPL-3.0+)."""
    _require_admin_for_install("Equalizer APO + FluidEQ")
    if not has_network():
        raise RuntimeError("No internet connection — APO + FluidEQ need to download.")
    import tempfile as _tf
    ctx.log("Installing Equalizer APO + FluidEQ (audio EQ stack)...")
    # 1) engine first — once per run/machine (same skip as the Peace bundle)
    apo = _APO_PARTS[0]
    if _apo_engine_present():
        ctx.log("  Equalizer APO engine already installed — skipping (FluidEQ needs it, it is here).")
    else:
        fd, dest = _tf.mkstemp(prefix="cleaner_apo_", suffix=".exe")
        os.close(fd)
        try:
            try:
                _download_binary(ctx, apo["url"], dest, apo["label"], apo["min_bytes"], part=apo)
            except TaskCancelled:
                raise
            except RuntimeError as exc:
                raise RuntimeError(f"{exc} Manual download: {apo['manual']}")
            _run_verified_installer(ctx, apo["label"], dest, apo["silent"], apo["manual"])
        finally:
            try:
                if os.path.exists(dest):
                    os.remove(dest)
            except OSError:
                pass
        if ctx.cancelled():
            ctx.log("Stopped — FluidEQ was skipped.")
            return
    # 2) FluidEQ GUI, resolved 'latest' from the developer's own feed
    resolved = _resolve_electron_latest_yml(
        ctx, "https://github.com/StartSWest/FluidEQ/releases/latest/download/latest.yml",
        "FluidEQ")
    if resolved is None:
        raise RuntimeError("Could not resolve the current FluidEQ release from its "
                           "official update feed. Manual download: "
                           "https://fluideq.com/")
    installer_url, sha512, exact_size, _version = resolved
    part = {"label": "FluidEQ (equalizer interface)", "url": installer_url,
            "min_bytes": 100_000_000, "sha512": sha512, "exact_size": exact_size,
            "manual": "https://fluideq.com/"}
    fd, dest = _tf.mkstemp(prefix="cleaner_fluideq_", suffix=".exe")
    os.close(fd)
    try:
        try:
            _download_binary(ctx, part["url"], dest, part["label"], part["min_bytes"], part=part)
        except TaskCancelled:
            raise
        except RuntimeError as exc:
            raise RuntimeError(f"{exc} Manual download: {part['manual']}")
        # electron-builder NSIS: /S is the documented silent flag
        _run_verified_installer(ctx, part["label"], dest, "/S", part["manual"])
    finally:
        try:
            if os.path.exists(dest):
                os.remove(dest)
        except OSError:
            pass
    ctx.log("APO + FluidEQ complete — reboot to finish the audio driver, then open "
            "FluidEQ and pick your output device.")


def install_rustdesk(ctx: TaskContext):
    """RUSTDESK (round-8, user request: the APO+Peace download method for
    apps winget does not carry — RustDesk was removed from winget,
    verified 2026-09-08: 'No package found matching input criteria').
    Resolves the CURRENT x64 EXE from the official rustdesk/rustdesk
    GitHub 'latest' release and verifies its SHA-256 against GitHub's
    own API digest before running it — the pin moves with every release,
    so nothing goes stale (verified live: 1.4.9, sha256 matches the API).

    Installer: WiX Burn bundle (verified: 'burn'/'WiX' markers in the PE
    strings, Authenticode valid, signer PURSLANE — RustDesk's legal
    entity). Silent flags '/quiet /norestart'; 3010 (reboot needed) and
    1638 (already installed) count as success. FOSS; installs as a
    service, so admin is required for a silent run."""
    _require_admin_for_install("RustDesk")
    if not has_network():
        raise RuntimeError("No internet connection — RustDesk needs to download.")
    import tempfile as _tf
    ctx.log("Installing RustDesk (FOSS remote desktop)...")
    resolved = _resolve_github_latest_asset(
        ctx, "rustdesk/rustdesk", "x86_64.exe", "RustDesk")
    if resolved is None or "-x86_64.exe" not in resolved[0] or "sciter" in resolved[0].lower():
        # guard: the pattern must hit the FULL x64 setup exe, not the
        # sciter legacy/32-bit variants
        ctx.log("  ! exact x86_64 setup asset not found — aborting honestly.")
        raise RuntimeError("Could not resolve RustDesk's x64 installer from the "
                           "official GitHub releases. Manual download: "
                           "https://github.com/rustdesk/rustdesk/releases/latest")
    installer_url, sha256, exact_size = resolved
    part = {"label": "RustDesk (remote desktop)", "url": installer_url,
            "min_bytes": 15_000_000, "sha256": sha256, "exact_size": exact_size,
            "manual": "https://github.com/rustdesk/rustdesk/releases/latest"}
    fd, dest = _tf.mkstemp(prefix="cleaner_rustdesk_", suffix=".exe")
    os.close(fd)
    try:
        try:
            _download_binary(ctx, part["url"], dest, part["label"], part["min_bytes"], part=part)
        except TaskCancelled:
            raise
        except RuntimeError as exc:
            raise RuntimeError(f"{exc} Manual download: {part['manual']}")
        _run_verified_installer(ctx, part["label"], dest, "/quiet /norestart", part["manual"])
    finally:
        try:
            if os.path.exists(dest):
                os.remove(dest)
        except OSError:
            pass
    ctx.log("RustDesk installed — launch it and set a password to accept remote sessions.")


def install_freefilesync(ctx: TaskContext):
    """FREEFILESYNC (round-8, user request): the author (Zenju) distributes
    only via freefilesync.org with NO published hashes and NO winget
    package — so this uses the Peace-style pin: resolve the CURRENT
    versioned setup URL from the official download page (the static
    /download/FreeFileSync_<ver>_Windows_Setup.exe path), then require a
    VALID Authenticode signature (verified live: every release is signed
    'Florian BAUER', the author's cert) before the silent Inno Setup run
    (/VERYSILENT /NORESTART). A signature failure aborts — exactly the
    fail-closed contract Peace established for moving pins."""
    _require_admin_for_install("FreeFileSync")
    if not has_network():
        raise RuntimeError("No internet connection — FreeFileSync needs to download.")
    import re as _re
    import tempfile as _tf
    import urllib.request as _urllib
    ctx.log("Installing FreeFileSync (folder backup & sync)...")
    # resolve the current versioned installer URL from the official page
    page_url = "https://freefilesync.org/download.php"
    req = _urllib.Request(page_url, headers={"User-Agent": _ELECTRON_YML_UA})
    try:
        with _urllib.urlopen(req, timeout=30) as resp:
            html = resp.read().decode("utf-8", "replace")
    except Exception as exc:
        raise RuntimeError(f"could not load the FreeFileSync download page ({exc}). "
                           "Manual download: https://freefilesync.org/download.php")
    m = _re.search(r"(/download/FreeFileSync_[\w.]+_Windows_Setup\.exe)", html)
    if not m:
        raise RuntimeError("could not find the current FreeFileSync Windows installer "
                           "link on the official page. Manual download: "
                           "https://freefilesync.org/download.php")
    installer_url = "https://freefilesync.org" + m.group(1)
    ctx.log(f"  resolved current installer: {m.group(1)}")
    part = {"label": "FreeFileSync", "url": installer_url, "min_bytes": 10_000_000,
            "require_signature": True, "manual": "https://freefilesync.org/download.php"}
    fd, dest = _tf.mkstemp(prefix="cleaner_ffs_", suffix=".exe")
    os.close(fd)
    try:
        try:
            _download_binary(ctx, part["url"], dest, part["label"], part["min_bytes"], part=part)
        except TaskCancelled:
            raise
        except RuntimeError as exc:
            raise RuntimeError(f"{exc} Manual download: {part['manual']}")
        # Inno Setup (verified live: 'Inno Setup' PE strings + signed)
        _run_verified_installer(ctx, part["label"], dest, "/VERYSILENT /NORESTART",
                                part["manual"])
    finally:
        try:
            if os.path.exists(dest):
                os.remove(dest)
        except OSError:
            pass
    ctx.log("FreeFileSync installed — open it to set up your first folder pair.")


def install_webview2(ctx: TaskContext):
    """WEBVIEW2 RUNTIME (LTSC need): the evergreen WebView2 runtime that
    EA App, CurseForge/Overwolf, Battle.net and other launchers embed —
    missing on LTSC, which breaks those launchers' login and store pages.
    Silent latest version via winget; skipped when already present."""
    if not has_network():
        raise RuntimeError("No internet connection — WebView2 needs to download.")
    _ensure_winget(ctx)
    rc = install_winget_app(ctx, "Microsoft.EdgeWebView2Runtime", "WebView2 Runtime",
                            fallback_url="https://developer.microsoft.com/microsoft-edge/webview2/")
    if rc != 0:
        raise RuntimeError("WebView2 install failed — see the log.")
    ctx.log("WebView2 Runtime installed.")


# cached installed-app set for the catalog "installed" badges (winget list
# is slow, so it is computed once per process, in a worker thread)
_INSTALLED_CACHE: list = [None]   # [set-of-lowercase-ids-or-None]


def get_installed_ids(refresh: bool = False) -> "set[str] | None":
    """Set of winget IDs already installed on this PC (lowercased), or
    None if not (yet) known. Cached per-process; `refresh=True` recomputes.
    Uses `winget list` output which the unified installer's log format
    keeps stable enough to parse line-by-line."""
    if _INSTALLED_CACHE[0] is not None and not refresh:
        return _INSTALLED_CACHE[0]
    import subprocess as _sp
    try:
        out = _sp.run(
            ["winget", "list", "--accept-source-agreements", "--disable-interactivity"],
            capture_output=True, text=True, timeout=120,
            creationflags=getattr(_sp, "CREATE_NO_WINDOW", 0),
        )
    except Exception:
        return None
    # H7: match tokens against the KNOWN catalog/Store IDs instead of
    # guessing the Id column position. The old heuristic read parts[-2]
    # (the Version/Available column when a Source column is present, so
    # versions like "1.2.3" were recorded as IDs) and its Store-ID branch
    # (`isalnum and len==12 and isdigit`) only accepted 12-digit numbers —
    # real Store IDs (e.g. 9MV0B5HZVK9Z) never matched. Token membership
    # is immune to column shifts and locale.
    try:
        from app.app_catalog import APP_CATALOG, MSSTORE_IDS
        known = {a.get("id", "").lower() for a in APP_CATALOG if not a.get("id", "").startswith("manual:")}
        known |= {m.lower() for m in MSSTORE_IDS}
    except Exception:
        known = set()
    found = set()
    for line in (out.stdout or "").splitlines():
        for tok in line.split():
            t = tok.strip().strip(",;").lower()
            if t and t in known:
                found.add(t)
    _INSTALLED_CACHE[0] = found
    return found


# cached upgradable-app count for the Install tab's "Update Apps (N)"
# button (winget upgrade is slow, so it is computed once per process in a
# worker thread, TTL-guarded; `refresh=True` recomputes)
_UPGRADE_CACHE: list = [None, 0.0]   # [count-or-None, monotonic-timestamp]
_UPGRADE_TTL = 300.0


def get_upgradable_count(refresh: bool = False) -> "int | None":
    """Number of installed apps with a winget upgrade available, or None
    when unknown (no winget / no run yet / parse failed). Cached per-process
    for _UPGRADE_TTL seconds; `refresh=True` recomputes.

    Parses `winget upgrade` table output: skip the header + dash separator,
    count data rows, stop at the footer ('N upgrades available' / 'No ...').
    Header/footer wording is locale-dependent, so detection is structural
    (header = line containing Name+Id+Version+Available; dashes; footer =
    empty or starts with a digit+'upgrade' or 'no '), not exact-match."""
    import subprocess as _sp
    import time as _time
    now = _time.monotonic()
    cached, stamp = _UPGRADE_CACHE
    if not refresh and cached is not None and (now - stamp) < _UPGRADE_TTL:
        return cached
    if not refresh and cached is None and (now - stamp) < _UPGRADE_TTL and stamp != 0.0:
        return None
    if not cap.has_winget():
        return None
    try:
        out = _sp.run(
            ["winget", "upgrade", "--accept-source-agreements", "--disable-interactivity"],
            capture_output=True, text=True, timeout=120,
            creationflags=getattr(_sp, "CREATE_NO_WINDOW", 0),
        )
    except Exception:
        return None
    text = (out.stdout or "").strip()
    if not text:
        return None
    lines = [ln.rstrip() for ln in text.splitlines()]
    # find the table header (Name ... Id ... Version ... Available)
    hdr_idx = -1
    for i, ln in enumerate(lines):
        low = ln.lower()
        if "name" in low and "id" in low and "version" in low and "available" in low:
            hdr_idx = i
            break
    data = lines[hdr_idx + 1:] if hdr_idx >= 0 else lines
    count = 0
    for ln in data:
        s = ln.strip()
        if not s:
            continue
        # dash separator under the header
        if set(s) <= {"-", " ", "\u2014", "\u2500"}:
            continue
        low = s.lower()
        # footers: "2 upgrades available.", "No installed package found ...",
        # "No applicable update found."
        if low.startswith("no ") or "upgrade" in low and low[0].isdigit() or low.startswith("the following"):
            # "N upgrades available" terminates the table — stop, don't count it
            if "available" in low:
                break
            continue
        count += 1
    _UPGRADE_CACHE[0], _UPGRADE_CACHE[1] = count, now
    return count


def _live_status_ctx(ctx: TaskContext) -> TaskContext:
    """Wrap ctx so winget's own progress lines also reach the status bar.

    The Install tab has no live log window — without this, a 10-minute
    `winget upgrade --all` run shows one static status line. Mirroring the
    interesting lines ('(3/10) Found X …', 'Downloading …', 'Starting
    package install…') keeps the user oriented; everything still lands in
    the full log via the wrapped ctx.log."""
    import re as _re
    _progress_re = _re.compile(r"\(\d+\s*/\s*\d+\)")
    _orig_log, _orig_status = ctx.log, ctx.set_status

    def _log(msg: str) -> None:
        _orig_log(msg)
        try:
            text = msg.strip()
            if text.startswith(">"):
                text = text[1:].strip()
            low = text.lower()
            interesting = (
                _progress_re.search(text) is not None
                or low.startswith("downloading ")
                or low.startswith("starting package ")
                or low.startswith("successfully installed")
            )
            if interesting:
                short = " ".join(text.split())
                if len(short) > 90:
                    short = short[:89].rstrip() + "…"
                _orig_status(short)
        except Exception:
            pass

    return TaskContext(log=_log, set_status=_orig_status, cancelled=ctx.cancelled)


def _summarize_upgrade_lines(lines: "list[str]") -> "tuple[list[str], list[str]]":
    """Split collected `winget upgrade --all` output into (updated, failed)
    app display names. Tracks the current '(N/M) Found NAME [ID]' header;
    'Successfully installed' credits it, any 'failed with exit code' line
    blames it. Best-effort — unknown lines are ignored, never counted."""
    import re as _re
    updated, failed = [], []
    current: "str | None" = None
    found_re = _re.compile(r"\(\d+\s*/\s*\d+\)\s+Found\s+(.+?)\s+\[", _re.IGNORECASE)
    for line in lines:
        m = found_re.search(line)
        if m:
            current = " ".join(m.group(1).split())
            continue
        low = line.lower()
        if "successfully installed" in low:
            if current and current not in updated:
                updated.append(current)
            current = None
        elif "failed with exit code" in low or "uninstall failed with exit code" in low:
            if current and current not in failed:
                failed.append(f"{current} ({line.strip()})")
            current = None
    return updated, failed


def update_all_apps(ctx: TaskContext):
    """UPDATE EVERYTHING: `winget upgrade --all --silent` — updates every
    app that has an available upgrade, one command, no GUI needed. This is
    the maintenance backbone of the Install tab (and of the scheduler's
    update option): every app the catalog installs stays current.

    Partial-failure contract (user log 2026-09-03: 7 of 10 upgrades
    succeeded but winget still exits non-zero): per-package results are
    parsed from the output. Any success counts as a run success — failures
    are logged as a warning with the raw exit detail, not raised. Only a
    run where NOTHING updated raises. Known upstream flakes needing no app
    fix: exit 1603 (the app's own MSI installer failed — usually close the
    app/games and retry, or a reboot is pending) and VS installer
    2148734208 (another installer running / reboot pending)."""
    if not has_network():
        raise RuntimeError("No internet connection — updates need to download.")
    _ensure_winget(ctx)
    ctx.set_status("Updating all apps (this can take a while)...")
    ctx.log("Running winget upgrade --all (silent, latest versions)...")
    live = _live_status_ctx(ctx)
    collected: "list[str]" = []
    rc = run_cmd(live, ["winget", "upgrade", "--all", "--silent",
                        "--accept-package-agreements", "--accept-source-agreements",
                        "--disable-interactivity"],
                  shell=False, timeout=3600, collect=collected)
    # (_norm_winget_rc/_WINGET_BENIGN imported once at module top.)
    rc = _norm_winget_rc(rc)
    if ctx.cancelled() or rc == -1:
        ctx.log("  [STOPPED] Update run cancelled.")
        # audit minor 2: this used to `return` (success). Both the
        # scheduler (run_auto_update) and the GUI install worker then
        # reported 'complete/succeeded' while the log said the run was
        # stopped. Surface the stop as TaskCancelled so the callers report
        # it honestly (stopped / cancelled — never success, never failure).
        raise TaskCancelled("update run was stopped before finishing")
    if rc == 0 or rc in _WINGET_BENIGN:
        ctx.log("  [OK] All apps updated (or already current).")
        ctx.log("Update run complete.")
        return
    updated, failed = _summarize_upgrade_lines(collected)
    if updated:
        ctx.log(f"  [OK] Updated {len(updated)} app(s): {', '.join(updated)}.")
    if failed:
        ctx.log(f"  [WARN] {len(failed)} app(s) did not update — their own installers "
                f"failed, not the app list (exit 1603 usually means close the app / "
                f"reboot and retry):")
        for f in failed:
            ctx.log(f"    - {f}")
    if updated and not failed:
        ctx.log("Update run complete.")
        return
    if updated:
        # partial success: honest warning, NOT an exception — the GUI would
        # otherwise report the whole 10-minute run as failed over 1-2 flakes
        ctx.log("Update run complete with warnings (see lines above).")
        return
    # nothing at all updated — honest failure with a pointer to the log
    raise RuntimeError(f"winget upgrade --all exited with code {rc} — nothing updated. Check the log.")


def install_winget_unigetui(ctx: TaskContext):
    """winget + UniGetUI BUNDLE (user request, LTSC need): winget itself is
    NOT installed on LTSC, and winget has no GUI — so this one click (1)
    bootstraps winget when missing (official Microsoft binaries, per-user,
    no Store needed) and (2) installs UniGetUI, the graphical update
    manager, so apps stay updated without ever touching a terminal.
    Already on Pro with winget present? The bootstrap is skipped and only
    UniGetUI installs (UniGetUI is also a standalone catalog checkbox).

    ID note (2026): the project moved from Martí Climent to Devolutions, so
    the winget ID is now Devolutions.UniGetUI — the old MartiCliment.UniGetUI
    ID no longer resolves ('No package found matching input criteria')."""
    if not has_network():
        raise RuntimeError("No internet connection — winget/UniGetUI need to download.")
    if cap.has_winget():
        ctx.log("winget already installed — skipping bootstrap.")
    elif not ensure_winget_installed(ctx):
        raise RuntimeError("winget bootstrap failed — install the Microsoft Store first "
                           "(Install tab: 'Install Microsoft Store').")
    cap.invalidate_caches()
    rc = install_winget_app(ctx, "Devolutions.UniGetUI", "UniGetUI (Winget GUI)",
                            fallback_url="https://unigetui.com/")
    if rc != 0:
        raise RuntimeError("UniGetUI install failed — see the log.")
    ctx.log("winget + UniGetUI complete — open UniGetUI to update everything with one click.")


# --------------------------------------------------------------------------- #
# Task list — the LTSC prerequisite installers (the catalog apps are picked
# in the Install tab UI and run through install_selected_apps, NOT one Task
# per app — 114 catalog + 17 manual apps as preset cards would be noise;
# checkboxes + one Run button per the Install.txt spec). `group` splits the
# Essentials section in two: "LTSC Missing Components" first (Store brings
# winget, so it stays first), then the "Essentials" runtime bundles. Updates
# live on the dedicated
# "Update Apps (N)" top-bar button (UPDATE_ALL_TASK above), not as a
# checkbox — an update run is an action, not an install selection.
# --------------------------------------------------------------------------- #

from app.tasks import Task  # noqa: E402

# Dedicated Update button task (NOT in TASKS below, so it never renders as
# an Essentials checkbox): the Install tab's "Update Apps (N)" button and
# the scheduler's "Update Everything Now" both run this through
# install_selected_mixed. Single source of truth for label/description/run.
UPDATE_ALL_TASK = Task("update_all", "Update Everything", "winget upgrade --all — one click to bring every installed app current", update_all_apps, default=False, admin_required=False, column=0)

TASKS = [
    Task("install_store", "Install Microsoft Store", "Adds the Store to stripped Windows like LTSC (takes several minutes)", install_microsoft_store, default=False, admin_required=True, column=0, group="LTSC Missing Components"),
    Task("install_winget_unigetui", "Install winget + UniGetUI", "Bootstraps winget if missing, then adds the UniGetUI update GUI — no terminal needed", install_winget_unigetui, default=False, admin_required=False, column=0, group="LTSC Missing Components"),
    Task("install_xbox_stack", "Install Xbox & Game Pass", "Adds Xbox app, Gaming Services and sign-in needed for Game Pass games", install_xbox_stack, default=False, admin_required=True, column=0, group="LTSC Missing Components"),
    Task("install_game_bar", "Install Game Bar (Win+G)", "Adds the Win+G overlay for clips, screenshots and performance info", install_game_bar, default=False, admin_required=True, column=0, group="LTSC Missing Components"),
    Task("install_codecs_bundle", "Install Windows Codecs (AV1, VP9 + Web Media)", "One click for the codecs behind broken or black in-game cutscenes", install_codecs_bundle, default=False, admin_required=False, column=0, group="LTSC Missing Components"),
    Task("install_webview2", "Install WebView2 Runtime", "Evergreen runtime required by EA App, CurseForge, Battle.net and more", install_webview2, default=False, admin_required=False, column=0, group="LTSC Missing Components"),
    Task("install_vc_bundle", "Install ALL VC++ Runtimes (2005-2022)", "One click for every Visual C++ runtime — x64 + x86, all years; no guessing which one a game needs", task_install_vc_redists, default=False, admin_required=True, column=0),
    Task("install_vc_allinone", "Install Microsoft Visual C++ All-in-One", "The single latest VC++ 2015-2022 x64 runtime modern games and apps ask for", install_vcredist_allinone, default=False, admin_required=False, column=0),
    Task("install_directx_bundle", "Install ALL DirectX Runtimes", "One click for d3dx9/d3dx10/d3dx11, XAudio, XInput — fixes missing-DLL game errors", install_directx_bundle, default=False, admin_required=True, column=0),
    Task("install_directx_runtimes", "Install DirectX End-User Runtimes", "Microsoft's winget package for the legacy d3dx9/d3dx10/d3dx11 libraries behind missing-DLL game errors", install_directx_runtimes_winget, default=False, admin_required=False, column=0),
    Task("install_dotnet_bundle", "Install ALL .NET Runtimes", "One click for .NET 8 + .NET 6 (+ .NET 3.5 & DirectPlay with admin)", install_dotnet_bundle, default=False, admin_required=False, column=0),
    Task("install_java_bundle", "Install ALL Java Runtimes", "One click for Java 17 JRE + JDK and legacy Java 8 — every Minecraft era covered", install_java_bundle, default=False, admin_required=False, column=0),
    Task("install_classic_runtimes", "Install Classic Game Runtimes (OpenAL, XNA, PhysX)", "One click for OpenAL 3D audio, XNA 4.0 and legacy PhysX — S.T.A.L.K.E.R., Terraria, Mirror's Edge", install_classic_runtimes, default=False, admin_required=True, column=0),
]

# Equalizer APO + Peace GUI, bundled as ONE install (user request): it
# lives as a row inside the "Media, Streaming & Audio" catalog category
# (not its own section), backed by this standalone task — same pattern as
# UPDATE_ALL_TASK. Label renamed per user request (was "APO + Peace Audio EQ").
APO_PEACE_TASK = Task("install_apo_peace", "Equalizer APO + Peace GUI",
                      "One click for system-wide EQ (hear footsteps) with the Peace interface — reboot finishes it",
                      install_apo_peace_bundle, default=False, admin_required=True, column=0)

# Equalizer APO + FluidEQ (round-8, user request: a second GUI choice for
# the same engine — the user picks Peace OR FluidEQ, both bundles live in
# the Media category). Same embedded-row pattern as APO_PEACE_TASK.
APO_FLUIDEQ_TASK = Task("install_apo_fluideq", "Equalizer APO + FluidEQ",
                        "Same EQ engine with the modern FluidEQ interface (per-output profiles, headphone correction) — bigger download",
                        install_apo_fluideq_bundle, default=False, admin_required=True, column=0)

# RustDesk + FreeFileSync (round-8): the APO+Peace download method applied
# to two former MANUAL_ONLY_APPS (both verified NOT on winget). They render
# as installable checkbox rows inside their old categories instead of
# link-only manual rows.
RUSTDESK_TASK = Task("install_rustdesk", "RustDesk",
                     "FOSS remote desktop (TeamViewer replacement) — installed from the official GitHub release, hash-verified",
                     install_rustdesk, default=False, admin_required=False, column=0)
FREEFILESYNC_TASK = Task("install_freefilesync", "FreeFileSync",
                         "1-click folder/drive backup mirror — installed from the author's signed official installer",
                         install_freefilesync, default=False, admin_required=False, column=0)

# Embedded per-category task rows (round-8): maps a catalog category to the
# standalone tasks that render as checkbox rows at the END of that
# category (after the winget apps + manual links), picked up by the
# Install tab's bundle-row renderer. The GUI reads ONLY this mapping —
# adding a future embedded task is a one-line change here.
EMBEDDED_TASKS_BY_CATEGORY = {
    "Media, Streaming & Audio": [APO_PEACE_TASK, APO_FLUIDEQ_TASK],
    "Utilities & Cleaners": [RUSTDESK_TASK, FREEFILESYNC_TASK],
}
