"""
Toast notifications for Windows 10/11.
Uses PowerShell with Windows.UI.Notifications for native toasts.

Audit fix (A1-L5/P1-20): CreateToastNotifier('CleanerTool') used an
UNREGISTERED AppUserModelID — Windows silently drops toasts from AUMIDs it
has no Start-menu/shortcut registration for, while the function still
returned True (fake success). PowerShell's own AUMID (registered by the
OS for powershell.exe) reliably displays, so the toast now uses it. The
app's identity still shows in the title text, which we control.
"""

import subprocess
import sys


def _esc(s: str) -> str:
    return s.replace("'", "''").replace("\n", " ").replace("\r", " ")


def show_toast(title: str, message: str, duration: str = "short"):
    """Show a Windows toast notification.

    Args:
        title: Toast title
        message: Toast body message
        duration: "short" (7s) or "long" (25s)
    """
    if not sys.platform.startswith("win"):
        return False

    if duration not in ("short", "long"):
        duration = "short"

    sec = 7 if duration == "short" else 25
    title_esc = _esc(title)
    msg_esc = _esc(message)

    ps_script = (
        f"[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] | Out-Null; "
        f"$template = [Windows.UI.Notifications.ToastTemplateType]::ToastText02; "
        f"$xml = [Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent($template); "
        f"$text = $xml.GetElementsByTagName('text'); "
        f"$text[0].AppendChild($xml.CreateTextNode('{title_esc}')) | Out-Null; "
        f"$text[1].AppendChild($xml.CreateTextNode('{msg_esc}')) | Out-Null; "
        f"$toast = [Windows.UI.Notifications.ToastNotification]::new($xml); "
        f"$toast.Duration = [Windows.UI.Notifications.ToastDuration]::{'Long' if sec >= 25 else 'Short'}; "
        f"$notifier = [Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier('Microsoft.Windows.PowerShell'); "
        f"$notifier.Show($toast)"
    )
    try:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", ps_script],
            capture_output=True, timeout=30, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)
        )
        return result.returncode == 0
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
        "Cleaner Tool - Auto Maintenance",
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
        "Cleaner Tool - Drive Nearly Full",
        f"{drive_letter}: only {free_txt} free. Open Storage Insight "
        "to see what's eating it and clean the safe junk.",
        "long",
    )