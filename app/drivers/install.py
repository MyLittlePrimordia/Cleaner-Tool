r"""Extract and install driver packages, with a backup first.

    expand.exe -F:* <pkg>.cab <folder>          unpack the cab (built in)
    pnputil /add-driver <folder>\*.inf /subdirs /install
                                                stage it in the driver store
                                                and install it on matching
                                                devices
    pnputil /export-driver <oemNN.inf> <folder> back up a driver BEFORE it is
                                                replaced

pnputil verifies the package's catalog signature itself and refuses anything
unsigned or tampered with, so the install step needs no trust in this code.
Both tools are Windows built-ins and need administrator rights to install.

Process access is injected: `runner(argv, timeout) -> (exit_code, text)`.
"""
from __future__ import annotations

import os
import re
import subprocess
from typing import Callable, Iterable, List, Tuple

Runner = Callable[[List[str], int], Tuple[int, str]]

#: pnputil exit codes that mean "installed" (3010 = ERROR_SUCCESS_REBOOT_REQUIRED)
OK_CODES = (0, 3010)
REBOOT_CODE = 3010

_OEM_RE = re.compile(r"^oem\d+\.inf$", re.I)


def default_runner(argv: List[str], timeout: int = 600) -> Tuple[int, str]:
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    p = subprocess.run(argv, capture_output=True, timeout=timeout,
                       creationflags=flags, stdin=subprocess.DEVNULL)
    raw = (p.stdout or b"") + (p.stderr or b"")
    for enc in ("utf-16-le",) if b"\x00" in raw[:64] else ():
        try:
            return p.returncode, raw.decode(enc, "replace")
        except Exception:
            pass
    return p.returncode, raw.decode("utf-8", "replace")


def safe_stem(name: str) -> str:
    stem = os.path.splitext(os.path.basename(name))[0]
    return re.sub(r"[^A-Za-z0-9._-]", "_", stem)[:80] or "driver"


def find_infs(folder: str) -> List[str]:
    out = []
    for root, _d, files in os.walk(folder):
        out.extend(os.path.join(root, f) for f in files
                   if f.lower().endswith(".inf"))
    return out


def extract(cab: str, extracted_root: str, runner: Runner = default_runner) -> str:
    """Unpack `cab` into its own subfolder; return it. RuntimeError if it
    unpacked to nothing installable."""
    if not os.path.isfile(cab):
        raise RuntimeError("The downloaded file is missing: %s" %
                           os.path.basename(cab))
    dest = os.path.join(extracted_root, safe_stem(cab))
    os.makedirs(dest, exist_ok=True)
    rc, text = runner(["expand.exe", "-F:*", cab, dest], 300)
    if rc != 0:
        raise RuntimeError("Could not unpack the download (expand.exe exit "
                           "code %s)." % rc)
    if not find_infs(dest):
        raise RuntimeError("The download contained no driver (.inf) files.")
    return dest


def install(folder: str, runner: Runner = default_runner) -> dict:
    """Stage and install every .inf under `folder`.

    Returns {"ok", "reboot", "code", "text"}. Never raises on a tool failure:
    the caller reports it per driver and carries on with the rest.
    """
    if not find_infs(folder):
        return {"ok": False, "reboot": False, "code": -1,
                "text": "No .inf files to install."}
    pattern = os.path.join(folder, "*.inf")
    rc, text = runner(["pnputil", "/add-driver", pattern, "/subdirs",
                       "/install"], 900)
    return {"ok": rc in OK_CODES, "reboot": rc == REBOOT_CODE,
            "code": rc, "text": text}


def backup(inf_names: Iterable[str], backup_root: str, stamp: str,
           runner: Runner = default_runner) -> Tuple[str, int, List[str]]:
    """Export the named installed drivers (oemNN.inf) to <root>\\<stamp>.

    Only third-party drivers have an oemNN.inf in the driver store; Windows'
    own are skipped (they cannot be replaced by this tool anyway). Returns
    (folder, exported_count, failures). A failed export is reported, not
    fatal: the restore point taken just before is the primary way back.
    """
    dest = os.path.join(backup_root, re.sub(r"[^0-9A-Za-z_-]", "_", stamp))
    os.makedirs(dest, exist_ok=True)
    done, failed = 0, []
    for inf in sorted({n.lower() for n in inf_names if n}):
        if not _OEM_RE.match(inf):
            continue
        sub = os.path.join(dest, inf[:-4])
        os.makedirs(sub, exist_ok=True)
        rc, _t = runner(["pnputil", "/export-driver", inf, sub], 120)
        if rc == 0:
            done += 1
        else:
            failed.append(inf)
    return dest, done, failed
