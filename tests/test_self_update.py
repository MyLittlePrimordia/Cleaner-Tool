"""U-006: the verified in-app update logic, with no Tk and no network.

Every rule that makes a self-update safe is pinned here: a download with no
digest is refused, a bad digest is refused and removed, a non-MZ file is
refused, a source run / read-only install cannot self-replace, and the swap
helper is generated with a rollback path. The downloader's HTTP layer is
replaced with an in-memory body, so nothing here touches the disk outside
tmp_path or the network at all.
"""
from __future__ import annotations

import hashlib
import io
import os

from app import self_update as su

GOOD_URL = ("https://github.com/MyLittlePrimordia/Cleaner-Tool/releases/"
            "download/latest/Cleaner-Tool.exe")
GOOD_SHA_URL = ("https://github.com/MyLittlePrimordia/Cleaner-Tool/releases/"
                "download/latest/Cleaner-Tool.exe.sha256")
HEX = "abcdef0123456789" * 4          # 64 valid hex chars


# --------------------------------------------------------------- checksum file
def test_parse_sha256_accepts_both_formats():
    assert su.parse_sha256("%s  Cleaner-Tool.exe" % HEX) == HEX
    assert su.parse_sha256(HEX) == HEX
    assert su.parse_sha256(HEX.upper()) == HEX          # normalised to lower
    assert su.parse_sha256("") == ""
    assert su.parse_sha256("not a digest") == ""
    assert su.parse_sha256("abc123  Cleaner-Tool.exe") == ""   # too short
    assert su.parse_sha256("g" * 64) == ""                     # not hex


# ------------------------------------------------------- asset trust / resolve
def test_asset_rejects_an_untrusted_url():
    rel = {"assets": [{"name": "Cleaner-Tool.exe",
                       "browser_download_url": "http://evil.example/x.exe",
                       "digest": "sha256:" + HEX, "size": 10}]}
    assert su._asset(rel, su.UPDATE_REPO, su.ASSET_NAME) == {}


def test_asset_rejects_a_lookalike_host():
    rel = {"assets": [{"name": "Cleaner-Tool.exe",
                       "browser_download_url":
                           "https://github.com.evil.example/Cleaner-Tool.exe"}]}
    assert su._asset(rel, su.UPDATE_REPO, su.ASSET_NAME) == {}


def test_asset_reads_the_github_digest():
    rel = {"assets": [{"name": "Cleaner-Tool.exe",
                       "browser_download_url": GOOD_URL,
                       "digest": "sha256:" + HEX, "size": 1234}]}
    got = su._asset(rel, su.UPDATE_REPO, su.ASSET_NAME)
    assert got["url"] == GOOD_URL and got["sha256"] == HEX and got["size"] == 1234


def test_resolve_update_prefers_the_published_digest():
    rel = {"assets": [{"name": "Cleaner-Tool.exe",
                       "browser_download_url": GOOD_URL,
                       "digest": "sha256:" + HEX, "size": 99}]}
    got = su.resolve_update(rel, su.UPDATE_REPO)
    assert got["sha256"] == HEX and got["url"] == GOOD_URL


def test_resolve_update_falls_back_to_the_sha256_sibling(monkeypatch):
    monkeypatch.setattr(su, "fetch_sha256", lambda url, timeout=20: HEX)
    rel = {"assets": [
        {"name": "Cleaner-Tool.exe", "browser_download_url": GOOD_URL,
         "size": 99},
        {"name": "Cleaner-Tool.exe.sha256",
         "browser_download_url": GOOD_SHA_URL, "size": 80},
    ]}
    assert su.resolve_update(rel, su.UPDATE_REPO)["sha256"] == HEX


def test_resolve_update_refuses_without_any_checksum(monkeypatch):
    monkeypatch.setattr(su, "fetch_sha256", lambda url, timeout=20: "")
    rel = {"assets": [{"name": "Cleaner-Tool.exe",
                       "browser_download_url": GOOD_URL, "size": 99}]}
    assert su.resolve_update(rel, su.UPDATE_REPO) == {}


# ------------------------------------------------------------ can_self_update
def test_can_self_update_refuses_a_source_run(monkeypatch):
    monkeypatch.setattr(su, "is_frozen", lambda: False)
    ok, why = su.can_self_update(r"C:\x\Cleaner-Tool.exe")
    assert ok is False and "source" in why.lower()


def test_can_self_update_refuses_a_non_exe(monkeypatch, tmp_path):
    monkeypatch.setattr(su, "is_frozen", lambda: True)
    ok, why = su.can_self_update(str(tmp_path / "main.py"))
    assert ok is False and "not a windows" in why.lower()


def test_can_self_update_refuses_a_readonly_folder(monkeypatch, tmp_path):
    monkeypatch.setattr(su, "is_frozen", lambda: True)
    monkeypatch.setattr(su, "_dir_writable", lambda p: False)
    exe = tmp_path / "Cleaner-Tool.exe"
    exe.write_bytes(b"MZ")
    ok, why = su.can_self_update(str(exe))
    assert ok is False and "writable" in why.lower()


def test_can_self_update_accepts_a_writable_install(monkeypatch, tmp_path):
    monkeypatch.setattr(su, "is_frozen", lambda: True)
    exe = tmp_path / "Cleaner-Tool.exe"
    exe.write_bytes(b"MZ")
    assert su.can_self_update(str(exe)) == (True, "")


# ---------------------------------------------------------- download_verified
def _serve(payload: bytes):
    """An in-memory stand-in for urllib.request.urlopen."""

    class _Resp:
        def __init__(self, data):
            self._b = io.BytesIO(data)

        def read(self, n=-1):
            return self._b.read(n)

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def _open(req, timeout=None):
        return _Resp(payload)
    return _open


def test_download_verified_accepts_a_matching_file(monkeypatch, tmp_path):
    monkeypatch.setattr(su, "_MIN_BYTES", 4)
    payload = b"MZ" + b"hello world" * 4
    sha = hashlib.sha256(payload).hexdigest()
    monkeypatch.setattr("urllib.request.urlopen", _serve(payload))
    dest = str(tmp_path / "new.exe")
    ok, msg = su.download_verified(GOOD_URL, sha, len(payload), dest)
    assert ok and msg == ""
    with open(dest, "rb") as f:
        assert f.read() == payload


def test_download_verified_removes_a_hash_mismatch(monkeypatch, tmp_path):
    monkeypatch.setattr(su, "_MIN_BYTES", 4)
    payload = b"MZ" + b"x" * 40
    monkeypatch.setattr("urllib.request.urlopen", _serve(payload))
    dest = str(tmp_path / "new.exe")
    ok, msg = su.download_verified(GOOD_URL, "a" * 64, len(payload), dest)
    assert not ok and "SHA-256" in msg
    assert not os.path.exists(dest)


def test_download_verified_removes_a_non_executable(monkeypatch, tmp_path):
    monkeypatch.setattr(su, "_MIN_BYTES", 4)
    payload = b"PK\x03\x04" + b"z" * 40          # a zip, not an exe
    sha = hashlib.sha256(payload).hexdigest()
    monkeypatch.setattr("urllib.request.urlopen", _serve(payload))
    dest = str(tmp_path / "new.exe")
    ok, msg = su.download_verified(GOOD_URL, sha, len(payload), dest)
    assert not ok and "executable" in msg.lower()
    assert not os.path.exists(dest)


def test_download_verified_enforces_the_expected_size(monkeypatch, tmp_path):
    monkeypatch.setattr(su, "_MIN_BYTES", 1)
    payload = b"MZ" + b"z" * 100
    sha = hashlib.sha256(payload).hexdigest()
    monkeypatch.setattr("urllib.request.urlopen", _serve(payload))
    dest = str(tmp_path / "new.exe")
    ok, msg = su.download_verified(GOOD_URL, sha, 10, dest)   # pinned 10 < 102
    assert not ok and "larger than expected" in msg
    assert not os.path.exists(dest)


def test_download_verified_honours_cancel(monkeypatch, tmp_path):
    monkeypatch.setattr(su, "_MIN_BYTES", 1)
    payload = b"MZ" + b"z" * 10
    monkeypatch.setattr("urllib.request.urlopen", _serve(payload))
    dest = str(tmp_path / "new.exe")
    ok, msg = su.download_verified(GOOD_URL, hashlib.sha256(payload).hexdigest(),
                                   len(payload), dest,
                                   cancelled=lambda: True)
    assert not ok and "cancelled" in msg.lower()


# ------------------------------------------------------------------- staging
def test_stage_then_staged_ready(monkeypatch, tmp_path):
    monkeypatch.setattr(su, "_MIN_BYTES", 4)
    monkeypatch.setattr(su, "staging_dir", lambda: str(tmp_path))
    payload = b"MZ" + b"abcd" * 10
    sha = hashlib.sha256(payload).hexdigest()
    monkeypatch.setattr("urllib.request.urlopen", _serve(payload))
    ok, msg = su.stage_download(GOOD_URL, sha, len(payload))
    assert ok, msg
    assert su.staged_ready() == su.staged_path()


def test_staged_ready_is_empty_when_nothing_is_staged(monkeypatch, tmp_path):
    monkeypatch.setattr(su, "staging_dir", lambda: str(tmp_path))
    assert su.staged_ready() == ""


def test_staged_ready_rejects_a_tampered_file(monkeypatch, tmp_path):
    monkeypatch.setattr(su, "_MIN_BYTES", 4)
    monkeypatch.setattr(su, "staging_dir", lambda: str(tmp_path))
    payload = b"MZ" + b"abcd" * 10
    sha = hashlib.sha256(payload).hexdigest()
    monkeypatch.setattr("urllib.request.urlopen", _serve(payload))
    assert su.stage_download(GOOD_URL, sha, len(payload))[0]
    # someone rewrites the staged bytes after the download
    with open(su.staged_path(), "wb") as f:
        f.write(b"MZ" + b"EVIL" * 10)
    assert su.staged_ready() == ""


# -------------------------------------------------------------- swap helper
def test_build_swap_script_quotes_and_rolls_back():
    s = su.build_swap_script(r"C:\App\Cleaner-Tool.exe",
                             r"C:\Temp\new.exe", relaunch=True)
    assert 'set "TARGET=C:\\App\\Cleaner-Tool.exe"' in s
    assert 'set "NEW=C:\\Temp\\new.exe"' in s
    assert ":rollback" in s
    assert 'start "" "%TARGET%"' in s
    assert 'del "%~f0"' in s
    assert s.endswith("\r\n")


def test_build_swap_script_can_skip_relaunch():
    s = su.build_swap_script("a.exe", "b.exe", relaunch=False)
    assert 'start ""' not in s
    assert ":rollback" in s          # rollback is always present


def test_schedule_swap_refuses_when_not_frozen(monkeypatch, tmp_path):
    monkeypatch.setattr(su, "is_frozen", lambda: False)
    ok, _ = su.schedule_swap("a.exe", "b.exe", spawn=lambda *a, **k: None)
    assert ok is False


def test_schedule_swap_writes_and_launches_a_script(monkeypatch, tmp_path):
    monkeypatch.setattr(su, "is_frozen", lambda: True)
    target = tmp_path / "Cleaner-Tool.exe"
    target.write_bytes(b"MZ")
    staged = tmp_path / "new.exe"
    staged.write_bytes(b"MZ")
    seen = {}

    def spawn(argv, **kw):
        seen["argv"] = argv
        seen["kw"] = kw

    ok, path = su.schedule_swap(str(target), str(staged), spawn=spawn)
    try:
        assert ok and os.path.isfile(path)
        assert seen["argv"][:2] == ["cmd", "/c"]
        assert seen["argv"][2] == path
        with open(path, "r", encoding="utf-8") as f:
            body = f.read()
        assert str(target) in body and str(staged) in body
    finally:
        if os.path.exists(path):
            os.remove(path)
