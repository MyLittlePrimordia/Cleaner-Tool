"""Who is holding a file open? Windows' Restart Manager, plus a safe way to ask.

Tk-free by design (matches app/netconn.py, app/svcs.py): this is a sampler,
the dialog that shows it lives in app/ui/dialogs/.

Why Restart Manager
-------------------
There is no supported Win32 call that answers "which process has this file
open". The ways people usually find out are all bad: holding the file open
exclusively and seeing who fails (racy, and it can itself cause the
"in use" error the user is trying to resolve), or brute-forcing every handle
with NtQuerySystemInformation (undocumented, needs SeDebugPrivilege, and
breaks when Microsoft changes it).

`Restart Manager` (Rstrtmgr.dll) is the supported answer, and it is what
Windows' own installer and file-move machinery uses. Verified on this machine
against a file the probe process was holding open:

    RmStartSession rc=0
    RmRegisterResources rc=0
    RmGetList -> ERROR_MORE_DATA, needed=1
    RmGetList -> rc=0 count=1  pid=24808 app='Python' rebootReasons=16

No elevation needed to ASK. rebootReasons=16 is
REBOOT_REASON_SERVICE_SPECIFIC, i.e. the holder is a service - which is
worth telling the user, because the right answer for a service is "restart it
from Services", not "kill it".

What this will not do
---------------------
It never force-kills anything on its own, and it refuses outright for the
processes where a mistake is unrecoverable: session-critical Windows
processes, anything running as SYSTEM that is a service, and Cleaner Tool
itself. `close_program(force=True)` is the only path to a hard kill and the
dialog gates it behind an explicit confirmation that names the program.
"""
from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import os
import subprocess
import sys

IS_WINDOWS = sys.platform.startswith("win")

#: How many files to register in one Restart Manager session. Registering a
#: whole drive's worth is pointless for a human asking about one folder, and
#: the session has to be torn down and rebuilt anyway when the answer changes.
MAX_FILES = 3000

#: Process names this tool refuses to close, ever. Killing any of these either
#: breaks the session, logs you out, or bricks the boot. Case-folded.
_NEVER_CLOSE = frozenset((
    "system", "system idle process", "secure system", "registry",
    "memory compression", "csrss.exe", "wininit.exe", "winlogon.exe",
    "smss.exe", "services.exe", "lsass.exe", "svchost.exe", "dwm.exe",
    "fontdrvhost.exe", "sihost.exe", "ctfmon.exe", "conhost.exe",
    "wudfhost.exe", "logonui.exe", "spoolsv.exe", "taskhostw.exe",
))

#: Same idea, by window class owner: refuse anything we can prove is the
#: shell or the session, even under an unfamiliar name.
_NEVER_PIDS = frozenset((0, 4))


def _bare(name: str) -> str:
    """A process name reduced to its stem, case-folded.

    BUG-036: `closable()` used to do `exe[:-4]` and then look the result up
    in a set whose entries all KEPT the extension - so every single by-name
    refusal was inert. Measured: csrss.exe, winlogon.exe, lsass.exe and
    svchost.exe all came back closable=True, and the dialog would have offered
    "Force close" on csrss.exe, which logs the user out immediately.
    Normalising both sides through one helper is the only way to be sure the
    comparison means something.
    """
    n = (name or "").strip().lower()
    if n.endswith(".exe"):
        n = n[:-4]
    return n


#: _NEVER_CLOSE reduced to bare stems, so a lookup cannot silently miss.
_NEVER_CLOSE_BARE = frozenset(_bare(n) for n in _NEVER_CLOSE)

CCH_RM_SESSION_KEY = 32
CCH_RM_MAX_APP_NAME = 255
CCH_RM_MAX_SVC_NAME = 63
_ERROR_MORE_DATA = 234
_RM_MAX_APP_TYPE = None

#: RM_APPLICATION_TYPE
_APP_TYPE_UNKNOWN = 0
_APP_TYPE_MAIN_WINDOW = 1
_APP_TYPE_OTHER_WINDOW = 2
_APP_TYPE_SERVICE = 3
_APP_TYPE_SERVER = 4

#: REBOOT_REASON_SERVICE_SPECIFIC
_REBOOT_REASON_SERVICE_SPECIFIC = 0x10

#: taskkill speaks in raw Windows strings. These are the two that actually
#: come up, translated into something a person can act on.
_TASKKILL_PLAIN = (
    ("can only be terminated forcefully",
     "It has no window to close, so it has to be ended. Unsaved work may be "
     "lost."),
    ("access is denied", "Windows will not let it be closed by this user."),
    ("not found", "It already exited."),
    ("no running instance", "It already exited."),
)

_APP_TYPE_PLAIN = {
    _APP_TYPE_MAIN_WINDOW: "an app you can see",
    _APP_TYPE_OTHER_WINDOW: "a background app",
    _APP_TYPE_SERVICE: "a Windows service",
    _APP_TYPE_SERVER: "a background service",
}


class _RM_UNIQUE_PROCESS(ctypes.Structure):
    _fields_ = [("dwProcessId", wt.DWORD), ("ProcessStartTime", wt.FILETIME)]


class _RM_PROCESS_INFO(ctypes.Structure):
    _fields_ = [
        ("Process", _RM_UNIQUE_PROCESS),
        ("strAppName", wt.WCHAR * (CCH_RM_MAX_APP_NAME + 1)),
        ("strServiceShortName", wt.WCHAR * (CCH_RM_MAX_SVC_NAME + 1)),
        ("ApplicationType", wt.DWORD),
        ("AppStatus", wt.ULONG),
        ("TSSessionId", wt.DWORD),
        ("bRestartable", wt.BOOL),
    ]


# ------------------------------------------------------------------ binding
_BINDED = [None]


def _bind():
    if _BINDED[0] is not None:
        return _BINDED[0]
    rm = ctypes.WinDLL("Rstrtmgr", use_last_error=True)
    # restype/argtypes declared explicitly: the default c_int truncates a
    # 64-bit HWND, which is the same trap that made the icon GDI work
    # silently produce nothing.
    rm.RmStartSession.restype = wt.DWORD
    rm.RmStartSession.argtypes = [ctypes.POINTER(wt.DWORD), wt.DWORD,
                                  wt.LPWSTR]
    rm.RmRegisterResources.restype = wt.DWORD
    rm.RmRegisterResources.argtypes = [wt.DWORD, wt.UINT,
                                       ctypes.POINTER(wt.LPWSTR), wt.UINT,
                                       ctypes.c_void_p, wt.UINT,
                                       ctypes.c_void_p]
    rm.RmGetList.restype = wt.DWORD
    rm.RmGetList.argtypes = [wt.DWORD, ctypes.POINTER(wt.UINT),
                             ctypes.POINTER(wt.UINT),
                             ctypes.POINTER(_RM_PROCESS_INFO),
                             ctypes.POINTER(wt.DWORD)]
    rm.RmEndSession.restype = wt.DWORD
    rm.RmEndSession.argtypes = [wt.DWORD]

    u = ctypes.WinDLL("user32", use_last_error=True)
    u.EnumWindows.restype = wt.BOOL
    u.EnumWindows.argtypes = [
        ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM), wt.LPARAM]
    u.GetWindowThreadProcessId.restype = wt.DWORD
    u.GetWindowThreadProcessId.argtypes = [wt.HWND, ctypes.POINTER(wt.DWORD)]
    u.IsWindowVisible.restype = wt.BOOL
    u.IsWindowVisible.argtypes = [wt.HWND]
    u.GetWindowTextLengthW.restype = ctypes.c_int
    u.GetWindowTextLengthW.argtypes = [wt.HWND]
    u.GetWindowTextW.restype = ctypes.c_int
    u.GetWindowTextW.argtypes = [wt.HWND, wt.LPWSTR, ctypes.c_int]

    k = ctypes.WinDLL("kernel32", use_last_error=True)
    k.QueryFullProcessImageNameW.restype = wt.BOOL
    k.QueryFullProcessImageNameW.argtypes = [wt.HANDLE, wt.DWORD, wt.LPWSTR,
                                              ctypes.POINTER(wt.DWORD)]
    k.OpenProcess.restype = wt.HANDLE
    k.OpenProcess.argtypes = [wt.DWORD, wt.BOOL, wt.DWORD]
    k.CloseHandle.restype = wt.BOOL
    k.CloseHandle.argtypes = [wt.HANDLE]
    k.GetCurrentProcessId.restype = wt.DWORD

    out = (rm, u, k)
    _BINDED[0] = out
    return out


# ------------------------------------------------------------- friendly names
def _window_titles() -> dict:
    """pid -> (title, visible). One pass over every top-level window.

    The title is what the user already recognises from their taskbar, and it
    often names the open document ("budget.xlsx - Excel"), which is a far
    better answer than "EXCEL.EXE (pid 4212)". Visible windows win over the
    invisible GDI+/message-only windows that every process owns.
    """
    if not IS_WINDOWS:
        return {}
    rm, u, _k = _bind()
    found = {}
    PROC = ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)

    def cb(hwnd, _lparam):
        pid = wt.DWORD(0)
        try:
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


def _exe_name(pid: int) -> str:
    """Full image path's file name, or '' when we may not look."""
    if not IS_WINDOWS or pid in _NEVER_PIDS:
        return ""
    _rm, _u, k = _bind()
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    h = k.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not h or h == ctypes.c_void_p(-1).value:
        return ""
    try:
        size = wt.DWORD(1024)
        buf = ctypes.create_unicode_buffer(size.value)
        if k.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
            return os.path.basename(buf.value)
        return ""
    except Exception:
        return ""
    finally:
        try:
            k.CloseHandle(h)
        except Exception:
            pass


def _proc_start(pid: int):
    """Process creation time for the PID-reuse guard, or None.

    Delegates to process_manager.process_create_time so the GetProcessTimes
    binding has one implementation.
    """
    try:
        from app.process_manager import process_create_time
        return process_create_time(pid)
    except Exception:
        return None


def display_name(pid: int, rm_app: str = "", titles=None) -> str:
    """The friendliest honest name we have for a process.

    Preference order, because each step is more recognisable than the last:
    visible window title -> executable name -> Restart Manager's own label.
    Never invents one; if all three are empty the caller shows the PID.
    """
    t = titles if titles is not None else _window_titles()
    got = t.get(pid)
    if got and got[1] and got[0]:
        return got[0]
    exe = _exe_name(pid)
    if exe:
        return exe
    if rm_app:
        return rm_app
    return "Program (PID %d)" % pid


# ------------------------------------------------------------------ scanning
def collect_files(target: str, max_files: int = MAX_FILES):
    """(files, truncated). A folder is walked shallowly; a file is itself.

    Reparse points are skipped: following a junction out of the target folder
    is how a "why is this locked" scan ends up walking an entire drive.
    """
    target = os.path.abspath(target)
    if os.path.isfile(target):
        return [target], False
    files = []
    truncated = False
    for dirpath, dirnames, filenames in os.walk(target, followlinks=False):
        # Drop reparse-point directories in place so os.walk skips them.
        kept = []
        for d in dirnames:
            full = os.path.join(dirpath, d)
            try:
                if not os.path.islink(full) and not os.path.isjunction(full):
                    kept.append(d)
            except (AttributeError, OSError):
                kept.append(d)
        dirnames[:] = kept
        for fn in filenames:
            if len(files) >= max_files:
                return files, True
            try:
                files.append(os.path.join(dirpath, fn))
            except OSError:
                continue
    return files, truncated


def _rm_pids_for(files, _bound=None):
    """The set of PIDs Restart Manager names as holding ANY of `files`.

    One RM session, discarded. Never raises - a failure here returns an
    empty set, which the caller reads as "nobody in this half", and the
    recursion then attributes the holder to the OTHER half. That is the
    wrong answer rather than a crash, and the caller re-checks the result
    with a whole-target scan afterwards, so a wrong attribution cannot make
    the dialog claim the file is free.
    """
    if not files:
        return set()
    try:
        rm, _u, _k = _bound or _bind()
    except Exception:
        return set()
    sess = wt.DWORD(0)
    key = ctypes.create_unicode_buffer(CCH_RM_SESSION_KEY + 1)
    try:
        if rm.RmStartSession(ctypes.byref(sess), 0, key) != 0:
            return set()
    except Exception:
        return set()
    try:
        BATCH = 256
        found = set()
        for start in range(0, len(files), BATCH):
            chunk = files[start:start + BATCH]
            arr = (wt.LPWSTR * len(chunk))(*chunk)
            if rm.RmRegisterResources(sess.value, len(chunk), arr, 0, None,
                                     0, None) != 0:
                continue
            # The sizing call's shape is copied VERBATIM from holders() below,
            # which is measured working on this machine. Passing None for the
            # buffer slot instead of a real (empty) UINT pointer looks
            # equivalent and is not: the bisection then converged on a file
            # nothing had open - f199 when f003 was the locked one - because
            # the left-half query reported the holder present for the wrong
            # half and every level agreed with the level above it.
            need = wt.UINT(0)
            cnt = wt.UINT(0)
            reasons = wt.DWORD(0)
            if rm.RmGetList(sess.value, ctypes.byref(need),
                            ctypes.byref(cnt), None,
                            ctypes.byref(reasons)) != _ERROR_MORE_DATA \
                    or not need.value:
                continue
            buf = (_RM_PROCESS_INFO * need.value)()
            cnt2 = wt.UINT(need.value)
            if rm.RmGetList(sess.value, ctypes.byref(need),
                            ctypes.byref(cnt2), buf,
                            ctypes.byref(reasons)) != 0:
                continue
            for i in range(cnt2.value):
                found.add(int(buf[i].Process.dwProcessId))
        return found
    except Exception:
        return set()
    finally:
        try:
            rm.RmEndSession(sess.value)
        except Exception:
            pass


#: Bisection is only attempted up to this many files. Each level costs one
#: RM pass, so the cost is log2(n) - measured at 73ms per pass on a 120-file
#: folder, i.e. about half a second. Past a few thousand files the scan
#: itself is already long enough that half a second stops mattering, and
#: past this cap we say "somewhere in here" rather than guess.
RESOLVE_FILES_MAX = 4000

#: Hard ceiling on RM passes per resolve, so a pathological tree cannot run
#: away. Measured on this machine: a session plus registration is ~2ms, and
#: the cost is RmGetList, at 9ms for 8 registered files and 22ms for 100.
#: So a pass is roughly 10-20ms and bisection naturally gets cheaper as the
#: halves shrink. 200 passes is about 3s worst case, which only a folder
#: with several programs each holding several files reaches; the common case
#: (one program, one file) finishes in about 11.
#:
#: The first value here was 40, chosen from the 73ms figure for a FULL
#: holders() scan rather than for a resolve pass. That was 4x too low and it
#: silently dropped files: a program holding f001, f100 and f199 was
#: reported as holding two of them.
RESOLVE_MAX_PASSES = 200


def resolve_locked_files(files, pids):
    """{pid: [paths]} - WHICH file(s) each holder has open, by bisection.

    Restart Manager answers "which programs are using any of these files",
    never "which file is this program using", so the old dialog could only
    ever say WHO had a folder open. That is the less useful half of the
    answer: the user's actual question is "which file in here?", and
    without it they cannot delete the folder one file at a time.

    Bisecting the file list gets it in far fewer than n passes - measured at
    73ms per pass, so 200 files costs a few hundred ms rather than 15s.

    BOTH halves are searched, and that is the whole subtlety. One program
    routinely holds several files in the folder being scanned - an editor
    with three files open, a game with a save folder. The first version
    tested only the left half and pushed the remaining holders right, so a
    program holding f003 and f137 was reported as holding f003 only, and
    f137 was silently never found. Splitting and descending into each half
    with the holders RM actually names in THAT half is the only formulation
    that is correct for multi-file holders.

    Returns (mapping, unresolved_pids). A pid in `unresolved` held SOME of
    the files but the search ran out of passes or hit an RM error - it is
    reported as "somewhere in here" rather than being pinned to a file we
    did not actually confirm.
    """
    wanted = set()
    for p in pids or ():
        try:
            wanted.add(int(p))
        except (TypeError, ValueError):
            continue
    out = {p: [] for p in wanted}
    if not files or not wanted:
        return out, set()

    _rm, _u, _k = _bind()
    state = {"passes": 0}

    def descend(chunk, holders):
        if not chunk or not holders:
            return
        if state["passes"] >= RESOLVE_MAX_PASSES:
            return
        if len(chunk) == 1:
            for p in holders:
                if chunk[0] not in out[p]:
                    out[p].append(chunk[0])
            return
        mid = len(chunk) // 2
        for half in (chunk[:mid], chunk[mid:]):
            if state["passes"] >= RESOLVE_MAX_PASSES:
                return
            state["passes"] += 1
            in_half = _rm_pids_for(half, _bound=(_rm, _u, _k)) & holders
            if in_half:
                descend(half, in_half)

    descend(list(files), wanted)
    done = {p for p, v in out.items() if v}
    return out, wanted - done


def holders(target: str, max_files: int = MAX_FILES,
            resolve_files: bool = False) -> dict:
    """Who is holding `target` (a file or a folder) open?

    Returns {"holders": [...], "scanned": int, "truncated": bool,
             "resolved": bool, "error": str|None}. Never raises.

    `resolve_files=True` additionally attaches `files` to each holder - which
    file it actually has open - by bisection. Off by default because it
    costs log2(n) extra Restart Manager passes and most callers only need
    the programs.
    """
    if not IS_WINDOWS:
        return {"holders": [], "scanned": 0, "truncated": False,
                "resolved": False, "error": "File locks are Windows-only."}
    if not target or not os.path.exists(target):
        return {"holders": [], "scanned": 0, "truncated": False,
                "resolved": False,
                "error": "That file or folder no longer exists."}

    try:
        files, truncated = collect_files(target, max_files)
    except Exception as exc:
        return {"holders": [], "scanned": 0, "truncated": False,
                "resolved": False,
                "error": "Could not read that folder: %s" % exc}

    if not files:
        return {"holders": [], "scanned": 0, "truncated": truncated,
                "resolved": False, "error": None}

    rm, _u, _k = _bind()
    key = ctypes.create_unicode_buffer(CCH_RM_SESSION_KEY + 1)
    sess = wt.DWORD(0)
    rc = rm.RmStartSession(ctypes.byref(sess), 0, key)
    if rc != 0:
        return {"holders": [], "scanned": len(files), "truncated": truncated,
                "resolved": False,
                "error": "Windows would not start a lock scan (error %d)."
                         % rc}

    try:
        out = []
        reasons = wt.DWORD(0)
        # Restart Manager takes a bounded list; registering everything at once
        # is correct but a 3000-element marshalled array is slow, so batch it.
        BATCH = 256
        seen = {}
        for start in range(0, len(files), BATCH):
            chunk = files[start:start + BATCH]
            arr = (wt.LPWSTR * len(chunk))(*chunk)
            rc = rm.RmRegisterResources(sess.value, len(chunk), arr,
                                        0, None, 0, None)
            if rc != 0:
                continue
            need = wt.UINT(0)
            cnt = wt.UINT(0)
            reasons = wt.DWORD(0)
            rc = rm.RmGetList(sess.value, ctypes.byref(need), ctypes.byref(cnt),
                              None, ctypes.byref(reasons))
            if rc != _ERROR_MORE_DATA or not need.value:
                continue
            buf = (_RM_PROCESS_INFO * need.value)()
            cnt2 = wt.UINT(need.value)
            rc = rm.RmGetList(sess.value, ctypes.byref(need),
                              ctypes.byref(cnt2), buf,
                              ctypes.byref(reasons))
            if rc != 0:
                continue
            for i in range(cnt2.value):
                pi = buf[i]
                pid = int(pi.Process.dwProcessId)
                if pid in seen:
                    continue
                svc = pi.strServiceShortName or ""
                seen[pid] = True
                out.append({
                    "pid": pid,
                    "rm_app": pi.strAppName or "",
                    "service": svc,
                    "app_type": int(pi.ApplicationType),
                    "is_service": bool(svc) or (
                        int(pi.ApplicationType) == _APP_TYPE_SERVICE),
                    "kind": _APP_TYPE_PLAIN.get(int(pi.ApplicationType),
                                                "a program"),
                })
        titles = _window_titles()
        for h in out:
            h["name"] = display_name(h["pid"], h["rm_app"], titles)
            # B-055-class / LOW: the image basename is the field closable() and
            # why_not_closable() actually test against _NEVER_CLOSE_BARE — it
            # was read as a key ("exe") that holders() never set, so the
            # by-name refusal was inert and rm_app (often a bare label, not a
            # path) was used instead. Populate the real field.
            h["exe"] = _exe_name(h["pid"])
            # PID-reuse guard carrier: creation time captured with this row.
            h["start"] = _proc_start(h["pid"])
            h["can_close"] = closable(h)
            # BUG-035: `taskkill` WITHOUT /F only works on a process that
            # owns a top-level window - it posts WM_CLOSE. Verified against a
            # console process holding a file: taskkill refused with "This
            # process can only be terminated forcefully (with /F option)",
            # and the file stayed locked. Offering "Ask it to close" for such
            # a process is offering an action that cannot work, so the
            # dialog needs to know which kind this is.
            got = titles.get(h["pid"])
            h["has_window"] = bool(got and got[1])
            h["graceful"] = bool(got and got[1])
        out.sort(key=lambda h: (h["is_service"], h["name"].lower()))

        # Which FILE, not just which program. Off unless asked for, because
        # it costs log2(n) extra Restart Manager passes.
        resolved = False
        if resolve_files and out and not truncated \
                and len(files) <= RESOLVE_FILES_MAX:
            try:
                mapping, unresolved = resolve_locked_files(
                    files, [h["pid"] for h in out])
                for h in out:
                    got = mapping.get(h["pid"]) or []
                    h["files"] = got
                    h["somewhere"] = h["pid"] in unresolved
                resolved = True
            except Exception:
                # A failed resolve must not fail the scan: the programs are
                # still known, and the dialog says which files were resolved.
                resolved = False

        return {"holders": out, "scanned": len(files),
                "truncated": truncated, "resolved": resolved,
                "error": None}
    except Exception as exc:
        return {"holders": [], "scanned": len(files), "truncated": truncated,
                "resolved": False, "error": "Lock scan failed: %s" % exc}
    finally:
        try:
            rm.RmEndSession(sess.value)
        except Exception:
            pass


def closable(holder: dict) -> bool:
    """May we offer to close this one? Conservative on purpose.

    False for session-critical Windows processes, for anything Restart
    Manager identifies as a service (the right fix there is a restart, not a
    kill), and for Cleaner Tool itself.
    """
    pid = int(holder.get("pid") or 0)
    if pid in _NEVER_PIDS:
        return False
    if holder.get("is_service"):
        return False
    if self_pid() and pid == self_pid():
        return False
    if _bare(holder.get("exe") or holder.get("rm_app") or "") in \
            _NEVER_CLOSE_BARE:
        return False
    return True


def self_pid() -> int:
    try:
        _rm, _u, k = _bind()
        return int(k.GetCurrentProcessId())
    except Exception:
        return 0


def why_not_closable(holder: dict) -> str:
    """Plain sentence for a greyed-out action. Never blank."""
    if holder.get("is_service"):
        return ("%s is a Windows service. Restart it from the Services tool "
                "instead of closing it here." % (holder.get("name") or "It"))
    if _bare(holder.get("exe") or holder.get("rm_app") or "") in \
            _NEVER_CLOSE_BARE:
        return ("%s is part of Windows itself. Closing it would end your "
                "session or restart the PC." % (holder.get("name") or "It"))
    if holder.get("pid") == self_pid():
        return "That is Cleaner Tool itself."
    return "This one cannot be closed safely from here."


def why_must_force(holder: dict) -> str:
    """Why the only route here is a hard kill. Empty when one is not needed."""
    if holder.get("graceful"):
        return ""
    return ("%s has no window, so there is nothing to close politely - it "
            "has to be ended. Unsaved work may be lost."
            % (holder.get("name") or "This program"))


# ------------------------------------------------------------------- closing
def close_program(pid: int, force: bool = False, timeout: int = 12,
                  expected_start=None) -> dict:
    """Ask a program to close. `force` escalates to a hard kill.

    Returns {"ok": bool, "message": str}. Uses taskkill because a graceful
    close is exactly "post WM_CLOSE to its windows", and taskkill does that
    correctly for console and GUI processes alike without needing to guess
    which window is the main one.

    Refuses outright rather than escalating on its own: a caller that passes
    an unsafe pid gets a refusal, not a kill.

    `expected_start` (the holder's "start" field) enables the PID-reuse guard:
    a PID seen at scan time can exit and be recycled before the user clicks
    Close, so a mismatched creation time refuses instead of killing an
    unrelated process.
    """
    if not IS_WINDOWS:
        return {"ok": False, "message": "Windows-only."}
    pid = int(pid)
    if pid in _NEVER_PIDS:
        return {"ok": False, "message": "Refusing to close a core Windows "
                                        "process."}
    if self_pid() and pid == self_pid():
        return {"ok": False, "message": "Refusing to close Cleaner Tool."}
    # PID-reuse guard.
    if expected_start is not None:
        cur = _proc_start(pid)
        if cur is None or cur != expected_start:
            return {"ok": False,
                    "message": "That program has already exited (its PID was "
                               "reused), so nothing was closed."}
    # By-name refusal, independent of what the caller pre-filtered: a caller
    # that reached here for a freshly-resolved PID must not be able to kill a
    # session-critical process just because it never ran closable().
    try:
        if _bare(_exe_name(pid)) in _NEVER_CLOSE_BARE:
            return {"ok": False, "message": "Refusing to close a core Windows "
                                            "process."}
    except Exception:
        pass

    cmd = ["taskkill", "/PID", str(pid)]
    if force:
        cmd.append("/F")
    try:
        proc = subprocess.run(
            cmd, capture_output=True, timeout=timeout,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except subprocess.TimeoutExpired:
        return {"ok": False, "message": "Windows did not respond in time."}
    except Exception as exc:
        return {"ok": False, "message": "Could not close it: %s" % exc}

    out = (proc.stdout or b"").decode("utf-8", "replace").strip()
    err = (proc.stderr or b"").decode("utf-8", "replace").strip()
    if proc.returncode == 0:
        verb = "Closed" if not force else "Force-closed"
        return {"ok": True,
                "message": out.splitlines()[0] if out
                else "%s PID %d." % (verb, pid)}
    detail = (err or out).splitlines()
    tail = detail[-1] if detail else "Windows refused."
    low = tail.lower()
    for needle, plain in _TASKKILL_PLAIN:
        if needle in low:
            return {"ok": False, "message": plain}
    if "access" in low or "denied" in low:
        return {"ok": False,
                "message": "Windows refused - %s. Try again as "
                            "administrator, or close it yourself." % tail}
    return {"ok": False, "message": tail}


def still_locked(target: str, max_files: int = MAX_FILES) -> bool:
    """Re-check after a close attempt, so the UI can say it worked."""
    res = holders(target, max_files)
    return bool(res.get("holders"))


def scan_limits() -> dict:
    """Whether this scan could be incomplete, and why.

    BUG-042: a non-elevated Restart Manager scan cannot attribute files held
    by processes running as another user, and the tool said nothing about it.
    Measured on this machine, non-elevated: 119 of 224 running processes
    cannot be opened for query at all - System, csrss.exe, wininit.exe,
    services.exe, LsaIso.exe and so on, which are precisely the ones that
    hold system files during a Windows Update. A scan that silently returns
    "nothing has this open" in that state is worse than useless: it tells
    the user to go ahead and delete something that is in use.

    So the count is reported, and the dialog offers a re-scan with
    administrator rights.
    """
    if not IS_WINDOWS:
        return {"elevated": False, "incomplete": False, "hidden": 0,
                "visible": 0, "reason": ""}
    from app.infra.elevated_run import is_elevated
    if is_elevated():
        return {"elevated": True, "incomplete": False, "hidden": 0,
                "visible": 0, "reason": ""}
    hidden = 0
    visible = 0
    try:
        from app import netconn
        procs = netconn._proc_table()
        _ip, k, _u = netconn._bind()
        for pid in procs:
            h = k.OpenProcess(0x1000, False, pid)   # QUERY_LIMITED_INFORMATION
            if not h or h == ctypes.c_void_p(-1).value:
                hidden += 1
            else:
                visible += 1
                k.CloseHandle(h)
    except Exception:
        # Counting is a nice-to-have; the WARNING is the point, and it stands
        # on being non-elevated alone.
        hidden = 0
    reason = ""
    if hidden:
        reason = ("%d running program(s) cannot be examined without "
                  "administrator rights, so this list may be incomplete."
                  % hidden)
    return {"elevated": False, "incomplete": True, "hidden": hidden,
            "visible": visible, "reason": reason}


#: The elevated scan, as an Add-Type C# block. PowerShell's own P/Invoke
#: marshalling for RmRegisterResources' mixed string/int-array signature is
#: awkward and easy to get subtly wrong; C# gives real types and lets the
#: compiler catch mistakes. Tab-separated output, same shape as `holders`.
_ELEVATED_SCAN = r'''
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [Text.Encoding]::UTF8
Add-Type -TypeDefinition @"
using System;
using System.Collections.Generic;
using System.Runtime.InteropServices;
using System.Text;

public static class CleanerLockScan {
    [StructLayout(LayoutKind.Sequential)]
    struct RM_UNIQUE_PROCESS {
        public int dwProcessId;
        public System.Runtime.InteropServices.ComTypes.FILETIME ProcessStartTime;
    }
    [StructLayout(LayoutKind.Sequential, CharSet = CharSet.Unicode)]
    struct RM_PROCESS_INFO {
        public RM_UNIQUE_PROCESS Process;
        [MarshalAs(UnmanagedType.ByValTStr, SizeConst = 256)] public string strAppName;
        [MarshalAs(UnmanagedType.ByValTStr, SizeConst = 64)] public string strServiceShortName;
        public uint ApplicationType; public uint AppStatus; public uint TSSessionId;
        [MarshalAs(UnmanagedType.Bool)] public bool bRestartable;
    }
    [DllImport("rstrtmgr.dll", CharSet = CharSet.Unicode)]
    static extern int RmStartSession(out uint h, int flags, string key);
    [DllImport("rstrtmgr.dll", CharSet = CharSet.Unicode)]
    static extern int RmRegisterResources(uint h, uint nFiles, string[] files,
        uint nApps, IntPtr apps, uint nSvc, string[] svcs);
    [DllImport("rstrtmgr.dll")]
    static extern int RmGetList(uint h, out uint needed, ref uint count,
        [In, Out] RM_PROCESS_INFO[] apps, ref uint reasons);
    [DllImport("rstrtmgr.dll")]
    static extern int RmEndSession(uint h);

    public static string Scan(string path) {
        var sb = new StringBuilder();
        uint sess = 0;
        string key = Guid.NewGuid().ToString("N").Substring(0, 32);
        int rc = RmStartSession(out sess, 0, key);
        if (rc != 0) return "ERR|start|" + rc;
        try {
            rc = RmRegisterResources(sess, 1, new string[] { path },
                0, IntPtr.Zero, 0, null);
            if (rc != 0) return "ERR|register|" + rc;
            uint needed = 0, count = 0, reasons = 0;
            rc = RmGetList(sess, out needed, ref count, null, ref reasons);
            if (rc == 0) return "OK|";
            if (rc != 234 || needed == 0) return "ERR|getlist|" + rc;
            var buf = new RM_PROCESS_INFO[needed];
            count = needed;
            rc = RmGetList(sess, out needed, ref count, buf, ref reasons);
            if (rc != 0) return "ERR|getlist2|" + rc;
            for (int i = 0; i < count; i++) {
                var p = buf[i];
                sb.Append(p.Process.dwProcessId).Append('\t')
                  .Append((p.strAppName ?? "").Replace('\t', ' ')).Append('\t')
                  .Append((p.strServiceShortName ?? "").Replace('\t', ' '))
                  .Append('\t').Append(p.ApplicationType).Append('\n');
            }
            return "OK|" + sb.ToString();
        } finally { try { RmEndSession(sess); } catch { } }
    }
}
"@
'CLEANER_SCAN_SENTINEL'
[CleanerLockScan]::Scan('__PATH__')
'''


def holders_elevated(target: str, timeout: int = 90) -> dict:
    """holders(), but with administrator rights, via a UAC prompt.

    Returns the same shape as holders() plus "cancelled". A declined prompt is
    a normal outcome and is reported as such, never as a failed scan.
    """
    if not IS_WINDOWS:
        return {"holders": [], "scanned": 0, "truncated": False,
                "error": "Windows-only.", "cancelled": False}
    if not target or not os.path.exists(target):
        return {"holders": [], "scanned": 0, "truncated": False,
                "error": "That file or folder no longer exists.",
                "cancelled": False}

    from app.infra.elevated_run import is_elevated, run_elevated
    if is_elevated():
        import subprocess
        script = _ELEVATED_SCAN.replace("__PATH__", target.replace("'", "''"))
        try:
            p = subprocess.run(
                ["powershell", "-NoProfile", "-NonInteractive", "-Command",
                 script], capture_output=True, timeout=timeout,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            text = (p.stdout or b"").decode("utf-8", "replace")
            return _parse_elevated(text, target)
        except Exception as exc:
            return {"holders": [], "scanned": 0, "truncated": False,
                    "error": "The administrator scan failed: %s" % exc,
                    "cancelled": False}

    ok, text = run_elevated(
        _ELEVATED_SCAN.replace("__PATH__", target.replace("'", "''")), timeout)
    if not ok:
        return {"holders": [], "scanned": 0, "truncated": False,
                "error": None, "cancelled": True}
    return _parse_elevated(text or "", target)


def _parse_elevated(text: str, target: str) -> dict:
    """Turn the sentinel-prefixed payload into holders()' shape."""
    if "ERR|" in text:
        code = text.split("ERR|")[-1].strip().splitlines()[0]
        return {"holders": [], "scanned": 0, "truncated": False,
                "error": "The administrator scan could not read the lock "
                         "table (%s)." % code, "cancelled": False}
    if "OK|" not in text:
        return {"holders": [], "scanned": 0, "truncated": False,
                "error": "The administrator scan returned nothing readable.",
                "cancelled": False}
    body = text.split("OK|", 1)[1]
    titles = _window_titles()
    out = []
    for line in body.splitlines():
        parts = line.split("\t")
        if len(parts) < 2 or not parts[0].strip().isdigit():
            continue
        pid = int(parts[0])
        app = parts[1].strip()
        svc = parts[2].strip() if len(parts) > 2 else ""
        try:
            app_type = int(parts[3]) if len(parts) > 3 else 0
        except ValueError:
            app_type = 0
        holder = {
            "pid": pid, "rm_app": app, "service": svc, "app_type": app_type,
            "is_service": bool(svc) or app_type == 3,
            "kind": _APP_TYPE_PLAIN.get(app_type, "a program"),
        }
        holder["name"] = display_name(pid, app, titles)
        holder["can_close"] = closable(holder)
        out.append(holder)
    out.sort(key=lambda h: (h["is_service"], h["name"].lower()))
    return {"holders": out, "scanned": 1, "truncated": False,
            "resolved": False, "error": None, "cancelled": False}


def verdict_for(data: dict) -> str:
    """One plain sentence, and never a promise we cannot keep.

    BUGFIX 2026-10: the zero-holders branch used to read "Nothing has this
    open. You can delete or rename it freely." unconditionally. But when the
    scan finds no files at all - an empty folder, or one whose contents it
    could not enumerate - it returns holders=[] AND scanned=0 having checked
    NOTHING. That sentence then told the user a file was safe to delete on the
    strength of zero evidence, and the early return skipped the "Checked N
    files" note that would have contradicted it. The docstring above already
    promised "never a promise we cannot keep"; this restores that.
    """
    if not data:
        return "Pick a file or folder to check."
    if data.get("error"):
        return data["error"]
    n = len(data.get("holders") or [])
    if n == 0:
        if not data.get("scanned"):
            return ("Nothing was actually checked - this folder has no files "
                    "in it, or none of them could be read.")
        if data.get("incomplete"):
            # The caller passed the scan_limits() caveat in. Unelevated scans
            # cannot see every holder, so "delete freely" is still a promise
            # we cannot keep - but the file may well be free.
            return ("Nothing visible has this open. Without administrator "
                    "rights that can't be certain.")
        return "Nothing has this open. You can delete or rename it freely."
    if n == 1:
        return "1 program is holding this open."
    return "%d programs are holding this open." % n