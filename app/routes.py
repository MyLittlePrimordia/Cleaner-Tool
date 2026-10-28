"""Where a recommendation becomes a screen.

AUDIT TX-003 / TL-009 (2026-10). Every tool used to end at its own window:
Process Manager could show a service-backed process but not open Services,
Storage could measure duplicate bytes but not open Duplicates, and Health kept
its own private routing table. That made the app a set of dead ends and put
the same target -> button mapping in more than one file. (The table originally
served the Advisor as well; that window and What Changed came out in the same
audit's later pass, so their rows are gone too.)

This module is the one table. It is deliberately data, not control flow:

    route = routes.get("duplicates")
    if route:
        routes.open_route(app, "duplicates")

The table stores a stable target name, not a widget. Consumers that only need
to label an action use `label()`; consumers that have an Application use
`open_route()`. Adding a tool means adding one row here, and a guard test can
check that every published finding resolves to a real destination.

Tk-free at import time. External destinations are imported inside
`open_route()` only, so importing this module never starts a subprocess.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class Route:
    """One destination: a tab, an Application method, or an external screen."""

    kind: str                    # "tab" | "method" | "external"
    tab: str = ""
    method: str = ""
    label: str = "Open"


#: The canonical table. Keys are the targets findings and Health already use;
#: aliases below keep older spellings working while callers migrate.
ROUTES = {
    # --- Tools popups -------------------------------------------------- #
    "storage": Route("method", method="_open_storage_insight",
                     label="Open Storage"),
    "duplicates": Route("method", method="_open_duplicates",
                        label="Open Duplicates"),
    "startup": Route("method", method="_open_startup_manager",
                     label="Open Startup"),
    "services": Route("method", method="_open_services",
                      label="Open Services"),
    "procmgr": Route("method", method="_open_process_manager",
                     label="Open Processes"),
    "drivetool": Route("method", method="_open_drive_toolkit",
                       label="Open Drive Toolkit"),
    "readiness": Route("method", method="_open_game_readiness",
                       label="Open Game Readiness"),
    "uninstall": Route("method", method="_open_uninstall_programs",
                       label="Open Uninstall"),
    "file_lock": Route("method", method="_open_file_lock",
                       label="Open File Lock"),
    "specs": Route("method", method="_open_pc_specs", label="Open PC Specs"),
    "safety_center": Route("method", method="_open_safety_center",
                           label="Open Safety & Undo"),
    "game_saves": Route("method", method="_open_game_saves",
                        label="Open Game Saves"),
    "restore_points": Route("method", method="_open_restore_points",
                            label="Open Restore Points"),
    "dns": Route("method", method="_open_dns_tester", label="Open DNS Test"),
    "speed": Route("method", method="_open_speed_test", label="Open Speed Test"),
    "gameping": Route("method", method="_open_game_server_ping",
                      label="Open Server Ping"),
    "hwmonitor": Route("method", method="_open_hw_monitor",
                       label="Open Hardware Monitor"),
    "network": Route("method", method="_open_network_connections",
                     label="Open Network Connections"),
    "updates": Route("method", method="_open_update_picker",
                     label="Open Windows Updates"),
    # 2026-10: the seven peripheral-tester rows (pad, keyboard, mouse,
    # display, camera, mic, speakers) are gone with their windows. Nothing
    # emitted those targets, so a row here would only advertise a screen the
    # app no longer has.
    "pilot": Route("method", method="_open_pilot_dialog",
                   label="Open Auto-Pilot"),

    # --- Health history ------------------------------------------------ #
    "health_history": Route("method", method="_open_health_history",
                            label="Open History"),
    # U-005: the old driver packages. Owned by the Drivers tab, which is why
    # the method opens it THROUGH that tab rather than as a bare registry
    # dialog - see Application._open_driver_store.
    "driver_store": Route("method", method="_open_driver_store",
                          label="Open Driver store"),
    "battery_report": Route("method", method="_open_battery_report",
                            label="See battery report"),

    # --- main tabs ------------------------------------------------------ #
    # Every main tab is in this table, not just the ones a finding happens to
    # point at, so a route never depends on which screen happens to link it.
    "clean": Route("tab", tab="Clean", label="Open Clean"),
    "repair": Route("tab", tab="Repair", label="Open Repair"),
    "tweak": Route("tab", tab="Tweak", label="Open Tweak"),
    "health": Route("tab", tab="Health", label="Open Health"),
    "install": Route("tab", tab="Install", label="Open Install"),
    "drivers": Route("tab", tab="Drivers", label="Open Drivers"),
    "tools": Route("tab", tab="Tools", label="Open Tools"),
    "settings": Route("tab", tab="Settings", label="Open Settings"),

    # --- external Windows screens -------------------------------------- #
    "reliability": Route("external", method="open_reliability_monitor",
                         label="Open Reliability"),
}

#: Older names that still appear in stored findings or dialog code. Aliases
#: are resolved before lookup, so publishers do not have to rewrite history.
_ALIASES = {
    "drivetoolkit": "drivetool",
    "game_readiness": "readiness",
    "pc_specs": "specs",
    "safety": "safety_center",
    "network_connections": "network",
    "hw_monitor": "hwmonitor",
    "game_server_ping": "gameping",
    "uninstall_programs": "uninstall",
    "filelock": "file_lock",
    "restore_point": "restore_points",
    "windows_updates": "updates",
    "autopilot": "pilot",
    "procman": "procmgr",
    # The seven peripheral aliases (`gamepad_tester`, `keyboard_tester`,
    # `mouse_tester`, `monitor_test`, `webcam_test`, `mic`/`microphone`,
    # `speaker_test`) are gone with their windows: they pointed at targets this
    # table no longer holds, so they only spelled a missing screen differently.
}


def normalise(target) -> str:
    """The canonical key for `target`, or '' when it is not a route."""
    key = str(target or "").strip()
    return _ALIASES.get(key, key)


def get(target) -> Optional[Route]:
    """The Route for `target`, or None. Never raises."""
    return ROUTES.get(normalise(target))


def label(target, default="Open") -> str:
    """Readable action text for a target, or `default` when unknown."""
    route = get(target)
    return route.label if route is not None else str(default)


def targets() -> tuple:
    """Every canonical target, sorted (tests and diagnostics)."""
    return tuple(sorted(ROUTES))


def open_route(app, target) -> bool:
    """Open the screen for `target` through `app`.

    Returns True when the destination was dispatched. Never raises: a broken
    route must not take down the surface that offered it. A tab switch that
    Application declined (busy) still reports True because the request was
    valid; the app owns that policy.
    """
    route = get(target)
    if route is None:
        return False
    try:
        if route.kind == "tab":
            app._switch_to(route.tab)
        elif route.kind == "method":
            getattr(app, route.method)()
        elif route.kind == "external":
            from app.health_scan import open_reliability_monitor
            open_reliability_monitor()
        else:
            return False
        return True
    except Exception:
        return False


__all__ = ["Route", "ROUTES", "get", "label", "normalise", "open_route",
           "targets"]
