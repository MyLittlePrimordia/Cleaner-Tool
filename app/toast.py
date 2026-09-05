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


def notify_clean_complete(freed_bytes: int, task_count: int, failed: int = 0):
    """Show toast for a finished Clean run. The copy is truthful about
    failures (audit minor 3: this used to say 'Clean Complete' even when
    every task failed)."""
    from app.utils import format_bytes
    if not failed:
        show_toast(
            "Cleaner Tool - Clean Complete",
            f"{task_count} tasks finished. Freed {format_bytes(freed_bytes)}.",
            "short"
        )
    elif task_count:
        show_toast(
            "Cleaner Tool - Clean Finished With Errors",
            f"{task_count} task(s) succeeded, {failed} failed. Freed {format_bytes(freed_bytes)}.",
            "short"
        )
    else:
        show_toast(
            "Cleaner Tool - Clean Failed",
            f"All {failed} task(s) failed — see the run log for details.",
            "short"
        )


def notify_scheduled_run():
    """Show toast when scheduled auto-clean runs."""
    show_toast(
        "Cleaner Tool - Auto Maintenance",
        "Scheduled maintenance started in background.",
        "short"
    )