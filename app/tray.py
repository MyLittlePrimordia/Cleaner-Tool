"""
System-tray icon (Phase 3 of tray/auto-elevate work).

Resident watchdog UI: the app's disk monitor and Auto-Pilot watcher only
live while the GUI process runs, so closing the window used to kill both.
The tray keeps the process alive with a 4-item menu instead.

stdlib + ctypes only (zero third-party runtime deps by design — no
pystray). Never raises out of start/stop/menu paths: a tray that cannot
exist (no shell, locked-down session) degrades to "no icon" and the app
behaves exactly as before (close quits via the normal path).

Threading: the hidden callback window lives on its own daemon thread
with a GetMessage loop (Tk must never block on it and it must never
touch Tk). Every menu action hops to the UI thread through the
`on_ui_thread` callable the host supplies (Application passes a
root.after wrapper); with on_ui_thread=None callbacks run inline (tests).

(q)uirks handled:
  * TrackPopupMenu needs a foreground window that is NOT message-only,
    so the callback window is a real (never-shown) overlapped window.
  * TPM_RETURNCMD makes selection synchronous — no WM_COMMAND plumbing.
  * TaskbarCreated re-adds the icon after an explorer restart/crash.
  * stop() destroys the window on its owner thread, joins bounded, and
    removes the icon first so no corpse icon lingers.
"""

from __future__ import annotations

import sys
import threading

WM_APP = 0x8000
WM_TRAYICON = WM_APP + 1
WM_APP_DESTROY = WM_APP + 2
WM_COMMAND = 0x0111
WM_NULL = 0x0000
WM_QUIT = 0x0012
WM_LBUTTONUP = 0x0202
WM_RBUTTONUP = 0x0205

NIM_ADD = 0x00000000
NIM_MODIFY = 0x00000001
NIM_DELETE = 0x00000002

NIF_MESSAGE = 0x00000001
NIF_ICON = 0x00000002
NIF_TIP = 0x00000004

MF_STRING = 0x00000000
MF_SEPARATOR = 0x00000800

TPM_RIGHTBUTTON = 0x0002
TPM_RETURNCMD = 0x0100

IDI_APPLICATION = 32512
IMAGE_ICON = 1
LR_LOADFROMFILE = 0x00000010

_TRAY_CLASS = "CleanerToolTrayWindow"

_PTR64 = None  # lazy: True on 64-bit interpreters


def _is_windows() -> bool:
    try:
        return sys.platform.startswith("win")
    except Exception:
        return False


def _types():
    """Explicit ctypes binding (64-bit-correct Handles/messages).

    ctypes defaults every foreign call through 32-bit c_int: real
    HWND/HANDLE/LPARAM values overflow (OverflowError inside the window
    proc, silently-wrong CloseHandle targets). Every function this module
    touches is bound here once, with pointer-width WPARAM/LPARAM/LRESULT.
    """
    import ctypes
    wide = ctypes.sizeof(ctypes.c_void_p) == 8
    T = {
        "HWND": ctypes.c_void_p, "HANDLE": ctypes.c_void_p,
        "HINSTANCE": ctypes.c_void_p, "HICON": ctypes.c_void_p,
        "HMENU": ctypes.c_void_p,
        "UINT": ctypes.c_uint, "DWORD": ctypes.c_uint32,
        "BOOL": ctypes.c_int, "LONG": ctypes.c_int32,
        "WPARAM": ctypes.c_uint64 if wide else ctypes.c_uint,
        "LPARAM": ctypes.c_int64 if wide else ctypes.c_long,
        "LRESULT": ctypes.c_int64 if wide else ctypes.c_long,
    }
    u = ctypes.windll.user32
    s = ctypes.windll.shell32
    k = ctypes.windll.kernel32
    H, U, W, L, R, D, B = (T["HWND"], T["UINT"], T["WPARAM"], T["LPARAM"],
                           T["LRESULT"], T["DWORD"], T["BOOL"])
    u.DefWindowProcW.argtypes = [H, U, W, L]
    u.DefWindowProcW.restype = R
    u.RegisterClassW.argtypes = [ctypes.c_void_p]
    u.RegisterClassW.restype = ctypes.c_ushort
    u.CreateWindowExW.argtypes = [D, ctypes.c_wchar_p, ctypes.c_wchar_p,
                                  D, ctypes.c_int, ctypes.c_int,
                                  ctypes.c_int, ctypes.c_int,
                                  H, H, H, ctypes.c_void_p]
    u.CreateWindowExW.restype = H
    u.DestroyWindow.argtypes = [H]
    u.DestroyWindow.restype = B
    u.PostMessageW.argtypes = [H, U, W, L]
    u.PostMessageW.restype = B
    u.PostQuitMessage.argtypes = [ctypes.c_int]
    u.PostQuitMessage.restype = None
    u.GetMessageW.argtypes = [ctypes.c_void_p, H, U, U]
    u.GetMessageW.restype = B
    u.TranslateMessage.argtypes = [ctypes.c_void_p]
    u.TranslateMessage.restype = B
    u.DispatchMessageW.argtypes = [ctypes.c_void_p]
    u.DispatchMessageW.restype = R
    u.GetCursorPos.argtypes = [ctypes.c_void_p]
    u.GetCursorPos.restype = B
    u.SetForegroundWindow.argtypes = [H]
    u.SetForegroundWindow.restype = B
    u.TrackPopupMenuEx.argtypes = [H, U, ctypes.c_int, ctypes.c_int, H,
                                   ctypes.c_void_p]
    u.TrackPopupMenuEx.restype = ctypes.c_int
    u.CreatePopupMenu.argtypes = []
    u.CreatePopupMenu.restype = H
    u.AppendMenuW.argtypes = [H, U, ctypes.c_void_p, ctypes.c_wchar_p]
    u.AppendMenuW.restype = B
    u.DestroyMenu.argtypes = [H]
    u.DestroyMenu.restype = B
    u.LoadIconW.argtypes = [H, ctypes.c_void_p]
    u.LoadIconW.restype = H
    u.LoadImageW.argtypes = [H, ctypes.c_wchar_p, U, ctypes.c_int,
                             ctypes.c_int, U]
    u.LoadImageW.restype = H
    u.DestroyIcon.argtypes = [H]
    u.DestroyIcon.restype = B
    u.RegisterWindowMessageW.argtypes = [ctypes.c_wchar_p]
    u.RegisterWindowMessageW.restype = U
    s.ExtractIconW.argtypes = [H, ctypes.c_wchar_p, U]
    s.ExtractIconW.restype = H
    s.Shell_NotifyIconW.argtypes = [D, ctypes.c_void_p]
    s.Shell_NotifyIconW.restype = B
    k.GetThreadId.argtypes = [H]
    k.GetThreadId.restype = D
    k.CloseHandle.argtypes = [H]
    k.CloseHandle.restype = B
    k.GetLastError.argtypes = []
    k.GetLastError.restype = D
    k.GetModuleHandleW.argtypes = [ctypes.c_wchar_p]
    k.GetModuleHandleW.restype = H
    return T


_TYPES = None


def _T():
    global _TYPES
    if _TYPES is None:
        _TYPES = _types()
    return _TYPES


class TrayIcon:
    """One tray icon + popup menu. See module docstring for the model."""

    def __init__(self, tooltip="Cleaner Tool", items=None, on_ui_thread=None,
                 icon_path=None):
        """
        items: [(cmd_id:int, text:str, callback:callable|None), ...] —
            text None makes a separator. Callbacks run on the UI thread
            (via on_ui_thread) or inline when on_ui_thread is None.
        icon_path: preferred .ico file (frozen exe icon tried first).
        """
        self.tooltip = tooltip or "Cleaner Tool"
        self.items = list(items or [])
        self._ui = on_ui_thread
        self._icon_path = icon_path
        self._hwnd = None
        self._hicon = None
        self._icon_owned = False
        self._hmenu = None
        self._callbacks = {}
        self._thread = None
        self._thread_id = None
        self._taskbar_created_msg = 0
        self._running = False
        self._wndproc_ref = None  # keep the CFUNCTYPE alive

    # ---------------- public ---------------- #

    @property
    def running(self) -> bool:
        return bool(self._running and self._hwnd)

    def start(self) -> bool:
        """Create window + menu + icon and spin the loop. Idempotent.

        The window is born on the loop thread (never the caller): Win32
        delivers a window's messages to its creator thread's queue, so a
        caller-thread window pumped nowhere would strand its messages —
        and a stray WM_QUIT in Tk's own queue can starve Tk timers
        outright (proven: frozen pill-switcher animation in the suite).
        Readiness crosses back via an Event; failures clean up fully."""
        if not _is_windows():
            return False
        if self.running:
            return True
        try:
            ready = threading.Event()
            outcome = {}
            th = threading.Thread(target=self._thread_main, daemon=True,
                                  args=(ready, outcome), name="TrayIconLoop")
            th.start()
            if not ready.wait(timeout=8.0):
                try:
                    th.join(timeout=1.0)
                except Exception:
                    pass
                self._cleanup_threadless()
                return False
            if not outcome.get("ok"):
                try:
                    th.join(timeout=1.0)
                except Exception:
                    pass
                self._cleanup_threadless()
                return False
            self._thread = th
            try:
                import ctypes
                self._thread_id = ctypes.windll.kernel32.GetThreadId(
                    ctypes.c_void_p(th.ident))
            except Exception:
                self._thread_id = None
            self._running = True
            return True
        except Exception:
            try:
                self._cleanup_threadless()
            except Exception:
                pass
            return False

    def _thread_main(self, ready, outcome) -> None:
        """Loop-thread entry: own the window end-to-end, then pump."""
        try:
            if not self._create_window():
                outcome["ok"] = False
                ready.set()
                return
            if not self._create_menu():
                outcome["ok"] = False
                ready.set()
                return
            if not self._load_icon():
                outcome["ok"] = False
                ready.set()
                return
            if not self._add_icon():
                outcome["ok"] = False
                ready.set()
                return
            outcome["ok"] = True
            outcome["hwnd"] = self._hwnd
            ready.set()
            self._loop()
        except Exception:
            try:
                outcome["ok"] = False
            except Exception:
                pass
            try:
                ready.set()
            except Exception:
                pass

    def _cleanup_threadless(self) -> None:
        """Start-failure path (caller thread): drop menu/icon handles.
        The window, if half-built, dies with its loop thread's exit."""
        try:
            self._delete_icon()
        except Exception:
            pass
        self._hwnd = None
        try:
            if self._hmenu:
                import ctypes
                ctypes.windll.user32.DestroyMenu(self._hmenu)
        except Exception:
            pass
        self._hmenu = None
        try:
            if self._hicon and self._icon_owned:
                import ctypes
                ctypes.windll.user32.DestroyIcon(self._hicon)
        except Exception:
            pass
        self._hicon = None
        self._icon_owned = False

    def stop(self) -> None:
        """Remove the icon, destroy the window on its owner thread, join
        bounded. Idempotent, never raises (safe to call twice, safe when
        start() never ran or failed halfway)."""
        try:
            self._running = False
            self._delete_icon()
            hwnd, thread = self._hwnd, self._thread
            self._hwnd = None
            self._thread = None
            if hwnd:
                try:
                    import ctypes
                    ctypes.windll.user32.PostMessageW(hwnd, WM_APP_DESTROY, 0, 0)
                except Exception:
                    pass
            if thread is not None and thread.is_alive():
                try:
                    thread.join(timeout=2.5)
                except Exception:
                    pass
            try:
                if self._hmenu:
                    import ctypes
                    ctypes.windll.user32.DestroyMenu(self._hmenu)
            except Exception:
                pass
            self._hmenu = None
            try:
                if self._hicon and self._icon_owned:
                    import ctypes
                    ctypes.windll.user32.DestroyIcon(self._hicon)
            except Exception:
                pass
            self._hicon = None
            self._icon_owned = False
        except Exception:
            pass

    def update_tooltip(self, text) -> bool:
        """Best-effort tooltip refresh. Returns False when no icon lives."""
        if not self.running:
            return False
        try:
            import ctypes
            from ctypes import wintypes
            nid = self._nid(text or self.tooltip)
            return bool(ctypes.windll.shell32.Shell_NotifyIconW(
                NIM_MODIFY, ctypes.byref(nid)))
        except Exception:
            return False

    # ---------------- test seam ---------------- #

    def fire(self, cmd_id) -> bool:
        """Invoke a menu callback through the UI-thread hop (tests drive
        routing without synthesizing clicks)."""
        try:
            fn = self._callbacks.get(cmd_id)
            if fn is None:
                return False
            self._dispatch(fn)
            return True
        except Exception:
            return False

    # ---------------- internals ---------------- #

    def _dispatch(self, fn) -> None:
        try:
            if self._ui is not None:
                self._ui(fn)
            else:
                fn()
        except Exception:
            pass

    def _nid(self, tip):
        import ctypes
        from ctypes import wintypes

        class _NID(ctypes.Structure):
            _fields_ = [
                ("cbSize", wintypes.DWORD),
                ("hWnd", wintypes.HWND),
                ("uID", wintypes.UINT),
                ("uFlags", wintypes.UINT),
                ("uCallbackMessage", wintypes.UINT),
                ("hIcon", wintypes.HANDLE),
                ("szTip", wintypes.WCHAR * 128),
                ("dwState", wintypes.DWORD),
                ("dwStateMask", wintypes.DWORD),
                ("szInfo", wintypes.WCHAR * 256),
                ("uTimeout", wintypes.UINT),
                ("szInfoTitle", wintypes.WCHAR * 64),
                ("dwInfoFlags", wintypes.DWORD),
                ("guidItem", ctypes.c_ubyte * 16),
                ("hBalloonIcon", wintypes.HANDLE),
            ]
        nid = _NID()
        nid.cbSize = ctypes.sizeof(_NID)
        nid.hWnd = self._hwnd
        nid.uID = 1
        nid.uFlags = NIF_MESSAGE | NIF_ICON | NIF_TIP
        nid.uCallbackMessage = WM_TRAYICON
        nid.hIcon = self._hicon or 0
        nid.szTip = (tip or "")[:127]
        return nid

    def _create_window(self) -> bool:
        try:
            import ctypes
            from ctypes import wintypes
            T = _T()
            user32 = ctypes.windll.user32
            WNDPROC = ctypes.WINFUNCTYPE(T["LRESULT"], T["HWND"], T["UINT"],
                                         T["WPARAM"], T["LPARAM"])
            self._taskbar_created_msg = 0
            try:
                self._taskbar_created_msg = user32.RegisterWindowMessageW("TaskbarCreated")
            except Exception:
                pass

            def _proc(hwnd, msg, wp, lp):
                try:
                    return self._on_wnd_msg(hwnd, msg, wp, lp)
                except Exception:
                    try:
                        return user32.DefWindowProcW(hwnd, msg, wp, lp)
                    except Exception:
                        return 0
            self._wndproc_ref = WNDPROC(_proc)

            class _WC(ctypes.Structure):
                _fields_ = [("style", wintypes.UINT),
                            ("lpfnWndProc", ctypes.c_void_p),
                            ("cbClsExtra", ctypes.c_int),
                            ("cbWndExtra", ctypes.c_int),
                            ("hInstance", wintypes.HINSTANCE),
                            ("hIcon", wintypes.HANDLE),
                            ("hCursor", wintypes.HANDLE),
                            ("hbrBackground", wintypes.HANDLE),
                            ("lpszMenuName", wintypes.LPCWSTR),
                            ("lpszClassName", wintypes.LPCWSTR)]
            hinst = ctypes.windll.kernel32.GetModuleHandleW(None)
            wc = _WC()
            wc.style = 0
            wc.lpfnWndProc = ctypes.cast(self._wndproc_ref, ctypes.c_void_p)
            wc.cbClsExtra = 0
            wc.cbWndExtra = 0
            wc.hInstance = hinst
            wc.hIcon = 0
            wc.hCursor = 0
            wc.hbrBackground = 0
            wc.lpszMenuName = None
            wc.lpszClassName = _TRAY_CLASS
            atom = user32.RegisterClassW(ctypes.byref(wc))
            if not atom and ctypes.windll.kernel32.GetLastError() != 1410:
                return False
            # real (never-shown) overlapped window: message-only windows
            # cannot take foreground, and TrackPopupMenu needs it.
            hwnd = user32.CreateWindowExW(
                0, _TRAY_CLASS, "Cleaner Tool tray", 0,
                0, 0, 0, 0, None, None, hinst, None)
            if not hwnd:
                return False
            self._hwnd = hwnd
            return True
        except Exception:
            return False

    def _on_wnd_msg(self, hwnd, msg, wp, lp):
        import ctypes
        user32 = ctypes.windll.user32
        if msg == WM_TRAYICON:
            try:
                if lp == WM_RBUTTONUP:
                    self._show_menu()
                elif lp == WM_LBUTTONUP:
                    fn = self._callbacks.get("open")
                    if fn is not None:
                        self._dispatch(fn)
                return 0
            except Exception:
                return 0
        if msg == WM_APP_DESTROY:
            try:
                user32.DestroyWindow(hwnd)
            except Exception:
                pass
            try:
                user32.PostQuitMessage(0)
            except Exception:
                pass
            return 0
        try:
            if self._taskbar_created_msg and msg == self._taskbar_created_msg:
                self._add_icon()
                return 0
        except Exception:
            pass
        return user32.DefWindowProcW(hwnd, msg, wp, lp)

    def _create_menu(self) -> bool:
        try:
            import ctypes
            user32 = ctypes.windll.user32
            hmenu = user32.CreatePopupMenu()
            if not hmenu:
                return False
            for item in self.items:
                try:
                    cid, text, fn = item
                except Exception:
                    continue
                try:
                    if text is None:
                        user32.AppendMenuW(hmenu, MF_SEPARATOR, 0, None)
                    else:
                        if callable(text):
                            # live label (e.g. "Game Mode: On/Off"),
                            # re-evaluated every time the menu opens
                            try:
                                text = text()
                            except Exception:
                                text = "..."
                        if fn is not None:
                            self._callbacks[cid] = fn
                        user32.AppendMenuW(hmenu, MF_STRING, int(cid), str(text))
                except Exception:
                    continue
            # "open" alias for single-left-click (first callable wins)
            try:
                if "open" not in self._callbacks:
                    for cid, _t, fn in self.items:
                        if fn is not None:
                            self._callbacks["open"] = fn
                            break
            except Exception:
                pass
            self._hmenu = hmenu
            return True
        except Exception:
            return False

    def _show_menu(self) -> None:
        try:
            import ctypes
            user32 = ctypes.windll.user32
            if not self._hwnd or not self._hmenu:
                return
            from ctypes import wintypes

            # refresh live labels: rebuild the popup when any item text is
            # a callable (cheap; the old menu is destroyed after the swap)
            try:
                if any(callable(_i[1]) for _i in self.items):
                    _old = self._hmenu
                    if self._create_menu() and _old:
                        user32.DestroyMenu(_old)
            except Exception:
                pass

            class _PT(ctypes.Structure):
                _fields_ = [("x", wintypes.LONG), ("y", wintypes.LONG)]
            pt = _PT()
            if not user32.GetCursorPos(ctypes.byref(pt)):
                return
            # foreground dance: required for the menu to dismiss on
            # outside-click; WM_NULL afterwards clears the stuck state.
            user32.SetForegroundWindow(self._hwnd)
            sel = user32.TrackPopupMenuEx(
                self._hmenu, TPM_RIGHTBUTTON | TPM_RETURNCMD,
                pt.x, pt.y, self._hwnd, None)
            try:
                user32.PostMessageW(self._hwnd, WM_NULL, 0, 0)
            except Exception:
                pass
            if sel:
                fn = self._callbacks.get(int(sel))
                if fn is not None:
                    self._dispatch(fn)
        except Exception:
            pass

    def _icon_candidates(self) -> "list[str]":
        """Candidate bundled-icon.ico paths, most-preferred first. Pure
        Python (no ctypes) by design: this is the part of icon loading
        that's actually testable cross-platform, and the part that broke
        once already (a stray bare `os` reference silently NameError'd,
        swallowed by the caller's broad except, so the tray fell through
        to the plain stock icon on every single run — this function
        existing separately, with its own test, is exactly how that class
        of bug gets caught next time instead of shipping silently)."""
        import os as _os
        cands = []
        if self._icon_path:
            cands.append(self._icon_path)
        try:
            here = _os.path.dirname(_os.path.abspath(__file__))
            cands.append(_os.path.join(here, "assets", "icon.ico"))
        except Exception:
            pass
        return cands

    def _load_icon(self) -> bool:
        """3-tier fallback, ordered by reliability rather than "prefer the
        frozen exe first": a direct file load of the bundled .ico is a
        single, simple Win32 call with one obvious failure mode (path
        missing); ExtractIconW additionally depends on the exe's resource
        section being intact and shell32's resource loader succeeding,
        which is more moving parts for the same picture. Sized via
        GetSystemMetrics(SM_CXSMICON/SM_CYSMICON) rather than a hardcoded
        16px — Windows' actual small-icon slot is 20/24/32px at higher
        text-scaling settings, and asking LoadImageW for the wrong size
        silently stretches whatever nearest frame the .ico has instead of
        picking the crisp one already sized for it."""
        try:
            import ctypes
            import os as _os
            user32 = ctypes.windll.user32
            try:
                cx = user32.GetSystemMetrics(49)  # SM_CXSMICON
                cy = user32.GetSystemMetrics(50)  # SM_CYSMICON
                if cx <= 0 or cy <= 0:
                    cx = cy = 16
            except Exception:
                cx = cy = 16
            # 1. bundled icon.ico — works identically for source runs and
            # frozen builds (PyInstaller's --add-data places it alongside
            # this module under _MEIPASS/app/assets/icon.ico).
            try:
                for c in self._icon_candidates():
                    try:
                        if c and _os.path.isfile(c):
                            h = user32.LoadImageW(
                                None, c, IMAGE_ICON, cx, cy, LR_LOADFROMFILE)
                            if not h:
                                # retry at the file's own native size (0,0
                                # tells LoadImage "don't scale") in case the
                                # DPI-scaled request above is the problem,
                                # not the file itself.
                                h = user32.LoadImageW(
                                    None, c, IMAGE_ICON, 0, 0, LR_LOADFROMFILE)
                            if h:
                                self._hicon, self._icon_owned = h, True
                                return True
                    except Exception:
                        continue
            except Exception:
                pass
            # 2. frozen exe's own embedded icon (PyInstaller --icon), in
            # case the bundled-asset path above ever goes missing.
            try:
                if getattr(sys, "frozen", False):
                    h = ctypes.windll.shell32.ExtractIconW(
                        None, sys.executable, 0)
                    if h and int(h) > 32:
                        self._hicon, self._icon_owned = h, True
                        return True
            except Exception:
                pass
            # 3. stock application icon — an OS-guaranteed handle, so the
            # tray always gets *some* icon even if both paths above fail.
            try:
                h = user32.LoadIconW(None, IDI_APPLICATION)
                if h:
                    self._hicon, self._icon_owned = h, False
                    return True
            except Exception:
                pass
        except Exception:
            pass
        return False

    def _add_icon(self) -> bool:
        try:
            import ctypes
            nid = self._nid(self.tooltip)
            return bool(ctypes.windll.shell32.Shell_NotifyIconW(
                NIM_ADD, ctypes.byref(nid)))
        except Exception:
            return False

    def _delete_icon(self) -> None:
        try:
            if self._hwnd:
                import ctypes
                nid = self._nid(self.tooltip)
                try:
                    ctypes.windll.shell32.Shell_NotifyIconW(
                        NIM_DELETE, ctypes.byref(nid))
                except Exception:
                    pass
        except Exception:
            pass

    def _loop(self) -> None:
        try:
            import ctypes
            T = _T()
            from ctypes import wintypes

            class _MSG(ctypes.Structure):
                _fields_ = [("hwnd", T["HWND"]),
                            ("message", T["UINT"]),
                            ("wParam", T["WPARAM"]),
                            ("lParam", T["LPARAM"]),
                            ("time", T["DWORD"]),
                            ("pt_x", T["LONG"]),
                            ("pt_y", T["LONG"])]
            user32 = ctypes.windll.user32
            msg = _MSG()
            while True:
                try:
                    rc = user32.GetMessageW(ctypes.byref(msg), None, 0, 0)
                except Exception:
                    return
                if rc <= 0:
                    return
                try:
                    user32.TranslateMessage(ctypes.byref(msg))
                    user32.DispatchMessageW(ctypes.byref(msg))
                except Exception:
                    continue
        except Exception:
            pass
