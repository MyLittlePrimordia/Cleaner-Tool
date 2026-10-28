"""Winget's progress output, turned into one status-bar line.

Extracted from app/tasks/install_tasks.py (audit 2026-10, R-004). The Install
tab has no live log window, so `_live_status_ctx` mirrors the interesting
lines from `winget upgrade --all` into the strip: the package plan ("2 of 7"),
the download (host + file name, middle-trimmed at a path separator), and the
size, painted once per package.

The two rules that keep this honest on a localized Windows:

  * the COUNTER and the [PackageId] brackets are format strings, not resource
    strings, so the header is matched structurally and the verb winget prints
    ("Found", "Wird heruntergeladen") is never part of a pattern;
  * when the outcome cannot be read, the package names that WERE printed are
    still listed - the log and the card say what changed even when the verdict
    is unknown.

Everything here is pure except `_live_status_ctx` and `_log_unreadable`, which
take a TaskContext and only use its log/set_status callbacks.
"""
from __future__ import annotations

import re
from typing import NamedTuple

from app.utils import TaskContext


def _live_status_ctx(ctx: TaskContext) -> TaskContext:
    """Wrap ctx so winget's own progress lines also reach the status bar.

    The Install tab has no live log window — without this, a 10-minute
    `winget upgrade --all` run shows one static status line. Mirroring the
    interesting lines ('(3/10) Found X …', 'Downloading …', 'Starting
    package install…') keeps the user oriented; everything still lands in
    the full log via the wrapped ctx.log.

    A package's download size is painted ONCE per file: on the first line
    that states it, and not on the lines that follow it with a moved counter.
    Those repeats say the same thing - only winget's "done" figure changed -
    so repainting them would churn the strip and, because only the first paint
    carries the size, drop it again on the next tick."""
    import re as _re
    _progress_re = _re.compile(r"\(\d+\s*/\s*\d+\)")
    _orig_log, _orig_status = ctx.log, ctx.set_status
    plan = (0, 0)  # the (index, total) winget last announced, carried forward
    #: (package index, URL) whose download size the strip already shows.
    sized: "tuple[int, str] | None" = None

    def _log(msg: str) -> None:
        nonlocal plan, sized
        _orig_log(msg)
        try:
            text = msg.strip()
            if text.startswith(">"):
                text = text[1:].strip()
            low = text.lower()
            # H-010: the English words below are RESOURCE strings - a German
            # build says "Wird heruntergeladen" - so they are not the rule,
            # only extra signals. A line carrying a URL is a download line in
            # every language, and the '(N/M)' counter is a format string; those
            # two structural facts are what keep the file name and the size
            # visible on a localized run. The words still cover the lines that
            # have neither ("Starting package install...").
            interesting = (
                _progress_re.search(text) is not None
                or _URL_IN_TEXT_RE.search(text) is not None
                or low.startswith("downloading ")
                or low.startswith("starting package ")
                or low.startswith("successfully installed")
            )
            if interesting:
                url = _URL_IN_TEXT_RE.search(text)
                key = (plan[0], url.group(0) if url else "")
                size = _download_size(text)
                show = size is not None and key != sized
                if size is not None and not show:
                    return      # only winget's "done" figure moved
                status, plan = _winget_status_line(text, plan, show_size=show)
                if show:
                    sized = key
                _orig_status(status)
        except Exception:
            pass

    # H-005: keep the caller's data fields (dry_run, quarantine, notes) — the
    # package names this run collects must reach the caller's `notes` list,
    # not a throwaway one. `derive` shares that list on purpose.
    return ctx.derive(log=_log, set_status=_orig_status)


# The two halves of a winget package block that are NOT translated:
#   '(3/7) Found Notepad++ [Notepad++.Notepad++]'  ->  '(' counter ')' … '[ID]'
# The counter and the brackets come from format strings; only the words
# around them ("Found") are resource strings, so the header is matched
# structurally and the verb is never part of the pattern.
_UPGRADE_COUNTER_RE = re.compile(r"\(\s*(\d+)\s*/\s*(\d+)\s*\)")
_UPGRADE_HEADER_RE = re.compile(r"\(\s*\d+\s*/\s*\d+\s*\)\s*(.*?)\s*\[([^\]]+)\]")
# winget's own error HRESULTs (0x8A15xxxx) survive translation, so a failed
# package is still recognized on a non-English build that prints one.
_WINGET_HRESULT_RE = re.compile(r"\b0x8a15[0-9a-f]{4}\b")
# A download URL, whatever words a localized build put in front of it.
_URL_IN_TEXT_RE = re.compile(r"https?://\S+")
# The figures winget's progress bar prints - the size counter "1.61 MB /
# 1.61 MB" and the percentage - which a captured stream can leave on the end
# of the download line.
_PROGRESS_TAIL_RE = re.compile(
    r"\s+(?:\d+(?:\.\d+)?\s*[KMGT]?B\s*/\s*\d+(?:\.\d+)?\s*[KMGT]?B"
    r"|\d{1,3}\s*%)$",
    re.IGNORECASE)
#: The pair of figures themselves - "<done> / <total>" - capturing the total.
_SIZE_PAIR_RE = re.compile(
    r"\d+(?:\.\d+)?\s*[KMGT]?B\s*/\s*(\d+(?:\.\d+)?\s*[KMGT]?B)",
    re.IGNORECASE)


def _winget_status_line(text: str,
                       plan: "tuple[int, int]" = (0, 0),
                       show_size: bool = True,
                       ) -> "tuple[str, tuple[int, int]]":
    """(status-bar text, plan) for one winget output line.

    winget states the size of the job on every package header - '(3/7)' means
    it plans to work through 7 packages and is on the third - and that is the
    only total it ever gives: `winget upgrade --all` has no dry-run summary.
    The plan is returned as (index, total) and handed back in by the caller,
    so the download and "Successfully installed" lines that follow a header
    keep saying "Updating 3 of 7 apps" instead of dropping the count until
    the next package starts. Only the BODY is trimmed to fit the strip, and
    a body carrying a download URL is trimmed through its middle so the file
    name - the only part that differs between files in one release - still
    reads (see _trim_body).

    winget's progress figures are dropped from the body first (see
    _without_progress_tail): the "done" half is stale as soon as it is
    painted. The size the pair ends on is put back as "(1.61 MB)" - a fact
    about the file, not a counter, so it still reads true minutes later - and
    `show_size` lets the caller paint it once per file instead of on every
    line that repeats the same download.
    """
    raw = " ".join(text.split())
    counter = _UPGRADE_COUNTER_RE.search(raw)
    header = _UPGRADE_HEADER_RE.search(raw)
    if counter:
        plan = (int(counter.group(1)), int(counter.group(2)))
    if counter and header:
        body = _package_name(header.group(1), header.group(2))
    else:
        body = _without_progress_tail(raw)
    size = _download_size(raw) if show_size else None
    if size:
        body = f"{body} ({size})"
    n, total = plan
    prefix = (f"Updating {n} of {total} app{'' if total == 1 else 's'}: "
              if total else "")
    return prefix + _trim_body(body, 90 - len(prefix)), plan


def _without_progress_tail(body: str) -> str:
    """`body` without the download progress figures winget appends to a URL.

    winget's bar prints "1.61 MB / 1.61 MB" while the file arrives, and a
    captured stream can leave that on the download line. It is dropped from
    the STRIP - never from the log, which keeps winget's line verbatim -
    because nothing repaints the status until the next package starts, by
    which time the "done" figure is stale. Only that figure is dropped; any
    other words winget prints are kept, and the size the pair ends on is put
    back by `_winget_status_line` as the file's size (see _download_size).
    """
    if _URL_IN_TEXT_RE.search(body) is None:
        return body
    return _PROGRESS_TAIL_RE.sub("", body)


def _download_size(text: str) -> "str | None":
    """The size winget states for the file on this line, or None.

    The bar prints "<downloaded> / <total>" while a file arrives, so the
    right-hand figure is the size of the download - the one number there that
    does not move as the file arrives. Read only from a line carrying a URL: a
    package name that ends in a size is not a download. A percentage on its
    own states no size, and a line with no figures gives None.
    """
    if _URL_IN_TEXT_RE.search(text) is None:
        return None
    pairs = _SIZE_PAIR_RE.findall(text)
    return " ".join(pairs[-1].split()) if pairs else None


def _trim_body(body: str, width: int) -> str:
    """`body` cut to `width`, keeping the part that says WHICH thing it is.

    winget prints a download URL so the user can see which file is arriving,
    and the file name is the only segment that differs between the files of
    one release - the repository path in front of it is shared. An end trim
    kept that path and threw the file name away, so a body carrying a URL
    loses its MIDDLE instead. Package names are the other way round: their
    identifying words come first, so they keep the plain end trim.

    The words winget printed in front of the URL are never the part that goes:
    they say what is happening ("Downloading"), and on a localized build they
    say it in the user's own language. The URL shortens to leave them room
    (see the sixty-ninth pass in AUDIT-2026-10.md).
    """
    if len(body) <= width:
        return body
    url = _URL_IN_TEXT_RE.search(body)
    if url is None:
        return _trim_to_strip(body, width)
    lead, link, rest = body[:url.start()], url.group(0), body[url.end():]
    room = width - len(lead) - len(rest)
    if room < 8:          # too little left for a URL to identify anything
        return _trim_to_strip(body, width)
    return lead + _trim_url(link, room) + rest


def _trim_url(url: str, width: int) -> str:
    """`url` cut in the MIDDLE, at a path separator, so its file name reads.

    "https://github.com/…/npp.8.6.9.Installer.x64.exe" - the host stays
    legible on the left, the file name whole on the right, and every fragment
    between them is a WHOLE directory: a cut landing mid-segment reads as a
    typo ("…/notepad-…") rather than as a shortened path. The head ends at
    the last separator that fits, so the visible line can come out a few
    characters short of the strip. When the file name alone is wider than the
    room there is, the tail wins and the front goes.

    The front prefers the scheme, then the host, then the host WITHOUT its
    scheme: a size on the same line can leave room for only one of them, and
    "github.com/…/npp.8.6.9.Installer.x64.exe" still says where the file comes
    from - which is the part the file name cannot.

    The scheme is kept whenever both it and the host fit. Dropping it
    unconditionally would save eight characters on every line and give back at
    most the intermediate directories of a deep path - text the file name
    already identifies better - while every short line would lose the only
    place the strip still quotes winget's URL as winget printed it (see the
    sixty-eighth pass in AUDIT-2026-10.md).
    """
    if len(url) <= width:
        return url
    if width <= 1:
        return "…"
    cut = url.rfind("/")
    tail = url[cut:] if cut >= 0 else ""
    # one segment only: a "tail" of "//host" is not a file name to anchor on
    if len(tail) > 1 and tail.count("/") == 1 and len(tail) <= width - 9:
        scheme_end = url.find("://")
        host_at = scheme_end + 3 if scheme_end >= 0 else 0
        host_end = url.find("/", host_at)   # first slash after host
        boundary = url[:width - len(tail) - 2].rfind("/")
        if boundary > host_end:                # a directory below the host
            return url[:boundary] + "/…" + tail
        if host_end >= 0 and host_end + 2 + len(tail) <= width:
            return url[:host_end] + "/…" + tail     # scheme and host both fit
        host = url[host_at:host_end] if host_end >= 0 else ""
        if host and len(host) + 2 + len(tail) <= width:
            return host + "/…" + tail            # the host, without its scheme
        return "…" + tail                          # the file name alone
    return "…" + url[-(width - 1):]


def _trim_to_strip(text: str, width: int) -> str:
    """`text` cut to `width` characters, ellipsis included."""
    if len(text) <= width:
        return text
    if width <= 1:
        return "…"
    return text[:width - 1].rstrip() + "…"


class UpgradeSummary(NamedTuple):
    """Per-package result of one `winget upgrade --all` run.

    `unreadable` is the third bucket, and the honest one. winget prints its
    per-app lines in the Windows display language, so a German/French/… run
    hands this parser headers it can still find (see _UPGRADE_HEADER_RE) and
    outcome lines it cannot read ("Successfully installed" is a resource
    string). Those packages are counted, not dropped: dropping them is what
    made every localized run report "all apps current" over an upgrade that
    had just happened.

    `unreadable_names` names them, because "2 results could not be read" over
    a log the user cannot match to anything is only half a report. The count
    can exceed the names: it falls back to winget's own '(N/M)' counter when
    not one header parsed, and a header is where a name comes from.
    """
    updated: "list[str]"
    failed: "list[str]"
    unreadable: int = 0
    unreadable_names: "tuple[str, ...]" = ()


def _summarize_upgrade_lines(lines: "list[str]") -> UpgradeSummary:
    """Split collected `winget upgrade --all` output into updated / failed /
    unreadable packages. Tracks the current '(N/M) … NAME [ID]' header;
    'Successfully installed' credits it, an exit-code or winget HRESULT line
    blames it, and a header whose block ends without either is counted as
    unreadable (localized build) rather than as nothing at all."""
    updated: "list[str]" = []
    failed: "list[str]" = []
    unread_names: "list[str]" = []
    current: "str | None" = None
    headers = 0
    unreadable = 0
    planned = 0  # winget's own package total, from the last '(N/M)' counter
    for line in lines:
        counter = _UPGRADE_COUNTER_RE.search(line)
        if counter:
            planned = max(planned, int(counter.group(2)))
        m = _UPGRADE_HEADER_RE.search(line)
        if m:
            if current is not None:
                # The previous block ended without a line we could read.
                unreadable += 1
                unread_names.append(current)
            headers += 1
            current = _package_name(m.group(1), m.group(2))
            continue
        low = line.lower()
        if "successfully installed" in low:
            if current and current not in updated:
                updated.append(current)
            current = None
        elif "failed with exit code" in low or _WINGET_HRESULT_RE.search(low):
            if current and current not in failed:
                failed.append(f"{current} ({line.strip()})")
            current = None
    if current is not None:
        unreadable += 1
        unread_names.append(current)
    if not headers and not updated and not failed and planned:
        # No header survived the parse, but winget's counter says it worked
        # through packages: report them unread, never as "nothing to do".
        unreadable = planned
    return UpgradeSummary(updated, failed, unreadable, tuple(unread_names))


def _id_anchors(name: str, package_id: str) -> bool:
    """Does `name` still refer to `package_id`?

    The package ID is untranslated, so it is the one part of a header a
    localized build cannot lie about. Only used to tell a translated verb
    ("Gefunden") from the first word of a real app name, so a match on any
    tail of the ID's components is enough.
    """
    flat = "".join(ch for ch in name.lower() if ch.isalnum())
    parts = ["".join(ch for ch in p.lower() if ch.isalnum())
             for p in package_id.split(".")]
    parts = [p for p in parts if p]
    for k in range(len(parts), 0, -1):
        tail = "".join(parts[-k:])
        if len(tail) >= 3 and not tail.isdigit() and tail in flat:
            return True
    return False


def _package_name(raw: str, package_id: str) -> str:
    """Display name from a header's '(N/M) <verb> NAME [ID]' middle part.

    English builds print "Found", which is stripped. Other locales put their
    own word there; rather than carry a translation table, one leading word is
    dropped only when the rest still names the package (see _id_anchors).
    When it does not, winget's own wording is kept rather than guessed at —
    it is what the lines above the report already say.
    """
    name = " ".join(raw.split())
    if name[:6].lower() == "found ":
        return name[6:].strip() or package_id.strip()
    rest = name.partition(" ")[2]
    if rest and _id_anchors(rest, package_id):
        return rest
    return name or package_id.strip()


def _unreadable_note(summary: UpgradeSummary) -> str:
    """The same list, shaped for a completion card body instead of a log."""
    head = (f"{summary.unreadable} update result"
            f"{'' if summary.unreadable == 1 else 's'} could not be read — "
            f"winget reports in the Windows display language")
    if not summary.unreadable_names:
        return head + "."
    listed = "\n".join(f"  - {name}" for name in summary.unreadable_names)
    return f"{head}:\n{listed}"


def _log_unreadable(ctx: TaskContext, summary: UpgradeSummary) -> None:
    """Record the packages whose per-app result could not be read.

    On a localized build the block can only say how many results were
    unreadable; without these lines the user has no way to tell WHICH apps
    the run touched, even though its own output is a few lines above.

    The same list also goes on `ctx.notes`: the windowed install runner's
    completion card is built from counts, and it is the only place a run
    started from the Install tab shows a result at all.
    """
    if not summary.unreadable:
        return
    names = summary.unreadable_names
    ctx.log(f"  [WARN] {summary.unreadable} package(s) went through winget, but "
            f"their result line could not be read — it reports in the Windows "
            f"display language"
            + (":" if names else "; the lines above are the record."))
    for name in names:
        ctx.log(f"    - {name}")
    ctx.notes.append(_unreadable_note(summary))


#: The one status line for a `winget upgrade --all` run. The Install tab sets
#: it before the job starts and `update_all_apps` sets it again as the command
#: begins; the two land one after the other, so two sentences would only
#: flicker past each other. Defined here because the task owns the action.
