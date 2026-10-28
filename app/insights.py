"""The findings bus: what a tool measured, in a form the rest of the app uses.

AUDIT TX-002 (2026-10). Every tool used to end at its own window. Drive
Toolkit knew a disk was reporting a warning, Storage knew 12 GB of junk was
sitting there, Duplicates knew a folder held 4 GB of copies and Game Readiness
knew the power plan was holding clocks back - and none of that outlived the
dialog. Health re-measured a narrower version of the same machine, the Tools
hub could only recommend from a checkup grade, and the Health tab showed only
what its own checkup had just measured.

This module is the memory between them. A tool publishes a `Finding` when it
measures something worth acting on; the Health tab and the Tools hub read the
active set instead of growing their own copy of every conclusion.

The rules that keep it honest
-----------------------------
* A finding is a CONCLUSION, not a metric dump. `title` names the problem in
  plain language, `detail` is one sentence, and the numbers ride in
  `evidence` so a later screen can quote them without parsing prose.
* Publishing never raises. The bus is best-effort: a config that cannot be
  written must not break the tool that found something. `publish()` reports
  what it actually stored (an empty list on failure), never a claim.
* Stale findings expire. A seven-day-old drive warning is history; showing it
  as current advice would train the user to ignore the real one. `active()`
  filters by age and `publish()` prunes old rows so the file stays bounded.
* A scan is authoritative for its own ids. `replace(prefix, ...)` drops every
  finding under a namespace the tool owns and writes the current set in one
  transaction, so a healthy re-check clears the old warning rather than
  leaving it to rot. `retract()` is for a single deliberate clear.
* One row per id. A newer observation replaces an older one; an older one is
  ignored, so a slow worker cannot overwrite a fresher reading.

Action targets are screen routes the app already has (`storage`,
`drivetoolkit`, `duplicates`, `readiness`, `drivers`, ...), or a `care`
target - a tab the Health tab maps to the screen that owns the same fix. The
bus does not know how to open them - consumers do - so it stays importable
without Tk.
"""
from __future__ import annotations

import math
import re
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Mapping, Optional

from app.config_schema import (
    INSIGHTS_MAX,
    INSIGHT_DETAIL_MAX,
    INSIGHT_EVIDENCE_MAX,
    INSIGHT_ID_MAX,
    INSIGHT_LABEL_MAX,
    INSIGHT_TITLE_MAX,
    INSIGHT_TARGET_MAX,
)

#: How long a finding is allowed to speak for the machine. Long enough to
#: survive a week of normal use, short enough that a fixed problem cannot be
#: quoted forever.
DEFAULT_TTL = 7 * 86400

#: The stored file keeps a little more than the shelf life, purely so two
#: quick scans cannot race one row out of existence. Active consumers still
#: apply DEFAULT_TTL.
_KEEP_TTL = 14 * 86400

DOMAINS = ("storage", "system", "network", "gaming", "security")
SEVERITIES = ("info", "warn", "critical")
CONFIDENCE = ("measured", "partial", "unknown")
ACTION_KINDS = ("route", "care")

#: Higher wins when the same tool is recommended for two reasons.
SEVERITY_RANK = {"info": 0, "warn": 1, "critical": 2}

_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]*$")


@dataclass(frozen=True)
class Finding:
    """One conclusion a tool reached, with the evidence behind it."""

    id: str
    domain: str
    severity: str
    title: str
    detail: str = ""
    evidence: dict = field(default_factory=dict)
    action: dict = field(default_factory=dict)
    observed_at: float = 0.0
    confidence: str = "measured"

    def as_dict(self) -> dict:
        """The stored shape. Bounded by the schema, readable by a human."""
        return {
            "id": self.id,
            "domain": self.domain,
            "severity": self.severity,
            "title": self.title,
            "detail": self.detail,
            "evidence": dict(self.evidence),
            "action": dict(self.action),
            "at": float(self.observed_at),
            "confidence": self.confidence,
        }


# --------------------------------------------------------------------------- #
# Normalisation
# --------------------------------------------------------------------------- #
def _text(value, cap) -> str:
    """One line, collapsed whitespace, bounded. Anything else becomes ''."""
    if not isinstance(value, str):
        return ""
    text = " ".join(value.split())
    return text[:cap]


def slug(text, limit: int = 48) -> str:
    """A stable, ASCII, id-safe fragment for a human name.

    Tools build ids such as ``drivetoolkit.drive.corsair-mp600`` from a disk
    name; this keeps the result valid for the id rule without letting a model
    name with a slash or a colon escape its namespace.
    """
    out = []
    for ch in str(text or ""):
        if ch.isascii() and ch.isalnum():
            out.append(ch.lower())
        else:
            out.append("-")
    cleaned = re.sub(r"-+", "-", "".join(out)).strip("-")
    return (cleaned[:limit].strip("-") or "item")


def _clean_action(value) -> dict:
    if not isinstance(value, Mapping):
        return {}
    kind = _text(value.get("kind"), 12).lower()
    target = _text(value.get("target"), INSIGHT_TARGET_MAX)
    label = _text(value.get("label"), INSIGHT_LABEL_MAX)
    if kind not in ACTION_KINDS or not target:
        return {}
    return {"kind": kind, "target": target, "label": label}


def _clean_evidence(value) -> dict:
    """Scalars only: an old row or a careless publisher cannot smuggle a
    nested object into the config file or into a tooltip."""
    if not isinstance(value, Mapping):
        return {}
    clean = {}
    for key, raw in list(value.items())[:INSIGHT_EVIDENCE_MAX]:
        name = _text(key, 32)
        if not name:
            continue
        if isinstance(raw, bool):
            clean[name] = raw
        elif isinstance(raw, int):
            clean[name] = raw
        elif isinstance(raw, float):
            if math.isfinite(raw):
                clean[name] = raw
        elif isinstance(raw, str):
            clean[name] = _text(raw, 120)
    return clean


def normalise(raw, now: Optional[float] = None) -> Optional[Finding]:
    """A trusted `Finding`, or None when the row cannot honestly be shown.

    Structural junk (a missing id, an unknown severity, an empty title) is
    dropped. A malformed optional field loses THAT field, not the finding:
    losing the whole conclusion because one action target was odd would make
    the bus less useful than the screen it came from.
    """
    now = time.time() if now is None else float(now)
    get = raw.get if isinstance(raw, Mapping) else (lambda k, d=None:
                                                     getattr(raw, k, d))
    raw_id = _text(get("id"), INSIGHT_ID_MAX + 8)
    if not raw_id or len(raw_id) > INSIGHT_ID_MAX or not _ID_RE.match(raw_id):
        return None
    domain = _text(get("domain"), 16).lower()
    severity = _text(get("severity"), 12).lower()
    title = _text(get("title"), INSIGHT_TITLE_MAX)
    if domain not in DOMAINS or severity not in SEVERITIES or not title:
        return None

    try:
        observed = float(get("at", get("observed_at", 0)) or 0)
    except (TypeError, ValueError, OverflowError):
        observed = 0.0
    if not math.isfinite(observed) or observed <= 0:
        observed = now
    # A clock that jumped forward must not make a finding immortal. Treat a
    # future timestamp as "now"; the row is still usable, just not trusted
    # for more than it can know.
    if observed > now + 300:
        observed = now

    confidence = _text(get("confidence"), 12).lower()
    if confidence not in CONFIDENCE:
        confidence = "measured"

    return Finding(
        id=raw_id,
        domain=domain,
        severity=severity,
        title=title,
        detail=_text(get("detail"), INSIGHT_DETAIL_MAX),
        evidence=_clean_evidence(get("evidence", {})),
        action=_clean_action(get("action", {})),
        observed_at=observed,
        confidence=confidence,
    )


def _normalise_rows(rows, now: Optional[float] = None) -> list:
    now = time.time() if now is None else float(now)
    out = []
    for raw in rows if isinstance(rows, (list, tuple)) else ():
        finding = normalise(raw, now)
        if finding is not None:
            out.append(finding)
    return out


def _fresh(finding: Finding, now: float, ttl: float) -> bool:
    # A small clock skew (a publisher's timestamp a second ahead) is still
    # current; a finding from the future by more than that is not trusted.
    # Normalise clamps larger jumps, so this mainly keeps two threads' clocks
    # from hiding a just-published row.
    return -300 <= now - finding.observed_at <= ttl


def _merge(existing, incoming, now: float, ttl: float = _KEEP_TTL) -> list:
    """One row per id, newest observation wins, bounded by time."""
    by_id = {f.id: f for f in existing if _fresh(f, now, ttl)}
    for finding in incoming:
        old = by_id.get(finding.id)
        if old is None or finding.observed_at >= old.observed_at:
            by_id[finding.id] = finding
    rows = sorted(by_id.values(), key=lambda f: f.observed_at)
    return rows[-INSIGHTS_MAX:]


# --------------------------------------------------------------------------- #
# The store
# --------------------------------------------------------------------------- #
def _read_config():
    from app.config_persist import load_config
    return load_config().get("insights")


class Insights:
    """The bus. One process-wide instance backs the module functions; tests
    construct their own with an in-memory load/save pair and a fake clock."""

    def __init__(self, load: Optional[Callable[[], object]] = None,
                 save: Optional[Callable[[list], None]] = None,
                 clock: Optional[Callable[[], float]] = None):
        if (load is None) != (save is None):
            raise ValueError("load and save must be supplied together")
        self._custom = load is not None
        self._load = load if self._custom else _read_config
        self._save = save
        self._clock = clock or time.time
        self._lock = threading.RLock()

    # ---- storage -------------------------------------------------------- #
    def _read(self, now: float) -> list:
        try:
            return _normalise_rows(self._load(), now)
        except Exception:
            return []

    def _mutate(self, change: Callable[[list], list]) -> bool:
        """Apply a list->list change atomically. False when it did not save."""
        now = self._clock()
        try:
            if self._custom:
                with self._lock:
                    rows = self._read(now)
                    self._save([f.as_dict() for f in change(rows)])
                return True

            from app.config_persist import update_config

            def _mutate_config(cfg):
                rows = _normalise_rows(cfg.get("insights"), now)
                cfg["insights"] = [f.as_dict() for f in change(rows)]
                return cfg

            update_config(_mutate_config)
            return True
        except Exception:
            return False

    # ---- reads ---------------------------------------------------------- #
    def active(self, domain=None, min_severity=None,
               max_age: float = DEFAULT_TTL, now: Optional[float] = None):
        """Usable findings, worst first, newest first within a severity.

        `domain` may be one domain or a collection. An unknown filter value
        raises: this is a programmer-facing query, and silently returning
        everything would hide the typo that made a screen empty.
        """
        now = self._clock() if now is None else float(now)
        if min_severity is not None and min_severity not in SEVERITIES:
            raise ValueError("unknown severity: %r" % (min_severity,))
        if domain is not None:
            wanted = (domain,) if isinstance(domain, str) else tuple(domain)
            bad = [d for d in wanted if d not in DOMAINS]
            if bad:
                raise ValueError("unknown domain: %r" % (bad[0],))
        else:
            wanted = None

        rows = self._read(now)
        by_id = {}
        for finding in rows:
            old = by_id.get(finding.id)
            if old is None or finding.observed_at >= old.observed_at:
                by_id[finding.id] = finding
        floor = SEVERITY_RANK.get(min_severity or "info", 0)
        out = [f for f in by_id.values()
               if _fresh(f, now, float(max_age))
               and SEVERITY_RANK[f.severity] >= floor
               and (wanted is None or f.domain in wanted)]
        out.sort(key=lambda f: (-SEVERITY_RANK[f.severity], -f.observed_at,
                                f.id))
        return out

    # ---- writes --------------------------------------------------------- #
    def publish(self, *findings) -> list:
        """Add or refresh findings. Returns those accepted and stored.

        Accepted rows and storage are deliberately not the same claim: a
        config that cannot be written returns [] even though the call was
        harmless, so a caller never reports \"published\" for a loss.
        """
        now = self._clock()
        clean = [f for f in (normalise(raw, now) for raw in findings)
                 if f is not None]
        if not clean:
            return []
        if not self._mutate(lambda rows: _merge(rows, clean, now)):
            return []
        return clean

    def replace(self, prefix: str, *findings) -> list:
        """Make `prefix` authoritative: drop its old rows, then add these.

        A scan that finds nothing calls this with only the prefix. The prefix
        is required and must name a namespace (``drivetoolkit.drive.``), so a
        typo cannot silently clear the whole board.
        """
        prefix = str(prefix or "").strip()
        if len(prefix) < 3:
            raise ValueError("replace() needs a real id prefix")
        now = self._clock()
        clean = [f for f in (normalise(raw, now) for raw in findings)
                 if f is not None and f.id.startswith(prefix)]
        kept = []

        def _change(rows):
            kept[:] = [f for f in rows if not f.id.startswith(prefix)]
            return _merge(kept, clean, now)

        if not self._mutate(_change):
            return []
        return clean

    def retract(self, *ids) -> int:
        """Remove exact ids. Returns how many rows were actually removed."""
        wanted = {str(i).strip() for i in ids if str(i).strip()}
        if not wanted:
            return 0
        removed = [0]

        def _change(rows):
            kept = [f for f in rows if f.id not in wanted]
            removed[0] = len(rows) - len(kept)
            return kept

        return removed[0] if self._mutate(_change) else 0

    def clear(self) -> bool:
        """Empty the whole board. Used by tests and a future Settings action."""
        return self._mutate(lambda rows: [])


#: The process-wide bus. Importing it measures nothing and reads no file.
_BUS = Insights()


def active(domain=None, min_severity=None, max_age: float = DEFAULT_TTL,
           now: Optional[float] = None):
    return _BUS.active(domain=domain, min_severity=min_severity,
                       max_age=max_age, now=now)


def publish(*findings) -> list:
    return _BUS.publish(*findings)


def replace(prefix: str, *findings) -> list:
    return _BUS.replace(prefix, *findings)


def retract(*ids) -> int:
    return _BUS.retract(*ids)


def clear() -> bool:
    return _BUS.clear()
