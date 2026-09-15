"""
Find My Fastest DNS (user-approved feature 5, 2026-09).

Measures which public DNS resolver answers fastest FOR THIS machine by
timing TCP connects to each resolver's port 53 (the DNS port — every
compliant resolver answers TCP, RFC 7766). No raw sockets, no admin,
stdlib socket only. Median of N probes per resolver; unreachable
resolvers report None instead of a fake number.

The dialog (gui.py) runs these in worker threads and paints results
live; applying the winner reuses tweak_tasks.apply_gaming_dns with an
explicit server pair, so the snapshot/undo contract is untouched.
"""

from __future__ import annotations

import socket
import time

# (display name, primary IPv4, secondary IPv4) — all anycast, all
# TCP-capable. The secondary is shipped in the SAME tuple as its primary
# (F4-1: it used to live in a separate DNS_SECONDARY dict keyed by IP, so
# the pair could drift — e.g. a renamed index — and gui.py imported the
# dict on its own; one row per provider now).
DNS_RESOLVERS = (
    ("Cloudflare", "1.1.1.1", "1.0.0.1"),
    ("Google", "8.8.8.8", "8.8.4.4"),
    ("Quad9", "9.9.9.9", "149.112.112.112"),
    ("OpenDNS", "208.67.222.222", "208.67.220.220"),
    ("AdGuard", "94.140.14.14", "94.140.15.15"),
)

DNS_PORT = 53
PROBE_TIMEOUT_S = 1.5
PROBE_ATTEMPTS = 2


def get_dns_pair(ip: str) -> tuple:
    """(primary, secondary) applied when the user picks `ip`.

    Every known provider keeps its documented secondary in the same row
    as its primary (F4-1: the pair cannot drift by construction); an
    unknown/custom IP falls back to (ip, ip) — a single-address pair the
    setter accepts. Pure."""
    primary = str(ip).strip()
    for _name, prim, sec in DNS_RESOLVERS:
        if prim == primary:
            return (primary, sec)
    return (primary, primary)


def time_resolver(ip: str, timeout: float = PROBE_TIMEOUT_S,
                  attempts: int = PROBE_ATTEMPTS,
                  cancelled=None) -> "float | None":
    """Median TCP-connect latency to ip:53 in milliseconds, or None when
    unreachable. Each attempt opens + closes a fresh socket (no state);
    cancelled() aborts between attempts (dialog-close path)."""
    is_cancelled = cancelled or (lambda: False)
    samples = []
    for _ in range(max(1, attempts)):
        if is_cancelled():
            break
        sock = None
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(timeout)
            t0 = time.perf_counter()
            sock.connect((ip, DNS_PORT))
            samples.append((time.perf_counter() - t0) * 1000.0)
        except OSError:
            pass
        finally:
            try:
                if sock is not None:
                    sock.close()
            except Exception:
                pass
    if not samples:
        return None
    samples.sort()
    mid = len(samples) // 2
    if len(samples) % 2:
        return samples[mid]
    return (samples[mid - 1] + samples[mid]) / 2.0


def get_current_dns(timeout: int = 15) -> list:
    """Currently configured IPv4 DNS servers (unique, order-kept, max 2)
    across Up adapters. Read-only PowerShell; [] on any failure (offline
    machine, stripped SKU) — the dialog then simply omits the 'current'
    row instead of guessing."""
    try:
        import subprocess as _sp
        out = _sp.run(
            ["powershell", "-NoProfile", "-Command",
             "Get-DnsClientServerAddress -AddressFamily IPv4 "
             "-ErrorAction SilentlyContinue | "
             "ForEach-Object { $_.ServerAddresses }"],
            capture_output=True, text=True, timeout=timeout,
            creationflags=getattr(_sp, "CREATE_NO_WINDOW", 0),
        )
    except Exception:
        return []
    seen = []
    for line in (out.stdout or "").splitlines():
        ip = line.strip()
        parts = ip.split(".")
        if (len(parts) == 4 and all(p.isdigit() and 0 <= int(p) <= 255 for p in parts)
                and ip not in seen):
            seen.append(ip)
        if len(seen) >= 2:
            break
    return seen


def pick_winner(results: dict) -> "str | None":
    """IP with the lowest latency among {ip: ms-or-None}; None when
    nothing answered. Pure function — unit-tested."""
    best, best_ms = None, None
    for ip, ms in results.items():
        if ms is None:
            continue
        if best_ms is None or ms < best_ms:
            best, best_ms = ip, ms
    return best


def is_private_ip(ip: str) -> bool:
    """True for non-publicly-testable IPv4: RFC1918, loopback,
    link-local, reserved, or malformed. Router DNS servers
    (192.168.x.x etc.) are forwarders that rarely answer TCP/53, so
    timing them reports a scary-but-meaningless 'no reply' — the
    tester skips them instead of alarming the user. Safe default:
    anything unparseable is also skipped, never probed blind."""
    try:
        parts = [int(p) for p in ip.strip().split(".")]
        if len(parts) != 4 or any(p < 0 or p > 255 for p in parts):
            return True
        a, b = parts[0], parts[1]
        if a == 10 or a == 127:
            return True
        # L05: CGNAT 100.64/10 (RFC6598) is ISP-internal, not testable.
        if a == 100 and 64 <= b <= 127:
            return True
        if a == 172 and 16 <= b <= 31:
            return True
        if a == 192 and b == 168:
            return True
        if a == 169 and b == 254:
            return True
        if a == 0 or a >= 224:
            return True
        return False
    except Exception:
        return True


def build_targets(current) -> tuple:
    """(targets, skipped): the rows to test + the current-DNS IPs held
    back. Public resolvers always; a current-DNS IP is added (flagged,
    deduped) only when publicly testable. Private/router IPs land in
    `skipped` for the dialog's honesty footnote. Pure — unit-tested."""
    known = {ip for _n, ip, _s in DNS_RESOLVERS}
    targets = [(name, ip, False) for name, ip, _s in DNS_RESOLVERS]
    skipped = []
    for ip in (current or []):
        if is_private_ip(ip):
            if ip not in skipped:
                skipped.append(ip)
            continue
        if ip not in known and ip not in [i for _n, i, _c in targets]:
            targets.append(("Your current DNS", ip, True))
        else:
            targets = [(n, i, True if i == ip else c)
                       for n, i, c in targets]
    return targets, skipped
