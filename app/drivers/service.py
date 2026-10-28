r"""The Drivers tab's operations, in the order a careful person would do them.

    scan_all      inventory (this PC) -> catalog (online) -> candidates
    download      each chosen package into <Drivers>\downloads, with progress
    install       restore point -> back up what is being replaced -> for each
                  package: unpack, install -> a per-driver result

Each function takes callbacks instead of touching any UI, never raises for an
expected failure (it returns a result the tab can show), and checks
`should_stop` between units of work so Stop is prompt.

Why a restore point AND a driver backup: a restore point is the way back from
a driver that makes the PC misbehave; the pnputil export is the exact files
that were replaced, for the rarer case where restore is not an option. If a
restore point cannot be made (System Protection off) the install is REFUSED
rather than attempted without a safety net - the tab says how to fix it.
"""
from __future__ import annotations

import datetime
import os
from typing import Callable, List, Optional

from app.drivers import catalog, download as dl, folder, install as ins
from app.drivers import inventory, scan as scanmod


def ps_lines(script: str, timeout: int) -> List[str]:
    """Run a PowerShell script (encoded, no window) and return its lines."""
    from app import updates
    rc, text = ins.default_runner(updates.powershell_argv(script), timeout)
    return text.splitlines()


def scan_all(progress=None, should_stop=lambda: False, full: bool = False,
             run_powershell: Callable = ps_lines,
             search: Callable = catalog.search,
             cache: Optional[scanmod.Cache] = None) -> dict:
    """-> {"items", "devices", "checked", "error"}; never raises."""
    try:
        devices = inventory.collect(run_powershell)
    except Exception as exc:                        # noqa: BLE001
        return {"items": [], "devices": [], "checked": 0,
                "error": "Could not read this PC's devices: %s" % exc}
    if cache is None:
        try:
            from app.config_paths import CONFIG_DIR
            cache = scanmod.Cache(os.path.join(str(CONFIG_DIR),
                                               "drivers_cache.json"))
        except Exception:
            cache = scanmod.Cache(None)
    res = scanmod.find_updates(devices, search, progress, should_stop, full,
                               cache)
    res["devices"] = devices
    return res


def download_items(items: List[dict], root: str,
                   progress: Optional[Callable] = None,
                   should_stop: Callable[[], bool] = lambda: False,
                   fetch: Callable = catalog.default_fetch,
                   downloader: Callable = dl.download) -> List[dict]:
    """Download each item's cab. Returns one result dict per item:
    {"item", "ok", "path", "error"}. progress(index, count, name, done,
    total, speed) fires for every chunk."""
    dest = folder.ensure_layout(root)["downloads"]
    out = []
    for i, it in enumerate(items):
        if should_stop():
            out.append({"item": it, "ok": False, "path": "",
                        "error": "Cancelled."})
            break
        try:
            urls = catalog.download_links(it["update_id"], fetch)
            path = downloader(
                urls[0], dest,
                progress=(lambda d, t, s, _i=i, _it=it: progress(
                    _i, len(items), _it["name"], d, t, s)) if progress
                else None,
                should_stop=should_stop)
            out.append({"item": it, "ok": True, "path": path, "error": ""})
        except Exception as exc:                    # noqa: BLE001
            out.append({"item": it, "ok": False, "path": "",
                        "error": str(exc)})
            if str(exc) == "Download cancelled.":
                break
    return out


def install_items(downloaded: List[dict], root: str,
                  progress: Optional[Callable[[int, int, str], None]] = None,
                  should_stop: Callable[[], bool] = lambda: False,
                  make_restore_point: Optional[Callable] = None,
                  runner: ins.Runner = ins.default_runner,
                  is_admin: Optional[bool] = None) -> dict:
    """Install already-downloaded packages.

    `downloaded` = the ok results from download_items. Returns
    {"results": [...], "reboot": bool, "backup": folder, "error": str}.
    """
    if is_admin is None:
        from app.elevation import is_admin as _adm
        is_admin = bool(_adm())
    if not is_admin:
        return {"results": [], "reboot": False, "backup": "",
                "error": "Installing drivers needs administrator rights. "
                         "Restart Cleaner Tool as administrator."}
    layout = folder.ensure_layout(root)
    if make_restore_point is None:
        make_restore_point = _restore_point
    ok, why = make_restore_point()
    if not ok:
        return {"results": [], "reboot": False, "backup": "",
                "error": "No restore point could be made, so nothing was "
                         "installed (%s). Turn on System Protection, or "
                         "install from the manufacturer's page." % why}
    infs = [inf for d in downloaded for inf in d["item"].get("infs", [])]
    stamp = datetime.datetime.now().strftime("%Y-%m-%d_%H%M%S")
    bdir, _n, _failed = ins.backup(infs, layout["backup"], stamp, runner)
    results, reboot = [], False
    for i, d in enumerate(downloaded):
        if should_stop():
            break
        name = d["item"]["name"]
        if progress:
            progress(i, len(downloaded), name)
        try:
            folder_ = ins.extract(d["path"], layout["extracted"], runner)
            r = ins.install(folder_, runner)
        except Exception as exc:                    # noqa: BLE001
            results.append({"name": name, "ok": False, "reboot": False,
                            "detail": str(exc)})
            continue
        reboot = reboot or r["reboot"]
        results.append({"name": name, "ok": r["ok"], "reboot": r["reboot"],
                        "detail": "" if r["ok"] else
                        "pnputil exit code %s" % r["code"]})
    return {"results": results, "reboot": reboot, "backup": bdir, "error": ""}


def _restore_point():
    """(ok, reason). 'Already made today' counts as ok: it is a way back."""
    try:
        from app.restorepts import create
        res = create("Cleaner Tool - before driver install")
    except Exception as exc:                        # noqa: BLE001
        return False, str(exc)
    if res.get("ok") or res.get("skipped"):
        return True, ""
    return False, res.get("error") or "unknown error"
