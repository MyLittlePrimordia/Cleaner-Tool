"""B-001 / B-002 / I-002: which release is newer, and how that is decided.

The checker used to (a) `int()` every dot-segment of the tag, so a prerelease
tag raised and the surrounding `except` reported "no update", and (b) return
the first versioned release in GitHub's newest-created-first list, so a
backport published later shadowed a newer feature release. Both produce the
same wrong answer — "you are up to date" — with no error shown.
"""
from __future__ import annotations

import pytest

from app import update_checker as uc

_REPO = "MyLittlePrimordia/Cleaner-Tool"
_EXE_URL = ("https://github.com/MyLittlePrimordia/Cleaner-Tool/"
            "releases/download/latest/Cleaner-Tool.exe")
_SHA_URL = ("https://github.com/MyLittlePrimordia/Cleaner-Tool/"
            "releases/download/latest/Cleaner-Tool.exe.sha256")


# --------------------------------------------------------------------------- #
# parse_version
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("text,expected", [
    ("2.1.0", (2, 1, 0)),
    ("v2.1.0", (2, 1, 0)),
    ("2.1", (2, 1)),
    ("2.1.0.1", (2, 1, 0, 1)),
    ("2.1.0-beta.2", (2, 1, 0)),          # B-001: suffix is not an error
    ("v2.1.0+build.7", (2, 1, 0)),
    ("v2.1.0 (hotfix)", (2, 1, 0)),
    ("latest", None),
    ("vNext", None),
    ("", None),
    (None, None),
])
def test_a_version_key_ignores_a_suffix(text, expected):
    assert uc.parse_version(text) == expected


# --------------------------------------------------------------------------- #
# _pick_release
# --------------------------------------------------------------------------- #

def _rel(tag, **extra):
    return {"tag_name": tag, **extra}


def test_the_highest_version_wins_even_when_listed_last():
    """GitHub lists by creation date: a backport can arrive after a release."""
    picked = uc._pick_release([_rel("v2.1.1"), _rel("v2.2.0"), _rel("latest")])
    assert picked["tag_name"] == "v2.2.0"


def test_a_higher_version_suffix_does_not_break_order():
    picked = uc._pick_release([_rel("v2.1.9"), _rel("v2.1.10")])
    assert picked["tag_name"] == "v2.1.10", "numeric, not lexicographic"


def test_the_rolling_release_is_only_the_fallback():
    assert uc._pick_release([_rel("latest")])["tag_name"] == "latest"
    assert uc._pick_release([_rel("v1.0.0"), _rel("latest")])["tag_name"] == "v1.0.0"


def test_unreadable_tags_are_skipped_not_fatal():
    picked = uc._pick_release([_rel("nightly"), _rel("v2.0.0")])
    assert picked["tag_name"] == "v2.0.0"
    assert uc._pick_release([_rel("nightly")]) is None
    assert uc._pick_release([]) is None


# --------------------------------------------------------------------------- #
# check_for_updates end to end (network stubbed)
# --------------------------------------------------------------------------- #

def _release(tag, assets):
    return {"tag_name": tag, "assets": assets, "body": ""}


def _exe_asset():
    return {"name": "Cleaner-Tool.exe", "browser_download_url": _EXE_URL,
            "size": 42, "digest": "sha256:" + "a" * 64}


def test_a_prerelease_tag_still_reports_the_update(monkeypatch):
    monkeypatch.setattr(uc, "fetch_latest_release_info",
                        lambda repo, timeout=20: _release("v2.1.0-beta.2",
                                                          [_exe_asset()]))
    result = uc.check_for_updates(_REPO)
    assert result["update_available"] is True
    assert result["error"] is None


def test_an_unreadable_tag_says_so_instead_of_claiming_up_to_date(monkeypatch):
    monkeypatch.setattr(uc, "fetch_latest_release_info",
                        lambda repo, timeout=20: _release("vNext", [_exe_asset()]))
    result = uc.check_for_updates(_REPO)
    assert result["update_available"] is False
    assert "Could not read the release version" in result["error"]


def test_the_rolling_tag_reads_the_published_checksum(monkeypatch):
    """I-002: a .sha256 asset costs bytes; hashing the exe costs the binary."""
    monkeypatch.setattr(
        uc, "fetch_latest_release_info",
        lambda repo, timeout=20: _release(
            "latest", [_exe_asset(),
                       {"name": uc.SHA_ASSET_NAME,
                        "browser_download_url": _SHA_URL, "size": 80}]))
    monkeypatch.setattr(uc, "get_current_exe_hash", lambda: "a" * 64)
    monkeypatch.setattr(uc, "fetch_sha256", lambda url, timeout=20: "b" * 64)

    def _never(*_a, **_k):
        raise AssertionError("the whole exe must not be streamed")

    monkeypatch.setattr(uc, "get_remote_exe_hash", _never)

    result = uc.check_for_updates(_REPO)
    assert result["update_available"] is True
    assert result["error"] is None


def test_the_rolling_tag_falls_back_to_hashing_without_a_checksum(monkeypatch):
    monkeypatch.setattr(uc, "fetch_latest_release_info",
                        lambda repo, timeout=20: _release("latest",
                                                          [_exe_asset()]))
    monkeypatch.setattr(uc, "get_current_exe_hash", lambda: "a" * 64)
    monkeypatch.setattr(uc, "get_remote_exe_hash", lambda url, timeout: "c" * 64)

    result = uc.check_for_updates(_REPO)
    assert result["update_available"] is True
