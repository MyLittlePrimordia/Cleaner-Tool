"""Every design literal a call site would have to justify, in one report.

A widget that writes `radius=16`, `padx=10`, `icon_target_px(24, …)` or
`font=(F, 9)` has made a design decision twice: once in `app/ui/theme.py` (or
`app/ui/metrics.py`) where the value has a name, and once here, where it does
not. The literal wins silently, and nothing fails when it drifts.

Three guards grew up around that in `dev/tests/test_design_tokens.py` - radius,
spacing, icon - each with its own walk, its own allowlist and its own negative
test, plus a fourth (font tuples) in the same file. Four answers to one
question. This module is the one walk and the one vocabulary:

    python dev/tools/design_literals.py    # the report, grouped by rule
    dev/tests/test_design_literals.py      # the gate: one finding fails the suite

Run it by hand to see the whole list; the gate is what keeps the list empty.

How it decides
--------------
Parsed, never grepped. The 16px radius call sites this exists for were wrapped
across lines, which a line regex reads as clean. Text scanning is used for
exactly one rule (font tuples), because there the shape is a token sequence
rather than an argument.

Each rule is a SHAPE, not a file list:

    radius    `radius=<number>` argument; class-level `RADIUS = <number>`
    spacing   `padx/pady/ipadx/ipady` with a non-zero number (or tuple of them);
              a module/class `*GAP|PAD|INSET|MARGIN|SPACING* = <non-zero>` (a
              `_MS` name is a duration, not a measurement)
    icon      `*ICON*_PX|_SIZE|_W = <int>`; a numeric argument to one of the
              icon builders; `StatusIcon(size=<int>)`
    font      a `(F, 9)` / `(FM, 9)` pair anywhere outside theme.py

Zero is absence, not a measurement, and is allowed everywhere it means that.
The two declaration files are exempt by name: a number there is the definition
of a token, not a bypass of one.

Adding a rule means adding an entry to REMEDY with the token to reach for, so
the report itself tells the reader what to write instead.
"""
from __future__ import annotations

import ast
import io
import pathlib
import sys
import tokenize
from dataclasses import dataclass

ROOT = pathlib.Path(__file__).resolve().parent.parent.parent

#: rule -> what a call site should have used. The report prints this beside the
#: findings, so a failure is an instruction rather than a complaint.
REMEDY = {
    "radius": "pick a SHAPE_*/RADIUS_* role from app.ui.theme",
    "spacing": "pick a SPACE_* rung or a named metric from app.ui.metrics",
    "icon": "pick a rung of theme.ICON_*",
    "font": "pick a role from theme.TYPE",
}

#: Rule -> the tree it governs. The three widget rules are about a call site
#: INSIDE the UI package; the font rule covers every module, because a font
#: tuple anywhere is a font tuple. The scopes are per rule on purpose: widened
#: to all of `app/`, the spacing shape flags domain names that merely look like
#: metrics (`drivers/scan.REQUEST_GAP_S`, `freed.GAP_SHARE`,
#: `watch.FREE_CLEAR_MARGIN`), and a guard that cries about a retry delay is a
#: guard people learn to ignore.
SCOPE = {
    "radius": "app/ui",
    "spacing": "app/ui",
    "icon": "app/ui",
    "font": "app",
}

#: Where a number IS the token: these files declare the scale.
_DECLARATION_FILES = frozenset({"theme.py", "metrics.py"})

_SPACING_KWARGS = frozenset({"padx", "pady", "ipadx", "ipady"})
_SPACING_NAME_PARTS = ("GAP", "PAD", "INSET", "MARGIN", "SPACING")

#: Every numeric icon size is a bypass, ladder value or not: the ladder is the
#: only place a mark's size is allowed to be chosen.
_ICON_CALLS = frozenset({"icon_target_px", "_load_title_icon", "icon_ppm_bytes",
                         "default_icon_ppm", "generic_app_icon_ppm"})

#: Families a hand-written font tuple may name, per the type scale.
_FONT_FAMILIES = frozenset({"F", "FM"})


@dataclass(frozen=True)
class Finding:
    """One literal, and the rule it breaks."""

    rule: str
    path: str        # relative to the scan root, POSIX separators
    line: int
    detail: str      # what was written, e.g. "radius=16"
    source: str      # the offending line, stripped and clipped

    def __str__(self) -> str:
        return "%s:%d  %s" % (self.path, self.line, self.detail)


def _is_number(value) -> bool:
    return (isinstance(value, (int, float))
            and not isinstance(value, bool))


def _is_int(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


# --------------------------------------------------------------------------- #
# The rules, as generators over one parsed module
# --------------------------------------------------------------------------- #

def _radius_findings(tree):
    """`radius=<number>` and `RADIUS = <number>`."""
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            for kw in node.keywords:
                if kw.arg == "radius" and _is_number(
                        getattr(kw.value, "value", None)):
                    yield node.lineno, "radius=%r" % (kw.value.value,)
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if (isinstance(target, ast.Name) and target.id == "RADIUS"
                        and _is_number(getattr(node.value, "value", None))):
                    yield node.lineno, "RADIUS=%r" % (node.value.value,)


def _spacing_findings(tree):
    """Inline padding numbers and spacing constants written as numbers."""
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            for kw in node.keywords:
                if kw.arg not in _SPACING_KWARGS:
                    continue
                values = []
                if _is_number(getattr(kw.value, "value", None)):
                    values = [kw.value.value]
                elif isinstance(kw.value, ast.Tuple):
                    values = [e.value for e in kw.value.elts
                              if _is_number(getattr(e, "value", None))]
                if any(v != 0 for v in values):
                    yield node.lineno, "%s=%s" % (kw.arg, values)
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if not isinstance(target, ast.Name):
                    continue
                if target.id.endswith("_MS"):        # a tempo, not a measurement
                    continue
                if not any(part in target.id for part in _SPACING_NAME_PARTS):
                    continue
                value = getattr(node.value, "value", None)
                if _is_number(value) and value != 0:
                    yield node.lineno, "%s=%s" % (target.id, value)


def _icon_findings(tree):
    """Icon sizes as numbers, at the constants and at the builders."""
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if not (isinstance(target, ast.Name) and "ICON" in target.id
                        and target.id.endswith(("_PX", "_SIZE", "_W"))
                        and _is_int(getattr(node.value, "value", None))):
                    continue
                yield node.lineno, "%s=%s" % (target.id, node.value.value)
        elif isinstance(node, ast.Call):
            name = getattr(node.func, "id", None)
            if name in _ICON_CALLS:
                args = [a for a in node.args if _is_int(getattr(a, "value", None))]
                kws = [kw.value.value for kw in node.keywords
                       if kw.arg in ("size", "target")
                       and _is_int(getattr(kw.value, "value", None))]
                if args or kws:
                    yield node.lineno, "%s(...)" % name
            elif name == "StatusIcon":
                for kw in node.keywords:
                    if kw.arg == "size" and _is_int(
                            getattr(kw.value, "value", None)):
                        yield node.lineno, "StatusIcon(size=%s)" % (
                            kw.value.value,)


def _font_findings(text):
    """A hand-written `(F, 9)` / `(FM, 9)` pair.

    Read as tokens rather than text: a tuple named in a comment or a docstring
    (there are several, explaining the old sizes) is not a call site, while
    `font=(F, 9)`, `or (F, 8)` and `measure(x, (FM, 9))` all are.
    """
    try:
        toks = [t for t in tokenize.generate_tokens(io.StringIO(text).readline)
                if t.type in (tokenize.NAME, tokenize.OP, tokenize.NUMBER)]
    except (tokenize.TokenError, IndentationError, SyntaxError):
        return
    for i, tok in enumerate(toks[:-2]):
        if (tok.type == tokenize.NAME and tok.string in _FONT_FAMILIES
                and toks[i + 1].string == ","
                and toks[i + 2].type == tokenize.NUMBER):
            yield tok.start[0], "(%s, %s)" % (tok.string, toks[i + 2].string)


# --------------------------------------------------------------------------- #
# The walk
# --------------------------------------------------------------------------- #

def _scan_file(rel, name, text):
    """Every finding in one module, in source order."""
    if name in _DECLARATION_FILES:
        return []
    lines = text.splitlines()

    def finding(rule, lineno, detail):
        source = lines[lineno - 1].strip() if 0 < lineno <= len(lines) else ""
        return Finding(rule, rel, lineno, detail, source[:100])

    out = []
    try:
        tree = ast.parse(text)
    except SyntaxError:
        tree = None
    if tree is not None:
        for rule, generator in (("radius", _radius_findings),
                                ("spacing", _spacing_findings),
                                ("icon", _icon_findings)):
            for lineno, detail in generator(tree):
                out.append(finding(rule, lineno, detail))
    for lineno, detail in _font_findings(text):
        out.append(finding("font", lineno, detail))
    return sorted(out, key=lambda f: (f.line, f.rule))


def _scan_tree(root) -> list:
    """Every rule over one tree, with product-relative paths where possible."""
    base = pathlib.Path(root)
    if not base.is_absolute():
        base = ROOT / base
    if not base.exists():
        return []
    out = []
    for path in sorted(base.rglob("*.py")):
        try:
            rel = path.relative_to(ROOT).as_posix()
        except ValueError:            # a planted tree outside the product
            rel = path.relative_to(base).as_posix()
        out.extend(_scan_file(rel, path.name, path.read_text(encoding="utf-8")))
    return out


def scan(root=None) -> list:
    """Findings for the product, or for every rule under one `root`.

    A `root` means "scan this tree with every rule" (the negative tests and a
    one-off look at a directory). No root means the real gate: each rule runs
    over its own scope from SCOPE, and the results are merged in source order.
    """
    if root is not None:
        return sorted(_scan_tree(root),
                      key=lambda f: (f.path, f.line, f.rule))
    out = []
    for tree in sorted(set(SCOPE.values())):
        rules = {rule for rule, scope in SCOPE.items() if scope == tree}
        out.extend(f for f in _scan_tree(tree) if f.rule in rules)
    return sorted(out, key=lambda f: (f.path, f.line, f.rule))


def report(root=None) -> dict:
    """{rule: [Finding]}, every rule present even when it is empty."""
    grouped = {rule: [] for rule in REMEDY}
    for found in scan(root):
        grouped[found.rule].append(found)
    return grouped


def main(argv=None) -> int:
    """Print the grouped report. 0 when the tree is clean, 1 when it is not."""
    argv = list(sys.argv[1:] if argv is None else argv)
    root = argv[0] if argv else None
    grouped = report(root)
    total = sum(len(rows) for rows in grouped.values())
    print("design literals: %d" % total)
    for rule, rows in grouped.items():
        print("\n%s (%d) in %s - %s"
              % (rule.upper(), len(rows),
                 root or SCOPE[rule], REMEDY[rule]))
        for found in rows:
            print("   %s\n       %s" % (found, found.source))
    return 0 if not total else 1


if __name__ == "__main__":
    sys.exit(main())
