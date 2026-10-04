"""
Startup Manager (feature: Tools tab card) — see what launches when
Windows signs in, and turn any of it off with one toggle.

Scope, deliberately: Registry Run keys (HKCU + HKLM, including the
32-bit view on 64-bit Windows) and Startup-folder shortcuts (the current
user's and the All-Users one). Between them these two mechanisms cover
the overwhelming majority of real-world "why does Discord/Steam/Spotify
launch at boot" cases.

Task Scheduler is deliberately NOT covered here. Windows registers a
large number of its own maintenance tasks there, and reliably telling
"a genuine startup app someone installed" apart from "a Windows or
driver task that only looks similar" needs parsing `schtasks`' fragile
verbose output (or a PowerShell trigger-type filter) with no safe way
to test the parser here before it runs on someone's real machine. Get
this wrong and a toggle could disable something that isn't a startup
app at all. Registry + Startup folder can be verified confidently
without a live Windows box; Task Scheduler could not, so it stays out
for now rather than shipping a guess.

Every entry this module disables is reversible from data this module
itself records — nothing is ever just deleted:
  * Run-key values: the exact name/data/type are captured before the
    value is removed, so re-enabling writes back the identical value.
  * Startup-folder shortcuts: the file is MOVED into a holding folder
    under this app's own config directory, never deleted — re-enabling
    moves it back.

Reuses app.utils' registry helpers (reg_set_value / reg_delete_value)
rather than raw winreg calls, so the 64-bit-view handling and error
logging stay identical to every other registry-touching task in this
app. Those helpers take a TaskContext for logging; this module builds
one throwaway ctx internally rather than asking callers to supply one,
since Startup Manager's UI is a self-contained dialog, not a run-engine
task.
"""

from __future__ import annotations

import os

IS_WINDOWS = os.name == "nt"

_RUN_PATH = r"Software\Microsoft\Windows\CurrentVersion\Run"

# BUG-009: Windows 10/11 keep a per-item enabled/disabled flag for Run entries
# in a parallel key, and that flag -- not the presence of the Run value -- is
# what decides whether the entry launches. Task Manager's Startup tab, Autoruns
# and msconfig all write it; it was never read or written here (0 hits
# repo-wide), which caused two defects:
#
#   * list_startup_items() hardcoded "enabled": True, so an entry the user had
#     already disabled natively showed as ON;
#   * re-enabling restored the Run value but left the marker saying disabled,
#     so Windows still did not launch it -- while the UI reported
#     "X will run at sign-in again." That is a false success with no way for
#     the user to learn why.
#
# The value is a 12-byte blob: 8 bytes of flags (only the first is used) then a
# 4-byte FILETIME. 02 = enabled, 03 = disabled, 06/07 = enabled-by-policy.
# It lives under the Explorer key, NOT under Run.
_STARTUP_APPROVED = (r"Software\Microsoft\Windows\CurrentVersion"
                     r"\Explorer\StartupApproved")
_STARTUP_APPROVED_RUN = _STARTUP_APPROVED + r"\Run"
_STARTUP_APPROVED_RUN32 = _STARTUP_APPROVED + r"\Run32"
#: First byte values meaning "Windows will launch this".
_APPROVED_ENABLED_BYTES = (0x02, 0x06)

# (hive, is_64bit_view, source_key) — the four places Windows itself
# reads Run entries from at logon. WOW6432Node is where a 32-bit
# installer's Run entry lands on 64-bit Windows; without checking it
# separately, half of older/simpler installers' entries are invisible.
_RUN_LOCATIONS = (
    ("HKCU", False, "hkcu_run"),
    ("HKLM", False, "hklm_run"),
    ("HKLM", True, "hklm_run_wow64"),
)

_ADMIN_SOURCES = {"hklm_run", "hklm_run_wow64", "startup_common"}


def _approved_key_path(wow64: bool) -> str:
    return _STARTUP_APPROVED_RUN32 if wow64 else _STARTUP_APPROVED_RUN


def _approved_state(hive: str, wow64: bool, name: str):
    """True / False / None -- enabled, disabled, or "Windows has no opinion".

    None means there is no StartupApproved marker, which is the normal case on
    Windows 7/8 and on any entry Windows has never been told about; the Run
    value alone then governs, i.e. it is enabled.
    """
    if not IS_WINDOWS:
        return None
    try:
        import winreg
        root = winreg.HKEY_CURRENT_USER if hive == "HKCU" else winreg.HKEY_LOCAL_MACHINE
        access = winreg.KEY_READ
        if wow64:
            access |= getattr(winreg, "KEY_WOW64_64KEY", 0)
        key = winreg.OpenKey(root, _approved_key_path(wow64), 0, access)
        try:
            data, _t = winreg.QueryValueEx(key, name)
        finally:
            key.Close()
        if not isinstance(data, (bytes, bytearray)) or not data:
            return None
        return data[0] in _APPROVED_ENABLED_BYTES
    except FileNotFoundError:
        return None
    except OSError:
        return None
    except Exception:
        return None


def _write_approved(hive: str, wow64: bool, name: str, enabled: bool,
                    log=None) -> bool:
    """Write the StartupApproved marker. True when the write (or the already-
    correct state) leaves Windows agreeing with `enabled`."""
    if not IS_WINDOWS:
        return False
    try:
        import winreg
        root = winreg.HKEY_CURRENT_USER if hive == "HKCU" else winreg.HKEY_LOCAL_MACHINE
        access = winreg.KEY_READ | winreg.KEY_WRITE | winreg.KEY_SET_VALUE
        if wow64:
            access |= getattr(winreg, "KEY_WOW64_64KEY", 0)
        key = winreg.CreateKeyEx(root, _approved_key_path(wow64), 0, access)
        try:
            existing = None
            try:
                existing, _t = winreg.QueryValueEx(key, name)
            except FileNotFoundError:
                existing = None
            want = 0x02 if enabled else 0x03
            if isinstance(existing, (bytes, bytearray)) and existing \
                    and existing[0] == want:
                return True          # already what we want
            # 8 flag bytes then a 4-byte FILETIME. Windows only reads the
            # first byte, but the blob is expected to be 12 bytes.
            # (Build it as one bytearray: bytearray([x]) + [0]*7 is a
            # bytearray-plus-list TypeError, which the except below would
            # swallow into a silent "could not write".)
            blob = bytes(bytearray([want, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0]))
            winreg.SetValueEx(key, name, 0, winreg.REG_BINARY, blob)
        finally:
            key.Close()
        if log:
            log("Startup Manager: %s StartupApproved marker for %s"
                % ("enabled" if enabled else "disabled", name))
        return True
    except Exception as exc:
        if log:
            log("  (could not write the StartupApproved marker for %s: %s)"
                % (name, exc))
        return False


def _make_ctx(log=None):
    """Throwaway TaskContext so this module can reuse app.utils' proven
    registry helpers (64-bit view handling, consistent error logging)
    without asking every caller to build one."""
    from app.utils import TaskContext
    return TaskContext(log=log or (lambda *_a: None),
                       set_status=lambda *_a: None)


def _startup_folders():
    """{"startup_user": path, "startup_common": path} — resolved from
    the environment rather than hard-coded, so this works whether or
    not the user has redirected their profile folders."""
    out = {}
    appdata = os.environ.get("APPDATA")
    if appdata:
        out["startup_user"] = os.path.join(
            appdata, "Microsoft", "Windows", "Start Menu", "Programs", "Startup")
    programdata = os.environ.get("ProgramData")
    if programdata:
        out["startup_common"] = os.path.join(
            programdata, "Microsoft", "Windows", "Start Menu", "Programs", "Startup")
    return out


def _holding_dir():
    """Where disabled Startup-folder shortcuts live until re-enabled —
    inside this app's own config directory, never inside Windows'
    folders, so nothing here is mistaken for a real startup item by
    Windows or by Autoruns-style tools."""
    from app.config_persist import CONFIG_DIR
    path = os.path.join(str(CONFIG_DIR), "disabled_startup")
    os.makedirs(path, exist_ok=True)
    return path


_LABEL_SOURCE = {
    "hkcu_run": "Runs at sign-in for your account",
    "hklm_run": "Runs at sign-in for everyone on this PC",
    "hklm_run_wow64": "Runs at sign-in for everyone on this PC",
    "startup_user": "Shortcut in your Startup folder",
    "startup_common": "Shortcut in the shared Startup folder",
}


def list_startup_items():
    """Every enabled AND disabled item this module knows about, merged
    into one list the UI can render as a flat set of toggles:

        [{"id", "name", "command", "source", "source_label",
          "admin_required", "enabled"}, ...]

    "id" is stable across a disable/enable round-trip (f"{source}:{name}"),
    so the UI can find the same row again after a toggle rebuilds the
    list. Never raises: any single source that fails to enumerate is
    skipped rather than aborting the whole list — one broken registry
    view should not hide every other entry."""
    if not IS_WINDOWS:
        return []
    items = []
    seen_ids = set()

    for hive, wow64, source in _RUN_LOCATIONS:
        for name, data, _vtype in _enum_run_values(hive, wow64):
            item_id = f"{source}:{name}"
            if item_id in seen_ids:
                continue
            seen_ids.add(item_id)
            # BUG-009: ask Windows whether this entry will actually run,
            # instead of assuming yes.
            _en = _approved_state(hive, wow64, name)
            items.append({
                "id": item_id, "name": name, "command": data,
                "source": source, "source_label": _LABEL_SOURCE[source],
                "admin_required": source in _ADMIN_SOURCES,
                "enabled": True if _en is None else _en,
            })

    folders = _startup_folders()
    for source, folder in folders.items():
        for name, path in _enum_startup_folder(folder):
            item_id = f"{source}:{name}"
            if item_id in seen_ids:
                continue
            seen_ids.add(item_id)
            items.append({
                "id": item_id, "name": name, "command": path,
                "source": source, "source_label": _LABEL_SOURCE[source],
                "admin_required": source in _ADMIN_SOURCES, "enabled": True,
            })

    # disabled items we are holding, so the user can see and re-enable
    # them even though Windows itself no longer sees them at all
    try:
        from app.config_persist import get_startup_disabled
        for rec in get_startup_disabled():
            item_id = f"{rec.get('source')}:{rec.get('name')}"
            if item_id in seen_ids:
                continue  # shouldn't happen, but never show a duplicate
            seen_ids.add(item_id)
            items.append({
                "id": item_id, "name": rec.get("name", "?"),
                "command": rec.get("command", ""),
                "source": rec.get("source", ""),
                "source_label": _LABEL_SOURCE.get(rec.get("source"), "Disabled"),
                "admin_required": rec.get("source") in _ADMIN_SOURCES,
                "enabled": False,
            })
    except Exception:
        pass

    items.sort(key=lambda it: it["name"].lower())
    return items


def _enum_run_values(hive, wow64):
    """[(name, data, type_str)] for one Run key view. [] if the key
    does not exist or cannot be opened — both are normal, not errors
    (most machines have nothing in the HKLM WOW64 view, say)."""
    try:
        import winreg
    except Exception:
        return []
    root = winreg.HKEY_CURRENT_USER if hive == "HKCU" else winreg.HKEY_LOCAL_MACHINE
    access = winreg.KEY_READ
    if wow64:
        try:
            access |= winreg.KEY_WOW64_32KEY
        except AttributeError:
            pass
    else:
        try:
            access |= winreg.KEY_WOW64_64KEY
        except AttributeError:
            pass
    out = []
    key = None
    try:
        key = winreg.OpenKey(root, _RUN_PATH, 0, access)
        i = 0
        while True:
            try:
                name, data, vtype = winreg.EnumValue(key, i)
            except OSError:
                break
            i += 1
            if vtype == winreg.REG_SZ:
                out.append((name, str(data), "REG_SZ"))
            elif vtype == winreg.REG_EXPAND_SZ:
                out.append((name, str(data), "REG_EXPAND_SZ"))
            # Any other value type under Run is not a real startup
            # command (rare, non-standard) — skipped rather than
            # guessing how to redisplay or restore it faithfully.
    except FileNotFoundError:
        pass
    except Exception:
        pass
    finally:
        if key is not None:
            try:
                winreg.CloseKey(key)
            except Exception:
                pass
    return out


_SHORTCUT_EXTS = (".lnk", ".url", ".exe", ".bat", ".cmd", ".pif")


def _enum_startup_folder(folder):
    """[(display_name, full_path)] for one Startup folder. [] if the
    folder does not exist (normal on a fresh profile)."""
    out = []
    try:
        if not os.path.isdir(folder):
            return []
        for fname in os.listdir(folder):
            path = os.path.join(folder, fname)
            if not os.path.isfile(path):
                continue
            ext = os.path.splitext(fname)[1].lower()
            if ext not in _SHORTCUT_EXTS:
                continue
            name = os.path.splitext(fname)[0]
            out.append((name, path))
    except Exception:
        return []
    return out


def set_item_enabled(item_id, enabled, log=None):
    """Toggle one item by the id list_startup_items() gave it.

    Returns (ok: bool, message: str) — message is always a plain
    sentence suitable for showing directly in the dialog, success or
    failure alike (e.g. "Administrator rights are needed for this
    one." rather than a raised exception)."""
    try:
        source, name = item_id.split(":", 1)
    except ValueError:
        return False, "That item could not be found."

    from app.elevation import is_admin
    if source in _ADMIN_SOURCES and not is_admin():
        return False, ("Administrator rights are needed to change this one "
                       "— restart Cleaner Tool as Administrator.")

    if source in ("hkcu_run", "hklm_run", "hklm_run_wow64"):
        return _toggle_registry(source, name, enabled, log)
    if source in ("startup_user", "startup_common"):
        return _toggle_shortcut(source, name, enabled, log)
    return False, "That item's type is not recognised."


def _toggle_registry(source, name, enabled, log):
    hive = "HKCU" if source == "hkcu_run" else "HKLM"
    wow64 = source == "hklm_run_wow64"
    ctx = _make_ctx(log)
    if enabled:
        # Re-enable: restore the exact value we captured on disable.
        try:
            from app.config_persist import (get_startup_disabled,
                                            remove_startup_disabled)
            rec = next((r for r in get_startup_disabled()
                       if r.get("source") == source and r.get("name") == name),
                      None)
        except Exception:
            rec = None
        if rec is None:
            return False, "Nothing saved to restore for this item."
        ok = _reg_set_view(hive, wow64, name, rec.get("command", ""),
                          rec.get("value_type", "REG_SZ"), ctx)
        if ok:
            # BUG-009: restoring the Run value is not enough. If a
            # StartupApproved marker still says "disabled", Windows keeps
            # skipping the entry -- and the old code returned
            # "{name} will run at sign-in again." regardless, which is simply
            # false and left the user with no way to learn why. Clear the
            # marker too, and if we cannot, say that rather than claim success.
            _marked = _write_approved(hive, wow64, name, True,
                                      log=getattr(ctx, "log", None))
            try:
                remove_startup_disabled(source, name)
            except Exception:
                pass
            if not _marked:
                return (False, "%s was restored, but Windows still has it "
                               "marked as disabled in Task Manager > Startup. "
                               "Enable it there to finish." % name)
            return True, f"{name} will run at sign-in again."
        return False, "Could not write to the registry."
    else:
        # Disable: capture the live value BEFORE removing it, so it can
        # be restored byte-for-byte later.
        data, vtype = _reg_get_view(hive, wow64, name)
        if data is None:
            return False, "That item is no longer there."
        ok = _reg_delete_view(hive, wow64, name, ctx)
        if not ok:
            return False, "Could not remove it from the registry."
        try:
            from app.config_persist import add_startup_disabled
            add_startup_disabled({"source": source, "name": name,
                                  "command": data, "value_type": vtype})
        except Exception:
            # The registry change already succeeded; losing the undo
            # record would strand the user with no way back. Put the
            # value right back rather than leave it half-done.
            _reg_set_view(hive, wow64, name, data, vtype, ctx)
            return False, "Could not save how to undo this — left unchanged."
        # BUG-009: removing the Run value is enough to stop it launching, but
        # Windows also keeps a StartupApproved marker. If the user later
        # re-enables the entry from Windows' own Startup tab, a stale marker
        # would win. Clear it on the way out, so our undo record is the only
        # thing describing this entry's state.
        _write_approved(hive, wow64, name, False, log=log)
        return True, f"{name} will no longer run at sign-in."


def _reg_get_view(hive, wow64, name):
    """(data, type_str) for one Run value in a specific bitness view,
    or (None, None) if absent/unreadable. Direct winreg here (not
    utils.reg_get_value) because that helper always forces the 64-bit
    view — the WOW64 32-bit Run key needs the other view on purpose."""
    try:
        import winreg
    except Exception:
        return None, None
    root = winreg.HKEY_CURRENT_USER if hive == "HKCU" else winreg.HKEY_LOCAL_MACHINE
    access = winreg.KEY_READ
    try:
        access |= (winreg.KEY_WOW64_32KEY if wow64 else winreg.KEY_WOW64_64KEY)
    except AttributeError:
        pass
    key = None
    try:
        key = winreg.OpenKey(root, _RUN_PATH, 0, access)
        data, vtype = winreg.QueryValueEx(key, name)
        type_str = "REG_EXPAND_SZ" if vtype == winreg.REG_EXPAND_SZ else "REG_SZ"
        return str(data), type_str
    except Exception:
        return None, None
    finally:
        if key is not None:
            try:
                winreg.CloseKey(key)
            except Exception:
                pass


def _reg_set_view(hive, wow64, name, data, value_type, ctx):
    try:
        import winreg
    except Exception:
        return False
    root = winreg.HKEY_CURRENT_USER if hive == "HKCU" else winreg.HKEY_LOCAL_MACHINE
    access = winreg.KEY_WRITE
    try:
        access |= (winreg.KEY_WOW64_32KEY if wow64 else winreg.KEY_WOW64_64KEY)
    except AttributeError:
        pass
    key = None
    try:
        key = winreg.CreateKeyEx(root, _RUN_PATH, 0, access)
        vtype = winreg.REG_EXPAND_SZ if value_type == "REG_EXPAND_SZ" else winreg.REG_SZ
        winreg.SetValueEx(key, name, 0, vtype, data)
        ctx.log(f"Startup Manager: restored {hive}\\{_RUN_PATH}\\{name}")
        return True
    except Exception as exc:
        ctx.log(f"  ! could not restore {name}: {exc}")
        return False
    finally:
        if key is not None:
            try:
                winreg.CloseKey(key)
            except Exception:
                pass


def _reg_delete_view(hive, wow64, name, ctx):
    try:
        import winreg
    except Exception:
        return False
    root = winreg.HKEY_CURRENT_USER if hive == "HKCU" else winreg.HKEY_LOCAL_MACHINE
    access = winreg.KEY_WRITE
    try:
        access |= (winreg.KEY_WOW64_32KEY if wow64 else winreg.KEY_WOW64_64KEY)
    except AttributeError:
        pass
    key = None
    try:
        key = winreg.OpenKey(root, _RUN_PATH, 0, access)
        winreg.DeleteValue(key, name)
        ctx.log(f"Startup Manager: removed {hive}\\{_RUN_PATH}\\{name}")
        return True
    except FileNotFoundError:
        return True
    except Exception as exc:
        ctx.log(f"  ! could not remove {name}: {exc}")
        return False
    finally:
        if key is not None:
            try:
                winreg.CloseKey(key)
            except Exception:
                pass


def _toggle_shortcut(source, name, enabled, log):
    folders = _startup_folders()
    folder = folders.get(source)
    if not folder:
        return False, "That Startup folder could not be found."
    if enabled:
        try:
            from app.config_persist import (get_startup_disabled,
                                            remove_startup_disabled)
            rec = next((r for r in get_startup_disabled()
                       if r.get("source") == source and r.get("name") == name),
                      None)
        except Exception:
            rec = None
        if rec is None:
            return False, "Nothing saved to restore for this item."
        # SEC-002: the restore source must be RECONSTRUCTED from the trusted
        # holding directory, never read out of config. `rec["command"]` used
        # to go straight into os.replace(), so a same-user process could edit
        # one record in the user-writable config.json and have this ELEVATED
        # code move an arbitrary same-volume file into a Startup folder -
        # arbitrary file relocation, or persistence creation.
        #
        # So: the expected path is derived from _holding_dir() plus the
        # recorded base name, the record's own "command" must agree with it,
        # and the result must resolve to somewhere inside the holding dir.
        # `holding` is resolved FIRST: the legacy-record fallback below scans
        # it. (Deriving it afterwards made that scan raise NameError, which the
        # bare except swallowed into "not usable" — a silent regression that
        # broke every legacy record.)
        try:
            holding = os.path.abspath(_holding_dir())
        except Exception as exc:
            return False, ("Could not open the holding folder: %s" % exc)
        held_name = rec.get("held_name")
        if isinstance(held_name, str) and held_name.strip():
            held_name = os.path.basename(held_name)
        else:
            # Legacy record written before held_name existed. Derive it from
            # the TRUSTED holding dir instead of from config: the display
            # name has no extension (Spotify.lnk lists as "Spotify"), so match
            # on the splitext-less stem and accept only files that are really
            # sitting in the holding folder.
            held_name = None
            try:
                for fn in os.listdir(holding):
                    if os.path.splitext(fn)[0] == name:
                        held_name = fn
                        break
            except Exception:
                held_name = None
        if not held_name or held_name in (".", ".."):
            return False, "The saved record for this item is not usable."
        expected = os.path.abspath(os.path.join(holding, held_name))
        # The record's own command must match what we derived. A mismatch is
        # exactly the tampering signature, so refuse rather than "prefer" one.
        recorded = rec.get("command")
        if not isinstance(recorded, str) or not recorded.strip():
            return False, "The saved record for this item is not usable."
        try:
            if os.path.abspath(recorded) != expected:
                return False, ("The saved record for this item does not match "
                               "the held copy — refusing to restore it.")
        except Exception:
            return False, "The saved record for this item is not usable."

        held_path = expected
        try:
            from app.utils import _is_reparse_point
        except Exception:
            _is_reparse_point = None
        # containment, on the RESOLVED path, so .. and symlinks cannot escape
        try:
            real = os.path.realpath(held_path)
            if os.path.commonpath([real, os.path.realpath(holding)]) != \
                    os.path.realpath(holding):
                return False, ("The held copy is outside this app's own "
                               "holding folder — refusing to restore it.")
        except Exception:
            return False, "The held copy could not be verified."
        if not os.path.isfile(held_path):
            # Checked BEFORE the reparse test on purpose: _is_reparse_point
            # reports True for a path that does not exist, so testing it first
            # made a missing held copy report "is a shortcut or link" - a
            # false statement about the user's machine, which is the exact
            # dishonesty MED-001 is about.
            return False, "The held copy for this item is no longer there."
        if _is_reparse_point is not None:
            try:
                if _is_reparse_point(held_path):
                    return False, ("The held copy is a shortcut or link — "
                                   "refusing to restore it.")
            except Exception:
                pass
        target = os.path.join(folder, held_name)
        if os.path.exists(target):
            return False, ("Something with that name already exists in the "
                           "Startup folder — remove it first.")
        try:
            os.makedirs(folder, exist_ok=True)
            os.replace(held_path, target)
        except Exception as exc:
            if log:
                log(f"  ! could not restore {name}: {exc}")
            return False, "Could not move the shortcut back."
        try:
            remove_startup_disabled(source, name)
        except Exception:
            pass
        return True, f"{name} will run at sign-in again."
    else:
        # Find the live file again (name alone isn't a full path).
        live = None
        for n, path in _enum_startup_folder(folder):
            if n == name:
                live = path
                break
        if live is None:
            return False, "That shortcut is no longer there."
        try:
            holding = _holding_dir()
            dest = os.path.join(holding, os.path.basename(live))
            if os.path.exists(dest):
                # Extremely unlikely (would need the exact same
                # filename disabled twice without ever being restored) —
                # refuse rather than silently overwrite a held copy.
                return False, "A copy is already held for this item."
            os.replace(live, dest)
        except Exception as exc:
            if log:
                log(f"  ! could not disable {name}: {exc}")
            return False, "Could not move the shortcut."
        try:
            from app.config_persist import add_startup_disabled
            # SEC-002: record the HELD FILENAME alongside the path. The
            # display `name` has no extension (Spotify.lnk lists as
            # "Spotify"), so without this the restore side would have to
            # guess the extension - and guessing from config is precisely
            # what this fix removed.
            add_startup_disabled({"source": source, "name": name,
                                  "command": dest,
                                  "held_name": os.path.basename(dest),
                                  "value_type": ""})
        except Exception:
            try:
                os.replace(dest, live)  # put it back — see registry path above
            except Exception:
                pass
            return False, "Could not save how to undo this — left unchanged."
        return True, f"{name} will no longer run at sign-in."
