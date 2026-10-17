"""Driver version and date comparison. Pure.

Driver versions are dotted numbers ("31.0.101.5186", "10.0.22621.1") and are
compared NUMERICALLY, part by part - as strings "9.0" > "31.0". A candidate is
"newer" only on evidence: a higher version, or - when either version cannot
be read - a later date. Equal or older is never an update, so the tool does
not offer to "update" a driver to what is already installed.
"""
from __future__ import annotations

import datetime
import re
from typing import Optional, Tuple


def parse_version(text) -> Tuple[int, ...]:
    """'31.0.101.5186' -> (31, 0, 101, 5186); unparseable -> ()."""
    parts = re.findall(r"\d+", str(text or ""))
    return tuple(int(p) for p in parts[:6])


def parse_date(text) -> Optional[datetime.date]:
    """Accepts 2026-10-01, 10/1/2026, 2026-10-01T00:00:00 and the catalog's
    'M/D/YYYY'. None when it cannot be read."""
    s = str(text or "").strip()
    if not s:
        return None
    m = re.match(r"(\d{4})-(\d{1,2})-(\d{1,2})", s)
    if m:
        y, mo, d = map(int, m.groups())
    else:
        m = re.match(r"(\d{1,2})/(\d{1,2})/(\d{4})", s)
        if not m:
            return None
        mo, d, y = map(int, m.groups())
    try:
        return datetime.date(y, mo, d)
    except ValueError:
        return None


def is_newer(cand_version, cand_date, have_version, have_date) -> bool:
    """True if the candidate is genuinely newer than what is installed."""
    cv, hv = parse_version(cand_version), parse_version(have_version)
    if cv and hv:
        # compare as equal-length tuples so (1,2) vs (1,2,0) are equal
        n = max(len(cv), len(hv))
        cv = cv + (0,) * (n - len(cv))
        hv = hv + (0,) * (n - len(hv))
        if cv != hv:
            return cv > hv
        # same version number: only a clearly later date counts
    cd, hd = parse_date(cand_date), parse_date(have_date)
    if cd and hd:
        return cd > hd
    return False
