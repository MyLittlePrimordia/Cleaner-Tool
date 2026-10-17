"""The Microsoft Update Catalog, searched by hardware id.

https://www.catalog.update.microsoft.com is a public website with no API.
Searching it by a device's hardware id is a supported thing a person does by
hand (Microsoft's own driver-troubleshooting guidance says to), and several
open-source tools drive the same two pages this module uses:

    GET  Search.aspx?q=<hardware id>          -> an HTML table of packages
    POST DownloadDialog.aspx                  -> a page whose script lists
         updateIDs=[{"size":0,"updateID":...}]   the file URLs

Because that is page-scraping, not a contract, this module is written to
FAIL LOUDLY AND SMALL: an unrecognised page is reported as "the catalog's
page format was not recognised" (never as "no drivers found"), every request
has a timeout and bounded retries, and the Drivers tab can always fall back to
opening the same search in the user's browser (`browser_url`).

Network access is injected (`fetch(url, data=None) -> str`) so the parsing is
tested against saved pages, and so tests never touch the network.
"""
from __future__ import annotations

import json
import platform
import re
import time
import urllib.parse
import urllib.request
from html.parser import HTMLParser
from typing import Callable, List, Optional

BASE = "https://www.catalog.update.microsoft.com/"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) CleanerTool-DriverFinder "
      "(+user-initiated; one request per device)")
RESULTS_ID = "ctl00_catalogBody_updateMatches"
NO_RESULTS_ID = "ctl00_catalogBody_noResultText"

#: Download hosts we will fetch from. The catalog hands out Microsoft CDN
#: URLs; anything else in a response is refused.
ALLOWED_HOSTS = ("download.windowsupdate.com", "dl.delivery.mp.microsoft.com",
                 "delivery.mp.microsoft.com", "download.microsoft.com",
                 "windowsupdate.com", "catalog.sf.dl.delivery.mp.microsoft.com")


class CatalogError(RuntimeError):
    """The catalog could not be reached or its page was not understood."""


# ------------------------------------------------------------------ fetching
def default_fetch(url: str, data: Optional[bytes] = None,
                  timeout: int = 20) -> str:
    """One HTTP request with a polite User-Agent and bounded retries."""
    last = None
    for attempt in range(3):
        try:
            req = urllib.request.Request(
                url, data=data, headers={
                    "User-Agent": UA, "Accept-Language": "en-US,en;q=0.8",
                    **({"Content-Type": "application/x-www-form-urlencoded"}
                       if data is not None else {})})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read().decode("utf-8", "replace")
        except Exception as exc:                       # noqa: BLE001
            last = exc
            time.sleep(1.5 * (attempt + 1))
    raise CatalogError("Could not reach the Microsoft Update Catalog: %s"
                       % last)


def browser_url(query: str) -> str:
    """The same search as a normal web page, for the user's browser."""
    return BASE + "Search.aspx?q=" + urllib.parse.quote(query)


# ------------------------------------------------------------------- parsing
class _Rows(HTMLParser):
    """Collects the results table into rows of cell texts + spans."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.in_table = False
        self.depth = 0
        self.rows: List[dict] = []
        self._row: Optional[dict] = None
        self._cell: Optional[dict] = None
        self._span_text: Optional[list] = None
        self.no_results = False

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if a.get("id") == NO_RESULTS_ID:
            self.no_results = True
        if tag == "table":
            if a.get("id") == RESULTS_ID:
                self.in_table = True
                self.depth = 1
            elif self.in_table:
                self.depth += 1
        if not self.in_table:
            return
        if tag == "tr":
            rid = a.get("id") or ""
            if rid and "headerRow" not in rid and "_R" in rid:
                self._row = {"id": rid.split("_")[0], "cells": []}
        elif tag == "td" and self._row is not None:
            self._cell = {"text": [], "spans": []}
        elif tag == "span" and self._cell is not None:
            self._span_text = []

    def handle_endtag(self, tag):
        if not self.in_table:
            return
        if tag == "span" and self._cell is not None \
                and self._span_text is not None:
            self._cell["spans"].append("".join(self._span_text).strip())
            self._span_text = None
        elif tag == "td" and self._cell is not None and self._row is not None:
            self._cell["joined"] = " ".join(
                "".join(self._cell["text"]).split())
            self._row["cells"].append(self._cell)
            self._cell = None
        elif tag == "tr" and self._row is not None:
            self.rows.append(self._row)
            self._row = None
        elif tag == "table":
            self.depth -= 1
            if self.depth <= 0:
                self.in_table = False

    def handle_data(self, data):
        if self._cell is not None:
            self._cell["text"].append(data)
            if self._span_text is not None:
                self._span_text.append(data)


def parse_results(html: str) -> List[dict]:
    """Rows of the search-results table.

    Returns [] for a page that says there are no results. Raises CatalogError
    for a page that is neither (so a changed page format is never mistaken for
    'no drivers exist').
    """
    p = _Rows()
    p.feed(html or "")
    if not p.rows:
        if p.no_results or "did not find any results" in (html or "").lower():
            return []
        if RESULTS_ID in (html or ""):
            return []        # a results table with only a header row
        raise CatalogError("The catalog's page format was not recognised "
                           "(it may have changed). Use 'Open in browser'.")
    out = []
    for r in p.rows:
        c = r["cells"]
        if len(c) < 6:
            continue
        # columns: [0] (icon) [1] title [2] products [3] classification
        #          [4] last updated [5] version [6] size (+ spans)
        size = 0
        if len(c) > 6:
            for s in reversed(c[6]["spans"]):
                if s.isdigit():
                    size = int(s)
                    break
        out.append({
            "id": r["id"], "title": c[1]["joined"],
            "products": c[2]["joined"], "classification": c[3]["joined"],
            "updated": c[4]["joined"], "version": c[5]["joined"],
            "size": size,
        })
    return out


def wants(result: dict, arch: Optional[str] = None) -> bool:
    """Is this catalog row a driver for a normal Windows 10/11 PC like this?"""
    arch = (arch or platform.machine() or "").upper()
    if not result.get("classification", "").lower().startswith("driver"):
        return False
    prod = result.get("products", "")
    if "windows" not in prod.lower() or "server" in prod.lower():
        return False
    if not any(k in prod for k in ("Windows 10", "Windows 11")):
        return False
    title = result.get("title", "").upper()
    if arch in ("AMD64", "X86_64") and ("ARM64" in title or "ARM" == title):
        return False
    return True


# --------------------------------------------------------------- the lookups
def search(query: str, fetch: Callable = default_fetch) -> List[dict]:
    """Driver packages the catalog lists for this hardware id, newest first."""
    url = BASE + "Search.aspx?q=" + urllib.parse.quote(query)
    rows = [r for r in parse_results(fetch(url)) if wants(r)]
    from app.drivers.versions import parse_date, parse_version
    rows.sort(key=lambda r: (parse_version(r["version"]),
                             str(parse_date(r["updated"]) or "")),
              reverse=True)
    return rows


_URL_RE = re.compile(
    r"downloadInformation\[\d+\]\.files\[\d+\]\.url\s*=\s*'([^']+)'")


def allowed_url(url: str) -> bool:
    try:
        u = urllib.parse.urlparse(url)
    except Exception:
        return False
    host = (u.hostname or "").lower()
    return (u.scheme in ("http", "https")
            and any(host == h or host.endswith("." + h)
                    for h in ALLOWED_HOSTS))


def download_links(update_id: str, fetch: Callable = default_fetch) -> List[str]:
    """The file URLs for one catalog package (usually a single .cab)."""
    payload = json.dumps([{"size": 0, "languages": "", "uidInfo": update_id,
                           "updateID": update_id}])
    body = urllib.parse.urlencode({"updateIDs": payload}).encode()
    page = fetch(BASE + "DownloadDialog.aspx", body)
    urls = [u for u in _URL_RE.findall(page) if allowed_url(u)]
    if not urls:
        raise CatalogError("The catalog gave no download link for this "
                           "driver. Use 'Open in browser'.")
    return urls
