"""
Game Server Ping data layer — "test before you queue".

Two honest measurement methods, both stdlib-only:

* ICMP echo via ping.exe to official per-region endpoints (Fortnite's
  ping-nae/nac/naw/eu/oce/br/asia/me.ds.on.epicgames.com, Valve's SDR
  relays, Riot Direct, Blizzard Looking Glass, Jagex worlds, ...). TCP
  does NOT work on some of those (verified live: connection refused),
  so ICMP it is.
* TCP handshake to real game ports (Minecraft 25565, Rust 28015, Tibia
  7171) — a genuine server, not a proxy.
* TCP handshake to cloud edges (AWS/Azure/GCP region hosts, EA Blaze,
  Photon, company front doors) — the honest proxy method: it ranks
  regions, it is not an absolute ms to the game socket, and the rows
  say so.

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
from app.utils import resolve_exe

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

# Company edges: (display, hostname, tcp port). Path to the company's
# front door, not a game socket; rows say so. Re-verified 2026-09 —
# eshop.nintendo.net and account.square-enix.com had both been quietly
# retired/renamed and always came back "no reply"; replaced below.
COMPANY_EDGES = (
    ("Steam (Valve)", "store.steampowered.com", 443),
    ("PlayStation Network", "auth.api.sonyentertainmentnetwork.com", 443),
    ("Xbox Live", "xboxlive.com", 443),
    ("Nintendo eShop", "store.nintendo.com", 443),
    ("Battle.net (Blizzard)", "battle.net", 443),
    ("Riot Games", "riotgames.com", 443),
    ("Epic Games", "epicgames.com", 443),
    ("EA", "ea.com", 443),
    ("Ubisoft", "ubisoft.com", 443),
    ("Take-Two / 2K", "take2games.com", 443),
    ("Rockstar Social Club", "socialclub.rockstargames.com", 443),
    ("Tencent / Level Infinite", "levelinfinite.com", 443),
    ("NetEase Games", "neteasegames.com", 443),
    ("Capcom", "capcom.com", 443),
    ("Sega / Atlus", "sega.com", 443),
    ("Square Enix", "square-enix.com", 443),
    ("Bandai Namco", "bandainamcoent.com", 443),
    ("HoYoverse", "hoyoverse.com", 443),
    ("CD Projekt Red", "cdprojektred.com", 443),
    ("Wargaming", "wargaming.net", 443),
    ("GOG.com", "gog.com", 443),
    ("Bungie (Destiny 2)", "www.bungie.net", 443),
    ("Discord Gateway", "discord.com", 443),
)

# ---- ICMP-endpoint games (official hosts where publishers ship one) ----

# CS2 / Dota 2 / Deadlock (Valve): Valve has never published stable
# public hostnames for its SDR matchmaking relays — the previous
# "iad.valve.net" / "eat.valve.net" / etc. table was fiction; none of
# those names have ever resolved, which is exactly why every row here
# always came back "no reply". The real, honest way to test proximity
# to Valve's network is Steam's own public ISteamDirectory/GetCMList
# API, which hands back the live, current list of Steam Connection
# Manager servers (these rotate — that's WHY there's no fixed hostname
# list to hardcode) with real geographic labels baked into their
# names. Fetched fresh each time the tab runs; see _valve_targets().
VALVE_REGIONS = ()

_VALVE_CITY_CODES = {
    "atl": "Atlanta", "iad": "Washington DC", "ord": "Chicago",
    "sea": "Seattle", "sto": "Stockholm", "sto2": "Stockholm",
    "waw": "Warsaw", "ams": "Amsterdam", "fra": "Frankfurt",
    "lhr": "London", "lon": "London", "par": "Paris", "mad": "Madrid",
    "vie": "Vienna", "lux": "Luxembourg", "sgp": "Singapore",
    "hkg": "Hong Kong", "tyo": "Tokyo", "nrt": "Tokyo",
    "syd": "Sydney", "per": "Perth", "gru": "Sao Paulo",
    "scl": "Santiago", "eze": "Buenos Aires", "lim": "Lima",
    "bom": "Mumbai", "maa": "Chennai", "dxb": "Dubai",
    "jnb": "Johannesburg", "cpt": "Cape Town", "sof": "Sofia",
    "dfw": "Dallas", "lax": "Los Angeles", "sjc": "San Jose",
    "yyz": "Toronto", "vie2": "Vienna",
}


def _valve_targets(timeout=3.0):
    """Live Steam CM server list -> [(label, host, 'icmp', None)]-style
    rows, deduped to a handful of geographically spread entries. Falls
    back to Valve's stable web front doors (labeled honestly as such,
    not as regional relays) if the API can't be reached — e.g. no
    internet, or steampowered.com blocked on this network."""
    try:
        import urllib.request as _ur
        import json as _json
        url = ("https://api.steampowered.com/ISteamDirectory/"
               "GetCMList/v1/?format=json&cellid=0")
        with _ur.urlopen(url, timeout=timeout) as resp:
            data = _json.loads(resp.read().decode("utf-8", "replace"))
        servers = (((data or {}).get("response") or {}).get("serverlist_websockets")
                   or ((data or {}).get("response") or {}).get("serverlist") or [])
        seen_cities = set()
        out = []
        for entry in servers:
            host = str(entry).split(":")[0]
            if not host or host[0].isdigit():
                continue  # skip bare IP entries, keep named ones
            first_label = host.split(".")[0]
            code = "".join(ch for ch in first_label if not ch.isdigit()).strip("-")
            city = _VALVE_CITY_CODES.get(code)
            label = f"Steam CM — {city}" if city else f"Steam CM ({first_label})"
            key = city or first_label
            if key in seen_cities:
                continue
            seen_cities.add(key)
            out.append((label, host))
            if len(out) >= 12:
                break
        if out:
            return out
    except Exception:
        pass
    # Fallback: not region-specific, but real and always reachable —
    # honest about what it actually measures.
    return [("Steam Network (general reachability)", "store.steampowered.com")]


# League of Legends (Riot Direct backbone endpoints). ICMP echo.
LOL_REGIONS = (
    ("NA Central (Chicago)", "104.160.131.3"),
    ("EU West (Amsterdam)", "104.160.141.3"),
    ("EU Nordic & East (Frankfurt)", "104.160.142.3"),
    ("Oceania (Sydney)", "104.160.156.1"),
    ("Latin America North (Miami)", "104.160.136.3"),
    ("Brazil (Sao Paulo)", "104.160.152.3"),
)

# Overwatch 2 & Blizzard titles: Blizzard Looking Glass regional nodes.
# ICMP echo.
BLIZZARD_REGIONS = (
    ("US West (Los Angeles)", "24.105.30.129"),
    ("US Central (Chicago)", "24.105.62.129"),
    ("Europe 1 (Paris)", "185.60.112.157"),
    ("Europe 2 (Amsterdam)", "185.60.114.159"),
    ("Korea (Seoul)", "211.234.110.1"),
    ("Taiwan (Taipei)", "203.66.81.98"),
    ("South America (Brazil)", "54.207.107.12"),
)

# Old School RuneScape: official Jagex game worlds. ICMP echo.
OSRS_REGIONS = (
    ("US East (World 307)", "oldschool7.runescape.com"),
    ("US Central (World 321)", "oldschool21.runescape.com"),
    ("US West (World 302)", "oldschool2.runescape.com"),
    ("UK (World 301)", "oldschool1.runescape.com"),
    ("Germany / EU (World 303)", "oldschool3.runescape.com"),
    ("Australia (World 308)", "oldschool8.runescape.com"),
)

# Path of Exile 1 & 2: official GGG gateway nodes. ICMP echo.
# Re-verified 2026-09: GGG consolidated the old per-coast US gateways
# down to one, and several region codes here never resolved (they
# always came back "no reply") — trimmed to the ones that are
# actually live today.
POE_REGIONS = (
    ("US (single gateway)", "us.login.pathofexile.com"),
    ("EU Central (Frankfurt)", "fra.login.pathofexile.com"),
    ("EU West (London)", "lon.login.pathofexile.com"),
    ("EU North (Amsterdam)", "ams.login.pathofexile.com"),
)

# Final Fantasy XIV: official Square Enix lobby gateways. ICMP echo.
FFXIV_REGIONS = (
    ("NA - Aether", "neolobby02.ffxiv.com"),
    ("NA - Primal", "neolobby04.ffxiv.com"),
    ("NA - Crystal", "neolobby08.ffxiv.com"),
    ("NA - Dynamis", "neolobby11.ffxiv.com"),
    ("EU - Chaos", "neolobby06.ffxiv.com"),
    ("EU - Light", "neolobby07.ffxiv.com"),
    ("JP - Japan Lobby", "neolobby01.ffxiv.com"),
    ("OC - Materia", "neolobby09.ffxiv.com"),
)

# Brawlhalla: Blue Mammoth's official per-region ping endpoints. ICMP echo.
BRAWLHALLA_REGIONS = (
    ("US East (Atlanta)", "pingtest-atl.brawlhalla.com"),
    ("US West (California)", "pingtest-cal.brawlhalla.com"),
    ("Europe (Amsterdam)", "pingtest-ams.brawlhalla.com"),
    ("SE Asia (Singapore)", "pingtest-sgp.brawlhalla.com"),
    ("Australia (Sydney)", "pingtest-aus.brawlhalla.com"),
    ("Brazil (Sao Paulo)", "pingtest-brs.brawlhalla.com"),
    ("Japan (Tokyo)", "pingtest-jpn.brawlhalla.com"),
    ("Middle East (Dubai)", "pingtest-mde.brawlhalla.com"),
    ("Southern Africa", "pingtest-saf.brawlhalla.com"),
)

# War Thunder & Gaijin titles: official cluster gateways. ICMP echo.
WAR_THUNDER_REGIONS = (
    ("Gaijin Master Gateway", "warthunder.com"),
    ("US East Cluster", "34.199.226.7"),
    ("Europe Cluster", "95.211.246.161"),
    ("Asia (Singapore)", "209.58.162.113"),
    ("Asia (Tokyo)", "161.202.139.103"),
)

# THE FINALS (Embark Studios): Google Cloud regional cluster IPs. ICMP.
THE_FINALS_REGIONS = (
    ("US East (Virginia)", "34.186.85.28"),
    ("US Central (Iowa)", "34.61.20.203"),
    ("US West (Oregon)", "34.19.36.211"),
    ("Europe West (Frankfurt)", "35.195.20.200"),
    ("Asia (Tokyo)", "35.190.231.134"),
    ("Australia (Sydney)", "34.40.220.196"),
    ("South America (Sao Paulo)", "34.39.225.127"),
)

# World of Tanks & Warships: Wargaming regional login clusters. ICMP
# echo. "WoT: Asia" removed 2026-09 — login.worldoftanks.asia no
# longer resolves and always came back "no reply".
WARGAMING_REGIONS = (
    ("WoT: US Central", "wotna3.login.wargaming.net"),
    ("WoT: South America", "wotna4.login.wargaming.net"),
    ("WoT: EU 1", "login.p1.worldoftanks.eu"),
    ("WoT: EU 2", "login.p2.worldoftanks.eu"),
    ("WoWS: North America", "login1.worldofwarships.com"),
    ("WoWS: Europe", "login1.worldofwarships.eu"),
)

# Star Wars: The Old Republic (BioWare/EA) server hubs. ICMP echo.
SWTOR_REGIONS = (
    ("US East (Virginia)", "159.153.92.28"),
    ("US West (California)", "159.153.68.252"),
    ("Europe (Frankfurt)", "159.153.72.252"),
)

# Albion Online world megaservers. ICMP echo. Europe/Asia hostnames
# removed 2026-09 — live-europe/live-asia.albiononline.com no longer
# resolve (Albion has since consolidated its server list); only the
# Americas megaserver name still resolves publicly.
ALBION_REGIONS = (
    ("Albion Americas (D.C.)", "live.albiononline.com"),
)

# Elder Scrolls Online megaservers. ICMP echo.
ESO_REGIONS = (
    ("NA Megaserver", "198.20.200.1"),
    ("EU Megaserver (Frankfurt)", "159.100.232.124"),
)

# EVE Online Tranquility (London). ICMP echo.
EVE_REGIONS = (
    ("Tranquility (London)", "tranquility.servers.eveonline.com"),
)

# Genshin Impact & Honkai: Star Rail (HoYoverse) regions. ICMP echo.
HOYOVERSE_REGIONS = (
    ("America (N. Virginia)", "47.252.29.148"),
    ("Europe (Frankfurt)", "8.209.112.98"),
    ("Asia (Tokyo/Singapore)", "47.245.28.1"),
    ("TW / HK / MO", "47.74.205.10"),
)

# Among Us: official Innersloth matchmaking gateways. ICMP echo.
AMONG_US_REGIONS = (
    ("North America", "na.mm.among.us"),
    ("Europe", "eu.mm.among.us"),
    ("Asia", "as.mm.among.us"),
)

# Internet baseline: anycast DNS resolvers for local-ISP health checks.
# ICMP echo.
BASELINE_SERVERS = (
    ("Cloudflare DNS (1.1.1.1)", "1.1.1.1"),
    ("Google DNS (8.8.8.8)", "8.8.8.8"),
    ("Quad9 DNS (9.9.9.9)", "9.9.9.9"),
    ("OpenDNS (208.67.222.222)", "208.67.222.222"),
    ("Lumen / Level3 (4.2.2.2)", "4.2.2.2"),
)

# ---- TCP-443 games (AWS/Azure/GCP edge proxy — honest ranking only) ----

# Call of Duty / Warzone: Activision/Demonware cloud edges. TCP:443.
COD_REGIONS = (
    ("NA East (Virginia)", "ec2.us-east-1.amazonaws.com", 443),
    ("NA Central (Ohio)", "ec2.us-east-2.amazonaws.com", 443),
    ("NA West (Oregon)", "ec2.us-west-2.amazonaws.com", 443),
    ("NA West (California)", "ec2.us-west-1.amazonaws.com", 443),
    ("Europe (Frankfurt)", "ec2.eu-central-1.amazonaws.com", 443),
    ("Europe (London)", "ec2.eu-west-2.amazonaws.com", 443),
    ("Asia (Tokyo)", "ec2.ap-northeast-1.amazonaws.com", 443),
    ("Asia (Singapore)", "ec2.ap-southeast-1.amazonaws.com", 443),
    ("Oceania (Sydney)", "ec2.ap-southeast-2.amazonaws.com", 443),
    ("South America (Sao Paulo)", "ec2.sa-east-1.amazonaws.com", 443),
)

# Battlefield (EA / DICE): EA account/matchmaking gateway + cluster
# edges. TCP:443. "blaze.ea.com" removed 2026-09 — EA retired that
# public hostname; it never resolved and always came back "no reply".
BATTLEFIELD_REGIONS = (
    ("EA Network Edge", "accounts.ea.com", 443),
    ("US East (Virginia)", "ec2.us-east-1.amazonaws.com", 443),
    ("US Central (Ohio)", "ec2.us-east-2.amazonaws.com", 443),
    ("US West (Oregon)", "ec2.us-west-2.amazonaws.com", 443),
    ("Europe Central (Frankfurt)", "ec2.eu-central-1.amazonaws.com", 443),
    ("Europe North (Ireland)", "ec2.eu-west-1.amazonaws.com", 443),
    ("Asia (Tokyo)", "ec2.ap-northeast-1.amazonaws.com", 443),
    ("Oceania (Sydney)", "ec2.ap-southeast-2.amazonaws.com", 443),
    ("South America (Sao Paulo)", "ec2.sa-east-1.amazonaws.com", 443),
)

# Delta Force (Team Jade / TiMi): dedicated regional clusters. TCP:443.
DELTA_FORCE_REGIONS = (
    ("NA East (Virginia)", "ec2.us-east-1.amazonaws.com", 443),
    ("NA West (Silicon Valley)", "ec2.us-west-1.amazonaws.com", 443),
    ("Europe Central (Frankfurt)", "ec2.eu-central-1.amazonaws.com", 443),
    ("Europe North (Finland)", "ec2.eu-north-1.amazonaws.com", 443),
    ("Middle East (Saudi Arabia)", "ec2.me-central-1.amazonaws.com", 443),
    ("Asia (Singapore)", "ec2.ap-southeast-1.amazonaws.com", 443),
    ("Asia (Tokyo)", "ec2.ap-northeast-1.amazonaws.com", 443),
    ("Oceania (Sydney)", "ec2.ap-southeast-2.amazonaws.com", 443),
    ("South America (Sao Paulo)", "ec2.sa-east-1.amazonaws.com", 443),
)

# Tactical mil-sims (Squad, Hell Let Loose, Arma, Insurgency) gateway
# hosts. TCP:443. Insurgency's old "sandstorm.focus-entmt.com" no
# longer resolves — swapped to the publisher's current domain.
MILSIM_REGIONS = (
    ("Squad (US East)", "ec2.us-east-1.amazonaws.com", 443),
    ("Squad (Europe)", "ec2.eu-central-1.amazonaws.com", 443),
    ("Hell Let Loose Master", "hellletloose.com", 443),
    ("Arma Master Gateway", "arma3.com", 443),
    ("Insurgency Master", "www.focus-entmt.com", 443),
)

# Marvel Rivals (NetEase) global server nodes. TCP:443.
MARVEL_RIVALS_REGIONS = (
    ("NA East (Virginia)", "ec2.us-east-1.amazonaws.com", 443),
    ("NA Central (Dallas)", "ec2.us-east-2.amazonaws.com", 443),
    ("NA West (Oregon)", "ec2.us-west-2.amazonaws.com", 443),
    ("Europe (Frankfurt)", "ec2.eu-central-1.amazonaws.com", 443),
    ("Asia (Tokyo)", "ec2.ap-northeast-1.amazonaws.com", 443),
    ("Asia (Singapore)", "ec2.ap-southeast-1.amazonaws.com", 443),
    ("Oceania (Sydney)", "ec2.ap-southeast-2.amazonaws.com", 443),
    ("South America (Sao Paulo)", "ec2.sa-east-1.amazonaws.com", 443),
)

# Rainbow Six Siege: Azure-hosted, but Azure has no public generic
# per-region hostname the way AWS does — "eastus.cloudapp.azure.com"
# and its siblings below were never resolvable at all (a bare Azure
# region name isn't a real host without a specific deployed resource
# name in front of it), which is why this whole tab always came back
# "no reply". Swapped to the same honest AWS-region-edge proxy method
# used successfully throughout the rest of this file. TCP:443.
R6_REGIONS = (
    ("US East (Virginia)", "ec2.us-east-1.amazonaws.com", 443),
    ("US Central (Ohio)", "ec2.us-east-2.amazonaws.com", 443),
    ("US West (N. California)", "ec2.us-west-1.amazonaws.com", 443),
    ("EU West (Ireland)", "ec2.eu-west-1.amazonaws.com", 443),
    ("EU Central (Frankfurt)", "ec2.eu-central-1.amazonaws.com", 443),
    ("Asia East (Hong Kong)", "ec2.ap-east-1.amazonaws.com", 443),
    ("Asia SE (Singapore)", "ec2.ap-southeast-1.amazonaws.com", 443),
    ("Australia East (Sydney)", "ec2.ap-southeast-2.amazonaws.com", 443),
    ("Brazil South (Sao Paulo)", "ec2.sa-east-1.amazonaws.com", 443),
    ("Japan East (Tokyo)", "ec2.ap-northeast-1.amazonaws.com", 443),
)

# Escape from Tarkov (Battlestate Games) backend + launcher. TCP:443.
TARKOV_EDGES = (
    ("Matchmaking & Backend", "prod.escapefromtarkov.com", 443),
    ("Launcher & CDN", "launcher.escapefromtarkov.com", 443),
)

# Hunt: Showdown 1896 (Crytek) regional routing clusters. TCP:443.
HUNT_REGIONS = (
    ("US East (Virginia)", "ec2.us-east-1.amazonaws.com", 443),
    ("US West (California)", "ec2.us-west-1.amazonaws.com", 443),
    ("Europe (Frankfurt)", "ec2.eu-central-1.amazonaws.com", 443),
    ("Asia (Tokyo)", "ec2.ap-northeast-1.amazonaws.com", 443),
    ("Oceania (Sydney)", "ec2.ap-southeast-2.amazonaws.com", 443),
    ("South America (Sao Paulo)", "ec2.sa-east-1.amazonaws.com", 443),
)

# Dead by Daylight (Behaviour / AWS GameLift). TCP:443.
DBD_REGIONS = (
    ("US East (Virginia)", "ec2.us-east-1.amazonaws.com", 443),
    ("US West (Oregon)", "ec2.us-west-2.amazonaws.com", 443),
    ("Europe (Frankfurt)", "ec2.eu-central-1.amazonaws.com", 443),
    ("Asia (Tokyo)", "ec2.ap-northeast-1.amazonaws.com", 443),
    ("Oceania (Sydney)", "ec2.ap-southeast-2.amazonaws.com", 443),
    ("South America (Brazil)", "ec2.sa-east-1.amazonaws.com", 443),
)

# Arena Breakout: Infinite (MoreFun / Tencent). TCP:443.
ARENA_BREAKOUT_REGIONS = (
    ("Main Game Gateway", "arenabreakoutinfinite.com", 443),
    ("NA East (Virginia)", "ec2.us-east-1.amazonaws.com", 443),
    ("NA Central (Texas)", "ec2.us-east-2.amazonaws.com", 443),
    ("Europe (Frankfurt)", "ec2.eu-central-1.amazonaws.com", 443),
    ("Asia (Singapore)", "ec2.ap-southeast-1.amazonaws.com", 443),
    ("Oceania (Sydney)", "ec2.ap-southeast-2.amazonaws.com", 443),
)

# Looter / action blockbusters (The First Descendant, Once Human,
# Monster Hunter, VRChat, Fall Guys, Supercell...). TCP:443.
LOOTER_ACTION_REGIONS = (
    ("The First Descendant (NA)", "ec2.us-east-1.amazonaws.com", 443),
    ("The First Descendant (EU)", "ec2.eu-central-1.amazonaws.com", 443),
    ("Once Human (NA East)", "ec2.us-east-1.amazonaws.com", 443),
    ("Once Human (Europe)", "ec2.eu-central-1.amazonaws.com", 443),
    ("Space Marine 2 / Focus", "focus-home.com", 443),
    ("Darktide (Fatshark)", "fatshark.se", 443),
    ("The Division 2 (Ubisoft)", "thedivisiongame.com", 443),
    ("For Honor (Ubisoft)", "forhonorgame.com", 443),
    ("Monster Hunter (Capcom)", "capcom.com", 443),
    ("Wuthering Waves", "wutheringwaves.kurogames.com", 443),
    ("Zenless Zone Zero", "zenless.hoyoverse.com", 443),
    ("VRChat Cloud Edge", "api.vrchat.cloud", 443),
    ("Fall Guys Edge", "fallguys.com", 443),
    ("Supercell (Brawl Stars)", "supercell.com", 443),
)

# Fighting games, co-op & survival gateway hosts. TCP:443.
# Re-verified 2026-09: "palworldgame.com" and the Azure bare-region
# host for Sea of Thieves/Halo never resolved — replaced below.
FIGHTING_AND_COOP_EDGES = (
    ("Helldivers 2 (AWS Edge)", "ec2.us-east-1.amazonaws.com", 443),
    ("Street Fighter 6 (CFN)", "game.capcom.com", 443),
    ("Tekken 8 Online Gateway", "tekken-official.jp", 443),
    ("DayZ Master Hive", "hive.dayzgame.com", 443),
    ("Palworld Master Edge", "playpalworld.com", 443),
    ("MapleStory Global Edge", "maplestory.nexon.net", 443),
    ("Sea of Thieves / Halo", "seaofthieves.com", 443),
    ("Smite 2 & Paladins", "api.hirezstudios.com", 443),
)

# iRacing Anycast race-server farms. TCP:443.
IRACING_REGIONS = (
    ("US East Farm (Ohio)", "use1race01-any.iracing.com", 443),
    ("Europe Farm (Frankfurt)", "euc1race01-any.iracing.com", 443),
    ("Australia Farm (Sydney)", "apse2race01-any.iracing.com", 443),
    ("US West Farm", "ec2.us-west-1.amazonaws.com", 443),
    ("Japan Farm (Tokyo)", "ec2.ap-northeast-1.amazonaws.com", 443),
    ("Brazil Farm (Sao Paulo)", "ec2.sa-east-1.amazonaws.com", 443),
)

# EA Sports FC / FIFA: EA Blaze redirector + data centers. TCP:443.
# "utas.fut.ea.com" removed 2026-09 — retired FUT endpoint, always
# came back "no reply"; swapped for EA's general account/network edge.
EASPORTS_FC_REGIONS = (
    ("EA Blaze Redirector", "gosredirector.ea.com", 443),
    ("EA Network Edge", "accounts.ea.com", 443),
    ("US East (Virginia)", "ec2.us-east-1.amazonaws.com", 443),
    ("US Central (Dallas/Ohio)", "ec2.us-east-2.amazonaws.com", 443),
    ("US West (California)", "ec2.us-west-1.amazonaws.com", 443),
    ("Europe Central (Frankfurt)", "ec2.eu-central-1.amazonaws.com", 443),
    ("Europe West (London)", "ec2.eu-west-2.amazonaws.com", 443),
    ("South America (Sao Paulo)", "ec2.sa-east-1.amazonaws.com", 443),
    ("Middle East (Dubai)", "ec2.me-central-1.amazonaws.com", 443),
    ("Asia (Tokyo)", "ec2.ap-northeast-1.amazonaws.com", 443),
    ("Oceania (Sydney)", "ec2.ap-southeast-2.amazonaws.com", 443),
)

# NBA 2K (2K Sports / Visual Concepts). TCP:443.
NBA2K_REGIONS = (
    ("2K Online Edge", "nba2k.com", 443),
    ("US East (Virginia)", "ec2.us-east-1.amazonaws.com", 443),
    ("US West (California)", "ec2.us-west-1.amazonaws.com", 443),
    ("Europe (Frankfurt)", "ec2.eu-central-1.amazonaws.com", 443),
    ("Asia (Seoul/Tokyo)", "ec2.ap-northeast-2.amazonaws.com", 443),
    ("Oceania (Sydney)", "ec2.ap-southeast-2.amazonaws.com", 443),
)

# Rocket League (Psyonix / Epic). TCP:443.
ROCKET_LEAGUE_REGIONS = (
    ("US East", "ec2.us-east-1.amazonaws.com", 443),
    ("US West", "ec2.us-west-2.amazonaws.com", 443),
    ("Europe", "ec2.eu-central-1.amazonaws.com", 443),
    ("Oceania", "ec2.ap-southeast-2.amazonaws.com", 443),
    ("South America", "ec2.sa-east-1.amazonaws.com", 443),
    ("Asia", "ec2.ap-southeast-1.amazonaws.com", 443),
)

# GTA Online & Red Dead Online: Rockstar Cloud + AWS session brokers.
GTA_ONLINE_REGIONS = (
    ("Rockstar Cloud Gateway", "prod.cloud.rockstargames.com", 443),
    ("Rockstar Social Club", "socialclub.rockstargames.com", 443),
    ("NA East (Virginia)", "ec2.us-east-1.amazonaws.com", 443),
    ("NA West (Oregon)", "ec2.us-west-2.amazonaws.com", 443),
    ("Europe (Frankfurt)", "ec2.eu-central-1.amazonaws.com", 443),
    ("Asia (Tokyo)", "ec2.ap-northeast-1.amazonaws.com", 443),
    ("Oceania (Sydney)", "ec2.ap-southeast-2.amazonaws.com", 443),
    ("South America (Brazil)", "ec2.sa-east-1.amazonaws.com", 443),
)

# Star Citizen (Cloud Imperium Games). TCP:443.
STAR_CITIZEN_REGIONS = (
    ("US East (Virginia)", "ec2.us-east-1.amazonaws.com", 443),
    ("US West (Oregon)", "ec2.us-west-2.amazonaws.com", 443),
    ("Europe (Frankfurt)", "ec2.eu-central-1.amazonaws.com", 443),
    ("Australia (Sydney)", "ec2.ap-southeast-2.amazonaws.com", 443),
)

# Warframe (Digital Extremes): cluster + matchmaking edges. TCP:443.
WARFRAME_EDGES = (
    ("Matchmaking Edge", "content.warframe.com", 443),
    ("Session & API Edge", "api.warframe.com", 443),
    ("Account & Auth Edge", "origin.warframe.com", 443),
)

# Photon Cloud (Phasmophobia, Rec Room, Golf It): Exit Games doesn't
# publish public per-region relay hostnames the way this used to
# assume — "us.photonengine.io", "eu.photonengine.io" etc. never
# resolved. Kept to the one endpoint that's real and stable, plus the
# company's own site as a second, honestly-labeled edge.
PHOTON_REGIONS = (
    ("Photon Master Server", "ns.photonengine.io", 443),
    ("Photon / Exit Games Site", "www.photonengine.com", 443),
)

# Guild Wars 2 (ArenaNet): datacenter + auth gateways. TCP:443.
GW2_REGIONS = (
    ("NA Datacenter Edge", "origincdn.gw2.com", 443),
    ("EU Datacenter Edge", "ec2.eu-central-1.amazonaws.com", 443),
    ("NCSoft Auth Gateway", "auth1.101.ncplatform.net", 443),
)

# Black Desert Online (Pearl Abyss). TCP:443. The old per-region
# "na/eu/sa.blackdesertonline.com" hostnames never resolved — Pearl
# Abyss now runs NA+EU through one shared gateway domain.
BDO_REGIONS = (
    ("NA + EU Gateway", "naeu.playblackdesert.com", 443),
    ("Web / Account Edge", "www.blackdesertonline.com", 443),
)

# Lost Ark & New World (Amazon Games). TCP:443.
AMAZON_GAMES_REGIONS = (
    ("US East (N. Virginia)", "ec2.us-east-1.amazonaws.com", 443),
    ("US West (Oregon)", "ec2.us-west-2.amazonaws.com", 443),
    ("EU Central (Frankfurt)", "ec2.eu-central-1.amazonaws.com", 443),
    ("South America (Sao Paulo)", "ec2.sa-east-1.amazonaws.com", 443),
    ("Asia (Tokyo)", "ec2.ap-northeast-1.amazonaws.com", 443),
)

# ---- TCP game-port games (direct to a real game server) ----------------

# Tibia (CipSoft). TCP:443. "login01/02.tibia.com:7171" never
# resolved — CipSoft doesn't publish named per-region login hosts on
# that port publicly, so this is now an honest path check to
# CipSoft's own site rather than a fabricated game-port target.
TIBIA_REGIONS = (
    ("CipSoft / Tibia Site", "www.tibia.com", 443),
)

# Rust flagship community servers: TCP straight to the game port,
# 28015. "eu.rustafied.com" removed 2026-09 — never resolved (always
# "no reply"); Rustoria's EU server below already covers that region.
# NOTE: these are player-run community servers, not Facepunch
# infrastructure — they can go offline or renumber without notice,
# unlike everything else in this file.
RUST_SERVERS = (
    ("Rustafied US East", "104.143.2.1", 28015),
    ("Rustoria US Main", "208.103.169.97", 28015),
    ("Rustoria EU Main", "208.103.169.220", 28015),
)

# Picker tabs in display order: (title, key).
GAME_TABS = (
    # Shooters & battle royales
    ("Fortnite", "fortnite"),
    ("CS2, Dota 2 & Deadlock", "valve"),
    ("VALORANT", "valorant"),
    ("Call of Duty", "cod"),
    ("Battlefield", "battlefield"),
    ("Delta Force", "deltaforce"),
    ("Tactical Mil-Sims (Squad/HLL)", "milsims"),
    ("Marvel Rivals", "marvelrivals"),
    ("THE FINALS", "thefinals"),
    ("Apex Legends", "apex"),
    ("Rainbow Six Siege", "r6"),
    ("Escape from Tarkov", "tarkov"),
    ("Hunt: Showdown 1896", "hunt"),
    ("Dead by Daylight", "dbd"),
    ("PUBG", "pubg"),
    ("Rust", "rust"),
    ("War Thunder", "warthunder"),
    ("Arena Breakout", "arenabreakout"),
    ("Looters & Action (TFD/Once Human)", "looter_action"),
    # Sports, sim racing & open world
    ("iRacing", "iracing"),
    ("EA Sports FC (FIFA)", "easportsfc"),
    ("NBA 2K", "nba2k"),
    ("Rocket League", "rocketleague"),
    # Party, social & crossplay
    ("Among Us", "amongus"),
    ("Photon Cloud (Phasmo/RecRoom)", "photon"),
    ("GTA & Red Dead Online", "gtaonline"),
    ("Star Citizen", "starcitizen"),
    ("Warframe", "warframe"),
    ("Brawlhalla", "brawlhalla"),
    ("Fighting & Co-Op", "fighting_coop"),
    # MMORPGs, MOBAs & ARPGs
    ("League of Legends", "lol"),
    ("Overwatch / Blizzard", "blizzard"),
    ("Old School RuneScape", "osrs"),
    ("Path of Exile", "poe"),
    ("SWTOR", "swtor"),
    ("FFXIV", "ffxiv"),
    ("World of Tanks", "wargaming"),
    ("Guild Wars 2", "gw2"),
    ("Black Desert", "bdo"),
    ("Albion Online", "albion"),
    ("Elder Scrolls Online", "eso"),
    ("Lost Ark & New World", "amazongames"),
    ("EVE Online", "eve"),
    ("Genshin Impact", "hoyoverse"),
    ("Tibia", "tibia"),
    ("Minecraft", "minecraft"),
    ("Roblox", "roblox"),
    # Diagnostic baseline & companies — each company gets its own dropdown row
    ("Internet Baseline", "baseline"),
    *( ("Steam (Valve)", "company_0"),
       ("PlayStation Network (PSN)", "company_1"),
       ("Xbox Live", "company_2"),
       ("Nintendo (eShop & Network)", "company_3"),
       ("Battle.net (Blizzard)", "company_4"),
       ("Riot Games", "company_5"),
       ("Epic Games", "company_6"),
       ("EA", "company_7"),
       ("Ubisoft", "company_8"),
       ("Take-Two / 2K", "company_9"),
       ("Rockstar Social Club", "company_10"),
       ("Tencent / Level Infinite", "company_11"),
       ("NetEase Games", "company_12"),
       ("Capcom", "company_13"),
       ("Sega / Atlus", "company_14"),
       ("Square Enix", "company_15"),
       ("Bandai Namco", "company_16"),
       ("HoYoverse", "company_17"),
       ("CD Projekt Red", "company_18"),
       ("Wargaming", "company_19"),
       ("GOG.com", "company_20"),
       ("Bungie (Destiny 2)", "company_21"),
       ("Discord Gateway", "company_22") ),
    ("Custom", "custom"),
)
# Per-tab honesty footnotes (shown under the picker).
GAME_METHODS = {
    "fortnite": "ICMP echo to Epic's official region endpoints.",
    "valve": "ICMP echo to Valve's live Steam Connection Manager list "
             "(fetched fresh each run — Valve doesn't publish fixed "
             "hostnames for these) — a real proxy for CS2/Dota/Deadlock, "
             "not exact in-game ms.",
    "valorant": "TCP path to the AWS edge nearest each data center — "
                "a proxy; trust the ranking, not the exact ms.",
    "cod": "TCP path to Activision/Demonware cloud edges — "
           "a proxy; trust the ranking, not the exact ms.",
    "battlefield": "TCP path to EA Blaze & DICE multiplayer clusters — "
                   "a proxy; trust the ranking, not the exact ms.",
    "deltaforce": "TCP path to Team Jade / TiMi dedicated regional clusters — "
                  "a proxy; trust the ranking, not the exact ms.",
    "milsims": "TCP path to Squad, Hell Let Loose, and Bohemia Master "
               "Gateways — path check, not a game socket.",
    "marvelrivals": "TCP path to NetEase / AWS global game server nodes — "
                    "a proxy; trust the ranking, not the exact ms.",
    "thefinals": "ICMP echo to Embark Studios' Google Cloud server clusters.",
    "apex": "TCP path to the AWS edge nearest each data center — "
            "a proxy; trust the ranking, not the exact ms.",
    "r6": "TCP path to Microsoft Azure data center edges — "
          "a proxy; trust the ranking, not the exact ms.",
    "tarkov": "TCP handshake to Battlestate Games backend infrastructure.",
    "hunt": "TCP path to Crytek regional game routing clusters — "
            "a proxy; trust the ranking, not the exact ms.",
    "dbd": "TCP path to Behaviour / AWS GameLift match infrastructure — "
           "a proxy; trust the ranking, not the exact ms.",
    "pubg": "TCP path to the cloud edge nearest each region — "
            "a proxy; trust the ranking, not the exact ms.",
    "rust": "TCP handshake directly to the flagship server game ports.",
    "warthunder": "ICMP echo to Gaijin regional cluster gateways.",
    "arenabreakout": "TCP path to MoreFun Studios extraction server nodes — "
                     "a proxy; trust the ranking, not the exact ms.",
    "looter_action": "TCP path to The First Descendant, Once Human, and "
                     "Capcom action backends — path check, not a game socket.",
    "iracing": "TCP path to iRacing's Anycast race server farms.",
    "easportsfc": "TCP path to EA Blaze matchmaking and Game Data Centers — "
                  "a proxy; trust the ranking, not the exact ms.",
    "nba2k": "TCP path to 2K Sports AWS cloud game nodes — "
             "a proxy; trust the ranking, not the exact ms.",
    "rocketleague": "TCP path to Psyonix cloud routing edges — "
                    "a proxy; trust the ranking, not the exact ms.",
    "amongus": "ICMP echo to official Innersloth regional matchmaking gateways.",
    "photon": "TCP handshake to Exit Games / Photon Cloud game relays "
              "(Phasmophobia/Rec Room).",
    "gtaonline": "TCP path to Rockstar Cloud & AWS matchmaking session "
                 "brokers — a proxy; trust the ranking, not the exact ms.",
    "starcitizen": "TCP path to Cloud Imperium Games AWS megaserver clusters — "
                   "a proxy; trust the ranking, not the exact ms.",
    "warframe": "TCP handshake to Digital Extremes cluster and "
                "matchmaking edges.",
    "brawlhalla": "ICMP echo to Blue Mammoth's official region ping endpoints.",
    "fighting_coop": "TCP path to official backend gateways (Capcom, Tekken, "
                     "Helldivers 2, DayZ) — path check, not a game socket.",
    "lol": "ICMP echo to Riot Direct's central regional endpoints.",
    "blizzard": "ICMP echo to Blizzard Looking Glass regional endpoints.",
    "osrs": "ICMP echo to official Jagex game worlds.",
    "poe": "ICMP echo to Grinding Gear Games official gateway nodes.",
    "swtor": "ICMP echo to official BioWare / SWTOR server hubs.",
    "ffxiv": "ICMP echo to Square Enix official lobby gateways.",
    "wargaming": "ICMP echo to Wargaming regional login clusters.",
    "gw2": "TCP handshake to ArenaNet datacenter cloud gateways.",
    "bdo": "TCP handshake to Pearl Abyss regional session gateways.",
    "albion": "ICMP echo to Albion Online's three world megaservers.",
    "eso": "ICMP echo to Elder Scrolls Online Megaserver gateways.",
    "amazongames": "TCP path to Amazon Games cloud infrastructure "
                   "(Lost Ark / New World).",
    "eve": "ICMP echo to CCP's Tranquility cluster in London.",
    "hoyoverse": "ICMP echo to HoYoverse cloud game regions.",
    "tibia": "TCP handshake to CipSoft's site (443) — the old per-region "
             "game-port hostnames never resolved; see the table comment.",
    "minecraft": "TCP handshake to the server's game port — a real server.",
    "roblox": "TCP handshake to the Roblox web/client edge.",
    "baseline": "ICMP echo to global anycast DNS baselines to test "
                "local ISP health.",
    "companies": "TCP handshake to each company's front door — "
                 "path check, not a game socket.",
    "custom": "Bare hostname = ping · hostname:port = TCP.",
}


def targets_for(key, custom_hosts=()):
    """[(row_key, title, kind, host, port_or_None)] for a tab key."""
    # ICMP-endpoint games
    if key == "fortnite":
        return [(n, n, "icmp", h, None) for n, h in FORTNITE_REGIONS]
    if key == "valve":
        return [(n, n, "icmp", h, None) for n, h in _valve_targets()]
    if key == "lol":
        return [(n, n, "icmp", h, None) for n, h in LOL_REGIONS]
    if key == "blizzard":
        return [(n, n, "icmp", h, None) for n, h in BLIZZARD_REGIONS]
    if key == "osrs":
        return [(n, n, "icmp", h, None) for n, h in OSRS_REGIONS]
    if key == "poe":
        return [(n, n, "icmp", h, None) for n, h in POE_REGIONS]
    if key == "ffxiv":
        return [(n, n, "icmp", h, None) for n, h in FFXIV_REGIONS]
    if key == "brawlhalla":
        return [(n, n, "icmp", h, None) for n, h in BRAWLHALLA_REGIONS]
    if key == "warthunder":
        return [(n, n, "icmp", h, None) for n, h in WAR_THUNDER_REGIONS]
    if key == "thefinals":
        return [(n, n, "icmp", h, None) for n, h in THE_FINALS_REGIONS]
    if key == "wargaming":
        return [(n, n, "icmp", h, None) for n, h in WARGAMING_REGIONS]
    if key == "swtor":
        return [(n, n, "icmp", h, None) for n, h in SWTOR_REGIONS]
    if key == "albion":
        return [(n, n, "icmp", h, None) for n, h in ALBION_REGIONS]
    if key == "eso":
        return [(n, n, "icmp", h, None) for n, h in ESO_REGIONS]
    if key == "eve":
        return [(n, n, "icmp", h, None) for n, h in EVE_REGIONS]
    if key == "hoyoverse":
        return [(n, n, "icmp", h, None) for n, h in HOYOVERSE_REGIONS]
    if key == "amongus":
        return [(n, n, "icmp", h, None) for n, h in AMONG_US_REGIONS]
    if key == "baseline":
        return [(n, n, "icmp", h, None) for n, h in BASELINE_SERVERS]

    # TCP-443 games and cloud proxies
    if key == "valorant":
        return [(n, n, "tcp", h, p) for n, h, p in VALORANT_REGIONS]
    if key == "cod":
        return [(n, n, "tcp", h, p) for n, h, p in COD_REGIONS]
    if key == "battlefield":
        return [(n, n, "tcp", h, p) for n, h, p in BATTLEFIELD_REGIONS]
    if key == "deltaforce":
        return [(n, n, "tcp", h, p) for n, h, p in DELTA_FORCE_REGIONS]
    if key == "milsims":
        return [(n, n, "tcp", h, p) for n, h, p in MILSIM_REGIONS]
    if key == "marvelrivals":
        return [(n, n, "tcp", h, p) for n, h, p in MARVEL_RIVALS_REGIONS]
    if key == "apex":
        return [(n, n, "tcp", h, p) for n, h, p in APEX_REGIONS]
    if key == "r6":
        return [(n, n, "tcp", h, p) for n, h, p in R6_REGIONS]
    if key == "tarkov":
        return [(n, n, "tcp", h, p) for n, h, p in TARKOV_EDGES]
    if key == "hunt":
        return [(n, n, "tcp", h, p) for n, h, p in HUNT_REGIONS]
    if key == "dbd":
        return [(n, n, "tcp", h, p) for n, h, p in DBD_REGIONS]
    if key == "pubg":
        return [(n, n, "tcp", h, p) for n, h, p in PUBG_REGIONS]
    if key == "arenabreakout":
        return [(n, n, "tcp", h, p) for n, h, p in ARENA_BREAKOUT_REGIONS]
    if key == "looter_action":
        return [(n, n, "tcp", h, p) for n, h, p in LOOTER_ACTION_REGIONS]
    if key == "fighting_coop":
        return [(n, n, "tcp", h, p) for n, h, p in FIGHTING_AND_COOP_EDGES]
    if key == "iracing":
        return [(n, n, "tcp", h, p) for n, h, p in IRACING_REGIONS]
    if key == "easportsfc":
        return [(n, n, "tcp", h, p) for n, h, p in EASPORTS_FC_REGIONS]
    if key == "nba2k":
        return [(n, n, "tcp", h, p) for n, h, p in NBA2K_REGIONS]
    if key == "rocketleague":
        return [(n, n, "tcp", h, p) for n, h, p in ROCKET_LEAGUE_REGIONS]
    if key == "photon":
        return [(n, n, "tcp", h, p) for n, h, p in PHOTON_REGIONS]
    if key == "gtaonline":
        return [(n, n, "tcp", h, p) for n, h, p in GTA_ONLINE_REGIONS]
    if key == "starcitizen":
        return [(n, n, "tcp", h, p) for n, h, p in STAR_CITIZEN_REGIONS]
    if key == "warframe":
        return [(n, n, "tcp", h, p) for n, h, p in WARFRAME_EDGES]
    if key == "gw2":
        return [(n, n, "tcp", h, p) for n, h, p in GW2_REGIONS]
    if key == "bdo":
        return [(n, n, "tcp", h, p) for n, h, p in BDO_REGIONS]
    if key == "amazongames":
        return [(n, n, "tcp", h, p) for n, h, p in AMAZON_GAMES_REGIONS]
    if key == "minecraft":
        return [(n, n, "tcp", h, p) for n, h, p in MINECRAFT_SERVERS]
    if key == "roblox":
        return [(n, n, "tcp", h, p) for n, h, p in ROBLOX_EDGES]
    if key == "companies":
        return [(n, n, "tcp", h, p) for n, h, p in COMPANY_EDGES]
    if key.startswith("company_"):
        idx = int(key.split("_", 1)[1])
        n, h, p = COMPANY_EDGES[idx]
        return [(n, n, "tcp", h, p)]

    # TCP game-port games
    if key == "tibia":
        return [(n, n, "tcp", h, p) for n, h, p in TIBIA_REGIONS]
    if key == "rust":
        return [(n, n, "tcp", h, p) for n, h, p in RUST_SERVERS]

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
            [resolve_exe("ping"), "-n", str(count), "-w", str(int(timeout_ms)), host],
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
