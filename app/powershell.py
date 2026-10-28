"""The ONE place a PowerShell argv is built (audit 2026-10, R-008).

Repair, Clean, Tweak, the tools and the probes all drive Windows through
PowerShell, and every one of them used to spell the exe and the flags itself.
The copies had drifted: most said `"powershell"`, which is not resolvable on a
machine where PowerShell is not on PATH even though `resolve_exe` exists for
exactly that; and a few pasted the script into a shell string, which is where
the nested-quote fragility came from.

    ps_argv("Get-Date")                       # -NoProfile -Command <script>
    ps_argv(script, non_interactive=True)     # + -NonInteractive, for dialogs
    ps_argv(script, encoded=True)             # -EncodedCommand <base64>

`encoded=True` is the base64(UTF-16LE) form, used where the script is long and
contains JSON/quotes that are not worth escaping (the Windows Update scan): the
script never appears in the command line at all.

This module is a LEAF - its only app import is `app.exe_paths`, which is itself
a leaf - so `app/utils.py` can import it for `create_restore_point` without a
cycle. `app/tasks/ps.py` re-exports `ps_argv` and adds the run helpers, which
need a TaskContext; this half is usable from anywhere.
"""
from __future__ import annotations

import base64

from app.exe_paths import resolve_exe


def encode_command(script: str) -> str:
    """base64(UTF-16LE) for powershell -EncodedCommand."""
    return base64.b64encode(script.encode("utf-16-le")).decode("ascii")


def ps_argv(script: str, *, non_interactive: bool = False,
            encoded: bool = False) -> "list[str]":
    """The argv for running `script` as a single PowerShell command.

    No profile (a user profile that prints a banner would corrupt captured
    output), and the exe resolved the same way everywhere else in the app.
    """
    argv = [resolve_exe("powershell"), "-NoProfile"]
    if non_interactive:
        argv.append("-NonInteractive")
    if encoded:
        argv += ["-EncodedCommand", encode_command(script)]
    else:
        argv += ["-Command", script]
    return argv
