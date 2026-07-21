"""Tier 4 — Sonnet adjudication of the residue, plus the run summary.

The LLM never decides anything an exact ID join can decide. It sees only the
rows the deterministic tiers could not settle (REVIEW rows, and LINK_ERROR
rows that have Tier-3 candidates), batched into as few calls as practical,
and answers two question types (same-person? / same-post?) with strict JSON.
Any UNSURE or malformed answer stays REVIEW for the human — never a silent
guess. Engagement numbers are provided as context with an explicit caveat
and the model is instructed not to use them as a decision signal.
"""
from __future__ import annotations

import json
from typing import Callable, Optional

from pydantic import BaseModel, Field, ValidationError

from .. import config
from ..core import llm
from .domain import LINK_ERROR, MATCH, REVIEW, Verdict


class Adjudication(BaseModel):
    row: str
    verdict: str = Field(pattern="^(SAME_PERSON|SAME_POST|DIFFERENT|UNSURE)$")
    confidence: float = Field(ge=0, le=1)
    rationale_en: str
    rationale_zh: str


class AdjudicationBatch(BaseModel):
    items: list[Adjudication]


SYSTEM_PROMPT = (
    "You adjudicate ambiguous rows from a KOL-campaign reconciliation between an "
    "internal tracker (PLOG) and a DMR social-listening export for Xiaohongshu.\n"
    "Rules:\n"
    "- Answer ONLY with JSON matching the given schema. No prose outside JSON.\n"
    "- verdict is SAME_PERSON, SAME_POST, DIFFERENT, or UNSURE.\n"
    "- Engagement counts are NOT comparable across the two files (DMR is an "
    "early crawl snapshot; ~30% median drift, p90≈96% on verified same-post "
    "pairs). NEVER use engagement similarity or difference as evidence.\n"
    "- Names may be romanized, truncated, or decorated differently between "
    "files; dates can drift several days on true matches.\n"
    "- When evidence is genuinely insufficient, say UNSURE — a human reviews "
    "those. Never guess.\n"
    "- rationale_en and rationale_zh are one line each."
)

def _row_key(v: Verdict) -> str:
    # excel_row makes the key unique even when (CAMPAIGN, NO) identities
    # collide in degenerate inputs.
    return f"{v.campaign}|{v.no}|r{v.excel_row}"


def _question_for(v: Verdict) -> Optional[dict]:
    """Build the per-row question payload, or None if there is nothing useful
    for the model to look at."""
    if not v.candidates:
        return None
    kind = "same_person" if v.tier.endswith("name-conflict") else "same_post"
    return {
        "row": _row_key(v),
        "question": kind,
        "plog": {
            "name": v.name,
            "post_date": v.post_date,
            "status_so_far": v.status,
            "reason": v.review_reason or "link unresolvable",
        },
        "resolved": {
            "note_id": v.resolved_note_id,
            "author_id": v.resolved_author_id,
            "author_nickname": v.resolved_author_name,
        },
        "dmr_candidates": [
            {
                "post_id": c.post_id,
                "blogger": c.blogger,
                "username": c.username,
                "post_date": c.post_date,
                "date_delta_days": c.date_delta_days,
                "name_method": c.name_method,
            }
            for c in v.candidates[:5]
        ],
    }


def _parse_batch(text: str) -> Optional[AdjudicationBatch]:
    for c in llm.json_candidates(text):
        try:
            data = json.loads(c)
        except ValueError:
            continue
        if isinstance(data, list):
            data = {"items": data}
        try:
            return AdjudicationBatch.model_validate(data)
        except (ValidationError, ValueError):
            continue
    return None


# Questions per API call. Keeps max_tokens comfortably under the anthropic
# SDK's non-streaming ceiling (~21k tokens ≈ 10-minute guard) while leaving
# headroom for adaptive thinking, which counts against max_tokens on Sonnet 5.
BATCH_SIZE = 15
BATCH_MAX_TOKENS = 8000


def adjudicate(verdicts: list[Verdict],
               llm_counter: Optional[Callable[[], None]] = None) -> None:
    """Annotate residue verdicts in place. No-op without an API key."""
    if not config.ANTHROPIC_API_KEY:
        return
    residue = [v for v in verdicts if v.status == REVIEW or
               (v.status == LINK_ERROR and v.candidates)]
    asked = [(v, q) for v, q in ((v, _question_for(v)) for v in residue) if q]
    if not asked:
        return
    client = llm.make_client()
    for start in range(0, len(asked), BATCH_SIZE):
        chunk = asked[start:start + BATCH_SIZE]
        _adjudicate_chunk(client, chunk, llm_counter)


def _adjudicate_chunk(client, chunk: list[tuple[Verdict, dict]],
                      llm_counter: Optional[Callable[[], None]]) -> None:
    questions = [q for _, q in chunk]
    by_key = {_row_key(v): v for v, _ in chunk}
    schema_hint = (
        '{"items": [{"row": "<echo the given row id>", "verdict": "SAME_PERSON|'
        'SAME_POST|DIFFERENT|UNSURE", "confidence": 0.0, "rationale_en": "...", '
        '"rationale_zh": "..."}]}'
    )
    user_msg = (
        "Adjudicate each row below. Return JSON only, exactly this shape:\n"
        f"{schema_hint}\n\nRows:\n"
        + json.dumps(questions, ensure_ascii=False, indent=1)
    )

    batch: Optional[AdjudicationBatch] = None
    for attempt in range(2):  # validate and retry once on parse failure
        try:
            text = llm.complete(
                client, system=SYSTEM_PROMPT, user=user_msg,
                max_tokens=max(config.ANTHROPIC_MAX_TOKENS, BATCH_MAX_TOKENS))
            if llm_counter:
                llm_counter()
            batch = _parse_batch(text)
            if batch:
                break
            user_msg = (
                "Your previous answer was not valid JSON for the schema "
                f"{schema_hint}. Re-answer with JSON only.\n\nRows:\n"
                + json.dumps(questions, ensure_ascii=False, indent=1)
            )
        except Exception as e:  # API/network failure must not kill the run
            for v, _ in chunk:
                v.notes.append(f"LLM adjudication unavailable: {e}")
            return

    if not batch:
        for v, _ in chunk:
            v.notes.append("LLM adjudication returned malformed JSON twice — kept for human review.")
        return

    for item in batch.items:
        v = by_key.get(item.row)
        if not v:
            continue
        v.llm_verdict = item.verdict
        v.llm_confidence = item.confidence
        v.llm_rationale_en = item.rationale_en
        v.llm_rationale_zh = item.rationale_zh
        # The LLM refines the human-facing note; it never flips a row to a
        # deterministic status (MATCH/NO_POST/NO_BLOGGER stay tier-1/2 only).
        if item.verdict == "UNSURE" and v.status != REVIEW:
            v.notes.append("Adjudicator unsure — treat the candidate list with care.")


def summarize_run(verdicts: list[Verdict], counts: dict, warnings: list[str],
                  llm_counter: Optional[Callable[[], None]] = None) -> dict:
    """One final Sonnet call drafting a bilingual run summary. Falls back to a
    deterministic summary when no key is configured or the call fails."""
    fallback = {
        "zh": "、".join(f"{k}: {v}" for k, v in counts.items()) or "无数据",
        "en": ", ".join(f"{k}: {v}" for k, v in counts.items()) or "no data",
    }
    if not config.ANTHROPIC_API_KEY:
        return fallback
    anomalies = [
        {"row": f"{v.campaign}|{v.no}", "name": v.name, "status": v.status,
         "column_s": v.column_s(), "notes": v.notes[:2]}
        for v in verdicts if v.status != MATCH
    ][:40]
    try:
        text = llm.complete(
            llm.make_client(),
            system=(
                "Write a short reconciliation-run summary for a KOL campaign "
                "team, in Chinese then English. 3-5 sentences each. Mention "
                "counts per status and notable anomalies. Return JSON only: "
                '{"zh": "...", "en": "..."}'
            ),
            user=json.dumps(
                {"counts": counts, "anomalies": anomalies,
                 "parser_warnings": warnings[:20]},
                ensure_ascii=False),
            max_tokens=2000,  # headroom for adaptive thinking + both languages
        )
        if llm_counter:
            llm_counter()
        m = llm.JSON_OBJECT_RE.search(text)
        if m:
            data = json.loads(m.group(0))
            if isinstance(data, dict) and data.get("zh") and data.get("en"):
                return {"zh": str(data["zh"]), "en": str(data["en"])}
    except Exception:
        pass
    return fallback
