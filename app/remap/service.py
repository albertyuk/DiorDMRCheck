"""Header-remap orchestration shared by both products.

When a workbook's headers don't match the deterministic fingerprint, Claude
proposes a header→canonical mapping from a small structural sample. NOTHING
is applied until a human approves it on the audit screen; approved mappings
are cached by header signature and auto-applied (visibly) afterwards.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import logging
from pathlib import Path
from typing import Optional
from zipfile import BadZipFile

from defusedxml.ElementTree import ParseError
from defusedxml.common import DefusedXmlException
from lxml.etree import XMLSyntaxError
from openpyxl.utils.exceptions import InvalidFileException

from .. import config
from ..core.token_store import TokenStore
from ..core.uploads import remove_tree
from . import mapper
from .registry import FIELDS, KIND_LABELS

logger = logging.getLogger(__name__)

_WORKBOOK_INSPECTION_ERRORS = (
    BadZipFile,
    InvalidFileException,
    ParseError,
    DefusedXmlException,
    XMLSyntaxError,
    EOFError,
    IndexError,
    KeyError,
    ValueError,
)

def cleanup_pending_entry(entry: dict) -> None:
    """Remove on-disk staging owned by an abandoned reconciliation audit."""
    run_dir = entry.get("run_dir")
    if entry.get("flow") == "run" and run_dir:
        remove_tree(Path(run_dir))


# Pending audits awaiting human approval (token handed to the browser).
PENDING_MAPS = TokenStore(
    ttl_seconds=30 * 60,
    max_entries=5,
    on_discard=cleanup_pending_entry,
)


@dataclass
class RemapOutcome:
    """Typed result of attempt_remap — replaces the old variant tuples."""
    status: str                                   # cached | audit | fail
    mapping: Optional[dict] = None                # cached: approved mapping
    proposal: Optional[mapper.Proposal] = None    # audit: LLM proposal
    choices: list = field(default_factory=list)   # audit: column choices
    sig: str = ""                                 # audit: header signature
    error: Optional[str] = None                   # fail: mapper error text
    sample: Optional[dict] = None                 # ready: consented LLM payload


def inspect_remap(kind: str, data: bytes) -> RemapOutcome:
    """Perform cache lookup and build a bounded sample without network I/O."""
    try:
        # Approved-mapping cache first, keyed by header-row LAYOUT: probe
        # every candidate header row so a re-upload of a known format skips
        # both the LLM and the human, regardless of its data content.
        candidates = mapper.candidate_signatures(data)
    except _WORKBOOK_INSPECTION_ERRORS:
        logger.info("header-map workbook inspection failed", exc_info=True)
        return RemapOutcome("fail", error="workbook mapping inspection failed")

    # Database/cache corruption is an internal outage, not a user workbook
    # problem. Deliberately keep this outside the format-error catches so the
    # calling route logs it and returns its stable generic 500 response.
    hits = mapper.cache_get_many(kind, [sig for _, _, sig in candidates])
    for sheet, row, sig in candidates:
        cached = hits.get(sig)
        if (cached and cached["sheet"] == sheet
                and int(cached["header_row"]) == row):
            return RemapOutcome("cached", mapping=cached)

    try:
        sample = mapper.build_sample(data)
    except _WORKBOOK_INSPECTION_ERRORS:
        logger.info("header-map workbook sampling failed", exc_info=True)
        return RemapOutcome("fail", error="workbook mapping inspection failed")
    if not config.ANTHROPIC_API_KEY:
        return RemapOutcome("fail")
    return RemapOutcome("ready", sample=sample)


def finish_remap(kind: str, data: bytes,
                 prop: mapper.Proposal) -> RemapOutcome:
    """Build local audit metadata after the external proposal returns."""
    try:
        choices = mapper.column_choices(data, prop.sheet, prop.header_row)
        sig = mapper.header_signature(data, prop.sheet, prop.header_row)
        return RemapOutcome("audit", proposal=prop, choices=choices, sig=sig)
    except mapper.SchemaMapError as e:
        return RemapOutcome("fail", error=str(e))


def attempt_remap(kind: str, data: bytes) -> RemapOutcome:
    """Synchronous compatibility wrapper.

    Web routes should call :func:`inspect_remap` under the workbook gate,
    invoke ``mapper.propose`` in a plain thread-pool task, then call
    :func:`finish_remap` under the gate.  Keeping the wrapper makes CLI/tests
    straightforward without reintroducing gate coupling.
    """
    outcome = inspect_remap(kind, data)
    if outcome.status != "ready":
        return outcome
    try:
        proposal = mapper.propose(outcome.sample or {}, kind)
    except mapper.SchemaMapError as exc:
        return RemapOutcome("fail", error=str(exc))
    return finish_remap(kind, data, proposal)


def remap_note(mapping: dict, kind: str, auto: bool,
               shared: Optional[bool] = None) -> dict:
    """Human-readable provenance of an applied mapping (preview/report)."""
    canonical = {key: text for text, key, _, _ in FIELDS[kind]}
    return {
        "kind": kind, "sig": mapping.get("sig", ""),
        "sheet": mapping["sheet"], "header_row": mapping["header_row"],
        "columns": {canonical.get(k, k): v
                    for k, v in mapping["columns"].items()},
        "approved_by": mapping.get("approved_by", ""),
        "approved_at": mapping.get("approved_at", ""),
        "auto": auto, "shared": auto if shared is None else shared,
    }


def audit_context(token: str, entry: dict, error: str = "") -> dict:
    """View model for the audit screen."""
    files = []
    for kind, audit in entry["audits"].items():
        prop = audit["proposal"]
        fields = [{
            "key": key, "canonical": text, "required": req, "desc": desc,
            "proposed": prop["columns"].get(key),
            "confidence": prop.get("confidence", {}).get(key),
        } for text, key, req, desc in FIELDS[kind]]
        files.append({
            "kind": kind, "label": KIND_LABELS[kind],
            "filename": (entry.get("names", {}).get(kind)
                         or entry.get("filename", "")),
            "sheet": prop["sheet"], "header_row": prop["header_row"],
            "warnings": prop.get("warnings", []),
            "fields": fields, "choices": audit["choices"],
        })
    return {"token": token, "files": files, "flow": entry["flow"],
            "error": error}
