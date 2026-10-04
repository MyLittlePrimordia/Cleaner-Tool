"""Icon delivery: Uninstall Programs and Startup Manager.

Both dialogs used to show grey squares, for reasons that turned out to be
three separate bugs rather than one:

  1. CROSS-THREAD .after(). The scan result and every icon swap were posted
     with `widget.after(0, fn)` FROM A WORKER THREAD. That is the documented
     cross-thread Tcl race and the callback is simply dropped when the main
     thread is not inside the event loop. Measured here: the scan callback
     never arrived at all and the program list stayed empty through 15s of
     pumping. app/ui/tkdispatch.py is the fix — a queue drained by a Tk-thread
     pump, the same pattern the Game Session pilot was moved onto earlier for
     the same reason.

  2. NO ICON SOURCE for many entries. 16 of 41 installed programs have no
     usable DisplayIcon: MSI installers and Electron/Squirrel apps write none
     at all, some nest the real exe in bin/, and some point at a binary with
     no icon resource. app/icon_resolve.py now hunts the install folder.

  3. A HEX STRING passed where an RGB TUPLE was expected. Both dialogs pass
     COLORS["bg_alt"], which is "#151B24"; unpacking that as bg_r, bg_g, bg_b
     raised ValueError, and icon_ppm_bytes's blanket `except Exception`
     converted it to a silent None. Every Startup Manager icon vanished with
     no error anywhere. icon_extract._as_rgb now coerces at the boundary.

These checks assert the observable outcome, on the real machine's real
registry, because that is the only place the failure modes exist.
"""
from __future__ import annotations

import io
import os
import sys
import time

import pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import tkinter as tk

FAILS = []
N = [0]


def ck(label, cond, detail=""):
    N[0] += 1
    if cond:
        print("  PASS  %s" % label)
    else:
        FAILS.append(label)
        print("  FAIL  %s%s" % (label, ("  <- " + repr(detail)) if detail else ""))


def pump(root, dlg, n=400, delay=0.01):
    for _ in range(n):
        try:
            root.update()
            dlg._dlg.update()
        except Exception:
            pass
        time.sleep(delay)


def count_icon_labels(dlg):
    """(real, glyph) image labels anywhere in the dialog's widget tree."""
    ph = str(getattr(dlg, "_icon_default_img", None))
    real = glyph = 0

    def walk(w):
        nonlocal real, glyph
        for c in w.winfo_children():
            if isinstance(c, tk.Label):
                im = c.cget("image")
                if im:
                    if im == ph:
                        glyph += 1
                    else:
                        real += 1
            walk(c)

    try:
        walk(dlg._dlg)
    except Exception:
        pass
    return real, glyph


# --------------------------------------------------------------------------- #
print("\n[1] pure helpers (no registry, no Tk)")
from app.icon_extract import _as_rgb, default_icon_ppm, icon_ppm_bytes
from app.icon_resolve import (exe_from_command, find_icon_in_folder,
                              generic_app_icon_ppm, icon_source_for_command,
                              lnk_target, resolve_program_icon)

ck("_as_rgb accepts a hex string", _as_rgb("#151B24") == (21, 27, 36),
   _as_rgb("#151B24"))
ck("_as_rgb accepts a tuple", _as_rgb((1, 2, 3)) == (1, 2, 3))
ck("_as_rgb rejects junk without raising", _as_rgb("nope", (9, 9, 9)) == (9, 9, 9))
ck("_as_rgb survives a None", _as_rgb(None) == (30, 30, 30))
ck("default_icon_ppm takes a hex string",
   default_icon_ppm(4, "#151B24").startswith(b"P6\n4 4\n255\n"))
ck("icon_ppm_bytes takes a hex bg",
   (icon_ppm_bytes(r"C:\Windows\System32\shell32.dll", 16, "#151B24") or b"")
   .startswith(b"P6\n"))

g = generic_app_icon_ppm(18, "#151B24")
ck("the generic glyph is produced from a hex colour", bool(g))
if g:
    body = g.split(b"255\n", 1)[1]
    cols = {body[i:i + 3] for i in range(0, len(body), 3)}
    ck("the glyph is real artwork, not a flat square", len(cols) >= 2, len(cols))

# command lines: the bug that produced a RELATIVE path
ck("a quoted command resolves", (exe_from_command(
    '"C:\\Program Files (x86)\\Steam\\steam.exe" -silent') or "").endswith("steam.exe"),
   exe_from_command('"C:\\Program Files (x86)\\Steam\\steam.exe" -silent'))
ck("an UNQUOTED path with spaces resolves to the whole path",
   exe_from_command(r"C:\Program Files (x86)\Common Files\Java\Java Update\jusched.exe")
   == r"C:\Program Files (x86)\Common Files\Java\Java Update\jusched.exe",
   exe_from_command(r"C:\Program Files (x86)\Common Files\Java\Java Update\jusched.exe"))
ck("a bare name with no file on disk still returns a token",
   (exe_from_command("MsiExec.exe /X{1234}") or "").lower().endswith("msiexec.exe"))
ck("an empty command is None", exe_from_command("") is None)
ck("a non-exe command is None", exe_from_command("notepad /x") is None)

# --------------------------------------------------------------------------- #
print("\n[2] against the real registry")
from app.game_catalog import list_installed_programs
from app.startup_manager import list_startup_items

t0 = time.perf_counter()
progs = list_installed_programs()
scan = time.perf_counter() - t0
with_spec = [p for p in progs if p.get("icon")]
with_pixels = [p for p in with_spec
               if icon_ppm_bytes(p["icon"], 18, (30, 32, 36))]
print("        %d programs, scan %.2fs, %d with a spec, %d with pixels"
      % (len(progs), scan, len(with_spec), len(with_pixels)))
ck("the scan stays responsive (< 6s)", scan < 6.0, scan)
ck("most programs now have an icon spec",
   len(with_spec) >= max(1, int(len(progs) * 0.85)),
   "%d/%d" % (len(with_spec), len(progs)))
ck("most of those yield real pixels",
   len(with_pixels) >= max(1, int(len(with_spec) * 0.7)),
   "%d/%d" % (len(with_pixels), len(with_spec)))

items = list_startup_items()
s_spec = [i for i in items if icon_source_for_command(i.get("command") or "")]
s_px = [i for i in s_spec
        if icon_ppm_bytes(icon_source_for_command(i["command"]), 18, (30, 32, 36))]
print("        %d startup items, %d resolve, %d with pixels"
      % (len(items), len(s_spec), len(s_px)))
ck("most startup items resolve to an icon source",
   len(s_spec) >= max(1, int(len(items) * 0.7)),
   "%d/%d" % (len(s_spec), len(items)))

# the nested-exe case that motivated the folder hunt
aud = [p for p in progs if "audacity" in p["name"].lower()]
if aud:
    spec = aud[0].get("icon") or ""
    ck("a nested exe is found (Audacity ships bin/Audacity4.exe)",
       spec.lower().endswith(".exe") and os.path.isfile(spec), spec)
    ck("...and it has pixels",
       bool(icon_ppm_bytes(spec, 18, (30, 32, 36))))

lnks = [i for i in items if (i.get("command") or "").lower().endswith(".lnk")]
if lnks:
    got = [i["name"] for i in lnks if lnk_target(i["command"].strip('"'))]
    print("        .lnk items resolved: %d/%d %s"
          % (len(got), len(lnks), got))
    ck("Startup-folder .lnk shortcuts resolve", len(got) >= max(1, len(lnks) // 2),
       got)

# --------------------------------------------------------------------------- #
print("\n[3] both dialogs, live")
import app.gui as gui
from app.elevation import is_admin

root = tk.Tk()
root.withdraw()
app = gui.Application(root)
if not is_admin():
    for w in root.winfo_children():
        if isinstance(w, gui.AdminGateFrame):
            w._continue_limited()
            break

try:
    from app.ui.dialogs.uninstall_programs_dialog import UninstallProgramsDialog
    d = UninstallProgramsDialog(root, app)
    pump(root, d, 900)
    ck("the program list arrived (the cross-thread .after fix)",
       len(d._programs) > 0, len(d._programs))
    ck("every icon spec was delivered (nothing left waiting)",
       sum(len(v) for v in d._icon_waiters.values()) == 0)
    real, glyph = count_icon_labels(d)
    print("        uninstall rows: %d real, %d generic glyph"
          % (real, glyph))
    ck("most uninstall rows show a REAL icon", real >= len(d._programs) // 2,
       "%d/%d" % (real, len(d._programs)))
    ck("no row is left without any icon at all", real + glyph >= len(d._programs),
       "%d real + %d glyph vs %d rows" % (real, glyph, len(d._programs)))
    try:
        d._close()
    except Exception:
        pass
except Exception as exc:
    ck("Uninstall Programs dialog", False, repr(exc))

try:
    from app.ui.dialogs.startup_manager_dialog import StartupManagerDialog
    s = StartupManagerDialog(root, app)
    pump(root, s, 500)
    ck("the startup rows were built", len(s._rows) > 0, len(s._rows))
    real, glyph = count_icon_labels(s)
    print("        startup rows: %d real, %d generic glyph" % (real, glyph))
    ck("most startup rows show a REAL icon (this dialog had none before)",
       real >= max(1, len(s._rows) // 2), "%d/%d" % (real, len(s._rows)))
    ck("every startup row has an icon slot", real + glyph >= len(s._rows),
       "%d real + %d glyph vs %d rows" % (real, glyph, len(s._rows)))
    try:
        s._close()
    except Exception:
        pass
except Exception as exc:
    ck("Startup Manager dialog", False, repr(exc))

try:
    root.destroy()
except Exception:
    pass

print()
if FAILS:
    print("ICON VERIFY: %d/%d FAILED -> %s" % (len(FAILS), N[0], FAILS))
    sys.exit(1)
print("ICON VERIFY: ALL PASS (%d checks)" % N[0])
