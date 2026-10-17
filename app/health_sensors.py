"""New PC Health sensors: the things Microsoft's own health tools omit.

Research (Windows' PC Health Check app and the Windows Security
"Device performance & health" page) shows both built-ins cover only a
handful of items: storage free space, battery capacity, Windows Update
status, and a Windows Time service check. No crash history, no memory, no
startup picture, no drive type, no maintenance settings.

That is the gap this module fills. Every sensor here is READ-ONLY: nothing
in this file writes a registry key, changes a setting, deletes a file or
starts a process beyond the short-lived PowerShell probes used to read
values. A health report that mutated the machine would not be a health
report.

DESIGN NOTES worth knowing before editing
-----------------------------------------
* Grading vocabulary is fixed by the dialog's GRADE_TINT map:
  "A" green, "B" teal, "C" yellow, "D" orange, "F" red, "?" hairline.
  "?" is a legitimate answer, not a failure - it means "this machine could
  not tell us", which is what a non-elevated probe usually gets.

* Every probe is bounded by a timeout and degrades to "?" with a PLAIN
  reason. A health check that silently omits half its cards is worse than
  one that admits what it could not read.

* THE PROBES ARE BATCHED, and that is not a micro-optimisation - it is most
  of this module's speed. Measured on the reference machine, running each
  sensor as its own PowerShell launch cost 8.7s for the six fast checks,
  almost all of it process startup rather than the queries themselves
  (the individual cmdlets answer in 6-130ms). One combined launch answers
  the same six questions in 1.55s. So `_batched` runs one script per phase
  and each sensor parses its keys out of the shared result.

  The cache lives on `ctx` rather than in a module global: a new TaskContext
  is built per scan, so it expires naturally, and `verify_no_globals` stays
  green.

* `OK|1` is emitted by both scripts as a success sentinel. Without it a
  script that failed silently would look exactly like one that found
  nothing, and every sensor would report a confident "A" about a machine it
  never actually inspected.

* DCOM 10016 noise is filtered out on purpose (see _CRASH_NOISE). It is the
  single most common System-log error on a healthy Windows install, and
  grading it red would train the user to ignore red.

* SSD wear percentage is deliberately NOT reported. Get-StorageReliabilityCounter
  needs elevation AND community reports show its `Wear` field returning 0 or
  blank on many NVMe drives. A fabricated health percentage is worse than no
  percentage, so the disk card reports only what the drive actually states
  (model, media type, bus type, size).
"""
from __future__ import annotations

from app.utils import TaskContext, run_cmd

_PS = "powershell"

#: Providers whose log noise is not a real fault. DistributedCOM 10016 fires
#: constantly on healthy installs purely because a service and an explorer
#: shell object do not share an identity - it is not a problem to act on.
_CRASH_NOISE = "DistributedCOM|WindowsUpdateClient|Microsoft-Windows-DC"

#: Get-PhysicalDisk reports MediaType in the raw forms "SSD"/"HDD"/"NVMe".
#: str.title() would render "SSD" as "Ssd", which looks like a typo in a
#: report the user is meant to trust.
_MEDIA_NAMES = {
    "ssd": "SSD", "hdd": "hard drive", "nvme": "NVMe SSD",
    "removable": "removable drive", "cd-rom": "CD/DVD drive",
    "unknown": "unknown drive type",
}

#: One launch for all six fast checks. Measured 1.55s as a single process
#: against 8.74s as six.
_FAST_SCRIPT = r"""
$ErrorActionPreference='SilentlyContinue'
'OK|1'

# --- memory + pagefile
$m = @(Get-CimInstance Win32_PhysicalMemory)
"RAMTOTAL|$([math]::Round(($m | Measure-Object Capacity -Sum).Sum / 1GB))"
"RAMSPEED|$($m[0].Speed)"
$p = Get-CimInstance Win32_PageFileUsage | Select-Object -First 1
if ($p) {
  "PFALLOC|$([math]::Round($p.AllocatedBaseSize))"
  "PFPEAK|$([math]::Round($p.PeakUsage))"
}

# --- crashes / hangs, last 7 days
$cap = 400
$ev = Get-WinEvent -FilterHashtable @{LogName='Application';Level=1,2;
      StartTime=(Get-Date).AddDays(-7)} -MaxEvents $cap -EA SilentlyContinue
$real = @($ev | Where-Object { $_.ProviderName -notmatch '%(noise)s' })
if ($real.Count -ge $cap) { 'CRASHSAT|1' }
"CRASHCOUNT|$($real.Count)"
# Events 1000 (crash) and 1002 (hang) carry the offending program's name as
# their first insertion string, so we can name the app instead of the log
# source "Application Error" - and it costs nothing, the events are here.
$real | Where-Object { $_.Id -eq 1000 -or $_.Id -eq 1002 } |
  ForEach-Object { $_.Properties[0].Value } | Where-Object { $_ } |
  Group-Object | Sort-Object Count -Descending | Select-Object -First 3 |
  ForEach-Object { "APP|$($_.Count)|$($_.Name)" }

# --- security
$d = Get-MpComputerStatus
"DEFRTP|$($d.RealTimeProtectionEnabled)"
"DEFSIG|$($d.AntivirusSignatureAge)"
"DEFTAMPER|$($d.IsTamperProtected)"
try { "SECBOOT|$([bool](Confirm-SecureBootUEFI))" } catch { 'SECBOOT|?' }

# --- maintenance settings
$t = (fsutil behavior query DisableDeleteNotify | Out-String)
if ($t -match 'DisableDeleteNotify\s*=\s*0') { 'TRIM|on' } else { 'TRIM|off' }
$ss = (Get-ItemProperty 'HKCU:\SOFTWARE\Microsoft\Windows\CurrentVersion\StorageSense\Parameters\StoragePolicy' -Name 01 -EA SilentlyContinue).'01'
if ($null -eq $ss) { 'SENSE|unknown' } elseif ($ss -eq '1') { 'SENSE|on' } else { 'SENSE|off' }
$fb = (Get-ItemProperty 'HKLM:\SYSTEM\CurrentControlSet\Control\Session Manager\Power' -Name HiberbootEnabled -EA SilentlyContinue).HiberbootEnabled
if ($fb -eq 1) { 'FASTBOOT|on' } elseif ($fb -eq 0) { 'FASTBOOT|off' } else { 'FASTBOOT|unknown' }

# --- time service + uptime
$w = Get-Service W32Time -EA SilentlyContinue
if ($w) { "W32STATUS|$($w.Status)" }
$b = (Get-CimInstance Win32_OperatingSystem).LastBootUpTime
if ($b) { "UPTIME|$([int]((Get-Date) - $b).TotalHours)" }

# --- startup programs
$st = @(Get-CimInstance Win32_StartupCommand)
"STARTOTAL|$($st.Count)"
# StartupApproved stores a blob whose FIRST byte is the verdict: 2 enabled,
# 3 disabled. A missing entry means "not decided", which Windows treats as
# enabled - so it counts as on.
$keys = @(
  'HKCU:\SOFTWARE\Microsoft\Windows\CurrentVersion\Explorer\StartupApproved\Run',
  'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Explorer\StartupApproved\Run',
  'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Explorer\StartupApproved\Run32'
)
$on = 0
foreach ($x in $st) {
  $verdict = $null
  foreach ($kk in $keys) {
    $v = (Get-ItemProperty $kk -Name $x.Name -EA SilentlyContinue).$($x.Name)
    if ($v) { $verdict = $v[0]; break }
  }
  if ($null -eq $verdict -or $verdict -eq 2) { $on++ }
}
"STARTON|$on"
""".replace("%(noise)s", _CRASH_NOISE)

#: One launch for the two slow background checks.
_SLOW_SCRIPT = r"""
$ErrorActionPreference='SilentlyContinue'
'OK|1'
Get-PhysicalDisk | ForEach-Object {
  "DISK|$($_.FriendlyName)|$($_.MediaType)|$($_.BusType)|$([math]::Round($_.Size/1GB))"
}
# Third-party drivers in the classes people actually need drivers for. Windows'
# own class drivers are dated 2006 and are not "outdated"; counting them made
# every PC - even a brand-new one - read "Oldest drivers are from 2006".
$cls = @('DISPLAY','NET','MEDIA','SYSTEM','BLUETOOTH','HDC','SCSIADAPTER','IMAGE','CAMERA','BIOMETRIC','FIRMWARE')
Get-CimInstance Win32_PnPSignedDriver | Where-Object {
  $_.DriverDate -and $_.DriverProviderName -and
  $_.DriverProviderName -notmatch 'Microsoft' -and ($cls -contains ([string]$_.DeviceClass).ToUpper())
} | Sort-Object DriverDate | Select-Object -First 5 | ForEach-Object {
  "OLD|$($_.DriverDate.ToString('yyyy-MM-dd'))|$($_.DeviceName)"
}
# Devices Windows says have NO driver (problem code 28).
Get-CimInstance Win32_PnPEntity | Where-Object { $_.ConfigManagerErrorCode -eq 28 } |
  Select-Object -First 8 | ForEach-Object { "MISS|$($_.Name)" }
"""


def _elevated() -> bool:
    try:
        import ctypes
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def _run_script(ctx: TaskContext, script: str, timeout: int):
    """Run one read-only PowerShell script; return {KEY: [values]} or None.

    Repeated keys accumulate into a list, because APP|, DISK| and OLD| are
    emitted once per entry. Returns None on any failure or if the success
    sentinel is missing - a silently-empty script must never be mistaken for
    a machine that reported nothing wrong.
    """
    lines: list = []
    try:
        run_cmd(ctx, [_PS, "-NoProfile", "-NonInteractive", "-Command", script],
                shell=False, timeout=timeout, collect=lines)
    except Exception:
        return None
    data: dict = {}
    for raw in lines:
        s = str(raw).strip().lstrip("\ufeff")
        if "|" not in s:
            continue
        key, _, val = s.partition("|")
        data.setdefault(key.strip().upper(), []).append(val.strip())
    if not data.get("OK"):
        return None
    return data


def _batched(ctx: TaskContext, attr: str, script: str, timeout: int):
    """Run `script` at most once per ctx and memoise it on the ctx object.

    Storing the cache on ctx rather than in a module global keeps
    verify_no_globals green, and a fresh TaskContext per scan means the
    cache expires on its own.
    """
    cached = getattr(ctx, attr, None)
    if cached is not None:
        return cached
    data = _run_script(ctx, script, timeout) or {}
    try:
        setattr(ctx, attr, data)
    except Exception:
        pass
    return data


def _one(data: dict, key: str, default=None):
    vals = data.get(key) or []
    return vals[0] if vals else default


def _int(data: dict, key: str, default=None):
    try:
        return int(float(_one(data, key)))
    except Exception:
        return default


def _fast(ctx: TaskContext) -> dict:
    return _batched(ctx, "_hs_fast", _FAST_SCRIPT, 60)


def _slow(ctx: TaskContext) -> dict:
    return _batched(ctx, "_hs_slow", _SLOW_SCRIPT, 60)


# --------------------------------------------------------------------------- #
# FAST sensors
# --------------------------------------------------------------------------- #

def sense_memory(ctx: TaskContext):
    """Installed RAM plus the pagefile, which is where the useful judgement is.

    A pagefile many times larger than the machine's actual peak usage is
    disk space reserved for a spike that never happens. Windows will not
    tell the user that, so it is worth saying out loud.
    """
    data = _fast(ctx)
    total = _int(data, "RAMTOTAL")
    if not total or total <= 0:
        return ("?", "Couldn't read memory details",
                "Windows did not report how much memory is installed.", "")
    speed = _one(data, "RAMSPEED") or ""
    detail = "%d GB installed%s." % (
        total, (" at %s MHz" % speed) if speed.isdigit() else "")
    alloc = _int(data, "PFALLOC")
    peak = _int(data, "PFPEAK")
    if alloc and peak:
        if alloc >= max(peak * 3, 1) and alloc >= 2048:
            return ("C", "%d GB of memory installed" % total,
                    detail + " Windows has reserved a %d MB pagefile but "
                    "your highest recent use was %d MB." % (alloc, peak),
                    "A smaller pagefile would free up disk space. You can "
                    "change this in Settings > System > About.")
        detail += " Pagefile: %d MB reserved, %d MB actually used." % (
            alloc, peak)
    if total <= 2:
        return ("C", "Only %d GB of memory" % total,
                detail + " This may be slow with modern Windows.", "")
    return ("A", "%d GB of memory installed" % total, detail, "")


def sense_crashes(ctx: TaskContext):
    """Application hangs and crashes in the last 7 days.

    This is the card people actually want and no built-in provides: not
    "your PC is healthy" but "testhost.exe crashed 68 times". Answers with
    real application names, taken from the crash events themselves.
    """
    data = _fast(ctx)
    if not data:
        return ("?", "Couldn't read the event log",
                "Windows did not let us read recent application events.", "")
    count = _int(data, "CRASHCOUNT")
    if count is None:
        return ("?", "Couldn't count recent problems", "", "")
    if count <= 0:
        return ("A", "No crashes or hangs this week",
                "Nothing has hung or crashed in the last 7 days.", "")

    names = []
    for v in data.get("APP") or []:
        # _run_script partitions on the FIRST "|", so the stored value is
        # "count|name" - the count is the HEAD, not the tail.
        cnt, _, name = v.rpartition("|")
        if name.strip() and cnt.strip().isdigit():
            names.append("%s (%s)" % (name.strip()[:34], cnt.strip()))
    names = names[:3]

    grade = "C" if count <= 5 else "D" if count <= 20 else "F"
    if data.get("CRASHSAT"):
        # We stopped reading at the cap, so we do NOT know the real total.
        # Saying "Over N" is honest; saying "N" would not be.
        return (grade, "Over %d crashes or hangs this week" % count,
                "At least %d application problem(s) in the last 7 days, and "
                "there may be more than we counted. Most frequent: %s."
                % (count, ", ".join(names) if names else "several sources"),
                "Programs that fail repeatedly are often worth updating or "
                "reinstalling.")

    detail = "%d application problem(s) in the last 7 days." % count
    if names:
        detail += " Most often: " + ", ".join(names) + "."
    return (grade, "%d recent crash%s or hang%s" % (
        count, "" if count == 1 else "es", "" if count == 1 else "s"),
        detail,
        "Programs that fail repeatedly are often worth updating or "
        "reinstalling.")


def sense_security(ctx: TaskContext):
    """Defender's real state, plus Secure Boot when we are allowed to ask."""
    data = _fast(ctx)
    if not data:
        return ("?", "Couldn't read security status",
                "Windows did not report its security state.", "")
    rtp = (_one(data, "DEFRTP") or "").lower()
    if rtp == "true":
        grade, headline = "A", "Protection is on"
        detail = "Microsoft Defender is watching for threats in real time."
        note = ""
        sig = _int(data, "DEFSIG")
        if sig is not None and sig > 7:
            grade = "C"
            detail += " Its virus definitions are %d days old." % sig
            note = ("Open Windows Security > Virus & threat protection to "
                    "update.")
        if (_one(data, "DEFTAMPER") or "").lower() == "true":
            detail += " Tamper Protection is on, so malware cannot switch it off."
        boot = (_one(data, "SECBOOT") or "").lower()
        if boot == "true":
            detail += " Secure Boot is on."
        elif boot == "false":
            grade = "C"
            detail += " Secure Boot is off."
        elif not _elevated():
            # Confirm-SecureBootUEFI refuses without elevation. Say WHY the
            # card is short rather than letting the user assume all was well.
            note = ((note + " ") if note else "") + (
                "Secure Boot can only be checked as an administrator, so that "
                "part was skipped.")
        return (grade, headline, detail, note)
    if rtp == "false":
        return ("F", "Real-time protection is OFF",
                "Microsoft Defender is not watching for threats.",
                "Turn it back on in Windows Security > Virus & threat "
                "protection.")
    return ("?", "Protection status unknown",
            "Windows did not report its antivirus state.", "")


def sense_maintenance(ctx: TaskContext):
    """Three quiet settings that decide whether a drive stays healthy."""
    data = _fast(ctx)
    if not data:
        return ("?", "Couldn't read maintenance settings",
                "Windows did not report these settings.", "")
    trim = (_one(data, "TRIM") or "").lower()
    sense = (_one(data, "SENSE") or "").lower()
    fb = (_one(data, "FASTBOOT") or "").lower()

    bits, problems = [], []
    if trim == "on":
        bits.append("TRIM is on, so Windows tells your SSD which blocks are "
                    "free")
    else:
        problems.append("TRIM is off, which lets an SSD slow down over time")
    if sense == "on":
        bits.append("Storage Sense is on, so junk clears itself")
    elif sense == "off":
        problems.append("Storage Sense is off, so temp files build up until "
                        "you clean them")
    if fb == "on":
        bits.append("Fast Startup is on")
    elif fb == "off":
        problems.append("Fast Startup is off, so shutdowns take longer")

    if problems:
        return ("C", "%d setting%s could be improved" % (
            len(problems), "" if len(problems) == 1 else "s"),
            " ".join(problems) + ".", "")
    return ("A", "Drive maintenance is set up properly",
            " ".join(bits) + ".", "")


def sense_time(ctx: TaskContext):
    """The Windows Time service and how long the PC has been up.

    Windows Security lists a stopped Windows Time service as a health
    warning, and a wrong clock breaks certificate checks and signed installs
    - so this is a real check, not a novelty.
    """
    data = _fast(ctx)
    if not data:
        return ("?", "Couldn't check the clock",
                "Windows did not report the time service.", "")
    status = (_one(data, "W32STATUS") or "").lower()
    hours = _int(data, "UPTIME")

    detail = []
    if hours is not None:
        if hours >= 24 * 30:
            detail.append("This PC has been running for %d days without a "
                          "restart." % (hours // 24))
        elif hours >= 24 * 2:
            # At 24h multiples "Up 6 days" beats "Up 147 hours" every time.
            detail.append("Up %d days without a restart." % (hours // 24))
        elif hours >= 24:
            detail.append("Up %d days." % (hours // 24))
        else:
            detail.append("Up %d hours." % hours)

    if status == "running":
        return ("A", "The clock is being kept in sync",
                ("The Windows Time service is running. " if not detail else "")
                + " ".join(detail), "")
    if status == "stopped":
        return ("C", "The clock is not being corrected",
                "The Windows Time service is stopped, so your clock can "
                "drift. " + " ".join(detail),
                "An incorrect time can break certificate checks and app "
                "signing.")
    return ("?", "Time service state unknown",
            " ".join(detail) or "Windows did not report a time service.", "")


def sense_startup(ctx: TaskContext):
    """How many programs start with Windows, and how many are actually on."""
    data = _fast(ctx)
    total = _int(data, "STARTOTAL")
    if total is None:
        return ("?", "Couldn't read startup programs",
                "Windows did not report the list of startup programs.", "")
    if total <= 0:
        return ("A", "No startup programs",
                "Nothing is set to start automatically with Windows.", "")
    on = _int(data, "STARTON")
    on_txt = ("%d of them are currently switched on. " % on
              if on is not None else "")
    grade = ("A" if total <= 5 else "B" if total <= 10 else
             "C" if total <= 20 else "D")
    return (grade, "%d program%s start with Windows" % (
        total, "" if total == 1 else "s"),
        on_txt + "Each one slows down sign-in a little.",
        "" if total <= 10 else
        "You can turn them off in Task Manager > Startup apps.")


# --------------------------------------------------------------------------- #
# SLOW sensors - measured 1.4s and 2.8s individually. These run AFTER the
# fast ones and off the UI thread; the dialog paints a placeholder and these
# fill it in.
# --------------------------------------------------------------------------- #

def sense_disk_detail(ctx: TaskContext):
    """What the drives actually are: SSD or spinner, NVMe or SATA."""
    data = _slow(ctx)
    rows = []
    for v in data.get("DISK") or []:
        p = v.split("|")
        if len(p) >= 3:
            rows.append((p[0].strip(), p[1].strip(), p[2].strip(),
                         p[3].strip() if len(p) > 3 else "?"))
    if not rows:
        return ("?", "Couldn't identify your drives",
                "Windows did not report the physical disks.", "")
    bits = []
    ssd = nvme = 0
    for name, media, bus, size in rows:
        low = media.lower()
        if low.startswith("ssd") or low == "nvme":
            ssd += 1
        if bus.lower() == "nvme":
            nvme += 1
        bits.append("%s - %s, %s GB%s" % (
            name, _MEDIA_NAMES.get(media.lower(), media or "unknown drive"),
            size, ", NVMe" if bus.lower() == "nvme" else ""))
    headline = ("%d solid-state drive%s" % (ssd, "" if ssd == 1 else "s")
                if ssd else "%d drive%s" % (len(rows),
                                            "" if len(rows) == 1 else "s"))
    note = ""
    if not ssd:
        note = ("No solid-state drive detected. If this is a laptop, that is "
                "the single biggest reason it feels slow.")
    elif nvme:
        note = "NVMe drives are several times faster than older SATA drives."
    return ("A" if ssd else "C", headline, "; ".join(bits) + ".", note)


def grade_drivers(old_rows, missing, today=None):
    """(grade, headline, detail, note) from third-party driver ages and the
    devices that have no driver. Pure, so it is unit-tested.

    old_rows: [(yyyy-mm-dd, device name)] oldest first (third-party only).
    missing:  [device name] with problem code 28.

    Honest by construction: Windows' own drivers are excluded upstream, one
    legacy device is a "B" (not worth a warning), and "A" means nothing was
    found wrong - not that nothing was measured.
    """
    import datetime
    today = today or datetime.date.today()
    cutoff = today.replace(year=today.year - 3) if today.month != 2 or \
        today.day != 29 else today.replace(year=today.year - 3, day=28)
    old = [(d, n) for d, n in old_rows if d[:10] < cutoff.isoformat()]
    if missing:
        names = "; ".join(m[:44] for m in missing[:4])
        return ("D" if len(missing) >= 3 else "C",
                "%d device%s ha%s no driver" % (
                    len(missing), "" if len(missing) == 1 else "s",
                    "s" if len(missing) == 1 else "ve"),
                "No driver installed for: %s." % names,
                "The Drivers tab can look these up and install them.")
    if len(old) >= 3:
        listing = "; ".join("%s (%s)" % (n[:44], d[:4]) for d, n in old[:3])
        return ("C", "Several drivers are over 3 years old",
                "Oldest third-party drivers: %s." % listing,
                "The Drivers tab checks for newer ones.")
    if old:
        return ("B", "A driver is over 3 years old",
                "%s (%s)." % (old[0][1][:50], old[0][0][:4]), "")
    return ("A", "Drivers look current",
            "No missing drivers, and no third-party driver over 3 years old.",
            "")


def sense_drivers(ctx: TaskContext):
    """Missing drivers, and old third-party drivers.

    ~3s, which is why it is a background sensor: the report is fully useful
    before this arrives.
    """
    data = _slow(ctx)
    if not data.get("OK"):
        return ("?", "Couldn't check drivers",
                "Windows did not report driver information.", "")
    rows = []
    for v in data.get("OLD") or []:
        p = v.split("|")
        if len(p) >= 2 and p[0].strip()[:4].isdigit():
            rows.append((p[0].strip(), p[1].strip() if len(p) > 1 else ""))
    missing = [m.strip() for m in (data.get("MISS") or []) if m.strip()]
    return grade_drivers(rows, missing)


#: The slow sensors, as (key, title, function). health_scan.SENSORS owns the
#: master list - it merges these in with a `slow=True` flag so they are
#: deferred off the UI thread.
SLOW_SENSORS = (
    ("disks", "Your Drives", sense_disk_detail),
    ("drivers", "Drivers", sense_drivers),
)

# BUG-044: a third background sensor (sense_crashes_deep, reading
# Win32_ReliabilityRecords for named crashing apps) was written and then
# REMOVED rather than shipped. Measured, it was the reason for deleting it:
# the query took 6.7s consistently and spiked to 37.4s once, and because
# run_health_scan JOINS the slow phase to keep the returned card map
# complete, that variance would have delayed the whole report's verdict. The
# server-side WQL alternative measured 23ms but silently matched nothing.
# sense_crashes now names the failing application straight from the event
# properties it already reads - a better answer, from data already in hand.