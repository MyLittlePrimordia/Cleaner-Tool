"""app/ui/dialogs: the feature dialogs, one per module (H12).

Every dialog inherits ThemedModal and may use the widget set, but NOT another
dialog — with two deliberate exceptions that predate this split and are
called out in the modules themselves:

  * PilotDialog opens _CatalogPickerDialog
  * InstallTab opens InstallProfilesDialog

The Quick Tools table lives in app/tools_shortcuts.py rather than on ToolsTab,
because QuickToolsDialog needs it and a dialog must not import a tab.

Adding a new dialog means adding a module here and re-exporting it from
app/gui.py, which stays the compatibility facade for the smoke suite.
"""
from app.ui.dialogs.app_update_dialog import AppUpdateDialog
from app.ui.dialogs.catalog_picker_dialog import _CatalogPickerDialog
from app.ui.dialogs.dns_tester_dialog import DnsTesterDialog
from app.ui.dialogs.drive_toolkit_dialog import DriveToolkitDialog
from app.ui.dialogs.duplicates_dialog import DuplicatesDialog
from app.ui.dialogs.file_lock_dialog import FileLockDialog
from app.ui.dialogs.game_server_ping_dialog import GameServerPingDialog
from app.ui.dialogs.gamepad_dialog import GamepadDialog
from app.ui.dialogs.hardware_monitor_dialog import HardwareMonitorDialog
from app.ui.dialogs.health_report_dialog import HealthReportDialog
from app.ui.dialogs.install_profiles_dialog import InstallProfilesDialog
from app.ui.dialogs.keyboard_tester_dialog import KeyboardTesterDialog
from app.ui.dialogs.mic_check_dialog import MicCheckDialog
from app.ui.dialogs.monitor_test_dialog import MonitorTestDialog
from app.ui.dialogs.mouse_tester_dialog import MouseTesterDialog
from app.ui.dialogs.network_connections_dialog import NetworkConnectionsDialog
from app.ui.dialogs.pilot_dialog import PilotDialog
from app.ui.dialogs.process_manager_dialog import ProcessManagerDialog
from app.ui.dialogs.quick_tools_dialog import QuickToolsDialog
from app.ui.dialogs.restore_point_dialog import RestorePointDialog
from app.ui.dialogs.run_history_dialog import RunHistoryDialog
from app.ui.dialogs.safety_center_dialog import SafetyCenterDialog
from app.ui.dialogs.update_picker_dialog import UpdatePickerDialog
from app.ui.dialogs.scorecard_dialog import ScorecardDialog
from app.ui.dialogs.services_dialog import ServicesDialog
from app.ui.dialogs.speaker_test_dialog import SpeakerTestDialog
from app.ui.dialogs.specs_dialog import SpecsDialog
from app.ui.dialogs.speed_test_dialog import SpeedTestDialog
from app.ui.dialogs.startup_manager_dialog import StartupManagerDialog
from app.ui.dialogs.storage_insight_dialog import StorageInsightDialog
from app.ui.dialogs.uninstall_programs_dialog import UninstallProgramsDialog
from app.ui.dialogs.webcam_dialog import WebcamDialog

__all__ = [
    "DnsTesterDialog", "DuplicatesDialog", "FileLockDialog", "DriveToolkitDialog", "GameServerPingDialog",
    "AppUpdateDialog",
    "GamepadDialog", "HardwareMonitorDialog", "HealthReportDialog",
    "InstallProfilesDialog", "KeyboardTesterDialog", "MicCheckDialog",
    "MonitorTestDialog", "MouseTesterDialog", "NetworkConnectionsDialog",
    "PilotDialog",
    "ProcessManagerDialog", "QuickToolsDialog", "RestorePointDialog", "RunHistoryDialog", "SafetyCenterDialog", "UpdatePickerDialog", "ScorecardDialog",
    "ServicesDialog", "SpeakerTestDialog", "SpecsDialog", "SpeedTestDialog",
    "StartupManagerDialog", "StorageInsightDialog",
    "UninstallProgramsDialog", "WebcamDialog", "_CatalogPickerDialog",
]
