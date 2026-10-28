"""Regenerate the README screenshots from the real app.

A-006. The committed shots had drifted two redesigns behind (and one path was
case-broken on GitHub), because there was no way to take a new one except by
hand. This script builds the application off the normal launch path, visits
each main tab, and writes ``screenshots/<tab>.png`` with a stdlib-only capture
(ctypes + GDI + a small PNG encoder), so the front page can be refreshed with
one command and no new dependency:

    python dev/tools/capture_screenshots.py

The extension is lowercase on purpose: the README links these files by name and
GitHub serves them case-sensitively, so a ``.PNG`` written here is a 404 there
even though Windows hides the difference locally.

It opens the real window for a few seconds per tab. Run it on a desktop
session; a headless/session-0 process paints nothing, and the blank-frame
check refuses to write that over a good screenshot.

The capture asks the window to draw itself (PrintWindow) instead of reading
that patch of screen, because a fullscreen game on top of the window used to
end up in the shot. It also opts the process into DPI awareness first: without
that, Windows reports the window rect inflated by the display scale while the
window itself paints at 96 DPI, so every frame came out with the UI crammed
into a corner over black.
"""
from __future__ import annotations

import ctypes
import os
import struct
import sys
import time
import zlib
from ctypes import wintypes

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
OUT_DIR = os.path.join(ROOT, "screenshots")

#: Tab -> file stem. The order matches the README table.
TABS = (
    ("Clean", "clean"),
    ("Repair", "repair"),
    ("Tweak", "tweak"),
    ("Install", "install"),
    ("Drivers", "drivers"),
    ("Tools", "tools"),
    ("Settings", "settings"),
    ("Health", "health"),
)

user32 = ctypes.windll.user32
gdi32 = ctypes.windll.gdi32


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [
        ("biSize", wintypes.DWORD), ("biWidth", wintypes.LONG),
        ("biHeight", wintypes.LONG), ("biPlanes", wintypes.WORD),
        ("biBitCount", wintypes.WORD), ("biCompression", wintypes.DWORD),
        ("biSizeImage", wintypes.DWORD), ("biXPelsPerMeter", wintypes.LONG),
        ("biYPelsPerMeter", wintypes.LONG), ("biClrUsed", wintypes.DWORD),
        ("biClrImportant", wintypes.DWORD),
    ]


class BITMAPINFO(ctypes.Structure):
    _fields_ = [("bmiHeader", BITMAPINFOHEADER),
                ("bmiColors", wintypes.DWORD * 3)]


def _screen_rect(hwnd):
    """The window's VISIBLE bounds, without the DWM drop-shadow margin.

    GetWindowRect includes the transparent shadow frame (about 8px a side on
    Windows 11), so capturing it reads whatever is behind the window along
    every edge. DWMWA_EXTENDED_FRAME_BOUNDS is the documented way to ask for
    the pixels that are actually the window.
    """
    rect = wintypes.RECT()
    try:
        hr = ctypes.windll.dwmapi.DwmGetWindowAttribute(
            ctypes.c_void_p(hwnd), 9,  # DWMWA_EXTENDED_FRAME_BOUNDS
            ctypes.byref(rect), ctypes.sizeof(rect))
    except Exception:
        hr = -1
    if hr != 0:
        rect = wintypes.RECT()
        if not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
            raise OSError("GetWindowRect failed")
    return rect.left, rect.top, rect.right - rect.left, rect.bottom - rect.top


def _write_png(path, width, height, bgra):
    """Minimal PNG writer: RGB, filter 0, zlib-deflated scanlines."""
    raw = bytearray()
    stride = width * 4
    for y in range(height - 1, -1, -1):   # BGRA rows arrive bottom-up
        row = bgra[y * stride:(y + 1) * stride]
        raw.append(0)
        for x in range(0, len(row), 4):
            raw.extend((row[x + 2], row[x + 1], row[x]))  # B,G,R -> R,G,B

    def chunk(tag, payload):
        return (struct.pack(">I", len(payload)) + tag + payload
                + struct.pack(">I", zlib.crc32(tag + payload) & 0xFFFFFFFF))

    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    png = (b"\x89PNG\r\n\x1a\n"
           + chunk(b"IHDR", header)
           + chunk(b"IDAT", zlib.compress(bytes(raw), 6))
           + chunk(b"IEND", b""))
    with open(path, "wb") as fh:
        fh.write(png)


SRCCOPY = 0x00CC0020
CAPTUREBLT = 0x40000000
PW_RENDERFULLCONTENT = 0x00000002


def _grab(width, height, draw):
    """Paint through `draw` into an off-screen DC; return its BGRA rows.

    `draw(hdc)` returns truthy on success. None means the paint failed, which
    lets the caller try the next method instead of raising.
    """
    hdc_screen = user32.GetDC(0)
    hdc_mem = gdi32.CreateCompatibleDC(hdc_screen)
    bmp = gdi32.CreateCompatibleBitmap(hdc_screen, width, height)
    old = gdi32.SelectObject(hdc_mem, bmp)
    try:
        if not draw(hdc_mem):
            return None
        header = BITMAPINFO()
        header.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
        header.bmiHeader.biWidth = width
        header.bmiHeader.biHeight = height
        header.bmiHeader.biPlanes = 1
        header.bmiHeader.biBitCount = 32
        header.bmiHeader.biCompression = 0  # BI_RGB
        buf = ctypes.create_string_buffer(width * height * 4)
        got = gdi32.GetDIBits(hdc_mem, bmp, 0, height, buf,
                              ctypes.byref(header), 0)
        if got != height:
            return None
        return buf.raw
    finally:
        gdi32.SelectObject(hdc_mem, old)
        gdi32.DeleteObject(bmp)
        gdi32.DeleteDC(hdc_mem)
        user32.ReleaseDC(0, hdc_screen)


def _looks_blank(bgra):
    """True when the frame is effectively one flat colour.

    A window that never painted (session 0, or a minimized window) comes back
    as a single shade. Writing that over a good screenshot is worse than
    failing loudly, so the caller refuses it.
    """
    step = max(4, (len(bgra) // 4096) // 4 * 4)
    shades = set()
    lowest, highest = 255, 0
    for i in range(0, len(bgra) - 3, step):
        b, g, r = bgra[i], bgra[i + 1], bgra[i + 2]
        shades.add((r >> 3, g >> 3, b >> 3))
        luma = (r * 54 + g * 183 + b * 19) >> 8
        lowest = min(lowest, luma)
        highest = max(highest, luma)
    return len(shades) < 8 or highest - lowest < 12


def capture(hwnd, path):
    """Save the window's own pixels as PNG.

    PrintWindow asks the window to draw itself, so whatever else is on top of
    it (a fullscreen game, for one) cannot appear in the shot. A window that
    refuses WM_PRINT falls back to reading that patch of screen, which is only
    correct when nothing overlaps it.
    """
    left, top, width, height = _screen_rect(hwnd)
    if width < 64 or height < 64:
        raise OSError("window has no measurable size")
    pixels = _grab(width, height, lambda hdc: user32.PrintWindow(
        ctypes.c_void_p(hwnd), hdc, PW_RENDERFULLCONTENT))
    if pixels is None or _looks_blank(pixels):
        hdc_screen = user32.GetDC(0)
        try:
            # SRCCOPY | CAPTUREBLT: the whole window including layered
            # children (Tk's custom chrome draws into its own surface).
            pixels = _grab(width, height, lambda hdc: gdi32.BitBlt(
                hdc, 0, 0, width, height, hdc_screen, left, top,
                SRCCOPY | CAPTUREBLT))
        finally:
            user32.ReleaseDC(0, hdc_screen)
    if pixels is None:
        raise OSError("could not read the window's pixels")
    if _looks_blank(pixels):
        raise OSError("the window painted a blank frame "
                      "(no interactive desktop?)")
    _write_png(path, width, height, pixels)


def main(argv=None):
    if not sys.platform.startswith("win"):
        print("This script captures a Windows desktop.")
        return 1
    os.makedirs(OUT_DIR, exist_ok=True)
    if ROOT not in sys.path:
        sys.path.insert(0, ROOT)
    # Imported here so a syntax check on any OS does not pull in Tk.
    from app.dpi import apply_ui_density, enable_dpi_awareness
    import app.ui.app as appmod
    import tkinter as tk

    # Screenshots are of the app, not of the administrator gate. Without this
    # the launch path shows AdminGateFrame on a non-elevated machine and the
    # "tabs" are all gate.
    appmod.is_admin = lambda: True
    Application = appmod.Application
    # A capture is not a real launch: the startup health check would append a
    # checkup to the user's own ledger and leave the badge reading
    # "PC: checking..." for the whole session, because its worker reports back
    # through a main loop this script never runs. The app already gates that
    # check on this variable for its test harness.
    os.environ.setdefault("CLEANER_TOOL_NO_AUTO_HEALTH", "1")

    # Before the first Tk window: the app's normal entry path does this too.
    # Without it Windows reports the window rect inflated by the display
    # scale while the window paints at 96 DPI, and the capture is the UI
    # crammed into one corner of a mostly black frame.
    enable_dpi_awareness()

    root = tk.Tk()
    root.withdraw()
    apply_ui_density(root, "system")
    app = Application(root)
    # Keep the footprint Application resolved for this display (it scales the
    # window with the DPI); only move it, and park it away from the edges.
    root.geometry("+40+40")
    root.deiconify()
    root.lift()
    root.update()
    time.sleep(0.6)          # let the first paint and catalog slice settle
    try:
        hwnd = int(root.wm_frame(), 16)
    except Exception:
        hwnd = int(root.winfo_id())

    failures = []
    for tab, stem in TABS:
        try:
            app._switch_to(tab)
            root.update()
            # Two pumps: a page's own after() paint (cards, icons) runs on
            # the second, which is why one update captures an empty grid.
            time.sleep(0.35)
            root.update()
            time.sleep(0.25)
            root.update()
            path = os.path.join(OUT_DIR, "%s.png" % stem)
            capture(hwnd, path)
            print("wrote %s" % os.path.relpath(path, ROOT))
        except Exception as exc:
            failures.append("%s: %s" % (tab, exc))
    try:
        root.destroy()
    except Exception:
        pass
    if failures:
        print("could not capture: %s" % "; ".join(failures))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
