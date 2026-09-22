"""Live CPU / RAM / GPU / VRAM / Disk / Network sampling for the
Hardware Monitor dialog.

Design constraint: no extra installs, no admin required, works the same
whether the GPU is Intel, AMD, or Nvidia. That rules out kernel-driver
based tools (WinRing0/RTCore64-style — what HWMonitor/Afterburner use for
temperatures) entirely: real hardware temperature reads require a signed
kernel driver, needs admin, and is exactly the kind of low-level access
some anti-cheat/AV software flags. So:

  * RAM:  GlobalMemoryStatusEx (kernel32) — stdlib ctypes, always present.
  * CPU:  GetSystemTimes (kernel32) delta sampling — same technique Task
          Manager itself is built on. Static name via the registry
          (no WMI/subprocess needed).
  * GPU:  Windows' own "GPU Engine" / "GPU Adapter Memory" performance
          counters via the PDH API (pdh.dll) — the same data source
          Task Manager's own GPU graphs use. This is an OS feature, not
          a vendor SDK, so it's identical across Intel/AMD/Nvidia with
          nothing to install.
  * GPU temperature: NOT available from any public vendor-neutral API —
          notably, Task Manager doesn't show it either, for the same
          reason. Nvidia is the one exception: nvml.dll ships with every
          standard Nvidia driver (not a separate install), so a
          best-effort Nvidia-only temperature reading is included via
          NvmlTemp. Silently unavailable on AMD/Intel by design.

Every public class/function here is failure-tolerant: any Win32 call
failing, any missing counter set, any non-Windows platform degrades to
None/"N/A" rather than raising, so a sampling gap never crashes the
dialog around it.

Additional sources beyond the original CPU/RAM/GPU set:

  * Multi-GPU: gpu_list() enumerates EVERY adapter in the registry
    (not just the biggest-VRAM one), and GpuSampler buckets its
    engtype_3D engine counters per physical adapter (phys_N in the
    PDH instance path) so the dialog can offer one entry per GPU.
    Registry order and PDH phys order are both best-effort — the
    dialog pairs them positionally and falls back to totals when the
    counts disagree, never inventing a mapping.
  * Disk: PhysicalDisk active-time + read/write throughput via PDH
    (same OS counters Task Manager's Disk graph uses), free/total
    bytes via GetDiskFreeSpaceExW.
  * Network: per-interface received/sent throughput via PDH
    ("Network Interface" counters) plus Current Bandwidth for the
    link-speed scale Task Manager shows; gracefully degrades to an
    auto-scaled bar when the link speed is unreadable.
"""
import ctypes
import os
import re
import sys

IS_WINDOWS = sys.platform.startswith("win")

if IS_WINDOWS:
    import winreg


# --------------------------------------------------------------- RAM -- #

class _MEMORYSTATUSEX(ctypes.Structure):
    # Must match the real Win32 MEMORYSTATUSEX exactly (9 fields) — a
    # missing/renamed field here silently misaligns everything after it,
    # since dwLength tells GlobalMemoryStatusEx how many bytes it's
    # allowed to write into this buffer.
    #
    # Fixed-width c_uint32/c_uint64 rather than c_ulong/c_ulonglong:
    # DWORD is *always* 32 bits on Windows, but ctypes.c_ulong follows
    # the platform's native "unsigned long" (4 bytes on Windows' LLP64
    # model, 8 bytes on Linux's LP64) — using the fixed-width type keeps
    # this struct's layout unambiguous (and independently verifiable —
    # sizeof() comes out identical on any platform, not just Windows).
    _fields_ = [
        ("dwLength", ctypes.c_uint32),
        ("dwMemoryLoad", ctypes.c_uint32),
        ("ullTotalPhys", ctypes.c_uint64),
        ("ullAvailPhys", ctypes.c_uint64),
        ("ullTotalPageFile", ctypes.c_uint64),
        ("ullAvailPageFile", ctypes.c_uint64),
        ("ullTotalVirtual", ctypes.c_uint64),
        ("ullAvailVirtual", ctypes.c_uint64),
        ("ullAvailExtendedVirtual", ctypes.c_uint64),
    ]


def sample_ram():
    """-> {'percent', 'used_bytes', 'total_bytes'} or None on failure."""
    if not IS_WINDOWS:
        return None
    try:
        stat = _MEMORYSTATUSEX()
        stat.dwLength = ctypes.sizeof(_MEMORYSTATUSEX)
        if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(stat)):
            return None
        used = stat.ullTotalPhys - stat.ullAvailPhys
        return {
            "percent": float(stat.dwMemoryLoad),
            "used_bytes": int(used),
            "total_bytes": int(stat.ullTotalPhys),
        }
    except Exception:
        return None


# --------------------------------------------------------------- CPU -- #

class _FILETIME(ctypes.Structure):
    _fields_ = [("dwLowDateTime", ctypes.c_uint32),
                ("dwHighDateTime", ctypes.c_uint32)]


def _filetime_to_int(ft):
    return (ft.dwHighDateTime << 32) | ft.dwLowDateTime


def cpu_percent_from_deltas(d_idle, d_kernel, d_user):
    """Pure math, split out from CpuSampler so it's testable without
    Windows. GetSystemTimes' kernel time already INCLUDES idle time (per
    MSDN), so total elapsed = d_kernel + d_user, and busy = total - idle.
    This is the standard GetSystemTimes CPU% formula."""
    total = d_kernel + d_user
    if total <= 0:
        return 0.0
    busy = total - d_idle
    return max(0.0, min(100.0, (busy / total) * 100.0))


def cpu_static_info():
    """-> {'name', 'logical_cores'}. Registry read (no WMI/subprocess
    on the hot path) + os.cpu_count(); vendor-agnostic since it's just
    reporting the name the CPU/firmware itself already exposes."""
    name = "Unknown CPU"
    if IS_WINDOWS:
        try:
            with winreg.OpenKey(
                    winreg.HKEY_LOCAL_MACHINE,
                    r"HARDWARE\DESCRIPTION\System\CentralProcessor\0") as k:
                val, _t = winreg.QueryValueEx(k, "ProcessorNameString")
                decoded = _decode_reg_str(val)
                if decoded:
                    name = " ".join(decoded.split())
        except Exception:
            pass
    return {"name": name, "logical_cores": os.cpu_count() or 0}


class CpuSampler:
    """Stateful — call .sample() once per tick. Returns None on the
    first call (needs two readings to compute a delta, same as any
    rate-based counter), a 0-100 float on every call after."""

    def __init__(self):
        self._prev = None

    def sample(self):
        if not IS_WINDOWS:
            return None
        try:
            idle, kernel, user = _FILETIME(), _FILETIME(), _FILETIME()
            ok = ctypes.windll.kernel32.GetSystemTimes(
                ctypes.byref(idle), ctypes.byref(kernel), ctypes.byref(user))
            if not ok:
                return None
            cur = (_filetime_to_int(idle), _filetime_to_int(kernel),
                   _filetime_to_int(user))
            prev, self._prev = self._prev, cur
            if prev is None:
                return None
            return cpu_percent_from_deltas(
                cur[0] - prev[0], cur[1] - prev[1], cur[2] - prev[2])
        except Exception:
            return None


# ---------------------------------------------------------------- GPU -- #

_PDH_FMT_DOUBLE = 0x00000200
# PDH_STATUS is a signed 32-bit LONG; PDH_MORE_DATA's bit pattern
# (0x800007D2) read as signed 32-bit comes out negative. Computed via
# c_int32 (fixed-width) rather than c_long — c_long follows the
# platform's native "long" width (4 bytes on Windows, 8 on Linux), which
# would silently skip the sign flip on a 64-bit non-Windows machine.
_PDH_MORE_DATA = ctypes.c_int32(0x800007D2).value


class _PDH_FMT_COUNTERVALUE(ctypes.Structure):
    _fields_ = [("CStatus", ctypes.c_uint32), ("doubleValue", ctypes.c_double)]


def _pdh_expand_wildcard(path):
    """-> list[str] of concrete counter paths matching a PDH wildcard
    path, via the standard two-pass PdhExpandWildCardPathW pattern
    (probe for the required buffer size, then fetch)."""
    try:
        pdh = ctypes.windll.pdh
        size = ctypes.c_ulong(0)
        rc = pdh.PdhExpandWildCardPathW(None, path, None, ctypes.byref(size), 0)
        if rc != _PDH_MORE_DATA and rc != 0:
            return []
        if size.value <= 0:
            return []
        buf = ctypes.create_unicode_buffer(size.value)
        rc = pdh.PdhExpandWildCardPathW(None, path, buf, ctypes.byref(size), 0)
        if rc != 0:
            return []
        return [p for p in buf[:size.value].split("\x00") if p]
    except Exception:
        return []


_PHYS_RE = re.compile(r"phys_(\d+)", re.IGNORECASE)


def _phys_index_from_path(path):
    """Physical-adapter index parsed from a PDH instance path, or None.

    GPU Engine instances look like
    pid_1234_luid_0x1A2B_0x0_phys_0_eng_0_engtype_3D — the phys_N
    segment is the physical adapter the engine belongs to. GPU
    Adapter Memory instances carry the same phys_N segment on most
    drivers; older ones don't, and those fall back to enumeration
    order (see _setup). Pure function, never raises."""
    try:
        m = _PHYS_RE.search(path or "")
        return int(m.group(1)) if m else None
    except Exception:
        return None


class GpuSampler:
    """Vendor-agnostic GPU utilization % and VRAM usage via Windows'
    'GPU Engine' / 'GPU Adapter Memory' performance counters — the exact
    data source backing Task Manager's own GPU graphs, so this reads the
    same on Intel/AMD/Nvidia with nothing to install. "GPU %" is the sum
    of Utilization Percentage across every engtype_3D engine instance,
    matching what Task Manager's headline "GPU" number shows.

    Multi-GPU: engine counters are additionally bucketed per physical
    adapter (phys_N in the instance path) so sample_per_adapter()
    can report one {gpu_percent, vram_used_bytes} pair per GPU for
    the dialog's GPU picker. VRAM adapter instances that carry no
    phys_N segment are assigned positionally in sorted-instance
    order (best-effort, documented at the call site)."""

    def __init__(self):
        self._hquery = None
        self._util_counters = []      # [(phys|None, hcounter)]
        self._vram_counters = []      # [(phys|None, hcounter)]
        self._ok = self._setup()

    def _setup(self):
        if not IS_WINDOWS:
            return False
        try:
            pdh = ctypes.windll.pdh
            hquery = ctypes.c_void_p()
            if pdh.PdhOpenQueryW(None, 0, ctypes.byref(hquery)) != 0:
                return False
            self._hquery = hquery
            for p in _pdh_expand_wildcard(r"\GPU Engine(*)\Utilization Percentage"):
                if "engtype_3d" not in p.lower():
                    continue
                hc = ctypes.c_void_p()
                if pdh.PdhAddEnglishCounterW(hquery, p, 0, ctypes.byref(hc)) == 0:
                    self._util_counters.append(
                        (_phys_index_from_path(p), hc))
            vram_paths = sorted(
                _pdh_expand_wildcard(r"\GPU Adapter Memory(*)\Dedicated Usage"))
            # Positional fallback for adapter instances without a phys_N
            # segment: assign them 0..N in sorted order. Instances WITH a
            # segment keep their parsed index.
            auto_idx = 0
            used_phys = set()
            for p in vram_paths:
                hc = ctypes.c_void_p()
                if pdh.PdhAddEnglishCounterW(hquery, p, 0, ctypes.byref(hc)) != 0:
                    continue
                phys = _phys_index_from_path(p)
                if phys is None:
                    while auto_idx in used_phys:
                        auto_idx += 1
                    phys = auto_idx
                    auto_idx += 1
                used_phys.add(phys)
                self._vram_counters.append((phys, hc))
            return bool(self._util_counters or self._vram_counters)
        except Exception:
            return False

    @property
    def phys_indices(self):
        """Sorted physical-adapter indices seen across engine+VRAM
        counters (e.g. [0] single-GPU, [0, 1] mixed iGPU+dGPU).
        Empty when counters are unavailable."""
        try:
            idx = set()
            for phys, _hc in self._util_counters:
                if phys is not None:
                    idx.add(phys)
            for phys, _hc in self._vram_counters:
                if phys is not None:
                    idx.add(phys)
            if not idx and (self._util_counters or self._vram_counters):
                return [0]
            return sorted(idx)
        except Exception:
            return []

    def _read_counter(self, pdh, hc):
        """One formatted-double read -> float|None (CStatus must be 0)."""
        try:
            val = _PDH_FMT_COUNTERVALUE()
            rc = pdh.PdhGetFormattedCounterValue(
                hc, _PDH_FMT_DOUBLE, None, ctypes.byref(val))
            if rc == 0 and val.CStatus == 0:
                return float(val.doubleValue)
        except Exception:
            pass
        return None

    def sample(self):
        """-> {'gpu_percent': float|None, 'vram_used_bytes': int|None}.
        Totals across all adapters (backwards-compatible headline).
        The first call after (re)setup primes the rate counters and
        returns gpu_percent=None (PDH rate counters need two collections
        — same reason CpuSampler's first call returns None)."""
        per = self.sample_per_adapter()
        out = {"gpu_percent": None, "vram_used_bytes": None}
        try:
            pcts = [v["gpu_percent"] for v in per.values()
                    if v.get("gpu_percent") is not None]
            if pcts:
                out["gpu_percent"] = max(0.0, min(100.0, sum(pcts)))
            vrams = [v["vram_used_bytes"] for v in per.values()
                     if v.get("vram_used_bytes") is not None]
            if vrams:
                out["vram_used_bytes"] = int(sum(vrams))
        except Exception:
            pass
        return out

    def sample_per_adapter(self):
        """-> {phys_index: {'gpu_percent': float|None,
        'vram_used_bytes': int|None}}. One CollectQueryData per call,
        then per-bucket sums. Buckets with no readable counters report
        Nones (honest gaps, never fake 0s). Empty dict when unavailable.

        NOTE: like sample(), the first call after (re)setup primes the
        rate counters — utilization legs read None on that pass while
        VRAM (an instantaneous counter) may already land."""
        out = {}
        if not self._ok or self._hquery is None:
            return out
        try:
            pdh = ctypes.windll.pdh
            if pdh.PdhCollectQueryData(self._hquery) != 0:
                return out
            buckets = {}
            for phys, hc in self._util_counters:
                key = phys if phys is not None else 0
                v = self._read_counter(pdh, hc)
                if v is not None:
                    b = buckets.setdefault(
                        key, {"gpu": 0.0, "gpu_got": False,
                              "vram": 0, "vram_got": False})
                    b["gpu"] += v
                    b["gpu_got"] = True
            for phys, hc in self._vram_counters:
                key = phys if phys is not None else 0
                v = self._read_counter(pdh, hc)
                if v is not None:
                    b = buckets.setdefault(
                        key, {"gpu": 0.0, "gpu_got": False,
                              "vram": 0, "vram_got": False})
                    b["vram"] += int(v)
                    b["vram_got"] = True
            # Ensure every known phys index appears, even when all its
            # counters misbehaved this pass (honest Nones).
            for phys in self.phys_indices:
                buckets.setdefault(phys, {"gpu": 0.0, "gpu_got": False,
                                          "vram": 0, "vram_got": False})
            for phys, b in buckets.items():
                out[phys] = {
                    "gpu_percent": (max(0.0, min(100.0, b["gpu"]))
                                    if b["gpu_got"] else None),
                    "vram_used_bytes": (int(b["vram"])
                                        if b["vram_got"] else None),
                }
            return out
        except Exception:
            return out

    def refresh_instances(self):
        """Re-enumerate GPU Engine/Adapter Memory instances. The set of
        instances changes as games/apps launch or close (each gets its
        own engine instance) — call this every ~10-15s from the dialog's
        tick loop, since a counter for an instance that's gone quietly
        reads 0 forever rather than erroring."""
        self.close()
        self._util_counters = []
        self._vram_counters = []
        self._ok = self._setup()

    def close(self):
        try:
            if self._hquery is not None:
                ctypes.windll.pdh.PdhCloseQuery(self._hquery)
        except Exception:
            pass
        self._hquery = None
        self._util_counters = []
        self._vram_counters = []


def _decode_reg_str(v):
    """winreg.QueryValueEx sometimes hands back raw bytes for a value
    that's semantically a string — HardwareInformation.AdapterString in
    particular is commonly stored as REG_BINARY containing UTF-16LE text
    rather than a proper REG_SZ, depending on the driver. str(bytes_obj)
    does NOT decode it — it produces the literal "b'A\\x00M\\x00...'"
    repr, which is exactly the garbled text this fixes. Falls back to
    latin-1 (never fails, always returns *some* text) if UTF-16LE
    decoding doesn't look right."""
    if v is None:
        return None
    if isinstance(v, bytes):
        try:
            v = v.decode("utf-16le")
        except UnicodeDecodeError:
            v = v.decode("latin-1", errors="ignore")
    text = str(v).replace("\x00", "").strip()
    return text or None


def gpu_list():
    """-> [{'name', 'total_vram_bytes', 'total_vram_approx'}, ...] — ONE
    entry per display adapter found under Control\\Video, sorted
    largest-VRAM-first (discrete GPU first on mixed iGPU+dGPU rigs).

    Same registry source as the old single-GPU reader (accurate 64-bit
    HardwareInformation.qwMemorySize, 32-bit MemorySize fallback flagged
    approximate, AdapterString/DriverDesc names with the bytes-decode
    fix) — just no longer discarding every adapter but one. Entries
    with no readable memory still appear (total_vram_bytes None) so
    the picker can name them. Empty list (never None) when unreadable
    or non-Windows; the dialog falls back to "Unknown GPU"."""
    out = []
    if not IS_WINDOWS:
        return out
    base = r"SYSTEM\CurrentControlSet\Control\Video"
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, base) as root:
            i = 0
            while True:
                try:
                    guid = winreg.EnumKey(root, i)
                    i += 1
                except OSError:
                    break
                try:
                    with winreg.OpenKey(root, guid + r"\0000") as k:
                        mem, is_approx = None, False
                        try:
                            v, _t = winreg.QueryValueEx(
                                k, "HardwareInformation.qwMemorySize")
                            mem = int(v)
                        except OSError:
                            try:
                                v, _t = winreg.QueryValueEx(
                                    k, "HardwareInformation.MemorySize")
                                mem, is_approx = int(v), True
                            except OSError:
                                pass
                        name = None
                        for value_name in ("HardwareInformation.AdapterString",
                                           "DriverDesc"):
                            try:
                                v, _t = winreg.QueryValueEx(k, value_name)
                                name = _decode_reg_str(v)
                                if name:
                                    break
                            except OSError:
                                continue
                        if not name and mem is None:
                            continue  # empty Video subkey, not an adapter
                        out.append({
                            "name": name or "Unknown GPU",
                            "total_vram_bytes": mem,
                            "total_vram_approx": is_approx,
                        })
                except OSError:
                    continue
    except Exception:
        pass
    # De-dupe mirrored 0000 entries some drivers leave behind, then
    # discrete-first so index 0 is the interesting GPU on mixed rigs.
    seen, uniq = set(), []
    for entry in out:
        key = (entry["name"], entry["total_vram_bytes"])
        if key not in seen:
            seen.add(key)
            uniq.append(entry)
    uniq.sort(key=lambda e: (e["total_vram_bytes"] is None,
                             -(e["total_vram_bytes"] or 0)))
    return uniq


def gpu_static_info():
    """-> {'name', 'total_vram_bytes'} for the largest-VRAM adapter.

    Backwards-compatible headline kept for existing callers/tests:
    implemented on top of gpu_list() — identical "largest dedicated
    memory wins" choice as before, Unknown/None when unmeasurable."""
    try:
        entries = gpu_list()
    except Exception:
        entries = []
    if not entries:
        return {"name": "Unknown GPU", "total_vram_bytes": None,
                "total_vram_approx": False}
    best = entries[0]
    return {"name": best.get("name") or "Unknown GPU",
            "total_vram_bytes": best.get("total_vram_bytes"),
            "total_vram_approx": bool(best.get("total_vram_approx"))}


# --------------------------------------------- Nvidia-only temperature -- #

class NvmlTemp:
    """Best-effort Nvidia GPU temperature via nvml.dll, which ships with
    every standard Nvidia driver — nothing extra to install for Nvidia
    users. Silently unavailable (sample() returns None) on AMD/Intel, or
    if nvml can't be found/initialized for any reason: this is a bonus
    on top of the vendor-agnostic dashboard, never required by it.

    Multi-GPU: handles are opened per device index (up to device_count)
    so the dialog can read the temperature of the SELECTED GPU, not
    just GPU 0. sample() keeps its old meaning (device 0) for
    backwards compatibility; sample_at(i) reads any index."""

    NVML_TEMPERATURE_GPU = 0

    def __init__(self):
        self._lib = None
        self._handle = None
        self._handles = {}    # index -> c_void_p
        self._count = 0
        self._ok = self._setup()

    def _load_nvml(self):
        try:
            return ctypes.CDLL("nvml.dll")
        except OSError:
            pass
        # Standard NVSMI install path — same fallback pynvml uses when
        # nvml.dll isn't already on PATH.
        nvsmi = os.path.join(
            os.environ.get("ProgramFiles", r"C:\Program Files"),
            "NVIDIA Corporation", "NVSMI")
        dll_path = os.path.join(nvsmi, "nvml.dll")
        if not os.path.isfile(dll_path):
            return None
        try:
            if hasattr(os, "add_dll_directory"):
                os.add_dll_directory(nvsmi)
        except Exception:
            pass
        try:
            return ctypes.CDLL(dll_path)
        except OSError:
            return None

    def _setup(self):
        if not IS_WINDOWS:
            return False
        try:
            lib = self._load_nvml()
            if lib is None:
                return False
            if lib.nvmlInit_v2() != 0:
                return False
            self._lib = lib
            handle = ctypes.c_void_p()
            if lib.nvmlDeviceGetHandleByIndex_v2(0, ctypes.byref(handle)) != 0:
                return False
            self._handle = handle
            self._handles[0] = handle
            # Best-effort device count (drives the GPU picker's temp
            # availability); a failure here never fails setup — device 0
            # already works and count just stays 1.
            try:
                n = ctypes.c_uint(0)
                if lib.nvmlDeviceGetCount_v2(ctypes.byref(n)) == 0:
                    self._count = max(1, int(n.value))
                else:
                    self._count = 1
            except Exception:
                self._count = 1
            return True
        except Exception:
            return False

    @property
    def available(self):
        """True when an Nvidia driver answered (temp CAN be read). The
        dialog uses this — not vendor-name sniffing — to decide
        whether the temperature readout exists at all."""
        try:
            return bool(self._ok and self._lib is not None)
        except Exception:
            return False

    @property
    def device_count(self):
        """Number of NVML-visible Nvidia GPUs (0 when unavailable)."""
        try:
            return int(self._count) if self.available else 0
        except Exception:
            return 0

    def _handle_for(self, index):
        """c_void_p handle for device `index`, opening it on first use.
        None when unavailable/out of range — never raises."""
        try:
            index = int(index)
        except Exception:
            return None
        if not self.available or index < 0:
            return None
        try:
            if index in self._handles:
                return self._handles[index]
            handle = ctypes.c_void_p()
            if self._lib.nvmlDeviceGetHandleByIndex_v2(
                    index, ctypes.byref(handle)) != 0:
                return None
            self._handles[index] = handle
            return handle
        except Exception:
            return None

    def sample_at(self, index):
        """-> int (Celsius) for device `index`, or None. Used by the
        GPU picker so the temperature follows the selected GPU."""
        handle = self._handle_for(index)
        if handle is None:
            return None
        try:
            temp = ctypes.c_uint(0)
            rc = self._lib.nvmlDeviceGetTemperature(
                handle, ctypes.c_int(self.NVML_TEMPERATURE_GPU),
                ctypes.byref(temp))
            if rc != 0:
                return None
            return int(temp.value)
        except Exception:
            return None

    def sample(self):
        """-> int (Celsius) or None (device 0 — backwards compatible)."""
        return self.sample_at(0)

    def close(self):
        try:
            if self._lib is not None:
                self._lib.nvmlShutdown()
        except Exception:
            pass
        self._lib = None
        self._handle = None
        try:
            self._handles = {}
        except Exception:
            pass
        self._count = 0


# ---------------------------------------------------------------- Disk -- #

def drive_free_total(root_path):
    """(free_bytes, total_bytes) for one drive root, or None.

    Same GetDiskFreeSpaceExW call the scorecard and drive chips use —
    instant, no WMI. Never raises."""
    if not IS_WINDOWS:
        return None
    try:
        free = ctypes.c_ulonglong(0)
        total = ctypes.c_ulonglong(0)
        avail = ctypes.c_ulonglong(0)
        if ctypes.windll.kernel32.GetDiskFreeSpaceExW(
                ctypes.c_wchar_p(root_path), ctypes.byref(avail),
                ctypes.byref(total), ctypes.byref(free)):
            return int(free.value), int(total.value)
    except Exception:
        pass
    return None


def list_drives():
    """[(letter, root)] for every ready logical drive, C: first.

    GetLogicalDrives bitmask + a GetDiskFreeSpaceExW readiness probe
    (empty card-reader slots answer 0 total and are skipped). Never
    raises; [] when unreadable/non-Windows."""
    out = []
    if not IS_WINDOWS:
        return out
    try:
        bitmask = ctypes.windll.kernel32.GetLogicalDrives()
    except Exception:
        return out
    if not bitmask:
        return out
    try:
        for i in range(26):
            if not (bitmask >> i) & 1:
                continue
            letter = chr(ord("A") + i)
            root = "%s:\\" % letter
            try:
                pair = drive_free_total(root)
            except Exception:
                pair = None
            if pair and pair[1] > 0:
                out.append((letter, root))
    except Exception:
        pass
    out.sort(key=lambda d: (d[0] != "C", d[0]))
    return out


class DiskSampler:
    """Disk active-time % + read/write throughput via Windows'
    PhysicalDisk performance counters — the same source as Task
    Manager's Disk graph. No admin, no installs, vendor-neutral.

    sample() -> {'active_percent', 'read_bps', 'write_bps'} with Nones
    on gaps. Active time is the MAX across physical disks (the
    busiest disk — what a single Disk card should headline); _Total
    instances are excluded from the max but included in throughput
    only when no per-disk instances exist. Rate counters need two
    collections, so the first call primes and reads None on the %
    leg (throughput uses the same pass)."""

    def __init__(self):
        self._hquery = None
        self._active = []   # [hcounter] (per-disk, no _Total)
        self._read = []     # [hcounter]
        self._write = []    # [hcounter]
        self._ok = self._setup()

    def _add(self, hquery, path):
        try:
            hc = ctypes.c_void_p()
            if ctypes.windll.pdh.PdhAddEnglishCounterW(
                    hquery, path, 0, ctypes.byref(hc)) == 0:
                return hc
        except Exception:
            pass
        return None

    def _setup(self):
        if not IS_WINDOWS:
            return False
        try:
            pdh = ctypes.windll.pdh
            hquery = ctypes.c_void_p()
            if pdh.PdhOpenQueryW(None, 0, ctypes.byref(hquery)) != 0:
                return False
            self._hquery = hquery
            for p in _pdh_expand_wildcard(r"\PhysicalDisk(*)\% Disk Time"):
                if "_total" in p.lower():
                    continue
                hc = self._add(hquery, p)
                if hc is not None:
                    self._active.append(hc)
            for p in _pdh_expand_wildcard(
                    r"\PhysicalDisk(*)\Disk Read Bytes/sec"):
                if "_total" in p.lower():
                    continue
                hc = self._add(hquery, p)
                if hc is not None:
                    self._read.append(hc)
            for p in _pdh_expand_wildcard(
                    r"\PhysicalDisk(*)\Disk Write Bytes/sec"):
                if "_total" in p.lower():
                    continue
                hc = self._add(hquery, p)
                if hc is not None:
                    self._write.append(hc)
            return bool(self._active or self._read or self._write)
        except Exception:
            return False

    def _read_one(self, hc):
        try:
            val = _PDH_FMT_COUNTERVALUE()
            rc = ctypes.windll.pdh.PdhGetFormattedCounterValue(
                hc, _PDH_FMT_DOUBLE, None, ctypes.byref(val))
            if rc == 0 and val.CStatus == 0:
                return float(val.doubleValue)
        except Exception:
            pass
        return None

    def sample(self):
        out = {"active_percent": None, "read_bps": None,
               "write_bps": None}
        if not self._ok or self._hquery is None:
            return out
        try:
            if ctypes.windll.pdh.PdhCollectQueryData(self._hquery) != 0:
                return out
            if self._active:
                vals = [self._read_one(hc) for hc in self._active]
                vals = [v for v in vals if v is not None]
                if vals:
                    out["active_percent"] = max(
                        0.0, min(100.0, max(vals)))
            for key, counters in (("read_bps", self._read),
                                  ("write_bps", self._write)):
                if counters:
                    vals = [self._read_one(hc) for hc in counters]
                    vals = [v for v in vals if v is not None]
                    if vals:
                        out[key] = max(0.0, sum(vals))
            return out
        except Exception:
            return out

    def close(self):
        try:
            if self._hquery is not None:
                ctypes.windll.pdh.PdhCloseQuery(self._hquery)
        except Exception:
            pass
        self._hquery = None
        self._active = []
        self._read = []
        self._write = []


# -------------------------------------------------------------- Network -- #

def _is_loopback_iface(name):
    try:
        n = (name or "").lower()
        return ("loopback" in n or "isatap" in n or "teredo" in n
                or "6to4" in n)
    except Exception:
        return False


class NetworkSampler:
    """LAN/Wi-Fi throughput via Windows' 'Network Interface'
    performance counters (Task Manager's Networking source).

    sample() -> {'down_bps', 'up_bps', 'total_mbps', 'iface',
    'link_mbps'} where iface is the busiest interface name and
    link_mbps its Current Bandwidth link speed (None when unreadable
    — the dialog then auto-scales the bar against the observed max
    instead of showing a fake %). interfaces() lists usable
    interface names for a picker; select(name) pins one (None =
    auto-busiest). First call primes rate counters (Nones on the
    throughput legs)."""

    def __init__(self):
        self._hquery = None
        self._rx = {}       # iface -> hcounter
        self._tx = {}       # iface -> hcounter
        self._bw = {}       # iface -> hcounter (Current Bandwidth)
        self._pinned = None
        self._max_seen_bps = 0.0
        self._ok = self._setup()

    def _add(self, hquery, path):
        try:
            hc = ctypes.c_void_p()
            if ctypes.windll.pdh.PdhAddEnglishCounterW(
                    hquery, path, 0, ctypes.byref(hc)) == 0:
                return hc
        except Exception:
            pass
        return None

    @staticmethod
    def _iface_from_path(path, counter):
        """Instance name between parens: '\\Network Interface(X)\\ctr'."""
        try:
            start = path.index("(") + 1
            end = path.rindex(")", 0, path.rindex("\\"))
            name = path[start:end].strip()
            return name or None
        except Exception:
            try:
                start = path.index("(") + 1
                end = path.index(")", start)
                return path[start:end].strip() or None
            except Exception:
                return None

    def _setup(self):
        if not IS_WINDOWS:
            return False
        try:
            pdh = ctypes.windll.pdh
            hquery = ctypes.c_void_p()
            if pdh.PdhOpenQueryW(None, 0, ctypes.byref(hquery)) != 0:
                return False
            self._hquery = hquery
            for counter, store in (
                    ("Bytes Received/sec", self._rx),
                    ("Bytes Sent/sec", self._tx),
                    ("Current Bandwidth", self._bw)):
                for p in _pdh_expand_wildcard(
                        r"\Network Interface(*)\%s" % counter):
                    name = self._iface_from_path(p, counter)
                    if not name or _is_loopback_iface(name):
                        continue
                    hc = self._add(hquery, p)
                    if hc is not None:
                        store[name] = hc
            return bool(self._rx or self._tx)
        except Exception:
            return False

    def interfaces(self):
        """Sorted usable interface names (never raises)."""
        try:
            return sorted(set(self._rx) | set(self._tx))
        except Exception:
            return []

    def select(self, name):
        """Pin one interface (or None for auto-busiest). Unknown names
        are ignored (stay on the previous selection)."""
        try:
            if name is None:
                self._pinned = None
                return
            if name in self.interfaces():
                self._pinned = name
        except Exception:
            pass

    def _read_one(self, hc):
        try:
            val = _PDH_FMT_COUNTERVALUE()
            rc = ctypes.windll.pdh.PdhGetFormattedCounterValue(
                hc, _PDH_FMT_DOUBLE, None, ctypes.byref(val))
            if rc == 0 and val.CStatus == 0:
                return max(0.0, float(val.doubleValue))
        except Exception:
            pass
        return None

    def sample(self):
        out = {"down_bps": None, "up_bps": None, "total_mbps": None,
               "iface": None, "link_mbps": None}
        if not self._ok or self._hquery is None:
            return out
        try:
            if ctypes.windll.pdh.PdhCollectQueryData(self._hquery) != 0:
                return out
            per = {}
            for name in self.interfaces():
                rx = (self._read_one(self._rx[name])
                      if name in self._rx else None)
                tx = (self._read_one(self._tx[name])
                      if name in self._tx else None)
                if rx is None and tx is None:
                    continue
                per[name] = ((rx or 0.0), (tx or 0.0))
            if not per:
                return out
            if self._pinned in per:
                name = self._pinned
            else:
                name = max(per, key=lambda k: per[k][0] + per[k][1])
            rx, tx = per[name]
            total = rx + tx
            if total > self._max_seen_bps:
                self._max_seen_bps = total
            link = self._read_one(self._bw[name]) if name in self._bw else None
            out["down_bps"] = rx
            out["up_bps"] = tx
            out["total_mbps"] = total * 8.0 / 1_000_000.0
            out["iface"] = name
            if link and link > 0:
                out["link_mbps"] = link / 1_000_000.0
            return out
        except Exception:
            return out

    @property
    def max_seen_mbps(self):
        """Peak total throughput observed (for auto-scaled bars)."""
        try:
            return self._max_seen_bps * 8.0 / 1_000_000.0
        except Exception:
            return 0.0

    def close(self):
        try:
            if self._hquery is not None:
                ctypes.windll.pdh.PdhCloseQuery(self._hquery)
        except Exception:
            pass
        self._hquery = None
        self._rx = {}
        self._tx = {}
        self._bw = {}
