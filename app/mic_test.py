"""
Mic record-and-playback engine (Tools tab, Mic Check card) — Tk-free.

Records a few seconds from the Windows DEFAULT capture device via
winmm waveIn (ctypes, stdlib-only) and plays the take back with
winsound SND_MEMORY (no file ever written). Answers "what do I sound
like" without a live loopback path (which would need a render stream
the app deliberately does not open).

Honesty notes (surfaced in-UI, not hidden):
  * waveIn records the DEFAULT input (WAVE_MAPPER) — the dialog
    preselects the default mic, and says so when it doesn't match.
  * record_wav() blocks ~seconds+overhead: callers run it on a worker
    thread and hop the result back. stop() unblocks promptly.
  * Every winmm return code is checked; any failure yields None (the
    dialog reports instead of playing silence). Never raises.
"""

from __future__ import annotations

import ctypes as _ct
import io
import time
import wave

_WAVE_MAPPER = -1
_CALLBACK_NULL = 0x0
_WAVE_FORMAT_PCM = 1


class _WAVEFORMATEX(_ct.Structure):
    _fields_ = [("wFormatTag", _ct.c_ushort),
                ("nChannels", _ct.c_ushort),
                ("nSamplesPerSec", _ct.c_uint),
                ("nAvgBytesPerSec", _ct.c_uint),
                ("nBlockAlign", _ct.c_ushort),
                ("wBitsPerSample", _ct.c_ushort),
                ("cbSize", _ct.c_ushort)]


class _WAVEHDR(_ct.Structure):
    _fields_ = [("lpData", _ct.c_char_p),
                ("dwBufferLength", _ct.c_uint),
                ("dwBytesRecorded", _ct.c_uint),
                ("dwUser", _ct.c_void_p),
                ("dwFlags", _ct.c_uint),
                ("dwLoops", _ct.c_uint),
                ("lpNext", _ct.c_void_p),
                ("reserved", _ct.c_void_p)]


class Recorder:
    """One blocking take. Tk-free; construct + record on a worker."""

    def __init__(self, seconds=3.0, rate=44100):
        self.seconds = max(1.0, min(6.0, float(seconds)))
        self.rate = int(rate)
        self._stop = False
        self._hwi = None

    def stop(self):
        self._stop = True
        try:
            if self._hwi is not None:
                _ct.windll.winmm.waveInStop(self._hwi)
        except Exception:
            pass

    def record_wav(self):
        """WAV bytes (mono 16-bit) or None. Never raises."""
        try:
            winmm = _ct.windll.winmm
        except Exception:
            return None
        fmt = _WAVEFORMATEX()
        fmt.wFormatTag = _WAVE_FORMAT_PCM
        fmt.nChannels = 1
        fmt.nSamplesPerSec = self.rate
        fmt.wBitsPerSample = 16
        fmt.nBlockAlign = 2
        fmt.nAvgBytesPerSec = self.rate * 2
        fmt.cbSize = 0
        hwi = _ct.c_void_p()
        try:
            if winmm.waveInOpen(_ct.byref(hwi), _WAVE_MAPPER,
                                _ct.byref(fmt), 0, 0,
                                _CALLBACK_NULL) != 0:
                return None
        except Exception:
            return None
        self._hwi = hwi.value
        # 0.5s buffers — small enough for picky drivers, big enough to
        # keep the poll loop cheap.
        nbuf = max(2, int(self.seconds * 2) + 2)
        buflen = self.rate  # 0.5s of mono16
        bufs, hdrs, blobs = [], [], []
        try:
            for _ in range(nbuf):
                blobs.append(_ct.create_string_buffer(buflen))
                hd = _WAVEHDR()
                hd.lpData = _ct.cast(blobs[-1], _ct.c_char_p)
                hd.dwBufferLength = buflen
                bufs.append(blobs[-1])
                hdrs.append(hd)
                if winmm.waveInPrepareHeader(hwi, _ct.byref(hd),
                                             _ct.sizeof(hd)) != 0:
                    return None
                if winmm.waveInAddBuffer(hwi, _ct.byref(hd),
                                         _ct.sizeof(hd)) != 0:
                    return None
            if winmm.waveInStart(hwi) != 0:
                return None
            want = int(self.seconds * self.rate * 2)
            got = 0
            t0 = time.perf_counter()
            while got < want and not self._stop:
                if time.perf_counter() - t0 > self.seconds + 5.0:
                    break
                time.sleep(0.05)
                got = sum(int(h.dwBytesRecorded) for h in hdrs)
            try:
                winmm.waveInStop(hwi)
            except Exception:
                pass
            try:
                winmm.waveInReset(hwi)
            except Exception:
                pass
            if self._stop:
                return None
            pcm = bytearray()
            for h, b in zip(hdrs, bufs):
                n = max(0, min(int(h.dwBytesRecorded), buflen))
                pcm += bytes(b.raw[:n])
                if len(pcm) >= want:
                    break
            pcm = bytes(pcm[:want])
            if len(pcm) < self.rate:  # <0.5s of audio = not a take
                return None
            out = io.BytesIO()
            with wave.open(out, "wb") as wh:
                wh.setnchannels(1)
                wh.setsampwidth(2)
                wh.setframerate(self.rate)
                wh.writeframes(pcm)
            return out.getvalue()
        except Exception:
            return None
        finally:
            try:
                for h in hdrs:
                    try:
                        winmm.waveInUnprepareHeader(hwi, _ct.byref(h),
                                                    _ct.sizeof(h))
                    except Exception:
                        pass
            except Exception:
                pass
            try:
                winmm.waveInClose(hwi)
            except Exception:
                pass
            self._hwi = None


def play_wav_bytes(data):
    """Play WAV bytes on the default output. True = played. Never raises."""
    if not data:
        return False
    try:
        import winsound
        winsound.PlaySound(data, winsound.SND_MEMORY)
        return True
    except Exception:
        return False
