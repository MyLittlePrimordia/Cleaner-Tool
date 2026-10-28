"""N-005: one file that answers the questions a bug report needs.

"Export logs" already writes the in-memory run log plus the diagnostics header
and the swallowed-failure trail. What it does NOT carry is the thing half of
all bug reports actually turn on: the *configuration* the user was running
(which cleaners, which toggles), what the last run did, and whether a scheduled
run was missed. On the machine behind this audit, the answer was sitting in
three files nobody had asked for.

So a bundle is a zip of small, plain-text files:

  diagnostics.txt  versions, Windows build, elevation, recent crash log
  applog.txt       the in-memory log of this session (what the user saw)
  events.txt       the swallowed-failure trail (elevation, ops, snapshots)
  config.txt       the settings, as key = value, bulky ledgers left out
  last_run.txt     the newest recorded run and what it changed
  health.txt       the score ledger (the trend, not the whole history file)
  manifest.txt     what is in the zip, and why each file is there

Two rules hold it together:

  * **Redaction happens once, here.** Every text goes through
    `logsafe.redact` on the way in, so a new producer cannot leak the elevation
    token or the account name by forgetting to scrub.
  * **A broken source cannot break the bundle.** Each producer is wrapped: a
    section that cannot be read says so in its own file. A support bundle that
    fails because one trail is locked is worse than useless.

The bundle is built in memory first, so the dialog can show the exact file list
and sizes before anything is written. Nothing here uploads anything: the user
chooses where the zip lands.
"""
from __future__ import annotations

import io
import os
import platform
import sys
import time
import zipfile
from dataclasses import dataclass

#: Per-file ceiling. The trails are already capped by their writers; this is the
#: belt to that braces, so one pathological log cannot produce a 200 MB zip.
MAX_TEXT_CHARS = 400_000

#: Ledgers that are summarised in their own file (or would dwarf the rest).
#: Their names are reported in config.txt so the reader knows they were there.
_BULKY_KEYS = ("run_history", "health_history")


@dataclass
class BundleFile:
    """One plain-text member of the bundle."""

    name: str
    why: str
    text: str

    @property
    def size(self) -> int:
        return len(self.text.encode("utf-8", "replace"))


def _redact(text: str) -> str:
    try:
        from app.logsafe import redact
        return redact(text)
    except Exception:
        return str(text or "")


def _clip(text: str, limit: int = MAX_TEXT_CHARS) -> str:
    text = str(text or "")
    if len(text) <= limit:
        return text
    return (text[:limit]
            + "\n\n... truncated at %d characters; the tail was dropped.\n"
            % limit)


def _section(name: str, why: str, producer) -> BundleFile:
    """Build one member; a raising producer becomes a readable explanation."""
    try:
        text = producer()
    except Exception as exc:
        text = ("This section could not be read on this machine: %s: %s\n"
                % (type(exc).__name__, exc))
    return BundleFile(name=name, why=why, text=_clip(_redact(text)))


# --------------------------------------------------------------- producers

def _diagnostics_text() -> str:
    from app.diagnostics import diagnostics_text
    return diagnostics_text()


def _applog_text(in_memory_log) -> str:
    lines = list(in_memory_log or ())
    if not lines:
        return ("No in-memory log was handed to the bundle (the app was not "
                "running when it was built, or the session had just started).\n")
    return "\n".join(str(line) for line in lines) + "\n"


def _events_text() -> str:
    from app.config_events import read_events
    events = read_events(500)
    if not events:
        return "No swallowed failures were recorded.\n"
    return "\n".join(events) + "\n"


def _config_text() -> str:
    """Settings as sorted ``key = value`` lines, minus the big ledgers.

    The ledgers are not redacted away - they are *summarised* in their own
    files, and named here so a reader knows the config held more than this.
    """
    from app.config_persist import load_config
    cfg = dict(load_config() or {})
    omitted = [key for key in _BULKY_KEYS if key in cfg]
    lines = ["# Cleaner Tool settings at the time of the report.",
             "# Not included: " + (", ".join(omitted) if omitted
                                   else "nothing was omitted") + ".",
             ""]
    for key in sorted(cfg):
        if key in _BULKY_KEYS:
            continue
        value = cfg[key]
        if isinstance(value, (list, dict)):
            lines.append("%s = <%s with %d item(s)>"
                         % (key, type(value).__name__, len(value)))
        else:
            lines.append("%s = %s" % (key, value))
    return "\n".join(lines) + "\n"


def _last_run_text() -> str:
    from app.history import kind_label, runs
    entries = runs()
    if not entries:
        return "No completed run has been recorded yet.\n"
    last = entries[-1]
    when = time.strftime("%Y-%m-%d %H:%M:%S",
                         time.localtime(float(last.get("t") or 0)))
    lines = ["Newest recorded run",
             "=" * 60,
             "When:  %s" % when,
             "Kind:  %s" % kind_label(last.get("k")),
             "Freed: %s bytes" % int(last.get("b") or 0)]
    changes = last.get("c") or []
    if changes:
        lines += ["", "Changes it made (%d):" % len(changes)]
        for change in changes:
            if not isinstance(change, dict):
                continue
            label = change.get("l") or change.get("p") or "(unnamed)"
            delta = change.get("d") or ""
            lines.append("  - %s: %s" % (label, delta))
    else:
        lines += ["", "It recorded no typed changes (older runs kept none)."]
    return "\n".join(lines) + "\n"


def _health_text() -> str:
    from app.health_history import entries
    points = entries()
    if not points:
        return "No checkup has been recorded yet.\n"
    lines = ["Checkup ledger (oldest first)", "=" * 60,
             "%d recorded point(s)" % len(points), ""]
    for point in points[-20:]:
        stamp = time.strftime("%Y-%m-%d %H:%M",
                              time.localtime(float(point.get("t") or 0)))
        lines.append("  %s  score=%s  grade=%s"
                     % (stamp, point.get("s", "?"), point.get("g", "?")))
    if len(points) > 20:
        lines.insert(4, "  (showing the newest 20 of %d)" % len(points))
    return "\n".join(lines) + "\n"


def _manifest_text(files) -> str:
    from app.config import APP_NAME, APP_VERSION
    lines = ["%s support bundle" % APP_NAME,
             "=" * 60,
             "App version: %s" % APP_VERSION,
             "Created: %s" % time.strftime("%Y-%m-%d %H:%M:%S"),
             "Python: %s" % sys.version.split()[0],
             "Platform: %s" % platform.platform(),
             "",
             "Files in this zip:",
             ""]
    for item in files:
        lines.append("  %-16s %7s bytes  %s"
                     % (item.name, item.size, item.why))
    lines += ["",
              "Every file here was passed through the app's redaction rules: "
              "the",
              "elevation token and the Windows account name are removed. The "
              "zip",
              "was written on this PC and is not uploaded anywhere.",
              ""]
    return "\n".join(lines)


# -------------------------------------------------------------------- api

def build(in_memory_log=None) -> "list[BundleFile]":
    """The bundle's files, redacted and clipped, ready to preview or zip."""
    files = [
        _section("diagnostics.txt",
                 "versions, Windows build, elevation, recent crashes",
                 _diagnostics_text),
        _section("applog.txt",
                 "the log of this session, as the user saw it",
                 lambda: _applog_text(in_memory_log)),
        _section("events.txt",
                 "swallowed failures (elevation, ops, snapshots)",
                 _events_text),
        _section("config.txt",
                 "the settings in effect; bulky ledgers left out",
                 _config_text),
        _section("last_run.txt",
                 "the newest run and what it changed",
                 _last_run_text),
        _section("health.txt",
                 "the checkup ledger behind the Health trend",
                 _health_text),
    ]
    # The manifest names itself last, and only after the sizes are known.
    files.append(_section("manifest.txt",
                          "this list: what is here, and why",
                          lambda: _manifest_text(files)))
    return files


def summary_lines(files) -> "list[str]":
    """One line per file, for the preview before writing.

    Names and sizes only: the dialog has to stay readable, and the manifest in
    the zip carries the reasons.
    """
    return ["%s  (%s)" % (item.name, human_size(item.size))
            for item in files]


def total_bytes(files) -> int:
    return sum(item.size for item in files)


def human_size(count: int) -> str:
    size = float(count)
    if size < 1024:
        return "%d B" % int(size)
    size /= 1024.0
    if size < 1024:
        return "%.1f KB" % size
    return "%.1f MB" % (size / 1024.0)


def default_name(now=None) -> str:
    stamp = time.strftime("%Y%m%d_%H%M%S",
                          time.localtime(time.time() if now is None else now))
    return "CleanerTool_Support_%s.zip" % stamp


def write(files, path: str) -> str:
    """Write the zip. Returns `path`; raises if the file cannot be written."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for item in files:
            archive.writestr(item.name, item.text)
    directory = os.path.dirname(os.path.abspath(path))
    if directory:
        os.makedirs(directory, exist_ok=True)
    with open(path, "wb") as handle:
        handle.write(buffer.getvalue())
    return path
