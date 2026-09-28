"""Shared gamepad/keyboard input capture pump.

H4. Extracted verbatim from app/gui.py. Bridges the XInput and MMDevice
helpers into the counts the dialogs render (buttons pressed, stick deltas,
trigger travel, mic level). Tk-free.
"""
from __future__ import annotations

from app.ui.hardware.mmdev import (
    _MM_CLSID_ENUM, _MM_IID_AUDIOCLIENT, _MM_IID_ENUM,
    _MM_PKEY_FRIENDLY_FMT, _MM_PKEY_FRIENDLY_PID,
    _mm_ensure_com, _mm_guid, _mm_vfn,
)
from app.ui.hardware import xinput

def _pump_capture(endpoint_id, token):
    """Worker-thread capture keepalive: full COM chain (init included)
    runs HERE so STA objects never cross threads. Opens the endpoint's
    mix format in shared mode and STARTs it — a running stream is what
    makes drivers push audio to the peak meter (Windows' own Sound
    panel does exactly this to light its level bars). No capture
    client is opened: packets are never read, stored, or played, so
    the GetService surface (and its per-build vtable risk) is avoided
    entirely. Stops + releases on the token. Never raises; never
    touches Tk."""
    try:
        import ctypes as _ct
        from ctypes import wintypes as _wt
        import time as _time
        _ole = _ct.windll.ole32
        try:
            _ole.CoInitialize(None)
        except Exception:
            pass
        try:
            _enum = _ct.c_void_p()
            if _ole.CoCreateInstance(
                    _ct.byref(_mm_guid(_MM_CLSID_ENUM)), None, 0x1,
                    _ct.byref(_mm_guid(_MM_IID_ENUM)),
                    _ct.byref(_enum)) != 0:
                return
            try:
                _get_device = _mm_vfn(_ct, _enum.value, 5, _ct.HRESULT,
                                      _ct.c_wchar_p,
                                      _ct.POINTER(_ct.c_void_p))
                _dev = _ct.c_void_p()
                if _get_device(_enum.value, str(endpoint_id),
                               _ct.byref(_dev)) != 0:
                    return
                try:
                    _activate = _mm_vfn(_ct, _dev.value, 3, _ct.HRESULT,
                                        _ct.c_void_p, _wt.DWORD,
                                        _ct.c_void_p,
                                        _ct.POINTER(_ct.c_void_p))
                    _client = _ct.c_void_p()
                    if _activate(_dev.value,
                                 _ct.byref(_mm_guid(_MM_IID_AUDIOCLIENT)),
                                 0x1, None, _ct.byref(_client)) != 0:
                        return
                    try:
                        _get_mix = _mm_vfn(_ct, _client.value, 8, _ct.HRESULT,
                                           _ct.POINTER(_ct.c_void_p))
                        _fmt = _ct.c_void_p()
                        if _get_mix(_client.value, _ct.byref(_fmt)) != 0:
                            return
                        try:
                            _init = _mm_vfn(
                                _ct, _client.value, 3, _ct.HRESULT, _wt.DWORD,
                                _wt.DWORD, _ct.c_int64, _ct.c_int64,
                                _ct.c_void_p, _ct.c_void_p)
                            if _init(_client.value, 0, 0, 10000000, 0,
                                     _fmt, None) != 0:
                                return
                        finally:
                            try:
                                _ole.CoTaskMemFree(_fmt)
                            except Exception:
                                pass
                        _get_service = None
                        _cap = None
                        _start = _mm_vfn(_ct, _client.value, 10,
                                         _ct.HRESULT)
                        if _start(_client.value) != 0:
                            return
                        try:
                            while not token[0]:
                                _time.sleep(0.05)
                        finally:
                            try:
                                _mm_vfn(_ct, _client.value, 11,
                                        _ct.HRESULT)(_client.value)
                            except Exception:
                                pass
                    finally:
                        try:
                            _mm_vfn(_ct, _client.value, 2,
                                    _ct.c_ulong)(_client.value)
                        except Exception:
                            pass
                finally:
                    try:
                        _mm_vfn(_ct, _dev.value, 2,
                                _ct.c_ulong)(_dev.value)
                    except Exception:
                        pass
            finally:
                try:
                    _mm_vfn(_ct, _enum.value, 2,
                            _ct.c_ulong)(_enum.value)
                except Exception:
                    pass
        finally:
            try:
                _ole.CoUninitialize()
            except Exception:
                pass
    except Exception:
        pass


def _default_capture_id():
    """Endpoint-ID string of the Windows default capture device (console
    role) or '' — for preselecting what the user actually talks into.
    Read-only; never raises."""
    try:
        import ctypes as _ct
        from ctypes import wintypes as _wt
        if not _mm_ensure_com(_ct):
            return ""
        _ole = _ct.windll.ole32
        _enum = _ct.c_void_p()
        if _ole.CoCreateInstance(
                _ct.byref(_mm_guid(_MM_CLSID_ENUM)), None, 0x1,
                _ct.byref(_mm_guid(_MM_IID_ENUM)),
                _ct.byref(_enum)) != 0:
            return ""
        try:
            _get_default = _mm_vfn(_ct, _enum.value, 4, _ct.HRESULT, _ct.c_int,
                                   _ct.c_int, _ct.POINTER(_ct.c_void_p))
            _dev = _ct.c_void_p()
            if _get_default(_enum.value, 1, 0, _ct.byref(_dev)) != 0:
                return ""
            try:
                _get_id = _mm_vfn(_ct, _dev.value, 5, _ct.HRESULT,
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
                try:
                    _mm_vfn(_ct, _dev.value, 2, _ct.c_ulong)(_dev.value)
                except Exception:
                    pass
        finally:
            try:
                _mm_vfn(_ct, _enum.value, 2, _ct.c_ulong)(_enum.value)
            except Exception:
                pass
    except Exception:
        return ""
    return ""


def _capture_inputs():
    """[{name, state_int, dev_ptr_or_None, id_str}, ...] for every
    capture endpoint, COM-enumerated (names via the property store, so
    they match what Sound settings shows).

    dev_ptr is kept alive for meter binding — pass it to
    _InputMeter.open(), which owns (and releases) it. id_str is the
    endpoint ID for stream binding + default matching. Never raises;
    [] means none found / COM unavailable. Tk-thread only."""
    out = []
    try:
        import ctypes as _ct
        from ctypes import wintypes as _wt
        if not _mm_ensure_com(_ct):
            return []
        _ole = _ct.windll.ole32
        _enum = _ct.c_void_p()
        if _ole.CoCreateInstance(
                _ct.byref(_mm_guid(_MM_CLSID_ENUM)), None, 0x1,
                _ct.byref(_mm_guid(_MM_IID_ENUM)),
                _ct.byref(_enum)) != 0:
            return []
        try:
            _enum_audio = _mm_vfn(_ct, _enum.value, 3, _ct.HRESULT, _ct.c_int,
                                  _wt.DWORD, _ct.POINTER(_ct.c_void_p))
            _coll = _ct.c_void_p()
            if _enum_audio(_enum.value, 1, 0xB, _ct.byref(_coll)) != 0:
                return []
            try:
                _get_count = _mm_vfn(_ct, _coll.value, 3, _ct.HRESULT,
                                     _ct.POINTER(_wt.UINT))
                _n = _wt.UINT()
                if _get_count(_coll.value, _ct.byref(_n)) != 0:
                    return []
                _get_item = _mm_vfn(_ct, _coll.value, 4, _ct.HRESULT, _wt.UINT,
                                    _ct.POINTER(_ct.c_void_p))
                for _i in range(int(_n.value)):
                    _dev = _ct.c_void_p()
                    try:
                        if _get_item(_coll.value, _i, _ct.byref(_dev)) != 0:
                            continue
                    except Exception:
                        continue
                    _state = 0
                    try:
                        _get_state = _mm_vfn(_ct, _dev.value, 6, _ct.HRESULT,
                                             _ct.POINTER(_wt.DWORD))
                        _st = _wt.DWORD()
                        if _get_state(_dev.value, _ct.byref(_st)) == 0:
                            _state = int(_st.value)
                    except Exception:
                        pass
                    _epid = ""
                    try:
                        _get_id = _mm_vfn(_ct, _dev.value, 5, _ct.HRESULT,
                                          _ct.POINTER(_ct.c_void_p))
                        _pid = _ct.c_void_p()
                        if _get_id(_dev.value, _ct.byref(_pid)) == 0 \
                                and _pid.value:
                            try:
                                _epid = str(_ct.wstring_at(_pid.value) or "")
                            finally:
                                try:
                                    _ole.CoTaskMemFree(_pid)
                                except Exception:
                                    pass
                    except Exception:
                        pass
                    _name = f"Input {_i + 1}"
                    try:
                        _open_ps = _mm_vfn(_ct, _dev.value, 4, _ct.HRESULT,
                                           _wt.DWORD,
                                           _ct.POINTER(_ct.c_void_p))
                        _ps = _ct.c_void_p()
                        if _open_ps(_dev.value, 0, _ct.byref(_ps)) == 0:
                            try:
                                _get_value = _mm_vfn(
                                    _ct, _ps.value, 5, _ct.HRESULT, _ct.c_void_p,
                                    _ct.c_void_p)

                                class _PKEY(_ct.Structure):
                                    _fields_ = [("fmtid", _ct.c_ubyte * 16),
                                                ("pid", _wt.DWORD)]

                                class _PV(_ct.Structure):
                                    _fields_ = [("vt", _wt.USHORT),
                                                ("r1", _wt.WORD),
                                                ("r2", _wt.WORD),
                                                ("r3", _wt.WORD),
                                                ("ptr", _ct.c_void_p),
                                                ("rest", _ct.c_ubyte * 4)]

                                import uuid as _uuid
                                _u = _uuid.UUID(_MM_PKEY_FRIENDLY_FMT)
                                _fmt = (_ct.c_ubyte * 16).from_buffer_copy(
                                    _u.bytes_le)
                                _pk = _PKEY(_fmt, _MM_PKEY_FRIENDLY_PID)
                                _pv = _PV()
                                if _get_value(_ps.value, _ct.byref(_pk),
                                              _ct.byref(_pv)) == 0 \
                                        and _pv.vt == 31 and _pv.ptr:
                                    try:
                                        _name = _ct.wstring_at(_pv.ptr) or _name
                                    finally:
                                        try:
                                            _ole.PropVariantClear(_ct.byref(_pv))
                                        except Exception:
                                            pass
                            finally:
                                try:
                                    _mm_vfn(_ct, _ps.value, 2,
                                            _ct.c_ulong)(_ps.value)
                                except Exception:
                                    pass
                    except Exception:
                        pass
                    _devnum = int(_dev.value)
                    if _state != 1:
                        # only the tested (active) device keeps its ref —
                        # inactive entries release immediately, no leak
                        try:
                            _mm_vfn(_ct, _devnum, 2, _ct.c_ulong)(_devnum)
                        except Exception:
                            pass
                        _devnum = None
                    out.append({"name": str(_name), "state": _state,
                                "dev": _devnum, "id": _epid})
            finally:
                try:
                    _mm_vfn(_ct, _coll.value, 2, _ct.c_ulong)(_coll.value)
                except Exception:
                    pass
        finally:
            try:
                _mm_vfn(_ct, _enum.value, 2, _ct.c_ulong)(_enum.value)
            except Exception:
                pass
    except Exception:
        return []
    return out
