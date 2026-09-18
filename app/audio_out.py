"""
Speaker output engine (Tools tab, Speaker Test card) — Tk-free.

Two stdlib-only (ctypes + wave + struct + math) pieces:

1. Render-endpoint enumeration + default switching via the same
raw-ctypes COM vtable pattern Mic Check already uses for capture
(app/gui.py `_mm_*`): IMMDeviceEnumerator::EnumAudioEndpoints with
eRender, friendly names via the property store, and the default flip
via IPolicyConfig::SetDefaultEndpoint (vtable slot 13, stable since
Vista) on CPolicyConfigClient. Every HRESULT is checked — any failure
returns False/[] and the dialog falls back to Sound settings. Never
raises.

2. Directional test tones: short stereo WAVs (bytes) with
constant-power panning, played with winsound SND_MEMORY. Front row is
true-panned; side/rear rows are level-stepped simulations for stereo
setups and are labeled as such in-UI — never a fake surround claim.
"""

from __future__ import annotations

import math
import struct
import wave
import io

# --------------------------------------------------------------------------- #
# Raw-ctypes COM plumbing (mirrors gui.py `_mm_*` — engine must not import gui)
# Reads are registry (COM-free); only default-ID + flip use COM direct.
# --------------------------------------------------------------------------- #

_MM_CLSID_ENUM = "BCDE0395-E52F-467C-8E3D-C4579291692E"
_MM_IID_ENUM = "A95664D2-9614-4F35-A746-DE8DB63617E6"
_ERENDER = 0

_CLSID_POLICYCONFIG = "870af99c-171d-4f9e-af0d-e63df40c2bc9"
_IID_POLICYCONFIG = "f8679f50-850a-41cf-9c72-430f290290c8"
_POLICY_SET_DEFAULT_SLOT = 13  # IPolicyConfig::SetDefaultEndpoint

_DEVICE_STATE_ACTIVE = 0x1

_ROLE_CONSOLE = 0
_ROLE_MULTIMEDIA = 1
_ROLE_COMMUNICATIONS = 2


def _guid(s):
    import ctypes as _ct
    from ctypes import wintypes as _wt
    import uuid as _uuid

    class _GUID(_ct.Structure):
        _fields_ = [("Data1", _wt.DWORD), ("Data2", _wt.WORD),
                    ("Data3", _wt.WORD), ("Data4", _ct.c_ubyte * 8)]

    u = _uuid.UUID(s)
    return _GUID(u.time_low, u.time_mid, u.time_hi_version,
                 (_ct.c_ubyte * 8)(*u.bytes[8:]))


def _vfn(ct, iface, index, restype, *argtypes):
    vt = ct.c_void_p.from_address(iface).value
    addr = (ct.c_void_p * (index + 1)).from_address(vt)[index]
    return ct.WINFUNCTYPE(restype, ct.c_void_p, *argtypes)(addr)


def _co_create(clsid, iid):
    """CoCreateInstance(CLSCTX_INPROC_SERVER) -> c_void_p or None."""
    import ctypes as _ct
    try:
        _ole = _ct.windll.ole32
        try:
            _ole.CoInitialize(None)
        except Exception:
            pass
        _obj = _ct.c_void_p()
        if _ole.CoCreateInstance(
                _ct.byref(_guid(clsid)), None, 0x1,
                _ct.byref(_guid(iid)), _ct.byref(_obj)) != 0:
            return None
        return _obj.value
    except Exception:
        return None


def _release(ct, iface):
    try:
        _vfn(ct, iface, 2, ct.c_ulong)(iface)
    except Exception:
        pass


def _reg_render_endpoints():
    """[{id, name, state}] straight from the MMDevices registry hive —
    the same source repair_tasks.list_capture_devices uses for inputs.

    Deliberately COM-free: endpoint ACTIVATION can wedge process-wide
    after display queries on some multimedia stacks (observed: every
    CoCreateInstance wedges while plain registry/user32 calls sail
    through), and a frozen picker helps nobody. Reads are instant and
    need no admin. Never raises."""
    out = []
    try:
        import winreg as _wr
        base = "SOFTWARE\\Microsoft\\Windows\\CurrentVersion" \
               "\\MMDevices\\Audio\\Render"
        with _wr.OpenKey(_wr.HKEY_LOCAL_MACHINE, base, 0,
                         _wr.KEY_READ) as root:
            n = _wr.QueryInfoKey(root)[0]
            for i in range(n):
                try:
                    sub = _wr.EnumKey(root, i)
                except OSError:
                    continue
                try:
                    with _wr.OpenKey(root, sub, 0, _wr.KEY_READ) as ek:
                        try:
                            state, _ = _wr.QueryValueEx(ek, "DeviceState")
                            state = int(state)
                        except OSError:
                            state = -1
                        name = ""
                        try:
                            with _wr.OpenKey(ek, "Properties") as pk:
                                try:
                                    name, _ = _wr.QueryValueEx(
                                        pk, "{a45c254e-df1c-4efd-8020-67d146a850e0},2")
                                except OSError:
                                    pass
                                # Interface name disambiguates identical
                                # labels ("Speakers" x3) — same join the
                                # capture list uses: "Speakers (USB Audio)".
                                try:
                                    iface, _ = _wr.QueryValueEx(
                                        pk, "{b3f8fa53-0004-438e-9003-51a46e139bfc},6")
                                except OSError:
                                    iface = ""
                                if iface and iface != name:
                                    name = f"{name} ({iface})" if name else str(iface)
                        except OSError:
                            pass
                        out.append({"id": "{0.0.0.00000000}." + sub,
                                    "name": str(name or "") or sub,
                                    "state": state})
                except OSError:
                    continue
    except Exception:
        pass
    return out


def list_render_endpoints(timeout_s=8.0):
    """[{id, name, state}] for every render endpoint. Never raises.

    Registry reads (COM-free by design — see _reg_render_endpoints);
    timeout_s is accepted for API compatibility and ignored."""
    try:
        return list(_reg_render_endpoints())
    except Exception:
        return []


def default_render_id(timeout_s=8.0):
    """Endpoint-ID of the default render output, or ''.

    Direct COM (like Mic Check's capture path — fast when the stack is
    healthy, which is every real machine; the enumeration above stays
    COM-free regardless). timeout_s is accepted for API compatibility
    and ignored. Never raises."""
    try:
        import ctypes as _ct
        _obj = _co_create(_MM_CLSID_ENUM, _MM_IID_ENUM)
        if not _obj:
            return ""
        try:
            _ole = _ct.windll.ole32
            _get_default = _vfn(_ct, _obj, 4, _ct.HRESULT, _ct.c_int,
                                _ct.c_int, _ct.POINTER(_ct.c_void_p))
            _dev = _ct.c_void_p()
            if _get_default(_obj, _ERENDER, _ROLE_MULTIMEDIA,
                            _ct.byref(_dev)) != 0:
                return ""
            try:
                _get_id = _vfn(_ct, _dev.value, 5, _ct.HRESULT,
                               _ct.POINTER(_ct.c_void_p))
                _pid = _ct.c_void_p()
                if _get_id(_dev.value, _ct.byref(_pid)) != 0 or not _pid.value:
                    return ""
                try:
                    return str(_ct.wstring_at(_pid.value) or "")
                finally:
                    try:
                        _ole.CoTaskMemFree(_pid)
                    except Exception:
                        pass
            finally:
                _release(_ct, _dev.value)
        finally:
            _release(_ct, _obj)
    except Exception:
        return ""
    return ""


def set_default_render(endpoint_id):
    """Flip the Windows default output to endpoint_id (all 3 roles).

    Every SetDefaultEndpoint HRESULT must be S_OK or this returns False
    (fail-closed — a half-flipped default is worse than none). Direct
    COM; the Speaker dialog no longer calls this (per-device playback
    replaced the flip) — kept as a tested utility. Never raises."""
    if not endpoint_id:
        return False
    try:
        import ctypes as _ct
        _obj = _co_create(_CLSID_POLICYCONFIG, _IID_POLICYCONFIG)
        if not _obj:
            return False
        try:
            _set_default = _vfn(_ct, _obj, _POLICY_SET_DEFAULT_SLOT,
                                _ct.HRESULT, _ct.c_wchar_p, _ct.c_ulong)
            for _role in (_ROLE_CONSOLE, _ROLE_MULTIMEDIA,
                          _ROLE_COMMUNICATIONS):
                try:
                    if _set_default(_obj, str(endpoint_id), _role) != 0:
                        return False
                except Exception:
                    return False
            return True
        finally:
            _release(_ct, _obj)
    except Exception:
        return False


# --------------------------------------------------------------------------- #
# Directional test tones — stereo WAV bytes, constant-power panning
# --------------------------------------------------------------------------- #

_SAMPLE_RATE = 44100
_TONE_HZ = 660.0
_TONE_S = 0.45

# (key, label, pan -1..+1, level_db, freq_hz) — every direction plays
# at the SAME loudness (like a real Dolby test: position, not volume,
# tells you which speaker it is). Rows differ in PITCH so all 8 stay
# distinct on plain stereo, where side/rear placement is simulated —
# the dialog footnotes exactly that. Never a fake surround claim.
DIRECTIONS = (
    ("FL", "Front Left", -1.0, 0.0, 660.0),
    ("FC", "Front Center", 0.0, 0.0, 660.0),
    ("FR", "Front Right", 1.0, 0.0, 660.0),
    ("SL", "Side Left", -1.0, 0.0, 550.0),
    ("SR", "Side Right", 1.0, 0.0, 550.0),
    ("RL", "Rear Left", -1.0, 0.0, 440.0),
    ("RC", "Rear Center", 0.0, 0.0, 440.0),
    ("RR", "Rear Right", 1.0, 0.0, 440.0),
)


def directional_wav(pan, level_db, duration_s=_TONE_S, freq_hz=_TONE_HZ):
    """Stereo WAV bytes with the tone panned (constant-power) and scaled.

    Pure computation — no audio device touched. Never raises (b""
    on any failure; the caller then reports instead of playing)."""
    try:
        pan = max(-1.0, min(1.0, float(pan)))
        amp = 0.5 * (10.0 ** (float(level_db) / 20.0))
        freq = max(50.0, min(8000.0, float(freq_hz)))
        # constant-power: angle across the stereo field
        ang = (pan + 1.0) * math.pi / 4.0
        gl, gr = math.cos(ang) * amp, math.sin(ang) * amp
        n = max(1, int(_SAMPLE_RATE * float(duration_s)))
        fade = max(1, int(_SAMPLE_RATE * 0.01))
        frames = bytearray()
        for i in range(n):
            t = i / _SAMPLE_RATE
            s = math.sin(2.0 * math.pi * freq * t)
            env = min(1.0, i / fade, (n - 1 - i) / fade)
            frames += struct.pack("<hh",
                                  int(max(-1.0, min(1.0, s * gl * env)) * 32767),
                                  int(max(-1.0, min(1.0, s * gr * env)) * 32767))
        buf = io.BytesIO()
        with wave.open(buf, "wb") as wh:
            wh.setnchannels(2)
            wh.setsampwidth(2)
            wh.setframerate(_SAMPLE_RATE)
            wh.writeframes(bytes(frames))
        return buf.getvalue()
    except Exception:
        return b""

def play_wav_bytes(data):
    """Play WAV bytes on the default output. True = played, False = not.

    winsound SND_MEMORY only — never writes a file. Never raises."""
    if not data:
        return False
    try:
        import winsound
        winsound.PlaySound(data, winsound.SND_MEMORY)
        return True
    except Exception:
        return False


# --------------------------------------------------------------------------- #
# Per-device playback — test any output WITHOUT flipping the default
# --------------------------------------------------------------------------- #

def _norm_name(s):
    import re as _re
    return _re.sub(r"[^a-z0-9]+", "", str(s or "").lower())


def waveout_devices():
    """[(waveout_index, name)] for every playback device, via winmm.

    Names come from waveOutGetDevCaps (truncated to ~31 chars by the
    API itself). Never raises; [] means none found."""
    try:
        import ctypes as _ct
        winmm = _ct.windll.winmm
        n = winmm.waveOutGetNumDevs()
    except Exception:
        return []
    out = []
    try:
        class _CAPS(_ct.Structure):
            _fields_ = [("wMid", _ct.c_ushort), ("wPid", _ct.c_ushort),
                        ("vDriverVersion", _ct.c_uint),
                        ("szPname", _ct.c_char * 32),
                        ("dwFormats", _ct.c_uint),
                        ("wChannels", _ct.c_ushort),
                        ("wReserved1", _ct.c_ushort),
                        ("dwSupport", _ct.c_uint)]

        for i in range(max(0, int(n))):
            try:
                caps = _CAPS()
                if winmm.waveOutGetDevCapsA(i, _ct.byref(caps),
                                            _ct.sizeof(caps)) != 0:
                    continue
                try:
                    name = bytes(caps.szPname).split(b"\x00")[0].decode(
                        "mbcs", "replace")
                except Exception:
                    name = f"Output {i + 1}"
                out.append((i, name))
            except Exception:
                continue
    except Exception:
        pass
    return out


def match_waveout(endpoint_name, endpoint_id=""):
    """Best waveOut index for an MMDevice endpoint, or None.

    Matches on normalized friendly names (prefix either way — caps
    names truncate). Never raises."""
    try:
        want = _norm_name(endpoint_name)
        if not want:
            return None
        best, best_len = None, 0
        for idx, name in waveout_devices():
            norm = _norm_name(name)
            if not norm:
                continue
            if norm.startswith(want) or want.startswith(norm):
                if len(norm) > best_len:
                    best, best_len = idx, len(norm)
        return best
    except Exception:
        return None


def play_wav_on_device(data, waveout_index):
    """Play WAV bytes on one waveOut device (no default flip).

    Returns (played_bool, used_default_bool): used_default is True when
    the exact device couldn't be opened and winsound covered for it —
    the dialog says so honestly. Never raises, never writes a file."""
    if not data:
        return False, False
    # Exact path first: winmm waveOut on the device index.
    try:
        import ctypes as _ct
        import io as _io
        import wave as _wave
        winmm = _ct.windll.winmm
        with _wave.open(_io.BytesIO(bytes(data)), "rb") as wh:
            nch, sw, rate = wh.getnchannels(), wh.getsampwidth(), wh.getframerate()
            pcm = wh.readframes(wh.getnframes())

        class _FMT(_ct.Structure):
            _fields_ = [("wFormatTag", _ct.c_ushort),
                        ("nChannels", _ct.c_ushort),
                        ("nSamplesPerSec", _ct.c_uint),
                        ("nAvgBytesPerSec", _ct.c_uint),
                        ("nBlockAlign", _ct.c_ushort),
                        ("wBitsPerSample", _ct.c_ushort),
                        ("cbSize", _ct.c_ushort)]

        class _HDR(_ct.Structure):
            _fields_ = [("lpData", _ct.c_char_p),
                        ("dwBufferLength", _ct.c_uint),
                        ("dwBytesRecorded", _ct.c_uint),
                        ("dwUser", _ct.c_void_p),
                        ("dwFlags", _ct.c_uint),
                        ("dwLoops", _ct.c_uint),
                        ("lpNext", _ct.c_void_p),
                        ("reserved", _ct.c_void_p)]

        fmt = _FMT()
        fmt.wFormatTag = 1
        fmt.nChannels = nch
        fmt.nSamplesPerSec = rate
        fmt.wBitsPerSample = sw * 8
        fmt.nBlockAlign = nch * sw
        fmt.nAvgBytesPerSec = rate * nch * sw
        fmt.cbSize = 0
        hwo = _ct.c_void_p()
        if winmm.waveOutOpen(_ct.byref(hwo), int(waveout_index),
                             _ct.byref(fmt), 0, 0, 0) != 0:
            raise OSError("open failed")
        try:
            raw = _ct.create_string_buffer(bytes(pcm), len(pcm))
            hd = _HDR()
            hd.lpData = _ct.cast(raw, _ct.c_char_p)
            hd.dwBufferLength = len(pcm)
            if winmm.waveOutPrepareHeader(hwo, _ct.byref(hd),
                                          _ct.sizeof(hd)) != 0:
                raise OSError("prepare failed")
            try:
                if winmm.waveOutWrite(hwo, _ct.byref(hd),
                                      _ct.sizeof(hd)) != 0:
                    raise OSError("write failed")
                import time as _time
                dur = len(pcm) / max(1, rate * nch * sw)
                t0 = _time.perf_counter()
                while _time.perf_counter() - t0 < dur + 2.0:
                    _time.sleep(0.05)
                    try:
                        # WAVERR_STILLPLAYING (33) while sounding
                        if winmm.waveOutUnprepareHeader(
                                hwo, _ct.byref(hd), _ct.sizeof(hd)) == 0:
                            break
                    except Exception:
                        break
            finally:
                try:
                    winmm.waveOutUnprepareHeader(hwo, _ct.byref(hd),
                                                _ct.sizeof(hd))
                except Exception:
                    pass
        finally:
            try:
                winmm.waveOutClose(hwo)
            except Exception:
                pass
        return True, False
    except Exception:
        pass
    # Honest fallback: default output, flagged as such.
    return play_wav_bytes(data), True
