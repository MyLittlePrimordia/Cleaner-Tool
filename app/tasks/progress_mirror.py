"""U-006: status-strip mirrors for the long poles beyond winget.

`winget_status._live_status_ctx` proved the pattern on `winget upgrade
--all`: mirror the tool's own progress lines into the status strip so a
run that sits for minutes does not look hung. DISM, SFC and chkdsk are
the same shape of problem — multi-minute runs whose only live output is
a percentage buried in the hidden log — so this module generalizes the
wrapper: `mirror_ctx(ctx, label, rules)` rewords matching lines into the
strip and leaves everything else (including dry_run, quarantine, notes)
exactly where `derive` keeps it.

Honesty rules, carried over from the winget mirror:

* only the tool's own printed numbers reach the strip — a line matching
  nothing never touches it, so the strip can never claim progress the
  tool did not print;
* percentages are matched structurally (`NN%`), never by English verbs,
  so a localized DISM/SFC paints the same strip;
* repeats of one integer percent paint once (dedup), so a bar that
  redraws at 12% fifty times does not churn the strip;
* out-of-range numbers (>100) are ignored, never painted.
"""
from __future__ import annotations

import re

#: A bare percentage anywhere in the line — DISM's `[===== 12.3% ]`,
#: SFC's `Verification 45% complete.`, chkdsk's stage percents. The words
#: around it are resource strings; the number is a format string.
_PERCENT_RE = re.compile(r"(?P<pct>\d{1,3}(?:\.\d+)?)\s*%")


def _pct(label):
    """Rule formatter: `match -> strip line` for `label`, None to skip."""
    def _fmt(m):
        try:
            value = float(m.group("pct"))
        except (ValueError, IndexError, AttributeError):
            return None
        if not 0 <= value <= 100:
            return None
        return ("%s: %d%% — still working; holding one number for "
                "minutes is normal." % (label, int(value)))
    return _fmt


#: Ready-made rule sets. One tuple per tool so a call site reads as
#: `mirror_ctx(ctx, "DISM", DISM_RULES)` with no regex at the task.
DISM_RULES = ((_PERCENT_RE, _pct("DISM")),)
SFC_RULES = ((_PERCENT_RE, _pct("SFC")),)
CHKDSK_RULES = ((_PERCENT_RE, _pct("chkdsk")),)


def mirror_ctx(ctx, label, rules):
    """Wrap `ctx` so lines matching `rules` also reach the status strip.

    `rules` is an iterable of (compiled pattern, formatter); the first
    matching rule wins. Everything still lands in the full log via the
    wrapped `ctx.log`. Never raises out of the log path — a mirror
    failure must not cost the run its own log line.
    """
    _orig_log, _orig_status = ctx.log, ctx.set_status
    _rules = list(rules or [])
    _label = str(label or "")
    last = [None]

    def _log(msg: str) -> None:
        _orig_log(msg)
        try:
            text = msg.strip()
            if text.startswith(">"):
                text = text[1:].strip()
            for pattern, fmt in _rules:
                try:
                    m = pattern.search(text)
                except Exception:
                    continue
                if not m:
                    continue
                try:
                    status = fmt(m)
                except Exception:
                    status = None
                if status and status != last[0]:
                    last[0] = status
                    _orig_status(status)
                break
        except Exception:
            pass

    # derive (not a rebuild): dry_run, quarantine and notes stay shared
    # with the caller — see TaskContext.derive / H-005.
    return ctx.derive(log=_log, set_status=_orig_status)
