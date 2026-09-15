"""
Game Server Ping data layer — "test before you queue".

Two honest measurement methods, both stdlib-only:

* Fortnite regions: ICMP echo via ping.exe to Epic's OFFICIAL per-region
  matchmaking ping endpoints (ping-nae/nac/naw/eu/oce/br/asia/me
  .ds.on.epicgames.com — the same hosts Epic support tells players to
  test). TCP does NOT work there (verified live: connection refused),
  so ICMP it is.
* Company edges (Steam, Xbox, Ubisoft, Rockstar, EA, Riot): TCP
  handshake timing to their public HTTPS endpoints (verified live —
  every one below answered TCP/443 from the dev machine). This
  measures the path to the company's edge, labeled as such.

Reply parsing is locale-tolerant by design: only lines containing the
untranslated `TTL=` token are read, and the first `<int>ms` on them is
the sample. Summary lines ("Average = ...", "Mittelwert = ...") are
never parsed — stats are computed from the samples, so a localized
Windows can't skew the numbers. Anything unparseable degrades to
"no reply", never a fake number.

Custom hosts (`hostname` = ICMP ping, `hostname:port` = TCP) let
players cover anything not built in (CoD, LoL, Battlefield…).
"""

import re
import socket
import subprocess
import time

# Epic's official per-region ping endpoints (Epic support docs point
# players at these very hosts; verified ICMP-responsive 2026).
FORTNITE_REGIONS = (
    ("NA East", "ping-nae.ds.on.epicgames.com"),
    ("NA Central", "ping-nac.ds.on.epicgames.com"),
    ("NA West", "ping-naw.ds.on.epicgames.com"),
    ("Europe", "ping-eu.ds.on.epicgames.com"),
    ("Oceania", "ping-oce.ds.on.epicgames.com"),
    ("Brazil", "ping-br.ds.on.epicgames.com"),
    ("Asia", "ping-asia.ds.on.epicgames.com"),
    ("Middle East", "ping-me.ds.on.epicgames.com"),
)

# VALORANT regions: Riot publishes no test endpoints, so these are
# TCP paths to the AWS region edge nearest each data center (the same
# proxy method the serious ping sites disclose). Every host below
# answered TCP/443 in the live ship-gate probe (Bahrain timed out and
# was dropped). Ranking regions is what matters, not absolute ms.
VALORANT_REGIONS = (
    ("Frankfurt", "ec2.eu-central-1.amazonaws.com", 443),
    ("Paris", "ec2.eu-west-3.amazonaws.com", 443),
    ("London", "ec2.eu-west-2.amazonaws.com", 443),
    ("N. Virginia", "ec2.us-east-1.amazonaws.com", 443),
    ("Ohio", "ec2.us-east-2.amazonaws.com", 443),
    ("Tokyo", "ec2.ap-northeast-1.amazonaws.com", 443),
    ("Singapore", "ec2.ap-southeast-1.amazonaws.com", 443),
    ("Sydney", "ec2.ap-southeast-2.amazonaws.com", 443),
    ("Sao Paulo", "ec2.sa-east-1.amazonaws.com", 443),
    ("Mumbai", "ec2.ap-south-1.amazonaws.com", 443),
)

# Apex Legends regions: AWS GameLift since 2025 (in-game list shows
# AWS codes). Same honest proxy method + probe gate as VALORANT.
APEX_REGIONS = (
    ("N. Virginia", "ec2.us-east-1.amazonaws.com", 443),
    ("Ohio", "ec2.us-east-2.amazonaws.com", 443),
    ("Oregon", "ec2.us-west-2.amazonaws.com", 443),
    ("Frankfurt", "ec2.eu-central-1.amazonaws.com", 443),
    ("Ireland", "ec2.eu-west-1.amazonaws.com", 443),
    ("Tokyo", "ec2.ap-northeast-1.amazonaws.com", 443),
    ("Singapore", "ec2.ap-southeast-1.amazonaws.com", 443),
    ("Sydney", "ec2.ap-southeast-2.amazonaws.com", 443),
    ("Sao Paulo", "ec2.sa-east-1.amazonaws.com", 443),
)

# PUBG regions: same honest AWS-edge proxy as VALORANT/Apex
# (PUBG sessions are cloud-hosted; no official ping targets exist).
# Endpoints already gated live by the shared probe runs.
PUBG_REGIONS = (
    ("N. Virginia", "ec2.us-east-1.amazonaws.com", 443),
    ("Ohio", "ec2.us-east-2.amazonaws.com", 443),
    ("Oregon", "ec2.us-west-2.amazonaws.com", 443),
    ("Frankfurt", "ec2.eu-central-1.amazonaws.com", 443),
    ("Ireland", "ec2.eu-west-1.amazonaws.com", 443),
    ("Sao Paulo", "ec2.sa-east-1.amazonaws.com", 443),
    ("Tokyo", "ec2.ap-northeast-1.amazonaws.com", 443),
    ("Singapore", "ec2.ap-southeast-1.amazonaws.com", 443),
    ("Sydney", "ec2.ap-southeast-2.amazonaws.com", 443),
    ("Mumbai", "ec2.ap-south-1.amazonaws.com", 443),
)

# Minecraft Java flagships: TCP handshake straight to the game port —
# a real game server, not a proxy (both answered live).
MINECRAFT_SERVERS = (
    ("Hypixel", "mc.hypixel.net", 25565),
    ("CubeCraft", "play.cubecraft.net", 25565),
)

# Roblox edge (verified live; api.roblox.com doesn't resolve — dropped).
ROBLOX_EDGES = (
    ("Roblox Web", "www.roblox.com", 443),
    ("Roblox Client", "clientsettings.roblox.com", 443),
)

# Company edges: (display, hostname, tcp port). Every entry answered
# TCP/443 in the live ship-gate probe — see the audit trail. This is
# the path to the company's front door, not a game socket; rows say so.
COMPANY_EDGES = (
    ("Steam (Valve)", "store.steampowered.com", 443),
    ("Xbox Live", "xboxlive.com", 443),
    ("Ubisoft", "ubisoft.com", 443),
    ("Rockstar Social Club", "socialclub.rockstargames.com", 443),
    ("EA", "ea.com", 443),
    ("Riot Games", "riotgames.com", 443),
    ("Epic Games", "epicgames.com", 443),
    ("Bungie (Destiny 2)", "www.bungie.net", 443),
)

# Picker tabs in display order: (title, key).
GAME_TABS = (
    ("Fortnite", "fortnite"),
    ("VALORANT", "valorant"),
    ("Apex Legends", "apex"),
    ("PUBG", "pubg"),
    ("Minecraft", "minecraft"),
    ("Roblox", "roblox"),
    ("Company Servers", "companies"),
    ("Custom", "custom"),
)
# Per-tab honesty footnotes (shown under the picker).
GAME_METHODS = {
    "fortnite": "ICMP echo to Epic's official region endpoints.",
    "valorant": "TCP path to the AWS edge nearest each data center — "
                "a proxy; trust the ranking, not the exact ms.",
    "apex": "TCP path to the AWS edge nearest each data center — "
            "a proxy; trust the ranking, not the exact ms.",
    "pubg": "TCP path to the cloud edge nearest each region — "
            "a proxy; trust the ranking, not the exact ms.",
    "minecraft": "TCP handshake to the server's game port — a real server.",
    "roblox": "TCP handshake to the Roblox web/client edge.",
    "companies": "TCP handshake to each company's front door — "
                 "path check, not a game socket.",
    "custom": "Bare hostname = ping · hostname:port = TCP.",
}


def targets_for(key, custom_hosts=()):
    """[(row_key, title, kind, host, port_or_None)] for a tab key."""
    if key == "fortnite":
        return [(n, n, "icmp", h, None) for n, h in FORTNITE_REGIONS]
    if key == "valorant":
        return [(n, n, "tcp", h, p) for n, h, p in VALORANT_REGIONS]
    if key == "apex":
        return [(n, n, "tcp", h, p) for n, h, p in APEX_REGIONS]
    if key == "pubg":
        return [(n, n, "tcp", h, p) for n, h, p in PUBG_REGIONS]
    if key == "minecraft":
        return [(n, n, "tcp", h, p) for n, h, p in MINECRAFT_SERVERS]
    if key == "roblox":
        return [(n, n, "tcp", h, p) for n, h, p in ROBLOX_EDGES]
    if key == "companies":
        return [(n, n, "tcp", h, p) for n, h, p in COMPANY_EDGES]
    if key == "custom":
        out = []
        for raw in list(custom_hosts or [])[:64]:
            host, port = split_custom(raw)
            if not host:
                continue
            if port is None:
                out.append((raw, raw, "icmp", host, None))
            else:
                out.append((raw, raw, "tcp", host, port))
        return out
    return []

# Verdict bands (upper bound ms, word). 125 ms reads Poor — matches the
# folk scale players already know from ping-test sites.
VERDICT_BANDS = (
    (30, "Tournament"),
    (60, "Excellent"),
    (100, "Playable"),
    (float("inf"), "Poor"),
)

_HOST_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9.-]{0,251}[A-Za-z0-9])?$")
_MS_RE = re.compile(r"(\d+)\s*ms", re.IGNORECASE)


def clean_host(raw):
    """Normalized hostname or '' (rejects shell metachars outright —
    the value still always travels argv-style, never through a shell)."""
    try:
        h = (raw or "").strip().lower()
        if h.startswith("[") and h.endswith("]"):
            h = h[1:-1]
        if not h or not _HOST_RE.match(h):
            return ""
        return h
    except Exception:
        return ""


def split_custom(raw):
    """'host' -> (host, None=ICMP) | 'host:port' -> (host, port)."""
    try:
        s = (raw or "").strip()
        if s.count(":") == 1 and not s.startswith("["):
            host, _, port_s = s.partition(":")
            host = clean_host(host)
            port = int(port_s.strip())
            if host and 1 <= port <= 65535:
                return host, port
            return "", None
        host = clean_host(s)
        return (host, None) if host else ("", None)
    except Exception:
        return "", None


def verdict(avg_ms):
    """(word, is_good_bool) — None grades unknown, never fake-healthy."""
    try:
        if avg_ms is None:
            return "unknown", False
        for bound, word in VERDICT_BANDS:
            if float(avg_ms) < bound:
                return word, bound <= 100
        return "Poor", False
    except Exception:
        return "unknown", False


def probe_icmp(host, count=3, timeout_ms=1500, cancelled=None):
    """(sent, received, avg_ms|None) via one ping.exe run (argv-style,
    shell=False — the hostname can never inject). Only TTL-anchored
    reply lines are read; stats computed locally. Honors cancelled()
    before launching (a run in flight finishes fast: count is small)."""
    host = clean_host(host)
    if not host:
        return 0, 0, None
    try:
        if cancelled is not None and cancelled():
            return 0, 0, None
    except Exception:
        return 0, 0, None
    try:
        count = max(1, min(5, int(count)))
    except Exception:
        count = 3
    try:
        proc = subprocess.run(
            ["ping", "-n", str(count), "-w", str(int(timeout_ms)), host],
            shell=False, capture_output=True, text=True, timeout=count * 6 + 10,
            errors="replace",
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        out = proc.stdout or ""
    except Exception:
        return count, 0, None
    samples = []
    try:
        for line in out.splitlines():
            if "TTL=" not in line.upper():
                continue
            m = _MS_RE.search(line)
            if m:
                try:
                    samples.append(float(m.group(1)))
                except Exception:
                    continue
    except Exception:
        samples = []
    if not samples:
        return count, 0, None
    return count, len(samples), sum(samples) / len(samples)


def probe_tcp(host, port=443, samples=3, timeout=3.0, cancelled=None):
    """(sent, ok, avg_ms|None) — TCP handshake timing, best-of samples.
    Honest path latency to an edge that accepts TCP; refusals and
    timeouts count as loss, never as numbers."""
    host = clean_host(host)
    try:
        port = int(port)
        if not (1 <= port <= 65535):
            return 0, 0, None
    except Exception:
        return 0, 0, None
    if not host:
        return 0, 0, None
    try:
        samples = max(1, min(5, int(samples)))
    except Exception:
        samples = 3
    times = []
    try:
        for _ in range(samples):
            try:
                if cancelled is not None and cancelled():
                    break
            except Exception:
                break
            try:
                t0 = time.perf_counter()
                s = socket.create_connection((host, port), timeout=timeout)
                dt = (time.perf_counter() - t0) * 1000.0
                try:
                    s.close()
                except Exception:
                    pass
                times.append(dt)
            except Exception:
                continue
    except Exception:
        pass
    if not times:
        return samples, 0, None
    return samples, len(times), sum(times) / len(times)
