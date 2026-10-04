"""SEC-001: config.json must never reach a command interpreter.

Run:  python -u tools/verify_sec001_command_injection.py
Exit 0 = clean, 1 = at least one failure.

Why this exists. Scheduled-task prior states are recorded in config.json
keyed by task name, and config.json lives in the user's profile. Two Undo
paths used to interpolate that name into a shell string:

    revert_stop_telemetry    f'schtasks /change /tn "{_task}" /{_want}'
    _set_task_enabled        'powershell -NoProfile -Command "%s-ScheduledTask
                              -TaskName \'%s\' ..."'  (nested quotes, unescaped)

Both ran elevated. A task name of `x" & calc.exe & rem "` in config.json was
arbitrary command execution at Undo time. The sibling `sc` call had already
been converted to argv + shell=False with the reasoning written down; these
two were missed.

This test asserts the property directly, by substituting a spy for run_cmd
and confirming that no shell metacharacter can escape the argv vector. It
does not execute anything.
"""

from __future__ import annotations

import io
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

FAILS = []


def ck(cond, label, detail=""):
    if cond:
        print("  PASS  %s" % label)
    else:
        FAILS.append(label)
        print("  FAIL  %s%s" % (label, ("  <- " + str(detail)) if detail else ""))


print("SEC-001 COMMAND INJECTION VERIFY")

from app.tasks import tweak_tasks as tt  # noqa: E402
from app.utils import TaskContext  # noqa: E402

# Names a real Windows scheduled task can carry. These MUST keep working.
REAL_NAMES = [
    "\\Microsoft\\Windows\\Application Experience\\Microsoft Compatibility Appraiser",
    "\\Microsoft\\Windows\\Customer Experience Improvement Program\\Consolidator",
    "\\Microsoft\\Windows\\DiskDiagnostic\\Microsoft-Windows-DiskDiagnosticDataCollector",
    "NvTmRep_C_1a2b3c4d",
    "NvTmMon_L",
    "\\OEM\\Vendor Task 1.2.3",
]
# Names that are attempts to break out. None is a real task.
INJECTIONS = [
    'x" & calc.exe & rem "',
    "x' ; calc.exe ; '",
    "x`calc.exe`",
    "x$(calc.exe)",
    "x|c calc.exe",
    "x&calc.exe",
    "x;calc.exe",
    "x\ncalc.exe",
    "x\rc calc.exe",
    'x" | powershell -enc SQBFAFgA',
    "x\u0000calc.exe",
    "../../evil",
    "x" * 400,
    " leading-space",
    "trailing-space ",
    "",
]

print("\n[1] _valid_scheduled_task_name accepts real task names")
for n in REAL_NAMES:
    ck(tt._valid_scheduled_task_name(n), "accepts %r" % (n[:46],))

print("\n[2] _valid_scheduled_task_name rejects injection attempts")
for n in INJECTIONS:
    ck(not tt._valid_scheduled_task_name(n),
       "rejects %r" % (n[:40],), "it was ACCEPTED")

print("\n[3] rejects non-strings too (a config.json key is always str, but "
      "the restore path is reachable from other dicts)")
for n in (None, 5, [], {}, b"x"):
    ck(not tt._valid_scheduled_task_name(n), "rejects %r" % (n,))


# ------------------------------------------------------- argv, not a shell ---
print("\n[4] _set_task_enabled never hands the name to a shell")
calls = []


def spy_run_cmd(ctx, cmd, shell=True, timeout=None, **kw):
    calls.append({"cmd": cmd, "shell": shell})
    return 0


real_run_cmd = tt.run_cmd
real_get = tt.get_tweak_snapshot
real_clear = tt.clear_tweak_snapshot
real_merge = tt._merge_tweak_snapshot


def _ctx():
    return TaskContext(log=lambda m: None, set_status=lambda m: None,
                       cancelled=lambda: False)


try:
    tt.run_cmd = spy_run_cmd

    calls.clear()
    tt._set_task_enabled(_ctx(), REAL_NAMES[0], False)
    ck(bool(calls), "_set_task_enabled invoked run_cmd", "it never ran")
    if calls:
        c = calls[-1]
        ck(c["shell"] is False,
           "_set_task_enabled passes shell=False (cmd.exe never parses it)",
           "shell=%r" % (c["shell"],))
        ck(isinstance(c["cmd"], list),
           "_set_task_enabled passes an argv LIST, not a string",
           type(c["cmd"]).__name__)
        if isinstance(c["cmd"], list):
            # Get-ScheduledTask has no argv form for -TaskName, so the name
            # has to live INSIDE the PowerShell script. That is safe only
            # because the whole script is a single argv element and the shell
            # is off: Windows hands it to powershell.exe verbatim, and
            # PowerShell treats single-quoted content literally.
            carriers = [x for x in c["cmd"] if REAL_NAMES[0] in str(x)]
            ck(len(carriers) == 1,
               "the task name appears in exactly ONE argv element",
               "found %d" % len(carriers))
            ck(carriers and str(carriers[0]).count("'") >= 2,
               "the name is single-quoted inside the script",
               carriers[0] if carriers else None)

    # The critical property: an injection name must not be executed. Either
    # it is refused outright, or it is confined to a single argv element.
    print("\n[5] an injection payload can never become a second command")
    for payload in INJECTIONS:
        calls.clear()
        tt._set_task_enabled(_ctx(), payload, False)
        if not calls:
            continue                       # refused before spawning anything
        c = calls[-1]
        ck(c["shell"] is False and isinstance(c["cmd"], list),
           "%r cannot reach a shell" % (payload[:24],),
           "shell=%r type=%s" % (c["shell"], type(c["cmd"]).__name__))
finally:
    tt.run_cmd = real_run_cmd
    tt.get_tweak_snapshot = real_get
    tt.clear_tweak_snapshot = real_clear
    tt._merge_tweak_snapshot = real_merge


print("\n[6] no computed command reaches a shell")
# Checked against the AST, not the text: the old vulnerable forms are still
# quoted verbatim in docstrings that document the bug, and a text match would
# flag its own documentation.
#
# The rule is not "never interpolate" -- run_cmd's FIRST argument may be built
# by a helper, and `shell=False` makes argv safe no matter how it was
# assembled. The rule that matters is: a command that is not a plain literal
# must carry an explicit shell=False, because run_cmd DEFAULTS to shell=True.
# An interpolated string with no shell=False is exactly the SEC-001 bug.
import ast  # noqa: E402

try:
    tree = ast.parse(io.open(ROOT / "app" / "tasks" / "tweak_tasks.py",
                             encoding="utf-8").read())
    offenders = []
    argv_calls = 0
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "run_cmd"):
            continue
        cmd = node.args[1] if len(node.args) > 1 else None
        shell_false = any(
            kw.arg == "shell" and isinstance(kw.value, ast.Constant)
            and kw.value.value is False
            for kw in node.keywords)
        if isinstance(cmd, (ast.List, ast.Tuple)):
            argv_calls += 1
            if not shell_false:
                offenders.append("L%d argv list without shell=False"
                                 % node.lineno)
            continue
        if isinstance(cmd, ast.Constant) and isinstance(cmd.value, str):
            continue                      # a literal command: nothing to inject
        if shell_false:
            argv_calls += 1
            continue                      # computed but never shell-parsed
        offenders.append("L%d computed command reaches a shell (%s)"
                         % (node.lineno, type(cmd).__name__))
    ck(not offenders,
       "every computed/interpolated command carries an explicit shell=False "
       "(%d argv call sites)" % argv_calls, offenders)
except Exception as exc:                # noqa: BLE001
    ck(False, "tweak_tasks.py parses", repr(exc))

print()
if FAILS:
    print("SEC-001 COMMAND INJECTION VERIFY: %d FAILURE(S)" % len(FAILS))
    sys.exit(1)
print("SEC-001 COMMAND INJECTION VERIFY: ALL PASS (%d real names, "
      "%d payloads)" % (len(REAL_NAMES), len(INJECTIONS)))
sys.exit(0)