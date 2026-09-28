"""SEC-004 migration: the folder-wide exclusion an older build left behind.

SEC-004 made the Defender exclusion opt-in and narrowed it from the
executable's whole PARENT FOLDER to the exact binary. But the blast radius the
report called out still stands: "Existing installations may already have
exclusions requiring migration."

Concretely - a user who ran a pre-fix build, from Desktop or Downloads, has
that entire FOLDER excluded from Defender right now. That entry is NOT in
`defender_exclusions_applied`, because the record did not exist when it was
added, so `remove_defender_exclusions()` deliberately ignores it. Without
migration it would persist forever, and a portable copy carries it with it.

Two design decisions are load-bearing and are pinned here:

  * It is REPORTED, never removed automatically. The app cannot distinguish an
    exclusion it added from one the user added on purpose, and silently
    deleting a security setting is worse than asking. So
    `remove_path_exclusion()` is separate from `remove_defender_exclusions()`:
    the first requires the user to name the exact path, the second only ever
    touches paths in the record.
  * It is asked ONCE. `defender_migration_notice_shown` is set whether the
    user removed it or declined, because re-asking every launch about a
    security setting they may legitimately want to keep is nagging.
"""

from __future__ import annotations

import ast
import io
import sys

sys.path.insert(0, ".")

from app import elevation as el  # noqa: E402

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


class FakePS:
    """Stand-in for _run_mp_script that records the script and returns a body."""

    def __init__(self, out="", ok=True):
        self.out, self.ok, self.scripts = out, ok, []

    def __call__(self, script):
        self.scripts.append(script)
        return self.ok, self.out


_real_ps = el._run_mp_script
_real_exe = el._current_exe_path
_real_admin = el.is_admin

EXE = r"C:\Users\Me\Desktop\Cleaner Tool\CleanerTool.exe"
PARENT = r"C:\Users\Me\Desktop\Cleaner Tool"


def with_fakes(ps, exe=EXE, admin=True):
    el._run_mp_script = ps
    el._current_exe_path = lambda: exe
    el.is_admin = lambda: admin
    return ps


try:
    print("[1] the detection helpers exist")
    ck("list_defender_exclusions is public", callable(getattr(el, "list_defender_exclusions", None)))
    ck("find_stale_folder_exclusion is public", callable(getattr(el, "find_stale_folder_exclusion", None)))
    ck("remove_path_exclusion is public", callable(getattr(el, "remove_path_exclusion", None)))

    print()
    print("[2] a folder exclusion from an older build IS detected")
    with_fakes(FakePS("C:\\Users\\Me\\Documents\r\n" + PARENT))
    ck("the parent folder is reported", el.find_stale_folder_exclusion() == PARENT,
       el.find_stale_folder_exclusion())

    print()
    print("[3] and nothing is reported when there is nothing to migrate")
    with_fakes(FakePS("C:\\Users\\Me\\Documents"))
    ck("an unrelated exclusion reports nothing", el.find_stale_folder_exclusion() == "",
       el.find_stale_folder_exclusion())
    with_fakes(FakePS(""))
    ck("an empty exclusion list reports nothing", el.find_stale_folder_exclusion() == "")
    with_fakes(FakePS("", ok=False))
    ck("a failed query reports nothing rather than guessing", el.find_stale_folder_exclusion() == "")
    el._run_mp_script = lambda s: (_ for _ in ()).throw(RuntimeError("boom"))
    ck("an exception reports nothing rather than propagating", el.find_stale_folder_exclusion() == "")

    print()
    print("[4] a drive root is NEVER flagged - that would be catastrophic")
    with_fakes(FakePS("C:\\"), exe=r"C:\CleanerTool.exe")
    ck("exe directly in C:\\ does not flag C:\\", el.find_stale_folder_exclusion() == "",
       el.find_stale_folder_exclusion())
    # A broad folder such as C:\Users IS reported. An older build would have
    # excluded it, it is exactly the harm SEC-004 describes, and nothing is
    # removed without the user agreeing - so suppressing it would hide the very
    # case this notice exists for.
    with_fakes(FakePS("C:\\Users"), exe=r"C:\Users\CleanerTool.exe")
    ck("a broad folder like C:\\Users IS reported (it is the SEC-004 harm)",
       el.find_stale_folder_exclusion() == r"C:\Users",
       el.find_stale_folder_exclusion())

    print()
    print("[5] matching is case- and separator-insensitive (a re-spelling is still ours)")
    with_fakes(FakePS(PARENT.upper()))
    ck("upper-case exclusion still matches", el.find_stale_folder_exclusion() == PARENT,
       el.find_stale_folder_exclusion())
    with_fakes(FakePS(PARENT.replace("\\", "/")))
    ck("forward-slash exclusion still matches", el.find_stale_folder_exclusion() == PARENT,
       el.find_stale_folder_exclusion())
    with_fakes(FakePS(PARENT + "\\"))
    ck("a trailing separator still matches", el.find_stale_folder_exclusion() == PARENT,
       el.find_stale_folder_exclusion())

    print()
    print("[6] removal is explicit, and separate from the recorded path")
    ps = with_fakes(FakePS(""), admin=True)
    ok, msg = el.remove_path_exclusion(PARENT)
    ck("removal succeeds", ok is True, (ok, msg))
    ck("...and really called Remove-MpPreference",
       ps.scripts and "Remove-MpPreference" in ps.scripts[0], ps.scripts[:1])
    ck("...for the exact path asked for", PARENT in ps.scripts[0], ps.scripts[:1])
    ps = with_fakes(FakePS(""), admin=False)
    ok, msg = el.remove_path_exclusion(PARENT)
    ck("non-admin cannot remove", ok is False and "administrator" in msg, (ok, msg))
    ck("...and nothing was sent to PowerShell", ps.scripts == [], ps.scripts)
    ps = with_fakes(FakePS("", ok=False))
    ok, msg = el.remove_path_exclusion(PARENT)
    ck("a failed removal is reported, not claimed", ok is False, (ok, msg))
    ok, msg = el.remove_path_exclusion("")
    ck("an empty path is refused", ok is False, (ok, msg))

    print()
    print("[7] the recorded-path remover is NOT widened by any of this")
    _src = io.open(r"app\elevation.py", encoding="utf-8", newline="").read()
    _fn = next(n for n in ast.walk(ast.parse(_src))
               if isinstance(n, ast.FunctionDef) and n.name == "remove_defender_exclusions")
    _body = ast.get_source_segment(_src, _fn) or ""
    # It reads the record through _applied_exclusions(), so assert on the call
    # rather than on the config key appearing literally in this function.
    ck("remove_defender_exclusions still only uses the RECORD",
       "_applied_exclusions()" in _body, _body[:90])
    ck("...it does not call the new explicit remover",
       "remove_path_exclusion" not in _body)
    ck("...and never calls list_defender_exclusions to widen its scope",
       "list_defender_exclusions" not in _body)
    ck("SEC-004 is documented at the migration helper",
       "SEC-004 migration" in _src and "never removed automatically" in _src)

    print()
    print("[8] the notice is wired, one-time, and asks rather than acts")
    _gate = io.open(r"app\ui\tabs\admingateframe.py", encoding="utf-8", newline="").read()
    # STRUCTURAL, not textual. A text check for the name is satisfied by the
    # import alone - a sabotage that rewrites the import to something else
    # leaves the CALL behind and passes. Assert a call node exists.
    _gtree = ast.parse(_gate)
    _calls = [n for n in ast.walk(_gtree)
              if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
              and n.func.id == "find_stale_folder_exclusion"]
    ck("the Admin Gate actually CALLS find_stale_folder_exclusion", len(_calls) == 1,
       len(_calls))
    ck("it names the folder so the user can see what is being removed",
       "excluded this whole" in _gate)
    ck("the user is asked, not told", "Remove that exclusion?" in _gate)
    ck("removal happens only from the button's command",
       "command=_do_remove" in _gate)
    # And the handler itself must actually remove. Checking only that the button
    # is wired to _do_remove passes even if _do_remove stops calling
    # remove_path_exclusion - the button would then silently do nothing.
    _rm = [n for n in ast.walk(_gtree)
           if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
           and n.func.id == "remove_path_exclusion"]
    ck("the handler really calls remove_path_exclusion", len(_rm) == 1, len(_rm))
    # Also structural: the flag is both READ and WRITTEN, and a text check for
    # the name is satisfied by the read alone - so a sabotage that deletes the
    # write would pass. Assert a store to that key exists.
    _reads = [n for n in ast.walk(_gtree)
              if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
              and n.func.attr == "get"
              and n.args and isinstance(n.args[0], ast.Constant)
              and n.args[0].value == "defender_migration_notice_shown"]
    _writes = [n for n in ast.walk(_gtree)
               if isinstance(n, ast.Subscript) and isinstance(n.ctx, ast.Store)
               and isinstance(n.slice, ast.Constant)
               and n.slice.value == "defender_migration_notice_shown"]
    ck("the notice is asked at most once (the flag is READ)", len(_reads) == 1, len(_reads))
    ck("...and it is WRITTEN once shown, so it is never asked twice",
       len(_writes) == 1, len(_writes))
    ck("...and only while the preference is OFF (a current opt-in is not nagged)",
       "and not _def0" in _gate)
    _gate_fn = next(n for n in ast.walk(ast.parse(_gate))
                    if isinstance(n, ast.FunctionDef) and n.name == "__init__")
    _gbody = ast.get_source_segment(_gate, _gate_fn) or ""
    ck("there is no unconditional Remove-MpPreference anywhere in the frame",
       "Remove-MpPreference" not in _gate)

    print()
    print("[9] the one-time flag is a real, validated config key")
    _sch = io.open(r"app\config_schema.py", encoding="utf-8", newline="").read()
    _cfg = io.open(r"app\config_persist.py", encoding="utf-8", newline="").read()
    ck("declared in the schema", '"defender_migration_notice_shown"' in _sch)
    ck("has a default", '"defender_migration_notice_shown": False' in _cfg)
    from app.config_persist import DEFAULT_CONFIG
    ck("the default is a real bool", DEFAULT_CONFIG.get("defender_migration_notice_shown") is False)
    ck("SEC-004 is annotated on the schema key", "SEC-004" in _sch)

finally:
    el._run_mp_script, el._current_exe_path, el.is_admin = _real_ps, _real_exe, _real_admin

print()
if FAILS:
    print("SEC-004 MIGRATION VERIFY: %d/%d FAILED -> %s" % (FAILS, CHECKS, FAILS))
    sys.exit(1)
print("SEC-004 MIGRATION VERIFY: ALL PASS (%d checks)" % CHECKS)
