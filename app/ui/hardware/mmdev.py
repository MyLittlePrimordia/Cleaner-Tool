"""Audio input metering via the MMDevice API (COM, ctypes).

H4. Extracted verbatim from app/gui.py. Used by the Mic Check dialog, the
Speaker Test dialog and the shared input-capture pump.
"""
from __future__ import annotations

import ctypes
from typing import Optional

class _PadReportStats:
    """Packet-delta report-rate tracker (mirrors GamepadStats from the
    reference): deltas between consecutive *changed* packets over a
    128-sample window give avg interval ms, Hz and peak Hz. Quiet
    controller = no samples = honestly 'no data', never a fake 0 Hz."""

    _MAX = 128

    def __init__(self):
        self.reset()

    def reset(self):
        self._last_packet = None
        self._last_ns = 0
        self._deltas = []
        self.avg_ms = 0.0
        self.hz = 0.0
        self.peak_hz = 0.0
        self.packets = 0

    def update(self, packet):
        try:
            import time as _time
            now = _time.perf_counter_ns()
            if self._last_packet is None:
                self._last_packet, self._last_ns = packet, now
                self.packets += 1
                return
            if packet == self._last_packet:
                return
            self._last_packet = packet
            self.packets += 1
            if now > self._last_ns:
                dt = (now - self._last_ns) / 1_000_000.0
                if 0.0 < dt < 500.0:
                    self._deltas.append(dt)
                    if len(self._deltas) > self._MAX:
                        self._deltas.pop(0)
                    self.avg_ms = sum(self._deltas) / len(self._deltas)
                    if self.avg_ms > 0:
                        self.hz = 1000.0 / self.avg_ms
                        if self.hz > self.peak_hz:
                            self.peak_hz = self.hz
            self._last_ns = now
        except Exception:
            pass


def _mm_guid(s):
    """GUID struct instance from a registry-format string."""
    import ctypes as _ct
    from ctypes import wintypes as _wt
    import uuid as _uuid

    class _GUID(_ct.Structure):
        _fields_ = [("Data1", _wt.DWORD), ("Data2", _wt.WORD),
                    ("Data3", _wt.WORD), ("Data4", _ct.c_ubyte * 8)]

    u = _uuid.UUID(s)
    return _GUID(u.time_low, u.time_mid, u.time_hi_version,
                 (_ct.c_ubyte * 8)(*u.bytes[8:]))


def _mm_vfn(ct, iface, index, restype, *argtypes):
    """Dereferenced COM vtable call (iface -> vtbl -> slot); the pattern
    proven live during the WASAPI probing (direct indexing faulted)."""
    vt = ct.c_void_p.from_address(iface).value
    addr = (ct.c_void_p * (index + 1)).from_address(vt)[index]
    return ct.WINFUNCTYPE(restype, ct.c_void_p, *argtypes)(addr)


_MM_CLSID_ENUM = "BCDE0395-E52F-467C-8E3D-C4579291692E"
_MM_IID_ENUM = "A95664D2-9614-4F35-A746-DE8DB63617E6"
_MM_IID_METER = "C02216F6-8C67-4B5B-9D00-D008E73E0064"
_MM_IID_AUDIOCLIENT = "1CB9AD4C-DBFA-4C32-B178-C2F568A703B2"
_MM_PKEY_FRIENDLY_FMT = "A45C254E-DF1C-4EFD-8020-67D146A850E0"
_MM_PKEY_FRIENDLY_PID = 14
_MM_STATE_NAMES = {1: "ready", 2: "off", 4: "not present", 8: "unplugged"}
_MM_COM_INIT = {"done": False}


def _mm_ensure_com(ct):
    if _MM_COM_INIT["done"]:
        return True
    try:
        ct.windll.ole32.CoInitialize(None)
        _MM_COM_INIT["done"] = True
        return True
    except Exception:
        return False


class _InputMeter:
    """Peak meter bound to ONE capture endpoint (not just the default).

    Tk-thread only. read() -> 0.0..1.0 or None (sticky). close()
    releases the COM refs (best-effort). Read-only: GetPeakValue never
    touches device state."""

    def __init__(self):
        self._peak_fn = None
        self._meter_ptr = None
        self._dev_ptr = None
        self._dead = False
        self._ct = None

    @classmethod
    def open(cls, dev_ptr):
        self = cls()
        try:
            import ctypes as _ct
            if not _mm_ensure_com(_ct):
                return None
            self._ct = _ct
        except Exception:
            return None
        return self._finish(dev_ptr)

    def _finish(self, dev_ptr):
        import ctypes as _ct
        from ctypes import wintypes as _wt
        try:
            _activate = _mm_vfn(_ct, dev_ptr, 3, _ct.HRESULT,
                                _ct.c_void_p, _wt.DWORD, _ct.c_void_p,
                                _ct.POINTER(_ct.c_void_p))
            _iid = _mm_guid(_MM_IID_METER)
            _meter = _ct.c_void_p()
            if _activate(dev_ptr, _ct.byref(_iid), 0x1, None,
                         _ct.byref(_meter)) != 0:
                raise OSError("no meter")
            self._peak_fn = _mm_vfn(_ct, _meter.value, 3, _ct.HRESULT,
                                    _ct.POINTER(_ct.c_float))
            self._meter_ptr = _meter.value
            self._dev_ptr = dev_ptr
            self._float = _ct.c_float
            self._byref = _ct.byref
            return self
        except Exception:
            try:
                _mm_vfn(_ct, dev_ptr, 2, _ct.c_ulong)(dev_ptr)
            except Exception:
                pass
            return None

    def read(self):
        if self._dead or self._peak_fn is None:
            return None
        try:
            peak = self._float()
            if self._peak_fn(self._meter_ptr, self._byref(peak)) != 0:
                return 0.0
            return max(0.0, min(1.0, float(peak.value)))
        except Exception:
            self._dead = True
            return None

    def start_stream(self, endpoint_id=""):
        """Open a shared-mode capture stream and drain it on a worker
        thread (packets discarded). Why: some USB drivers only push
        audio — and only feed the peak meter — while a capture stream
        is open (Windows' own Sound panel does exactly this to light
        its level bars). Read-only: data is never stored or played.

        The whole stream (COM init included) lives on the worker
        thread — STA objects must never cross threads. The Tk thread
        keeps only the peak meter. Returns True when pumping."""
        try:
            import threading as _th
            if getattr(self, "_pump_token", None) is not None:
                return True
            if not endpoint_id:
                return False
            _token = [False]
            self._pump_token = _token
            try:
                # Function-local import: `_InputMeter` starts the shared
                # capture pump, and that pump needs the MMDevice constants
                # defined in THIS module. Importing capture at module scope
                # would make the pair circular. Deferred to the call site,
                # which is the pattern the rest of this codebase uses.
                from app.ui.hardware.capture import _pump_capture
                _th.Thread(target=_pump_capture,
                           args=(str(endpoint_id), _token),
                           daemon=True, name="MicCheckPump").start()
            except Exception:
                self._pump_token = None
                return False
            return True
        except Exception:
            try:
                self._pump_token = None
            except Exception:
                pass
            return False

    def _stop_stream(self):
        try:
            _token = getattr(self, "_pump_token", None)
            self._pump_token = None
            if _token is not None:
                _token[0] = True
        except Exception:
            pass

    def close(self):
        self._dead = True
        self._stop_stream()
        try:
            _ct = self._ct
            if _ct is None:
                return
            _release = _mm_vfn(_ct, self._meter_ptr, 2, _ct.c_ulong) \
                if self._meter_ptr else None
            if _release is not None:
                try:
                    _release(self._meter_ptr)
                except Exception:
                    pass
            if self._dev_ptr:
                try:
                    _mm_vfn(_ct, self._dev_ptr, 2, _ct.c_ulong)(self._dev_ptr)
                except Exception:
                    pass
        except Exception:
            pass
        finally:
            self._peak_fn = None
            self._meter_ptr = None
            self._dev_ptr = None
