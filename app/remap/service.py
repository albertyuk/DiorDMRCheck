"""Header-remap orchestration shared by both products.

When a workbook's headers don't match the deterministic fingerprint, Claude
proposes a header→canonical mapping from a small structural sample. NOTHING
is applied until a human approves it on the audit screen; approved mappings
are cached by header signature and auto-applied (visibly) afterwards.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from .. import config
from ..core.token_store import TokenStore
from . import mapper
from .registry import FIELDS, KIND_LABELS

# Pending audits awaiting human approval (token handed to the browser).
PENDING_MAPS = TokenStore(ttl_seconds=30 * 60, max_entries=10)


@dataclass
class RemapOutcome:
    """Typed result of attempt_remap — replaces the old variant tuples."""
    status: str                                   # cached | audit | fail
    mapping: Optional[dict] = None                # cached: approved mapping
    proposal: Optional[mapper.Proposal] = None    # audit: LLM proposal
    choices: list = field(default_factory=list)   # audit: column choices
    sig: str = ""                                 # audit: header signature
    error: Optional[str] = None                   # fail: mapper error text


def attempt_remap(kind: str, data: bytes) -> RemapOutcome:
    """After a fingerprint failure: an already-approved mapping that fits
    this format, an LLM proposal awaiting human approval, or failure."""
    try:
        # Approved-mapping cache first, keyed by header-row LAYOUT: probe
        # every candidate header row so a re-upload of a known format skips
        # both the LLM and the human, regardless of its data content.
        candidates = mapper.candidate_signatures(data)
        hits = mapper.cache_get_many(kind, [sig for _, _, sig in candidates])
        for sheet, row, sig in candidates:
            cached = hits.get(sig)
            if (cached and cached["sheet"] == sheet
                    and int(cached["header_row"]) == row):
                return RemapOutcome("cached", mapping=cached)
        sample = mapper.build_sample(data)
    except Exception:
        return RemapOutcome("fail")
    if not config.ANTHROPIC_API_KEY:
        return RemapOutcome("fail")
    try:
        prop = mapper.propose(sample, kind)
        choices = mapper.column_choices(data, prop.sheet, prop.header_row)
        sig = mapper.header_signature(data, prop.sheet, prop.header_row)
        return RemapOutcome("audit", proposal=prop, choices=choices, sig=sig)
    except mapper.SchemaMapError as e:
        return RemapOutcome("fail", error=str(e))


def remap_note(mapping: dict, kind: str, auto: bool) -> dict:
    """Human-readable provenance of an applied mapping (preview/report)."""
    canonical = {key: text for text, key, _, _ in FIELDS[kind]}
    return {
        "sheet": mapping["sheet"], "header_row": mapping["header_row"],
        "columns": {canonical.get(k, k): v
                    for k, v in mapping["columns"].items()},
        "approved_by": mapping.get("approved_by", ""),
        "approved_at": mapping.get("approved_at", ""),
        "auto": auto,
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
