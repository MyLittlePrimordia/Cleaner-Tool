"""Prior SEC-004: a default-ON, folder-wide Defender exclusion with no way out.

The app offered "Add Windows Defender exclusion" as a PRE-CHECKED box, applied
it on every elevated launch, and excluded not just its own binary but the
binary's ENTIRE PARENT DIRECTORY. For a portable app that is severe: run it
from Desktop, Downloads, or a shared folder and every other file in that
folder stops being scanned by Defender. On top of that there was no removal
path anywhere in the codebase - a user who wanted it gone had to open an
elevated PowerShell and guess at Remove-MpPreference themselves.

Three separate defects, so three separate things to pin:
  1. the default was on (a security-weakening preference should be opt-in);
  2. the scope was the parent directory, not the exact executable;
  3. nothing could be undone.

The subtle one is #3's shape. The fix must remove ONLY what this app added.
The tempting implementation - clear Defender's whole exclusion list - is worse
than the bug, because it silently discards exclusions the user or another tool
set on purpose. So the test asserts the app keeps a record, and that removal
never reaches a path it did not record.
"""

from __future__ import annotations

import inspect
import io
import re
import os
import sys

sys.path.insert(0, ".")

from app import elevation as el  # noqa: E402
from app.config_persist import DEFAULT_CONFIG  # noqa: E402
from tools._config_guard import ConfigGuard  # noqa: E402

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


class FakeRun:
    """Captures the PowerShell script instead of touching Defender."""

    def __init__(self, rc=0):
        self.rc = rc
        self.scripts = []

    def __call__(self, script):
        self.scripts.append(script)
        return self.rc == 0, ""


def with_admin(fn, runner=None):
    """Run fn with is_admin() True and a captured PowerShell runner.

    The runner is ALWAYS replaced, including with a no-op capture, so a test
    can never reach the real PowerShell and touch the machine's Defender.
    """
    real_admin, real_run = el.is_admin, el._run_mp_script
    el.is_admin = lambda: True
    el._run_mp_script = runner if runner is not None else FakeRun()
    try:
        return fn()
    finally:
        el.is_admin, el._run_mp_script = real_admin, real_run


print("[1] the preference is opt-IN, not opt-out")
ck("DEFAULT_CONFIG has it off", DEFAULT_CONFIG.get("add_defender_exclusion") is False,
   DEFAULT_CONFIG.get("add_defender_exclusion"))
ck("the startup gate no longer defaults it on",
   'get("add_defender_exclusion", False)' in inspect.getsource(el.apply_defender_exclusion_if_wanted))
ck("the UI checkbox no longer defaults it on",
   'get("add_defender_exclusion", False)' in
   io.open(r"app\ui\tabs\admingateframe.py", encoding="utf-8", newline="").read())
ck("the checkbox is not labelled '(recommended)'",
   "recommended" not in io.open(r"app\ui\tabs\admingateframe.py",
                               encoding="utf-8", newline="").read())

print()
print("[2] the exclusion covers the EXACT EXECUTABLE, never its folder")


def excluded_paths(script):
    """The path literals actually handed to Add-MpPreference.

    Comparing substrings is useless here: the exe lives INSIDE the folder, so
    'Desktop'/'Downloads' appear in the correct single-path script too. The
    only meaningful question is which paths were passed.
    """
    return re.findall(r"-ExclusionPath '([^']*)'", script or "")


EXE = r"C:\Users\Someone\Desktop\Cleaner Tool\CleanerTool.exe"
FOLDER = os.path.dirname(EXE)
with ConfigGuard():  # a successful add WRITES the record; never to the real config
    fake = FakeRun()
    with_admin(lambda: el.ensure_defender_exclusion([EXE]), fake)
    ck("Add-MpPreference ran", len(fake.scripts) == 1)
    ck("the exact .exe is the ONLY path excluded",
       excluded_paths(fake.scripts[0]) == [EXE], excluded_paths(fake.scripts[0]))
    # the decisive assertion: the folder itself is never passed
    ck("the PARENT DIRECTORY is NOT excluded",
       FOLDER not in excluded_paths(fake.scripts[0]), excluded_paths(fake.scripts[0]))

    # and prove it via the real default branch (no paths=), which is the branch
    # that used to append os.path.dirname(exe)
    real_exe = el._current_exe_path
    fake2 = FakeRun()
    el._current_exe_path = lambda: r"C:\Users\Someone\Downloads\CleanerTool.exe"
    try:
        with_admin(lambda: el.ensure_defender_exclusion(), fake2)
    finally:
        el._current_exe_path = real_exe
    ck("default branch: only the exe, never its folder",
       excluded_paths(fake2.scripts[0]) == [r"C:\Users\Someone\Downloads\CleanerTool.exe"],
       excluded_paths(fake2.scripts[0]))

print()
print("[3] a removal path exists, and it removes ONLY what we recorded")
ck("remove_defender_exclusions is public", callable(getattr(el, "remove_defender_exclusions", None)))
ck("the source really calls Remove-MpPreference", "Remove-MpPreference" in
   inspect.getsource(el.remove_defender_exclusions))

with ConfigGuard():
    el._record_applied_exclusions([r"C:\Apps\CleanerTool.exe"])
    ck("the add is recorded", el._applied_exclusions() == [r"C:\Apps\CleanerTool.exe"],
       el._applied_exclusions())

    # An explicit path this app never added must be refused.
    fake3 = FakeRun()
    ok, msg = with_admin(lambda: el.remove_defender_exclusions([r"C:\Users\Me\Documents\taxes.docx"]), fake3)
    ck("an UNRECORDED path is refused", ok and fake3.scripts == [], (ok, msg, fake3.scripts))
    ck("...and it was not removed", el._applied_exclusions() == [r"C:\Apps\CleanerTool.exe"])

    # The recorded one is removed, and the record is pruned.
    fake4 = FakeRun()
    ok, msg = with_admin(lambda: el.remove_defender_exclusions(), fake4)
    ck("the recorded exclusion is removed", ok and "Remove-MpPreference" in fake4.scripts[0],
       (ok, msg, fake4.scripts[:1]))
    ck("...and the record is pruned afterwards", el._applied_exclusions() == [],
       el._applied_exclusions())

    # Nothing recorded -> no-op, and crucially NO blanket clear.
    fake5 = FakeRun()
    ok, msg = with_admin(lambda: el.remove_defender_exclusions(), fake5)
    ck("with nothing recorded it is a no-op", ok and fake5.scripts == [], (ok, msg))

print()
print("[4] a FAILED removal stays recorded, so the next launch retries it")
with ConfigGuard():
    el._record_applied_exclusions([r"C:\Apps\CleanerTool.exe"])
    fake6 = FakeRun(rc=1)
    ok, msg = with_admin(lambda: el.remove_defender_exclusions(), fake6)
    ck("failure is reported, not hidden", ok is False, (ok, msg))
    ck("the exclusion is STILL recorded after a failure",
       el._applied_exclusions() == [r"C:\Apps\CleanerTool.exe"],
       el._applied_exclusions())

print()
print("[5] turning the setting OFF removes what we added (the self-repair path)")
with ConfigGuard():
    el._record_applied_exclusions([r"C:\Apps\CleanerTool.exe"])

    def _set(pref):
        from app.config_persist import update_config
        def _mut(cfg):
            cfg["add_defender_exclusion"] = pref
        update_config(_mut)

    _set(False)
    fake7 = FakeRun()
    with_admin(el.apply_defender_exclusion_if_wanted, fake7)
    ck("pref OFF + recorded -> Remove-MpPreference runs",
       fake7.scripts and "Remove-MpPreference" in fake7.scripts[0], fake7.scripts[:1])
    ck("...and the record is cleared", el._applied_exclusions() == [], el._applied_exclusions())

    fake8 = FakeRun()
    with_admin(el.apply_defender_exclusion_if_wanted, fake8)
    ck("a second launch does nothing at all", fake8.scripts == [], fake8.scripts)

print()
print("[6] non-admin cannot remove, and quoting is injection-safe")
real_admin = el.is_admin
el.is_admin = lambda: False
try:
    ok, msg = el.remove_defender_exclusions()
    ck("non-admin removal is refused", ok is False and "administrator" in msg, (ok, msg))
finally:
    el.is_admin = real_admin

ck("quotes are single-quoted", el._ps_quote(r"C:\a b") == "'C:\\a b'", el._ps_quote(r"C:\a b"))
ck("embedded quotes are doubled (no breakout)",
   el._ps_quote("a'b") == "'a''b'", el._ps_quote("a'b"))
ck("an injected Remove-MpPreference stays inside the string",
   el._ps_quote("x'; Remove-MpPreference -ExclusionPath 'C:\\") == "'x''; Remove-MpPreference -ExclusionPath ''C:\\'",
   el._ps_quote("x'; Remove-MpPreference -ExclusionPath 'C:\\"))

print()
print("[7] a corrupt record cannot make removal unsafe")
with ConfigGuard():
    from app.config_persist import update_config
    def _mut(cfg):
        cfg["defender_exclusions_applied"] = {"not": "a list"}
    update_config(_mut)
    ck("a non-list record reads as empty", el._applied_exclusions() == [], el._applied_exclusions())
    fake9 = FakeRun()
    ok, msg = with_admin(lambda: el.remove_defender_exclusions(), fake9)
    ck("...and removal is a safe no-op", ok and fake9.scripts == [], (ok, msg))

print()
if FAILS:
    print("PRIOR SEC-004 VERIFY: %d/%d FAILED" % (FAILS, CHECKS))
    sys.exit(1)
print("PRIOR SEC-004 VERIFY: ALL PASS (%d checks)" % CHECKS)
