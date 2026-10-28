"""N-005: the support bundle is redacted, complete, and cannot be broken.

A bundle is only worth attaching to a public issue if two things are true: it
contains no credential or account name, and it still exists when one of its
sources is unreadable on the reporter's machine. Both are pinned here, plus the
manifest actually listing what the zip holds -- a manifest that drifts from the
archive is worse than none.
"""
from __future__ import annotations

import zipfile

from app import support_bundle


def _plain_build(monkeypatch, log=None):
    """Build with empty sources, so the tests never read the developer's PC."""
    monkeypatch.setattr("app.diagnostics.diagnostics_text", lambda: "diag\n")
    monkeypatch.setattr("app.config_events.read_events", lambda limit=400: [])
    monkeypatch.setattr("app.config_persist.load_config", lambda: {"theme": "dark"})
    monkeypatch.setattr("app.history.runs", lambda: [])
    monkeypatch.setattr("app.health_history.entries", lambda limit=0: [])
    return support_bundle.build(in_memory_log=log)


def test_every_file_is_redacted(monkeypatch):
    monkeypatch.setattr("app.diagnostics.diagnostics_text", lambda: (
        "argv=[--elevation-token=deadbeefdeadbeef]\n"
        "C:\\Users\\Bob\\AppData\\Local\\CleanerTool\n"))
    files = support_bundle.build(
        in_memory_log=["relaunch --elevation-token=deadbeefdeadbeef\n"])
    joined = "".join(item.text for item in files)
    assert "deadbeefdeadbeef" not in joined
    assert "Bob" not in joined
    assert "<redacted>" in joined
    assert "<user>" in joined


def test_the_manifest_lists_exactly_what_the_zip_holds(monkeypatch, tmp_path):
    files = _plain_build(monkeypatch)
    path = tmp_path / "bundle.zip"
    support_bundle.write(files, str(path))

    with zipfile.ZipFile(path) as archive:
        names = archive.namelist()
        manifest = archive.read("manifest.txt").decode("utf-8")
    assert sorted(names) == sorted(item.name for item in files)
    for name in names:
        if name != "manifest.txt":
            assert name in manifest, "%s missing from the manifest" % name
    assert "not uploaded anywhere" in manifest


def test_the_manifest_is_written_last_so_it_knows_the_sizes(monkeypatch):
    files = _plain_build(monkeypatch, log=["hello"])
    assert files[-1].name == "manifest.txt"
    assert any("applog.txt" in line for line in files[-1].text.splitlines())


def test_a_broken_source_becomes_a_readable_line_not_an_exception(monkeypatch):
    def _boom():
        raise OSError("config file locked")

    monkeypatch.setattr("app.config_persist.load_config", _boom)
    files = support_bundle.build(in_memory_log=[])
    config = next(item for item in files if item.name == "config.txt")
    assert "could not be read" in config.text
    assert "config file locked" in config.text
    # And the rest of the bundle still exists.
    assert len(files) == 7


def test_the_config_file_omits_the_bulky_ledgers_but_names_them(monkeypatch):
    monkeypatch.setattr(
        "app.config_persist.load_config",
        lambda: {"theme": "dark", "run_history": [{"t": 1, "b": 2}],
                 "health_history": [{"t": 1, "s": 90, "g": "A"}]})
    files = support_bundle.build(in_memory_log=[])
    config = next(item for item in files if item.name == "config.txt").text
    assert "theme = dark" in config
    assert "run_history = " not in config
    assert "Not included: run_history, health_history" in config


def test_the_last_run_names_its_changes(monkeypatch):
    monkeypatch.setattr("app.history.runs", lambda: [
        {"t": 1700000000, "b": 2048, "k": "clean",
         "c": [{"k": "registry", "l": "GameDVR_Enabled", "d": "1 \u2192 0"}]},
    ])
    files = support_bundle.build(in_memory_log=[])
    text = next(item for item in files if item.name == "last_run.txt").text
    assert "Clean" in text
    assert "2048 bytes" in text
    assert "GameDVR_Enabled: 1 \u2192 0" in text


def test_a_missing_history_says_so_instead_of_failing(monkeypatch):
    files = _plain_build(monkeypatch)
    last_run = next(item for item in files if item.name == "last_run.txt").text
    health = next(item for item in files if item.name == "health.txt").text
    assert "No completed run" in last_run
    assert "No checkup" in health


def test_the_preview_is_one_line_per_file_with_a_size(monkeypatch):
    files = _plain_build(monkeypatch, log=["x" * 2048])
    lines = support_bundle.summary_lines(files)
    assert len(lines) == len(files)
    assert any(line.startswith("applog.txt  (") and "KB" in line
               for line in lines)
    assert all("(" in line and line.endswith(")") for line in lines)


def test_a_long_source_is_clipped_rather_than_shipped_whole(monkeypatch):
    files = support_bundle.build(
        in_memory_log=["y" * (support_bundle.MAX_TEXT_CHARS + 5000)])
    applog = next(item for item in files if item.name == "applog.txt")
    assert applog.size < support_bundle.MAX_TEXT_CHARS + 2000
    assert "truncated" in applog.text


def test_the_default_name_is_a_zip(monkeypatch):
    name = support_bundle.default_name(now=1700000000)
    assert name.startswith("CleanerTool_Support_") and name.endswith(".zip")
