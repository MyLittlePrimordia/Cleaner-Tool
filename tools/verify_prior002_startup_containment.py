"""Prior SEC-002 (report fix-order item 25, second half): the Startup
Manager's restore trusted a path out of user-writable config.

Re-enabling a disabled Startup-folder shortcut used to do this:

    held_path = rec.get("command", "")      # straight out of config.json
    target = os.path.join(folder, os.path.basename(held_path))
    os.replace(held_path, target)           # ELEVATED

`startup_disabled` lives in the user's config.json. A same-user process edits
one record so `command` points at some other file, the user re-enables the
item while Cleaner Tool is elevated, and an elevated process MOVES that file
into a Startup folder. That is arbitrary same-volume file relocation and,
if the destination is a Startup folder, persistence creation.

The fix reconstructs the restore source from the trusted holding directory
(`_holding_dir()`) plus validated metadata, and never from `rec["command"]`:

  * the expected path is derived, then the record's own command must AGREE
    with it - a mismatch is the tampering signature;
  * containment is enforced on the RESOLVED path, so `..` cannot escape;
  * reparse points are refused;
  * the held filename is recorded at disable time (`held_name`), because the
    display name has no extension - "Spotify.lnk" lists as "Spotify" - and
    guessing the extension from config is what this fix removed.

Legacy records (written before `held_name` existed) still restore: the
filename is recovered by scanning the TRUSTED holding dir for a matching
stem, not by reading config.

The report's verification plan is followed literally: a disposable canary
outside the holding directory must survive untouched, and existing valid
records must keep working.
"""

from __future__ import annotations

import io
import os
import shutil
import sys
import tempfile

sys.path.insert(0, ".")

from app import config_persist as cp  # noqa: E402
from app import startup_manager as sm  # noqa: E402

FAILS = 0
CHECKS = 0


def ck(label, cond, detail=""):
    global FAILS, CHECKS
    CHECKS += 1
    if cond:
        print("  PASS  %s" % label)
    else:
        FAILS += 1
        print("  FAIL  %s%s" % (label, ("  <- " + str(detail)) if detail else ""))


TMP = tempfile.mkdtemp(prefix="sec002_verify_")
HOLDING = os.path.join(TMP, "disabled_startup")
STARTUP = os.path.join(TMP, "startup")
os.makedirs(HOLDING)
os.makedirs(STARTUP)

CANARY = os.path.join(TMP, "ImportantData.txt")
with open(CANARY, "w") as fh:
    fh.write("do not move me")

# Drive the REAL restore path; only the two locations and the record source
# are redirected. Everything under test is production code.
_real_holding, _real_folders = sm._holding_dir, sm._startup_folders
_real_get, _real_remove = cp.get_startup_disabled, cp.remove_startup_disabled
sm._holding_dir = lambda: HOLDING
sm._startup_folders = lambda: {"startup_user": STARTUP}
cp.remove_startup_disabled = lambda *a, **k: None


def restore(rec, item=None):
    cp.get_startup_disabled = lambda r=rec: [r]
    name = item or ("startup_user:%s" % rec.get("name", "?"))
    return sm.set_item_enabled(name, True, log=lambda m: None)


def touch(fname):
    p = os.path.join(HOLDING, fname)
    with open(p, "w") as fh:
        fh.write("shortcut")
    return p


try:
    print("[1] THE ATTACK: a record pointing outside the holding directory")
    ok, msg = restore({"source": "startup_user", "name": "ImportantData",
                       "command": CANARY, "value_type": ""})
    ck("restore is REFUSED", ok is False, (ok, msg))
    ck("the canary is still exactly where it was", os.path.isfile(CANARY))
    ck("nothing was moved into the Startup folder",
       not os.path.exists(os.path.join(STARTUP, "ImportantData.txt")))

    print()
    print("[2] a LEGITIMATE record still restores (no regression)")
    p = touch("Spotify.lnk")
    ok, msg = restore({"source": "startup_user", "name": "Spotify",
                       "command": p, "held_name": "Spotify.lnk",
                       "value_type": ""})
    ck("held_name record restores", ok is True, (ok, msg))
    ck("...and the file is back in the Startup folder",
       os.path.exists(os.path.join(STARTUP, "Spotify.lnk")))

    print()
    print("[3] a LEGACY record (no held_name) still restores")
    p = touch("Steam.lnk")
    ok, msg = restore({"source": "startup_user", "name": "Steam",
                       "command": p, "value_type": ""})
    ck("legacy record restores", ok is True, (ok, msg))
    ck("...recovered the extension from the TRUSTED holding dir",
       os.path.exists(os.path.join(STARTUP, "Steam.lnk")))

    print()
    print("[4] alternate spellings and escapes are refused")
    p = touch("Evil.lnk")
    ok, msg = restore({"source": "startup_user", "name": "Evil",
                       "command": CANARY, "held_name": ".." + os.sep + "ImportantData.txt",
                       "value_type": ""})
    ck("a ..\\ held_name cannot escape", ok is False, (ok, msg))
    ok, msg = restore({"source": "startup_user", "name": "Evil",
                       "command": p, "held_name": "..", "value_type": ""})
    ck("held_name '..' is refused", ok is False, (ok, msg))
    ok, msg = restore({"source": "startup_user", "name": "Evil",
                       "command": os.path.join(HOLDING, "sub", "Evil.lnk"),
                       "held_name": os.path.join("sub", "Evil.lnk"),
                       "value_type": ""})
    ck("a held_name with separators is reduced to its basename", ok is False,
       (ok, msg))

    print()
    print("[5] malformed and missing records fail closed, with honest messages")
    for label, rec in (
        ("command is not a string", {"source": "startup_user", "name": "X",
                                     "command": 12345, "held_name": "X.lnk"}),
        ("command is empty", {"source": "startup_user", "name": "X",
                              "command": "   ", "held_name": "X.lnk"}),
        ("held copy is absent", {"source": "startup_user", "name": "Ghost",
                                 "command": os.path.join(HOLDING, "Ghost.lnk"),
                                 "held_name": "Ghost.lnk"}),
    ):
        ok, msg = restore(rec)
        ck("%-26s -> refused" % label, ok is False, (ok, msg))
        ck("%-26s -> message is specific" % label,
           bool(msg) and "not usable" in msg or "no longer there" in msg, msg)

    print()
    print("[5b] the reparse refusal is actually CONSULTED, not just present")
    # A text check for "_is_reparse_point" is worthless here: a sabotage that
    # empties the guard body leaves the name in the file and passes. Creating a
    # real file symlink needs privileges we may not have, so instead make the
    # predicate report True and assert the refusal is HONOURED - which is
    # exactly what removing the guard body would break.
    p = touch("Linky.lnk")
    real_rp = None
    try:
        import app.utils as _u
        real_rp = _u._is_reparse_point
        _u._is_reparse_point = lambda path: str(path).endswith("Linky.lnk")
        ok, msg = restore({"source": "startup_user", "name": "Linky",
                           "command": p, "held_name": "Linky.lnk",
                           "value_type": ""})
        ck("a reparse point is refused", ok is False, (ok, msg))
        ck("...and says so specifically", "shortcut or link" in msg, msg)
        ck("...and it was NOT moved into Startup",
           not os.path.exists(os.path.join(STARTUP, "Linky.lnk")))
    finally:
        if real_rp is not None:
            _u._is_reparse_point = real_rp

    print()
    print("[6] the disable side records the held filename")
    src = io.open(r"app\startup_manager.py", encoding="utf-8", newline="").read()
    ck("held_name is persisted on disable", '"held_name": os.path.basename(dest)' in src)
    ck("SEC-002 is documented at the restore site", "SEC-002" in src)

    print()
    print("[7] the old trust-the-config pattern is GONE from the source")
    ck("no bare rec.get(\"command\") feeding os.replace",
       'os.replace(held_path, target)' in src and
       'held_path = rec.get("command"' not in src)
    ck("the derived path is cross-checked against the record",
       'if os.path.abspath(recorded) != expected' in src)
    ck("containment is enforced on the resolved path", "commonpath" in src)
    ck("reparse points are refused", "_is_reparse_point" in src)

finally:
    sm._holding_dir, sm._startup_folders = _real_holding, _real_folders
    cp.get_startup_disabled, cp.remove_startup_disabled = _real_get, _real_remove
    shutil.rmtree(TMP, ignore_errors=True)

print()
if FAILS:
    print("PRIOR SEC-002 VERIFY: %d/%d FAILED -> %s" % (FAILS, CHECKS, FAILS))
    sys.exit(1)
print("PRIOR SEC-002 VERIFY: ALL PASS (%d checks)" % CHECKS)
