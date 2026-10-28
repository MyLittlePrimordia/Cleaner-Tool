"""Verified-freed accounting (audit N-010).

A run's byte total is what its tasks *removed*: file sizes added up as each
file was deleted. What the disk actually got back is a different number, and
it is the only one the user can check in Explorer. The two disagree for
ordinary reasons, none of them a bug:

* a file deleted while another program still holds it open (NTFS keeps the
  space until the last handle closes);
* a cache an app rebuilds seconds after it is cleared, so the run removed
  bytes and the disk never noticed;
* a file moved into the Quarantine bin - it left the folder, it is still on
  disk (B-006 already subtracts those, and this module names them as a cause);
* an NTFS-compressed or deduplicated file, where the logical size a walk
  reads is bigger than the allocation it held;
* other programs freeing (or filling) space while the run was happening.

So the app stops reporting one number as if it were the other. `account()`
reconciles the two readings and `Account` is what every surface reads:

* **credited** is the number the app stands behind: never more than the run
  removed, never more than the disk actually gained. A run is not credited
  with a game updating itself mid-clean.
* **measured** is the drive-gain total, or None when free space could not be
  read on both sides of the run. None is not zero: an unreadable reading is
  labelled unverified, never dressed up as a measurement.
* **note()** explains a material gap in one sentence, in the causes it can
  actually name. Below the material threshold the two numbers simply agree,
  and there is nothing to explain.

The gap threshold is relative (10%) with an absolute floor (16 MiB), so a
1 GB run is not called short over rounding while a 4 MB run is not silently
"verified" over a 3 MB difference.

Tk-free and pure, like `app/watch.py`: `app/ui/runflow.py` supplies the two
snapshots it already took and the dialog draws what comes back.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Sequence, Tuple

#: Smallest gap worth a sentence, in bytes. Below this the two readings are
#: simply "the same number" - free space moves by megabytes on an idle PC.
MIN_GAP = 16 * 1024 * 1024
#: ... and the gap must also be this share of what the run removed, so a 20 MB
#: clean is not called short over 5 MB of ordinary noise.
GAP_SHARE = 0.10


def _fmt(n: int, compact: bool = False) -> str:
    """Bytes in the app's own form. Never raises.

    `compact` for the card's caption ("400 MB", where width is scarce), the
    long form for the transcript (matching the "Disk space freed:" line right
    above it in the same block).
    """
    try:
        from app.utils import format_bytes
        return format_bytes(int(n), compact=compact)
    except Exception:
        try:
            return f"{int(n):,} bytes"
        except Exception:
            return "0 bytes"


def gains(before_map, after_map) -> Sequence[Tuple[str, int]]:
    """[(root, gained_bytes)] for drives whose free space actually grew.

    Sorted biggest gain first. A drive missing from either snapshot, or one
    that did not gain, is left out - the reading is never invented, and a
    drive that LOST space during the run is not subtracted from the others
    (that loss may be entirely someone else's doing; crediting a negative
    would make an honest clean look like it filled the disk).
    """
    out = []
    try:
        for root, before in (before_map or {}).items():
            after = (after_map or {}).get(root)
            if not before or not after:
                continue
            delta = (after[0] or 0) - (before[0] or 0)
            if delta > 0:
                out.append((root, delta))
    except Exception:
        return []
    out.sort(key=lambda g: g[1], reverse=True)
    return out


def material_gap(counted: int) -> int:
    """Bytes of disagreement worth explaining for a run that removed `counted`."""
    try:
        counted = max(0, int(counted))
    except (TypeError, ValueError):
        return MIN_GAP
    return max(MIN_GAP, int(counted * GAP_SHARE))


@dataclass(frozen=True)
class Account:
    """What one run removed, what the disk gained, and what it is credited.

    Built by `account()`; every field is a plain number or a tuple so this can
    cross a thread boundary (the run worker builds it, the scorecard reads it).
    """

    counted: int = 0
    measured: Optional[int] = None
    moved: int = 0
    locked_files: int = 0
    gains: Tuple[Tuple[str, int], ...] = ()

    # -- readings -------------------------------------------------------- #

    @property
    def verified(self) -> bool:
        """True when free space was read on both sides of the run."""
        return self.measured is not None

    @property
    def credited(self) -> int:
        """The number the app reports: the smaller of removed and regained.

        With no measurement the run is credited with what it removed - those
        bytes did leave the disk - and `note()` says it was not verified.
        """
        if self.measured is None:
            return self.counted
        return max(0, min(self.counted, self.measured))

    @property
    def shortfall(self) -> int:
        """Bytes removed but not visible as free space (0 when unknown)."""
        if self.measured is None:
            return 0
        return max(0, self.counted - self.measured)

    @property
    def excess(self) -> int:
        """Bytes the disk gained beyond what this run removed (0 when unknown)."""
        if self.measured is None:
            return 0
        return max(0, self.measured - self.counted)

    @property
    def verdict(self) -> str:
        """One of ``unmeasured``, ``verified``, ``short``, ``over``.

        ``verified`` covers agreement within `material_gap()`, including the
        ordinary case where other programs moved a few megabytes either way.
        """
        if self.measured is None:
            return "unmeasured"
        gap = material_gap(self.counted)
        if self.shortfall > gap:
            return "short"
        if self.excess > gap:
            return "over"
        return "verified"

    # -- copy ------------------------------------------------------------ #

    def note(self) -> str:
        """The one sentence under the freed number, or "" when there is none.

        Silent in three cases, all deliberate: nothing was removed (a Tweak or
        Repair run has no byte story to reconcile), the two readings agree, or
        there is no measurement and no number on screen to qualify.
        """
        if self.counted <= 0:
            return ""
        verdict = self.verdict
        if verdict == "unmeasured":
            return ("Free space could not be read before and after this run, "
                    "so this is what its tasks removed, not a verified figure.")
        if verdict == "verified":
            return ""
        if verdict == "short":
            causes = []
            if self.moved > 0:
                causes.append("%s went to Quarantine and still uses disk"
                              % _fmt(self.moved, True))
            if self.locked_files > 0:
                causes.append("%d file(s) were locked and left in place"
                              % int(self.locked_files))
            causes.append("the rest was likely recreated by running apps or is "
                          "still held open by Windows")
            tail = "; ".join(causes)
            return ("Windows shows %s more free space \u2014 %s less than the "
                    "%s this run removed. %s."
                    % (_fmt(self.measured, True), _fmt(self.shortfall, True),
                       _fmt(self.counted, True), tail[:1].upper() + tail[1:]))
        return ("Windows shows %s more free space \u2014 more than the %s this "
                "run removed. Other programs freed space during the run too, "
                "so not all of that is from this run."
                % (_fmt(self.measured, True), _fmt(self.counted, True)))

    def log_line(self) -> str:
        """The transcript's version of the same fact, or "" when unmeasured.

        Shorter than `note()` on purpose: a log export is read by someone
        looking for a cause, so the numbers come first and the explanation is
        one clause rather than a paragraph.
        """
        if self.counted <= 0 or self.measured is None:
            return ""
        verdict = self.verdict
        if verdict == "short":
            return ("Free space check: Windows shows %s more free \u2014 %s "
                    "less than the %s removed (locked files, or caches the "
                    "apps recreated)."
                    % (_fmt(self.measured), _fmt(self.shortfall),
                       _fmt(self.counted)))
        if verdict == "over":
            return ("Free space check: Windows shows %s more free \u2014 more "
                    "than the %s removed; other programs freed space during "
                    "the run as well."
                    % (_fmt(self.measured), _fmt(self.counted)))
        return ("Free space check: Windows shows %s more free, matching the %s "
                "removed." % (_fmt(self.measured), _fmt(self.counted)))


def account(counted, before_map=None, after_map=None, *,
            moved_bytes: int = 0, locked_files: int = 0) -> Account:
    """Reconcile a run's removed bytes with its before/after drive readings.

    `before_map` / `after_map` are the `{root: (free, total)}` snapshots the
    run worker already takes for the scorecard's bar, so this adds no query of
    its own. Bad input degrades to an unmeasured account (credited = removed)
    rather than raising inside a run's completion path: a reporting failure
    must never disturb a run whose work is already done.
    """
    # OverflowError is not hypothetical: json.load() parses numbers of any
    # size, so `inf` or a 400-digit count arrives as a valid float/int that
    # int() refuses. This runs inside a run's completion path, where raising
    # would swallow the result card.
    try:
        counted = max(0, int(counted or 0))
    except (TypeError, ValueError, OverflowError):
        counted = 0
    try:
        moved = max(0, int(moved_bytes or 0))
    except (TypeError, ValueError, OverflowError):
        moved = 0
    try:
        locked = max(0, int(locked_files or 0))
    except (TypeError, ValueError, OverflowError):
        locked = 0

    found = tuple(gains(before_map, after_map))
    measured = None
    if before_map and after_map:
        # An empty snapshot is "not read", not "nothing gained": the worker
        # hands over {} when the drive query failed.
        measured = sum(g for _root, g in found)

    return Account(counted=counted, measured=measured, moved=moved,
                   locked_files=locked, gains=found)
