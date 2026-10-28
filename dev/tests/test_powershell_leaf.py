"""R-008 (last slice): the argv builder is a leaf, so utils can use it too.

`app/utils.py` owns the restore-point call, and `ps.py` imports utils - so the
builder had to move below both of them before that last literal could go. These
tests pin the layering (a leaf really is a leaf), the encoded form the update
scanner needs, and that both callers now hand over the runner's argv.
"""
from __future__ import annotations

import base64
import inspect
import sys

import pytest

#: Modules that must not import back up into the app's heavier layers. If one
#: of these ever imports app.utils, the cycle this slice removed is back.
LEAVES = ["app.exe_paths", "app.powershell"]


class _Ctx:
    def __init__(self):
        self.status = []
        self.lines = []

    def set_status(self, text):
        self.status.append(text)

    def log(self, text):
        self.lines.append(text)


@pytest.mark.parametrize("name", LEAVES)
def test_the_runner_is_a_leaf(name):
    import ast

    from app import exe_paths, powershell  # noqa: F401  (import order proof)

    # Parsed, not string-matched: these modules TALK about app.utils in their
    # docstrings (explaining why they must not import it).
    tree = ast.parse(inspect.getsource(sys.modules[name]))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)

    assert not any(m == "app.utils" or m.startswith("app.utils.")
                   for m in imported), imported
    assert not any(m.startswith("app.tasks") for m in imported), imported


def test_resolve_exe_is_one_function_and_still_re_exported():
    import app.exe_paths as paths
    import app.utils as utils

    assert utils.resolve_exe is paths.resolve_exe
    # Cached per process, so two calls cannot disagree mid-session.
    assert paths.resolve_exe("powershell") == paths.resolve_exe("powershell")


# --------------------------------------------------------------------------- #
# The encoded form (Windows Update scans)
# --------------------------------------------------------------------------- #

def test_encoded_argv_carries_base64_utf16le_and_no_script():
    from app.powershell import ps_argv

    script = "Get-WindowsUpdate -Json '{\"a\": 1}'"
    argv = ps_argv(script, non_interactive=True, encoded=True)

    assert "-EncodedCommand" in argv
    assert "-Command" not in argv
    assert script not in argv                     # never on the command line
    payload = argv[argv.index("-EncodedCommand") + 1]
    assert base64.b64decode(payload).decode("utf-16-le") == script
    assert argv.index("-NonInteractive") < argv.index("-EncodedCommand")


def test_the_update_scanner_uses_the_runner():
    import app.updates as updates
    from app.powershell import ps_argv

    script = "Write-Output 'scan'"
    assert updates.powershell_argv(script) == ps_argv(script, non_interactive=True,
                                                      encoded=True)
    # The helper it used to define lives in the leaf now, still reachable here.
    assert updates.encode_command("x") == base64.b64encode(
        "x".encode("utf-16-le")).decode("ascii")


# --------------------------------------------------------------------------- #
# The restore-point call that forced the split
# --------------------------------------------------------------------------- #

def test_create_restore_point_hands_over_the_runner_argv(monkeypatch):
    import app.utils as utils

    seen = {}

    def fake_run_cmd(ctx, command, shell=False, timeout=None, collect=None):
        seen.update(command=command, shell=shell, timeout=timeout)
        if collect is not None:
            collect.append("RESTORE_OK")
        return 0

    monkeypatch.setattr(utils, "run_cmd", fake_run_cmd)

    assert utils.create_restore_point(_Ctx(), "before changes") is True
    assert seen["command"][-2] == "-Command"
    assert seen["command"][-1] == utils.restore_point_create_ps("before changes")
    assert "-NoProfile" in seen["command"]
    assert seen["shell"] is False and seen["timeout"] == 120


def test_the_store_probe_is_non_interactive(monkeypatch):
    """A probe that decides whether Windows may pop a dialog must never wait
    for input it cannot get - and must never show one either."""
    import subprocess

    import app.utils as utils

    seen = {}

    def fake_check_output(argv, **kwargs):
        seen["argv"] = list(argv)
        return "STORE_YES\n"

    monkeypatch.setattr(subprocess, "check_output", fake_check_output)

    assert utils.store_available() is True
    assert "-NonInteractive" in seen["argv"]
    assert seen["argv"][-1].startswith(
        "if (Get-AppxPackage -Name Microsoft.WindowsStore")
