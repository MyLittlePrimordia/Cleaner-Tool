"""
Toast notifications for Windows 10/11.
Uses PowerShell with Windows.UI.Notifications for native toasts.

Identity: toasts used to borrow PowerShell's own AppUserModelID because an
UNREGISTERED id makes Windows silently drop the toast (audit A1-L5/P1-20).
The catch was the small "Microsoft.Windows.PowerShell" attribution line on
every toast. The fix is to REGISTER our own id: one HKCU registry key
(stdlib winreg, no admin, no shortcut, no install step) gives it a display
name + icon, so Windows shows "Cleaner Tool" with the app icon instead.
If the registry write ever fails we fall back to PowerShell's id, so a toast
still appears (just with the old attribution) rather than vanishing.
"""

import os
import subprocess
import sys
import time
import threading

_APP_NAME = "Cleaner Tool"
_AUMID = "CleanerTool.App"
_FALLBACK_AUMID = "Microsoft.Windows.PowerShell"

_aumid_lock = threading.Lock()
_aumid_cache = None


def _esc(s: str) -> str:
    """Escape for PowerShell single-quoted string. Also neutralizes
    control characters (not just \\n/\\r) that could otherwise break the
    one-liner or, in a future template change, become an injection
    vector — title/message can carry game names, drive labels, or other
    semi-external text."""
    if not isinstance(s, str):
        s = str(s)
    # Single-quote doubling is the only legal escape inside '...'
    s = s.replace("'", "''")
    # Strip all control chars (not just \n/\r) that could break the
    # one-liner if they ever reach it.
    s = "".join(ch if ord(ch) >= 32 else " " for ch in s)
    return s


def _stable_icon_path():
    """Persistent path for the toast icon, reused across launches.

    A onefile PyInstaller build unpacks to a fresh temp dir every run, so
    writing that path into HKCU (as IconUri) leaves a dangling reference
    the moment the process exits — Windows then falls back to a generic
    icon until the next successful launch. Instead, copy the bundled icon
    once into the app's own persistent config dir (the same
    %LOCALAPPDATA%\\CleanerTool used for config.json / events.log, with
    the same missing-LOCALAPPDATA / redirected-profile fallback already
    handled there) and reuse that copy.
    """
    try:
        import shutil
        from pathlib import Path
        from app.config_persist import CONFIG_DIR

        here = Path(__file__).resolve().parent
        src = here / "assets" / "icon.png"
        if not src.is_file():
            # Frozen fallback: PyInstaller layout can put bundled data
            # under either <_MEIPASS>/app/assets or <_MEIPASS>/assets
            # depending on build config — the rest of the codebase's
            # icon lookups (_set_window_icon and friends) check both, so
            # this does too.
            meipass = getattr(sys, "_MEIPASS", None)
            if meipass:
                for cand in (Path(meipass) / "app" / "assets" / "icon.png",
                            Path(meipass) / "assets" / "icon.png"):
                    if cand.is_file():
                        src = cand
                        break
            if not src.is_file():
                return None

        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        dest = CONFIG_DIR / "toast_icon.png"

        # Copy only when missing or size differs (cheap, avoids a write
        # on every single launch).
        if not dest.is_file() or dest.stat().st_size != src.stat().st_size:
            shutil.copy2(src, dest)
        return str(dest)
    except Exception:
        return None


def _ensure_aumid() -> str:
    """Register the app's toast identity once per run and return the id to
    use. The icon path is now a stable, reused copy under CONFIG_DIR
    rather than the onefile temp-extraction path, so it survives past the
    process that registered it (see _stable_icon_path)."""
    global _aumid_cache
    with _aumid_lock:
        if _aumid_cache is not None:
            return _aumid_cache
        aumid = _FALLBACK_AUMID
        try:
            import winreg
            path = "Software\\Classes\\AppUserModelId\\" + _AUMID
            with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, path, 0,
                                    winreg.KEY_SET_VALUE) as k:
                winreg.SetValueEx(k, "DisplayName", 0, winreg.REG_SZ, _APP_NAME)
                icon = _stable_icon_path()
                if icon:
                    winreg.SetValueEx(k, "IconUri", 0, winreg.REG_SZ, icon)
            aumid = _AUMID
        except Exception:
            aumid = _FALLBACK_AUMID
        _aumid_cache = aumid
        return aumid


def _build_script(title: str, message: str, long_duration: bool,
                  aumid: str, tag: str = "") -> str:
    hide_after = 10 if long_duration else 4
    title_esc = _esc(title)
    msg_esc = _esc(message)
    tag_line = ""
    if tag:
        # Same tag+group replaces the earlier toast in Action Center, so a
        # "started" toast doesn't linger next to its own result.
        tag_line = (f"$toast.Tag = '{_esc(tag)}'; "
                    f"$toast.Group = 'CleanerTool'; ")
    return (
        "[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] | Out-Null; "
        "$template = [Windows.UI.Notifications.ToastTemplateType]::ToastText02; "
        "$xml = [Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent($template); "
        "$text = $xml.GetElementsByTagName('text'); "
        f"$text[0].AppendChild($xml.CreateTextNode('{title_esc}')) | Out-Null; "
        f"$text[1].AppendChild($xml.CreateTextNode('{msg_esc}')) | Out-Null; "
        "$toast = [Windows.UI.Notifications.ToastNotification]::new($xml); "
        f"$toast.Duration = [Windows.UI.Notifications.ToastDuration]::{'Long' if long_duration else 'Short'}; "
        f"{tag_line}"
        f"$notifier = [Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier('{_esc(aumid)}'); "
        "$notifier.Show($toast); "
        # Auto-dismiss: some setups leave a toast on screen until the X is
        # clicked, so this same process waits out the toast's duration and
        # then hides it (also clears it from the notification list).
        f"Start-Sleep -Seconds {hide_after}; "
        "$notifier.Hide($toast)"
    )


# Bounds concurrent PowerShell hide-helper processes (TOAST-001): a burst
# of toasts can otherwise spawn one long-lived process each.
_toast_sem = threading.Semaphore(3)


def show_toast(title: str, message: str, duration: str = "short",
               tag: str = ""):
    """Show a Windows toast notification.

    Args:
        title: Toast title
        message: Toast body message
        duration: "short" (7s) or "long" (25s)
        tag: optional id; a later toast with the same tag replaces this one
             in Action Center (used for start -> result pairs)
    """
    if not sys.platform.startswith("win"):
        return False

    if duration not in ("short", "long"):
        duration = "short"

    ps_script = _build_script(title, message, duration == "long",
                              _ensure_aumid(), tag)

    def _run():
        acquired = False
        try:
            # Cap concurrent hide-helpers so a burst of toasts (e.g. a
            # misbehaving watcher, repeated low-space checks) can't spawn
            # unbounded long-lived PowerShell processes. A dropped toast
            # under heavy burst is an acceptable trade — there's no
            # delivery guarantee here to begin with.
            acquired = _toast_sem.acquire(blocking=False)
            if not acquired:
                return
            subprocess.Popen(
                ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
                 "-Command", ps_script],
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)
            )
            # The helper itself lives for hide_after seconds (see
            # _build_script); hold the slot a little past that so we
            # don't release while it's still technically starting up.
            time.sleep(12 if duration == "long" else 6)
        except Exception:
            pass
        finally:
            if acquired:
                try:
                    _toast_sem.release()
                except Exception:
                    pass

    try:
        # Fire-and-forget: the helper stays alive for the toast's duration
        # (to hide it), so never wait on it — callers must not block.
        threading.Thread(target=_run, daemon=True, name="ToastHelper").start()
        return True
    except Exception:
        return False


def notify_clean_complete(freed_bytes: int, task_count: int, failed: int = 0,
                          skipped: int = 0):
    """Show toast for a finished Clean run. The copy is truthful about
    failures (audit minor 3: this used to say 'Clean Complete' even when
    every task failed). `skipped` (nothing-to-do on this PC) is reported
    too, so an all-skipped run doesn't toast a bare "0 tasks finished".

    NOTE: returncode==0 from the PowerShell host only means the COM call
    was issued — notifications-disabled / Focus Assist / dropped delivery
    still return 0. Best-effort, no delivery guarantee.

    UX-001: copy now comes from the single format_run_summary() helper
    (app.utils) so this toast can never disagree with the Scorecard's
    own chip counts, and an all-skipped run ("0 tasks finished") reads
    honestly as "nothing needed cleaning" instead of implying work was
    done.
    """
    from app.utils import format_run_summary
    title, body = format_run_summary(task_count, failed, skipped, freed_bytes)
    show_toast(title, body, "short")


def notify_scheduled_run():
    """Show toast when scheduled auto-clean runs."""
    show_toast(
        "Auto Maintenance",
        "Scheduled maintenance started in background.",
        "short"
    )


def notify_low_space(drive_letter: str, free_gb: float):
    """Nudge toast when a drive crosses the red fill threshold (<=5%
    free). Fire-and-forget like the other notifiers: no delivery
    guarantee, and never raised. Duration long — a low-space warning
    deserves the full 25s on screen."""
    try:
        free_txt = f"{free_gb:.1f} GB"
    except Exception:
        free_txt = "very little space"
    show_toast(
        "Drive Nearly Full",
        f"{drive_letter}: only {free_txt} free. Open Storage Insight "
        "to see what's eating it and clean the safe junk.",
        "long",
    )