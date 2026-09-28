"""XInput gamepad access via ctypes.

H4. Extracted verbatim from app/gui.py. Tk-free and dependency-free apart
from the stdlib, so the gamepad dialog and the capture pump can share it
without pulling in the UI layer.

Every ctypes signature here is deliberately left unbound; that is the
pre-existing behaviour and is recorded as a known item rather than changed
mid-move (see references/Hardening findings 2026-09-27, "app/elevation.py is
the one file in scope that never bound its ctypes signatures").
"""
from __future__ import annotations

# The ctypes Structures (_Pad / _State / _Vibration) are defined INSIDE the
# functions that use them, exactly as they were in app/gui.py. They are not
# hoisted to module scope here: doing so would add module-level names the
# original never had, and a struct whose layout silently disagrees with the
# DLL's is a memory-corruption bug rather than a style question.

_XINPUT_BUTTONS = (
    ("A", 0x1000), ("B", 0x2000), ("X", 0x4000), ("Y", 0x8000),
    ("LB", 0x0100), ("RB", 0x0200), ("Back", 0x0020), ("Start", 0x0010),
    ("L-Stick", 0x0040), ("R-Stick", 0x0080),
    ("Up", 0x0001), ("Down", 0x0002), ("Left", 0x0004), ("Right", 0x0008),
)
_XINPUT_STICK_DEADZONE = 7849
_XINPUT_TRIGGER_THRESHOLD = 30
_XINPUT_RUMBLE_MS = 1000


def _xinput_lib():
    """Loaded XInput DLL or None (resolved once, shared by poll+rumble)."""
    cached = getattr(_xinput_lib, "_dll", "unset")
    if cached != "unset":
        return cached
    dll = None
    try:
        import ctypes as _ct
        for _name in ("xinput1_4.dll", "xinput9_1_0.dll", "xinput1_3.dll"):
            try:
                dll = _ct.windll.LoadLibrary(_name)
                break
            except Exception:
                continue
    except Exception:
        dll = None
    _xinput_lib._dll = dll
    return dll


def _xinput_state_fn():
    """XInputGetState callable(slot) -> state dict (with packet number)
    or None (no XInput on this machine).

    Read-only by construction — only GetState is ever bound here."""
    cached = getattr(_xinput_state_fn, "_fn", "unset")
    if cached != "unset":
        return cached
    fn = None
    try:
        import ctypes as _ct
        from ctypes import wintypes as _wt

        class _Pad(_ct.Structure):
            _fields_ = [("wButtons", _wt.WORD),
                        ("bLeftTrigger", _ct.c_ubyte),
                        ("bRightTrigger", _ct.c_ubyte),
                        ("sThumbLX", _ct.c_short),
                        ("sThumbLY", _ct.c_short),
                        ("sThumbRX", _ct.c_short),
                        ("sThumbRY", _ct.c_short)]

        class _State(_ct.Structure):
            _fields_ = [("dwPacketNumber", _wt.DWORD),
                        ("Gamepad", _Pad)]

        dll = _xinput_lib()
        if dll is not None and hasattr(dll, "XInputGetState"):
            _raw = dll.XInputGetState
            _raw.argtypes = [_wt.DWORD, _ct.POINTER(_State)]
            _raw.restype = _wt.DWORD

            def fn(slot):  # noqa: F811
                st = _State()
                try:
                    rc = _raw(_wt.DWORD(slot), _ct.byref(st))
                except Exception:
                    return None
                if rc != 0:  # 1167 = not connected; anything else: no data
                    return None
                g = st.Gamepad
                return {"packet": int(st.dwPacketNumber),
                        "buttons": int(g.wButtons),
                        "lt": int(g.bLeftTrigger), "rt": int(g.bRightTrigger),
                        "lx": int(g.sThumbLX), "ly": int(g.sThumbLY),
                        "rx": int(g.sThumbRX), "ry": int(g.sThumbRY)}
    except Exception:
        fn = None
    _xinput_state_fn._fn = fn
    return fn


def _xinput_rumble(slot, weak, strong):
    """Vibrate (weak/strong 0.0..1.0) or stop (0,0). Returns True if the
    device accepted it. Device output only — user-initiated test path."""
    try:
        import ctypes as _ct
        from ctypes import wintypes as _wt

        class _Vib(_ct.Structure):
            _fields_ = [("wLeftMotorSpeed", _wt.WORD),
                        ("wRightMotorSpeed", _wt.WORD)]

        dll = _xinput_lib()
        if dll is None or not hasattr(dll, "XInputSetState"):
            return False
        _raw = dll.XInputSetState
        _raw.argtypes = [_wt.DWORD, _ct.POINTER(_Vib)]
        _raw.restype = _wt.DWORD
        vib = _Vib(int(max(0.0, min(1.0, weak)) * 65535),
                   int(max(0.0, min(1.0, strong)) * 65535))
        return _raw(_wt.DWORD(slot), _ct.byref(vib)) == 0
    except Exception:
        return False
