"""Download a driver file with a progress callback.

Streams to <name>.part and renames on success, so an interrupted download
never leaves a file that looks complete. Refuses any host outside the
catalog's CDN allow-list (a redirect cannot smuggle in another host: the
FINAL url is checked too). Windows Update file names end in the file's SHA-1
(".../<guid>_<40 hex>.cab"), so when the name carries one it is verified; a
mismatch deletes the file and fails. Signature validity is enforced again by
pnputil at install time.
"""
from __future__ import annotations

import hashlib
import os
import re
import time
import urllib.parse
import urllib.request
from typing import Callable, Optional

from app.drivers.catalog import CatalogError, UA, allowed_url

CHUNK = 64 * 1024


def filename_from_url(url: str) -> str:
    name = os.path.basename(urllib.parse.urlparse(url).path) or "driver.cab"
    return re.sub(r"[^A-Za-z0-9._-]", "_", name)[:140]


def expected_sha1(filename: str) -> Optional[str]:
    m = re.search(r"([0-9a-fA-F]{40})(?:\.[A-Za-z0-9]+)?$", filename)
    return m.group(1).lower() if m else None


def download(url: str, dest_dir: str,
             progress: Optional[Callable[[int, int, float], None]] = None,
             should_stop: Callable[[], bool] = lambda: False,
             opener: Callable = None) -> str:
    """Download `url` into `dest_dir`; return the file path.

    progress(done_bytes, total_bytes_or_0, bytes_per_second)
    Raises CatalogError (bad host / hash), RuntimeError (cancelled) or OSError.
    """
    if not allowed_url(url):
        raise CatalogError("Refusing to download from an unexpected host.")
    os.makedirs(dest_dir, exist_ok=True)
    name = filename_from_url(url)
    final = os.path.join(dest_dir, name)
    part = final + ".part"
    opener = opener or (lambda req: urllib.request.urlopen(req, timeout=30))
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    sha = hashlib.sha1()
    done = 0
    t0 = time.time()
    with opener(req) as resp:
        final_url = getattr(resp, "geturl", lambda: url)()
        if not allowed_url(final_url):
            raise CatalogError("The download was redirected to an "
                               "unexpected host - refused.")
        total = int(getattr(resp, "headers", {}).get("Content-Length", 0) or 0)
        with open(part, "wb") as f:
            while True:
                if should_stop():
                    f.close()
                    os.remove(part)
                    raise RuntimeError("Download cancelled.")
                chunk = resp.read(CHUNK)
                if not chunk:
                    break
                f.write(chunk)
                sha.update(chunk)
                done += len(chunk)
                if progress:
                    elapsed = max(0.001, time.time() - t0)
                    progress(done, total, done / elapsed)
    want = expected_sha1(name)
    if want and sha.hexdigest() != want:
        os.remove(part)
        raise CatalogError("The downloaded file's checksum did not match "
                           "its name - it was discarded.")
    os.replace(part, final)
    return final
