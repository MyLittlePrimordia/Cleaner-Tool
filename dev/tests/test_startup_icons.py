"""Startup Manager icons: use the real large resource, at the display size.

The rows draw at 32 logical pixels. The extractor used to prefer the 16x16
"small" resource and upscale it, which is why every startup icon looked
blocky. These tests pin the two axes that caused it: which handle is chosen,
and what size the row asks for.
"""
from __future__ import annotations

import tkinter as tk

from _tkhelpers import make_root


def test_tool_row_leading_slot_is_tall_enough_for_an_icon():
    """Regression: a width-only leading slot used to clip icons to 1px."""
    from app.ui.widgets import ToolRow

    root = make_root()
    try:
        root.geometry("420x180")
        row = ToolRow(root, title="Startup item", leading_px=32, size="lg")
        photo = tk.PhotoImage(width=32, height=32)
        photo.put("#ffffff", to=(0, 0, 32, 32))
        icon = tk.Label(row.leading, image=photo)
        icon.image = photo
        icon.pack(fill="both", expand=True)
        row.pack(fill="x")
        root.update()
        assert row.leading.winfo_height() >= 32
        assert icon.winfo_height() >= 32
    finally:
        root.destroy()


def test_extract_prefers_the_large_icon_handle(monkeypatch):
    """A 32/48px large handle must win over a 16px small handle."""
    from app import icon_extract

    calls = []

    class _Shell:
        def ExtractIconExW(self, path, index, large, small, count):
            # Give both handles a value, as a real multi-size icon does.
            large._obj.value = 111
            small._obj.value = 222
            return 1

    class _User:
        def DestroyIcon(self, handle):
            calls.append(("destroy", handle))

    monkeypatch.setattr(icon_extract, "IS_WINDOWS", True)
    monkeypatch.setattr(icon_extract.ctypes, "windll",
                        type("W", (), {"shell32": _Shell(),
                                       "user32": _User()})(), raising=False)
    seen = []
    monkeypatch.setattr(icon_extract, "_hicon_to_bgra",
                        lambda hicon: (seen.append(hicon)
                                       or (b"\x00" * 16, 2, 2)))
    icon_extract._extract_icon_rgba("C:/fake/app.exe", 0)
    assert seen == [111]


def test_extract_falls_back_to_small_when_large_is_missing(monkeypatch):
    from app import icon_extract

    class _Shell:
        def ExtractIconExW(self, path, index, large, small, count):
            large._obj.value = 0
            small._obj.value = 222
            return 1

    class _User:
        def DestroyIcon(self, handle):
            pass

    monkeypatch.setattr(icon_extract, "IS_WINDOWS", True)
    monkeypatch.setattr(icon_extract.ctypes, "windll",
                        type("W", (), {"shell32": _Shell(),
                                       "user32": _User()})(), raising=False)
    seen = []
    monkeypatch.setattr(icon_extract, "_hicon_to_bgra",
                        lambda hicon: (seen.append(hicon)
                                       or (b"\x00" * 16, 2, 2)))
    icon_extract._extract_icon_rgba("C:/fake/app.exe", 0)
    assert seen == [222]
