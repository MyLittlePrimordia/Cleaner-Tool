"""hero_icon: a large tonal icon container for simple tool popups.

Material 3 empty/hero states put one big icon in a tinted circle above a
short headline. The icon is the same Noto emoji PNG the Tools grid uses
(assets/tools_icons/<key>.png, 72px native), so popup and card match.
"""
from __future__ import annotations

import tkinter as tk

from app.ui.theme import COLORS, _hex_lerp


def hero_icon(parent, key, accent, bg=None, box=96, folder="tools_icons"):
    try:
        page = bg or parent["bg"]
    except Exception:
        page = COLORS["bg"]
    cv = tk.Canvas(parent, width=box, height=box, bg=page,
                   highlightthickness=0, bd=0)
    cv.create_oval(1, 1, box - 1, box - 1,
                   fill=_hex_lerp(page, accent, 0.22), outline="")
    cv._img = None
    try:
        from app.utils import resolve_asset_path
        path = resolve_asset_path(f"{folder}/{key}.png")
        if path:
            cv._img = tk.PhotoImage(file=path)
            cv.create_image(box // 2, box // 2, image=cv._img)
    except Exception:
        cv._img = None
    return cv
