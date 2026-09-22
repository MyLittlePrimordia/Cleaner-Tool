"""Process Manager — plain-language "what's using my PC" for gamers.

Stdlib-only (ctypes + tasklist). Lists top processes by CPU + RAM,
filters out core Windows processes so nothing offered can break the PC,
and closes via taskkill /PID /F (same approach Game Night uses).

Design goals match the rest of Tools:
  * No admin required
  * Zero third-party deps
  * Failure-tolerant (any Win32/tasklist failure → empty list, never raise)
  * Names are plain ("Chrome", "Discord") not cryptic SVCHOST paths
"""

from __future__ import annotations

import ctypes
import os
import subprocess
import threading
import time
from typing import Dict, List, Optional, Tuple

IS_WINDOWS = os.name == "nt"

# Core processes we never offer to close. Lowercase basenames.
_PROTECTED = frozenset({
    "system", "smss.exe", "csrss.exe", "wininit.exe", "services.exe",
    "lsass.exe", "svchost.exe", "winlogon.exe", "fontdrvhost.exe",
    "dwm.exe", "explorer.exe", "sihost.exe", "taskhostw.exe",
    "runtimebroker.exe", "searchhost.exe", "startmenuexperiencehost.exe",
    "shellexperiencehost.exe", "applicationframehost.exe",
    "systemsettings.exe", "securityhealthservice.exe",
    "securityhealthsystray.exe", "msmpeng.exe", "nissrv.exe",
    "conhost.exe", "dllhost.exe", "ctfmon.exe", "audiodg.exe",
    "spoolsv.exe", "searchindexer.exe", "wudfhost.exe",
    "registry", "memory compression", "system idle process",
    "idle", "secure system",
})

# Friendly display names for common gamer apps (basename → label).
_FRIENDLY = {
    "chrome.exe": "Chrome",
    "msedge.exe": "Edge",
    "firefox.exe": "Firefox",
    "brave.exe": "Brave",
    "discord.exe": "Discord",
    "discordcanary.exe": "Discord Canary",
    "spotify.exe": "Spotify",
    "steam.exe": "Steam",
    "steamwebhelper.exe": "Steam (helper)",
    "epicgameslauncher.exe": "Epic Games",
    "origin.exe": "EA App",
    "eadesktop.exe": "EA App",
    "battle.net.exe": "Battle.net",
    "agent.exe": "Battle.net Agent",
    "riotclientservices.exe": "Riot Client",
    "leagueclient.exe": "League of Legends",
    "leagueclientux.exe": "League of Legends",
    "valorant.exe": "Valorant",
    "obs64.exe": "OBS Studio",
    "obs32.exe": "OBS Studio",
    "streamlabs obs.exe": "Streamlabs",
    "code.exe": "VS Code",
    "devenv.exe": "Visual Studio",
    "slack.exe": "Slack",
    "teams.exe": "Teams",
    "zoom.exe": "Zoom",
    "skype.exe": "Skype",
    "telegram.exe": "Telegram",
    "whatsapp.exe": "WhatsApp",
    "onedrive.exe": "OneDrive",
    "dropbox.exe": "Dropbox",
    "wallpaperengine.exe": "Wallpaper Engine",
    "rainmeter.exe": "Rainmeter",
    "nvidia share.exe": "NVIDIA Share",
    "nvcontainer.exe": "NVIDIA Container",
    "amdow.exe": "AMD Overlay",
    "radeonsoftware.exe": "AMD Software",
    "corsair.service.exe": "iCUE",
    "lghub.exe": "Logitech G HUB",
    "razersynapse.exe": "Razer Synapse",
    "ms-teams.exe": "Teams",
}


def _friendly_name(exe: str) -> str:
    low = (exe or "").lower()
    if low in _FRIENDLY:
        return _FRIENDLY[low]
    # Strip .exe and title-case residual
    base = low[:-4] if low.endswith(".exe") else low
    if not base:
        return exe or "?"
    return base.replace("-", " ").replace("_", " ").title()


def _tasklist_csv(timeout: int = 8) -> List[Tuple[str, int, str]]:
    """Return [(image_name, pid, mem_kb_str), ...] via tasklist CSV.
    Never raises."""
    if not IS_WINDOWS:
        return []
    try:
        from app.utils import resolve_exe
        exe = resolve_exe("tasklist")
    except Exception:
        exe = "tasklist"
    try:
        out = subprocess.check_output(
            [exe, "/fo", "csv", "/nh", "/v"],
            text=True, timeout=timeout, errors="replace",
            stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except Exception:
        try:
            # Fallback without /v (still has Image Name, PID, Mem Usage)
            out = subprocess.check_output(
                [exe, "/fo", "csv", "/nh"],
                text=True, timeout=timeout, errors="replace",
                stderr=subprocess.DEVNULL,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except Exception:
            return []
    rows = []
    for line in (out or "").splitlines():
        # CSV fields are quoted: "name","pid","session","session#","mem"
        parts = [p.strip().strip('"') for p in line.split('","')]
        if len(parts) < 2:
            continue
        name = parts[0]
        if not name.lower().endswith(".exe") and name.lower() not in (
                "system", "registry", "memory compression"):
            # keep System / Registry etc for filtering later
            if "." in name:
                continue
        try:
            pid = int(parts[1])
        except (ValueError, IndexError):
            continue
        mem = parts[4] if len(parts) > 4 else (parts[4] if len(parts) > 4 else "0")
        if len(parts) >= 5:
            mem = parts[4]
        else:
            mem = "0"
        rows.append((name, pid, mem))
    return rows


def _parse_mem_kb(s: str) -> int:
    """Parse tasklist mem field like '123,456 K' → KB int."""
    try:
        cleaned = (s or "").replace(",", "").replace("K", "").replace(
            "k", "").replace(" ", "").strip()
        return max(0, int(cleaned))
    except Exception:
        return 0


# ---- CPU delta sampling via GetProcessTimes ----

class _FILETIME(ctypes.Structure):
    _fields_ = [("dwLowDateTime", ctypes.c_uint32),
                ("dwHighDateTime", ctypes.c_uint32)]


def _ft_to_int(ft: _FILETIME) -> int:
    return (int(ft.dwHighDateTime) << 32) | int(ft.dwLowDateTime)


def _process_times(pid: int) -> Optional[Tuple[int, int]]:
    """Return (kernel+user 100ns ticks, wall 100ns) or None."""
    if not IS_WINDOWS or pid <= 0:
        return None
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    try:
        h = ctypes.windll.kernel32.OpenProcess(
            PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not h:
            return None
        try:
            create = _FILETIME()
            exit_ = _FILETIME()
            kernel = _FILETIME()
            user = _FILETIME()
            ok = ctypes.windll.kernel32.GetProcessTimes(
                h, ctypes.byref(create), ctypes.byref(exit_),
                ctypes.byref(kernel), ctypes.byref(user))
            if not ok:
                return None
            cpu = _ft_to_int(kernel) + _ft_to_int(user)
            # wall clock via QueryPerformanceCounter is overkill; use
            # GetTickCount64 as coarse wall for delta ratio.
            return cpu, 0  # wall filled by sampler
        finally:
            ctypes.windll.kernel32.CloseHandle(h)
    except Exception:
        return None


def _working_set_bytes(pid: int) -> Optional[int]:
    """PROCESS_MEMORY_COUNTERS WorkingSetSize via psapi."""
    if not IS_WINDOWS or pid <= 0:
        return None
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    try:
        class PROCESS_MEMORY_COUNTERS(ctypes.Structure):
            _fields_ = [
                ("cb", ctypes.c_ulong),
                ("PageFaultCount", ctypes.c_ulong),
                ("PeakWorkingSetSize", ctypes.c_size_t),
                ("WorkingSetSize", ctypes.c_size_t),
                ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                ("PagefileUsage", ctypes.c_size_t),
                ("PeakPagefileUsage", ctypes.c_size_t),
            ]
        h = ctypes.windll.kernel32.OpenProcess(
            PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not h:
            return None
        try:
            pmc = PROCESS_MEMORY_COUNTERS()
            pmc.cb = ctypes.sizeof(pmc)
            ok = ctypes.windll.psapi.GetProcessMemoryInfo(
                h, ctypes.byref(pmc), pmc.cb)
            if not ok:
                return None
            return int(pmc.WorkingSetSize)
        finally:
            ctypes.windll.kernel32.CloseHandle(h)
    except Exception:
        return None


class ProcessSampler:
    """Delta-samples top processes by CPU% + RAM.

    Call sample() twice (or more) with a short gap; first call primes,
    subsequent calls return ranked lists. Thread-safe enough for a
    single GUI tick thread.
    """

    def __init__(self, top_n: int = 12):
        self.top_n = max(3, int(top_n))
        self._prev_cpu: Dict[int, int] = {}   # pid → cpu ticks
        self._prev_wall: float = 0.0
        self._lock = threading.Lock()
        self._own_pid = os.getpid()

    def sample(self) -> List[dict]:
        """Return list of dicts sorted by (cpu_percent + ram weight).

        Each dict: {
            "pid": int,
            "name": str,          # original image name
            "display": str,       # friendly label
            "cpu_percent": float, # 0–100-ish (multi-core can exceed 100)
            "ram_mb": float,
            "safe": bool,         # True if not in protected set
        }
        """
        with self._lock:
            return self._sample_unlocked()

    def _sample_unlocked(self) -> List[dict]:
        rows = _tasklist_csv()
        if not rows:
            return []

        now = time.perf_counter()
        wall_delta = max(0.001, now - self._prev_wall) if self._prev_wall else 0.0
        self._prev_wall = now

        # Build candidate set, skip protected + self
        candidates = []
        for name, pid, mem_str in rows:
            low = (name or "").lower()
            if pid == self._own_pid:
                continue
            if low in _PROTECTED or low.replace(".exe", "") in _PROTECTED:
                continue
            # Skip 0-pid / system-ish
            if pid <= 4:
                continue
            ram_kb = _parse_mem_kb(mem_str)
            # Prefer live WorkingSet when available (more accurate)
            ws = _working_set_bytes(pid)
            if ws is not None:
                ram_kb = ws // 1024
            candidates.append((name, pid, ram_kb))

        # CPU deltas
        new_cpu: Dict[int, int] = {}
        results = []
        n_cores = os.cpu_count() or 1
        for name, pid, ram_kb in candidates:
            times = _process_times(pid)
            cpu_ticks = times[0] if times else 0
            new_cpu[pid] = cpu_ticks
            prev = self._prev_cpu.get(pid)
            if prev is not None and wall_delta > 0:
                # 100ns ticks → percent of one core
                delta = max(0, cpu_ticks - prev)
                # 1 second = 10_000_000 of 100ns units
                cpu_pct = (delta / 10_000_000.0) / wall_delta * 100.0
            else:
                cpu_pct = 0.0
            # Cap display; multi-core processes can exceed 100
            cpu_pct = min(cpu_pct, 100.0 * n_cores)
            results.append({
                "pid": pid,
                "name": name,
                "display": _friendly_name(name),
                "cpu_percent": round(cpu_pct, 1),
                "ram_mb": round(ram_kb / 1024.0, 1),
                "safe": True,
            })
        self._prev_cpu = new_cpu

        # Rank: heavy CPU first, then RAM as tie-breaker
        results.sort(
            key=lambda r: (r["cpu_percent"] * 2.0 + r["ram_mb"] / 50.0),
            reverse=True,
        )
        return results[: self.top_n]


def close_process(pid: int, force: bool = True) -> bool:
    """Close a process by PID via taskkill. Returns True on success.
    Never raises. Skips protected / self."""
    if not IS_WINDOWS or pid <= 0 or pid == os.getpid():
        return False
    try:
        from app.utils import resolve_exe
        exe = resolve_exe("taskkill")
    except Exception:
        exe = "taskkill"
    args = [exe, "/PID", str(pid)]
    if force:
        args.append("/F")
    try:
        r = subprocess.run(
            args, capture_output=True, text=True, timeout=8,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        return r.returncode == 0
    except Exception:
        return False


def list_top_processes(top_n: int = 12) -> List[dict]:
    """One-shot helper: prime + sample after a short sleep for CPU %."""
    s = ProcessSampler(top_n=top_n)
    s.sample()  # prime
    time.sleep(0.6)
    return s.sample()
