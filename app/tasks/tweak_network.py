"""Tweak tab — Network tweaks: throttling index, Nagle, gaming DNS, interface power (EEE),
IPv4 preference and the dynamic tick.

Split out of app/tasks/tweak_tasks.py (audit 2026-10, R-002 stage 2). Every
function here is one Task's apply/revert/verify implementation; the shared
constants and primitives live in tweak_common, and `TASKS` is assembled in
tweak_tasks.py so the registry still reads as one literal list.
"""
from __future__ import annotations

from app.tasks.tweak_common import (  # noqa: F401 - re-exported
    IS_WINDOWS,
    TaskContext,
    _DNS_CLOUDFLARE,
    _IPV4_PATH,
    _IPV4_SPECS,
    _NET_THROTTLE_SPECS,
    __verify_read,
    _bcd_value,
    _dns_set_command,
    _dns_snapshot_key,
    _network_interface_subs,
    _open_interfaces_key,
    _ps_sq,
    _query_eee_props,
    _regn_apply,
    _regn_revert,
    _restore_reg_values,
    _snap_reg_values,
    _verify_value,
    clear_tweak_snapshot,
    get_tweak_snapshot,
    reg_set_value_checked,
)
from app.utils import (  # noqa: F401 - re-exported
    TaskCancelled,
    TaskSkipped,
    run_cmd,
    run_cmd_checked,
)
from app.tasks.ps import ps_argv, run_ps
from app.config_persist import save_tweak_snapshot  # noqa: F401 - re-exported



if IS_WINDOWS:
    import winreg


def apply_network_throttling(ctx: TaskContext):
    # M3: snapshot priors — the old revert hardcoded 0xA/20.
    had_snapshot = bool(get_tweak_snapshot("network_throttling"))
    _snap_reg_values(ctx, "network_throttling", _NET_THROTTLE_SPECS)
    try:
        reg_set_value_checked(ctx, "HKLM",
                      "SOFTWARE\\Microsoft\\Windows NT\\CurrentVersion\\Multimedia\\SystemProfile",
                      "NetworkThrottlingIndex", 0xFFFFFFFF)
        # SystemResponsiveness: 10 = reserve 10% CPU for background (values <10 treated as 20%)
        reg_set_value_checked(ctx, "HKLM",
                      "SOFTWARE\\Microsoft\\Windows NT\\CurrentVersion\\Multimedia\\SystemProfile",
                      "SystemResponsiveness", 10)
    except Exception:
        if not had_snapshot:
            clear_tweak_snapshot("network_throttling")
        raise


def revert_network_throttling(ctx: TaskContext):
    snap = get_tweak_snapshot("network_throttling")
    if snap and "specs" in snap:
        _restore_reg_values(ctx, "network_throttling")
    else:
        reg_set_value_checked(ctx, "HKLM",
                      "SOFTWARE\\Microsoft\\Windows NT\\CurrentVersion\\Multimedia\\SystemProfile",
                      "NetworkThrottlingIndex", 0xA)
        reg_set_value_checked(ctx, "HKLM",
                      "SOFTWARE\\Microsoft\\Windows NT\\CurrentVersion\\Multimedia\\SystemProfile",
                      "SystemResponsiveness", 20)
        ctx.log("  (no snapshot found — restored to documented Windows defaults instead)")


def apply_disable_nagle(ctx: TaskContext):
    base = "SYSTEM\\CurrentControlSet\\Services\\Tcpip\\Parameters\\Interfaces"
    if not IS_WINDOWS:
        return
    try:
        subs = _network_interface_subs()
    except FileNotFoundError:
        # B5 audit fix: this used to log and fall off the end of the
        # function — the runner counted it applied although nothing was
        # written. A machine with no Interfaces key has nothing to change.
        raise TaskSkipped("No network interfaces found — nothing to tweak.")
    if not subs:
        raise TaskSkipped("No network interfaces found — nothing to tweak.")
    # MED-007: this is the worst case in the finding. It writes TcpAckFrequency
    # and TCPNoDelay on EVERY adapter, and the old revert blind-deleted both
    # from EVERY adapter — so a user who had deliberately set TcpAckFrequency=2
    # (the Windows delayed-ACK default) on their VPN interface lost that
    # setting, and one who had set TCPNoDelay=0 to re-enable Nagle for a
    # specific link lost that too. Snapshot each (adapter, value) pair.
    writes = []
    for sub in subs:
        for name in ("TcpAckFrequency", "TCPNoDelay"):
            writes.append(("HKLM", "%s\\%s" % (base, sub), name, 1))
    _regn_apply(ctx, "disable_nagle", writes)


def revert_disable_nagle(ctx: TaskContext):
    if not IS_WINDOWS:
        return
    # MED-007: was a blind reg_delete_value of both values on every adapter.
    # Now restores each value to what that adapter actually had, and leaves
    # adapters that appeared since apply completely alone (they were never
    # in the snapshot, so they are not in the spec list).
    _regn_revert(ctx, "disable_nagle", "Nagle optimisation")


def apply_eee_disable(ctx: TaskContext):
    """Disable Energy Efficient Ethernet / Green Ethernet on active NICs —
    the documented cause of micro-disconnects and speed drops on Realtek
    NICs. Snapshots each adapter's exact prior display value first (valid
    values are driver-defined: Realtek says Disabled/Enabled, others say
    Off/On — a hardcoded 'Enabled' revert would be wrong on some NICs)."""
    ctx.set_status("Disabling Energy Efficient Ethernet...")
    props = _query_eee_props()
    if not props:
        raise RuntimeError("No Energy Efficient Ethernet-capable adapter found.")
    snapshot = {"adapters": {}}                    # {adapter: {display: prior_value}}
    for adapter, display, value in props:
        snapshot["adapters"].setdefault(adapter, {})[display] = value
    # C5: a failed apply must not leave this snapshot behind (false badge).
    had_snapshot = bool(get_tweak_snapshot("eee_disable"))
    save_tweak_snapshot("eee_disable", snapshot)
    changed = 0
    try:
        for adapter, display, _value in props:
            # P7-02: honor Stop between adapters — a cancel must surface as
            # TaskCancelled, never as a half-applied 'success' or a bogus
            # 'no adapter accepted the change' error.
            if ctx.cancelled():
                raise TaskCancelled("EEE disable stopped by user (Undo can restore the partial state).")
            # shell=False argv (no cmd.exe mangling) + ''-escaped names.
            rc = run_ps(
                ctx,
                f"Set-NetAdapterAdvancedProperty -Name '{_ps_sq(adapter)}' "
                f"-DisplayName '{_ps_sq(display)}' -DisplayValue 'Disabled'",
                timeout=60)
            if rc == 0:
                changed += 1
            else:
                ctx.log(f"  ! {adapter}/{display}: could not set (code {rc}) — this NIC may use different values.")
        if not changed:
            # honest failure: nothing accepted the change — never claim success
            raise RuntimeError("No adapter accepted the change (NICs vary — check your adapter's own advanced tab).")
    except TaskCancelled:
        # keep the snapshot: a partial apply must stay undoable; the C5
        # 'no false badge' cleanup only applies to real failures.
        raise
    except Exception:
        if not had_snapshot:
            clear_tweak_snapshot("eee_disable")
        raise
    ctx.log(f"EEE disabled on {changed} propert(ies). Brief network blip is normal; reconnect if you were in a game.")


def revert_eee_disable(ctx: TaskContext):
    """Restore each adapter's exact prior display values from the snapshot
    (driver-defined strings, per the M1/M6 lesson). Adapters that vanished
    between apply and revert (USB NICs) just fail their Set- call and log —
    best-effort, mirror of revert_gaming_dns."""
    ctx.set_status("Re-enabling Energy Efficient Ethernet...")
    snap = get_tweak_snapshot("eee_disable")
    adapters = snap.get("adapters", {}) if snap else {}
    if not adapters:
        raise RuntimeError("No snapshot on file — cannot know the prior values. Set them manually in Device Manager.")
    restored = 0
    total = 0
    for adapter, props in adapters.items():
        for display, value in props.items():
            total += 1
            # P7-02: honor Stop mid-revert — snapshot stays on file (only
            # the 'nothing restored' path below decides the failure text).
            if ctx.cancelled():
                raise TaskCancelled("EEE restore stopped by user — snapshot kept for Undo.")
            rc = run_ps(ctx,
                        f"Set-NetAdapterAdvancedProperty -Name '{_ps_sq(adapter)}' "
                        f"-DisplayName '{_ps_sq(display)}' -DisplayValue '{_ps_sq(value)}'",
                        timeout=60)
            if rc == 0:
                restored += 1
            else:
                ctx.log(f"  ! {adapter}/{display}: could not restore (code {rc}) — adapter may be gone.")
    # C6: keep the snapshot when NOTHING restored so Undo can be retried.
    if restored == 0:
        raise RuntimeError("No EEE setting could be restored — snapshot kept so Undo can be retried.")
    # B-021: a PARTIAL restore must also keep the snapshot — clearing it here
    # discarded the only recorded prior value for every adapter that failed,
    # making those NICs permanently un-restorable (mirror revert_gaming_dns).
    if restored < total:
        raise RuntimeError(
            f"Restored only {restored} of {total} EEE setting(s) — snapshot kept "
            "so Undo can be retried for the rest.")
    clear_tweak_snapshot("eee_disable")
    ctx.log("Energy Efficient Ethernet restored to previous values.")


def apply_disable_dynamic_tick(ctx: TaskContext):
    """Disable the dynamic timer tick (bcdedit) — a legacy latency tweak.
    Gains are debatable on modern hardware; kept opt-in for users who
    benchmark. Reboot required."""
    # B-0xx: snapshot the real prior value. Undo used to blind-delete the
    # element, so a machine that had deliberately set disabledynamictick=No (or
    # some other value) had that restored to the Windows default instead of
    # what it actually was.
    had_snapshot = bool(get_tweak_snapshot("disable_dynamic_tick"))
    if not had_snapshot:
        save_tweak_snapshot("disable_dynamic_tick",
                            {"disabledynamictick": _bcd_value("disabledynamictick")})
    try:
        run_cmd_checked(ctx, "bcdedit /set disabledynamictick yes")
    except Exception:
        if not had_snapshot:
            clear_tweak_snapshot("disable_dynamic_tick")
        raise
    ctx.log("Dynamic tick disabled. Reboot required.")


def revert_disable_dynamic_tick(ctx: TaskContext):
    snap = get_tweak_snapshot("disable_dynamic_tick") or {}
    prior = snap.get("disabledynamictick")
    if prior:
        # Restore the exact prior element value (bcdedit accepts the same
        # Yes/No/True/False keywords /enum printed).
        rc = run_cmd(ctx, ["bcdedit", "/set", "disabledynamictick", str(prior)],
                     shell=False, timeout=30)
        if rc != 0:
            raise RuntimeError(
                "Could not restore the disabledynamictick element — snapshot "
                "kept so Undo can be retried.")
    else:
        # No snapshot (older app version) or the element did not exist before:
        # removing it is the documented Windows default. /deletevalue exits 1
        # when the value doesn't exist — that's a no-op, not a failure.
        rc = run_cmd(ctx, "bcdedit /deletevalue disabledynamictick")
        if rc != 0:
            ctx.log("  (value not present — nothing to remove)")
    clear_tweak_snapshot("disable_dynamic_tick")
    ctx.log("Dynamic tick restored to Windows default. Reboot required.")


def apply_gaming_dns(ctx: TaskContext, servers=None):
    """Switch every active adapter's DNS (default Cloudflare 1.1.1.1 /
    1.0.0.1), snapshotting the prior static servers first so revert is
    exact. 1.1.1.1 is consistently among the fastest public resolvers
    for game server lookups; fully reversible, no reset needed.

    servers: optional (primary, secondary) pair — the DNS tester dialog
    passes the measured winner here. Default None keeps the classic
    Cloudflare behavior byte-identical (same command, same snapshot,
    same logs for the default pair)."""
    if servers is None:
        servers = tuple(_DNS_CLOUDFLARE)
    else:
        servers = (str(servers[0]), str(servers[1]))
    _label = "Cloudflare" if list(servers) == _DNS_CLOUDFLARE else " / ".join(servers)
    ctx.set_status(f"Switching DNS to {_label}...")
    if not IS_WINDOWS:
        raise RuntimeError("Windows only.")
    import subprocess as _sp
    try:
        out = _sp.run(
            ps_argv("Get-NetAdapter | Where-Object Status -eq 'Up' | "
                    "ForEach-Object { $d = Get-DnsClientServerAddress -InterfaceIndex $_.ifIndex -AddressFamily IPv4 -ErrorAction SilentlyContinue; "
                    "'ADAPT|' + $_.ifIndex + '|' + ($d.ServerAddresses -join ',') }"),
            capture_output=True, text=True, timeout=30,
            creationflags=getattr(_sp, "CREATE_NO_WINDOW", 0),
        )
    except Exception as exc:
        raise RuntimeError(f"Could not list network adapters: {exc}")
    snapshot = _dns_snapshot_key()
    switched = 0
    for line in (out.stdout or "").splitlines():
        if not line.startswith("ADAPT|"):
            continue
        # B-1 audit fix: the unpack target MUST NOT be named `servers` — it
        # shadowed the function parameter holding the NEW pair to apply, so
        # _dns_set_command received each adapter's OLD value (a comma-joined
        # string, iterated char-by-char into garbage single-char 'servers')
        # and the chosen pair was never set. `prior` is the adapter's current
        # DNS (snapshot use only); the set below uses the parameter.
        _, idx, prior = line.split("|", 2)
        idx = idx.strip()
        if not idx.isdigit():
            continue
        snapshot["adapters"][idx] = prior.strip() or None   # None = DHCP
        # H4: netsh `name=` wants the adapter NAME, not the numeric ifIndex
        # (passing the index fails or hits the wrong NIC). The PowerShell
        # cmdlet takes -InterfaceIndex directly — unambiguous, shell=False
        # argv so no quoting/injection surface. NOTE: ifIndex can change
        # across reboots/NIC swaps; revert only touches these snapshot
        # indexes and reports misses honestly.
        rc = run_cmd(ctx, _dns_set_command(idx, servers),
                     shell=False, timeout=30)
        if rc == 0:
            switched += 1
        else:
            ctx.log(f"  ! could not set DNS on adapter index {idx} (exit {rc}) — skipping it.")
    if not snapshot["adapters"]:
        raise RuntimeError("No active network adapters found — nothing to switch.")
    # H4/C5: saving before verifying left a snapshot + success log on 0
    # switched adapters (false badge). Only save when something changed.
    if switched == 0:
        raise RuntimeError(
            "Could not switch DNS on any adapter — nothing was changed "
            "(snapshot NOT saved, tweak not marked applied)."
        )
    save_tweak_snapshot("gaming_dns", snapshot)
    ctx.log(f"DNS switched to {_label} on {switched} adapter(s). (Snapshot saved for undo.)")


def revert_gaming_dns(ctx: TaskContext):
    """Restore each adapter's exact prior DNS — including DHCP (no static
    servers), which is recorded as None in the snapshot. Only ever touches
    adapters present in the snapshot: with no snapshot there is nothing
    this app configured, so it refuses to rewrite the network config
    (a blanket DHCP reset would destroy static DNS the app never set)."""
    ctx.set_status("Restoring previous DNS settings...")
    if not IS_WINDOWS:
        raise RuntimeError("Windows only.")
    snap = get_tweak_snapshot("gaming_dns")
    adapters = snap.get("adapters", {}) if snap else {}
    if not adapters:
        # B3 fix (eee_disable contract): this used to fall back to a
        # "best-effort" Set-DnsClientServerAddress -ResetServerAddresses
        # over EVERY up adapter, wiping pre-existing static DNS this app
        # never configured. No snapshot -> raise before running anything.
        raise RuntimeError(
            "No DNS snapshot on file — cannot know the prior DNS servers "
            "to restore. Restore your DNS manually: Settings > Network & "
            "Internet > your adapter > DNS (or ask your network "
            "administrator / ISP for the correct servers)."
        )
    import re as _re
    failures: list = []
    for idx, servers in adapters.items():
        if not str(idx).strip().isdigit():
            failures.append(str(idx))
            continue
        if servers:  # had static servers before — validate (snapshot lives
            # in user-writable config.json; never interpolate raw into PS)
            toks = [s.strip() for s in str(servers).split(",") if s.strip()]
            if not toks or not all(_re.fullmatch(r"[0-9.]+", t) for t in toks):
                ctx.log(f"  ! snapshot for adapter {idx} has unexpected servers {servers!r} — skipping it.")
                failures.append(str(idx))
                continue
            quoted = ",".join(f"'{t}'" for t in toks)
            rc = run_ps(ctx,
                        f"Set-DnsClientServerAddress -InterfaceIndex {idx} "
                        f"-ServerAddresses ({quoted})",
                        timeout=30)
            if rc != 0:
                failures.append(str(idx))
        else:        # was DHCP — clear static servers back to automatic
            rc = run_ps(ctx,
                        f"Set-DnsClientServerAddress -InterfaceIndex {idx} "
                        "-ResetServerAddresses",
                        timeout=30)
            if rc != 0:
                failures.append(str(idx))
    # C6: keep the snapshot when any adapter failed so Undo can be retried.
    if failures:
        raise RuntimeError(
            f"Could not restore DNS on adapter(s) {', '.join(failures)} — "
            "snapshot kept so Undo can be retried. Restore manually: "
            "Settings > Network & Internet > your adapter > DNS."
        )
    clear_tweak_snapshot("gaming_dns")
    ctx.log("DNS restored to previous settings.")


def apply_prefer_ipv4(ctx: TaskContext):
    """Prefer IPv4 over IPv6 for gaming (fixes lag on bad IPv6 routers).
    Sets Microsoft's documented 0x20 flag - IPv6 stays enabled, IPv4 just
    wins. Needs reboot. Snapshot + revert restores the exact prior value.

    BUG-015: this was the one apply_* of ~30 missing the failure branch the
    siblings all use. The snapshot was written and then the system change
    could raise, leaving a snapshot for a tweak that was never applied \u2014 so
    get_tweak_state() unions the snapshot into the applied set, the GUI showed
    a green "Active" badge for a change that never happened, and because
    save_tweak_snapshot refuses to overwrite an existing non-empty snapshot,
    a retry could not repair it either. Same had_snapshot/clear/raise
    wrapper as apply_visual_effects_perf and the rest."""
    had_snapshot = bool(get_tweak_snapshot("prefer_ipv4"))
    _snap_reg_values(ctx, "prefer_ipv4", _IPV4_SPECS)
    try:
        reg_set_value_checked(ctx, "HKLM", _IPV4_PATH, "DisabledComponents", 0x20)
    except Exception:
        if not had_snapshot:
            clear_tweak_snapshot("prefer_ipv4")
        raise
    ctx.log("IPv4 preferred for gaming (reboot to take effect).")


def revert_prefer_ipv4(ctx: TaskContext):
    _restore_reg_values(ctx, "prefer_ipv4")
    ctx.log("IP version preference restored (reboot to take effect).")


def verify_prefer_ipv4() -> "bool | None":
    try:
        v = __verify_read("HKLM", _IPV4_PATH, "DisabledComponents")
        if v is None:
            return None
        return int(v) == 0x20
    except Exception:
        return None


def verify_network_throttling() -> "bool | None":
    return _verify_value("HKLM", "SOFTWARE\\Microsoft\\Windows NT\\CurrentVersion\\Multimedia\\SystemProfile",
                         "NetworkThrottlingIndex", 0xFFFFFFFF)


def verify_disable_nagle() -> "bool | None":
    """Nagle applies to every interface subkey — check the first readable
    one. TcpAckFrequency=1/TCPNoDelay=1 after apply; revert deletes them."""
    try:
        key = _open_interfaces_key()
        try:
            i = 0
            while True:
                try:
                    sub = winreg.EnumKey(key, i)
                except OSError:
                    break
                i += 1
                base = f"SYSTEM\\CurrentControlSet\\Services\\Tcpip\\Parameters\\Interfaces\\{sub}"
                v1 = __verify_read("HKLM", base, "TcpAckFrequency")
                v2 = __verify_read("HKLM", base, "TCPNoDelay")
                if v1 is None and v2 is None:
                    continue   # adapter with neither value (e.g. read-only)
                return (v1 == 1 and v2 == 1)
        finally:
            winreg.CloseKey(key)
    except Exception:
        return None
    return None
