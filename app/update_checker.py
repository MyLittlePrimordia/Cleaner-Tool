"""
Update checker - detects new Cleaner Tool releases from GitHub.

Uses the GitHub API to fetch the latest release information and compares
the release's version tag (e.g. "v2.1.0" -> 2.1.0) against APP_VERSION to
decide whether an update is available. No third-party dependencies - uses
stdlib urllib.request only. get_current_exe_hash() is exposed so callers can
verify the downloaded exe's integrity, but the update decision itself is
version-based, not hash-based.
"""

import hashlib
import json
import os
import re
import sys
import urllib.request
import urllib.error

#: A release tag's version head: `v2.1.0`, `2.1`, `2.1.0-beta.2`, `2.1.0+ci`.
#: Only the leading numeric segments are compared; a suffix is NOT a reason to
#: call the release unreadable (B-001: `int("0-beta")` used to raise, and the
#: surrounding `except` reported "no update" for a build newer than this one).
_VERSION_HEAD = re.compile(r"^v?(\d+(?:\.\d+)*)")

#: The checksum asset the build workflow publishes next to the exe.
SHA_ASSET_NAME = "Cleaner-Tool.exe.sha256"


def parse_version(text: str) -> "tuple[int, ...] | None":
    """Numeric version key for a tag or version string, or None.

    `v2.1.0` -> (2, 1, 0); `2.1.0-beta.2` -> (2, 1, 0); `latest` -> None.
    """
    m = _VERSION_HEAD.match(str(text or "").strip())
    if not m:
        return None
    try:
        return tuple(int(part) for part in m.group(1).split("."))
    except ValueError:
        return None


def _pick_release(releases) -> "dict | None":
    """The release with the HIGHEST version, else the rolling `latest` one.

    B-002: the old loop returned the first versioned release in the API's
    order, which is newest-CREATED first. A backport or re-cut published after
    a newer feature release therefore shadowed it, and the check reported "up
    to date" while a newer build existed.
    """
    best = None
    best_key = None
    latest = None
    for release in releases or []:
        if not isinstance(release, dict):
            continue
        tag = str(release.get("tag_name") or "")
        if not tag:
            continue
        if tag == "latest":
            latest = release
            continue
        key = parse_version(tag)
        if key is None:
            continue
        if best_key is None or key > best_key:
            best, best_key = release, key
    return best if best is not None else latest


def get_current_exe_path() -> str:
    """Get the path to the current running executable."""
    if getattr(sys, 'frozen', False):
        # Running as compiled exe
        return sys.executable
    else:
        # Running as Python script
        return os.path.abspath(sys.argv[0])


def get_current_exe_hash() -> str:
    """Calculate SHA256 hash of the current executable."""
    exe_path = get_current_exe_path()
    try:
        sha256 = hashlib.sha256()
        with open(exe_path, 'rb') as f:
            # Read in chunks to handle large files
            for chunk in iter(lambda: f.read(8192), b''):
                sha256.update(chunk)
        return sha256.hexdigest()
    except Exception as exc:
        from app.utils import log_or_swallow
        log_or_swallow(None, "update.exe_hash", exc)
        return ""


def fetch_latest_release_info(repo: str = "owner/repo",
                              timeout: int = 10) -> "dict | None":
    """Fetch latest release info from GitHub API.
    
    Args:
        repo: GitHub repository in format "owner/repo"
        timeout: Request timeout in seconds
        
    Returns:
        dict with release info, or None on failure (the annotation says so —
        every caller already handles None)
    """
    try:
        # Fetch all releases and find the latest versioned one (not "latest")
        url = f"https://api.github.com/repos/{repo}/releases"
        req = urllib.request.Request(
            url,
            headers={
                "User-Agent": "CleanerTool/2.0",
                "Accept": "application/vnd.github.v3+json"
            }
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            if resp.status == 200:
                releases = json.loads(resp.read().decode('utf-8'))
                # Highest version wins; the rolling "latest" is only the
                # fallback when no versioned release exists at all.
                return _pick_release(releases)
            return None
    except urllib.error.HTTPError as e:
        if e.code == 404:
            # Repository or release not found
            return None
        return None
    except Exception as exc:
        from app.utils import log_or_swallow
        log_or_swallow(None, "update.fetch", exc)
        return None


def _trusted_release_url(url: str, repo: str) -> str:
    """Return `url` only if it is an https GitHub release-asset URL for `repo`.

    MED-012: `browser_download_url` is 100% server-controlled - whoever can
    publish a release can choose this string - and it went straight to
    `webbrowser.open` with no scheme check and no host check. The report is
    right that this needs repo compromise to exploit, but the app IS the
    system-tuning tool, so "Update Available" carries very high trust, and a
    silent downgrade to `http://` is the whole ballgame.

    Two checks, both required:
      * scheme must be exactly https (so no http:// downgrade, and no
        file://, javascript:, data: or ms-msdt: hand-off);
      * host must be a GitHub release host AND the path must belong to the
        repo we asked about - so a redirector or a lookalike host cannot be
        substituted for the release asset.

    Anything else returns "" and the caller treats the release as having no
    download. The check is deliberately at this boundary as well as at the
    webbrowser.open call site, so neither layer alone is load-bearing.
    """
    if not isinstance(url, str) or not url:
        return ""
    try:
        from urllib.parse import urlparse
        parts = urlparse(url)
    except Exception as exc:
        from app.utils import log_or_swallow
        log_or_swallow(None, "update.url_parse", exc)
        return ""
    if parts.scheme != "https":
        return ""
    host = (parts.hostname or "").lower()
    if host not in ("github.com", "www.github.com", "objects.githubusercontent.com"):
        return ""
    # the asset URL must name the repo we actually queried
    owner_repo = "/".join(str(repo or "").strip("/").split("/")[:2]).lower()
    if not owner_repo or "/" not in owner_repo:
        return ""
    if owner_repo not in url.lower():
        return ""
    return url


def check_for_updates(repo: str = "owner/repo", timeout: int = 10) -> dict:
    """Check if a new version of the app is available.
    
    Args:
        repo: GitHub repository in format "owner/repo"
        timeout: Request timeout in seconds
        
    Returns:
        dict with keys:
        - 'update_available': bool
        - 'current_version': str (from config)
        - 'latest_version': str (from tag)
        - 'download_url': str
        - 'release_notes': str
        - 'error': str (if check failed)
    """
    from app.config import APP_VERSION
    
    result = {
        'update_available': False,
        'current_version': APP_VERSION,
        'latest_version': '',
        'download_url': '',
        'release_notes': '',
        'error': None
    }
    
    # Fetch latest release info
    release_info = fetch_latest_release_info(repo, timeout)
    if not release_info:
        result['error'] = "Could not fetch release information from GitHub"
        return result
    
    # Extract version from tag (e.g., "v2.0.0" -> "2.0.0")
    # BUG-002: strip a single leading "v" only (lstrip would eat "vv2")
    tag_name = release_info.get('tag_name', '') or ''
    if tag_name.startswith("v") and len(tag_name) > 1 and tag_name[1].isdigit():
        latest_version = tag_name[1:]
    else:
        latest_version = tag_name
    result['latest_version'] = latest_version
    
    # Get download URL for the exe asset
    assets = release_info.get('assets', [])
    download_url = ''
    for asset in assets:
        if asset.get('name', '') == 'Cleaner-Tool.exe':
            # MED-012: this string is entirely server-controlled. Validate it
            # (https + GitHub host + the repo we asked about) before it becomes
            # the "Update Available" link.
            download_url = _trusted_release_url(
                asset.get('browser_download_url', ''), repo)
            if not download_url:
                result['error'] = (
                    "Release advertised a download link that is not an https "
                    "GitHub asset for this project; refusing to link it.")
            break
    result['download_url'] = download_url
    
    # Get release notes
    result['release_notes'] = release_info.get('body', '')
    
    # Version comparison
    try:
        if latest_version and latest_version != 'latest':
            # B-001: parse the numeric head (a `-beta`/`+build` suffix is not
            # an error) and NAME an unreadable tag instead of reporting "no
            # update", which is what the old int()/except pair did.
            current_parts = parse_version(APP_VERSION)
            latest_parts = parse_version(latest_version)
            if latest_parts is None or current_parts is None:
                result['error'] = (
                    "Could not read the release version %r; check the "
                    "release page." % (latest_version,))
            else:
                for i in range(max(len(current_parts), len(latest_parts))):
                    curr = current_parts[i] if i < len(current_parts) else 0
                    lat = latest_parts[i] if i < len(latest_parts) else 0
                    if lat > curr:
                        result['update_available'] = True
                        break
        elif latest_version == 'latest' and download_url:
            # For rolling "latest" releases, compare SHA256 hashes.
            # REL-002: if the remote body is too large or the download
            # fails, report no update rather than hanging the worker.
            try:
                current_hash = get_current_exe_hash()
                if current_hash:
                    # I-002: the build publishes a .sha256 sibling; reading
                    # that is a few hundred bytes instead of hashing a
                    # multi-hundred-MB exe on every check. Only when the
                    # release has no checksum asset do we stream the binary.
                    sha_url = ""
                    for asset in assets or []:
                        if (isinstance(asset, dict)
                                and asset.get("name") == SHA_ASSET_NAME):
                            sha_url = _trusted_release_url(
                                asset.get("browser_download_url", ""), repo)
                            break
                    remote_hash = (fetch_sha256(sha_url, timeout=timeout)
                                   if sha_url else "")
                    if not remote_hash:
                        remote_hash = get_remote_exe_hash(download_url, timeout)
                    if remote_hash and remote_hash != current_hash:
                        result['update_available'] = True
                    elif not remote_hash:
                        result['error'] = "rolling build — manual check (hash unavailable or file too large)"
            except Exception as exc:
                # If hash comparison fails, assume no update
                from app.utils import log_or_swallow
                log_or_swallow(None, "update.hash_compare", exc)
    except Exception as exc:
        # If version parsing fails, assume no update
        from app.utils import log_or_swallow
        log_or_swallow(None, "update.version_parse", exc)
    
    return result


_HEX64 = re.compile(r"\b([0-9a-fA-F]{64})\b")


def fetch_sha256(url: str, timeout: int = 20) -> str:
    """Read a published `<exe>.sha256` sibling and return its digest, or "".

    The file is a few hundred bytes, so this is the cheap path for the rolling
    `latest` tag. Three layouts are accepted, because it may be produced by
    hand or by a different tool: a bare digest, `digest  filename` (what
    `sha256sum` writes) and `SHA256 (file) = digest` (certutil).
    """
    if not url:
        return ""
    try:
        req = urllib.request.Request(
            url, headers={"User-Agent": "CleanerTool/2.0"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read(4096).decode("utf-8", "replace")
    except Exception as exc:
        from app.utils import log_or_swallow
        log_or_swallow(None, "update.fetch_sha256", exc)
        return ""
    m = _HEX64.search(body)
    return m.group(1).lower() if m else ""


def get_remote_exe_hash(url: str, timeout: int = 30) -> str:
    """Download and calculate SHA256 hash of remote exe.

    REL-002: refuse to stream more than 150 MB (Content-Length or
    cumulative) so a rolling-latest check cannot burn bandwidth or hang
    the worker on a multi-hundred-MB binary. Prefer publishing a .sha256
    asset long-term; until then this is a hard safety cap.
    """
    _MAX_BYTES = 150 * 1024 * 1024
    try:
        sha256 = hashlib.sha256()
        req = urllib.request.Request(
            url,
            headers={"User-Agent": "CleanerTool/2.0"}
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            cl = resp.headers.get("Content-Length")
            if cl is not None:
                try:
                    if int(cl) > _MAX_BYTES:
                        return ""
                except (TypeError, ValueError):
                    pass
            total = 0
            for chunk in iter(lambda: resp.read(8192), b''):
                total += len(chunk)
                if total > _MAX_BYTES:
                    return ""
                sha256.update(chunk)
        return sha256.hexdigest()
    except Exception as exc:
        from app.utils import log_or_swallow
        log_or_swallow(None, "update.remote_hash", exc)
        return ""


# Simple test function
if __name__ == "__main__":
    print("Current exe path:", get_current_exe_path())
    print("Current exe hash:", get_current_exe_hash())
    
    # Test with actual repo
    result = check_for_updates("MyLittlePrimordia/Cleaner-Tool")
    print("Update check result:", result)
    print("Update available:", result.get('update_available'))
    print("Current version:", result.get('current_version'))
    print("Latest version:", result.get('latest_version'))
    print("Download URL:", result.get('download_url'))
    print("Error:", result.get('error'))