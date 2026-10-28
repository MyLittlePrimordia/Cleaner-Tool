"""H-006 / H-007: bounded transfers, and progress lines that survive `collect`.

H-006: `timeout` on urlopen bounds each socket READ. An origin that drips one
byte at a time never trips it, so a download could sit "in progress" for hours
with only the Stop button as an escape. Both downloaders now also carry a
wall-clock deadline for the whole transfer.

H-007: `run_cmd` collapses DISM/SFC progress lines to one per 10% bucket for
the log. That dedupe ran BEFORE the `collect` branch, so the collected lines -
the same list the winget upgrade summary parses - lost every repeated
percentage. Only the log should be thinned.
"""
from __future__ import annotations

import sys

from app import downloader
from app.utils import TaskContext, run_cmd


def _ctx(logs):
    return TaskContext(log=logs.append, set_status=lambda m: None)


# --------------------------------------------------------------------------- #
# H-006
# --------------------------------------------------------------------------- #

class _Resp:
    """A body that yields `chunks` lines and then EOF."""

    def __init__(self, chunks, headers=None):
        self._chunks = list(chunks)
        self.headers = headers or {}

    def read(self, n=-1):
        return self._chunks.pop(0) if self._chunks else b""

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _Clock:
    """A monotonic clock that jumps 100s on every look."""

    def __init__(self):
        self.now = 0.0

    def monotonic(self):
        self.now += 100.0
        return self.now


def test_a_slow_drip_download_hits_the_wall_clock_deadline(monkeypatch,
                                                          tmp_path):
    logs = []
    payload = _Resp([b"MZ" + b"a" * 10] * 50)
    monkeypatch.setattr("urllib.request.urlopen", lambda *a, **k: payload)
    monkeypatch.setattr(downloader, "time", _Clock())
    dest = str(tmp_path / "part.exe")

    ok = downloader._download(_ctx(logs), "https://example.test/x.exe", dest,
                              expected_size=0, sha256="a" * 64,
                              label="Thing", timeout=600)

    assert ok is False
    assert any("took longer than" in line for line in logs), logs
    assert not __import__("os").path.exists(dest)


def test_a_quick_download_is_unaffected(monkeypatch, tmp_path):
    import hashlib

    payload_bytes = b"MZ" + b"b" * 24
    sha = hashlib.sha256(payload_bytes).hexdigest()
    monkeypatch.setattr("urllib.request.urlopen",
                        lambda *a, **k: _Resp([payload_bytes]))
    dest = str(tmp_path / "ok.exe")
    logs = []
    assert downloader._download(_ctx(logs), "https://example.test/x.exe", dest,
                                expected_size=len(payload_bytes), sha256=sha,
                                label="Thing", timeout=600) is True


# --------------------------------------------------------------------------- #
# H-007
# --------------------------------------------------------------------------- #

def test_collect_keeps_progress_lines_the_log_still_dedupes():
    logs, collected = [], []
    script = ("print('[=====   8.8%   ]'); print('[=====   9.0%   ]');"
              " print('[=====  45.0%   ]'); print('Verification 90% complete.')")
    rc = run_cmd(_ctx(logs), [sys.executable, "-c", script], timeout=60,
                 collect=collected)

    assert rc == 0
    # every line reaches the summary, including the same-bucket repeats
    assert collected == ["[=====   8.8%   ]", "[=====   9.0%   ]",
                         "[=====  45.0%   ]", "Verification 90% complete."]
    # the log keeps one line per bucket, as before
    assert "  > [=====   8.8%   ]" in logs
    assert "  > [=====   9.0%   ]" not in logs
    assert "  > [=====  45.0%   ]" in logs
    assert "  > Verification 90% complete." in logs


def test_collect_is_still_optional():
    logs = []
    rc = run_cmd(_ctx(logs), [sys.executable, "-c", "print('hello')"],
                 timeout=60)
    assert rc == 0
    assert "  > hello" in logs
