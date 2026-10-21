"""The crash log must capture what the windowed build used to lose."""
from __future__ import annotations

import logging
import os
import sys

from app import diagnostics


def _use_tmp_log(monkeypatch, tmp_path):
    monkeypatch.setenv("CLEANER_TOOL_LOG_DIR", str(tmp_path))
    logger = logging.getLogger("cleaner_tool")
    for h in list(logger.handlers):
        logger.removeHandler(h)
        try:
            h.close()
        except Exception:
            pass
    return tmp_path / "cleaner-tool.log"


def test_exception_is_written_with_traceback(monkeypatch, tmp_path):
    path = _use_tmp_log(monkeypatch, tmp_path)
    try:
        raise ValueError("boom-canary")
    except ValueError:
        diagnostics.log_exception(*sys.exc_info(), source="test")

    text = path.read_text(encoding="utf-8")
    assert "boom-canary" in text
    assert "EXCEPTION in test" in text
    assert "ValueError" in text


def test_diagnostics_text_has_version_and_environment(monkeypatch, tmp_path):
    _use_tmp_log(monkeypatch, tmp_path)
    diagnostics.log("hello-diagnostics")
    text = diagnostics.diagnostics_text()
    assert "Cleaner Tool" in text
    assert "Python" in text
    assert "hello-diagnostics" in text


def test_install_hooks_is_idempotent_and_chains(monkeypatch, tmp_path):
    _use_tmp_log(monkeypatch, tmp_path)
    original = sys.excepthook
    try:
        diagnostics.install_hooks()
        first = sys.excepthook
        diagnostics.install_hooks()
        assert sys.excepthook is first, "a second install must not wrap again"

        try:
            raise RuntimeError("hook-canary")
        except RuntimeError:
            first(*sys.exc_info())

        assert "hook-canary" in (tmp_path / "cleaner-tool.log").read_text(
            encoding="utf-8")
    finally:
        sys.excepthook = original
