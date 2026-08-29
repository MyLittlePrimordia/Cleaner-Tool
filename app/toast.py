"""
Toast notifications for Windows 10/11.
Uses PowerShell with Windows.UI.Notifications for native toasts.
"""

import subprocess
import sys


def show_toast(title: str, message: str, duration: str = "short"):
    """Show a Windows toast notification.
    
    Args:
        title: Toast title
        message: Toast body message
        duration: "short" (7s) or "long" (25s)
    """
    if not sys.platform.startswith("win"):
        return False
    
    # Escape for PowerShell
    title_esc = title.replace("'", "''").replace('"', '""')
    msg_esc = message.replace("'", "''").replace('"', '""')
    
    ps_script = f'''
[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] | Out-Null
$template = [Windows.UI.Notifications.ToastTemplateType]::ToastText02
$xml = [Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent($template)
$text = $xml.GetElementsByTagName("text")
$text[0].AppendChild($xml.CreateTextNode("{title_esc}")) | Out-Null
$text[1].AppendChild($xml.CreateTextNode("{msg_esc}")) | Out-Null
$toast = [Windows.UI.Notifications.ToastNotification]::new($xml)
$toast.ExpirationTime = [DateTimeOffset]::Now.AddSeconds($({"7" if duration == "short" else "25"}))
$notifier = [Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier("CleanerTool")
$notifier.Show($toast)
'''
    try:
        subprocess.run(
            ["powershell", "-NoProfile", "-Command", ps_script],
            capture_output=True, timeout=10, creationflags=subprocess.CREATE_NO_WINDOW
        )
        return True
    except Exception:
        return False


def notify_clean_complete(freed_bytes: int, task_count: int):
    """Show toast for completed cleaning run."""
    from app.utils import format_bytes
    show_toast(
        "Cleaner Tool - Clean Complete",
        f"{task_count} tasks finished. Freed {format_bytes(freed_bytes)}.",
        "short"
    )


def notify_scheduled_run():
    """Show toast when scheduled auto-clean runs."""
    show_toast(
        "Cleaner Tool - Auto Maintenance",
        "Scheduled maintenance started in background.",
        "short"
    )


def notify_error(title: str, message: str):
    """Show error toast."""
    show_toast(f"Cleaner Tool - {title}", message, "long")