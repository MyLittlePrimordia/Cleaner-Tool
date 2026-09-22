"""Infrastructure helpers extracted for boundary defense.

fs_safe: fail-closed reparse detection and safe walks.
Re-exports from app.utils for backward compatibility during the
controlled hardening migration.
"""
from app.utils import _is_reparse_point  # fail-closed after SEC/BOUNDARY fix

__all__ = ["_is_reparse_point"]
