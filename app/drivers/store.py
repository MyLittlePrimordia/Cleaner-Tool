r"""The driver store: what is in it, what nothing is using, and the export-first delete.

U-005 (audit 2026-10). The Drivers tab could find, download and install a
driver, and it could back one up before replacing it - but it never showed
that Windows keeps the OLD package in the driver store every time a driver
updates. That store quietly grows to several gigabytes, and nothing in
Windows' own UI lists it.

    pnputil /enum-drivers               every third-party package in the store
    pnputil /export-driver oemNN.inf    write one package to a folder
    pnputil /delete-driver oemNN.inf    remove it from the store

Two facts this module refuses to get wrong
------------------------------------------
1. "Not in use right now" is not "safe to delete". An unplugged device's only
   driver is not in use either. A package is therefore only OFFERED when
   another package with the same original INF name is currently bound to a
   device: the previous version of a driver this PC is actually running.
   Everything else unused stays on the list as read-only, with the reason.
2. There is no delete without a successful export. `clean_elevated` builds one
   PowerShell script that exports a package, checks the exit code, and only
   then deletes; a package whose export failed is reported and left alone.
   pnputil also refuses to delete an in-use package on its own - that refusal
   is a feature, so nothing here passes /force.

Parsing is shape-based, not label-based: `pnputil /enum-drivers` prints
localized labels ("Published Name" / "Veröffentlichter Name"), but the values
have fixed shapes (oemNN.inf, a vendor .inf, a class GUID, a date + version),
so a German or Japanese Windows parses exactly like an English one.

The inventory side comes from `app.drivers.inventory`, which already reads
Win32_PnPSignedDriver's InfName - the INF a device is really bound to. That is
what decides "in use", so it agrees with Device Manager rather than with a
second guess at pnputil's output.
"""
from __future__ import annotations

import datetime
import re
from dataclasses import dataclass
from typing import Iterable, List, Optional, Set

from app.drivers import install as ins

#: `oem12.inf` - the name a third-party package has in the store.
_OEM = re.compile(r"^oem\d+\.inf$", re.I)
#: Any other .inf (the vendor's own file name).
_INF = re.compile(r"^[^\\/:*?\"<>|\s]+\.inf$", re.I)
_GUID = re.compile(r"^\{[0-9a-fA-F]{8}-(?:[0-9a-fA-F]{4}-){3}[0-9a-fA-F]{12}\}$")
#: "10/27/2023 31.0.15.3699", or a locale that writes 27.10.2023 / 2023-10-27.
_VERSION = re.compile(r"^\d{1,4}[./-]\d{1,2}[./-]\d{1,4}\s+\d[\w.]*$")

@dataclass(frozen=True)
class Package:
    """One package in the driver store, as pnputil describes it."""

    published: str                  # oem12.inf
    original: str = ""              # nvlt.inf - the vendor's file name
    provider: str = ""
    cls: str = ""
    version: str = ""               # date + version, verbatim
    signer: str = ""

    @property
    def label(self) -> str:
        """What to call it on screen: the vendor name, not the oem number."""
        return self.original or self.published

    @property
    def is_oem(self) -> bool:
        return bool(_OEM.match(self.published or ""))


@dataclass(frozen=True)
class Row:
    """A package plus the decision this PC's inventory allows for it."""

    package: Package
    state: str                      # in-use | superseded | orphaned
    why: str

    @property
    def offered(self) -> bool:
        """Only a superseded version - the one case with a real way back."""
        return self.state == "superseded"


def _block(values: List[str]) -> Optional[Package]:
    """Turn one block's values into a Package, by shape, not by label."""
    published = original = provider = cls = version = signer = ""
    spare: List[str] = []
    for value in values:
        if not published and _OEM.match(value):
            published = value
        elif not original and _INF.match(value):
            original = value
        elif _GUID.match(value):
            continue
        elif not version and _VERSION.match(value):
            version = value
        else:
            spare.append(value)
    if not published:
        return None
    # Field ORDER is fixed by pnputil, so the values with no recognisable
    # shape land in their known slots: provider, then class, then signer.
    # Only provider and class are trusted unconditionally.
    if spare:
        provider = spare[0]
    if len(spare) > 1:
        cls = spare[1]
    if len(spare) == 3:
        # Exactly three unrecognised values is the OLD shape
        # (Provider / Class / Signer). Windows 11's pnputil adds
        # "Attributes: Universal" and "WHCP Version: Unknown" before the
        # block ends, so a fourth value turns the signer slot into
        # "Universal" - which is why it is filled only when the shape is
        # unambiguous. Nothing displays the signer, and an empty field is
        # better than a confident lie.
        signer = spare[2]
    return Package(published=published, original=original, provider=provider,
                   cls=cls, version=version, signer=signer)


def parse_enum(text: str) -> List[Package]:
    """Every driver package in `pnputil /enum-drivers` output.

    Tolerant by design: a block without a published name is skipped (it is
    the tool's header, or output this parser does not understand), and a
    value that matches no shape lands in a spare slot instead of aborting the
    whole listing.
    """
    out: List[Package] = []
    values: List[str] = []

    def flush():
        if values:
            pkg = _block(values)
            if pkg is not None:
                out.append(pkg)
            values.clear()

    for raw in str(text or "").splitlines():
        line = raw.rstrip()
        if not line.strip():
            flush()
            continue
        at = line.find(":")
        if at < 0:
            continue
        value = line[at + 1:].strip()
        if not value:
            continue
        # A second published name without a blank line between blocks: some
        # pnputil builds print them back to back.
        if _OEM.match(value) and any(_OEM.match(v) for v in values):
            flush()
        values.append(value)
    flush()
    return out


def in_use(devices: Iterable[dict]) -> Set[str]:
    """Lowercased INF names a device is currently bound to."""
    out: Set[str] = set()
    for dev in devices or ():
        try:
            inf = str((dev or {}).get("inf") or "").strip().lower()
        except AttributeError:
            continue
        if inf:
            out.add(inf)
    return out


def _family(package: Package) -> str:
    return (package.original or package.published or "").strip().lower()


def assess(packages: Iterable[Package], used: Iterable[str]) -> List[Row]:
    """Decide, per package, whether it may be offered for removal.

    `used` is `in_use(devices)`. The three states are the whole safety story:

        in-use      a device is running it - never offered, never touched
        superseded  an old version of a driver that IS in use - offered
        orphaned    nothing is using it and no sibling is either - listed
                    read-only, because the hardware may simply be unplugged
    """
    used_set = {str(u).strip().lower() for u in (used or ())
                if str(u).strip()}
    families = {}
    for pkg in packages:
        families.setdefault(_family(pkg), []).append(pkg)

    rows: List[Row] = []
    for pkg in packages:
        if (pkg.published or "").strip().lower() in used_set:
            state = "in-use"
            why = "A device is using this driver right now."
        else:
            live = [s for s in families.get(_family(pkg), ())
                    if s.published != pkg.published
                    and (s.published or "").strip().lower() in used_set]
            if live:
                state = "superseded"
                why = ("An older package for %s \u2014 %s is the one in use."
                       % (pkg.label, live[0].published))
            else:
                state = "orphaned"
                why = ("No device is using it right now. It may belong to "
                       "hardware that is unplugged, so it is listed and not "
                       "offered.")
        rows.append(Row(package=pkg, state=state, why=why))

    # Offered rows first, then the explanation-only ones. Within a state the
    # vendor name sorts, so two versions of one driver sit together.
    order = {"superseded": 0, "orphaned": 1, "in-use": 2}
    rows.sort(key=lambda r: (order.get(r.state, 9),
                             r.package.label.lower(),
                             r.package.published.lower()))
    return rows


def reclaimable(rows: Iterable[Row]) -> List[Package]:
    return [r.package for r in rows or () if r.offered]


# --------------------------------------------------------------------------- #
# the elevated export-then-delete
# --------------------------------------------------------------------------- #

#: One machine-readable line per package, plus a last-line sentinel so an
#: empty or truncated capture is never read as success (B-057).
TOKEN = "CLEANER_STORE"
DONE = "CLEANER_STORE_DONE"


def build_clean_script(packages: Iterable[Package], backup_dir: str) -> str:
    """The PowerShell that exports each package and only then deletes it.

    Pure, so the two guarantees can be asserted without Windows: every delete
    is preceded by an export whose exit code is checked, and a failed export
    prints EXPORT_FAILED instead of deleting. `backup_dir` is created first;
    the elevated process writes it, so it is not assumed to exist.
    """
    from app.infra.elevated_run import quote_ps

    lines = [
        "$ErrorActionPreference = 'Continue'",
        "New-Item -ItemType Directory -Force -Path %s | Out-Null"
        % quote_ps(backup_dir),
    ]
    for pkg in packages or ():
        published = str(pkg.published or "").strip()
        if not _OEM.match(published):
            continue
        name = published[:-4] if published.lower().endswith(".inf") \
            else published
        lines += [
            "$dest = Join-Path %s %s" % (quote_ps(backup_dir),
                                         quote_ps(name)),
            "$ok = $false",
            "& pnputil /export-driver %s $dest *> $null" % quote_ps(published),
            "if ($LASTEXITCODE -eq 0) { $ok = $true }",
            "if ($ok) {",
            "  & pnputil /delete-driver %s *> $null" % quote_ps(published),
            "  if ($LASTEXITCODE -eq 0 -or $LASTEXITCODE -eq 3010) {",
            "    Write-Output ('%s %s REMOVED')" % (TOKEN, published),
            "  } else {",
            "    Write-Output ('%s %s DELETE_FAILED ' + $LASTEXITCODE)"
            % (TOKEN, published),
            "  }",
            "} else {",
            "  Write-Output ('%s %s EXPORT_FAILED')" % (TOKEN, published),
            "}",
        ]
    lines.append("Write-Output '%s'" % DONE)
    return "\n".join(lines)


def parse_clean_output(text: str) -> dict:
    """-> {"removed": [published], "failed": [(published, reason)],
    "complete": bool}.

    `complete` requires the sentinel: without it the script never reached its
    last line, so a package with no result line is "unknown", not "succeeded".
    """
    removed, failed = [], []
    for raw in str(text or "").splitlines():
        line = raw.strip()
        if not line.startswith(TOKEN):
            continue
        parts = line.split()
        if len(parts) < 3:
            continue
        published, verdict = parts[1], parts[2]
        if verdict == "REMOVED":
            removed.append(published)
        elif verdict == "DELETE_FAILED":
            reason = ("Windows refused to remove it (exit code %s)."
                      % (parts[3] if len(parts) > 3 else "?"))
            failed.append((published, reason))
        elif verdict == "EXPORT_FAILED":
            failed.append((published,
                           "It could not be backed up first, so it was left "
                           "alone."))
    return {"removed": removed, "failed": failed,
            "complete": DONE in str(text or "")}


def clean_elevated(packages: Iterable[Package], backup_root: str,
                   stamp: Optional[str] = None, runner=None,
                   timeout: int = 300) -> dict:
    """Export and remove `packages` behind one UAC prompt.

    Returns {"folder", "removed", "failed", "cancelled", "error", "complete"}.
    A declined prompt is `cancelled`, never `error`: the Restore Point tool
    set that rule and it applies here too.
    """
    picked = [p for p in packages or () if isinstance(p, Package) and p.is_oem]
    out = {"folder": "", "removed": [], "failed": [], "cancelled": False,
           "error": "", "complete": False}
    if not picked:
        out["error"] = "Nothing was selected."
        return out
    stamp = stamp or datetime.datetime.now().strftime("%Y-%m-%d_%H%M%S")
    safe_stamp = re.sub(r"[^0-9A-Za-z_-]", "_", str(stamp))
    folder = str(backup_root).rstrip("\\/") + "\\" + safe_stamp
    out["folder"] = folder
    if runner is None:
        from app.infra.elevated_run import run_elevated as runner
    try:
        ok, text = runner(build_clean_script(picked, folder), timeout)
    except Exception as exc:                            # noqa: BLE001
        out["error"] = "Could not run the removal: %s" % exc
        return out
    if not ok:
        out["cancelled"] = True
        return out
    parsed = parse_clean_output(text)
    out["removed"] = parsed["removed"]
    out["failed"] = parsed["failed"]
    out["complete"] = parsed["complete"]
    if not parsed["complete"] and not parsed["removed"]:
        out["error"] = ("Windows did not finish removing packages. Nothing "
                        "was changed; try again.")
    return out


# --------------------------------------------------------------------------- #
# the read side
# --------------------------------------------------------------------------- #

def report(run_powershell=None, runner=ins.default_runner) -> dict:
    """Read the store and this PC's devices, and assess them together.

    -> {"rows", "packages", "used", "error"}. Never raises: a failure is an
    `error` string the dialog shows, not a traceback in a worker thread.
    """
    if run_powershell is None:
        from app.drivers.service import ps_lines as run_powershell
    try:
        from app.drivers import inventory
        devices = inventory.collect(run_powershell)
    except Exception as exc:                            # noqa: BLE001
        return {"rows": [], "packages": 0, "used": 0,
                "error": "Could not read this PC's devices: %s" % exc}
    try:
        rc, text = runner(["pnputil", "/enum-drivers"], 120)
    except Exception as exc:                            # noqa: BLE001
        return {"rows": [], "packages": 0, "used": 0,
                "error": "Could not read the driver store: %s" % exc}
    if rc != 0:
        return {"rows": [], "packages": 0, "used": 0,
                "error": "Windows would not list the driver store (exit "
                         "code %s)." % rc}
    packages = parse_enum(text)
    used = in_use(devices)
    return {"rows": assess(packages, used), "packages": len(packages),
            "used": len(used), "error": ""}


__all__ = [
    "Package", "Row", "assess", "build_clean_script", "clean_elevated",
    "in_use", "parse_clean_output", "parse_enum", "reclaimable", "report",
]
