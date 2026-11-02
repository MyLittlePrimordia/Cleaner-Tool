"""One bounded JSON GET for the three callers that parse HTTP responses.

Audit 2026-11, I-001. `update_checker`, `gameping` and
`install_tasks._resolve_github_latest_asset` each opened a request, read the
whole body and parsed it, three times over. Consolidating them is not only
tidiness: none of the three capped the body, so an origin could stream an
unbounded response into memory (the same class LOW-005 fixed for downloads,
where the ceiling is now enforced while writing).

Each caller keeps its own error policy — every one of them already wraps this
in `try/except` and decides whether to log, fall back or return None. What is
shared is the mechanics: the request, the byte ceiling, the status check and
the decode.

`errors` is a parameter because the callers genuinely differ: the GitHub
release feed is decoded strictly (a malformed body should fail loudly), while
the Valve directory and the installer catalog tolerate replacement characters
and keep whatever parsed.
"""
from __future__ import annotations

import json
import urllib.request

#: Generous on purpose. A GitHub `/releases` feed with a hundred releases and
#: their assets is well under this; the cap exists so a hostile or broken
#: origin cannot exhaust memory, not to police normal payloads.
MAX_JSON_BYTES = 8 * 1024 * 1024


def get_json(url: str, *, headers=None, timeout: int = 15,
             max_bytes: int = MAX_JSON_BYTES,
             errors: str = "replace",
             expect_status: "int | None" = 200):
    """GET `url` and return the parsed JSON body.

    Raises on transport failure, on a status other than `expect_status`
    (pass None to accept whatever `urlopen` returned) and on a body larger
    than `max_bytes` or that does not parse. Callers keep their own
    try/except: this never swallows a failure into a sentinel.
    """
    req = urllib.request.Request(url, headers=dict(headers or {}))
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        status = getattr(resp, "status", 200)
        if expect_status is not None and status != expect_status:
            raise RuntimeError("unexpected HTTP status %s" % status)
        # read ceiling+1 so an over-size body is detected without ever
        # holding more than the cap.
        raw = resp.read(max_bytes + 1)
    if len(raw) > max_bytes:
        raise RuntimeError("response larger than %d bytes" % max_bytes)
    return json.loads(raw.decode("utf-8", errors))
