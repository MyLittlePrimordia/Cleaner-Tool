"""What a tool reads, what it changes, and whether that can be undone.

AUDIT TV-003 (2026-10). Every Tools popup already carried a good sentence
about its scope — "Read-only: nothing on your PC is changed." — but it lived
behind the ⓘ in the footer, so a cautious user had to find a tooltip before
they could tell a tester from a deleter. The sentence is still there (it is
the long explanation); this module is the VISIBLE half: three short slots the
shared ToolPage banner renders as chips under the headline.

Why a table and not more arguments at each call site
----------------------------------------------------
The declarations are small and comparable. Kept together, a reviewer can see
at a glance that every read-only tool says "Nothing" in the same place, and
that every destructive one names its undo (or says there is none). The prose
stays at the call site because it is a paragraph about that tool; the chips
are facts about the family, and the family is what this table holds.

The guard in dev/tests/test_phase1_tool_shell.py enumerates every ToolPage
subclass and fails if one is missing an entry or if a value grows past the
chip's comfortable width. A new dialog therefore cannot ship with an
undisclosed scope, and a scope cannot quietly become a paragraph.

Tone is derived, never typed per chip
-------------------------------------
A chip's colour is a function of the fact:
    Changes   green when the answer is "Nothing", amber otherwise;
    Reversible green when there is a real undo, red only when the action is
              destructive and nothing puts it back.
So a read-only tool cannot accidentally be painted as a risk, and no call
site can choose a reassuring colour for a permanent delete.
"""
from __future__ import annotations

from dataclasses import dataclass

@dataclass(frozen=True)
class ToolScope:
    """The three facts the scope strip shows, plus the long explanation.

    `reads` and `changes` are required in spirit: use "Nothing" for a tool
    that only looks. `reversible` is empty when the question does not apply
    (a read-only tool has nothing to undo), which removes the third chip
    rather than printing a meaningless "n/a".

    `destructive` and `undoable` are what the tones are derived from. They are
    explicit because the wording alone must never have to be parsed to decide
    whether a red chip is warranted.
    """

    reads: str
    changes: str = "Nothing"
    reversible: str = ""
    destructive: bool = False
    undoable: bool = False
    detail: str = ""

    def chips(self):
        """[(verb, value, tone)] — one tuple per visible slot."""
        out = [("Reads", self.reads, "neutral")]
        out.append(("Changes", self.changes, "good" if self._is_noop()
                    else "warn"))
        if self.reversible:
            if self.undoable:
                tone = "good"
            elif self.destructive:
                tone = "bad"
            else:
                tone = "warn"
            out.append(("Reversible", self.reversible, tone))
        return out

    def _is_noop(self):
        return str(self.changes).strip().lower() in ("", "nothing", "none")

    def summary(self):
        """One-line fallback for a tooltip when no prose was supplied."""
        return " · ".join("%s: %s" % (verb, value)
                          for verb, value, _tone in self.chips())


# --------------------------------------------------------------------------- #
# The declarations. One per ToolPage subclass; keyed by class name so a
# dialog does not need to import this module at all.
# --------------------------------------------------------------------------- #
TOOL_SCOPES = {
    # --- storage ------------------------------------------------------- #
    "StorageInsightDialog": ToolScope(
        reads="Disk usage + caches",
        changes="Deletes the files you pick",
        reversible="Only with Quarantine",
        destructive=True),
    "StorageCategoryDialog": ToolScope(
        reads="Files in one category"),
    # N-002: read-only until a step is picked; a Clean step then uses the
    # normal Clean-tab engine, so its own confirmation and scorecard apply.
    "ReclaimPlanDialog": ToolScope(
        reads="Measured reclaim plan",
        changes="Runs the step you pick",
        reversible="Only with Quarantine",
        destructive=True),
    "DriveToolkitDialog": ToolScope(
        reads="Drive health + speed",
        changes="Writes temp test files",
        reversible="Yes — removed after",
        undoable=True),
    "UninstallProgramsDialog": ToolScope(
        reads="Installed programs",
        changes="Uninstalls your picks",
        reversible="No — reinstall to undo",
        destructive=True),
    "DuplicatesDialog": ToolScope(
        reads="Copies + largest files",
        changes="Moves picks to Recycle Bin",
        reversible="Yes — Recycle Bin",
        undoable=True),

    # --- system -------------------------------------------------------- #
    "ProcessManagerDialog": ToolScope(
        reads="Processes + usage",
        changes="Ends the apps you pick",
        reversible="No — unsaved work is lost",
        destructive=True),
    "HardwareMonitorDialog": ToolScope(
        reads="Live sensor readings"),
    "StartupManagerDialog": ToolScope(
        reads="Startup items",
        changes="Disables picked items",
        reversible="Yes — re-enable any time",
        undoable=True),
    "ServicesDialog": ToolScope(
        reads="Services + startup type",
        changes="Start / stop, startup type",
        reversible="Yes — Undo last change",
        undoable=True),
    "FileLockDialog": ToolScope(
        reads="Who holds a file",
        changes="Closes the app you pick",
        reversible="No — unsaved work is lost",
        destructive=True),
    "SpecsDialog": ToolScope(
        reads="Hardware facts"),
    # U-005: the removed packages are exported first, inside the same elevated
    # script, so a driver store clean is genuinely undoable by putting the
    # exported package back (pnputil /add-driver).
    "DriverStoreDialog": ToolScope(
        reads="Driver store packages",
        changes="Removes old versions",
        reversible="Yes \u2014 exported first",
        undoable=True),

    # --- safety -------------------------------------------------------- #
    "RestorePointDialog": ToolScope(
        reads="Restore points",
        changes="Adds a restore point",
        reversible="Yes — delete it again",
        undoable=True),
    "SafetyCenterDialog": ToolScope(
        reads="Every safety net",
        changes="Runs the undo you pick",
        reversible="Yes — it is the undo",
        undoable=True),

    # --- network ------------------------------------------------------- #
    "DnsTesterDialog": ToolScope(
        reads="Resolver speed",
        changes="Sets the DNS you apply",
        reversible="Yes — Restore resets",
        undoable=True),
    "SpeedTestDialog": ToolScope(
        reads="Throughput + latency"),
    "GameServerPingDialog": ToolScope(
        reads="Server latency"),
    "NetworkConnectionsDialog": ToolScope(
        reads="Open connections",
        changes="Ends the app you pick",
        reversible="No — unsaved work is lost",
        destructive=True),

    # --- gaming -------------------------------------------------------- #
    "PilotDialog": ToolScope(
        reads="Games + tweaks",
        changes="Applies your tweaks",
        reversible="Yes — Undo in Auto-Pilot",
        undoable=True),
    "GameSavesDialog": ToolScope(
        reads="Save backups",
        changes="Restores save files",
        reversible="Yes — pre-restore snapshot",
        undoable=True),
    "GameReadinessDialog": ToolScope(
        reads="Seven readiness checks",
        changes="Runs the fixes you pick",
        reversible="Yes — Safety & Undo",
        undoable=True),
    "HealthHistoryDialog": ToolScope(
        reads="Past checkup scores"),

    # 2026-10: the peripheral testers and the Advisor/What Changed pair are
    # gone. Their scope rows went with them -- an unreachable dialog has no
    # scope to declare.

    # --- update / install side dialogs ---------------------------------- #
    "AppUpdateDialog": ToolScope(
        reads="Release + checksum",
        changes="Replaces the app's files",
        reversible="No — reinstall to go back",
        destructive=True),
    "UpdatePickerDialog": ToolScope(
        reads="Pending Windows updates",
        changes="Installs picked updates",
        reversible="No — updates stay"),
    "InstallFailedDialog": ToolScope(
        reads="Failed installs"),
}


def scope_for(dialog_or_name):
    """The declaration for a dialog instance or class name, or None."""
    name = dialog_or_name
    if not isinstance(name, str):
        if isinstance(dialog_or_name, type):
            name = dialog_or_name.__name__
        else:
            name = type(dialog_or_name).__name__
    return TOOL_SCOPES.get(name)
