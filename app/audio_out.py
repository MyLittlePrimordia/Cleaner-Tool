"""
Render-endpoint listing (PC Specs, Tools tab audio rows) — Tk-free.

This module used to carry two more engines: the COM default-device flip
(IPolicyConfig::SetDefaultEndpoint) and the directional test tones the
speaker tester played on each output. That window was deleted in the
2026-10 sweep, and with it the only caller of both — what is left is the
piece a live caller still reads: which outputs exist, and what Windows
calls them.

Deliberately COM-free: endpoint ACTIVATION can wedge process-wide after
display queries on some multimedia stacks (observed: every
CoCreateInstance wedges while plain registry/user32 calls sail through),
and a frozen picker helps nobody. Registry reads are instant, need no
admin, and never raise.
"""

from __future__ import annotations


def _reg_render_endpoints():
    """[{id, name, state}] straight from the MMDevices registry hive —
    the same source repair_tasks.list_capture_devices uses for inputs.

    Never raises."""
    out = []
    try:
        import winreg as _wr
        base = "SOFTWARE\\Microsoft\\Windows\\CurrentVersion" \
               "\\MMDevices\\Audio\\Render"
        with _wr.OpenKey(_wr.HKEY_LOCAL_MACHINE, base, 0,
                         _wr.KEY_READ) as root:
            n = _wr.QueryInfoKey(root)[0]
            for i in range(n):
                try:
                    sub = _wr.EnumKey(root, i)
                except OSError:
                    continue
                try:
                    with _wr.OpenKey(root, sub, 0, _wr.KEY_READ) as ek:
                        try:
                            state, _ = _wr.QueryValueEx(ek, "DeviceState")
                            state = int(state)
                        except OSError:
                            state = -1
                        name = ""
                        try:
                            with _wr.OpenKey(ek, "Properties") as pk:
                                try:
                                    name, _ = _wr.QueryValueEx(
                                        pk, "{a45c254e-df1c-4efd-8020-67d146a850e0},2")
                                except OSError:
                                    pass
                                # Interface name disambiguates identical
                                # labels ("Speakers" x3) — same join the
                                # capture list uses: "Speakers (USB Audio)".
                                try:
                                    iface, _ = _wr.QueryValueEx(
                                        pk, "{b3f8fa53-0004-438e-9003-51a46e139bfc},6")
                                except OSError:
                                    iface = ""
                                if iface and iface != name:
                                    name = f"{name} ({iface})" if name else str(iface)
                        except OSError:
                            pass
                        out.append({"id": "{0.0.0.00000000}." + sub,
                                    "name": str(name or "") or sub,
                                    "state": state})
                except OSError:
                    continue
    except Exception:
        pass
    return out


def list_render_endpoints(timeout_s=8.0):
    """[{id, name, state}] for every render endpoint. Never raises.

    Registry reads (COM-free by design — see _reg_render_endpoints);
    timeout_s is accepted for API compatibility and ignored."""
    try:
        return list(_reg_render_endpoints())
    except Exception:
        return []
