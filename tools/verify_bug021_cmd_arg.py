"""BUG-021: `cmd_arg` lied about escaping metacharacters. It is now gone.

The bug: `cmd_arg()` was documented as making `&`, `|`, `>`, `%`, `^` and spaces
"travel as data, never as command separators" inside a `shell=True` string. Its
entire body was `subprocess.list2cmdline([str(value)])`, which quotes ONLY for
whitespace, embedded quotes and empty strings. It performs no metacharacter
escaping whatsoever, so a `SYSTEMDRIVE` of `C:^&calc` produced
`chkdsk C:^&calc /scan` executed ELEVATED (chkdsk_scan is admin_required).

The fix deletes the helper entirely and converts the single call site to an argv
list with `shell=False` — no shell, so nothing to parse a metacharacter.

This test does not just grep. It:
  * captures what the fixed code actually hands to Popen, and asserts the
    payload arrives as ONE argv element with shell=False;
  * DEMONSTRATES the old behaviour by running the previous shell string against
    a harmless `echo` builtin, proving it really did split into two commands.

Only `echo` is ever used as a payload. Nothing here can touch the system.
"""

from __future__ import annotations

import os
import subprocess
import sys

sys.path.insert(0, ".")
sys.path.insert(0, os.path.join("tools", ""))

import app.utils as u                       # noqa: E402
import app.tasks.repair_tasks as rt         # noqa: E402

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


# The finding's own verification case.
PAYLOAD = "C:^&whoami"
# A harmless stand-in used for the live demonstration. `echo` is a cmd builtin:
# it prints and does nothing else.
ECHO_PAYLOAD = "C:&echo INJECTED"


class Ctx:
    def __init__(self):
        self.lines = []

    def log(self, m, *a, **k):
        self.lines.append(str(m))

    def set_status(self, *a, **k):
        pass

    def cancelled(self):
        return False


def capture_popen(env_root, command_result):
    """Run repair_chkdsk_scan with Popen intercepted. Returns the captured
    Popen kwargs. Sets rc via command_result."""
    captured = {}
    real_popen = subprocess.Popen

    class FakePopen:
        def __init__(self, cmd, **kw):
            captured["cmd"] = cmd
            captured["shell"] = kw.get("shell")

            def _out(_self, *a, **k):
                return b""

            proc = real_popen.__new__(real_popen)
            proc.stdout = None
            proc.stderr = None
            proc.stdin = None
            proc.args = cmd
            proc.returncode = 0
            self.__class__._real = proc
            command_result.append(0)

        def __getattr__(self, name):
            return getattr(self.__class__._real, name)

    import app.utils as _u
    _u.subprocess.Popen = FakePopen
    old = os.environ.get("SYSTEMDRIVE")
    os.environ["SYSTEMDRIVE"] = env_root
    try:
        ctx = Ctx()
        try:
            rt.repair_chkdsk_scan(ctx)
            raised = None
        except Exception as exc:
            raised = exc
        return captured, ctx, raised
    finally:
        _u.subprocess.Popen = real_popen
        if old is None:
            os.environ.pop("SYSTEMDRIVE", None)
        else:
            os.environ["SYSTEMDRIVE"] = old


print("[1] the helper is GONE, not merely unused")
ck("app.utils has no cmd_attr", not hasattr(u, "cmd_arg"))
src_utils = open("app/utils.py", encoding="utf-8").read()
ck("...and no `def cmd_arg` remains", "def cmd_arg" not in src_utils)
ck("...and repair_tasks does not import it",
   "cmd_arg," not in open("app/tasks/repair_tasks.py", encoding="utf-8").read()
   .split("def ")[0])
ck("the deletion is documented where the helper used to live",
   "BUG-021" in src_utils and "DELETED" in src_utils)

print()
print("[2] the call site uses argv + shell=False")
cp = capture_popen("C:", [])
ck("Popen was reached", "cmd" in cp[0], cp[0])
ck("shell is False (there is no shell to parse metacharacters)",
   cp[0].get("shell") is False, cp[0].get("shell"))
ck("the command is an argv sequence, not a string",
   isinstance(cp[0].get("cmd"), (list, tuple)), type(cp[0].get("cmd")).__name__)
if isinstance(cp[0].get("cmd"), (list, tuple)):
    parts = list(cp[0]["cmd"])
    ck("argv is [chkdsk, <root>, /scan]",
       parts[0].lower() == "chkdsk" and parts[-1] == "/scan", parts)
    ck("the drive letter is exactly ONE argument (not split)",
       len(parts) == 3, parts)

print()
print("[3] THE FINDING'S CASE: SYSTEMDRIVE=%r" % PAYLOAD)
cap, ctx, raised = capture_popen(PAYLOAD, [])
cmd = cap.get("cmd")
ck("Popen was reached", cmd is not None, cap)
ck("shell is still False", cap.get("shell") is False, cap.get("shell"))
if isinstance(cmd, (list, tuple)):
    parts = list(cmd)
    ck("the payload is a SINGLE argv element, not split into commands",
       len(parts) == 3, parts)
    ck("...and it is the drive argument, verbatim",
       parts[1] == PAYLOAD + "\\" or parts[1] == PAYLOAD, parts[1])
    ck("...so 'whoami' is an ARGUMENT and never a command",
       "whoami" not in parts[0] and "whoami" not in parts[2], parts)
    ck("the whole payload stayed inside the drive slot",
       parts[1].startswith("C:") and "^" in parts[1] and "&" in parts[1],
       parts[1])
ck("the log shows the command with the payload intact",
   any("C:^&whoami" in l for l in ctx.lines), ctx.lines[:3])

print()
print("[4] DEMONSTRATION: the old shell string really did split")
# Reproduce exactly what the removed line produced, and show that cmd.exe runs
# it as two commands. `echo` is a builtin that only prints.
old_style = "echo %s /scan" % ECHO_PAYLOAD          # what f"chkdsk {..} /scan" did
res = subprocess.run(old_style, shell=True, capture_output=True, text=True,
                     errors="replace")
out = (res.stdout or "") + (res.stderr or "")
ck("the old shell-string form really does execute the injected command",
   "INJECTED" in out, "output was %r" % out[:120])
print("        old form  : %s" % old_style)
print("        cmd said  : %r" % out.strip()[:100])
# ...whereas the new form keeps it inert. Demonstrate with a REAL executable
# that prints its argv: with shell=False the payload is handed over untouched,
# with nothing to interpret it. (An earlier version of this check used
# ["echo", ...] with shell=False, which fails outright — `echo` is a cmd
# builtin, not an executable, so CreateProcess cannot find it.)
probe = [sys.executable, "-c",
         "import sys; print('ARGC=%d' % len(sys.argv)); "
         "print('ARG1=' + repr(sys.argv[1]))",
         ECHO_PAYLOAD + " /scan"]
res2 = subprocess.run(probe, shell=False, capture_output=True, text=True,
                      errors="replace")
out2 = ((res2.stdout or "") + (res2.stderr or "")).strip()
ck("the new argv form hands the text over as ONE untouched argument",
   # `python -c CODE ARG` puts "-c" at argv[0], so the payload is argv[1] and
   # len(argv) == 2. The first version of this check expected ARGC=1 and failed
   # against correct behaviour.
   "ARGC=2" in out2 and "ARG1=" in out2
   and ECHO_PAYLOAD in out2 and "&" in out2, "output was %r" % out2[:140])
print("        new form  : %s" % probe[2])
print("        python saw: %s" % out2.replace("\n", " | ")[:110])

print()
print("[5] a normal SYSTEMDRIVE still works (no regression)")
cap, ctx, raised = capture_popen("C:", [])
ck("no exception for an ordinary C:", raised is None, raised)
ck("argv is exactly [chkdsk, 'C:\\\\', '/scan']",
   list(cap.get("cmd") or []) == ["chkdsk", "C:\\", "/scan"],
   list(cap.get("cmd") or []))
ck("the command is logged readably, not as a raw list repr",
   any(l.startswith("$ chkdsk") for l in ctx.lines), ctx.lines[:2])

print()
print("[6] run_cmd still accepts a plain string (backward compatible)")
seen = {}
_real_popen = subprocess.Popen


class _StrPopen:
    def __init__(self, cmd, **kw):
        seen["cmd"] = cmd
        seen["shell"] = kw.get("shell")
        p = _real_popen.__new__(_real_popen)
        p.stdout = p.stderr = p.stdin = None
        p.args = cmd
        p.returncode = 0
        self.__class__._real = p

    def __getattr__(self, n):
        return getattr(self.__class__._real, n)


subprocess.Popen = _StrPopen
try:
    rc = u.run_cmd(Ctx(), "echo hello")
    ck("a string command still runs", rc == 0, rc)
    ck("...with shell defaulting to True as before", seen.get("shell") is True,
       seen.get("shell"))
    ck("...and is passed through as a string", seen.get("cmd") == "echo hello",
       seen.get("cmd"))
finally:
    subprocess.Popen = _real_popen

print()
if FAILS:
    print("BUG-021 VERIFY: %d/%d FAILED" % (FAILS, CHECKS))
    sys.exit(1)
print("BUG-021 VERIFY: ALL PASS (%d checks)" % CHECKS)
