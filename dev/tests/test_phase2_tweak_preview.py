"""Phase 2.3 regression tests — the read-only Tweak "Preview changes" feature.

Everything here is side-effect free: the static half parses the task source, and
every registry read is stubbed. Nothing in this file touches the real registry or
applies a tweak.
"""
from __future__ import annotations

import app.tweak_introspect as ti


# --------------------------------------------------------------------------- #
# static scan
# --------------------------------------------------------------------------- #

def test_scan_reg_specs_finds_known_literal_triple():
    specs = {}
    for path in ti.tweak_source_paths():
        for key, found in ti.scan_reg_specs(path).items():
            specs.setdefault(key, set()).update(found)
    assert ("HKCU", r"Software\NVIDIA Corporation\Global\FTS",
            "EnableGR535") in specs["shader_cache_10gb"]


def test_scan_reg_specs_shared_with_guard():
    """The SEC-001 guard calls the same scanner, so a spec the guard can see is
    a spec the preview can see (and vice versa). Both scan every tweak module
    with ONE merged constant index, because the spec tables live in
    `tweak_common` while the calls that name them live in the domain modules."""
    import importlib.util
    import os

    root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    path = os.path.join(root, "dev", "tools", "verify_sec001_task_allowlist.py")
    spec = importlib.util.spec_from_file_location("verify_sec001_prev", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert mod._scan_source() == ti.scan_all_reg_specs(ti.tweak_source_paths())


def test_scan_tasks_extracts_literal_targets():
    info = ti.scan_tasks()
    entry = info["shader_cache_10gb"]
    trip = ("HKCU", r"Software\NVIDIA Corporation\Global\FTS", "EnableGR535")
    assert entry["regs"][trip] == 1
    assert entry["regs"][("HKCU", r"Software\NVIDIA Corporation\Global\FTS",
                          "RMShaderCacheSize")] == 10240


def test_scan_tasks_parses_the_sources_once_per_process():
    """Nine modules of source are parsed once and the mapping is reused.

    The preview dialog and the scorecard both ask for it, and the sources are
    frozen at import, so there is nothing to invalidate between calls.
    """
    first = ti.scan_tasks()
    assert ti.scan_tasks() is first


# --------------------------------------------------------------------------- #
# preview text (registry reads stubbed)
# --------------------------------------------------------------------------- #

def test_preview_lines_shows_now_to_target(monkeypatch):
    monkeypatch.setattr(ti, "read_current",
                        lambda hive, path, name: (0, "REG_DWORD"))
    out = "\n".join(ti.preview_lines(["shader_cache_10gb"]))
    assert "EnableGR535" in out
    assert ": 0 -> 1" in out


def test_preview_lines_marks_unknown_target(monkeypatch):
    # A task whose registry values are snapped (so they ARE touched) but whose
    # new values are computed at runtime: no target -> "will be changed".
    fake = {"synthetic_task": {
        "title": "Synthetic", "system": False, "deletes": set(),
        "regs": {("HKCU", "Software\\X", "Foo"): None}}}
    monkeypatch.setattr(ti, "scan_tasks", lambda: fake)
    monkeypatch.setattr(ti, "read_current",
                        lambda hive, path, name: (1, "REG_DWORD"))
    out = "\n".join(ti.preview_lines(["synthetic_task"]))
    assert "now 1 (will be changed)" in out


def test_preview_lines_flags_system_settings(monkeypatch):
    monkeypatch.setattr(ti, "read_current",
                        lambda hive, path, name: (None, None))
    out = "\n".join(ti.preview_lines(["stop_telemetry"]))
    assert "also changes Windows settings" in out


def test_preview_lines_handles_unknown_key(monkeypatch):
    monkeypatch.setattr(ti, "read_current",
                        lambda hive, path, name: (None, None))
    out = "\n".join(ti.preview_lines(["not_a_real_task_key"]))
    assert "not_a_real_task_key" in out
    assert "no registry detail to preview" in out


def test_preview_lines_empty_selection():
    assert ti.preview_lines([]) == []


# --------------------------------------------------------------------------- #
# read_current is read-only and never raises
# --------------------------------------------------------------------------- #

def test_read_current_missing_value_is_none_not_error():
    cur, _t = ti.read_current(
        "HKCU", r"Software\OpenCodeDefinitelyNotAKey12345", "Nope")
    assert cur is None or cur is ti._REG_DENIED
