"""
PC Specs engine (Tools tab, PC Specs card) — Tk-free, read-only.

Speccy-style plain-language facts, nothing guessed: OS/CPU/RAM/board
from the registry, RAM sticks from the SMBIOS table, GPUs from the
display-class keys, drives from GetDiskFreeSpaceEx, audio from the
existing endpoint list, network names from the environment + the DNS
module. Anything unreadable comes back "Unknown" (the dialog says so
instead of inventing numbers). Temperatures are deliberately absent —
they need a kernel driver stdlib code cannot honestly read.

Never raises (each getter fails closed to Unknown/empty).
"""

from __future__ import annotations

import os
import platform

# Warm engine imports up front (not lazily per-tab): on a healthy box
# this is just speed, but lazy pyc compilation mid-session trips over
# heap damage from flaky display drivers on broken boxes — and the
# frozen exe never compiles at runtime anyway. No cycles: neither
# module imports app code at top level... except audio_out/dns_test
# import nothing from app, so this is safe.
try:
    from app.audio_out import list_render_endpoints as _audio_eps
except Exception:
    _audio_eps = None
try:
    from app.dns_test import get_current_dns as _dns_now
except Exception:
    _dns_now = None


def _reg_str(hive, path, name):
    try:
        import winreg as _wr
        roots = {"HKLM": _wr.HKEY_LOCAL_MACHINE, "HKCU": _wr.HKEY_CURRENT_USER}
        with _wr.OpenKey(roots[hive], path, 0, _wr.KEY_READ) as key:
            try:
                val, _typ = _wr.QueryValueEx(key, name)
            except OSError:
                return ""
            return str(val) if val is not None else ""
    except Exception:
        return ""


def _reg_dword(hive, path, name):
    try:
        import winreg as _wr
        roots = {"HKLM": _wr.HKEY_LOCAL_MACHINE}
        with _wr.OpenKey(roots[hive], path, 0, _wr.KEY_READ) as key:
            try:
                val, _typ = _wr.QueryValueEx(key, name)
            except OSError:
                return None
            return int(val)
    except Exception:
        return None


def _gb(nbytes):
    try:
        return f"{float(nbytes) / (1024 ** 3):.1f}GB"
    except Exception:
        return "?"


# Known OEM placeholder junk that boards/BIOSes ship with when the
# builder never filled in the real SMBIOS strings — showing this text
# is functionally the same as "Unknown" but looks like a real (wrong)
# answer, so it's treated as blank rather than displayed.
_PLACEHOLDER_STRINGS = {
    "to be filled by o.e.m.", "to be filled by o.e.m", "default string",
    "system manufacturer", "system product name", "system version",
    "not applicable", "not specified", "o.e.m.", "oem", "unknown",
    "none", "n/a", "na", ""}


def _is_placeholder(s):
    return (s or "").strip().lower() in _PLACEHOLDER_STRINGS


def _ps_query(class_name, props, timeout=4.0):
    """Best-effort Get-CimInstance fallback for facts the registry
    doesn't reliably carry (board on many laptops, GPU on some driver
    models, CPU name/cores on stripped installs). Returns a list of
    dicts, or [] on any failure — this is a fallback path, never the
    only path, so a slow/blocked PowerShell must never raise or hang
    the dialog. Uses Get-CimInstance (works on Windows 10/11 and both
    LTSC releases; the legacy Get-WmiObject cmdlet is what's actually
    missing on some stripped images, not CIM)."""
    try:
        import subprocess as _sp
        import json as _json
        prop_list = ",".join(props)
        cmd = (
            f"Get-CimInstance -ClassName {class_name} -ErrorAction Stop "
            f"| Select-Object {prop_list} | ConvertTo-Json -Compress"
        )
        creationflags = getattr(_sp, "CREATE_NO_WINDOW", 0)
        proc = _sp.run(
            ["powershell", "-NoProfile", "-NonInteractive",
             "-ExecutionPolicy", "Bypass", "-Command", cmd],
            capture_output=True, text=True, timeout=timeout,
            creationflags=creationflags)
        raw = (proc.stdout or "").strip()
        if not raw:
            return []
        data = _json.loads(raw)
        if isinstance(data, dict):
            data = [data]
        return data if isinstance(data, list) else []
    except Exception:
        return []


def os_info():
    """(caption, build_line) e.g. ('Windows 11 Pro 64-bit', '24H2 · build 26100.1')."""
    try:
        base = "HKLM\\SOFTWARE\\Microsoft\\Windows NT\\CurrentVersion"
        name = _reg_str("HKLM", base, "ProductName")
        if not name:
            # PC Specs audit fix: LTSC/IoT editions have been seen with an
            # unreadable ProductName value, showing the generic "Windows"
            # fallback instead of the real edition. Win32_OperatingSystem's
            # Caption carries the same string through WMI — a completely
            # separate path from the registry read above — so try it
            # before giving up.
            rows = _ps_query("Win32_OperatingSystem", ["Caption"])
            if rows:
                name = str(rows[0].get("Caption") or "").strip()
                # WMI captions come back "Microsoft Windows 11 ..." — trim
                # the vendor prefix to match the plain style used here.
                if name.lower().startswith("microsoft "):
                    name = name[len("Microsoft "):]
        name = name or platform.system()
        arch = platform.machine() or ""
        bits = "64-bit" if "64" in arch else ("32-bit" if arch else "")
        caption = f"{name} {bits}".strip()
        disp = _reg_str("HKLM", base, "DisplayVersion")
        build = _reg_str("HKLM", base, "CurrentBuildNumber")
        ubr = _reg_dword("HKLM", base, "UBR")
        if not build:
            # PC Specs audit fix: seen returning "Unknown build" on a real
            # machine even though CurrentBuildNumber is normally reliable.
            # Win32_OperatingSystem carries the same data through WMI, a
            # completely separate path from the registry read above — try
            # it before giving up rather than assuming the registry is the
            # only source.
            rows = _ps_query("Win32_OperatingSystem", ["Version", "BuildNumber"])
            if rows:
                build = str(rows[0].get("BuildNumber") or "").strip()
        build_part = f"build {build}.{ubr}" if build and ubr is not None else (f"build {build}" if build else "")
        parts = [p for p in (disp, build_part) if p]
        return caption or "Windows", " · ".join(parts) or "Unknown build"
    except Exception:
        return "Windows", "Unknown build"


def cpu_info():
    """(name, detail) e.g. ('Intel Core i7-14700K', '3.40GHz · 8 cores / 20 threads')."""
    try:
        base = "HKLM\\HARDWARE\\DESCRIPTION\\System\\CentralProcessor\\0"
        name = _reg_str("HKLM", base, "ProcessorNameString").strip()
        mhz = _reg_dword("HKLM", base, "~MHz")
        threads = 0
        try:
            threads = int(os.cpu_count() or 0)
        except Exception:
            threads = 0
        cores = 0
        # Registry only ever gives logical processors (threads); ask
        # CIM for the real physical core count so hyperthreaded/SMT
        # chips don't get mislabeled (an 8-core/16-thread CPU used to
        # just say "16 cores", which is wrong, not just incomplete).
        if not name or not mhz or not threads:
            rows = _ps_query("Win32_Processor",
                              ["Name", "MaxClockSpeed", "NumberOfCores",
                               "NumberOfLogicalProcessors"])
            if rows:
                row = rows[0]
                name = name or str(row.get("Name") or "").strip()
                if not mhz:
                    mhz = row.get("MaxClockSpeed")
                    mhz = int(mhz) if mhz else None
                cores = int(row.get("NumberOfCores") or 0)
                if not threads:
                    threads = int(row.get("NumberOfLogicalProcessors") or 0)
        else:
            rows = _ps_query("Win32_Processor", ["NumberOfCores"])
            if rows:
                cores = int(rows[0].get("NumberOfCores") or 0)
        bits = []
        if mhz:
            bits.append(f"{mhz / 1000.0:.2f}GHz")
        if cores and threads and cores != threads:
            bits.append(f"{cores} cores / {threads} threads")
        elif threads:
            bits.append(f"{threads} threads")
        return name or "Unknown CPU", " · ".join(bits) or "Unknown speed"
    except Exception:
        return "Unknown CPU", "Unknown speed"


def mem_totals():
    """(total_bytes, avail_bytes) via GlobalMemoryStatusEx. (0, 0) on failure."""
    try:
        import ctypes as _ct

        class _MS(_ct.Structure):
            _fields_ = [("dwLength", _ct.c_ulong),
                        ("dwMemoryLoad", _ct.c_ulong),
                        ("ullTotalPhys", _ct.c_ulonglong),
                        ("ullAvailPhys", _ct.c_ulonglong),
                        ("ullTotalPageFile", _ct.c_ulonglong),
                        ("ullAvailPageFile", _ct.c_ulonglong),
                        ("ullTotalVirtual", _ct.c_ulonglong),
                        ("ullAvailVirtual", _ct.c_ulonglong),
                        ("ullAvailExtendedVirtual", _ct.c_ulonglong)]

        st = _MS()
        st.dwLength = _ct.sizeof(_MS)
        if _ct.windll.kernel32.GlobalMemoryStatusEx(_ct.byref(st)) == 0:
            return 0, 0
        return int(st.ullTotalPhys), int(st.ullAvailPhys)
    except Exception:
        return 0, 0


def _smbios_table():
    """Raw SMBIOS bytes via GetSystemFirmwareTable('RSMB'), or b""."""
    try:
        import ctypes as _ct
        _RSMB = 0x52534D42
        k32 = _ct.windll.kernel32
        need = k32.GetSystemFirmwareTable(_RSMB, 0, None, 0)
        if not need or need > (8 << 20):
            return b""
        buf = _ct.create_string_buffer(int(need))
        got = k32.GetSystemFirmwareTable(_RSMB, 0, buf, int(need))
        if not got:
            return b""
        return bytes(buf.raw[:int(got)])
    except Exception:
        return b""


def ram_sticks():
    """[(size_mb, speed_mts, locator)] populated DDR sticks. [] on failure."""
    out = []
    try:
        data = _smbios_table()
        # CT-004 audit fix: GetSystemFirmwareTable('RSMB') returns a
        # RawSMBIOSData wrapper, NOT the SMBIOS structure table directly —
        # an 8-byte header (Used20CallingMethod, SMBIOSMajorVersion,
        # SMBIOSMinorVersion, DmiRevision, then a 4-byte Length) comes
        # before the actual table data. Starting the parse at offset 0
        # instead of 8 read the header's own bytes as a fake structure
        # (type=Used20CallingMethod, length=SMBIOSMajorVersion, which is
        # always 2 or 3 and so always < 4) and broke out of the loop
        # immediately — ram_sticks() returned [] on every machine.
        n = len(data)
        pos = 8
        if n < pos + 4:
            return []
        while pos + 4 <= n:
            _typ = data[pos]
            _ln = data[pos + 1]
            if _ln < 4 or pos + _ln > n:
                break
            if _typ == 17 and _ln >= 28:
                size_raw = int.from_bytes(data[pos + 12:pos + 14], "little")
                speed = int.from_bytes(data[pos + 21:pos + 23], "little")
                # Configured Memory Speed (offset 0x20, a WORD) reflects
                # what's actually running (post-XMP/DOCP); the plain Speed
                # field above is only the module's rated maximum. Prefer
                # the configured value when the structure carries it.
                # (Second audit fix alongside CT-004: this used to read
                # 0x1E:0x20, which is the tail of the adjacent Extended
                # Size DWORD field at 0x1C, not Configured Memory Speed —
                # confirmed against the SMBIOS Type 17 field layout.)
                if _ln >= 0x22:
                    cfg = int.from_bytes(data[pos + 0x20:pos + 0x22], "little")
                    if cfg not in (0, 0xFFFF):
                        speed = cfg
                if size_raw not in (0, 0xFFFF, 0x7FFF):
                    if size_raw & 0x8000:
                        size_mb = size_raw & 0x7FFF  # KB units -> MB
                        size_mb = max(1, size_mb // 1024)
                    else:
                        size_mb = size_raw
                    if size_mb > 0:
                        out.append((size_mb, speed or 0, ""))
            # skip the string set (double-null terminated)
            end = pos + _ln
            while end + 1 < n and not (data[end] == 0 and data[end + 1] == 0):
                end += 1
            pos = end + 2
    except Exception:
        return []
    return out


def ram_info():
    """('16.0GB total (2 sticks @ 3200MT/s), 9.1GB free', ...). Honest fallbacks."""
    try:
        total, avail = mem_totals()
        sticks = ram_sticks()
        if total <= 0:
            return "Unknown RAM", ""
        head = _gb(total) + " total"
        if sticks:
            speeds = sorted({s for _, s, _ in sticks if s})
            head += f" ({len(sticks)} stick{'s' if len(sticks) != 1 else ''}"
            head += f" @ {speeds[0]}MT/s" if speeds else ""
            head += ")"
        detail = f"{_gb(avail)} free" if avail > 0 else ""
        return head, detail
    except Exception:
        return "Unknown RAM", ""


def board_info():
    """('Micro-Star B650 TOMAHAWK', 'BIOS 1.20'). Falls back to CIM,
    then to the computer's own manufacturer/model (common on laptops,
    which often leave BaseBoard* blank but always fill in the system
    identity), before finally saying Unknown."""
    try:
        base = "HKLM\\HARDWARE\\DESCRIPTION\\System\\BIOS"
        maker = _reg_str("HKLM", base, "BaseBoardManufacturer").strip()
        product = _reg_str("HKLM", base, "BaseBoardProduct").strip()
        ver = _reg_str("HKLM", base, "BIOSVersion").strip()
        if ver.startswith("[") and ver.endswith("]"):
            ver = ver[1:-1].split(",")[0].strip()
        if _is_placeholder(ver):
            ver = ""
        if not ver:
            # PC Specs audit fix: BIOSVersion under this registry key comes
            # back empty on some real machines (seen: a mini-PC/NUC-class
            # system) even though the BIOS obviously has a version string.
            # Win32_BIOS carries it through WMI — same fallback pattern
            # already used above for maker/product on this function.
            rows = _ps_query("Win32_BIOS", ["SMBIOSBIOSVersion"])
            if rows:
                v2 = str(rows[0].get("SMBIOSBIOSVersion") or "").strip()
                if v2 and not _is_placeholder(v2):
                    ver = v2
        if _is_placeholder(maker):
            maker = ""
        if _is_placeholder(product):
            product = ""
        if not maker or not product:
            rows = _ps_query("Win32_BaseBoard", ["Manufacturer", "Product"])
            if rows:
                m2 = str(rows[0].get("Manufacturer") or "").strip()
                p2 = str(rows[0].get("Product") or "").strip()
                if not maker and not _is_placeholder(m2):
                    maker = m2
                if not product and not _is_placeholder(p2):
                    product = p2
        name = f"{maker} {product}".strip()
        if not name:
            # Laptops in particular: the chassis identity is more
            # useful (and far more often populated) than a blank
            # baseboard.
            rows = _ps_query("Win32_ComputerSystem", ["Manufacturer", "Model"])
            if rows:
                m2 = str(rows[0].get("Manufacturer") or "").strip()
                p2 = str(rows[0].get("Model") or "").strip()
                if not _is_placeholder(m2) or not _is_placeholder(p2):
                    name = " ".join(x for x in (m2, p2)
                                     if x and not _is_placeholder(x)).strip()
        return name or "Unknown motherboard", f"BIOS {ver}" if ver else ""
    except Exception:
        return "Unknown motherboard", ""


def gpu_info():
    """[names] of real display adapters (filters the Basic Display stub).
    Falls back to CIM when the registry class keys come back empty —
    some driver models (and a few hybrid-GPU laptops) don't populate
    DriverDesc where this code expects it."""
    names = []
    try:
        import winreg as _wr
        cls = "SYSTEM\\CurrentControlSet\\Control\\Class" \
              "\\{4d36e968-e325-11ce-bfc1-08002be10318}"
        with _wr.OpenKey(_wr.HKEY_LOCAL_MACHINE, cls, 0, _wr.KEY_READ) as root:
            i = 0
            while True:
                try:
                    sub = _wr.EnumKey(root, i)
                except OSError:
                    break
                i += 1
                if not sub.isdigit():
                    continue
                try:
                    with _wr.OpenKey(root, sub, 0, _wr.KEY_READ) as key:
                        try:
                            desc, _t = _wr.QueryValueEx(key, "DriverDesc")
                        except OSError:
                            continue
                        desc = str(desc or "").strip()
                        if not desc or "Basic Display" in desc:
                            continue
                        if desc not in names:
                            names.append(desc)
                except OSError:
                    continue
    except Exception:
        pass
    if not names:
        try:
            rows = _ps_query("Win32_VideoController", ["Name"])
            for row in rows:
                desc = str(row.get("Name") or "").strip()
                if desc and "Basic Display" not in desc and "Basic Render" not in desc \
                        and desc not in names:
                    names.append(desc)
        except Exception:
            pass
    return names


def storage_info():
    """[{drive, label, fs, total, free}] fixed local disks. Never raises."""
    out = []
    try:
        import ctypes as _ct
        from ctypes import wintypes as _wt
        k32 = _ct.windll.kernel32
        for letter in "CDEFGHIJKLMNOPQRSTUVWXYZ":
            root = letter + ":\\"
            try:
                if k32.GetDriveTypeW(_ct.c_wchar_p(root)) != 3:  # DRIVE_FIXED
                    continue
                free = _ct.c_ulonglong(0)
                total = _ct.c_ulonglong(0)
                if k32.GetDiskFreeSpaceExW(
                        _ct.c_wchar_p(root), None,
                        _ct.byref(total), _ct.byref(free)) == 0:
                    continue
                _vol = _ct.create_unicode_buffer(261)
                _fs = _ct.create_unicode_buffer(261)
                label, fs = "", ""
                try:
                    if k32.GetVolumeInformationW(
                            _ct.c_wchar_p(root), _vol, 261, None, None, None,
                            _fs, 261) != 0:
                        label, fs = _vol.value or "", _fs.value or ""
                except Exception:
                    pass
                out.append({"drive": letter + ":", "label": label, "fs": fs,
                            "total": int(total.value),
                            "free": int(free.value)})
            except Exception:
                continue
    except Exception:
        pass
    return out


def audio_info():
    """[render endpoint names] via the existing endpoint list. Never raises."""
    try:
        fn = _audio_eps
        if fn is None:
            from app.audio_out import list_render_endpoints as _late
            fn = _late
        return [(e or {}).get("name", "") for e in (fn() or [])
                if (e or {}).get("state") == 1 and (e or {}).get("name")]
    except Exception:
        return []


def net_info():
    """(computer_name, dns_line). Never raises."""
    try:
        name = os.environ.get("COMPUTERNAME", "") or platform.node() or "This PC"
    except Exception:
        name = "This PC"
    dns = ""
    try:
        fn = _dns_now
        if fn is None:
            from app.dns_test import get_current_dns as _late_dns
            fn = _late_dns
        if callable(fn):
            servers = fn() or []
            dns = ", ".join([s for s in servers if s][:2])
    except Exception:
        dns = ""
    return name, (f"DNS {dns}" if dns else "DNS unknown")


def summary_lines():
    """One plain line per section for Summary + Copy. Never raises."""
    lines = []
    try:
        cap, build = os_info()
        lines.append(f"OS: {cap}" + (f" ({build})" if build else ""))
    except Exception:
        pass
    try:
        name, detail = cpu_info()
        lines.append(f"CPU: {name}" + (f" ({detail})" if detail else ""))
    except Exception:
        pass
    try:
        head, detail = ram_info()
        lines.append(f"RAM: {head}" + (f" — {detail}" if detail else ""))
    except Exception:
        pass
    try:
        name, detail = board_info()
        lines.append(f"Board: {name}" + (f" ({detail})" if detail else ""))
    except Exception:
        pass
    try:
        gpus = gpu_info()
        lines.append("GPU: " + (" + ".join(gpus) if gpus else "Unknown"))
    except Exception:
        pass
    try:
        disks = storage_info()
        parts = [f"{d['drive']} {_gb(d['free'])} free of {_gb(d['total'])}"
                 for d in disks]
        lines.append("Drives: " + (" · ".join(parts) if parts else "Unknown"))
    except Exception:
        pass
    return lines


def full_report_lines():
    """Every tab as plain lines for support tickets (Summary + 8 sections).

    Mirrors what PCSpecsDialog._show renders, but as text — same getters,
    same Unknown fallbacks, same never-raises contract. Display-now comes
    from the same tweak_tasks query the Graphics tab uses (lazy import,
    optional). Never raises; [] only if nothing at all was readable."""
    lines = []
    try:
        import time as _time
        lines.append("Cleaner Tool — PC Specs (full report)")
        try:
            lines.append("Generated: " + _time.strftime("%Y-%m-%d %H:%M:%S"))
        except Exception:
            pass
        lines.append("")
        for _s in (summary_lines() or []):
            lines.append(_s)
        lines.append("")
        try:
            cap, build = os_info()
            lines.append("[OS]")
            lines.append(f"Operating System: {cap or 'Unknown'}")
            lines.append(f"Version: {build or 'Unknown'}")
            lines.append("")
        except Exception:
            pass
        try:
            name, detail = cpu_info()
            lines.append("[CPU]")
            lines.append(f"Processor: {name or 'Unknown'}")
            lines.append(f"Speed / Cores: {detail or 'Unknown'}")
            lines.append("")
        except Exception:
            pass
        try:
            head, detail = ram_info()
            lines.append("[RAM]")
            lines.append(f"Memory: {head or 'Unknown'}")
            lines.append(f"Available: {detail or 'Unknown'}")
            lines.append("")
        except Exception:
            pass
        try:
            name, detail = board_info()
            lines.append("[Board]")
            lines.append(f"Motherboard: {name or 'Unknown'}")
            lines.append(f"Firmware: {detail or 'Unknown'}")
            lines.append("")
        except Exception:
            pass
        try:
            gpus = gpu_info() or []
            lines.append("[Graphics]")
            if gpus:
                for i, g in enumerate(gpus):
                    lines.append(f"GPU {i + 1}: {g}")
            else:
                lines.append("Graphics: Unknown")
            try:
                from app.tasks.tweak_tasks import _query_video_mode_list
                cur, _mx = _query_video_mode_list()
                if cur and cur[0] and cur[1]:
                    lines.append(f"Display now: {cur[0]}x{cur[1]} @ {cur[2]}Hz")
            except Exception:
                pass
            lines.append("")
        except Exception:
            pass
        try:
            disks = storage_info() or []
            lines.append("[Storage]")
            if disks:
                for d in disks:
                    try:
                        label = f"{d.get('drive', '')} {d.get('label', '')}".strip()
                        fs = f" · {d['fs']}" if d.get("fs") else ""
                        lines.append(f"{label or 'Drive'}: "
                                     f"{_gb(d.get('free', 0))} free of "
                                     f"{_gb(d.get('total', 0))}{fs}")
                    except Exception:
                        continue
            else:
                lines.append("Drives: Unknown")
            lines.append("")
        except Exception:
            pass
        try:
            names = audio_info() or []
            lines.append("[Audio]")
            if names:
                for i, n in enumerate(names):
                    lines.append(f"Output {i + 1}: {n}")
            else:
                lines.append("Audio: Unknown")
            lines.append("")
        except Exception:
            pass
        try:
            name, dns = net_info()
            lines.append("[Network]")
            lines.append(f"Computer: {name or 'Unknown'}")
            lines.append(f"Network: {dns or 'Unknown'}")
        except Exception:
            pass
    except Exception:
        pass
    return lines


def full_report_text():
    """full_report_lines() joined for clipboard/file. Never raises."""
    try:
        return "\n".join(full_report_lines())
    except Exception:
        return ""
