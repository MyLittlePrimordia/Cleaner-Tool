"""CI guard: PowerShell argv is built in ONE place.

R-008 collapsed twenty hand-built `["powershell", "-NoProfile", ...]` copies
into `app/powershell.py`. Copies are how the original problem happened: most
said a bare `"powershell"`, which is not resolvable on a machine where
PowerShell is not on PATH, and three pasted the script into a shell string,
which is where the nested-quote fragility came from.

A guard that only greps for the exact list literal would miss the next variant
(`resolve_exe("powershell")` in a new module, or a `"powershell -Command ..."`
string), so this walks the AST of every module under `app/` and flags any
STRING CONSTANT that is

    * exactly "powershell" / "powershell.exe" (an argv word), or
    * starting with "powershell -<flag>" (a pasted shell command), or
    * containing "powershell -noprofile" / "powershell.exe -noprofile"
      (an invocation embedded mid-string),

outside the two modules that own this on purpose:

    app/powershell.py   builds every argv
    app/exe_paths.py    maps the exe name to its absolute path

Two more things are not call sites and are not flagged: docstrings (several
explain the cycle or quote the OLD broken form), and a string passed straight
to `resolve_exe(...)`, which is the sanctioned way to ask for the exe path.

A genuinely different call may stay with an explicit, in-place exception:

    ps_exe = "powershell.exe"   # ps-ok: <why this one is different>

Exceptions are printed by every run, so they are visible in CI rather than
silently accumulated.

Usage:
    python dev/tools/verify_powershell_single_call_site.py        # exit 1 on drift
    python dev/tools/verify_powershell_single_call_site.py -v     # list every hit
"""
from __future__ import annotations

import ast
import io
import sys
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
APP = ROOT / "app"

#: Modules allowed to name the exe, each for a stated reason. Anywhere else,
#: the name belongs behind `ps_argv` / `run_ps` / `resolve_exe`.
ALLOWED = {
    "powershell.py": "builds every PowerShell argv",
    "exe_paths.py": "maps the exe name to its absolute path",
}

#: In-place exception marker, e.g. `ps_exe = "powershell.exe"  # ps-ok: <why>`.
MARKER = "ps-ok:"

_EXE_WORDS = {"powershell", "powershell.exe"}
_EMBEDDED = ("powershell -noprofile", "powershell.exe -noprofile",
             "powershell -executionpolicy", "powershell.exe -executionpolicy")


@dataclass
class Hit:
    path: str
    line: int
    why: str
    value: str
    excepted: str = ""          # the reason from a `ps-ok:` marker, or ""


def _docstring_nodes(tree: ast.AST) -> set:
    """id() of every docstring Constant, so prose is never read as code."""
    out = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef,
                             ast.AsyncFunctionDef)):
            body = getattr(node, "body", None) or []
            if body and isinstance(body[0], ast.Expr) \
                    and isinstance(body[0].value, ast.Constant) \
                    and isinstance(body[0].value.value, str):
                out.add(id(body[0].value))
    return out


def _offends(value: str) -> str:
    """Why this string looks like a hand-built PowerShell invocation, or ""."""
    low = value.strip().lower()
    if low in _EXE_WORDS:
        return "the PowerShell exe as an argv word"
    # "powershell -<flag>" is a command; "PowerShell is not available." is a
    # message. Requiring the dash keeps user-facing copy out of the findings.
    if low.startswith("powershell -") or low.startswith("powershell.exe -"):
        return "a pasted PowerShell command string"
    for needle in _EMBEDDED:
        if needle in low:
            return "an embedded PowerShell invocation"
    return ""


def _walk_with_parents(node):
    for child in ast.iter_child_nodes(node):
        yield child, node
        yield from _walk_with_parents(child)


def _passed_to_resolver(node, parent) -> bool:
    """True when this exact string is the argument of `resolve_exe(...)`.

    That is the sanctioned way to ask for the exe path (e.g. ShellExecuteW,
    which needs a target and not an argv), and it is the resolver that owns
    the name -> path mapping - so it is not a hand-built call site.
    """
    return (isinstance(parent, ast.Call) and bool(parent.args)
            and parent.args[0] is node
            and isinstance(parent.func, (ast.Name, ast.Attribute))
            and (parent.func.id if isinstance(parent.func, ast.Name)
                 else parent.func.attr) == "resolve_exe")


def _marker(lines: list, lineno: int) -> str:
    """The reason from a `# ps-ok: ...` comment on this source line, or ""."""
    if 1 <= lineno <= len(lines):
        text = lines[lineno - 1]
        at = text.find("#")
        if at >= 0 and MARKER in text[at:]:
            return text[text.index(MARKER) + len(MARKER):].strip()
    return ""


def _display_path(path: Path) -> str:
    """Repo-relative when it can be, absolute otherwise (tests pass temp dirs)."""
    try:
        return path.relative_to(ROOT).as_posix()
    except ValueError:
        return path.as_posix()


def scan_all(app_dir: Path = APP) -> tuple:
    """(findings, exceptions) - both lists of Hit; exceptions carry a reason."""
    findings, exceptions = [], []
    for path in sorted(app_dir.rglob("*.py")):
        if path.name in ALLOWED:
            continue
        try:
            source = io.open(path, encoding="utf-8").read()
            tree = ast.parse(source, filename=str(path))
        except (OSError, SyntaxError) as exc:      # a broken file is ruff's job
            print("  ! could not parse %s: %s" % (path, exc))
            continue
        lines = source.splitlines()
        docstrings = _docstring_nodes(tree)
        for node, parent in _walk_with_parents(tree):
            if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
                continue
            if id(node) in docstrings or _passed_to_resolver(node, parent):
                continue
            why = _offends(node.value)
            if not why:
                continue
            lineno = getattr(node, "lineno", 0)
            hit = Hit(_display_path(path), lineno, why, node.value[:80])
            reason = _marker(lines, lineno)
            if reason:
                hit.excepted = reason
                exceptions.append(hit)
            else:
                findings.append(hit)
    return findings, exceptions


def scan(app_dir: Path = APP) -> list:
    """Just the unexplained offenders - what fails the build."""
    return scan_all(app_dir)[0]


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    verbose = "-v" in argv or "--verbose" in argv

    findings, exceptions = scan_all(APP)
    modules = len(list(APP.rglob("*.py")))
    print("PowerShell call-site check")
    print("  modules scanned          : %d" % modules)
    print("  allowed to name the exe  : %s" % ", ".join(
        "%s (%s)" % (name, why) for name, why in sorted(ALLOWED.items())))
    print("  offending string literals: %d" % len(findings))
    print("  documented exceptions    : %d" % len(exceptions))

    for hit in exceptions:
        print("    - %s:%d  %s  [%s]" % (hit.path, hit.line, hit.why,
                                         hit.excepted or "no reason given"))
    for hit in findings:
        print("  %s:%d  %s: %r" % (hit.path, hit.line, hit.why, hit.value))

    if findings:
        print("\nFAIL: PowerShell argv belongs in app/powershell.py - import "
              "ps_argv (or run_ps from app.tasks.ps) instead of building it. "
              "If one call genuinely cannot, mark the line with `# ps-ok: "
              "<reason>`.")
        return 1
    if verbose:
        print("\nOK: the exe and its flags are named in exactly one module.")
    else:
        print("\nOK: no hand-built PowerShell argv outside app/powershell.py.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
