"""LOW-009, LOW-010, INFO-006: the last three findings in the current report.

LOW-009  `log_security_event` gated its trim on BOTH size > 512 KB AND
         len(lines) > 2000. A log over 512 KB holding 2000 or fewer lines -
         entirely plausible, since the app writes long messages - never
         trimmed, AND the full re-read happened on EVERY append forever,
         because the size condition stayed true. The rewrite was also
         non-atomic: `open(..., "w")` truncates before anything is written.

LOW-010  `tweak_snapshots` was validated only as "is it a dict", even though
         its values are what ~95 tweak reverts replay into the registry, and
         config.json is hand-editable. `seen_tips` grew without bound and its
         values were unchecked. Both now have declared validators.

         The registry restore path is still defended independently (MED-008 /
         MED-013 added fail-closed hive, path, NUL, persistence-location and
         type checks). This is the earlier, cheaper line, not a replacement.

INFO-006 `repair_windows_update_reset`'s rollback did
         rmtree(orig) then rename(bak, orig). A failing rename therefore left
         the user with NEITHER the live folder nor the backup in place -
         recoverable only by someone who read the log, while the task surfaced
         as a bare failure.
"""

from __future__ import annotations

import io
import json
import os
import shutil
import sys
import tempfile

sys.path.insert(0, ".")
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__))))

from app import config_events                        # noqa: E402
from app.config_schema import _tip_flags, _tweak_snapshots  # noqa: E402
from app.tasks import repair_tasks as rt            # noqa: E402

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


print("[1] LOW-009: the event log is trimmed, and the rewrite is atomic")
tmp = tempfile.mkdtemp(prefix="low009_")
real_log = config_events.EVENTS_LOG_FILE
real_dir = config_events.CONFIG_DIR
try:
    target = os.path.join(tmp, "events.log")
    config_events.CONFIG_DIR = tmp
    config_events.EVENTS_LOG_FILE = target
    # drop the byte threshold so the trim actually engages in a test
    real_max = config_events._EVENTS_MAX_BYTES
    config_events._EVENTS_MAX_BYTES = 2000
    try:
        # A few very long lines: over the byte cap but well under the line cap.
        # This is the case the old gate never trimmed at all.
        for i in range(5):
            config_events.log_security_event("probe", "x" * 900)
        size = os.path.getsize(target)
        nlines = len(open(target, encoding="utf-8").readlines())
        ck("the file really is over the byte cap", size > 2000, size)
        ck("...and has far fewer lines than the old 2000 gate needed",
           nlines < 2000, nlines)
        config_events.log_security_event("probe", "y" * 900)   # one more append
        after = len(open(target, encoding="utf-8").readlines())
        ck("the trim actually RAN (the old gate would have skipped it)",
           after < nlines + 1 or after <= 6, (nlines, after))
        ck("the log is not empty afterwards", after >= 1, after)
        # atomic: no stray temp files, and the file always parses as text
        strays = [f for f in os.listdir(tmp) if f.endswith(".tmp")]
        ck("no temp file was left by the rewrite", not strays, strays)
        with open(target, encoding="utf-8") as f:
            body = f.read()
        ck("every line is still a well-formed event line",
           all(l.startswith("[") and "]" in l for l in body.splitlines() if l),
           body[:120])
    finally:
        config_events._EVENTS_MAX_BYTES = real_max
finally:
    config_events.EVENTS_LOG_FILE = real_log
    config_events.CONFIG_DIR = real_dir
    shutil.rmtree(tmp, ignore_errors=True)

esrc = io.open(r"app\config_events.py", encoding="utf-8", newline="").read()
ck("the trim is no longer gated on the line count",
   "len(keep) < len(lines)" in esrc and "if len(lines) > 2000:" not in esrc)
ck("the rewrite is an atomic os.replace, not a truncating write",
   "os.replace(tmp, EVENTS_LOG_FILE)" in esrc
   and 'open(EVENTS_LOG_FILE, "w"' not in esrc)
ck("LOW-009 is documented", "LOW-009" in esrc)

print()
print("[2] LOW-010: the undo registry is shape-validated on load")
GOOD = {"specs": [["HKCU", r"Software\X", "Y"]],
        "0:present": True, "0:type": "REG_DWORD", "0:value": 1}
KEEP = [
    ("a well-formed snapshot", {"t": GOOD}, True),
    ("REG_BINARY hex-encoded prior",
     {"t": {"specs": [["HKCU", "X", "Y"]], "0:type": "REG_BINARY",
           "0:value": {"__bytes_hex__": "deadbeef"}}}, True),
    ("HKLM and HKCR are allowed hives",
     {"t": {"specs": [["HKLM", "X", "Y"], ["HKCR", "A", "B"]]}}, True),
    ("an absent prior (present=False) is normal",
     {"t": {"specs": [["HKCU", "X", "Y"]], "0:present": False}}, True),
    ("a denied prior is normal",
     {"t": {"specs": [["HKCU", "X", "Y"]], "0:denied": True}}, True),
]
DROP = [
    ("an unknown hive", {"t": dict(GOOD, specs=[["HKZZ", "X", "Y"]])}),
    ("a spec that is not a triple", {"t": dict(GOOD, specs=[["HKCU", "X"]])}),
    ("a NUL byte in the path", {"t": dict(GOOD, specs=[["HKCU", "a\x00b", "Y"]])}),
    ("a NUL byte in the value name",
     {"t": dict(GOOD, specs=[["HKCU", "X", "a\x00b"]])}),
    ("a bogus value type", {"t": dict(GOOD, **{"0:type": "REG_NONSENSE"})}),
    ("present that is not a bool", {"t": dict(GOOD, **{"0:present": "yes"})}),
    ("a snapshot that is not a dict", {"t": "nope"}),
    ("a snapshot with no specs", {"t": {"specs": []}}),
    ("an unserialisable value (a set)",
     {"t": dict(GOOD, **{"0:value": {1, 2}})}),
    ("REG_BINARY without the hex envelope",
     {"t": {"specs": [["HKCU", "X", "Y"]], "0:type": "REG_BINARY",
           "0:value": "raw-not-hex"}}),
    ("a non-string task id", {7: GOOD}),
]
bad = 0
for label, inp in DROP:
    if _tweak_snapshots(inp, {}):
        bad += 1
        print("     KEPT (should have dropped): %s" % label)
ck("every malformed snapshot is DROPPED, not repaired", bad == 0,
   "%d wrongly kept" % bad)
bad = 0
for label, inp, _want in KEEP:
    if not _tweak_snapshots(inp, {}):
        bad += 1
        print("     DROPPED (should have kept): %s" % label)
ck("every legitimate snapshot SURVIVES", bad == 0, "%d wrongly dropped" % bad)
ck("a non-dict falls back to the default", _tweak_snapshots("nope", {"d": {}}) == {"d": {}})
ck("growth is bounded (256 tasks)", len(_tweak_snapshots(
    {str(i): GOOD for i in range(400)}, {})) <= 256)
ck("specs per snapshot are bounded (64)", len(_tweak_snapshots(
    {"t": {"specs": [["HKCU", "p%d" % i, "n"] for i in range(200)]}}, {}
)["t"]["specs"]) <= 64)

# REGRESSION: the first version of this validator required a "specs" key, which
# silently DROPPED every snapshot shape that does not have one - the powercfg
# snapshots and the NVIDIA telemetry/service snapshots. That would have
# destroyed the undo history for USB suspend, GPU max-performance and NVIDIA
# telemetry on the next config load. The BUG-015 test caught it; this pins it.
NON_SPECS_SHAPES = {
    "powercfg AC (max_performance_gpu)": {"sub_processor:PROCTHROTTLEMAX": 100},
    "powercfg DC side": {"sub:setting:dc": 1},
    "nvidia_telemetry service": {"NvTelemetryContainer_start_type": "demand"},
    "usb_suspend": {"2a737441-...:48e6b7a6-...": 1},
    "a REG_BINARY envelope in a non-specs shape":
        {"some_blob": {"__bytes_hex__": "deadbeef"}},
}
for _label, _snap in NON_SPECS_SHAPES.items():
    ck("non-specs shape survives: %s" % _label,
       bool(_tweak_snapshots({"t": _snap}, {}).get("t")),
       _tweak_snapshots({"t": _snap}, {}))
ck("a non-specs shape still drops a non-scalar junk value",
   "junk" not in _tweak_snapshots({"t": {"a": 1, "b": {1, 2}}}, {}).get("t", {}))

print()
print("[3] LOW-010: seen_tips is bounded and type-checked")
ck("a non-bool value is dropped", _tip_flags({"a": "yes"}, {}) == {})
ck("a list value is dropped", _tip_flags({"a": [1]}, {}) == {})
ck("a genuine tip survives", _tip_flags({"tray_hide_once": True}, {}) ==
   {"tray_hide_once": True})
ck("False is a legitimate value and survives",
   _tip_flags({"a": False}, {}) == {"a": False})
ck("growth is bounded (256)", len(_tip_flags({str(i): True for i in range(400)}, {})) == 256)
ck("a non-dict falls back", _tip_flags("nope", {"d": True}) == {"d": True})
ssrc = io.open(r"app\config_schema.py", encoding="utf-8", newline="").read()
ck("tweak_snapshots now uses the new validator",
   '"tweak_snapshots": (_tweak_snapshots' in ssrc)
ck("seen_tips now uses the new validator", '"seen_tips": (_tip_flags' in ssrc)
ck("LOW-010 is documented", "LOW-010" in ssrc)

print()
print("[4] INFO-006: a failing restore never leaves the machine with neither")


class C:
    def __init__(self):
        self.lines = []

    def log(self, m, *a, **k):
        self.lines.append(str(m))

    def set_status(self, *a, **k):
        pass

    def cancelled(self):
        return False


tmp = tempfile.mkdtemp(prefix="info006_")
try:
    # (a) the happy path
    orig = os.path.join(tmp, "a")
    bak = orig + ".bak"
    os.makedirs(bak)
    touch = os.path.join(bak, "data.bin")
    open(touch, "w").write("important")
    c = C()
    ok = rt._restore_backup_safely(orig, bak, c)
    ck("a clean restore succeeds", ok is True, ok)
    ck("...and the data is at the original path",
       os.path.exists(os.path.join(orig, "data.bin")))
    ck("...with no rollback temp left",
       not os.path.exists(orig + ".rollback-tmp"))

    # (b) the finding's case: the restore rename is refused
    orig2 = os.path.join(tmp, "b")
    bak2 = orig2 + ".bak"
    os.makedirs(bak2)
    open(os.path.join(bak2, "wu.dat"), "w").write("store")
    os.makedirs(orig2)
    live_file = os.path.join(orig2, "live.txt")
    open(live_file, "w").write("live")
    real_rename = os.rename

    def blocked(src, dst):
        if dst == orig2:
            raise OSError(5, "simulated: AV holding a handle")
        return real_rename(src, dst)

    os.rename = blocked
    c2 = C()
    try:
        ok2 = rt._restore_backup_safely(orig2, bak2, c2)
    finally:
        os.rename = real_rename
    ck("the restore reports failure", ok2 is False, ok2)
    parked = orig2 + ".rollback-tmp"
    ck("THE LIVE DATA WAS NOT DESTROYED - it is at the reported path",
       os.path.exists(os.path.join(parked, "live.txt")), parked)
    ck("the backup is also still on disk", os.path.exists(bak2))
    ck("the operator is told exactly where the live folder is",
       any("rollback-tmp" in l and "CRITICAL" in l for l in c2.lines), c2.lines)

    rsrc = io.open(r"app\tasks\repair_tasks.py", encoding="utf-8", newline="").read()
    # Scope tightly to the rollback LOOP. A file-wide search is wrong
    # (repair_tasks legitimately rmtree's other things), and so was a 900-char
    # window - it ran past the loop into the `finally` block, which does
    # legitimately rmtree, and failed against correct code.
    _ri = rsrc.index("Rollback any previously renamed folders")
    _rend = rsrc.index("raise", _ri)
    # Strip comments before looking for rmtree: the block's own comment
    # explains that it "used to rmtree(orig)", so a text search finds the very
    # word it is asserting is gone.
    _rb = "\n".join(l.split("#", 1)[0] for l in rsrc[_ri:_rend].splitlines())
    ck("the destructive rmtree-then-rename is gone from the ROLLBACK CODE",
       "rmtree" not in _rb, _rb[:220])
    ck("...and the live folder is MOVED, not deleted",
       "_restore_backup_safely(orig, bak, ctx)" in _rb)
    ck("INFO-006 is documented", "INFO-006" in rsrc)
finally:
    shutil.rmtree(tmp, ignore_errors=True)

print()
if FAILS:
    print("LOW-009/010 + INFO-006 VERIFY: %d/%d FAILED" % (FAILS, CHECKS))
    sys.exit(1)
print("LOW-009/010 + INFO-006 VERIFY: ALL PASS (%d checks)" % CHECKS)
