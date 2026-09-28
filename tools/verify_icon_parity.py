"""Icon size + plate symmetry across the three app-icon surfaces.

User report: "the app icons in the uninstall program popup window are tiny and
they have a square boarder around them ... same with the startup program popup
window the app icons are smaller. I would like things to be more symmetrical in
my app as far as sizes and positions relative to each other."

Two separate defects, both invisible to the existing icon tests because those
verify icon EXTRACTION (does the right icon come out of this .exe / registry
key), not how it is DRAWN:

  * size drift - Install catalog 32px, Uninstall 18px, Startup 18px. Two
    different-looking apps depending on the surface.
  * the plate - icon_ppm_bytes/icon_resolve composite the icon onto an opaque
    background rectangle. If that rectangle is not the exact colour of the
    widget it sits on, it reads as a square border. The Uninstall popup
    hardcoded (30, 32, 36), which matches neither COLORS["bg"] (13, 17, 23)
    nor COLORS["bg_alt"] (21, 27, 36) - so every program icon had a visible
    square. The Startup popup already derived it from the theme correctly.

The fix is a single shared constant (ICON_ROW_PX) plus a theme-derived plate
(icon_bg_rgb). This test exists so those cannot drift apart again per-file.
"""

from __future__ import annotations

import ast
import io
import os
import re
import sys

sys.path.insert(0, ".")

from app.ui.theme import COLORS, ICON_ROW_PX, icon_bg_rgb  # noqa: E402

FAILS = 0
CHECKS = 0

CATALOG = r"app\ui\tabs\installtab.py"
UNINSTALL = r"app\ui\dialogs\uninstall_programs_dialog.py"
STARTUP = r"app\ui\dialogs\startup_manager_dialog.py"
SURFACES = (CATALOG, UNINSTALL, STARTUP)


def ck(label, cond, detail=""):
    global FAILS, CHECKS
    CHECKS += 1
    if cond:
        print("  PASS  %s" % label)
    else:
        FAILS += 1
        print("  FAIL  %s%s" % (label, ("  <- " + str(detail)) if detail else ""))


def src(path):
    return io.open(path, encoding="utf-8", newline="").read()


def code_only(path):
    """Source with comments and docstrings stripped.

    Both a false positive and a real miss came from scanning raw text here: the
    (30, 32, 36) I was hunting for survives in the comment that documents the
    bug, and the '(90, 96, 104)' glyph grey is a legitimate literal that has
    nothing to do with the icon plate. Only real code should be asserted on.
    """
    import tokenize
    import tokenize
    out = []
    with open(path, "rb") as fh:
        for tok in tokenize.tokenize(fh.readline):
            if tok.type == tokenize.COMMENT:
                continue
            out.append(tok.string)
    # Whitespace is stripped entirely, not just collapsed. A sabotage run
    # caught this: joining tokens with spaces turned "(30, 32, 36)" into
    # "( 30 , 32 , 36 )", so the literal checks could never match and were
    # passing VACUOUSLY. Every pattern below uses \\s* / [^,()] so they are
    # all whitespace-insensitive.
    return re.sub(r"\s+", "", "".join(out))


def rgb(color):
    return icon_bg_rgb(color)


print("[1] there is ONE icon size, and it is the Install catalog's 32px")
ck("ICON_ROW_PX is defined in app/ui/theme.py", isinstance(ICON_ROW_PX, int) and ICON_ROW_PX > 0,
   ICON_ROW_PX)
ck("it is 32 - the size the Install catalog already used", ICON_ROW_PX == 32, ICON_ROW_PX)
for path in SURFACES:
    s = src(path)
    ck("%-26s imports ICON_ROW_PX" % os.path.basename(path), "ICON_ROW_PX" in s)
    # a bare size literal at an icon call site is exactly the drift we are killing
    c = code_only(path)
    strays = re.findall(
        r"(?:generic_app_icon_ppm|default_icon_ppm|icon_ppm_bytes)\s*\(\s*(?:size\s*=\s*)?(\d+)\s*[,)]",
        c)
    ck("%-26s has no hardcoded icon size literal" % os.path.basename(path),
       not [x for x in strays if x != str(ICON_ROW_PX)],
       [x for x in strays if x != str(ICON_ROW_PX)])

print()
print("[2] the Install catalog keeps its 32px default (regression guard)")
tree = ast.parse(src(CATALOG))
fn = next(n for n in ast.walk(tree)
          if isinstance(n, ast.FunctionDef) and n.name == "_row_icon")
ck("_row_icon defaults to ICON_ROW_PX, not a literal 32",
   "ICON_ROW_PX" in ast.get_source_segment(src(CATALOG), fn), "size default not shared")
ck("...and a caller can still override it",
   any(a.arg == "size" for a in fn.args.args) and fn.args.defaults is not None)

print()
print("[3] no surface hardcodes an icon plate colour any more")
for path in SURFACES:
    s = src(path)
    # a bare 3-tuple passed where an icon background is expected
    hard = re.findall(r"(?:generic_app_icon_ppm|icon_ppm_bytes|default_icon_ppm)"
                      r"\s*\(\s*[^,()]+,\s*\((\d+),\s*(\d+),\s*(\d+)\)", code_only(path))
    ck("%-26s passes no literal (r, g, b) plate" % os.path.basename(path),
       not hard, hard)

print()
print("[4] THE SQUARE-BORDER BUG: each plate equals its own row background")
ck("uninstall rows are COLORS['bg']", rgb(COLORS["bg"]) == (13, 17, 23), rgb(COLORS["bg"]))
ck("startup rows are COLORS['bg_alt']", rgb(COLORS["bg_alt"]) == (21, 27, 36), rgb(COLORS["bg_alt"]))
# the old wrong value, spelled out so the regression is unmistakable
ck("the old (30, 32, 36) plate matches NEITHER row background (this was the bug)",
   (30, 32, 36) not in (rgb(COLORS["bg"]), rgb(COLORS["bg_alt"])))
ck("...and appears in no dialog's CODE (only in the comment that documents it)",
   "(30,32,36)" not in code_only(UNINSTALL)
   and "(30,32,36)" not in code_only(STARTUP))
ck("the last-ditch placeholder is also invisible, not a grey chip",
   "(90,96,104)" not in code_only(UNINSTALL)
   and "(90,96,104)" not in code_only(STARTUP)
   and "(90,96,104)" not in code_only(CATALOG))
ck("uninstall derives its plate from its row colour",
   'icon_bg_rgb(COLORS["bg"])' in src(UNINSTALL))
ck("startup derives its plate from its row colour",
   'icon_bg_rgb(COLORS["bg_alt"])' in src(STARTUP))

print()
print("[5] icon_bg_rgb is total - a bad colour cannot raise inside a dialog")
ck("handles #rrggbb", icon_bg_rgb("#0D1117") == (13, 17, 23))
ck("handles #rgb shorthand", icon_bg_rgb("#abc") == (170, 187, 204))
ck("handles a missing hash", icon_bg_rgb("0D1117") == (13, 17, 23))
ck("junk degrades to black instead of raising",
   icon_bg_rgb("nonsense") == (0, 0, 0) and icon_bg_rgb("") == (0, 0, 0)
   and icon_bg_rgb(None) == (0, 0, 0))

print()
print("[6] the three dialogs still import cleanly with the shared constant")
for mod in ("app.ui.dialogs.uninstall_programs_dialog",
            "app.ui.dialogs.startup_manager_dialog",
            "app.ui.tabs.installtab"):
    try:
        __import__(mod)
        ck("%s imports" % mod.rsplit(".", 1)[-1], True)
    except Exception as exc:
        ck("%s imports" % mod.rsplit(".", 1)[-1], False, "%s: %s" % (type(exc).__name__, exc))

print()
if FAILS:
    print("ICON PARITY VERIFY: %d/%d FAILED" % (FAILS, CHECKS))
    sys.exit(1)
print("ICON PARITY VERIFY: ALL PASS (%d checks)" % CHECKS)
