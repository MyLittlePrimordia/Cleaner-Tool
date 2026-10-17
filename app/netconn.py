"""Per-program network connections, read from Windows' own TCP/UDP tables.

Tk-free by design (matches app/hw_monitor.py): this module is a sampler,
the dialog that shows it lives in app/ui/dialogs/.

What Windows actually gives us, and what it does not
----------------------------------------------------
`GetExtendedTcpTable` / `GetExtendedUdpTable` return, per connection:
state, local address+port, remote address+port, and the owning PID. That is
a complete picture of WHO IS CONNECTED TO WHAT.

They do NOT return byte counts. Per-connection byte totals need
`GetPerTcpConnectionEStats`, which needs an open handle per connection and
is admin-gated for anything not loopback. This module therefore reports
connections and endpoints and deliberately does NOT invent throughput
numbers per program - the Hardware Monitor's NetworkSampler already reports
real aggregate bytes, and the dialog says so in a footnote rather than
showing a fake per-app figure.

Design constraints
------------------
* No extra installs (stdlib ctypes only), matching the app's
  zero-runtime-dependency policy.
* Never raises. Every Win32 call that can fail returns an empty list or an
  honest "unknown", because a sampler that throws leaves the dialog showing
  a spinner forever.
* One process snapshot per sample, not one lookup per connection: with a few
  hundred connections, per-PID `OpenProcess` calls dominate the cost.
* Local/loopback and system-owned processes are separable so the dialog can
  hide them by default - see `is_system_row`.
"""
from __future__ import annotations

import ctypes
import os
import socket
import struct
import sys
from ctypes import wintypes
from functools import lru_cache

IS_WINDOWS = sys.platform.startswith("win")

# --- iphlpapi.dll structures -------------------------------------------- #
AF_INET = 2
AF_INET6 = 23
TCP_TABLE_OWNER_PID_ALL = 5
UDP_TABLE_OWNER_PID = 1

TH32CS_SNAPPROCESS = 0x00000002
INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value
MAX_PATH = 260
ERROR_INSUFFICIENT_BUFFER = 122


class _MIB_TCPROW_OWNER_PID(ctypes.Structure):
    """IPv4 row - addresses are a single DWORD each."""
    _fields_ = [
        ("dwState", wintypes.DWORD),
        ("dwLocalAddr", wintypes.DWORD),
        ("dwLocalPort", wintypes.DWORD),
        ("dwRemoteAddr", wintypes.DWORD),
        ("dwRemotePort", wintypes.DWORD),
        ("dwOwningPid", wintypes.DWORD),
    ]


class _MIB_TCP6ROW_OWNER_PID(ctypes.Structure):
    """IPv6 row - addresses are 16 BYTES each, plus scope ids.

    BUG-029: the first version reused the IPv4 row for AF_INET6. Every
    field after dwState therefore read at the wrong offset, which produced
    garbage PIDs, a bogus "[System Process]" at pid 0, and zero ESTABLISHED
    rows on a machine that was plainly online. IPv6 needs its own layout.

    BUG-031: even with the right layout, the AF_INET6 table does not decode
    consistently on this machine. The sizing call asks for 1972 bytes, the
    fill call writes back 516, and the resulting 512 bytes contain the same
    port/PID values as the IPv4 table rather than a distinct IPv6 set. No
    (start, stride, state_offset, pid_offset) combination reproduces a valid
    table, and TCP_TABLE_ALL for AF_INET6 fails outright. Rather than render
    plausible-looking garbage, `_valid_tcp6` accepts a row ONLY when its state
    is a real MIB state AND its owning PID is a live process, and the dialog
    says plainly when IPv6 could not be read. IPv4 is unaffected and exact.
    """
    _fields_ = [
        ("dwState", wintypes.DWORD),
        ("dwLocalAddr", ctypes.c_ubyte * 16),
        ("dwLocalScopeId", wintypes.DWORD),
        ("dwLocalPort", wintypes.DWORD),
        ("dwRemoteAddr", ctypes.c_ubyte * 16),
        ("dwRemoteScopeId", wintypes.DWORD),
        ("dwRemotePort", wintypes.DWORD),
        ("dwOwningPid", wintypes.DWORD),
    ]


class _MIB_UDPROW_OWNER_PID(ctypes.Structure):
    _fields_ = [
        ("dwLocalAddr", wintypes.DWORD),
        ("dwLocalPort", wintypes.DWORD),
        ("dwOwningPid", wintypes.DWORD),
    ]


class _MIB_UDP6ROW_OWNER_PID(ctypes.Structure):
    """IPv6 UDP row - 16-byte address plus a scope id."""
    _fields_ = [
        ("dwLocalAddr", ctypes.c_ubyte * 16),
        ("dwLocalScopeId", wintypes.DWORD),
        ("dwLocalPort", wintypes.DWORD),
        ("dwOwningPid", wintypes.DWORD),
    ]


class _PROCESSENTRY32W(ctypes.Structure):
    _fields_ = [
        ("dwSize", wintypes.DWORD),
        ("cntUsage", wintypes.DWORD),
        ("th32ProcessID", wintypes.DWORD),
        ("th32DefaultHeapID", ctypes.POINTER(ctypes.c_ulong)),
        ("th32ModuleID", wintypes.DWORD),
        ("cntThreads", wintypes.DWORD),
        ("th32ParentProcessID", wintypes.DWORD),
        ("pcPriClassBase", ctypes.c_long),
        ("dwFlags", wintypes.DWORD),
        ("szExeFile", ctypes.c_wchar * MAX_PATH),
    ]


class _IP_ADAPTER_ADDRESSES(ctypes.Structure):
    # Deprecated: see _local_ips(). Kept as an (unused) type only so any
    # external importer does not break; the struct was never populated
    # correctly (its "OperAddrOffset" field did not exist).
    _fields_ = [
        ("Length", wintypes.ULONG),
        ("IfIndex", wintypes.DWORD),
        ("Next", ctypes.c_void_p),
        ("AdapterName", ctypes.c_char * 256),
    ]


def _bind():
    """Bind the entry points with correct 64-bit types.

    ctypes defaults restype to c_int, which truncates a 64-bit handle or
    pointer - the same trap that made the icon GDI work silently produce
    nothing. Declared explicitly here.
    """
    iphlp = ctypes.WinDLL("iphlpapi.dll", use_last_error=True)
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)

    iphlp.GetExtendedTcpTable.restype = wintypes.DWORD
    iphlp.GetExtendedTcpTable.argtypes = [
        ctypes.c_void_p, ctypes.POINTER(wintypes.DWORD), wintypes.BOOL,
        ctypes.c_int, wintypes.DWORD, wintypes.DWORD]
    iphlp.GetExtendedUdpTable.restype = wintypes.DWORD
    iphlp.GetExtendedUdpTable.argtypes = [
        ctypes.c_void_p, ctypes.POINTER(wintypes.DWORD), wintypes.BOOL,
        ctypes.c_int, wintypes.DWORD, wintypes.DWORD]
    # B-055: GetAdaptersAddresses is NOT bound here any more. The old binding
    # declared the size parameter as c_void_p and callers passed None, so the
    # API could never report the required buffer length; the buffer was then
    # walked with a struct field that did not exist. It is replaced by
    # _local_ips()'s socket-based resolution — see that function.

    k32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    k32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    k32.Process32FirstW.restype = wintypes.BOOL
    k32.Process32FirstW.argtypes = [wintypes.HANDLE,
                                   ctypes.POINTER(_PROCESSENTRY32W)]
    k32.Process32NextW.restype = wintypes.BOOL
    k32.Process32NextW.argtypes = [wintypes.HANDLE,
                                  ctypes.POINTER(_PROCESSENTRY32W)]
    k32.CloseHandle.restype = wintypes.BOOL
    k32.CloseHandle.argtypes = [wintypes.HANDLE]
    # BUG-040: these two were never declared, so ctypes used its defaults -
    # restype c_int for OpenProcess, which TRUNCATES the 64-bit HANDLE, and no
    # argtypes for the Unicode call. Every image-path lookup therefore failed
    # and silently fell back to the bare filename, which is exactly what
    # grouping by full path exists to avoid: two Chrome versions, or 32- and
    # 64-bit Office, share a basename and are different programs. Caught by
    # the "rows are keyed by FULL image path" check reporting 0 of 16.
    k32.OpenProcess.restype = wintypes.HANDLE
    k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    k32.QueryFullProcessImageNameW.restype = wintypes.BOOL
    k32.QueryFullProcessImageNameW.argtypes = [
        wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR,
        ctypes.POINTER(wintypes.DWORD)]

    # BUG-041: the user32 entry points were never declared either, so
    # GetWindowTextLengthW received a TRUNCATED 64-bit HWND (ctypes defaults
    # every int argument to c_int) and returned 0 for nearly every window.
    # _window_titles() therefore found nothing and every row fell back to a
    # bare filename - which is the whole reason the friendly taskbar titles
    # ("budget.xlsx - Excel") never appeared.
    u32 = ctypes.WinDLL("user32", use_last_error=True)
    u32.EnumWindows.restype = wintypes.BOOL
    u32.EnumWindows.argtypes = [
        ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM),
        wintypes.LPARAM]
    u32.GetWindowThreadProcessId.restype = wintypes.DWORD
    u32.GetWindowThreadProcessId.argtypes = [wintypes.HWND,
                                            ctypes.POINTER(wintypes.DWORD)]
    u32.IsWindowVisible.restype = wintypes.BOOL
    u32.IsWindowVisible.argtypes = [wintypes.HWND]
    u32.GetWindowTextLengthW.restype = ctypes.c_int
    u32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
    u32.GetWindowTextW.restype = ctypes.c_int
    u32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR,
                                   ctypes.c_int]
    return iphlp, k32, u32


# --- helpers -------------------------------------------------------------- #
def _dword_to_ip(raw: int) -> str:
    """A MIB row stores an IPv4 address as a DWORD in NETWORK byte order."""
    try:
        return socket.inet_ntoa(struct.pack("<I", raw))
    except Exception:
        return "?"


def _bytes_to_ip(raw) -> str:
    """A 16-byte IPv6 address straight from the row."""
    try:
        return socket.inet_ntop(socket.AF_INET6, bytes(raw))
    except Exception:
        return "?"


def _port(v: int) -> int:
    """Ports are stored byte-swapped (network order in a DWORD)."""
    return ((v >> 8) & 0xFF) | ((v & 0xFF) << 8)


#: Well-known port names, resolved offline from the local services database.
#: Measured: 11 lookups in 16 ms with no network access at all.
#:
#: Reverse DNS was the obvious alternative and it is a trap: measured here,
#: 162.159.61.4 took 4594 ms to fail and 6 parallel lookups resolved 1 of 6,
#: because CDN addresses have no PTR record. That would have made the dialog
#: feel broken. A port name is instant and always available; a missing one is
#: simply left off.
_PORT_NAMES = {}


def port_name(port: int):
    """`443 -> 'https'`, or None when the port has no well-known name."""
    if not port or port < 0 or port > 65535:
        return None
    hit = _PORT_NAMES.get(port, False)
    if hit is not False:
        return hit or None
    name = None
    try:
        name = socket.getservbyport(port, "tcp")
    except (OSError, OverflowError):
        name = None
    _PORT_NAMES[port] = name
    return name


def format_endpoint(addr: str, port: int) -> str:
    """`1.1.1.1:443 https` - the port name only when one exists."""
    text = "%s:%d" % (addr, port)
    name = port_name(port)
    return "%s %s" % (text, name) if name else text


def _proc_table() -> dict:
    """pid -> exe name, from ONE toolhelp snapshot."""
    if not IS_WINDOWS:
        return {}
    try:
        _iphlp, k32, _u32 = _bind()
    except Exception:
        return {}
    snap = k32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if not snap or snap == INVALID_HANDLE_VALUE:
        return {}
    out = {}
    try:
        pe = _PROCESSENTRY32W()
        pe.dwSize = ctypes.sizeof(pe)
        if k32.Process32FirstW(snap, ctypes.byref(pe)):
            while True:
                out[int(pe.th32ProcessID)] = pe.szExeFile
                if not k32.Process32NextW(snap, ctypes.byref(pe)):
                    break
    except Exception:
        pass
    finally:
        try:
            k32.CloseHandle(snap)
        except Exception:
            pass
    return out


def _local_ips() -> set:
    """This machine's own IPv4 addresses, so loopback can be recognised.

    B-055: the old implementation called GetAdaptersAddresses with a NULL size
    pointer (so the API could never report the required buffer length) and
    then walked the buffer through a struct field ("OperAddrOffset") that did
    not exist. The whole body raised and was swallowed, so the set was always
    just the loopback seeds and a connection to one of our own interface
    addresses was shown as a remote endpoint. Resolving the machine's own
    addresses through the socket layer is what this set is actually for and
    cannot crash the way the hand-rolled 64-bit struct walk could.
    """
    ips = {"127.0.0.1", "0.0.0.0", "::1"}
    if not IS_WINDOWS:
        return ips
    try:
        _name, _aliases, addrs = socket.gethostbyname_ex(socket.gethostname())
        for a in addrs:
            if a:
                ips.add(str(a))
    except Exception:
        pass
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None,
                                       proto=socket.IPPROTO_TCP):
            if info[0] == socket.AF_INET and info[4] and info[4][0]:
                ips.add(info[4][0])
    except Exception:
        pass
    return ips


#: Executables Windows owns. Hidden by default so a casual user sees the
#: eight programs that matter rather than two hundred system rows. A
#: DISPLAY-only heuristic - it never hides anything on request.
_SYSTEM_EXES = frozenset((
    "svchost.exe", "system", "system idle process", "systemd",
    "lsass.exe", "services.exe", "csrss.exe", "wininit.exe",
    "winlogon.exe", "smss.exe", "sconhost.exe", "dwm.exe",
    "rtvsc.exe", "wmiprvse.exe", "spoolsv.exe", "searchindexer.exe",
    "mDNSresponder.exe", "dashost.exe", "conhost.exe", "fontdrvhost.exe",
    "dllhost.exe", "runtimebroker.exe", "wudfhost.exe", "ctfmon.exe",
    "taskhostw.exe", "audiodg.exe", "securityhealthservice.exe",
    "audiodg.exe", "wmiprvse.exe", "mpnotify.exe", "mousocoreworker.exe",
    "mouclassworker.exe", "tabtip.exe", "shellexperiencehost.exe",
    "startmenuexperiencehost.exe", "systemsettings.exe",
    "backgroundtaskhost.exe", "runtimebroker.exe", "sihost.exe",
    "searchfilterhost.exe", "searchapp.exe", "searchui.exe",
    "picksomething.exe", "lockapp.exe", "smartscreen.exe",
    "sniphelper.exe", "wudfhost.exe", "cmdhost.exe",
))


def is_system_row(name: str) -> bool:
    """True for Windows' own processes - the 'hide these' heuristic."""
    return (name or "").strip().lower() in _SYSTEM_EXES


#: Full image path per PID, resolved once per sample.
_IMG_CACHE = [None]


def _image_paths(pids) -> dict:
    """{pid: full image path} for the PIDs that have one.

    BUG-039: grouping by PID showed one row per PROCESS, so Chrome, Steam and
    every Electron app appeared as a dozen mystery entries. Grouping by the
    executable fixes it, and the executable's identity is its full path - the
    basename is not unique (two Chrome versions, 32- and 64-bit Office).

    QueryFullProcessImageNameW needs PROCESS_QUERY_LIMITED_INFORMATION, which
    a normal user holds for their own processes, so this does not require
    elevation. A PID we may not open falls back to its bare name rather than
    disappearing.
    """
    if _IMG_CACHE[0] is None:
        procs = _proc_table()
        out = {}
        try:
            # _bind() returns TWO values (iphlp, kernel32). Unpacking three
            # raised ValueError, which the broad except below swallowed, so
            # every lookup quietly fell back to the bare filename - the exact
            # thing grouping by full path is meant to prevent.
            _iphlp, k, _u32 = _bind()
            access = 0x1000          # PROCESS_QUERY_LIMITED_INFORMATION
            for pid in pids:
                name = procs.get(pid, "")
                if not name:
                    continue
                h = k.OpenProcess(access, False, pid)
                if not h or h == ctypes.c_void_p(-1).value:
                    out[pid] = name
                    continue
                try:
                    size = wintypes.DWORD(1024)
                    buf = ctypes.create_unicode_buffer(size.value)
                    if k.QueryFullProcessImageNameW(h, 0, buf,
                                                   ctypes.byref(size)):
                        out[pid] = buf.value
                    else:
                        out[pid] = name
                finally:
                    try:
                        k.CloseHandle(h)
                    except Exception:
                        pass
        except Exception:
            out = {pid: procs.get(pid, "") for pid in pids}
        _IMG_CACHE[0] = out
    return _IMG_CACHE[0]


def reset_image_cache() -> None:
    _IMG_CACHE[0] = None


#: Real MIB_TCP_STATE values. A row outside this set is a misdecode.
_TCP_STATES = frozenset(range(1, 13))

#: Key under which `connections()` returns its sampling diagnostics. PIDs are
#: ints, so a string key can never collide with one.
META = "__meta__"


def _live_pids() -> frozenset:
    """PIDs that are running right now, cached for the length of a sample.

    `lru_cache` rather than a module-level mutable holder: tools/
    verify_no_globals.py forbids a lowercase module-level list/dict/set
    because any caller can mutate shared state, and a one-element list used
    as a mutable box trips exactly that rule. A cache is the honest
    expression of "recompute when stale", and it needs no global.
    """
    return _live_pids_cached()


@lru_cache(maxsize=1)
def _live_pids_cached() -> frozenset:
    return frozenset(_proc_table())


def reset_pid_cache() -> None:
    """Drop the PID cache. Called at the end of each `connections()` call so
    a long-lived dialog never reports a process that has since exited."""
    _live_pids_cached.cache_clear()


def _read_table(fn, fam, cls, row_t) -> list:
    """One sizing call + one fill call, decoding a fixed-size row table.

    The sizing call's byte count is an OVER-estimate (measured for the
    AF_INET6 query: 1972 bytes asked for, 516 written back), so the
    POST-fill value of `size` is the trustworthy one and is what bounds the
    row walk - the sizing value can only be used for the allocation.

    The row array starts after the 4-byte dwNumEntries count. BUG-030 indexed
    from offset 0, which read the count as dwState and produced a state
    histogram full of values like 7584.
    """
    size = wintypes.DWORD(0)
    fn(None, ctypes.byref(size), False, fam, cls, 0)
    if not size.value:
        return []
    buf = ctypes.create_string_buffer(size.value)
    if fn(buf, ctypes.byref(size), False, fam, cls, 0) != 0:
        return []
    n = struct.unpack("<I", buf.raw[:4])[0]
    stride = ctypes.sizeof(row_t)
    room = max(0, (size.value - 4) // stride)
    base = ctypes.addressof(ctypes.c_char.from_buffer(buf)) + 4
    out = []
    for i in range(min(n, room)):
        out.append(ctypes.cast(base + i * stride,
                               ctypes.POINTER(row_t)).contents)
    return out


def _tcp_rows() -> tuple:
    """(rows, {"ipv6_unreadable": bool, "dropped": int})."""
    if not IS_WINDOWS:
        return [], {"ipv6_unreadable": False, "dropped": 0}
    try:
        iphlp, _k32, _u32 = _bind()
    except Exception:
        return [], {"ipv6_unreadable": False, "dropped": 0}
    live = _live_pids()
    rows = []
    dropped = 0
    # IPv4: the layout is unambiguous and verified (BUG-029/030/031).
    for r in _read_table(iphlp.GetExtendedTcpTable, AF_INET,
                         TCP_TABLE_OWNER_PID_ALL, _MIB_TCPROW_OWNER_PID):
        pid, state = int(r.dwOwningPid), int(r.dwState)
        # Validate every row, not just IPv6. The table is a moving target -
        # it is rebuilt by the kernel between calls, and a row whose state
        # is not a real MIB state is a torn read, not a connection. Such a
        # row is dropped (it could not be classified anyway) rather than
        # shown as something it is not.
        if state not in _TCP_STATES or pid not in live:
            dropped += 1
            continue
        rows.append({
            "pid": pid,
            "state": state,
            "lport": _port(int(r.dwLocalPort)),
            "laddr": _dword_to_ip(int(r.dwLocalAddr)),
            "rport": _port(int(r.dwRemotePort)),
            "raddr": _dword_to_ip(int(r.dwRemoteAddr)),
            "v6": False,
        })
    # IPv6: same rule, and it matters far more here, because the AF_INET6
    # table does not decode consistently at all on this machine (BUG-031).
    declared = 0
    for r in _read_table(iphlp.GetExtendedTcpTable, AF_INET6,
                         TCP_TABLE_OWNER_PID_ALL, _MIB_TCP6ROW_OWNER_PID):
        declared += 1
        pid, state = int(r.dwOwningPid), int(r.dwState)
        if state not in _TCP_STATES or pid not in live:
            continue
        rows.append({
            "pid": pid,
            "state": state,
            "lport": _port(int(r.dwLocalPort)),
            "laddr": _bytes_to_ip(r.dwLocalAddr),
            "rport": _port(int(r.dwRemotePort)),
            "raddr": _bytes_to_ip(r.dwRemoteAddr),
            "v6": True,
        })
    return rows, {
        "ipv6_unreadable": bool(declared) and not any(r["v6"] for r in rows),
        "dropped": dropped,
    }


def _udp_rows() -> list:
    if not IS_WINDOWS:
        return []
    try:
        iphlp, _k32, _u32 = _bind()
    except Exception:
        return []
    rows = []
    for r in _read_table(iphlp.GetExtendedUdpTable, AF_INET,
                         UDP_TABLE_OWNER_PID, _MIB_UDPROW_OWNER_PID):
        rows.append({"pid": int(r.dwOwningPid),
                     "lport": _port(int(r.dwLocalPort)),
                     "laddr": _dword_to_ip(int(r.dwLocalAddr)),
                     "v6": False})
    live = _live_pids()
    for r in _read_table(iphlp.GetExtendedUdpTable, AF_INET6,
                         UDP_TABLE_OWNER_PID, _MIB_UDP6ROW_OWNER_PID):
        pid = int(r.dwOwningPid)
        if pid not in live:
            continue
        rows.append({"pid": pid,
                     "lport": _port(int(r.dwLocalPort)),
                     "laddr": _bytes_to_ip(r.dwLocalAddr),
                     "v6": True})
    return rows


#: TCP states that mean "an actual conversation".
_ESTABLISHED = (5,)


def connections() -> dict:
    """{pid: {"name", "system", "active", "listening", "endpoints"}}.

    Carries one extra key, META ("__meta__"), holding this sample's
    diagnostics: whether the IPv6 table could be read and how many rows were
    dropped as torn. The dialog turns those into a visible caveat, because a
    tool that silently under-reports is worse than one that admits it. PIDs
    are ints, so a string key cannot collide with a row.

    `endpoints` is a de-duplicated, capped list of the REMOTE address:port
    a program is actually talking to - the thing a user is asking about when
    they open this. Loopback is kept out of it: 127.0.0.1 is this machine
    talking to itself and is never the answer.

    Never raises; returns {"error": ...} on total failure so the dialog can
    say why instead of spinning.
    """
    if not IS_WINDOWS:
        return {"error": "Network connections are Windows-only."}
    try:
        procs = _proc_table()
        mine = _local_ips()
        tcp, diag = _tcp_rows()
        udp = _udp_rows()
    except Exception as exc:
        reset_pid_cache()
        return {"error": "Could not read the connection tables: %s" % exc}

    out = {}
    max_endpoints = 6
    titles = _window_titles()

    # Group by EXECUTABLE, not by process. One row per program, the way every
    # task manager and network monitor on Windows does it.
    paths = _image_paths({r["pid"] for r in tcp} | {r["pid"] for r in udp})
    procs = _proc_table()

    def slot(pid):
        exe = paths.get(pid) or procs.get(pid) or ""
        key = exe.lower() or ("pid:%d" % pid)
        s = out.get(key)
        if s is None:
            s = out[key] = {
                "exe": exe or ("PID %d" % pid),
                "name": _group_name(pid, exe, procs, titles),
                "system": is_system_row(procs.get(pid, "")),
                "active": 0, "listening": 0, "endpoints": [],
                # BUGFIX 2026-10: "active" counted every established
                # connection while "endpoints" was capped at 6, so a program
                # with 40 connections reported "40 connected" and then listed
                # six addresses - and the dialog's "and N more" disclosure
                # could never fire, because len(endpoints) was never above 6.
                # These two counters make the cap visible instead of silent.
                "endpoints_total": 0,
                "omitted": 0,
                # Listening ports were checked for truthiness and then thrown
                # away, so a listening-only program had NO detail to show and
                # the pane fell back to "nothing right now".
                "listen_ports": [],
                "pids": [],
            }
        if pid not in s["pids"]:
            s["pids"].append(pid)
        return s

    for r in tcp:
        s = slot(r["pid"])
        state = r["state"]
        listening = state in (2, 12)          # LISTEN, CLOSE_WAIT-listening
        if state in _ESTABLISHED:
            s["active"] += 1
            ra, rp = r["raddr"], r["rport"]
            if rp and ra and ra not in ("0.0.0.0", "::", "127.0.0.1", "::1") \
                    and ra not in mine:
                ep = format_endpoint(ra, rp)
                if ep in s["endpoints"]:
                    continue
                s["endpoints_total"] += 1
                if len(s["endpoints"]) < max_endpoints:
                    s["endpoints"].append(ep)
                else:
                    s["omitted"] += 1
        elif listening and r["lport"]:
            s["listening"] += 1
            if r["lport"] not in s["listen_ports"] and \
                    len(s["listen_ports"]) < max_endpoints:
                s["listen_ports"].append(r["lport"])

    for r in udp:
        s = slot(r["pid"])
        if r["lport"] and r["laddr"] in ("0.0.0.0", "::"):
            s["listening"] += 1
            if r["lport"] not in s["listen_ports"] and \
                    len(s["listen_ports"]) < max_endpoints:
                s["listen_ports"].append(r["lport"])

    # A grouped row can hold several PIDs, so re-derive the friendliest name
    # now that we know the whole group.
    for s in out.values():
        s["name"] = _best_name(s["pids"], paths, procs, titles)

    # Added last, so every loop above only ever walked real rows.
    out[META] = diag
    reset_pid_cache()
    reset_image_cache()
    return out


def _window_titles() -> dict:
    """pid -> (title, visible). One pass over every top-level window."""
    if not IS_WINDOWS:
        return {}
    try:
        _iphlp, _k32, u = _bind()
    except Exception:
        return {}
    found = {}
    PROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    def cb(hwnd, _lparam):
        try:
            pid = wintypes.DWORD(0)
            u.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            if not pid.value:
                return True
            n = u.GetWindowTextLengthW(hwnd)
            if n <= 0:
                return True
            buf = ctypes.create_unicode_buffer(n + 1)
            u.GetWindowTextW(hwnd, buf, n + 1)
            title = buf.value.strip()
            if not title:
                return True
            vis = bool(u.IsWindowVisible(hwnd))
            prev = found.get(pid.value)
            if prev is None or (vis and not prev[1]):
                found[pid.value] = (title, vis)
        except Exception:
            pass
        return True

    try:
        u.EnumWindows(PROC(cb), 0)
    except Exception:
        pass
    return found


def _best_name(pids, paths, procs, titles) -> str:
    """The friendliest honest label for a group of one program's processes.

    A visible window title is best: it is what the user already recognises
    from their taskbar, and it often names the open document. Failing that,
    the executable's own name - never a bare PID, which means nothing.
    """
    for pid in pids:
        got = titles.get(pid)
        if got and got[1] and got[0]:
            return got[0]
    for pid in pids:
        exe = paths.get(pid) or procs.get(pid) or ""
        if exe:
            return os.path.basename(exe) or exe
    return "Program (PID %s)" % ", ".join(str(p) for p in pids[:3])


def _group_name(pid, exe, procs, titles) -> str:
    """Initial label for a group; refined once the whole group is known."""
    return _best_name([pid], {pid: exe}, procs, titles)


def _rows_only(data: dict) -> dict:
    """The PID entries, without the diagnostics key."""
    return {k: v for k, v in data.items() if k != META}


def summarize(data: dict) -> dict:
    """Plain-language counts for the dialog's headline.

    Deliberately does NOT label anything "suspicious" or "malware" - this
    data cannot support that judgement, and claiming it would be dishonest.
    """
    if not isinstance(data, dict) or data.get("error"):
        return {"apps": 0, "user_apps": 0, "active": 0, "hidden": 0,
                "processes": 0,
                "error": (data or {}).get("error", "unavailable"),
                "ipv6_unreadable": False, "dropped": 0}
    rows = _rows_only(data)
    meta = data.get(META) or {}
    apps = [s for s in rows.values() if not s["system"]]
    return {
        # "apps" counts PROGRAMS now, not processes. The two differ sharply on
        # a normal machine, and conflating them is exactly what made the old
        # list read as a dozen mystery entries for one browser.
        "apps": len(apps),
        "user_apps": len([s for s in apps if s["active"]]),
        "active": sum(s["active"] for s in rows.values()),
        "hidden": len([s for s in rows.values() if s["system"]]),
        "processes": sum(len(s.get("pids") or [1]) for s in rows.values()),
        "error": None,
        "ipv6_unreadable": bool(meta.get("ipv6_unreadable")),
        "dropped": int(meta.get("dropped") or 0),
    }