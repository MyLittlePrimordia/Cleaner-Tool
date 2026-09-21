"""
Update checker - detects new Cleaner Tool releases from GitHub.

Uses GitHub API to fetch the latest release information and compares
SHA256 hashes to determine if an update is available. No third-party
dependencies - uses stdlib urllib.request only.
"""

import hashlib
import json
import os
import sys
import urllib.request
import urllib.error


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
    except Exception:
        return ""


def fetch_latest_release_info(repo: str = "owner/repo", timeout: int = 10) -> dict:
    """Fetch latest release info from GitHub API.
    
    Args:
        repo: GitHub repository in format "owner/repo"
        timeout: Request timeout in seconds
        
    Returns:
        dict with release info or None on failure
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
                # Find the latest release that's not the rolling "latest" tag
                for release in releases:
                    tag_name = release.get('tag_name', '')
                    # Skip the rolling "latest" release, look for versioned tags
                    if tag_name and tag_name != 'latest' and tag_name.startswith('v'):
                        return release
                # If no versioned release found, fall back to the rolling "latest"
                for release in releases:
                    tag_name = release.get('tag_name', '')
                    if tag_name == 'latest':
                        return release
                # If no releases at all, return None
                return None
            return None
    except urllib.error.HTTPError as e:
        if e.code == 404:
            # Repository or release not found
            return None
        return None
    except Exception:
        return None


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
    tag_name = release_info.get('tag_name', '')
    latest_version = tag_name.lstrip('v') if tag_name else ''
    result['latest_version'] = latest_version
    
    # Get download URL for the exe asset
    assets = release_info.get('assets', [])
    download_url = ''
    for asset in assets:
        if asset.get('name', '') == 'Cleaner-Tool.exe':
            download_url = asset.get('browser_download_url', '')
            break
    result['download_url'] = download_url
    
    # Get release notes
    result['release_notes'] = release_info.get('body', '')
    
    # Version comparison
    try:
        if latest_version and latest_version != 'latest':
            # Compare version numbers for versioned releases
            current_parts = [int(x) for x in APP_VERSION.split('.')]
            latest_parts = [int(x) for x in latest_version.split('.')]
            
            for i in range(max(len(current_parts), len(latest_parts))):
                curr = current_parts[i] if i < len(current_parts) else 0
                lat = latest_parts[i] if i < len(latest_parts) else 0
                if lat > curr:
                    result['update_available'] = True
                    break
        elif latest_version == 'latest' and download_url:
            # For rolling "latest" releases, compare SHA256 hashes
            # Download the remote exe and compare with current exe
            try:
                current_hash = get_current_exe_hash()
                if current_hash:
                    remote_hash = get_remote_exe_hash(download_url, timeout)
                    if remote_hash and remote_hash != current_hash:
                        result['update_available'] = True
            except Exception:
                # If hash comparison fails, assume no update
                pass
    except Exception:
        # If version parsing fails, assume no update
        pass
    
    return result


def get_remote_exe_hash(url: str, timeout: int = 30) -> str:
    """Download and calculate SHA256 hash of remote exe.
    
    Args:
        url: URL to the exe file
        timeout: Download timeout in seconds
        
    Returns:
        SHA256 hash string or empty string on failure
    """
    try:
        sha256 = hashlib.sha256()
        req = urllib.request.Request(
            url,
            headers={"User-Agent": "CleanerTool/2.0"}
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            # Download in chunks to handle large files
            for chunk in iter(lambda: resp.read(8192), b''):
                sha256.update(chunk)
        return sha256.hexdigest()
    except Exception:
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