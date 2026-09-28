"""app/ui/tabs: the feature tab pages (H13).

A tab may use the widget set, the modal base, and the dialogs — it just may
not import another tab. Application is what wires them together.
"""
from app.ui.tabs.admingateframe import AdminGateFrame
from app.ui.tabs.installtab import InstallTab
from app.ui.tabs.tasktab import TaskTab
from app.ui.tabs.toolstab import ToolsTab

__all__ = ["AdminGateFrame", "InstallTab", "TaskTab", "ToolsTab"]
