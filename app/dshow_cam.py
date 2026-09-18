"""
Dep-free webcam preview engine (Tools tab, Webcam Test card) — Tk-free.

Draws the camera straight into a Tk frame via DirectShow (quartz.dll):
enumerate video-input monikers for REAL camera names, build a capture
graph (source -> Preview pin -> default renderer), parent the video
window to the caller's HWND as WS_CHILD. No OpenCV, no Pillow, no pip
anything — quartz + the inbox UVC driver ship with every Windows
including LTSC (which has no Store Camera app, so this path is the
test there, not a fallback).

Fail-closed throughout: any non-S_OK HRESULT releases everything
built so far and yields [] / False / None. Never raises. Tk thread
only (STA COM, same rule as the audio meters).
"""

from __future__ import annotations

_CLSID_SYSTEM_ENUM = "62BE5D10-60EB-11D0-BD3B-00A0C911CE86"
_CLSID_VIDEO_INPUT_CAT = "860BB310-5D01-11D0-BD3B-00A0C911CE86"
_CLSID_FILTER_GRAPH = "E436EBB3-524F-11CE-9F53-0020AF0BA770"
_CLSID_CAPTURE_BUILDER = "BF87B6E1-8C27-11D0-B3F0-00AA003761C5"
_IID_UNKNOWN = "00000000-0000-0000-C000-000000000046"
_IID_CREATE_ENUM = "29840822-5B84-11D0-BD3B-00A0C911CE86"
_IID_BASE_FILTER = "56A86895-0AD4-11CE-B03A-0020AF0BA770"
_IID_MEDIACONTROL = "56A868B1-0AD4-11CE-B03A-0020AF0BA770"
_IID_VIDEOWINDOW = "56A868B4-0AD4-11CE-B03A-0020AF0BA770"
_IID_CAPTURE_BUILDER = "93E5A4E0-2D50-11D2-ABFA-00A0C9C6E38E"
_PIN_PREVIEW = "FB6C4282-0353-11D1-905F-0000C0CC16BA"
_PIN_CAPTURE = "FB6C4281-0353-11D1-905F-0000C0CC16BA"
_CLSID_VIDEO_RENDERER = "70E102B0-5556-11CE-97C0-00AA0055595A"
# BUG FIX: this was "56A8689B" — one hex digit off from the real
# IID_IFilterGraph ("...689F"). CoCreateInstance(CLSID_FilterGraph,
# IID_IFilterGraph) was asking for an interface the object doesn't
# expose, so it failed with E_NOINTERFACE on literally every call,
# before a single frame was ever attempted. Preview.open() returned
# False immediately and WebcamDialog reported that as "in use by
# another program" — which is why that message showed up even for a
# completely free, working USB webcam. _CLSID_CAPTURE_BUILDER and
# _IID_CAPTURE_BUILDER above were also wrong/malformed (the latter was
# missing its final hex digit), which silently degraded every attempt
# to the slower manual render path — now fixed too.
_IID_FILTER_GRAPH = "56A8689F-0AD4-11CE-B03A-0020AF0BA770"

# IBaseFilter::EnumPins=10. IEnumPins::Next=3. IPin::QueryDirection=9
# (PINDIR_OUTPUT=1). IGraphBuilder::Render=12 (Intelligent Connect).
_SLOT_ENUM_PINS = 10
_SLOT_PIN_DIRECTION = 9
_SLOT_GRAPH_RENDER = 12
_PINDIR_OUTPUT = 1

_WS_CHILD = 0x40000000
_WS_CLIPSIBLINGS = 0x04000000

# IMediaControl (IDispatch base): Run=7 Stop=9. IVideoWindow (IDispatch
# base): put_WindowStyle=9 put_AutoShow=13 put_Visible=19 put_Owner=29
# SetWindowPosition=39. IMoniker::BindToObject=8.
_SLOT_RUN = 7
_SLOT_STOP = 9
_SLOT_PUT_STYLE = 9
_SLOT_PUT_AUTOSHOW = 13
_SLOT_PUT_VISIBLE = 19
_SLOT_PUT_OWNER = 29
_SLOT_SET_POS = 39
_SLOT_BIND = 8


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


def _release(ct, iface):
    try:
        if iface:
            _vfn(ct, iface, 2, ct.c_ulong)(iface)
    except Exception:
        pass


def _friendly_name(ct, ole, moniker):
    """IPropertyBag FriendlyName off a device moniker, or ''. Never raises.

    Binds via BindToObject, falling back to BindToStorage (some virtual
    drivers only serve the bag on one of the two). Real UVC webcams
    serve it on the first try; either way a miss degrades to the
    numbered fallback name downstream — never a failure."""
    try:
        from ctypes import wintypes as _wt
        import uuid as _uuid
        _u = _uuid.UUID("55272A00-42CB-11CE-8135-00AA004BB851")
        _iid = _ct_guid_bytes(ct, _u)
        _bag = ct.c_void_p()
        bound = False
        for _slot in (8, 9):  # BindToObject, then BindToStorage
            try:
                _bind = _vfn(ct, moniker, _slot, ct.HRESULT, ct.c_void_p,
                             ct.c_void_p, ct.c_void_p, ct.c_void_p)
                if _bind(moniker, None, None, ct.byref(_iid),
                         ct.byref(_bag)) == 0:
                    bound = True
                    break
            except Exception:
                continue
        if not bound:
            return ""
        try:
            _read = _vfn(ct, _bag.value, 3, ct.HRESULT, ct.c_wchar_p,
                         ct.c_void_p, ct.c_void_p)

            class _PV(ct.Structure):
                _fields_ = [("vt", _wt.USHORT), ("r1", _wt.WORD),
                            ("r2", _wt.WORD), ("r3", _wt.WORD),
                            ("ptr", ct.c_void_p),
                            ("rest", ct.c_ubyte * 4)]

            pv = _PV()
            try:
                ct.windll.oleaut32.VariantInit(ct.byref(pv))
            except Exception:
                pass
            if _read(_bag.value, "FriendlyName", ct.byref(pv), None) != 0:
                return ""
            if pv.vt != 8 or not pv.ptr:  # VT_BSTR
                return ""
            try:
                return str(ct.wstring_at(pv.ptr) or "")
            finally:
                try:
                    ct.windll.oleaut32.VariantClear(ct.byref(pv))
                except Exception:
                    pass
        finally:
            _release(ct, _bag.value)
    except Exception:
        return ""
    return ""


def _ct_guid_bytes(ct, u):
    class _G(ct.Structure):
        _fields_ = [("d1", ct.c_ulong), ("d2", ct.c_ushort),
                    ("d3", ct.c_ushort), ("d4", ct.c_ubyte * 8)]
    return _G(u.time_low, u.time_mid, u.time_hi_version,
              (ct.c_ubyte * 8)(*u.bytes[8:]))


def list_cameras():
    """[{index, name}] for every video-input device. Never raises."""
    try:
        import ctypes as _ct
        _ole = _ct.windll.ole32
        try:
            _ole.CoInitialize(None)
        except Exception:
            pass
        _devenum = _ct.c_void_p()
        if _ole.CoCreateInstance(
                _ct.byref(_guid(_CLSID_SYSTEM_ENUM)), None, 0x1,
                _ct.byref(_guid(_IID_CREATE_ENUM)),
                _ct.byref(_devenum)) != 0:
            return []
        try:
            _create = _vfn(_ct, _devenum.value, 3, _ct.HRESULT, _ct.c_void_p,
                           _ct.c_void_p, _ct.c_ulong)
            _etor = _ct.c_void_p()
            if _create(_devenum.value,
                       _ct.byref(_guid(_CLSID_VIDEO_INPUT_CAT)),
                       _ct.byref(_etor), 0) != 0:
                return []
            try:
                _next = _vfn(_ct, _etor.value, 3, _ct.HRESULT, _ct.c_ulong,
                             _ct.c_void_p,
                             _ct.POINTER(_ct.c_ulong))
                out = []
                idx = 0
                while True:
                    _mon = _ct.c_void_p()
                    _got = _ct.c_ulong()
                    try:
                        if _next(_etor.value, 1, _ct.byref(_mon),
                                 _ct.byref(_got)) != 0 or not _got.value:
                            break
                    except Exception:
                        break
                    try:
                        name = _friendly_name(_ct, _ole, _mon.value)
                        out.append({"index": idx,
                                    "name": name or f"Camera {idx}"})
                        idx += 1
                    finally:
                        _release(_ct, _mon.value)
                return out
            finally:
                _release(_ct, _etor.value)
        finally:
            _release(_ct, _devenum.value)
    except Exception:
        return []


class Preview:
    """One live DirectShow preview parented to an HWND. Tk thread only."""

    def __init__(self):
        self._ct = None
        self._graph = 0
        self._vidwin = 0
        self._media = 0
        self._extra = []
        self.live = False
        # Plain-English reason for the last open() failure, set at each
        # return-False point below. WebcamDialog reads this instead of
        # guessing — no more blanket "in use by another program" for
        # failures that have nothing to do with that.
        self.last_error = ""

    def _hold_extra(self, ptr):
        if ptr:
            self._extra.append(ptr)

    def open(self, index, hwnd, w, h):
        """Start camera `index` inside hwnd (pixels). True = running.

        Every failure path releases everything (no headless graph left
        running, no leak) — fail-closed by construction."""
        self.close()
        self.last_error = ""
        ok = False
        try:
            import ctypes as _ct
            self._ct = _ct
            _ole = _ct.windll.ole32
            try:
                _ole.CoInitialize(None)
            except Exception:
                pass
            # --- graph + builder (queried for their real interfaces —
            # a bare IUnknown's vtable has no derived slots, so slot
            # calls through it read garbage) ---
            _graph = _ct.c_void_p()
            _hr = _ole.CoCreateInstance(
                    _ct.byref(_guid(_CLSID_FILTER_GRAPH)), None, 0x1,
                    _ct.byref(_guid(_IID_FILTER_GRAPH)),
                    _ct.byref(_graph))
            if _hr != 0:
                self.last_error = (
                    f"Windows' video engine (DirectShow) didn't start "
                    f"(code 0x{_hr & 0xFFFFFFFF:08X}).")
                return False
            self._graph = _graph.value
            # Builder is best-effort (missing on stripped/server images
            # — the manual path below covers those).
            _builder = _ct.c_void_p()
            _have_builder = False
            try:
                if _ole.CoCreateInstance(
                        _ct.byref(_guid(_CLSID_CAPTURE_BUILDER)), None, 0x1,
                        _ct.byref(_guid(_IID_CAPTURE_BUILDER)),
                        _ct.byref(_builder)) == 0:
                    self._hold_extra(_builder.value)
                    _set_fg = _vfn(_ct, _builder.value, 3, _ct.HRESULT,
                                   _ct.c_void_p)
                    if _set_fg(_builder.value, self._graph) == 0:
                        _have_builder = True
            except Exception:
                _have_builder = False
            # --- source filter for this index ---
            cams = list_cameras()
            if index >= len(cams):
                self.last_error = "That camera is no longer listed by Windows."
                return False
            _src = self._source_for(index, _ct, _ole)
            if not _src:
                self.last_error = "Could not bind to that camera's driver."
                return False
            self._hold_extra(_src)
            _add = _vfn(_ct, self._graph, 3, _ct.HRESULT, _ct.c_void_p,
                        _ct.c_wchar_p)
            _hr = _add(self._graph, _src, "src")
            if _hr != 0:
                self.last_error = (
                    f"Couldn't add the camera to the preview graph "
                    f"(code 0x{_hr & 0xFFFFFFFF:08X}).")
                return False
            # --- render: builder Preview pin first (fast), Capture pin
            # next, manual Intelligent-Connect last (boxes without the
            # builder registered, e.g. stripped/server images) ---
            _rendered = False
            if _have_builder:
                try:
                    _rendered = self._render_via_builder(
                        _ct, _ole, _builder.value, _src)
                except Exception:
                    _rendered = False
            if not _rendered:
                if not self._render_manual(_ct, _ole, _graph.value, _src):
                    # A real "someone else has it open" case shows up
                    # here (the source filter can't stream to two
                    # graphs at once) — but so do plain driver quirks,
                    # so word it as a possibility, not a certainty.
                    self.last_error = (
                        "Couldn't start streaming from this camera — it may "
                        "be in use by another app (Discord, OBS, Zoom, "
                        "Teams...), or its driver may not support preview.")
                    return False
            # --- window plumbing (no popup renderer window, ever) ---
            _vid = _ct.c_void_p()
            _qi = _vfn(_ct, self._graph, 0, _ct.HRESULT, _ct.c_void_p,
                       _ct.c_void_p)
            if _qi(self._graph, _ct.byref(_guid(_IID_VIDEOWINDOW)),
                   _ct.byref(_vid)) != 0:
                self.last_error = "This camera's renderer has no preview window."
                return False
            self._vidwin = _vid.value
            _put_long = lambda slot, val: _vfn(
                _ct, self._vidwin, slot, _ct.HRESULT, _ct.c_long)(
                    self._vidwin, val)
            _put_ptr = lambda slot, val: _vfn(
                _ct, self._vidwin, slot, _ct.HRESULT, _ct.c_void_p)(
                    self._vidwin, val)
            if _put_long(_SLOT_PUT_AUTOSHOW, 0) != 0:
                self.last_error = "Couldn't configure the preview window."
                return False
            if _put_ptr(_SLOT_PUT_OWNER, int(hwnd)) != 0:
                self.last_error = "Couldn't attach the preview to this app's window."
                return False
            if _put_long(_SLOT_PUT_STYLE,
                         _WS_CHILD | _WS_CLIPSIBLINGS) != 0:
                self.last_error = "Couldn't configure the preview window."
                return False
            _pos = _vfn(_ct, self._vidwin, _SLOT_SET_POS, _ct.HRESULT,
                        _ct.c_long, _ct.c_long, _ct.c_long, _ct.c_long)
            if _pos(self._vidwin, 0, 0, int(w), int(h)) != 0:
                self.last_error = "Couldn't size the preview window."
                return False
            if _put_long(_SLOT_PUT_VISIBLE, -1) != 0:
                self.last_error = "Couldn't show the preview window."
                return False
            # --- run ---
            _mc = _ct.c_void_p()
            if _qi(self._graph, _ct.byref(_guid(_IID_MEDIACONTROL)),
                   _ct.byref(_mc)) != 0:
                self.last_error = "Couldn't get playback control for this camera."
                return False
            self._media = _mc.value
            _run = _vfn(_ct, self._media, _SLOT_RUN, _ct.HRESULT)
            _hr = _run(self._media)
            if _hr != 0:
                # 0x80070005 (E_ACCESSDENIED) and 0x800700AA
                # (ERROR_BUSY) are the two codes that genuinely mean
                # "something else has it open" — anything else gets an
                # honest generic message instead of that guess.
                if (_hr & 0xFFFFFFFF) in (0x80070005, 0x800700AA):
                    self.last_error = (
                        "This camera is in use by another app (Discord, "
                        "OBS, Zoom, Teams...) — close it there and try again.")
                else:
                    self.last_error = (
                        f"The camera stream wouldn't start "
                        f"(code 0x{_hr & 0xFFFFFFFF:08X}).")
                return False
            ok = True
            self.live = True
            return True
        except Exception as _exc:
            self.last_error = f"Unexpected camera error: {_exc}"
            return False
        finally:
            if not ok:
                self.close()

    def _render_via_builder(self, ct, ole, builder, src):
        """RenderStream Preview pin, then Capture pin. Bool, never raises."""
        try:
            _render = _vfn(ct, builder, 4, ct.HRESULT, ct.c_void_p,
                           ct.c_void_p, ct.c_void_p, ct.c_void_p,
                           ct.c_void_p)
            for _pin in (_PIN_PREVIEW, _PIN_CAPTURE):
                try:
                    if _render(builder, ct.byref(_guid(_pin)), None, src,
                               None, None) == 0:
                        return True
                except Exception:
                    continue
        except Exception:
            pass
        return False

    def _render_manual(self, ct, ole, graph, src):
        """Add a Video Renderer and IGraphBuilder::Render the source's
        first output pin (Intelligent Connect). Bool, never raises."""
        try:
            from ctypes import wintypes as _wt
            _rend = ct.c_void_p()
            if ole.CoCreateInstance(
                    ct.byref(_guid(_CLSID_VIDEO_RENDERER)), None, 0x1,
                    ct.byref(_guid(_IID_UNKNOWN)),
                    ct.byref(_rend)) != 0:
                return False
            self._hold_extra(_rend.value)
            _add = _vfn(ct, graph, 3, ct.HRESULT, ct.c_void_p,
                        ct.c_wchar_p)
            if _add(graph, _rend.value, "vid") != 0:
                return False
            _enumpins = _vfn(ct, src, _SLOT_ENUM_PINS, ct.HRESULT,
                             ct.POINTER(ct.c_void_p))
            _pins = ct.c_void_p()
            if _enumpins(src, ct.byref(_pins)) != 0:
                return False
            try:
                _next = _vfn(ct, _pins.value, 3, ct.HRESULT, ct.c_ulong,
                             ct.c_void_p, ct.POINTER(ct.c_ulong))
                while True:
                    _pin = ct.c_void_p()
                    _got = ct.c_ulong()
                    try:
                        if _next(_pins.value, 1, ct.byref(_pin),
                                 ct.byref(_got)) != 0 or not _got.value:
                            return False
                    except Exception:
                        return False
                    try:
                        _qdir = _vfn(ct, _pin.value, _SLOT_PIN_DIRECTION,
                                     ct.HRESULT, ct.POINTER(_wt.DWORD))
                        _d = _wt.DWORD()
                        if _qdir(_pin.value, ct.byref(_d)) == 0 \
                                and int(_d.value) == _PINDIR_OUTPUT:
                            _render2 = _vfn(ct, graph, _SLOT_GRAPH_RENDER,
                                            ct.HRESULT, ct.c_void_p,
                                            ct.c_void_p)
                            return _render2(graph, _pin.value, None) == 0
                    finally:
                        _release(ct, _pin.value)
            finally:
                _release(ct, _pins.value)
        except Exception:
            pass
        return False

    def _source_for(self, index, ct, ole):
        """IBaseFilter pointer (int) for the Nth video device, or 0."""
        try:
            _devenum = ct.c_void_p()
            if ole.CoCreateInstance(
                    ct.byref(_guid(_CLSID_SYSTEM_ENUM)), None, 0x1,
                    ct.byref(_guid(_IID_CREATE_ENUM)),
                    ct.byref(_devenum)) != 0:
                return 0
            try:
                _create = _vfn(ct, _devenum.value, 3, ct.HRESULT,
                               ct.c_void_p, ct.c_void_p,
                               ct.c_ulong)
                _etor = ct.c_void_p()
                if _create(_devenum.value,
                           ct.byref(_guid(_CLSID_VIDEO_INPUT_CAT)),
                           ct.byref(_etor), 0) != 0:
                    return 0
                try:
                    _next = _vfn(ct, _etor.value, 3, ct.HRESULT, ct.c_ulong,
                                 ct.c_void_p,
                                 ct.POINTER(ct.c_ulong))
                    for _i in range(int(index) + 1):
                        _mon = ct.c_void_p()
                        _got = ct.c_ulong()
                        if _next(_etor.value, 1, ct.byref(_mon),
                                 ct.byref(_got)) != 0 or not _got.value:
                            return 0
                        if _i < int(index):
                            _release(ct, _mon.value)
                            continue
                        try:
                            _bind = _vfn(ct, _mon.value, _SLOT_BIND,
                                         ct.HRESULT, ct.c_void_p, ct.c_void_p,
                                         ct.c_void_p,
                                         ct.c_void_p)
                            _filt = ct.c_void_p()
                            if _bind(_mon.value, None, None,
                                     ct.byref(_guid(_IID_BASE_FILTER)),
                                     ct.byref(_filt)) != 0:
                                return 0
                            return _filt.value
                        finally:
                            _release(ct, _mon.value)
                    return 0
                finally:
                    _release(ct, _etor.value)
            finally:
                _release(ct, _devenum.value)
        except Exception:
            return 0
    def resize(self, w, h):
        """Re-fit the video to its holder. Best-effort, never raises."""
        try:
            if not self.live or not self._vidwin or self._ct is None:
                return
            _pos = _vfn(self._ct, self._vidwin, _SLOT_SET_POS, self._ct.HRESULT,
                        self._ct.c_long, self._ct.c_long, self._ct.c_long,
                        self._ct.c_long)
            _pos(self._vidwin, 0, 0, int(w), int(h))
        except Exception:
            pass

    def close(self):
        """Stop + hide + release everything. Never raises."""
        try:
            ct = self._ct
            if ct is not None:
                try:
                    if self._media:
                        _vfn(ct, self._media, _SLOT_STOP, ct.HRESULT)(
                            self._media)
                except Exception:
                    pass
                try:
                    if self._vidwin:
                        _vfn(ct, self._vidwin, _SLOT_PUT_VISIBLE, ct.HRESULT,
                             ct.c_long)(self._vidwin, 0)
                except Exception:
                    pass
                try:
                    if self._vidwin:
                        _vfn(ct, self._vidwin, _SLOT_PUT_OWNER, ct.HRESULT,
                             ct.c_void_p)(self._vidwin, None)
                except Exception:
                    pass
                for _r in reversed(self._extra + [self._media, self._vidwin,
                                                  self._graph]):
                    _release(ct, _r)
        except Exception:
            pass
        finally:
            self._ct = None
            self._graph = 0
            self._vidwin = 0
            self._media = 0
            self._extra = []
            self.live = False
