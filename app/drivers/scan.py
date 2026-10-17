"""Inventory -> catalog -> "these can be updated".

For every device worth looking up (see inventory.relevant) ask the catalog
for its hardware ids, most specific first, and keep the first id that returns
a driver that is genuinely newer than what is installed (or any driver, if the
device has none). Devices that resolve to the same catalog package are merged
into one row ("Intel USB controllers (4)") so the user picks drivers, not
twenty identical lines.

Politeness and resilience: one request per query with a pause between them,
results cached on disk for a day (a re-scan is instant and costs the catalog
nothing), and three consecutive failures abort the scan with one clear
message instead of grinding through a hundred timeouts.
"""
from __future__ import annotations

import json
import os
import time
from typing import Callable, Dict, List, Optional

from app.drivers import inventory
from app.drivers.catalog import CatalogError
from app.drivers.versions import is_newer

CACHE_TTL_S = 24 * 3600
REQUEST_GAP_S = 0.6
MAX_CONSECUTIVE_ERRORS = 3


class Cache:
    """{query: (timestamp, results)} in one small JSON file. Never raises."""

    def __init__(self, path: Optional[str] = None, now=time.time):
        self.path, self._now = path, now
        self.data: Dict[str, list] = {}
        if path and os.path.isfile(path):
            try:
                with open(path, encoding="utf-8") as f:
                    self.data = json.load(f)
            except Exception:
                self.data = {}

    def get(self, key: str):
        hit = self.data.get(key)
        if hit and self._now() - float(hit[0]) < CACHE_TTL_S:
            return hit[1]
        return None

    def put(self, key: str, results: list) -> None:
        self.data[key] = [self._now(), results]

    def save(self) -> None:
        if not self.path:
            return
        try:
            tmp = self.path + ".tmp"
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self.data, f)
            os.replace(tmp, self.path)
        except Exception:
            pass


def find_updates(devices: List[dict], search: Callable[[str], List[dict]],
                 progress: Optional[Callable[[int, int, str], None]] = None,
                 should_stop: Callable[[], bool] = lambda: False,
                 full: bool = False, cache: Optional[Cache] = None,
                 sleep: Callable[[float], None] = time.sleep) -> dict:
    """-> {"items": [...], "checked": n, "total_devices": n, "error": str}"""
    targets = [d for d in devices if inventory.relevant(d, full)]
    cache = cache or Cache(None)
    by_pkg: Dict[str, dict] = {}
    errors, consec, last_error = 0, 0, ""

    for i, dev in enumerate(targets):
        if should_stop():
            break
        if progress:
            progress(i, len(targets), dev["name"])
        found = None
        for q in inventory.hwid_queries(dev):
            results = cache.get(q)
            if results is None:
                try:
                    results = search(q)
                    cache.put(q, results)
                    consec = 0
                except CatalogError as exc:
                    errors += 1
                    consec += 1
                    last_error = str(exc)
                    results = []
                    if consec >= MAX_CONSECUTIVE_ERRORS:
                        cache.save()
                        return {"items": _items(by_pkg), "checked": i,
                                "total_devices": len(targets),
                                "error": last_error}
                sleep(REQUEST_GAP_S)
            for r in results:
                if dev["state"] == "missing" or is_newer(
                        r["version"], r["updated"], dev["version"],
                        dev["date"]):
                    found = (q, r)
                    break
            if found:
                break
        if found:
            q, r = found
            pkg = by_pkg.setdefault(r["id"], {
                "update_id": r["id"], "title": r["title"],
                "version": r["version"], "date": r["updated"],
                "size": r["size"], "query": q, "devices": [],
                "state": "update", "installed": dev["version"],
                "installed_date": dev["date"], "infs": set(),
                "cls": dev["cls"]})
            pkg["devices"].append(dev["name"])
            if dev["state"] == "missing":
                pkg["state"] = "missing"
            if dev.get("inf"):
                pkg["infs"].add(dev["inf"])
    cache.save()
    if progress:
        progress(len(targets), len(targets), "")
    return {"items": _items(by_pkg), "checked": len(targets),
            "total_devices": len(devices), "error": last_error if
            errors and not by_pkg else ""}


def _items(by_pkg: Dict[str, dict]) -> List[dict]:
    items = []
    for p in by_pkg.values():
        p = dict(p)
        p["infs"] = sorted(p["infs"])
        names = p["devices"]
        p["name"] = names[0] if len(names) == 1 else \
            "%s (+%d more)" % (names[0], len(names) - 1)
        items.append(p)
    # missing first, then biggest jump in time is unknowable: keep stable
    items.sort(key=lambda p: (p["state"] != "missing", p["name"].lower()))
    return items
