"""The winget plumbing: one lock, one rc-checked run, one cache per query.

Extracted from app/tasks/install_tasks.py (audit 2026-10, R-004). The Install
tab's tasks are install recipes; this module is the process-level plumbing they
all sit on, and mixing the two is what made the recipe file 2,425 lines:

  * `_run_winget` - the ONLY way this app calls winget. It holds the
    process-wide lock (winget has its own mutex and concurrent calls fail with
    "another instance is running"), decodes as UTF-8 regardless of the console
    codepage, and treats a non-zero exit as failure rather than as data. The
    last part is a fixed bug: an error banner used to be parsed as a package
    list.
  * `get_installed_ids` - "what is already on this PC", from `winget export`
    JSON first (exact ids, no column parsing), with the text listing and the
    Store identity maps as additive fallbacks. `None` means UNKNOWN and is
    never conflated with an empty set.
  * `get_upgradable_count` - the badge count, TTL-cached, and `None` when the
    localized output cannot be read rather than a confidently wrong number.

Nothing here touches Tk or the status strip; see winget_status for the copy
that turns winget's own progress lines into one status-bar sentence.
"""
from __future__ import annotations

import threading

from app import capabilities as cap
from app.utils import resolve_exe
from app.tasks.ps import ps_argv


# cached installed-app set for the catalog "installed" badges (winget list
# is slow, so it is computed once per process, in a worker thread)
_INSTALLED_CACHE: list = [None]   # [set-of-lowercase-ids-or-None]
#: Replaced (never rebound) alongside _INSTALLED_CACHE and _INSTALLED_AT so a
#: GUI reader observes either the whole old reading or the whole new one.
#: A-009: when the cached answer was measured. A count with no timestamp reads
#: as live state; "checked 9 minutes ago" is the truth.
_INSTALLED_AT: list = [0.0]


#: A-009: how long a cached badge reading is presented as current. Installed
#: software only changes when something changes it, so this is minutes, not
#: seconds -- but the reading is NOT live, and a session that lasts six hours
#: must not still be showing a tick for an app the user uninstalled at lunch.
#: The Install tab re-reads on visit once the answer is older than this.
INSTALLED_TTL = 15 * 60.0


def installed_scan_age() -> float:
    """Seconds since the installed set was measured, or -1 if never."""
    import time
    if not _INSTALLED_AT[0]:
        return -1.0
    return max(0.0, time.time() - float(_INSTALLED_AT[0]))

# M-2 fix: MSSTORE product ids NEVER appear as an id token in `winget list`
# (Store apps print as `MSIX\<PackageFamilyName>_<ver>_<arch>__<hash>`), so the
# token-membership match below can never badge them. Match the catalog's Store
# apps by an unambiguous display-name substring instead, as a FALLBACK.
#
# 1.3 (durable fix): a store id does not appear in `winget export`/`winget list`,
# but the app's MSIX package identity Name DOES appear via `Get-AppxPackage`, so
# `_MSSTORE_IDENTITY` below badges a Store app EXACTLY. The name hints remain for
# a Store id with no MSIX identity (NVIDIA App is Win32-packaged and the Store
# API names no package for it) and for hosts where Get-AppxPackage is missing.
_MSSTORE_NAME_HINTS = {
    "9NKSQGP7F2NH": ("whatsapp",),
    "9NBLGGH30XJ3": ("xbox accessories",),
    "9P4CLT2RJ1RS": ("musicbee",),
    "XP8CLZL93F5Z4P": ("nvidia app",),
}

# store_id -> MSIX package identity Name(s), lower-cased. Taken from Microsoft's
# Store product API (`Payload.PackageFamilyNames`, publisher-hash suffix
# stripped) — the same identity Windows reports through Get-AppxPackage. This is
# static reference data, never fetched at runtime. An id with no entry is simply
# not badged this way (the name hints still cover it).
_MSSTORE_IDENTITY = {
    "9MSMLRH6LZF3": ("microsoft.windowsnotepad",),          # Windows Notepad
    "9MV0B5HZVK9Z": ("microsoft.gamingapp",),               # Xbox
    "9MVZQVXJBQ9V": ("microsoft.av1videoextension",),       # AV1 Video Extension
    "9MZ95KL8MR0L": ("microsoft.screensketch",),            # Snipping Tool
    "9N4D0MSMP0PT": ("microsoft.vp9videoextensions",),      # VP9 Video Extensions
    "9N5TDP8VCMHS": ("microsoft.webmediaextensions",),      # Web Media Extensions
    "9N95Q1ZZPMH4": ("microsoft.mpeg2videoextension",),     # MPEG-2 Video Extension
    "9NBLGGH30XJ3": ("microsoft.xboxdevices",),             # Xbox Accessories
    "9NBLGGH4QGHW": ("microsoft.microsoftstickynotes",),    # Sticky Notes
    "9NCTDW2W1BH8": ("microsoft.rawimageextension",),       # Raw Image Extension
    "9NKSQGP7F2NH": ("5319275a.whatsappdesktop",),          # WhatsApp
    "9NMPJ99VJBWV": ("microsoft.yourphone",),               # Phone Link
    "9NZKPSTSNW4P": ("microsoft.xboxgamingoverlay",),       # Game Bar
    "9P4CLT2RJ1RS": ("50072stevenmayall.musicbee",),        # MusicBee
    "9P7BP5VNWKX5": ("microsoftcorporationii.quickassist",),  # Quick Assist
    "9PCFS5B6T72H": ("microsoft.paint",),                   # Paint
    "9PG2DK419DRG": ("microsoft.webpimageextension",),      # WebP Image Extension
    "9PMMSR1CGPWG": ("microsoft.heifimageextension",),      # HEIF Image Extension
    "9WZDNCRFHVN5": ("microsoft.windowscalculator",),       # Calculator
    "9WZDNCRFHWKN": ("microsoft.windowssoundrecorder",),    # Sound Recorder
    "9WZDNCRFJ3PR": ("microsoft.windowsalarms",),           # Clock
    "9WZDNCRFJ3PT": ("microsoft.zunemusic",),               # Media Player
    "9WZDNCRFJBBG": ("microsoft.windowscamera",),           # Camera
    "9WZDNCRFJBH4": ("microsoft.windows.photos",),          # Photos
}


def _installed_package_names() -> "set[str] | None":
    """Lower-cased Store/MSIX package identity Names installed for this user
    (`microsoft.windowsnotepad`, `5319275a.whatsappdesktop`, ...), or None if
    Windows could not be asked.

    Read-only: `Get-AppxPackage` is the same API the Settings > Apps list uses.
    Returns None (not an empty set) on failure so an "unknown" never reads as
    "nothing installed"."""
    import subprocess as _sp
    try:
        proc = _sp.run(
            ps_argv("Get-AppxPackage | Select-Object -ExpandProperty Name",
                    non_interactive=True),
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=60, creationflags=getattr(_sp, "CREATE_NO_WINDOW", 0),
        )
    except Exception:
        return None
    if getattr(proc, "returncode", 0) != 0:
        return None
    out = proc.stdout or ""
    return {line.strip().lower() for line in out.splitlines() if line.strip()}


# winget serializes its own operations with an internal mutex; concurrent
# invocations (the badge scan and the upgrade count used to start together at
# launch) can block or fail with "another instance is running". One process-
# wide lock makes our calls queue instead.
_WINGET_LOCK = threading.Lock()


def _run_winget(args, timeout: int = 120) -> "str | None":
    """Run winget and return its stdout as text, or None on failure.

    3.2: force a UTF-8 decode. winget emits UTF-8; under a non-English console
    codepage the default locale decoding can raise UnicodeDecodeError or mojibake
    the package ids. errors='replace' keeps a stray byte from aborting the parse.

    2026-10: a non-zero exit is a failure (None), not data. The old version
    returned stdout regardless, so an error banner could be parsed as a
    package list. Callers all handle None.
    """
    import subprocess as _sp
    try:
        with _WINGET_LOCK:
            out = _sp.run(
                [resolve_exe("winget"), *args],
                capture_output=True, text=True, encoding="utf-8", errors="replace",
                timeout=timeout,
                creationflags=getattr(_sp, "CREATE_NO_WINDOW", 0),
            )
        if out.returncode != 0:
            return None
        return out.stdout or ""
    except Exception:
        return None


def _installed_ids_from_export() -> "set[str] | None":
    """Installed winget IDs from `winget export` JSON.

    3.1: exact PackageIdentifiers grouped by source — no column or locale
    parsing at all. This is what removes the M-2/M-4 *class* of bugs for
    winget-source apps. Returns a lowercased set, or None when export is
    unavailable/unsupported (older winget) so the caller falls back to the text
    listing. An EMPTY set is a valid, definite answer (nothing installed via
    winget) and is NOT the same as None.
    """
    import json
    import os as _os
    import tempfile
    path = None
    try:
        fd, path = tempfile.mkstemp(suffix=".json", prefix="ct_winget_export_")
        _os.close(fd)
        # --include-versions was dropped: only PackageIdentifier is read, and
        # --disable-interactivity keeps a source-agreement prompt from waiting
        # on a GUI process with no console.
        text = _run_winget(
            ["export", "--accept-source-agreements",
             "--disable-interactivity", "-o", path])
        if text is None:
            return None
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        if not isinstance(data, dict):
            return None
        ids = set()
        for src in data.get("Sources", []) or []:
            if not isinstance(src, dict):
                continue
            for pkg in src.get("Packages", []) or []:
                if not isinstance(pkg, dict):
                    continue
                pid = str(pkg.get("PackageIdentifier", "")).strip().lower()
                if pid:
                    ids.add(pid)
        return ids
    except Exception:
        return None
    finally:
        try:
            if path and _os.path.isfile(path):
                _os.remove(path)
        except Exception:
            pass


def get_installed_ids(refresh: bool = False) -> "set[str] | None":
    """Set of winget IDs already installed on this PC (lowercased), or None
    if not (yet) known. Cached per-process; `refresh=True` recomputes, which
    is what the Install tab asks for once the cached answer is older than
    INSTALLED_TTL (A-009).

    3.1: the primary source is now `winget export` JSON (exact ids, no column/
    locale parsing). The `winget list` text parse is kept only as a fallback
    when export is unavailable, plus the Store display-name hints (Store apps
    are absent from the export). Both winget calls are cached once per process,
    in a worker thread."""
    if _INSTALLED_CACHE[0] is not None and not refresh:
        return _INSTALLED_CACHE[0]

    # H7: match tokens against the KNOWN catalog/Store IDs instead of guessing
    # the Id column position. The old heuristic read parts[-2] (the Version/
    # Available column when a Source column is present, so versions like
    # "1.2.3" were recorded as IDs). Token membership is immune to column
    # shifts and locale, so it survives as the text FALLBACK below.
    try:
        from app.app_catalog import APP_CATALOG, MSSTORE_IDS
        known = {a.get("id", "").lower() for a in APP_CATALOG
                 if not a.get("id", "").startswith("manual:")}
        known |= {m.lower() for m in MSSTORE_IDS}
    except Exception:
        known = set()

    exported = _installed_ids_from_export()
    found = set(exported) if exported is not None else set()

    # `winget list` is only needed when the exact sources could not answer:
    # either the export failed (fallback IDs) or Get-AppxPackage failed (so
    # Store badges have no exact identity and must fall back to name hints).
    # On a normal machine both work, and this skips ~600 ms of subprocess
    # work at launch. Hints are additive only.
    pkg_names = _installed_package_names()
    listing = None
    if exported is None or pkg_names is None:
        listing = _run_winget(
            ["list", "--accept-source-agreements", "--disable-interactivity"])
        if listing is not None:
            if exported is None:
                for line in listing.splitlines():
                    for tok in line.split():
                        t = tok.strip().strip(",;").lower()
                        if t and t in known:
                            found.add(t)
            # M-2 fix: badge Store apps by name (their product id is absent
            # from the listing AND from the export — see _MSSTORE_NAME_HINTS).
            low_out = listing.lower()
            for sid, hints in _MSSTORE_NAME_HINTS.items():
                if any(h in low_out for h in hints):
                    found.add(sid.lower())

        if exported is None and listing is None:
            return None  # nothing worked — keep the honest "unknown" contract

    # 1.3: exact Store-app detection. Get-AppxPackage reports the package
    # identity Name of every Store/MSIX app, so a store id is badged only when
    # its real package is installed — no locale or display-name guessing. This
    # unions with the name hints above: an app the hints miss is now caught
    # exactly, and a hint's false positive is only ever additive.
    if pkg_names is not None:
        for sid, identities in _MSSTORE_IDENTITY.items():
            if any(i in pkg_names for i in identities):
                found.add(sid.lower())

    _INSTALLED_CACHE[0] = found
    import time as _time
    _INSTALLED_AT[0] = _time.time()
    return found


# cached upgradable-app count for the Install tab's "Update Apps (N)"
# button (winget upgrade is slow, so it is computed once per process in a
# worker thread, TTL-guarded; `refresh=True` recomputes).
# F5-4: the list-of-two trades a tuple because the writer REPLACES element 0
# (then element 1) instead of rebinding the name — the GUI's reader thread
# never races a half-rebound tuple. Documented here so nobody "cleans it up"
# back into a tuple and reintroduces the race.
_UPGRADE_CACHE: list = [None, 0.0]   # [count-or-None, monotonic-timestamp]
_UPGRADE_TTL = 300.0


def _is_upgrade_footer_line(low: str) -> bool:
    # F2-3: explicit grouping of the old `and`/`or` mixture. The math is
    # UNCHANGED — `or` binds looser than `and`, so the original was
    # `startswith("no ") or ("upgrade" in low and low[0].isdigit()) or
    # startswith("the following")` — but the parenthesized form can't be
    # misread (or re-ordered by a future edit into `(A or B) and C`).
    # low[0] is safe: callers only pass non-empty stripped lines.
    if low.startswith("the following"):
        return True
    if "upgrade" in low and low[0].isdigit():
        return True
    # M-4 fix: a genuine "No ..." footer always names packages/upgrades/versions
    # ("No installed package found...", "No applicable update found.", "No newer
    # package versions are available..."). Requiring one of those words stops an
    # app whose NAME starts with "No " (e.g. "No Man's Sky") from being discarded
    # as a footer and lost from the "Update Apps (N)" count.
    if low.startswith("no ") and any(
            w in low for w in ("upgrade", "package", "version", "applicable",
                               "installed", "update", "found")):
        return True
    return False


def get_upgradable_count(refresh: bool = False) -> "int | None":
    """Number of installed apps with a winget upgrade available, or None
    when unknown (no winget / no run yet / parse failed). Cached per-process
    for _UPGRADE_TTL seconds - INCLUDING the unknown answer, so a broken or
    offline winget is not re-probed with a 120 s subprocess on every scan.
    `refresh=True` recomputes.

    Parses `winget upgrade` table output: skip the header + dash separator,
    count data rows, stop at the footer ('N upgrades available' / 'No ...').
    Header/footer wording is locale-dependent, so detection is structural
    (header = line containing Name+Id+Version+Available; dashes; footer =
    empty or starts with a digit+'upgrade' or 'no '), not exact-match."""
    import time as _time
    now = _time.monotonic()
    cached, stamp = _UPGRADE_CACHE
    # I-004: BOTH outcomes are cached. Only successes were, so a machine with a
    # broken or offline winget paid the full 120 s subprocess on every scan
    # (tab launch, then each post-run refresh) to learn the same nothing. The
    # `cached is None and ...` branch that used to sit here was unreachable -
    # a None was never stored - so it is removed rather than left as a decoy.
    if not refresh and stamp and (now - stamp) < _UPGRADE_TTL:
        return cached
    if not cap.has_winget():
        _UPGRADE_CACHE[0], _UPGRADE_CACHE[1] = None, now
        return None
    text = (_run_winget(
        ["upgrade", "--accept-source-agreements", "--disable-interactivity"],
        timeout=120) or "").strip()
    if not text:
        _UPGRADE_CACHE[0], _UPGRADE_CACHE[1] = None, now
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
    if hdr_idx < 0:
        # BUG-002: non-English winget builds don't say "Name/Id/Version/
        # Available" so the header is never found. Counting from line 0
        # anyway (the old behavior) treats every stray line — including
        # localized "no upgrades" messages this parser doesn't
        # recognize — as a real upgrade, showing a phantom badge count
        # on a fully up-to-date system. Degrade to "unknown" (no badge)
        # instead of a confidently wrong number.
        _UPGRADE_CACHE[0], _UPGRADE_CACHE[1] = None, now
        return None
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
        # "No applicable update found." (F2-3: predicate extracted, explicit)
        if _is_upgrade_footer_line(low):
            # "N upgrades available" terminates the table — stop, don't count it
            if "available" in low:
                break
            continue
        count += 1
    _UPGRADE_CACHE[0], _UPGRADE_CACHE[1] = count, now
    return count
