"""Phase 3/4 regression tests.

  * 3.1 — installed-app detection from `winget export` JSON (+ text fallback)
  * 3.3 — the registry-write error taxonomy (no more blanket "Admin required")
  * 4.2 — the SEC-001 allowlist drift guard itself catches drift

No registry or winget side effects: the winget calls are monkeypatched and the
only real file touched is the real `winget export` temp target, which the code
cleans up itself.
"""
from __future__ import annotations

import ast
import importlib.util
import os

import pytest

from app.utils import TaskContext


def _ctx() -> TaskContext:
    return TaskContext(log=lambda *_a: None, set_status=lambda *_a: None,
                       cancelled=lambda: False)


# --------------------------------------------------------------------------- #
# 3.1 — installed ids from `winget export` JSON
# --------------------------------------------------------------------------- #

def test_installed_ids_from_export_parses_json(monkeypatch):
    import app.tasks.winget as it

    def fake_run_winget(args, timeout=120):
        assert args[0] == "export"
        path = args[args.index("-o") + 1]
        with open(path, "w", encoding="utf-8") as fh:
            fh.write('{"Sources":[{"Packages":['
                     '{"PackageIdentifier":"Git.Git"},'
                     '{"PackageIdentifier":"7zip.7zip"}]}]}')
        return ""

    monkeypatch.setattr(it, "_run_winget", fake_run_winget)
    assert it._installed_ids_from_export() == {"git.git", "7zip.7zip"}


def test_get_installed_ids_prefers_export_and_keeps_store_hints(monkeypatch):
    import app.tasks.winget as it

    def fake_run_winget(args, timeout=120):
        if args[0] == "export":
            path = args[args.index("-o") + 1]
            with open(path, "w", encoding="utf-8") as fh:
                fh.write('{"Sources":[{"Packages":[{"PackageIdentifier":"Git.Git"}]}]}')
            return ""
        # `winget list` text — only consulted here for the Store name hints
        return "WhatsApp  MSIX\\5319275A.WhatsAppDesktop_2.0_x64__abc  2.0\n"

    monkeypatch.setattr(it, "_run_winget", fake_run_winget)
    monkeypatch.setattr(it, "_installed_package_names", lambda: None)
    it._INSTALLED_CACHE[0] = None

    ids = it.get_installed_ids(refresh=True)
    assert "git.git" in ids
    assert "9nksqgp7f2nh" in ids, "Store app must still be badged via the name hint"


def test_get_installed_ids_falls_back_to_text(monkeypatch):
    import app.tasks.winget as it
    from app.app_catalog import APP_CATALOG

    sample = next(a["id"] for a in APP_CATALOG
                  if a.get("id") and not a["id"].startswith("manual:"))

    def fake_run_winget(args, timeout=120):
        if args[0] == "export":
            return None  # export unavailable (older winget) -> text fallback
        return f"Something  {sample}  1.0\n"

    monkeypatch.setattr(it, "_run_winget", fake_run_winget)
    monkeypatch.setattr(it, "_installed_package_names", lambda: None)
    it._INSTALLED_CACHE[0] = None

    ids = it.get_installed_ids(refresh=True)
    assert sample.lower() in ids


def test_get_installed_ids_unknown_when_both_fail(monkeypatch):
    import app.tasks.winget as it

    monkeypatch.setattr(it, "_run_winget", lambda args, timeout=120: None)
    monkeypatch.setattr(it, "_installed_package_names", lambda: None)
    it._INSTALLED_CACHE[0] = None
    assert it.get_installed_ids(refresh=True) is None


# --------------------------------------------------------------------------- #
# 1.3 — Store-app detection via Get-AppxPackage package identity
# --------------------------------------------------------------------------- #

def test_installed_package_names_parses_output(monkeypatch):
    import subprocess
    import app.tasks.winget as it

    class _R:
        returncode = 0
        stdout = ("Microsoft.WindowsNotepad\n"
                  "5319275A.WhatsAppDesktop\n\n")

    monkeypatch.setattr(subprocess, "run", lambda *a, **k: _R())
    monkeypatch.setattr(it, "resolve_exe", lambda name: name)
    assert it._installed_package_names() == {
        "microsoft.windowsnotepad", "5319275a.whatsappdesktop"}


def test_installed_package_names_none_on_failure(monkeypatch):
    import subprocess
    import app.tasks.winget as it

    def boom(*a, **k):
        raise OSError("no powershell on PATH")

    monkeypatch.setattr(subprocess, "run", boom)
    monkeypatch.setattr(it, "resolve_exe", lambda name: name)
    assert it._installed_package_names() is None


def test_msstore_identity_map_matches_catalog():
    """Every key is a real catalog Store id, every value looks like a package
    identity, and the one id with no MSIX identity (NVIDIA App) is absent so the
    name hint stays its fallback."""
    from app.app_catalog import MSSTORE_IDS
    import app.tasks.install_tasks as it

    assert set(it._MSSTORE_IDENTITY) <= set(MSSTORE_IDS)
    for names in it._MSSTORE_IDENTITY.values():
        assert names
        for n in names:
            assert "." in n and n == n.lower()
    assert "XP8CLZL93F5Z4P" not in it._MSSTORE_IDENTITY


def test_get_installed_ids_badges_store_app_exactly(monkeypatch):
    """1.3: a store app with NO name hint is still badged, because its MSIX
    package identity is installed (Windows Notepad has no entry in
    _MSSTORE_NAME_HINTS)."""
    import app.tasks.winget as it

    def fake_run_winget(args, timeout=120):
        return None if args[0] == "export" else "Something Else  1.0\n"

    monkeypatch.setattr(it, "_run_winget", fake_run_winget)
    monkeypatch.setattr(it, "_installed_package_names",
                        lambda: {"microsoft.windowsnotepad"})
    it._INSTALLED_CACHE[0] = None

    ids = it.get_installed_ids(refresh=True)
    assert "9msmlrh6lzf3" in ids, "Notepad Store id must badge via package identity"
    assert "9nksqgp7f2nh" not in ids, "WhatsApp identity not installed -> no badge"


# --------------------------------------------------------------------------- #
# 3.3 — registry error taxonomy
# --------------------------------------------------------------------------- #

def test_describe_reg_write_error_is_specific():
    from app.utils import _describe_reg_write_error

    assert "unknown registry value type" in _describe_reg_write_error(
        KeyError(None)).lower()
    assert "access denied" in _describe_reg_write_error(PermissionError()).lower()
    # a generic error keeps its own message, not a permissions excuse
    assert "ValueError: boom" in _describe_reg_write_error(ValueError("boom"))


def test_reg_set_value_checked_reports_specific_reason(monkeypatch):
    import app.utils as u

    monkeypatch.setattr(
        u, "_reg_set_value_ex",
        lambda *a, **k: (False, "access denied — run as Administrator"))

    with pytest.raises(RuntimeError) as ei:
        u.reg_set_value_checked(_ctx(), "HKCU", "Software\\X", "Y", 1)

    msg = str(ei.value)
    assert "access denied" in msg
    assert "Administrator rights may be required" not in msg


# --------------------------------------------------------------------------- #
# 4.2 — the SEC-001 drift guard
# --------------------------------------------------------------------------- #

def _load_verify():
    root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    path = os.path.join(root, "dev", "tools", "verify_sec001_task_allowlist.py")
    spec = importlib.util.spec_from_file_location("verify_sec001", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_literal_specs_parses_and_rejects_dynamic():
    mod = _load_verify()
    node = ast.parse("[('HKCU','P','N'), ('HKLM','Q','M',5)]").body[0].value
    assert mod._literal_specs(node) == [("HKCU", "P", "N"), ("HKLM", "Q", "M")]
    assert mod._literal_specs(ast.parse("some_var").body[0].value) is None


def test_literal_specs_resolves_module_constants():
    mod = _load_verify()
    src = ("PATH = 'Software\\\\X'\n"
           "SPECS = [('HKCU', PATH, 'V'), ('HKLM', PATH + '\\\\Y', 'W')]\n")
    tree = ast.parse(src)
    consts, nodes = mod._const_index(tree)
    spec_node = tree.body[1].value  # the SPECS list literal
    assert mod._literal_specs(spec_node, consts, nodes) == [
        ("HKCU", "Software\\X", "V"),
        ("HKLM", "Software\\X\\Y", "W"),
    ]


def test_literal_specs_resolves_percent_formatting():
    mod = _load_verify()
    src = ("CLSID = '{1111}'\n"
           "SPECS = [('HKCU', 'Software\\\\Classes\\\\CLSID\\\\%s\\\\InprocServer32'"
           " % CLSID, '')]\n")
    tree = ast.parse(src)
    consts, nodes = mod._const_index(tree)
    assert mod._literal_specs(tree.body[1].value, consts, nodes) == [
        ("HKCU", "Software\\Classes\\CLSID\\{1111}\\InprocServer32", "")]


def test_literal_specs_resolves_subscript_and_comprehension():
    mod = _load_verify()
    src = ("SENSE = ('HKCU', 'Software\\\\StoragePolicy')\n"
           "SPECS = [(SENSE[0], SENSE[1], n) for n in ('01', '04')]\n")
    tree = ast.parse(src)
    consts, nodes = mod._const_index(tree)
    assert mod._literal_specs(tree.body[1].value, consts, nodes) == [
        ("HKCU", "Software\\StoragePolicy", "01"),
        ("HKCU", "Software\\StoragePolicy", "04"),
    ]


def test_literal_specs_resolves_comprehension_over_constants():
    mod = _load_verify()
    src = ("FLAGS = (('HKCU', 'A', 'Flags'), ('HKCU', 'B', 'Flags'))\n"
           "SPECS = [w + ('REG_SZ',) for w in FLAGS]\n")
    tree = ast.parse(src)
    consts, nodes = mod._const_index(tree)
    assert mod._literal_specs(tree.body[1].value, consts, nodes) == [
        ("HKCU", "A", "Flags"), ("HKCU", "B", "Flags"),
    ]


def test_literal_specs_resolves_list_concatenation():
    mod = _load_verify()
    src = ("NAMES = ('a', 'b')\n"
           "BASE = 'P'\n"
           "SPECS = [('HKCU', BASE, n) for n in NAMES] + [('HKLM', BASE, 'z')]\n")
    tree = ast.parse(src)
    consts, nodes = mod._const_index(tree)
    assert mod._literal_specs(tree.body[2].value, consts, nodes) == [
        ("HKCU", "P", "a"), ("HKCU", "P", "b"), ("HKLM", "P", "z"),
    ]


def test_guard_scans_advanced_tasks():
    """4.1: the guard also derives advanced_tasks.py (via `_apply_registry`), so
    the advanced tab's allowlist entries are drift-checked too."""
    found = _load_verify()._scan_source()
    assert found["adv_copilot"] == {
        ("HKCU", r"Software\Policies\Microsoft\Windows\WindowsCopilot",
         "TurnOffWindowsCopilot")}
    assert ("HKLM", r"SOFTWARE\Policies\Microsoft\Windows\WindowsAI",
            "AllowRecallEnablement") in found["adv_disable_ai"]


def test_allowlist_guard_passes_on_current_tree():
    assert _load_verify().main() == 0


def test_allowlist_guard_detects_drift(monkeypatch):
    mod = _load_verify()

    # Pretend shader_cache_10gb writes a value its allowlist entry does not name.
    monkeypatch.setattr(
        mod, "_scan_source",
        lambda: {"shader_cache_10gb": {
            ("HKCU", "Software\\NVIDIA Corporation\\Global\\FTS", "Surprise")}})
    assert mod.main() == 1


# --------------------------------------------------------------------------- #
# 3.4 — the shared headless TaskContext (run_auto_clean / run_auto_update)
# --------------------------------------------------------------------------- #

def test_print_context_is_a_task_context(capsys):
    from app.runner.task_runner import print_context

    ctx = print_context("[X]", "[X STATUS]")
    assert isinstance(ctx, TaskContext)
    ctx.log("hello")
    ctx.set_status("working")
    assert ctx.cancelled() is False
    out = capsys.readouterr().out
    assert "[X] hello" in out
    assert "[X STATUS] working" in out


def test_scheduler_uses_shared_context():
    """The two headless entry points must use the runner's shared factory —
    a regression here means the old duplicated AutoCtx crept back."""
    import app.scheduler as sched
    from app.runner.task_runner import print_context

    assert sched.print_context is print_context
    src = open(sched.__file__, "r", encoding="utf-8").read()
    assert "class AutoCtx" not in src
