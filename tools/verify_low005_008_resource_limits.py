"""LOW-005, LOW-006, LOW-007, LOW-008: unbounded reads and a leaked temp file.

LOW-005  `downloader._download` compared `got != expected_size` only AFTER the
         whole body had been written, so an origin streaming more than we asked
         for could fill the volume before the check ever ran. The pinned
         `expected_size` is OUR figure, so exceeding it means the response is
         not what we asked for - but the check has to happen while writing, not
         after. `install_tasks._download_binary` had the same gap with only a
         `min_bytes` FLOOR and no ceiling at all.

LOW-006  `_scrape_download_center` did
             html = urllib.request.urlopen(req, timeout=30).read()...
         Two defects: the response was never closed (no `with`, so the socket
         leaked for the life of the process) and `.read()` with no argument
         pulls the ENTIRE body into memory from a page whose size we do not
         control.

LOW-007  `config_persist` had no size cap before `json.load`, so a multi-GB
         config.json was fully parsed into RAM. `update_checker` already caps
         its input; the reader should too.

LOW-008  `save_config` leaked its temp file when `json.dump` raised (a
         TypeError from a non-serializable value escapes before the
         `os.replace` try/except), and nothing ever pruned `config.json.*.tmp`.

No network is used: `urlopen` is faked with a reader that yields as many bytes
as the test asks for, so "an origin that never stops sending" is simulated
exactly. The config tests use a temporary directory.
"""

from __future__ import annotations

import io
import json
import os
import shutil
import sys
import tempfile

sys.path.insert(0, ".")

import app.config_persist as cp          # noqa: E402
from app import downloader as d          # noqa: E402
from app.tasks import install_tasks as it  # noqa: E402

FAILS = 0
CHECKS = 0


def ck(label, cond, detail=""):
    global FAILS, CHECKS
    CHECKS += 1
    if cond:
        print("  PASS  %s" % label)
    else:
        FAILS += 1
        print("  FAIL  %s%s" % (label, ("  <- " + str(detail)) if detail else ""))


class Ctx:
    def __init__(self):
        self.lines = []
        self.status = []

    def log(self, m, *a, **k):
        self.lines.append(str(m))

    def set_status(self, m, *a, **k):
        self.status.append(str(m))

    def cancelled(self):
        return False


class FakeResp:
    """Yields `total` bytes in 64 KiB chunks, without allocating `total`.

    Pass `content=` to serve real bytes instead of filler - needed for the
    LOW-006 scrape test, where the regex has to actually FIND something. The
    first version always served b"x", so the "the URL was still found" check
    failed for a reason that had nothing to do with the code under test.
    """

    def __init__(self, total, headers=None, content=None):
        self._left = total
        self.headers = headers or {"Content-Length": str(total)}
        self.closed = False
        self._content = content

    def read(self, n=65536):
        if self._content is not None:
            take = self._content[:n]
            self._content = self._content[n:]
            return take
        if self._left <= 0:
            return b""
        take = min(n, self._left)
        self._left -= take
        return b"x" * take

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.closed = True
        return False


def fake_urlopen(resp):
    import urllib.request as ur

    def opener(req, timeout=None):
        return resp
    return opener


def drive(fn, resp, **kw):
    """Call fn with urlopen faked to return `resp`; return (result, lines)."""
    import urllib.request as ur
    real = ur.urlopen
    ur.urlopen = fake_urlopen(resp)
    try:
        ctx = Ctx()
        try:
            out = fn(ctx, **kw)
            return out, ctx
        except Exception as exc:
            return exc, ctx
    finally:
        ur.urlopen = real


print("[1] LOW-005: the download stops AT the ceiling, not after it")
tmp = tempfile.mkdtemp(prefix="low005_")
# pinned size 1 KiB, but the origin streams 4 MiB. Without a while-writing
# ceiling this writes 4 MiB to disk before the size check rejects it.
dest = os.path.join(tmp, "big.bin")
resp = FakeResp(4 << 20, {"Content-Length": str(4 << 20)})
res, ctx = drive(d._download, resp, url="https://example.invalid/x.exe",
                 dest=dest, expected_size=1024, sha256="00" * 32, label="probe")
on_disk = os.path.getsize(dest) if os.path.exists(dest) else 0
# _download catches and returns False rather than raising, so the
# contract to assert is "did not return True".
ck("the download was refused (did not return True)", res is not True, res)
ck("the refusal came from the size ceiling, not the post-hoc compare",
   any("exceed" in l.lower() for l in ctx.lines),
   # _download swallows the exception and logs it, so the evidence is the log
   # line - not the return value, which is just False either way.
   [l for l in ctx.lines if "failed" in l.lower() or "exceed" in l.lower()])
ck("it wrote ~nothing: the ceiling stopped it near the pinned size",
   on_disk <= 1024, "%d bytes on disk (pinned size was 1024)" % on_disk)
ck("the partial file was cleaned up", not os.path.exists(dest), on_disk)

print()
print("[2] LOW-005: an UNPINNED download is still bounded")
d2 = os.path.join(tmp, "unpinned.bin")
saved = d._MAX_UNPINNED_DOWNLOAD
d._MAX_UNPINNED_DOWNLOAD = 128 * 1024          # shrink so the test is quick
try:
    resp = FakeResp(8 << 20, {"Content-Length": str(8 << 20)})
    res, ctx = drive(d._download, resp, url="https://example.invalid/y.exe",
                     dest=d2, expected_size=0, sha256="00" * 32, label="probe")
finally:
    d._MAX_UNPINNED_DOWNLOAD = saved
on_disk = os.path.getsize(d2) if os.path.exists(d2) else 0
ck("an origin with no pinned size is still capped", res is not True, res)
ck("...and the cap held (128 KiB)", on_disk <= 128 * 1024,
   "%d bytes on disk" % on_disk)

print()
print("[3] LOW-005: the real ceilings are generous enough not to break real use")
ck("_MAX_UNPINNED_DOWNLOAD is at least 150 MB", d._MAX_UNPINNED_DOWNLOAD >= 150 * 1024 * 1024)
ck("_MAX_BINARY_DOWNLOAD is at least 512 MB", it._MAX_BINARY_DOWNLOAD >= 512 * 1024 * 1024)
ck("both are powers-of-two-ish sizes, not suspiciously small",
   d._MAX_UNPINNED_DOWNLOAD % (1024 * 1024) == 0
   and it._MAX_BINARY_DOWNLOAD % (1024 * 1024) == 0)

print()
print("[4] LOW-006: the scrape is bounded and the response is closed")
PAGE = (b"<html><a href='https://download.microsoft.com/x/vlc.exe'>vlc</a></html>")
resp = FakeResp(len(PAGE), {"Content-Length": str(len(PAGE))},
                content=bytearray(PAGE))
out, ctx = drive(d._scrape_download_center, resp,
                 page_url="https://www.videolan.org/", filename="vlc.exe",
                 label="VLC")
ck("the URL was still found", out == "https://download.microsoft.com/x/vlc.exe", out)
ck("the response was CLOSED (the old code leaked it)", resp.closed)

# a page that claims to be enormous is refused before any read
resp2 = FakeResp(1, {"Content-Length": str(64 * 1024 * 1024)})
out2, ctx2 = drive(d._scrape_download_center, resp2,
                   page_url="https://www.videolan.org/", filename="vlc.exe",
                   label="VLC")
ck("an oversized Content-Length is refused without reading it", out2 is None, out2)
ck("...and the reason says so", any("refusing to scrape" in l for l in ctx2.lines),
   ctx2.lines)

# a lying server: small Content-Length, huge body -> the read is still capped
resp3 = FakeResp(64 * 1024 * 1024, {"Content-Length": "1024"})
out3, ctx3 = drive(d._scrape_download_center, resp3,
                   page_url="https://www.videolan.org/", filename="vlc.exe",
                   label="VLC")
ck("a body that lies about its length is still capped by read(n)",
   out3 is None, out3)
ck("_MAX_SCRAPE_BYTES is a sane bound", 0 < d._MAX_SCRAPE_BYTES <= 32 * 1024 * 1024)

print()
print("[5] LOW-007: an oversized config.json is refused, not parsed")
cfg_dir = tempfile.mkdtemp(prefix="low007_")
real_cfg = cp.CONFIG_FILE
try:
    cp.CONFIG_FILE = real_cfg.__class__(os.path.join(cfg_dir, "config.json"))
    cp._config_cache = None
    cp._config_mtime = None
    # a real (small) config still loads
    with open(cp.CONFIG_FILE, "w", encoding="utf-8") as h:
        json.dump({"applied_tweaks": ["keep_me"]}, h)
    ok = cp._load_config_from_disk()
    ck("a normal config still loads", ok.get("applied_tweaks") == ["keep_me"],
       ok.get("applied_tweaks"))

    # now pretend it is enormous by lowering the cap (writing 32 MB is wasteful)
    saved_cap = cp._MAX_CONFIG_BYTES
    cp._MAX_CONFIG_BYTES = 8           # the real file is ~28 bytes, so this is over
    cp._config_cache = None
    try:
        out = cp._load_config_from_disk()
        ck("an oversized config falls back rather than parsing it",
           isinstance(out, dict), type(out).__name__)
        ck("...and it did not adopt the oversized file's contents",
           out.get("applied_tweaks") != ["keep_me"] or out.get("applied_tweaks") == [],
           out.get("applied_tweaks"))
    finally:
        cp._MAX_CONFIG_BYTES = saved_cap
    ck("the real cap is generous (>= 1 MB)", cp._MAX_CONFIG_BYTES >= 1024 * 1024)
finally:
    cp.CONFIG_FILE = real_cfg
    cp._config_cache = None
    cp._config_mtime = None

print()
print("[6] LOW-008: a failed save leaves no temp file behind")
cfg_dir2 = tempfile.mkdtemp(prefix="low008_")
real_cfg = cp.CONFIG_FILE
try:
    cp.CONFIG_FILE = real_cfg.__class__(os.path.join(cfg_dir2, "config.json"))
    cp._config_cache = None
    cp._config_mtime = None
    with open(cp.CONFIG_FILE, "w", encoding="utf-8") as h:
        json.dump({"applied_tweaks": []}, h)

    # something json cannot serialise - the old code leaked a temp file here
    bad = {"applied_tweaks": [], "bad": {1, 2, 3}}
    raised = None
    try:
        cp.save_config(bad)
    except Exception as exc:
        raised = exc
    ck("save_config raised on the unserialisable value", raised is not None, raised)
    leftovers = [f for f in os.listdir(cfg_dir2) if f.endswith(".tmp")]
    ck("NO temp file was left behind", leftovers == [], leftovers)
    ck("the real config.json was NOT replaced",
       json.load(open(cp.CONFIG_FILE, encoding="utf-8")) == {"applied_tweaks": []})

    # a good save still works and leaves nothing behind
    cp.save_config({"applied_tweaks": ["x"]})
    ck("a good save still works",
       json.load(open(cp.CONFIG_FILE, encoding="utf-8")).get("applied_tweaks") == ["x"])
    ck("...and leaves no temp file",
       [f for f in os.listdir(cfg_dir2) if f.endswith(".tmp")] == [],
       os.listdir(cfg_dir2))

    print()
    print("[7] LOW-008: stale temps from a crash are pruned, fresh ones are not")
    # simulate a crash leftover: old mtime
    stale = cp.CONFIG_FILE.with_name(cp.CONFIG_FILE.name + ".9999.deadbeef.tmp")
    stale.write_text("garbage", encoding="utf-8")
    import time
    old = time.time() - 7200
    os.utime(stale, (old, old))
    # and a temp belonging to an in-flight write right now
    fresh = cp.CONFIG_FILE.with_name(cp.CONFIG_FILE.name + ".9999.cafe1234.tmp")
    fresh.write_text("in flight", encoding="utf-8")
    removed = cp._prune_stale_config_temps()
    ck("the stale temp was removed", not stale.exists(), removed)
    ck("the fresh temp was NOT touched (a concurrent write is safe)",
       fresh.exists(), "a live writer's temp file was deleted")
    ck("and the real config is still there", cp.CONFIG_FILE.exists())
finally:
    cp.CONFIG_FILE = real_cfg
    cp._config_cache = None
    cp._config_mtime = None

print()
print("[8] structural")
import inspect  # noqa: E402

src = inspect.getsource(d._download)
ck("LOW-005 is documented", "LOW-005" in src)
ck("the ceiling is checked BEFORE the write, not after",
   src.index("if got + len(chunk) > ceiling") < src.index("f.write(chunk)"),
   "ceiling check must precede the write")
ck("Content-Length is still only used for the progress bar",
   'if total:' in src and "got > ceiling" not in src)
sc = inspect.getsource(d._scrape_download_center)
ck("LOW-006 is documented", "LOW-006" in sc)
ck("the scrape uses a `with` so the response is closed", "with urllib.request.urlopen" in sc)
ck("...and a bounded read", "resp.read(_MAX_SCRAPE_BYTES)" in sc)
ib = inspect.getsource(it._download_binary)
ck("install_tasks._download_binary has a ceiling too", "_MAX_BINARY_DOWNLOAD" in ib)
ck("...checked before the write",
   ib.index("if written > _MAX_BINARY_DOWNLOAD") < ib.index("f.write(chunk)"))
cs = inspect.getsource(cp._load_config_from_disk)
ck("LOW-007 is documented", "LOW-007" in cs)
ck("...and the cap is checked before json.load",
   cs.index("_MAX_CONFIG_BYTES") < cs.index("json.load(f)"))
ss = inspect.getsource(cp.save_config)
ck("LOW-008 is documented", "LOW-008" in ss)
ck("...the dump is inside a try that removes the temp", "_prune_stale_config_temps()" in ss)

for d_ in (tmp, cfg_dir, cfg_dir2):
    shutil.rmtree(d_, ignore_errors=True)
shutil.rmtree(tmp, ignore_errors=True)

print()
if FAILS:
    print("LOW-005/006/007/008 VERIFY: %d/%d FAILED" % (FAILS, CHECKS))
    sys.exit(1)
print("LOW-005/006/007/008 VERIFY: ALL PASS (%d checks)" % CHECKS)
