"""Extract a small icon from an .exe (or a registry DisplayIcon spec like
'C:\\path\\app.exe,0') as raw PPM bytes tk.PhotoImage can load directly —
no Pillow, no pywin32, stdlib ctypes only (matches this app's zero-runtime-
dependency policy, see requirements.txt).

Pipeline: ExtractIconExW -> GetIconInfo -> GetObject (real pixel size) ->
GetDIBits (32bpp top-down BGRA) -> composite alpha over a background color
(falling back to the icon's own AND-mask for classic icons with no real
alpha plane) -> binary PPM (P6).

Every public entry point is failure-tolerant: any Win32 call failing, any
struct mismatch, any non-Windows platform returns None rather than raising,
so a bad/missing icon never breaks the caller — it just shows a placeholder.
"""
import os
import sys
import ctypes

IS_WINDOWS = sys.platform.startswith("win")

# GetDIBits usage constant
_DIB_RGB_COLORS = 0
_BI_RGB = 0


def default_icon_ppm(size=20, rgb=(90, 96, 104)):
    """A flat placeholder swatch, same PPM format as a real extracted
    icon, so callers can treat 'no icon yet' and 'real icon' identically.
    Pure Python — works on any platform, used for tests too."""
    size = max(1, int(size))
    r, g, b = (max(0, min(255, int(c))) for c in rgb)
    header = ("P6\n%d %d\n255\n" % (size, size)).encode("ascii")
    row = bytes((r, g, b)) * size
    return header + row * size


def _split_icon_spec(spec):
    """'"C:\\a\\b.exe",0' / 'C:\\a\\b.exe,0' / 'C:\\a\\b.exe' -> (path, index).
    Also expands %ENV% vars — DisplayIcon-style specs very often carry
    them (e.g. '%SystemRoot%\\System32\\shell32.dll,-1'), and
    ExtractIconExW needs a literal path. game_catalog already expands
    before storing, but this is defense-in-depth for any other caller."""
    spec = (spec or "").strip()
    if spec.startswith('"'):
        # quoted path, index (if any) comes after the closing quote
        end = spec.find('"', 1)
        if end != -1:
            path = os.path.expandvars(spec[1:end])
            rest = spec[end + 1:].lstrip(", ")
            try:
                return path, int(rest) if rest else 0
            except ValueError:
                return path, 0
    if "," in spec:
        # Only trust the split if the tail is actually a bare integer — a
        # folder name can itself contain a comma (e.g. "C:\...\Foo,
        # Inc\app.exe" with no index at all), and blindly rpartition-ing
        # would silently truncate the real path in that case.
        head, _, tail = spec.rpartition(",")
        tail = tail.strip()
        if tail.lstrip("-").isdigit():
            return os.path.expandvars(head.strip().strip('"')), int(tail)
        return os.path.expandvars(spec.strip().strip('"')), 0
    return os.path.expandvars(spec.strip().strip('"')), 0


def _composite_rgb(bgra, width, height, bg_rgb=(30, 30, 30)):
    """bgra: bytes, length width*height*4, top-down BGRA rows (may have an
    all-zero alpha plane on classic icons — treated as fully opaque in that
    case, since there's no usable alpha to composite with). -> raw RGB
    bytes (width*height*3), no PPM header — callers that need PPM wrap it
    with _rgb_to_ppm; callers that need a uniform output size resize first
    with _resize_rgb_nearest."""
    if not bgra or len(bgra) < width * height * 4:
        return None
    bg_r, bg_g, bg_b = bg_rgb
    has_alpha = any(bgra[i] for i in range(3, len(bgra), 4))
    out = bytearray(width * height * 3)
    for p in range(width * height):
        o = p * 4
        b, g, r, a = bgra[o], bgra[o + 1], bgra[o + 2], bgra[o + 3]
        if has_alpha and a != 255:
            if a == 0:
                r, g, b = bg_r, bg_g, bg_b
            else:
                inv = 255 - a
                r = (r * a + bg_r * inv) // 255
                g = (g * a + bg_g * inv) // 255
                b = (b * a + bg_b * inv) // 255
        oo = p * 3
        out[oo] = r
        out[oo + 1] = g
        out[oo + 2] = b
    return bytes(out)


def _resize_rgb_nearest(rgb, sw, sh, dw, dh):
    """Nearest-neighbor resize of a raw RGB buffer. Icons Windows hands
    back come in whatever native resolution the binary embeds (16x16,
    32x32, 48x48…) — without this every row in the uninstall list would
    render at a different pixel size, which wouldn't read as a clean,
    uniform icon column the way Control Panel's list does. No dependency
    needed (no Pillow): nearest-neighbor is a handful of lines and plenty
    good at icon sizes."""
    if sw == dw and sh == dh:
        return rgb
    out = bytearray(dw * dh * 3)
    for y in range(dh):
        sy = min(sh - 1, (y * sh) // dh)
        srow = sy * sw * 3
        drow = y * dw * 3
        for x in range(dw):
            sx = min(sw - 1, (x * sw) // dw)
            so = srow + sx * 3
            do = drow + x * 3
            out[do] = rgb[so]
            out[do + 1] = rgb[so + 1]
            out[do + 2] = rgb[so + 2]
    return bytes(out)


def _rgb_to_ppm(rgb, width, height):
    header = ("P6\n%d %d\n255\n" % (width, height)).encode("ascii")
    return header + rgb


def icon_ppm_bytes(icon_spec, size=20, bg_rgb=(30, 30, 30)):
    """Best-effort: extract the icon named by icon_spec (registry
    DisplayIcon format or a plain exe path) and return PPM bytes, resized
    to exactly size x size regardless of the binary's native icon
    resolution (16x16, 32x32, 48x48…) so every row in the list renders at
    the same pixel size. None on any failure or off Windows."""
    if not IS_WINDOWS or not icon_spec:
        return None
    try:
        path, index = _split_icon_spec(icon_spec)
        if not path:
            return None
        bgra, w, h = _extract_icon_rgba(path, index)
        if not bgra:
            return None
        rgb = _composite_rgb(bgra, w, h, bg_rgb)
        if not rgb:
            return None
        rgb = _resize_rgb_nearest(rgb, w, h, size, size)
        return _rgb_to_ppm(rgb, size, size)
    except Exception:
        return None


def _extract_icon_rgba(path, index):
    """-> (bgra_bytes, width, height) or (None, 0, 0). Windows-only.
    Always prefers the binary's 'small' icon resource (Windows' own
    system-small-icon size, typically 16x16 — the closest native match to
    a list row) and only falls back to 'large' when no small icon exists;
    icon_ppm_bytes resizes whichever comes back to a uniform output size."""
    shell32 = ctypes.windll.shell32
    user32 = ctypes.windll.user32

    large = ctypes.c_void_p(0)
    small = ctypes.c_void_p(0)
    try:
        n = shell32.ExtractIconExW(
            ctypes.c_wchar_p(path), ctypes.c_int(index),
            ctypes.byref(large), ctypes.byref(small), ctypes.c_uint(1))
    except Exception:
        return None, 0, 0
    if not n or (not large.value and not small.value):
        return None, 0, 0

    # Prefer whichever handle is populated; small (usually 16px) is the
    # closer match to our ~20px row height, large (usually 32/48px) is
    # the fallback when a binary has no small-icon resource.
    hicon = small.value or large.value
    try:
        return _hicon_to_bgra(hicon)
    finally:
        for h in (large, small):
            try:
                if h.value:
                    user32.DestroyIcon(h.value)
            except Exception:
                pass


class _ICONINFO(ctypes.Structure):
    _fields_ = [
        ("fIcon", ctypes.c_long),
        ("xHotspot", ctypes.c_ulong),
        ("yHotspot", ctypes.c_ulong),
        ("hbmMask", ctypes.c_void_p),
        ("hbmColor", ctypes.c_void_p),
    ]


class _BITMAP(ctypes.Structure):
    _fields_ = [
        ("bmType", ctypes.c_long),
        ("bmWidth", ctypes.c_long),
        ("bmHeight", ctypes.c_long),
        ("bmWidthBytes", ctypes.c_long),
        ("bmPlanes", ctypes.c_ushort),
        ("bmBitsPixel", ctypes.c_ushort),
        ("bmBits", ctypes.c_void_p),
    ]


class _BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [
        ("biSize", ctypes.c_ulong),
        ("biWidth", ctypes.c_long),
        ("biHeight", ctypes.c_long),
        ("biPlanes", ctypes.c_ushort),
        ("biBitCount", ctypes.c_ushort),
        ("biCompression", ctypes.c_ulong),
        ("biSizeImage", ctypes.c_ulong),
        ("biXPelsPerMeter", ctypes.c_long),
        ("biYPelsPerMeter", ctypes.c_long),
        ("biClrUsed", ctypes.c_ulong),
        ("biClrImportant", ctypes.c_ulong),
    ]


class _BITMAPINFO(ctypes.Structure):
    _fields_ = [
        ("bmiHeader", _BITMAPINFOHEADER),
        ("bmiColors", ctypes.c_ulong * 3),  # unused at 32bpp/BI_RGB
    ]


def _hicon_to_bgra(hicon):
    gdi32 = ctypes.windll.gdi32
    user32 = ctypes.windll.user32

    info = _ICONINFO()
    if not user32.GetIconInfo(ctypes.c_void_p(hicon), ctypes.byref(info)):
        return None, 0, 0

    hbm_color = info.hbmColor
    hbm_mask = info.hbmMask
    hdc = None
    try:
        if not hbm_color:
            return None, 0, 0  # monochrome-only icon, not handled here
        bmp = _BITMAP()
        if not gdi32.GetObjectW(
                ctypes.c_void_p(hbm_color), ctypes.sizeof(_BITMAP),
                ctypes.byref(bmp)):
            return None, 0, 0
        w, h = bmp.bmWidth, bmp.bmHeight
        if w <= 0 or h <= 0 or w > 512 or h > 512:
            return None, 0, 0

        hdc = gdi32.CreateCompatibleDC(None)
        if not hdc:
            return None, 0, 0

        bmi = _BITMAPINFO()
        bmi.bmiHeader.biSize = ctypes.sizeof(_BITMAPINFOHEADER)
        bmi.bmiHeader.biWidth = w
        bmi.bmiHeader.biHeight = -h  # negative = top-down rows
        bmi.bmiHeader.biPlanes = 1
        bmi.bmiHeader.biBitCount = 32
        bmi.bmiHeader.biCompression = _BI_RGB

        buf = ctypes.create_string_buffer(w * h * 4)
        got = gdi32.GetDIBits(
            ctypes.c_void_p(hdc), ctypes.c_void_p(hbm_color), 0, h,
            buf, ctypes.byref(bmi), _DIB_RGB_COLORS)
        if not got:
            return None, 0, 0
        return bytes(buf.raw), w, h
    finally:
        try:
            if hdc:
                gdi32.DeleteDC(hdc)
        except Exception:
            pass
        for hbm in (hbm_color, hbm_mask):
            try:
                if hbm:
                    gdi32.DeleteObject(ctypes.c_void_p(hbm))
            except Exception:
                pass
