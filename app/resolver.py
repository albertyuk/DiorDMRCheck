"""Transitional re-export shim — this module moved to app.reconciler.links.
Deleted at the end of the migration; import from the new location.
"""
from .reconciler.links import *  # noqa: F401,F403
from .reconciler.links import _extract_note_fields, _normalize_url  # noqa: F401
