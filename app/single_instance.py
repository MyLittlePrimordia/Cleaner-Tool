"""
Single-instance guard + focus handoff (Phase 1 of tray/auto-elevate work).

One running copy only: the first process owns a named mutex for life; a
second launch signals the owner (focus it) and exits quietly instead of
opening a twin window. The elevation handshake and the tray both depend
on this — the auto-elevate handoff (/Run + starter exits) is racy without
it, and a tray app that can be double-launched is broken by definition.

stdlib + ctypes only (the app has zero third-party runtime deps by
design). Never raises: every failure degrades to "no guard" (today's
behavior — twins possible, nothing else changes).

Cross-integrity note: an elevated owner and a standard-rights second
launch live in different integrity levels. The show-event is created
with a NULL-DACL security descriptor (signal-only object, no data) so a
medium-integrity process can still pulse it; UIPI still blocks raw window
messages, which is why the handoff is an event + Tk poll, never
SendMessage. If the signal fails, the loser toasts/exits — never hangs.

Namespace: Global\\ first (one copy per machine, covers the elevated /
unelevated same-session split), Local\\ fallback when Global\\ creation
is denied (standard users lack SeCreateGlobalPrivilege). Both sides use
the same probe order, so they always agree.
"""

from __future__ import annotations

import os
import sys
import threading

_MUTEX_BASENAME = "CleanerTool_SingleInstance_Mutex"
_EVENT_BASENAME = "CleanerTool_ShowWindow_Event"

_ERROR_ALREADY_EXISTS = 183
_ERROR_ACCESS_DENIED = 5
_WAIT_OBJECT_0 = 0

_state = {
    "namespace": None,      # "Global" or "Local" once probed
    "mutex": None,          # owned handle (winner only)
    "event": None,          # show-event handle (winner only)
    "bg_claim": None,       # background-claim thread (elevation child only)
}


def _is_windows() -> bool:
    try:
        return sys.platform.startswith("win")
    except Exception:
        return False


def _raw_kernel32():
    import ctypes
    return ctypes.windll.kernel32


def _bind():
    """64-bit-correct signatures for every kernel32 call below.

    ctypes defaults foreign calls through 32-bit c_int: real HANDLE
    values overflow it (CloseHandle on a truncated handle closes the
    wrong object or fails). Bound once, cached on the function."""
    import ctypes
    wide = ctypes.sizeof(ctypes.c_void_p) == 8
    H = ctypes.c_void_p
    U = ctypes.c_uint
    D = ctypes.c_uint32
    B = ctypes.c_int
    k = ctypes.windll.kernel32
    k.CreateMutexW.argtypes = [ctypes.c_void_p, B, ctypes.c_wchar_p]
    k.CreateMutexW.restype = H
    k.CloseHandle.argtypes = [H]
    k.CloseHandle.restype = B
    k.GetLastError.argtypes = []
    k.GetLastError.restype = D
    k.OpenEventW.argtypes = [D, B, ctypes.c_wchar_p]
    k.OpenEventW.restype = H
    k.SetEvent.argtypes = [H]
    k.SetEvent.restype = B
    k.WaitForSingleObject.argtypes = [H, D]
    k.WaitForSingleObject.restype = D
    k.CreateEventW.argtypes = [ctypes.c_void_p, B, B, ctypes.c_wchar_p]
    k.CreateEventW.restype = H
    # NOTE: the security-descriptor calls live in advapi32, not kernel32
    # (binding them on kernel32 raises AttributeError — caught by _k()).
    a = ctypes.windll.advapi32
    a.InitializeSecurityDescriptor.argtypes = [ctypes.c_void_p, D]
    a.InitializeSecurityDescriptor.restype = B
    a.SetSecurityDescriptorDacl.argtypes = [ctypes.c_void_p, B,
                                            ctypes.c_void_p, B]
    a.SetSecurityDescriptorDacl.restype = B
    k.GetThreadId.argtypes = [H]
    k.GetThreadId.restype = D


_bind_done = False


def _k():
    global _bind_done
    k = _raw_kernel32()
    if not _bind_done:
        try:
            _bind()
        except Exception:
            pass
        _bind_done = True
    return k


def _adv():
    """advapi32 with the security-descriptor calls bound (see _bind)."""
    _k()  # ensure bindings attempted once
    import ctypes
    return ctypes.windll.advapi32


def _null_dacl_attributes():
    """SECURITY_ATTRIBUTES with a NULL DACL (allow-everyone, signal-only
    objects). Returns (attributes, buffers) — the caller must keep the
    buffers alive until Create* returns. None when unavailable (caller
    falls back to default security, same-integrity only)."""
    try:
        import ctypes
        from ctypes import wintypes as _w

        class _SA(ctypes.Structure):
            _fields_ = [("nLength", _w.DWORD),
                        ("lpSecurityDescriptor", ctypes.c_void_p),
                        ("bInheritHandle", _w.BOOL)]
        k = _k()
        a = _adv()
        sd = ctypes.create_string_buffer(64)
        if not a.InitializeSecurityDescriptor(sd, 1):
            return None
        # NULL DACL, not present -> everyone allowed (signal-only object)
        if not a.SetSecurityDescriptorDacl(sd, True, None, False):
            return None
        sa = _SA()
        sa.nLength = ctypes.sizeof(sa)
        sa.lpSecurityDescriptor = ctypes.cast(sd, ctypes.c_void_p)
        sa.bInheritHandle = False
        return sa, sd
    except Exception:
        return None


def _probe_namespace() -> str:
    """Pick Global\\ when creatable, else Local\\. Cached after first probe."""
    ns = _state.get("namespace")
    if ns in ("Global", "Local"):
        return ns
    ns = "Local"
    if _is_windows():
        try:
            import ctypes
            k = _k()
            probe = f"Global\\CleanerTool_NsProbe_{os.getpid()}"
            h = k.CreateMutexW(None, False, probe)
            if h:
                err = k.GetLastError()
                try:
                    k.CloseHandle(h)
                except Exception:
                    pass
                # created (or opened) without access-denied -> Global works
                if err != _ERROR_ACCESS_DENIED:
                    ns = "Global"
            else:
                if k.GetLastError() != _ERROR_ACCESS_DENIED:
                    ns = "Global"
        except Exception:
            pass
    _state["namespace"] = ns
    return ns


def _full_name(basename: str, namespace: str | None = None) -> str:
    return f"{namespace or _probe_namespace()}\\{basename}"


def acquire(name: str | None = None):
    """Try to become the single instance. Returns a handle on success
    (callers keep it via set_owner() for process life), None when another
    copy already owns it. `name` overrides the mutex basename (tests use
    unique names so they can never collide with a real running app)."""
    if not _is_windows():
        return object()
    try:
        import ctypes
        k = _k()
        h = k.CreateMutexW(None, False, _full_name(name or _MUTEX_BASENAME))
        if not h:
            return None
        if k.GetLastError() == _ERROR_ALREADY_EXISTS:
            try:
                k.CloseHandle(h)
            except Exception:
                pass
            return None
        return h
    except Exception:
        return None


def release(handle) -> None:
    """Release a handle from acquire(). Best-effort, never raises. The OS
    reclaims handles on exit anyway; this exists for tests and clean
    shutdown paths."""
    try:
        if handle is None:
            return
        if not _is_windows():
            return
        _k().CloseHandle(handle)
    except Exception:
        pass
    try:
        if _state.get("mutex") is handle:
            _state["mutex"] = None
    except Exception:
        pass


def set_owner(handle) -> None:
    """Record the owned mutex so the winner holds it for process life."""
    try:
        _state["mutex"] = handle
    except Exception:
        pass


def _open_show_event(name: str | None = None, write: bool = False):
    """Open (never create) the winner's show-event. write=True requests
    modify rights for SetEvent; otherwise synchronize-only."""
    if not _is_windows():
        return None
    try:
        k = _k()
        access = 0x0002 | 0x00100000 if write else 0x00100000  # MODIFY_STATE|SYNCHRONIZE
        h = k.OpenEventW(access, False, _full_name(name or _EVENT_BASENAME))
        return h or None
    except Exception:
        return None


def ensure_show_event(name: str | None = None):
    """Winner side: open-or-create the show-event (NULL DACL so any
    integrity level can pulse it), then drain one stale pulse — an
    auto-reset event with no waiter stays signaled after a crashed
    signaler, which would otherwise fake-focus us once at startup."""
    if not _is_windows():
        return None
    if name is None and _state.get("event") is not None:
        return _state.get("event")
    try:
        import ctypes
        k = _k()
        h = _open_show_event(name, write=False)
        if h is None:
            sa_pack = _null_dacl_attributes()
            if sa_pack is not None:
                sa, _buf = sa_pack
                # auto-reset (False): each SetEvent wakes exactly one poll;
                # a manual-reset event would latch signaled and focus-loop
                # every 500ms forever after a single pulse.
                h = k.CreateEventW(ctypes.byref(sa), False, False,
                                   _full_name(name or _EVENT_BASENAME))
                _sa_keepalive = sa_pack  # noqa: F841 (buffers live till return)
            else:
                import ctypes
                h = k.CreateEventW(None, False, False,
                                   _full_name(name or _EVENT_BASENAME))
            if not h:
                return None
        # drain one stale pulse (auto-reset collapses N sets to one)
        try:
            k.WaitForSingleObject(h, 0)
        except Exception:
            pass
        if name is None:
            _state["event"] = h
        return h
    except Exception:
        return None


def signal_existing(name: str | None = None) -> bool:
    """Loser side: pulse the owner's show-event (focus it). Returns True
    when the pulse was delivered. Best-effort — False just means the
    loser exits quietly without focusing anyone."""
    if not _is_windows():
        return False
    h = None
    try:
        h = _open_show_event(name, write=True)
        if not h:
            return False
        rc = _k().SetEvent(h)
        return bool(rc)
    except Exception:
        return False
    finally:
        if h:
            try:
                _k().CloseHandle(h)
            except Exception:
                pass


def check_signalled(name: str | None = None) -> bool:
    """Winner poll (Tk thread): True once per delivered pulse. Drains the
    auto-reset event; never raises. `name` overrides the event basename
    (tests use unique names so they can never touch the real handoff)."""
    h, _close = None, False
    try:
        if name is None:
            h = ensure_show_event()
        else:
            h = _open_show_event(name)
            _close = h is not None
        if not h:
            return False
        return _k().WaitForSingleObject(h, 0) == _WAIT_OBJECT_0
    except Exception:
        return False
    finally:
        if _close and h:
            try:
                _k().CloseHandle(h)
            except Exception:
                pass


def is_elevation_child(argv=None) -> bool:
    """True when this process is the elevated half of the Admin-Gate
    handshake (carries --elevation-token=). It must never exit as a
    'second instance' — but it should still claim the mutex once free
    (see claim_in_background)."""
    try:
        for a in (argv if argv is not None else sys.argv):
            if str(a).startswith("--elevation-token="):
                return True
    except Exception:
        pass
    return False


def is_headless_run(argv=None) -> bool:
    """True for scheduler/headless invocations that must run concurrently
    with (or without) the GUI: --auto-clean, --auto-update. Fully exempt
    from single-instance handling."""
    try:
        args = [str(a) for a in (argv if argv is not None else sys.argv)]
    except Exception:
        return False
    return "--auto-clean" in args or "--auto-update" in args


def should_enforce(argv=None) -> bool:
    """True when single-instance exit applies: a normal GUI launch. False
    for the elevation-handshake child, headless runs, and the explicit
    --no-single-instance escape hatch (tests / debugging)."""
    try:
        args = [str(a) for a in (argv if argv is not None else sys.argv)]
    except Exception:
        return True
    if "--no-single-instance" in args:
        return False
    if is_elevation_child(args) or is_headless_run(args):
        return False
    return True


def claim_in_background(timeout_s: float = 70.0, name: str | None = None) -> None:
    """Elevation-child path: the unelevated parent still holds the mutex
    while it waits out the handshake, so a blocking claim would stall the
    child's window. Claim off-thread instead — normally free within a
    second of the parent's exit; give up quietly after the timeout
    (degraded to today's behavior: twins theoretically possible, nothing
    else changes). Idempotent per process."""
    if not _is_windows():
        return
    try:
        if _state.get("bg_claim") is not None:
            return
    except Exception:
        return

    def _run():
        import time as _t
        deadline = _t.monotonic() + max(1.0, timeout_s)
        while _t.monotonic() < deadline:
            try:
                h = acquire(name)
                if h is not None:
                    set_owner(h)
                    ensure_show_event()
                    return
            except Exception:
                pass
            try:
                _t.sleep(0.5)
            except Exception:
                return
    try:
        th = threading.Thread(target=_run, daemon=True, name="SingleInstanceClaim")
        _state["bg_claim"] = th
        th.start()
    except Exception:
        pass


def poll_enabled() -> bool:
    """False on exotic interpreters only; the Tk poll is otherwise always
    safe to start (best-effort inside)."""
    return _is_windows()
