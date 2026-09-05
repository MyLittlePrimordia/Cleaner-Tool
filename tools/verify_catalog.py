"""Verify the Install-tab catalog: every winget ID resolves, every URL is sane.

Offline checks (fast, always run):
  * duplicate IDs (case-insensitive) across APP_CATALOG + MANUAL_ONLY_APPS
  * winget ID shape: dotted (Publisher.Package) or 12-char Store ID
  * URL shape: https scheme + host (both `url` and `fallback_url`)
  * every catalog `category` present in CATEGORY_ORDER

Live checks (--live, slow: one `winget search --exact --id` per app):
  * reports IDs winget no longer resolves (renames/removals like the
    MartiCliment.UniGetUI -> Devolutions.UniGetUI move) so they can be fixed
    instead of failing at install time with 'No package found'.

Results stream to stdout (flushed per app, so a kill still leaves partial
data) and a report file is written incrementally.

Run:
  python tools/verify_catalog.py            # offline only
  python tools/verify_catalog.py --live     # offline + live winget check
"""

import subprocess
import sys
import time
import urllib.parse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.app_catalog import APP_CATALOG, MANUAL_ONLY_APPS, CATEGORY_ORDER  # noqa: E402

REPORT = Path(__file__).resolve().parents[1] / "shots" / "catalog_verify.txt"


def _is_store_id(app_id: str) -> bool:
    """Microsoft Store ID: legacy 12-char alphanumeric (9NBLGGH30XJ3) or the
    newer XP-prefixed form (XP8CLZL93F5Z4P, e.g. NVIDIA App)."""
    if len(app_id) == 12 and all(c.isalnum() for c in app_id):
        return True
    return (app_id.upper().startswith("XP") and 9 <= len(app_id) <= 16
            and all(c.isalnum() for c in app_id))


def offline_checks() -> "tuple[list[str], list[str]]":
    """Returns (problems, warnings). Warnings (e.g. http-only vendor pages
    with no https available) are reported but don't fail the check."""
    problems: "list[str]" = []
    warnings: "list[str]" = []
    seen: dict = {}
    all_apps = [("catalog", a) for a in APP_CATALOG] + [("manual", m) for m in MANUAL_ONLY_APPS]
    for src, app in all_apps:
        aid = app.get("id", "")
        name = app.get("name", "?")
        key = aid.lower()
        if key in seen:
            problems.append(f"DUPLICATE ID {aid!r}: {name!r} ({src}) already listed as {seen[key]!r}")
        else:
            seen[key] = f"{name} ({src})"
        if aid.startswith("manual:"):
            if src != "manual":
                problems.append(f"manual: ID in catalog (not MANUAL_ONLY_APPS): {aid!r}")
        else:
            if not ("." in aid or _is_store_id(aid)):
                problems.append(f"BAD ID SHAPE {aid!r} ({name}) — expected Publisher.Package or 12-char Store ID")
        for field in ("url", "fallback_url"):
            url = app.get(field, "")
            if field == "url" and not url:
                problems.append(f"MISSING url: {name} ({aid})")
                continue
            if not url:
                continue
            try:
                parts = urllib.parse.urlparse(url)
            except Exception as exc:
                problems.append(f"UNPARSEABLE {field} for {name}: {url} ({exc})")
                continue
            if parts.scheme != "https" or not parts.netloc or "." not in parts.netloc:
                # ridgecrop.co.uk serves http ONLY (verified: https fails) —
                # it is the vendor's real page, opened in the browser as a
                # fallback link, never auto-downloaded. Warn, don't fail.
                if parts.scheme == "http" and parts.netloc:
                    warnings.append(f"HTTP-ONLY {field} for {name} ({aid}): {url} "
                                    f"(vendor has no https; browser link only)")
                else:
                    problems.append(f"BAD {field} for {name} ({aid}): {url!r}")
        if app.get("category") not in CATEGORY_ORDER:
            problems.append(f"UNKNOWN CATEGORY {app.get('category')!r} for {name} ({aid})")
    return problems, warnings


def winget_search(app_id: str, source: str = "") -> "tuple[str, str]":
    """Returns (status, detail): FOUND / NOT_FOUND / ERROR."""
    cmd = ["winget", "search", "--exact", "--id", app_id,
           "--accept-source-agreements", "--disable-interactivity"]
    if source:
        cmd += ["--source", source]
    try:
        out = subprocess.run(
            cmd, capture_output=True, text=True, timeout=60,
            errors="replace",
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except FileNotFoundError:
        return "ERROR", "winget not on PATH"
    except subprocess.TimeoutExpired:
        return "ERROR", "timeout after 60s"
    except Exception as exc:
        return "ERROR", f"{type(exc).__name__}: {exc}"
    blob = (out.stdout or "") + (out.stderr or "")
    if out.returncode == 0 and app_id.lower() in (out.stdout or "").lower():
        return "FOUND", ""
    if "no package found matching input criteria" in blob.lower():
        return "NOT_FOUND", ""
    if out.returncode == 0:
        return "FOUND", "rc=0 (id not echoed)"
    return "ERROR", f"rc={out.returncode}: {(blob.strip().splitlines() or ['?'])[0][:120]}"


def live_checks() -> "list[str]":
    from app.app_catalog import MSSTORE_IDS

    problems: "list[str]" = []
    winget_apps = [a for a in APP_CATALOG if not a["id"].startswith("manual:")]
    print(f"live: checking {len(winget_apps)} IDs via `winget search --exact --id` ...", flush=True)
    for i, app in enumerate(winget_apps, 1):
        aid = app["id"]
        status, detail = winget_search(aid)
        if status == "FOUND" or (status == "ERROR" and "winget not on PATH" in detail):
            if "winget not on PATH" in detail:
                print("live: winget not available — aborting live check", flush=True)
                problems.append("LIVE ABORTED: winget not on PATH")
                break
        elif status == "NOT_FOUND" and aid in MSSTORE_IDS:
            # Store IDs sometimes only resolve against the msstore source
            status2, detail2 = winget_search(aid, source="msstore")
            status, detail = status2, detail2 or detail
        tag = "ok" if status == "FOUND" else status
        print(f"live [{i}/{len(winget_apps)}] {tag}: {aid} {detail}", flush=True)
        if status != "FOUND":
            problems.append(f"LIVE {status}: {aid} ({app['name']}) {detail}")
    return problems


def main() -> int:
    live = "--live" in sys.argv[1:]
    report_lines: "list[str]" = [f"catalog verify — {time.strftime('%Y-%m-%d %H:%M:%S')}"]
    off, off_warn = offline_checks()
    report_lines.append(f"--- offline: {len(off)} problem(s), {len(off_warn)} warning(s) ---")
    report_lines.extend(off or ["(none)"])
    report_lines.extend(f"WARN: {w}" for w in off_warn)
    for p in off:
        print("offline:", p, flush=True)
    for w in off_warn:
        print("offline WARN:", w, flush=True)
    print(f"offline: {len(APP_CATALOG)} catalog + {len(MANUAL_ONLY_APPS)} manual apps, "
          f"{len(off)} problem(s)", flush=True)
    if live:
        live_problems = live_checks()
        report_lines.append(f"--- live: {len(live_problems)} problem(s) ---")
        report_lines.extend(live_problems or ["(none)"])
    try:
        REPORT.parent.mkdir(exist_ok=True)
        REPORT.write_text("\n".join(report_lines) + "\n", encoding="utf-8")
        print(f"report -> {REPORT}", flush=True)
    except Exception as exc:
        print(f"could not write report: {exc}", flush=True)
    bad = [p for p in (off + (live_problems if live else [])) if not p.startswith("LIVE ABORTED")]
    print("VERIFY: ALL PASS" if not bad else f"VERIFY: {len(bad)} PROBLEM(S)", flush=True)
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
