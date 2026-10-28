"""The shipped artifact: one version, one asset tree, one exe name.

Nothing here can run the release build - it is a Windows PyInstaller step in
CI - so the three things a build quietly gets wrong are pinned from the source
instead:

  * the `.exe` must be named what the in-app updater looks for, or every
    future release is invisible to every installed copy;
  * the version stamped into the file (both the fixed struct and the strings)
    must be the version the app reports, or Windows shows one number and the
    app shows another;
  * every asset the app opens at runtime must sit inside the tree the build
    bundles, and the frozen lookup must prefer that tree over the source one.

The last point is the one that only fails on a user's PC: a missing file in
`app/assets` still resolves from the repo during development, so the defect
appears the first time someone runs the packaged exe.

The checks are plain functions taking text or names, so each rule can be
proved against a planted failure rather than only against a healthy tree.
"""
from __future__ import annotations

import ast
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parents[2]
WORKFLOW = ROOT / ".github" / "workflows" / "build.yml"
ASSETS = ROOT / "app" / "assets"

#: The flags the release depends on, with the exact text the step must carry.
#: `--onefile` is the portable single file, `--windowed` keeps the console
#: window out of a GUI product, `--name` is what the updater matches on, and
#: the three path flags are the assets, the elevation manifest and the version
#: resource. Drop any one of them and the build still succeeds - it just ships
#: something else.
_BUILD_FLAGS = (
    ("--onefile", "--onefile"),
    ("--windowed", "--windowed"),
    ("--name", '--name "Cleaner-Tool"'),
    ("--icon", '--icon "app/assets/icon.ico"'),
    ("--add-data", '--add-data "app/assets;app/assets"'),
    ("--manifest", '--manifest "app/assets/app_manifest.xml"'),
    ("--version-file", '--version-file "version_info.txt"'),
    ("entry point", "app/__main__.py"),
)

#: The flags whose argument names a repo file (the `--add-data` source half).
_BUILD_PATHS = {
    "--icon": "app/assets/icon.ico",
    "--add-data": "app/assets",
    "--manifest": "app/assets/app_manifest.xml",
    "--version-file": "version_info.txt",
    "entry point": "app/__main__.py",
}


def _workflow_text() -> str:
    return WORKFLOW.read_text(encoding="utf-8")


def _build_command(text: str) -> str:
    """The joined PyInstaller command from the workflow, spaces normalised.

    Reads from the line that invokes pyinstaller to the next step, so a flag
    added anywhere else in the file cannot satisfy a check.
    """
    lines = text.splitlines()
    start = next(i for i, line in enumerate(lines) if "pyinstaller" in line)
    block = []
    for line in lines[start:]:
        if line.strip().startswith("- name:") and block:
            break
        block.append(line)
    return " ".join(" ".join(block).split())


def _missing_build_flags(command: str) -> list:
    """Which required pieces `command` does not carry, as readable names."""
    return [label for label, needle in _BUILD_FLAGS if needle not in command]


def _version_findings(text: str, version: str) -> list:
    """Where `text` (version_info.txt) disagrees with `version`."""
    parts = [int(p) for p in version.split(".")]
    expected = tuple((parts + [0, 0, 0, 0])[:4])
    findings = []
    for name in ("filevers", "prodvers"):
        found = re.search(r"%s=\(([^)]*)\)" % name, text)
        if not found:
            findings.append("%s is missing" % name)
            continue
        got = tuple(int(p.strip()) for p in found.group(1).split(",") if p.strip())
        if got != expected:
            findings.append("%s=%s, the app reports %s"
                            % (name, got, expected))
    dotted = ".".join(str(p) for p in expected)
    for field in ("FileVersion", "ProductVersion"):
        found = re.search(r"u'%s',\s*u'([^']*)'" % field, text)
        if not found:
            findings.append("%s string is missing" % field)
        elif found.group(1) != dotted:
            findings.append("%s=%r, the app reports %r"
                            % (field, found.group(1), dotted))
    return findings


def _asset_literals(source: str) -> list:
    """Every literal name handed to `resolve_asset_path` in `source`."""
    names = []
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not (isinstance(func, ast.Attribute)
                and func.attr == "resolve_asset_path"):
            continue
        if node.args and isinstance(node.args[0], ast.Constant):
            value = node.args[0].value
            if isinstance(value, str):
                names.append(value)
    return names


def _orphan_icons(shipped, cards) -> list:
    """Icons in the bundled tree that no card names.

    The per-tool art is loaded as `tools_icons/<card key>.png`, so an icon
    whose key is not a card is a file the build carries and nothing opens -
    the debris nine deleted tools used to leave behind.
    """
    return sorted(set(shipped) - set(cards))


# --------------------------------------------------------------------------- #
# Version
# --------------------------------------------------------------------------- #
def test_the_version_stamped_into_the_exe_is_the_one_the_app_reports():
    from app import __version__ as package_version
    from app.config import APP_VERSION

    assert package_version == APP_VERSION, (
        "app/__init__.__version__ and config.APP_VERSION disagree: %r vs %r"
        % (package_version, APP_VERSION))
    findings = _version_findings((ROOT / "version_info.txt").read_text(
        encoding="utf-8"), APP_VERSION)
    assert not findings, "version_info.txt is out of step:\n  " + "\n  ".join(
        findings)


def test_the_version_guard_sees_a_stale_resource():
    stale = (
        "    filevers=(1, 9, 0, 0),\n"
        "    prodvers=(2, 0, 0, 0),\n"
        "StringStruct(u'FileVersion', u'1.9.0.0'),\n"
        "StringStruct(u'ProductVersion', u'2.0.0.0')]\n")
    findings = _version_findings(stale, "2.0.0")
    assert len(findings) == 2, findings
    assert any("filevers" in f for f in findings)
    assert any("FileVersion" in f for f in findings)


# --------------------------------------------------------------------------- #
# The build command
# --------------------------------------------------------------------------- #
def test_the_release_command_bundles_everything_the_app_opens():
    command = _build_command(_workflow_text())
    missing = _missing_build_flags(command)
    assert not missing, "the PyInstaller step is missing: " + ", ".join(missing)
    for label, path in _BUILD_PATHS.items():
        assert (ROOT / path).exists(), (
            "%s points at %s, which is not in the repo" % (label, path))


def test_the_build_runs_the_suite_first():
    """An artifact is never the first thing to see a change."""
    text = _workflow_text()
    assert text.index("python -m pytest -q") < text.index("pyinstaller"), (
        "the build step runs before the suite in build.yml")


def test_the_build_guard_sees_a_command_without_its_assets():
    partial = ('pyinstaller --onefile --windowed --name "Cleaner-Tool" '
               '--icon "app/assets/icon.ico" app/__main__.py')
    missing = _missing_build_flags(partial)
    assert "--add-data" in missing
    assert "--manifest" in missing
    assert "--version-file" in missing
    assert "--onefile" not in missing


def test_the_shipped_manifest_keeps_the_app_unelevated_and_dpi_aware():
    """Two manifest lines the product depends on.

    `asInvoker` is not caution: the Admin Gate, the elevation handshake and
    "start with Windows at login" all assume the exe starts as the user. A
    manifest that requested elevation would make every launch a UAC prompt and
    skip the gate that explains it. The DPI block is what keeps Tk from being
    bitmap-stretched at 125-200%; the runtime call in app/dpi.py only takes
    effect when the manifest has not already declared awareness, so dropping
    it changes how the app scales.
    """
    manifest = (ASSETS / "app_manifest.xml").read_text(encoding="utf-8")
    assert 'level="asInvoker"' in manifest
    assert "PerMonitorV2" in manifest


def test_the_artifact_name_is_the_one_the_updater_looks_for():
    from app import self_update, update_checker

    command = _build_command(_workflow_text())
    assert '--name "Cleaner-Tool"' in command
    assert self_update.ASSET_NAME == "Cleaner-Tool.exe"
    assert update_checker.SHA_ASSET_NAME == self_update.SHA_ASSET_NAME
    # The checksum published beside the exe is the name the updater prefers.
    assert "Cleaner-Tool.exe.sha256" in _workflow_text()


# --------------------------------------------------------------------------- #
# The asset tree
# --------------------------------------------------------------------------- #
def test_every_literal_asset_the_source_opens_is_in_the_bundled_tree():
    missing = []
    for path in sorted((ROOT / "app").rglob("*.py")):
        for name in _asset_literals(path.read_text(encoding="utf-8")):
            if "%s" in name:
                folder = name.split("/")[0]
                if not (ASSETS / folder).is_dir():
                    missing.append("%s: %s/ is not a bundled directory"
                                   % (path.relative_to(ROOT), folder))
                continue
            if not (ASSETS / name).is_file():
                missing.append("%s: app/assets/%s does not exist"
                               % (path.relative_to(ROOT), name))
    assert not missing, "\n  ".join(missing)


def test_every_shipped_tool_icon_belongs_to_a_card():
    from app.ui.tabs.toolstab import _CARDS

    shipped = {p.stem for p in (ASSETS / "tools_icons").glob("*.png")}
    cards = {key for key, _glyph, _title, _blurb in _CARDS}
    orphans = _orphan_icons(shipped, cards)
    assert not orphans, "bundled but never opened: " + ", ".join(orphans)


def test_the_orphan_check_fires():
    assert _orphan_icons({"storage", "gamepad"}, {"storage"}) == ["gamepad"]


# --------------------------------------------------------------------------- #
# The frozen import graph
# --------------------------------------------------------------------------- #
def _missing_app_imports(root):
    """Absolute `app.*` imports whose target module is not in the tree.

    A stale import does not fail the suite, and it does not fail the build:
    PyInstaller records it and carries on, so the only trace is one line in
    `warn-Cleaner-Tool.txt` that nobody reads. At runtime the same line is an
    ImportError, either fatal at startup or - the common case - swallowed by a
    `try/except` that leaves the feature silently degraded. That is exactly
    how `from app.run_state import get_tweak_state` survived: the registry
    moved into app/config_tweaks.py when config_persist was split, and the two
    copies in the Game Session pilot kept failing into an empty set. One made
    the Game Mode preview list settings that were already on; the other made
    the pilot report "did not change anything" after applying all of them.

    Only imports whose target is unambiguous are checked: `from app.a.b import
    name` may name a submodule or an attribute of app.a.b, so only app.a.b is
    required. `from app import name` is always a submodule here - app/__init__
    exports nothing but __version__.
    """
    package = root / "app"
    problems = []
    for path in sorted(package.rglob("*.py")):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (SyntaxError, UnicodeDecodeError):
            continue
        rel = path.relative_to(root).as_posix()
        wanted = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name.startswith("app."):
                        wanted.append(alias.name)
            elif isinstance(node, ast.ImportFrom) and node.level == 0:
                base = node.module or ""
                if base == "app":
                    wanted.extend("app." + a.name for a in node.names
                                  if not a.name.startswith("__"))
                elif base.startswith("app."):
                    wanted.append(base)
        for module in wanted:
            target = root.joinpath(*module.split("."))
            if not (target.with_suffix(".py").is_file()
                    or (target / "__init__.py").is_file()):
                problems.append("%s: %s" % (rel, module))
    return problems


def test_every_app_import_names_a_module_that_exists():
    missing = _missing_app_imports(ROOT)
    assert not missing, (
        "imports point at modules that do not exist (PyInstaller lists these "
        "only as warnings, and the try/except around them hides the failure):\n  "
        + "\n  ".join(missing))


def test_the_import_guard_sees_a_stale_module(tmp_path):
    pkg = tmp_path / "app"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("__version__ = '1'\n", encoding="utf-8")
    (pkg / "real.py").write_text("", encoding="utf-8")
    (pkg / "user.py").write_text(
        "from app.real import thing\n"
        "from app.gone import other\n"
        "import app.also_gone as x\n"
        "from . import sibling\n",
        encoding="utf-8")

    assert _missing_app_imports(tmp_path) == [
        "app/user.py: app.gone",
        "app/user.py: app.also_gone",
    ]


def test_a_frozen_lookup_reads_the_bundle_first(monkeypatch, tmp_path):
    """The one path that differs between development and the shipped exe."""
    from app.utils import resolve_asset_path

    bundled = tmp_path / "app" / "assets"
    bundled.mkdir(parents=True)
    (bundled / "icon.ico").write_bytes(b"bundled")
    monkeypatch.setattr(sys, "_MEIPASS", str(tmp_path), raising=False)

    found = resolve_asset_path("icon.ico")
    assert found is not None
    assert pathlib.Path(found) == bundled / "icon.ico", (
        "the frozen lookup answered from %r instead of the bundle" % found)
    assert resolve_asset_path("no-such-asset.png") is None


# --------------------------------------------------------------------------- #
# The release signature
# --------------------------------------------------------------------------- #
#: The custody service the release signs through, and the secrets it needs.
_SIGN_ACTION = "sslcom/esigner-codesign"
_SIGN_SECRETS = ("ES_USERNAME", "ES_PASSWORD", "ES_CREDENTIAL_ID",
                 "ES_TOTP_SECRET")
_PINNED_ACTION = re.compile(
    re.escape(_SIGN_ACTION) + r"@[0-9a-f]{40}\b")


def _signing_problems(text):
    """What is wrong with a workflow's signing, as a list of sentences.

    Four separate promises, each of which has failed somewhere before:

      * the key stays in qualifying hardware. Since 2023-06-01 the CA/Browser
        Forum requires a publicly trusted code-signing key to be generated,
        stored and used inside a hardware module, so the old .pfx-in-a-secret
        flow cannot work for a newly issued certificate - it is checked for
        ABSENCE, not just left unused.
      * the third-party action is pinned. `@develop` is a moving branch holding
        credentials that can sign malware as this project.
      * all four secrets are actually wired, not three of four (a missing TOTP
        seed fails only at signing time, on main, after the tag).
      * the result is verified. The old step could report success and leave an
        unsigned file; nothing looked, so nothing failed.
    """
    problems = []
    if _SIGN_ACTION not in text:
        return ["no %s action: the release is not signed" % _SIGN_ACTION]
    if not _PINNED_ACTION.search(text):
        problems.append("the signing action is not pinned to a commit SHA")

    before = text[:text.find("uses: " + _SIGN_ACTION)]
    window = before[-900:]
    if "if:" not in window:
        problems.append("the signing step runs unconditionally")
    if "event_name" not in window or "event_name == 'pull_request'" in window:
        problems.append(
            "signing is not restricted to release events - a pull request "
            "could spend a signature and sign reviewable code")

    for secret in _SIGN_SECRETS:
        # `secrets.NAME`, not the bare name: the explanatory comment above the
        # step lists all four, and a comment is not a wired input.
        if "secrets." + secret not in text:
            problems.append("missing secret %s" % secret)

    if "Get-AuthenticodeSignature" not in text:
        problems.append("nothing verifies the signature")
    if "-ne 'Valid'" not in text:
        problems.append("the verifier does not reject an invalid signature")
    if "TimeStamperCertificate" not in text:
        problems.append("the verifier accepts an untimestamped signature")

    if "CODE_SIGNING_PFX_BASE64" in text:
        problems.append(
            "the retired .pfx flow is still present (not compliant for a "
            "certificate issued after 2023-06-01)")
    return problems


def test_the_release_signs_through_a_pinned_custody_service():
    problems = _signing_problems(_workflow_text())
    assert not problems, "\n  ".join(problems)


def test_the_signing_guard_sees_each_planted_failure():
    good = _workflow_text()
    assert _signing_problems(good) == []

    floating = good.replace(
        _PINNED_ACTION.search(good).group(0),
        _SIGN_ACTION + "@develop")
    assert "not pinned" in " ".join(_signing_problems(floating))

    dropped = good.replace("totp_secret: ${{ secrets.ES_TOTP_SECRET }}", "")
    assert "missing secret ES_TOTP_SECRET" in " ".join(
        _signing_problems(dropped))

    unverified = good.replace("Get-AuthenticodeSignature", "Write-Host")
    assert "nothing verifies" in " ".join(_signing_problems(unverified))

    stale_pfx = good + "\n# CODE_SIGNING_PFX_BASE64\n"
    assert "retired .pfx flow" in " ".join(_signing_problems(stale_pfx))

    pr_signed = good.replace("github.event_name == 'workflow_dispatch'",
                             "github.event_name == 'pull_request'")
    assert "release events" in " ".join(_signing_problems(pr_signed))

    unconditional = good.replace(
        "if: >-\n          steps.creds.outputs.available == 'true' &&\n"
        "          (github.event_name == 'push' || github.event_name == "
        "'workflow_dispatch')\n",
        "")
    assert _signing_problems(unconditional), (
        "removing the signing gate must be reported")
