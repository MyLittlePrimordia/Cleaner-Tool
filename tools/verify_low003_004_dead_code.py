"""LOW-003 and LOW-004: dead code, and the drift risk between two safety rules.

LOW-003  `repair_tasks._repair_desktop_dir` had ZERO call sites. Its
         `strict=True` known-folder resolution - the one that rejects registry
         values carrying cmd metacharacters, because Desktop feeds shell
         strings (netsh/dism) while elevated - was therefore never exercised.
         Its live sibling `_repair_backups_dir` is inconsistent about applying
         the same filter. Dead code that documents a security property nobody
         runs is worse than no code: it reads as though the property is
         enforced. Deleted, and the repo is now checked for callers.

LOW-004  `app/infra/fs_safe.py` is correct but entirely unused, so there are
         three implementations of "is this path safe to delete": the primitives
         in app.utils, the composed helper in fs_safe, and inline checks in the
         cleaners.

         The decision here is NOT to rewire the live deletion path. utils holds
         the primitives because 30+ modules import them from there, and
         fs_safe imports FROM utils, so a module-level import back would be a
         cycle; and `clean_folder_contents` is the BUG-014 protected-root
         guard, which is one of the most heavily tested paths in the app.
         Rewiring a deletion guard late, to fix a maintainability concern,
         is a bad trade.

         So the drift risk is converted into a caught regression instead: this
         test pins the two implementations to AGREE over a matrix of real paths,
         and pins the live guard to actually refuse the dangerous ones. If a
         future change edits one and not the other, this fails.
"""

from __future__ import annotations

import ast
import io
import os
import sys

sys.path.insert(0, ".")
sys.path.insert(0, os.path.join("tools", ""))

from app import utils as u                        # noqa: E402
from app.infra import fs_safe                     # noqa: E402
from app.tasks import repair_tasks as rt          # noqa: E402

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


print("[1] LOW-003: the dead Desktop resolver is gone, nothing referenced it")
rsrc = io.open(r"app\tasks\repair_tasks.py", encoding="utf-8", newline="").read()
ck("_repair_desktop_dir no longer exists", "_repair_desktop_dir" not in rsrc)
rtree = ast.parse(rsrc)
defined = {n.name for n in rtree.body if isinstance(n, ast.FunctionDef)}
called = set()
for n in ast.walk(rtree):
    if isinstance(n, ast.Call) and isinstance(n.func, ast.Name):
        called.add(n.func.id)
# no other module referenced it either. This file is excluded: it necessarily
# NAMES the function in order to assert it is gone, so counting itself made
# the check unsatisfiable.
refs = []
for root, dirs, files in os.walk("."):
    dirs[:] = [d for d in dirs
               if d not in (".git", "__pycache__", "references", "shots",
                            "screenshots")]
    for f in sorted(files):
        if not f.endswith(".py") or f == "repair_tasks.py":
            continue
        if f == os.path.basename(__file__):
            continue
        p = os.path.join(root, f)
        if "_repair_desktop_dir" in io.open(p, encoding="utf-8",
                                            errors="replace").read():
            refs.append(p)
ck("nothing anywhere referenced it", not refs, refs)
ck("the live sibling still exists and is still called",
   "_repair_backups_dir" in defined and "_repair_backups_dir" in called)
ck("...and it still applies the strict metacharacter filter",
   "strict=True" in rsrc)

# every module-level private function in repair_tasks should be reachable
unreachable = sorted(d for d in defined
                     if d.startswith("_repair_") and d not in called)
ck("no other _repair_* helper is dead", not unreachable, unreachable)


print()
print("[2] LOW-004: fs_safe and utils agree about what is safe to delete")
# The matrix is chosen so every row exercises a DIFFERENT rule:
#   a protected root, a bare drive root, a normal existing dir, a missing
#   path, and (where present) a reparse point.
prof = os.environ.get("USERPROFILE", "")
windir = os.environ.get("WINDIR", r"C:\Windows")
MATRIX = [
    windir,
    os.path.join(windir, "System32"),
    prof,
    os.path.join(prof, "AppData", "Local"),
    "C:\\",
    os.path.join(prof, "Desktop", "somedir"),
    os.path.join(prof, "does_not_exist_zz"),
]
disagree = []
for path in MATRIX:
    try:
        protected = u._is_protected_root(path)
    except Exception as exc:
        protected = "raised: %r" % (exc,)
    try:
        safe = fs_safe.is_safe_to_delete(path)
    except Exception as exc:
        safe = "raised: %r" % (exc,)
    # is_safe_to_delete == (exists AND not protected AND not reparse)
    exists = os.path.exists(path)
    expected = bool(exists) and not bool(protected) \
        and not u._is_reparse_point(path)
    if bool(safe) != expected:
        disagree.append((path, protected, safe, expected))
ck("the composed helper matches its own documented rule on every path",
   not disagree, disagree)
ck("at least one protected root is recognised (the rule is doing something)",
   any(u._is_protected_root(p) for p in MATRIX if p))
ck("a bare drive root is protected",
   u._is_protected_root("C:\\") is True)
# A TEMP subdirectory, not AppData\Local: the latter really IS in the
# protected list, so asserting otherwise was my wrong expectation, not a bug.
import tempfile  # noqa: E402
_plain = os.path.join(tempfile.gettempdir(), "low004_plain_dir")
ck("an ordinary cache-style directory is NOT protected",
   u._is_protected_root(_plain) is False, _plain)
ck("...and is therefore deletable by the composed rule (it just does not exist)",
   fs_safe.is_safe_to_delete(_plain) is False)
ck("a missing path is 'safe to delete' only in the sense that it is absent",
   fs_safe.is_safe_to_delete(os.path.join(prof, "nope_zz")) is False)

print()
print("[3] LOW-004: the live BUG-014 guard is still the one in the hot path")
usrc = io.open(r"app\utils.py", encoding="utf-8", newline="").read()
unode = next(n for n in ast.walk(ast.parse(usrc))
             if isinstance(n, ast.FunctionDef)
             and n.name == "clean_folder_contents")
cbody = usrc.splitlines()[unode.lineno - 1:unode.end_lineno]
ck("clean_folder_contents still consults _is_protected_root",
   any("_is_protected_root(" in l for l in cbody))
ck("...and still consults _is_reparse_point per entry",
   sum(1 for l in cbody if "_is_reparse_point(" in l) >= 3,
   sum(1 for l in cbody if "_is_reparse_point(" in l))
ck("...and still refuses a protected root unconditionally, before the walk",
   cbody.index(next(l for l in cbody if "_is_protected_root(" in l))
   < cbody.index(next(l for l in cbody if "os.walk(" in l or "os.scandir(" in l)))
ck("utils does NOT import fs_safe at module level (that would be a cycle)",
   "from app.infra.fs_safe" not in usrc and "import fs_safe" not in usrc)
fsrc = io.open(r"app\infra\fs_safe.py", encoding="utf-8", newline="").read()
ck("fs_safe imports the primitives FROM utils (the documented direction)",
   "from app.utils import" in fsrc)

print()
print("[4] the deletion guard actually refuses a protected root")
import tkinter  # noqa: E402

FAKE = []


class Ctx:
    def __init__(self):
        self.lines = []

    def log(self, m, *a, **k):
        self.lines.append(str(m))

    def set_status(self, *a, **k):
        pass

    def cancelled(self):
        return False


# dry_run=True so this asserts the DECISION, not the deletion
ctx = Ctx()
u.dry_run = True
try:
    import app.utils as _u
    real_dry = _u.DRY_RUN if hasattr(_u, "DRY_RUN") else None
except Exception:
    real_dry = None
# use the documented dry_run path via TaskContext if available
try:
    from app.utils import TaskContext
    dctx = TaskContext(log=ctx.log, set_status=ctx.set_status,
                       cancelled=lambda: False, dry_run=True)
    freed = u.clean_folder_contents(dctx, windir)
    ck("cleaning %s frees nothing and is refused" % windir, freed == 0, freed)
    ck("...and says why",
       any("protected" in l.lower() for l in ctx.lines), ctx.lines)
except Exception as exc:
    ck("could not drive clean_folder_contents in dry-run", False, repr(exc))

print()
if FAILS:
    print("LOW-003/004 VERIFY: %d/%d FAILED" % (FAILS, CHECKS))
    sys.exit(1)
print("LOW-003/004 VERIFY: ALL PASS (%d checks)" % CHECKS)
