"""One plain sentence for a failed tool job.

AUDIT TV-010 (2026-10). Every popup invented its own failure line: one said
`Access is denied`, another printed the raw `[WinError 5]`, a third printed
`Something went wrong: [Errno 13] Permission denied: 'C:\\...'`. The same
situation therefore read as three different problems, and the least useful
version - a Python exception repr - was the most common.

`explain()` maps the handful of failures a Windows utility actually meets onto
sentences that say what happened and what the user can do. It is deliberately
small: anything it does not recognise keeps its own message, because inventing
a friendly cause for an unknown error would be worse than the raw text.

No Qt/Tk imports: this is copy, usable from workers and tests alike.
"""
from __future__ import annotations

#: Longest unknown message kept. A dialog's status line is one line; a
#: multi-kilobyte exception string would push the actual cause off-screen.
_TEXT_MAX = 160

_ACCESS_HINTS = ("access is denied", "permission denied", "access denied",
                 "requires elevation", "elevation required")
_LOCK_HINTS = ("being used by another process", "file is in use",
               "cannot access the file", "sharing violation")
_TIMEOUT_HINTS = ("timed out", "timeout", "took too long")
_DISK_HINTS = ("disk full", "not enough space", "no space left")
_NET_HINTS = ("no such host", "actively refused", "network is unreachable",
              "connection refused", "name or service not known")


def explain(exc) -> str:
    """A user-facing sentence for `exc`, ending in a full stop."""
    if exc is None:
        return "Something went wrong: unknown error."
    name = type(exc).__name__
    text = str(exc) or ""
    low = text.lower()
    winerror = getattr(exc, "winerror", None)
    errno = getattr(exc, "errno", None)

    if (name in ("PermissionError", "AccessDeniedError")
            or winerror == 5 or errno == 13
            or any(h in low for h in _ACCESS_HINTS)):
        return "Windows refused access \u2014 this needs administrator rights."
    if (name in ("FileNotFoundError", "NotADirectoryError")
            or winerror in (2, 3)):
        return "That file or folder is no longer there."
    if winerror in (32, 33) or any(h in low for h in _LOCK_HINTS):
        return "Another program is using that file."
    if (name == "TimeoutError" or winerror == 1460
            or any(h in low for h in _TIMEOUT_HINTS)):
        return "Windows took too long to answer. Try again."
    if (name == "OSError" and errno == 28) or winerror == 112 \
            or any(h in low for h in _DISK_HINTS):
        return "There is not enough free disk space."
    if name in ("ConnectionError", "ConnectionRefusedError",
                "ConnectionResetError") \
            or any(h in low for h in _NET_HINTS):
        return "The network did not answer. Check the connection."
    clean = " ".join(text.split())
    if clean:
        if len(clean) > _TEXT_MAX:
            clean = clean[:_TEXT_MAX - 1].rstrip() + "\u2026"
        clean = clean.rstrip(".")
        return "Something went wrong: %s." % clean
    return "Something went wrong: %s." % (name or "unknown error")
