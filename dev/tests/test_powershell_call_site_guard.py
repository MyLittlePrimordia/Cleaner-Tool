"""The one-call-site guard, tested as a guard: clean now, and loud when not.

`dev/tools/verify_powershell_single_call_site.py` runs as its own CI step AND here,
so a drifted tree fails both the static-check stage and the regression suite.
The negative cases scan a synthetic package, because a guard nobody has ever
seen fire is a guard you cannot trust.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

_TOOL = Path(__file__).resolve().parent.parent / "tools" / "verify_powershell_single_call_site.py"


def _load_tool():
    spec = importlib.util.spec_from_file_location("verify_ps_call_site", _TOOL)
    module = importlib.util.module_from_spec(spec)
    # Registered before executing, not just returned: `@dataclass` resolves a
    # field's annotations through `sys.modules[cls.__module__]`, and a module
    # that is only exec'd has no entry there. On Python 3.13 that is an
    # AttributeError inside dataclasses; the suite passed locally on 3.15,
    # where the lookup is guarded, and failed in CI on 3.13.
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        sys.modules.pop(spec.name, None)
        raise
    return module


@pytest.fixture(scope="module")
def tool():
    return _load_tool()


def _package(tmp_path: Path, name: str, source: str) -> Path:
    root = tmp_path / "pkg"
    root.mkdir(exist_ok=True)
    (root / name).write_text(source, encoding="utf-8")
    return root


# --------------------------------------------------------------------------- #
# The real tree
# --------------------------------------------------------------------------- #

def test_the_real_tree_has_one_call_site(tool):
    assert tool.scan() == []


def test_main_exits_zero_on_a_clean_tree(tool, capsys):
    assert tool.main([]) == 0
    assert "OK" in capsys.readouterr().out


# --------------------------------------------------------------------------- #
# The guard actually fires
# --------------------------------------------------------------------------- #

def test_a_new_hand_built_argv_is_caught(tool, tmp_path):
    root = _package(tmp_path, "fresh.py", (
        "import subprocess\n"
        "def go(script):\n"
        "    return subprocess.run(\n"
        "        [\"powershell\", \"-NoProfile\", \"-Command\", script],\n"
        "        capture_output=True)\n"
    ))
    findings = tool.scan(root)
    assert len(findings) == 1
    assert "argv word" in findings[0].why


def test_a_pasted_shell_command_is_caught(tool, tmp_path):
    root = _package(tmp_path, "pasted.py", (
        "def go():\n"
        "    return 'powershell -NoProfile -Command \"Get-Date\"'\n"
    ))
    findings = tool.scan(root)
    assert len(findings) == 1
    assert "pasted" in findings[0].why


def test_a_bare_exe_used_as_a_launch_target_is_caught(tool, tmp_path):
    """The elevation bug the guard was written after: a bare exe name handed
    to ShellExecuteW, i.e. an elevated launch resolved through PATH."""
    root = _package(tmp_path, "launch.py", (
        "import ctypes\n"
        "def go(params):\n"
        "    return ctypes.windll.shell32.ShellExecuteW(\n"
        "        None, 'runas', 'powershell.exe', params, None, 0)\n"
    ))
    assert len(tool.scan(root)) == 1


# --------------------------------------------------------------------------- #
# What must NOT be flagged
# --------------------------------------------------------------------------- #

def test_the_resolver_is_the_sanctioned_api(tool, tmp_path):
    root = _package(tmp_path, "target.py", (
        "def go(params):\n"
        "    from app.exe_paths import resolve_exe\n"
        "    return launch(resolve_exe('powershell'), params)\n"
    ))
    assert tool.scan(root) == []


def test_user_facing_copy_is_not_a_call_site(tool, tmp_path):
    root = _package(tmp_path, "messages.py", (
        "def go():\n"
        "    return 'PowerShell is not available.'\n"
    ))
    assert tool.scan(root) == []


def test_a_docstring_may_show_the_old_broken_form(tool, tmp_path):
    root = _package(tmp_path, "documented.py", (
        "def go():\n"
        "    \"\"\"This used to be a shell string:\n"
        "\n"
        "        'powershell -NoProfile -Command \"%s\"' % name\n"
        "\n"
        "    which is why the argv form exists.\n"
        "    \"\"\"\n"
        "    return 1\n"
    ))
    assert tool.scan(root) == []


def test_main_fails_the_build_on_drift(tool, tmp_path, monkeypatch, capsys):
    root = _package(tmp_path, "fresh.py",
                    "ARGV = ['powershell', '-Command', 'Get-Date']\n")
    monkeypatch.setattr(tool, "APP", root)
    assert tool.main([]) == 1
    assert "FAIL" in capsys.readouterr().out
