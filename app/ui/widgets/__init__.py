"""The hand-drawn widget set. One widget per module, no dialog knowledge.

Every one of these is a tk.Canvas/tk.Frame subclass that paints itself --
there is no ttk usage and no third-party widget library, by design (the app
is stdlib-only). Each module is extracted verbatim from app/gui.py and
depends only on app.ui.theme.
"""
from app.ui.widgets.button import AnimatedButton
from app.ui.widgets.entry import RoundedEntry
from app.ui.widgets.gauge import GaugeDial
from app.ui.widgets.progress import AnimatedProgressBar
from app.ui.widgets.scroll import ScrollableRoundedPanel
from app.ui.widgets.segmented import SegmentedTabs
from app.ui.widgets.toggle import ToggleSwitch
from app.ui.widgets.tooltip import Tooltip

__all__ = [
    "AnimatedButton",
    "AnimatedProgressBar",
    "GaugeDial",
    "RoundedEntry",
    "ScrollableRoundedPanel",
    "SegmentedTabs",
    "ToggleSwitch",
    "Tooltip",
]
