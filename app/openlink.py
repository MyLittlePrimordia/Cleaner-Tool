"""Open a web link so the browser actually comes forward.

Why this is its own module (U-001): the Install tab's failure path and the new
"Install failed" dialog both need one behaviour — hand a URL to the user's
browser and make sure it is *visible*. The logic was written for the Install
tab's mirror links after a user report ("opens the link but doesn't bring up
the browser in view"), and duplicating it in a dialog would guarantee the two
drift: one copy would keep the foreground fix and the other would regress to a
bare `os.startfile` the first time someone tidied it.

Three routes, because each one fails somewhere real (LTSC images with no
browser registered are the reported case):

  1. launch the default browser's exe with the URL as argv - raises the
     window even when the browser is already running, which ShellExecute
     does not (focus-stealing prevention leaves it behind the app);
  2. `os.startfile` / ShellExecute - the normal shell route;
  3. `cmd /c start` - last resort.

Every route is guarded and a failure is REPORTED through the caller's
`show_error` callback; the original bug was a link that silently did nothing.

Only http(s) URLs are ever launched: the metadata they come from is yours
(the app catalog) but every route ends in cmd.exe or ShellExecute, so a
`file:`/`javascript:`/custom-scheme typo must be refused here.
"""
from __future__ import annotations

import os
import shlex
import subprocess

from app.utils import is_web_url


def _resolve_default_browser_cmd() -> "list[str] | None":
    """Resolve the default https browser's launch command from the registry
    (UserChoice ProgId -> shell\\open\\command), or None.

    Registry reality (verified on a live machine): the ProgId command template
    is registered under HKEY_CLASSES_ROOT (the merged HKCU+HKLM view) — e.g.
    FirefoxURL-<hash>\\shell\\open\\command — and often NOT under
    HKCU\\Software\\Classes. Check both, HKCR first as the authoritative
    merged view.
    """
    try:
        import winreg
        with winreg.OpenKey(
                winreg.HKEY_CURRENT_USER,
                r"Software\Microsoft\Windows\Shell\Associations"
                r"\UrlAssociations\https\UserChoice") as k:
            prog_id, _ = winreg.QueryValueEx(k, "ProgId")
        cmd = None
        for root, prefix in ((winreg.HKEY_CLASSES_ROOT, prog_id),
                             (winreg.HKEY_CURRENT_USER,
                              rf"Software\Classes\{prog_id}")):
            try:
                with winreg.OpenKey(root,
                                    rf"{prefix}\shell\open\command") as k:
                    cmd, _ = winreg.QueryValueEx(k, "")
                    break
            except OSError:
                continue
        if not cmd:
            return None
        # template contains %1 (or empty) — strip it; shlex splits the
        # quoted exe path correctly
        cmd = cmd.replace("%1", "").replace("%*", "").strip()
        parts = shlex.split(cmd, posix=False)
        parts = [p.strip('"') for p in parts if p.strip('"')]
        return parts or None
    except Exception:
        return None


def open_web_url(url: str, show_error=None) -> bool:
    """Open `url` in the browser. Returns True when a route was started.

    `show_error(message)` is called with a plain sentence when every route
    failed (and for a refused non-web URL), so the caller can put it in front
    of the user however it likes. Never raises.
    """
    def _fail(message: str) -> bool:
        if show_error is not None:
            try:
                show_error(message)
            except Exception:
                pass
        return False

    url = str(url or "").strip()
    if not is_web_url(url):
        return _fail("That link is not a web address:\n%s" % url)

    # Route 1: direct browser launch (raises the window if already running)
    browser_cmd = _resolve_default_browser_cmd()
    if browser_cmd:
        try:
            subprocess.Popen(
                [*browser_cmd, url],
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            return True
        except Exception:
            pass  # fall through to the shell routes

    # Route 2: shell default handler
    try:
        os.startfile(url)
        return True
    except Exception:
        pass

    # Route 3: explicit shell open
    err = None
    try:
        subprocess.Popen(["cmd", "/c", "start", "", url], shell=False,
                         creationflags=getattr(subprocess,
                                               "CREATE_NO_WINDOW", 0))
        return True
    except Exception as exc:
        err = exc
    return _fail("%s\n\nWindows returned:\n%s" % (url, err))
