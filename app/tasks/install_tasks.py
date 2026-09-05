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
        rc = install_winget_app(ctx, app["id"], app["name"], fallback_url=app.get("url", ""))
        (ok if rc == 0 else failed).append(app["name"])
        if ctx.cancelled():
            ctx.log("Stopped — remaining apps were skipped.")
            break
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

_WINGET_ARGS = ["--accept-package-agreements", "--accept-source-agreements", "--silent"]


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
    if ctx.cancelled():
        raise RuntimeError(f"winget install of {label} cancelled.")
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
        rc = install_winget_app(ctx, pid, label, fallback_url="https://adoptium.net/")
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
        rc = install_winget_app(ctx, pid, label, fallback_url="https://dotnet.microsoft.com/download/dotnet")
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
    Note: DirectX 12 itself is built into Windows 10/11 and updated via
    Windows Update — there is no separate installer to run. The AV1/VP9/
    Web Media codec packs live in their own 'Install Windows Codecs'
    bundle now (user request: codecs bundled as windows codecs)."""
    install_directx_runtimes(ctx)      # legacy d3dx9/d3dx10/d3dx11/XAudio/XInput


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
        rc = install_winget_app(ctx, pid, label, fallback_url=url)
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
    # hash pins (streamed, so a 55MB file is not loaded whole)
    for algo, key in (("sha256", "sha256"), ("md5", "md5")):
        expected = part.get(key)
        if expected is None:
            continue
        h = getattr(hashlib, algo)()
        with open(dest, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                h.update(chunk)
        actual = h.hexdigest()
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
                    raise RuntimeError("cancelled by user")
                chunk = resp.read(1 << 16)
                if not chunk:
                    break
                f.write(chunk)
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
    if size < min_bytes or magic != b"MZ":
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
    for part in _APO_PARTS:
        fd, dest = _tf.mkstemp(prefix="cleaner_apo_", suffix=".exe")
        os.close(fd)
        try:
            try:
                _download_binary(ctx, part["url"], dest, part["label"], part["min_bytes"],
                                part=part)  # F-004b: pinned integrity gate
            except RuntimeError as exc:
                raise RuntimeError(f"{exc} Manual download: {part['manual']}")
            ctx.log(f"Running silent install: {part['label']}...")
            rc = run_cmd(ctx, f'"{dest}" {part["silent"]}', shell=True, timeout=900)
            if rc not in (0, 3010, 1638):  # 3010=reboot-needed success, 1638=already installed
                raise RuntimeError(
                    f"{part['label']} installer exited with code {rc}. "
                    f"Manual download: {part['manual']}")
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
    found = set()
    for line in (out.stdout or "").splitlines():
        # lines look like: "Name              Id                  Version"
        # we only need the second-from-last token column; ids never contain
        # spaces, so take the last token as version and the one before it
        # as the id candidate (must contain a dot or be a store id)
        parts = line.split()
        if len(parts) >= 3 and ("." in parts[-2] or parts[-2].isalnum() and len(parts[-2]) == 12 and parts[-2].isdigit()):
            found.add(parts[-2].lower())
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
    from app.downloader import _norm_winget_rc, _WINGET_BENIGN
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
# per app — 124 preset cards would be noise; checkboxes + one Run button per
# the Install.txt spec). `group` splits the Essentials section in two:
# "LTSC Missing Components" first (Store brings winget, so it stays first),
# then the "Essentials" runtime bundles. Updates live on the dedicated
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
    Task("install_directx_bundle", "Install ALL DirectX Runtimes", "One click for d3dx9/d3dx10/d3dx11, XAudio, XInput — fixes missing-DLL game errors", install_directx_bundle, default=False, admin_required=True, column=0),
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
