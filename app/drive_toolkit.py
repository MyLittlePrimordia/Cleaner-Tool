"""
Drive Toolkit (feature: Tools tab, 3-in-1 card) — three separate checks
sharing one card because a gamer picking a new drive or troubleshooting
an old one wants all three in one place:

  1. Health      — is a physical disk reporting problems?
  2. Speed        — how fast does a drive read/write, right now?
  3. Capacity     — is this USB stick or SD card actually the size it
                    claims, or a re-labelled fake?

Each is independent and stdlib/PowerShell-only, matching the rest of
this app's zero-dependency rule.

Health uses `Get-PhysicalDisk`, the same modern Storage Management
cmdlet Windows' own Settings app and Task Manager's "Disk" pane read —
not raw SMART parsing. Raw SMART access is unreliable behind USB/RAID
bridges and needs elevated direct-disk handles; Get-PhysicalDisk already
does that translation for us and works for SATA, NVMe and most USB
enclosures on Windows 10/11.

Speed reuses app.storage_scan.write_benchmark UNCHANGED — the exact
function Storage Insight already uses for its own benchmark — rather
than a second implementation. Storage Insight only ever benchmarks the
system drive; this exposes the same measurement for any fixed drive.

Capacity is the new logic. See capacity_test() for the algorithm and
why it is spoof-resistant.
"""

from __future__ import annotations

import os
import random
import time

IS_WINDOWS = os.name == "nt"

# --------------------------------------------------------------------------- #
# Health — Get-PhysicalDisk
# --------------------------------------------------------------------------- #

def list_physical_disks(timeout=6.0):
    """[{name, media_type, health, operational, size_bytes}], or [] on
    any failure (old Windows without the Storage module, PowerShell
    blocked by policy, timeout). An empty list means "couldn't check",
    which the dialog shows as its own honest state — never a fake
    all-healthy result.

    HealthStatus is one of "Healthy", "Warning", "Unhealthy" — Windows'
    own aggregated verdict from SMART/NVMe telemetry, already translated
    for us. MediaType is "HDD", "SSD", "SCM" or "Unspecified" (the last
    is common for USB bridges that don't report media type — shown as
    "Drive" rather than a technical placeholder)."""
    if not IS_WINDOWS:
        return []
    try:
        import subprocess as _sp
        import json as _json
        cmd = (
            "Get-PhysicalDisk -ErrorAction Stop | "
            "Select-Object FriendlyName,MediaType,HealthStatus,"
            "OperationalStatus,Size | ConvertTo-Json -Compress"
        )
        proc = _sp.run(
            ["powershell", "-NoProfile", "-NonInteractive",
             "-Command", cmd],
            capture_output=True, text=True, timeout=timeout,
            creationflags=getattr(_sp, "CREATE_NO_WINDOW", 0))
        raw = (proc.stdout or "").strip()
        if not raw:
            return []
        data = _json.loads(raw)
        if isinstance(data, dict):
            data = [data]
        if not isinstance(data, list):
            return []
        out = []
        for d in data:
            if not isinstance(d, dict):
                continue
            try:
                size = int(d.get("Size") or 0)
            except (TypeError, ValueError):
                size = 0
            media = str(d.get("MediaType") or "Unspecified")
            # PowerShell renders the MediaType enum as a plain int (0..3)
            # on some builds instead of its name — translate defensively
            # rather than showing "3" to a non-technical user.
            media = {"0": "Unspecified", "3": "HDD", "4": "SSD",
                     "5": "SCM"}.get(media, media)
            out.append({
                "name": str(d.get("FriendlyName") or "Unknown drive"),
                "media_type": media,
                "health": str(d.get("HealthStatus") or "Unknown"),
                "operational": str(d.get("OperationalStatus") or "Unknown"),
                "size_bytes": size,
            })
        return out
    except Exception:
        return []


# --------------------------------------------------------------------------- #
# Speed — thin re-export, zero duplication
# --------------------------------------------------------------------------- #

def list_fixed_drives():
    """[(letter, root, free_bytes, total_bytes)] for benchmarking targets.

    Local to this module (not imported from gui.py) so drive_toolkit has
    no dependency on the GUI — the same reason storage_scan.py and
    pc_specs.py each keep their own small drive walker instead of
    sharing one."""
    if not IS_WINDOWS:
        return []
    out = []
    try:
        import ctypes as _ct
        k32 = _ct.windll.kernel32
        bitmask = k32.GetLogicalDrives()
        for i in range(26):
            if not (bitmask >> i) & 1:
                continue
            letter = chr(ord("A") + i)
            root = f"{letter}:\\"
            try:
                if k32.GetDriveTypeW(_ct.c_wchar_p(root)) != 3:  # DRIVE_FIXED
                    continue
                free = _ct.c_ulonglong(0)
                total = _ct.c_ulonglong(0)
                if k32.GetDiskFreeSpaceExW(
                        _ct.c_wchar_p(root), None,
                        _ct.byref(total), _ct.byref(free)) == 0:
                    continue
                if total.value <= 0:
                    continue
                out.append((letter, root, free.value, total.value))
            except Exception:
                continue
    except Exception:
        return []
    return out


def benchmark_drive(root, size_mb=128, cancelled=None, progress_cb=None):
    """(write_mbps, read_mbps) for `root`. Delegates entirely to
    app.storage_scan.write_benchmark — the same measurement Storage
    Insight already uses, just exposed for any fixed drive instead of
    only the system drive. No duplicate benchmarking code to keep in
    sync.

    128 MB default (vs Storage Insight's internal 32 MB): big enough
    that a modern SSD's read-back does not finish before Windows' file
    cache has even settled, which would otherwise read as an
    implausibly high number. Still a same-machine, real-world estimate
    — not a queue-depth-swept lab benchmark — and the dialog says so."""
    try:
        from app.storage_scan import write_benchmark
        return write_benchmark(root, size_mb, cancelled=cancelled,
                               progress_cb=progress_cb)
    except Exception:
        return None, None


# --------------------------------------------------------------------------- #
# Capacity — the new check
# --------------------------------------------------------------------------- #
# Counterfeit flash almost always works by lying about its size: a chip
# with 8 GB of real NAND is flashed with a controller that reports
# 512 GB. Writes past the real 8 GB either fail, get silently dropped,
# or (the sneaky version) wrap around and overwrite earlier data. The
# only way to catch this from software is to actually write across the
# full claimed capacity and read every bit of it back afterwards — never
# verifying a block right after writing it, which would never observe a
# later wraparound.
#
# Each block's expected content is derived from a per-run random seed
# and the block's own index, not from a fixed or all-zero pattern. Some
# counterfeit controllers special-case all-zero writes (fast "success"
# without actually storing anything) — per-block pseudo-random data
# defeats that trick the same way H2testw and F3 avoid it.

_BLOCK = 1 << 20                 # 1 MiB per block — same chunk size as write_benchmark
_FILE_CAP = 3800 * (1 << 20)      # stay under FAT32's 4 GiB file limit (SD cards are FAT32)
_QUICK_SAMPLE_BLOCKS = 64         # spread across the whole capacity, sub-1-minute
_TEST_DIRNAME = "CleanerTool_CapacityTest"


def _drive_type(root):
    try:
        import ctypes as _ct
        return _ct.windll.kernel32.GetDriveTypeW(_ct.c_wchar_p(root))
    except Exception:
        return -1


def list_removable_drives():
    """[(letter, root, free_bytes, total_bytes, label)] — USB sticks and
    SD card readers only (DRIVE_REMOVABLE). Deliberately excludes fixed
    internal drives at the enumeration level, not just in the dialog's
    dropdown — capacity_test() below re-checks this itself too, so no
    caller can accidentally point a capacity test at the system drive."""
    if not IS_WINDOWS:
        return []
    out = []
    try:
        import ctypes as _ct
        k32 = _ct.windll.kernel32
        bitmask = k32.GetLogicalDrives()
        for i in range(26):
            if not (bitmask >> i) & 1:
                continue
            letter = chr(ord("A") + i)
            root = f"{letter}:\\"
            try:
                if k32.GetDriveTypeW(_ct.c_wchar_p(root)) != 2:  # DRIVE_REMOVABLE
                    continue
                free = _ct.c_ulonglong(0)
                total = _ct.c_ulonglong(0)
                if k32.GetDiskFreeSpaceExW(
                        _ct.c_wchar_p(root), None,
                        _ct.byref(total), _ct.byref(free)) == 0:
                    continue
                if total.value <= 0:
                    continue
                label = ""
                try:
                    buf = _ct.create_unicode_buffer(261)
                    if k32.GetVolumeInformationW(
                            _ct.c_wchar_p(root), buf, 261, None, None, None,
                            None, 0):
                        label = buf.value or ""
                except Exception:
                    label = ""
                out.append((letter, root, free.value, total.value, label))
            except Exception:
                continue
    except Exception:
        return []
    return out


def _block_bytes(seed, index, n):
    """Deterministic n-byte block for (seed, index) — same call always
    reproduces the same bytes, so the read phase can recompute exactly
    what the write phase put there without keeping every block in
    memory. Not cryptographic; just needs to be unpredictable enough
    that a controller cannot cheaply fake it.

    random.Random only accepts int/float/str/bytes seeds, not a tuple —
    the seed and index are folded into one int (index in the low bits,
    so consecutive blocks still get very different streams)."""
    rng = random.Random((seed << 32) ^ index)
    return rng.randbytes(n)


def capacity_test(root, mode="quick", cancelled=None, progress_cb=None,
                  log=None):
    """Write-then-verify a removable drive's free space to check whether
    it really holds what it claims. Returns a result dict:

        {"ok": bool, "declared_bytes": int, "good_bytes": int,
         "tested_bytes": int, "first_bad_at": int|None, "mode": str,
         "error": str|None}

    ok=True means every block written was read back correctly.
    good_bytes is the COUNT of good blocks times block size — not
    necessarily a contiguous prefix. This matters: a counterfeit chip
    wraps its ENTIRE logical range modulo its true physical size, so a
    later write can land on and overwrite an earlier block's physical
    location. Verifying in written order can then find block 0 already
    wrong (clobbered by whichever later block last wrapped onto it),
    even though the drive does hold some genuine bytes elsewhere. A
    "first mismatch" cutoff would report a misleading ~0 GB good for a
    drive that actually has some real capacity; counting matches
    instead gives an honest "roughly how much of this is real" figure —
    the number that actually answers the user's question. first_bad_at
    is still reported, as the offset of the first mismatch seen, for
    anyone who wants the detail. error is set (and ok=False) for
    anything that stopped the test early (drive removed, no free space,
    wrong drive type) rather than a genuine capacity finding.

    "quick" samples ~64 blocks spread evenly across the free space —
    seconds to a couple of minutes, and enough to catch the "2TB stick
    that is really 8GB" case that accounts for nearly all counterfeit
    listings. "full" writes and verifies every block of free space —
    thorough, but can take hours on a large drive and uses a full
    write/erase cycle across it, which the dialog must warn about
    before starting.

    Refuses to run on anything that is not DRIVE_REMOVABLE, regardless
    of what the caller passes — defense in depth so a UI bug can never
    point this at a fixed data drive.

    Test files live in a dedicated subfolder and are always removed
    (success, cancel, error, or drive-still-present at the end) — this
    never touches any of the user's existing files."""
    is_cancelled = cancelled or (lambda: False)
    _log = log or (lambda *_a: None)

    if not IS_WINDOWS:
        return _result(error="Windows only.")
    if _drive_type(root) != 2:
        return _result(error="Not a removable drive — refusing to test it.")

    try:
        import ctypes as _ct
        free = _ct.c_ulonglong(0)
        total = _ct.c_ulonglong(0)
        if not _ct.windll.kernel32.GetDiskFreeSpaceExW(
                _ct.c_wchar_p(root), None, _ct.byref(total), _ct.byref(free)):
            return _result(error="Could not read this drive's free space.")
    except Exception:
        return _result(error="Could not read this drive's free space.")

    declared = int(total.value)
    free_bytes = int(free.value)
    # Leave headroom so a full run can never claim the drive's last few
    # MB and make it look full to the user afterwards even once cleaned
    # up (should never happen given the finally-block cleanup below, but
    # costs nothing to be conservative).
    usable = max(0, free_bytes - _BLOCK)
    if usable < _BLOCK:
        return _result(error="Not enough free space on this drive to test "
                             "(need at least 1 MB free).")

    if mode == "quick":
        n_blocks = min(_QUICK_SAMPLE_BLOCKS,
                       max(1, usable // _BLOCK))
        # Evenly spread sample OFFSETS across the whole usable range —
        # this is what catches a "2TB" stick that is really 8GB: writes
        # placed beyond the real physical capacity land on top of
        # earlier ones, which the verify pass then finds mismatched.
        step = usable // n_blocks
        offsets = [i * step for i in range(n_blocks)]
    else:
        n_blocks = usable // _BLOCK
        offsets = [i * _BLOCK for i in range(n_blocks)]
    if not offsets:
        return _result(error="Not enough free space on this drive to test.")

    test_dir = os.path.join(root, _TEST_DIRNAME)
    seed = random.getrandbits(63)
    files_written = []
    try:
        try:
            os.makedirs(test_dir, exist_ok=True)
        except Exception:
            return _result(error="Could not create a test folder on this "
                                 "drive (it may be write-protected).")

        # --- write phase: every block, sequentially, before any verify ---
        _log("Writing test data…")
        for i, offset in enumerate(offsets):
            if is_cancelled():
                return _result(error="Cancelled.")
            file_idx = offset // _FILE_CAP
            path = os.path.join(test_dir, f"part{file_idx:03d}.bin")
            if path not in files_written:
                files_written.append(path)
            pos_in_file = offset % _FILE_CAP
            data = _block_bytes(seed, i, _BLOCK)
            try:
                # r+b / w+b so writes at arbitrary offsets in a sparse
                # file work without reading the whole thing first.
                mode_flag = "r+b" if os.path.exists(path) else "w+b"
                with open(path, mode_flag) as fh:
                    fh.seek(pos_in_file)
                    fh.write(data)
                    fh.flush()
                    os.fsync(fh.fileno())
            except OSError as exc:
                # A write failure THIS early (not on read-back) usually
                # means the drive vanished or is actually full — report
                # as a stopped test, not a capacity failure, since we
                # never got to verify anything.
                return _result(error=f"Writing stopped: {exc}",
                              declared_bytes=declared)
            try:
                if progress_cb is not None:
                    progress_cb(0.5 * (i + 1) / len(offsets))
            except Exception:
                pass

        # --- verify phase: read every block back in the same order ---
        _log("Checking what was actually written…")
        first_bad = None
        good_blocks = 0
        for i, offset in enumerate(offsets):
            if is_cancelled():
                return _result(error="Cancelled.")
            file_idx = offset // _FILE_CAP
            path = os.path.join(test_dir, f"part{file_idx:03d}.bin")
            pos_in_file = offset % _FILE_CAP
            expected = _block_bytes(seed, i, _BLOCK)
            try:
                with open(path, "rb") as fh:
                    fh.seek(pos_in_file)
                    got = fh.read(_BLOCK)
            except OSError:
                got = b""
            if got == expected:
                good_blocks += 1
            elif first_bad is None:
                first_bad = offset
            try:
                if progress_cb is not None:
                    progress_cb(0.5 + 0.5 * (i + 1) / len(offsets))
            except Exception:
                pass

        tested = offsets[-1] + _BLOCK
        good_bytes = good_blocks * _BLOCK
        if first_bad is None:
            return _result(ok=True, declared_bytes=declared,
                          good_bytes=tested, tested_bytes=tested, mode=mode)
        return _result(ok=False, declared_bytes=declared,
                      good_bytes=good_bytes, tested_bytes=tested,
                      first_bad_at=first_bad, mode=mode)
    finally:
        for p in files_written:
            try:
                os.remove(p)
            except Exception:
                pass
        try:
            os.rmdir(test_dir)
        except Exception:
            pass


def _result(ok=False, declared_bytes=0, good_bytes=0, tested_bytes=0,
           first_bad_at=None, mode="quick", error=None):
    return {
        "ok": bool(ok), "declared_bytes": int(declared_bytes),
        "good_bytes": int(good_bytes), "tested_bytes": int(tested_bytes),
        "first_bad_at": first_bad_at, "mode": mode, "error": error,
    }
