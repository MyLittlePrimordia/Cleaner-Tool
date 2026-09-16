"""
Internet Speed Test engine (Tools tab, Speed Test card).

Measures ping + download + upload FOR THIS machine with stdlib only
(socket + urllib, no API keys, no third-party deps):

  * ping     — median TCP-connect latency to speed.cloudflare.com:443
  * download — timed GET from https://speed.cloudflare.com/__down?bytes=N
               (Cloudflare edge, same engine that powers speed.cloudflare.com)
  * upload   — timed POST to https://speed.cloudflare.com/__up

Fallback: https://cachefly.cachefly.net/10mb.test when the Cloudflare
endpoint is unreachable. No keys on any path; the Cloudflare
`authorizationToken` stays null (only needed to attribute tests to your
own account — never requested here).

Honesty contract (mirrors dns_test / health_scan):
  * unreachable legs report None, never a fake 0
  * cancelled() aborts between attempts and mid-stream
  * dialog (gui.py) runs these in worker threads and paints live

The dialog lives in gui.py; this module stays Tk-free and unit-tested.
"""

from __future__ import annotations

import socket
import time
import urllib.request

PING_HOST = "speed.cloudflare.com"
PING_PORT = 443
PING_TIMEOUT_S = 1.5
PING_ATTEMPTS = 3

DOWNLOAD_URL = "https://speed.cloudflare.com/__down"
UPLOAD_URL = "https://speed.cloudflare.com/__up"
# Keyless fallback when the edge endpoint is down (well-known test file).
FALLBACK_DOWNLOAD_URL = "https://cachefly.cachefly.net/10mb.test"

DOWNLOAD_BYTES = (100_000, 1_000_000, 10_000_000)
UPLOAD_BYTES = (100_000, 1_000_000)
LEG_TIMEOUT_S = 10.0
# Parallel multi-stream rounds (like Ookla/Cloudflare web: one TCP
# stream cannot fill a fast pipe, and a lone 100KB sample is pure
# handshake — that combo once reported 2.9 Mbps on a fast link).
PARALLEL_STREAMS = 4
DOWNLOAD_ROUNDS = (2_000_000, 10_000_000, 25_000_000)  # per stream
UPLOAD_ROUNDS = (512_000, 2_000_000)                   # per stream
# F08: worst-case data budget surfaced in the Tools dialog copy
# ((2+10+25)MB x4 down + (0.5+2)MB x4 up ≈ 160MB on a fast link).
ESTIMATED_MAX_MB = (sum(DOWNLOAD_ROUNDS) * PARALLEL_STREAMS
                    + sum(UPLOAD_ROUNDS) * PARALLEL_STREAMS) / 1_000_000
ROUND_MIN_MBPS = 25.0    # run the next download round above this…
ROUND_FAST_MBPS = 250.0  # …and the jumbo round above this (else save data)
STREAM_TIMEOUT_S = 20.0
CHUNK = 1 << 16
_UA = "CleanerTool/2.0 (speed test)"

# Closest-server candidates (no keys): Cloudflare is anycast, so the
# edge already IS the nearest PoP — the race below just confirms it
# against the fallback and names the winner in the UI like other apps.
CANDIDATE_HOSTS = (
    ("Cloudflare edge", "speed.cloudflare.com", DOWNLOAD_URL),
    ("Cachefly", "cachefly.cachefly.net", FALLBACK_DOWNLOAD_URL),
)


def race_endpoints(timeout: float = PING_TIMEOUT_S, attempts: int = 2,
                   cancelled=None) -> tuple:
    """(name, base_url, ms-or-None) of the lowest-latency candidate.

    Never raises; offline returns ("Cloudflare edge", DOWNLOAD_URL, None)
    so the caller still attempts the primary and fails honestly."""
    is_cancelled = cancelled or (lambda: False)
    best = ("Cloudflare edge", DOWNLOAD_URL, None)
    for name, host, base in CANDIDATE_HOSTS:
        if is_cancelled():
            break
        try:
            ms = ping_ms(host, PING_PORT, timeout, attempts, cancelled)
        except Exception:
            ms = None
        if ms is not None and (best[2] is None or ms < best[2]):
            best = (name, base, ms)
    return best


def ping_ms(host: str = PING_HOST, port: int = PING_PORT,
            timeout: float = PING_TIMEOUT_S,
            attempts: int = PING_ATTEMPTS,
            cancelled=None) -> "float | None":
    """Median TCP-connect latency in ms, or None when unreachable."""
    is_cancelled = cancelled or (lambda: False)
    samples = []
    for _ in range(max(1, attempts)):
        if is_cancelled():
            break
        sock = None
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(timeout)
            t0 = time.perf_counter()
            sock.connect((host, port))
            samples.append((time.perf_counter() - t0) * 1000.0)
        except OSError:
            pass
        finally:
            try:
                if sock is not None:
                    sock.close()
            except Exception:
                pass
    if not samples:
        return None
    samples.sort()
    mid = len(samples) // 2
    if len(samples) % 2:
        return samples[mid]
    return (samples[mid - 1] + samples[mid]) / 2.0


def _mbps(num_bytes: int, seconds: float) -> "float | None":
    try:
        if seconds <= 0 or num_bytes <= 0:
            return None
        return (num_bytes * 8.0) / seconds / 1_000_000.0
    except Exception:
        return None


def download_mbps(num_bytes: int = 1_000_000,
                  timeout: float = LEG_TIMEOUT_S,
                  cancelled=None, progress_cb=None,
                  url: str = DOWNLOAD_URL) -> "float | None":
    """Timed GET of `num_bytes`; Mbps or None.

    progress_cb convention (L04): INCREMENTAL (delta_bytes, total) — the
    same contract the parallel streams use (the GUI sums deltas)."""
    is_cancelled = cancelled or (lambda: False)
    try:
        req_url = f"{url}?bytes={int(num_bytes)}" if url == DOWNLOAD_URL else url
        req = urllib.request.Request(req_url, headers={"User-Agent": _UA})
        t0 = time.perf_counter()
        got = 0
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            while True:
                if is_cancelled():
                    return None
                chunk = resp.read(CHUNK)
                if not chunk:
                    break
                got += len(chunk)
                try:
                    if progress_cb is not None:
                        progress_cb(len(chunk), num_bytes)
                except Exception:
                    pass
                # Stop once we have enough (fallback URL ignores ?bytes).
                if got >= num_bytes:
                    break
        dt = time.perf_counter() - t0
        if got <= 0:
            return None
        return _mbps(got, dt)
    except Exception:
        return None


def upload_mbps(num_bytes: int = 1_000_000,
                timeout: float = LEG_TIMEOUT_S,
                cancelled=None,
                url: str = UPLOAD_URL) -> "float | None":
    """Timed POST of random `num_bytes`; Mbps or None."""
    is_cancelled = cancelled or (lambda: False)
    try:
        import os
        payload = os.urandom(int(num_bytes))
    except Exception:
        return None
    if is_cancelled():
        return None
    try:
        req = urllib.request.Request(url, data=payload, method="POST",
                                     headers={"User-Agent": _UA,
                                              "Content-Type": "application/octet-stream"})
        t0 = time.perf_counter()
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            try:
                resp.read(1024)
            except Exception:
                pass
        dt = time.perf_counter() - t0
        return _mbps(len(payload), dt)
    except Exception:
        return None


def _one_download_stream(url: str, num_bytes: int, timeout: float,
                         cancelled, progress_cb, planned_total: int) -> "tuple | None":
    """Single GET stream -> (bytes, start, end) or None. Thread worker."""
    is_cancelled = cancelled or (lambda: False)
    try:
        req_url = f"{url}?bytes={int(num_bytes)}" if url == DOWNLOAD_URL else url
        req = urllib.request.Request(req_url, headers={"User-Agent": _UA})
        got = 0
        t0 = time.perf_counter()
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            while True:
                if is_cancelled():
                    return None
                chunk = resp.read(CHUNK)
                if not chunk:
                    break
                got += len(chunk)
                try:
                    if progress_cb is not None:
                        progress_cb(len(chunk), planned_total)
                except Exception:
                    pass
                if got >= num_bytes:
                    break
        end = time.perf_counter()
        if got <= 0:
            return None
        return (got, t0, end)
    except Exception:
        return None


def _one_upload_stream(url: str, payload: bytes, timeout: float,
                       cancelled) -> "tuple | None":
    """Single POST stream -> (bytes, start, end) or None. Thread worker."""
    is_cancelled = cancelled or (lambda: False)
    if is_cancelled():
        return None
    try:
        req = urllib.request.Request(url, data=payload, method="POST",
                                     headers={"User-Agent": _UA,
                                              "Content-Type": "application/octet-stream"})
        t0 = time.perf_counter()
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            try:
                resp.read(1024)
            except Exception:
                pass
        end = time.perf_counter()
        return (len(payload), t0, end)
    except Exception:
        return None


def download_parallel(num_bytes_per_stream: int, streams: int = PARALLEL_STREAMS,
                      timeout: float = STREAM_TIMEOUT_S,
                      cancelled=None, progress_cb=None,
                      url: str = DOWNLOAD_URL) -> "float | None":
    """Aggregate Mbps over N parallel GET streams (wall-clock across all
    streams). One stream can't fill a fast pipe; four can. None when
    nothing landed (offline/blocked) — never a fake number."""
    import threading as _th
    is_cancelled = cancelled or (lambda: False)
    planned = int(num_bytes_per_stream) * max(1, int(streams))
    results = []
    lock = _th.Lock()

    def _got(n, _total):
        if progress_cb is not None:
            try:
                progress_cb(n, _total)
            except Exception:
                pass

    def _work():
        r = _one_download_stream(url, int(num_bytes_per_stream), timeout,
                                 cancelled, _got, planned)
        if r is not None:
            with lock:
                results.append(r)

    threads = [_th.Thread(target=_work, daemon=True)
               for _ in range(max(1, int(streams)))]
    for t in threads:
        t.start()
    # L04: deadline joins — sequential t.join(timeout+5) stacked to ~100s
    # worst-case (4 streams x 25s). Cap the whole round at timeout+5.
    import time as _time
    deadline = _time.monotonic() + timeout + 5.0
    for t in threads:
        t.join(max(0.0, deadline - _time.monotonic()))
        if is_cancelled():
            break
    if not results:
        return None
    total = sum(b for b, _s, _e in results)
    wall = max(e for _b, _s, e in results) - min(s for _b, s, _e in results)
    return _mbps(total, wall)


def upload_parallel(num_bytes_per_stream: int, streams: int = PARALLEL_STREAMS,
                    timeout: float = STREAM_TIMEOUT_S,
                    cancelled=None,
                    url: str = UPLOAD_URL) -> "float | None":
    """Aggregate Mbps over N parallel POST streams. None when nothing
    landed. Payloads are fresh random bytes per stream."""
    import os as _os
    import threading as _th
    is_cancelled = cancelled or (lambda: False)
    try:
        payloads = [_os.urandom(int(num_bytes_per_stream))
                    for _ in range(max(1, int(streams)))]
    except Exception:
        return None
    results = []
    lock = _th.Lock()

    def _work(p):
        r = _one_upload_stream(url, p, timeout, cancelled)
        if r is not None:
            with lock:
                results.append(r)

    threads = [_th.Thread(target=_work, args=(p,), daemon=True) for p in payloads]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout + 5.0)
        if is_cancelled():
            break
    if not results:
        return None
    total = sum(b for b, _s, _e in results)
    wall = max(e for _b, _s, e in results) - min(s for _b, s, _e in results)
    return _mbps(total, wall)


# --------------------------------------------------------------------------- #
# Usage suitability: Browsing / Gaming / Streaming / Video call (0-5 stars).
# Thresholds from official publisher guidance (2025-26):
#   Netflix help center: HD 5 Mbps, 4K 15; YouTube 4K 20; Disney+ 4K 25.
#   Zoom docs: 1:1 1080p 3.8 down / 3.0 up; group 720p 1.8 / 2.6;
#     ping <150 acceptable, <50 ideal.
#   Console makers (via Tom's Guide): gaming 3+ down, 0.5+ up, <150 ms;
#     smooth play <50 ms; Fortnite-class titles 3-6 Mbps.
#   T-Mobile guide: browsing min 1 / comfy 5 Mbps; 4K + gaming 20-25.
# Pure functions — unit-tested, no I/O.
# --------------------------------------------------------------------------- #

USAGE_ORDER = ("browsing", "gaming", "streaming", "videocall")
USAGE_TITLES = {"browsing": "Browsing", "gaming": "Gaming",
                "streaming": "Streaming", "videocall": "Video call"}
USAGE_FILES = {"browsing": "browsing.png", "gaming": "gaming.png",
               "streaming": "streaming.png", "videocall": "videocall.png"}
USAGE_FALLBACK_EMOJI = {"browsing": "\U0001F310", "gaming": "\U0001F3AE",
                        "streaming": "\U0001F3AC", "videocall": "\U0001F4F9"}
USAGE_WORDS = {5: "Great", 4: "Good", 3: "Fair",
               2: "Slow", 1: "Poor", 0: "No result"}


def _band(value, cuts):
    """Stars for `value` against descending [(limit, stars)]; None -> None."""
    try:
        if value is None:
            return None
        v = float(value)
    except Exception:
        return None
    for limit, stars in cuts:
        if v >= limit:
            return stars
    return 0


def usage_stars(use: str, down=None, up=None, ping=None) -> int:
    """0-5 suitability stars for one use. Missing legs are skipped (grade
    on what's measured); all-missing grades 0, never a fake score."""
    if use == "browsing":
        s = _band(down, [(50, 5), (25, 4), (10, 3), (5, 2), (1, 1)])
        return s if s is not None else 0
    if use == "streaming":
        s = _band(down, [(50, 5), (25, 4), (15, 3), (5, 2), (3, 1)])
        return s if s is not None else 0
    if use == "gaming":
        # Ping-led: timing matters more than bulk for games.
        ps = None
        try:
            if ping is not None:
                p = float(ping)
                ps = 5 if p <= 30 else (4 if p <= 50 else (3 if p <= 80
                        else (2 if p <= 120 else (1 if p <= 180 else 0))))
        except Exception:
            ps = None
        ds = _band(down, [(50, 5), (25, 4), (10, 3), (5, 2), (1, 1)])
        base = ps if ps is not None else ds
        if base is None:
            return 0
        if ds is not None:
            try:
                d = float(down)
                if d < 3:
                    base = min(base, 1)
                elif d < 10:
                    base = min(base, 3)
                elif d < 20:
                    base = min(base, 4)
            except Exception:
                pass
        return base
    if use == "videocall":
        # Worst-leg-wins: a call needs down AND up AND quiet ping.
        ps = None
        try:
            if ping is not None:
                p = float(ping)
                ps = 5 if p <= 40 else (4 if p <= 70 else (3 if p <= 110
                        else (2 if p <= 150 else (1 if p <= 250 else 0))))
        except Exception:
            ps = None
        subs = [
            _band(down, [(25, 5), (10, 4), (5, 3), (3, 2), (1.5, 1)]),
            _band(up, [(10, 5), (5, 4), (3, 3), (1.5, 2), (0.6, 1)]),
            ps,
        ]
        avail = [s for s in subs if s is not None]
        return min(avail) if avail else 0
    return 0


def usage_word(stars) -> str:
    """Great / Good / Fair / Slow / Poor / No result."""
    try:
        return USAGE_WORDS.get(int(stars), "No result")
    except Exception:
        return "No result"


def format_mbps(mbps) -> str:
    """Honest display: '—' when unmeasured, else 'X.X Mbps'."""
    try:
        if mbps is None:
            return "—"
        v = float(mbps)
        if v < 0:
            return "—"
        if v >= 100:
            return f"{v:.0f} Mbps"
        return f"{v:.1f} Mbps"
    except Exception:
        return "—"


def format_ping(ms) -> str:
    """Honest display: '—' when unmeasured, else 'N ms'."""
    try:
        if ms is None:
            return "—"
        return f"{float(ms):.0f} ms"
    except Exception:
        return "—"
