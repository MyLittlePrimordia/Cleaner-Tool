"""R-002: the shared streaming loop behind the verified download paths.

`downloader._download` and `self_update.download_verified` both stream a
response into a sibling `.part`, enforce a byte ceiling while writing, cap
the whole transfer with a wall-clock deadline, and hash the bytes as they
arrive. This module holds that loop once. Each caller still owns its own
policy: the pinned size and magic checks, the hash compare, the host
allow-list, and how cancel and failure are reported. Nothing here decides
whether a download is acceptable.
"""
from __future__ import annotations

import hashlib
import time
import urllib.request
from typing import Callable, Optional


def stream_to_part(req: urllib.request.Request, part: str, *, ceiling: int,
                   timeout: int, cancelled: Optional[Callable[[], bool]] = None,
                   on_chunk: Optional[Callable[[int], None]] = None,
                   on_cancel: Optional[Callable[[], Exception]] = None,
                   ) -> "tuple[int, str, object]":
    """Stream `req` into the file `part` and return (bytes, sha256_hex, response).

    `ceiling` bounds the total bytes accepted; going over raises RuntimeError
    before the offending chunk is written. `timeout` is also the wall-clock
    deadline for the whole transfer, not just each socket read (H-006).
    `cancelled()` is checked before every chunk; when it returns True the
    function raises `on_cancel()` if given, else RuntimeError. `on_chunk(got)`
    is called after each chunk is written, for progress.

    The response is returned so the caller can read its headers (for example
    Content-Length for progress) after the stream ends.
    """
    h = hashlib.sha256()
    got = 0
    deadline = time.monotonic() + timeout if timeout else None
    with urllib.request.urlopen(req, timeout=timeout) as resp, \
            open(part, "wb") as f:
        while True:
            if cancelled is not None and cancelled():
                if on_cancel is not None:
                    raise on_cancel()
                raise RuntimeError("download cancelled")
            if deadline is not None and time.monotonic() > deadline:
                raise RuntimeError(
                    f"download took longer than {timeout}s (got {got} bytes)")
            chunk = resp.read(1 << 16)
            if not chunk:
                break
            if got + len(chunk) > ceiling:
                raise RuntimeError(
                    f"download is larger than expected (ceiling {ceiling} bytes)")
            f.write(chunk)
            h.update(chunk)
            got += len(chunk)
            if on_chunk is not None:
                on_chunk(got)
    return got, h.hexdigest(), resp
