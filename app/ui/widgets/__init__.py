"""The hand-drawn widget set. One widget per module, no dialog knowledge.

Every one of these is a tk.Canvas/tk.Frame subclass that paints itself --
there is no ttk usage and no third-party widget library, by design (the app
is stdlib-only). Each module is extracted verbatim from app/gui.py and
depends only on app.ui.theme.
"""
from app.ui.widgets.button import AnimatedButton
from app.ui.widgets.empty_state import EmptyState
from app.ui.widgets.entry import RoundedEntry
from app.ui.widgets.feature_card import feature_card_body
from app.ui.widgets.finding_row import FindingRow
from app.ui.widgets.gauge import GaugeDial
from app.ui.widgets.metric_strip import MetricStrip
from app.ui.widgets.modal_footer import ModalFooter
from app.ui.widgets.page import (
    CARD_MIN_W, PageActionBar, PageLead, flow_columns, grid_cards
)
from app.ui.widgets.permission_notice import PermissionNotice
from app.ui.widgets.progress import AnimatedProgressBar, ProgressRow
from app.ui.widgets.rounded_card import RoundedCard
from app.ui.widgets.scope_strip import ScopeStrip
from app.ui.widgets.scroll import ScrollableRoundedPanel
from app.ui.widgets.search_field import SearchField
from app.ui.widgets.segmented import SegmentedTabs
from app.ui.widgets.split_shell import SplitShell
from app.ui.widgets.status_line import StatusLine
from app.ui.widgets.toggle import ToggleSwitch
from app.ui.widgets.tool_card import CARD_HEIGHT as TOOL_CARD_HEIGHT
from app.ui.widgets.tool_card import ToolCard
from app.ui.widgets.tool_row import ToolRow
from app.ui.widgets.tooltip import Tooltip

__all__ = [
    "AnimatedButton",
    "AnimatedProgressBar",
    "CARD_MIN_W",
    "EmptyState",
    "feature_card_body",
    "FindingRow",
    "flow_columns",
    "GaugeDial",
    "grid_cards",
    "MetricStrip",
    "ModalFooter",
    "PageActionBar",
    "PageLead",
    "PermissionNotice",
    "ProgressRow",
    "RoundedCard",
    "RoundedEntry",
    "ScopeStrip",
    "ScrollableRoundedPanel",
    "SearchField",
    "SegmentedTabs",
    "SplitShell",
    "StatusLine",
    "ToggleSwitch",
    "ToolCard",
    "ToolRow",
    "Tooltip",
    "TOOL_CARD_HEIGHT",
]