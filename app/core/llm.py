"""The Anthropic call boundary.

Every LLM call site (header mapper, Tier-4 adjudicator, run summary) goes
through here for client construction, the messages.create call, text-block
joining, and tolerant JSON extraction — so cross-cutting changes (retry
policy, usage accounting, structured outputs) are one-file changes. Call
sites keep their own max_tokens, prompts, validation, and error policy.
"""
from __future__ import annotations

import re

from .. import config

JSON_OBJECT_RE = re.compile(r"\{.*\}", re.DOTALL)
_JSON_ARRAY_RE = re.compile(r"\[.*\]", re.DOTALL)
_FENCE_RE = re.compile(r"^```(?:json)?\s*(.*?)\s*```$", re.DOTALL)


def make_client():
    # anthropic is imported lazily on purpose: the SDK is heavy and only
    # needed once a key is configured.
    import anthropic
    return anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)


def complete(client, *, system: str, user: str, max_tokens: int) -> str:
    """One messages.create call → concatenated text blocks."""
    resp = client.messages.create(
        model=config.ANTHROPIC_MODEL,
        max_tokens=max_tokens,
        system=system,
        messages=[{"role": "user", "content": user}],
    )
    return "".join(b.text for b in resp.content if b.type == "text")


def strip_fence(text: str) -> str:
    text = (text or "").strip()
    m = _FENCE_RE.match(text)
    return m.group(1) if m else text


def json_candidates(text: str) -> list[str]:
    """Substrings of a model answer worth attempting json.loads on, in order:
    the fence-stripped text itself, the greedy {...} capture, the greedy
    [...] capture. Callers validate each until one parses."""
    text = strip_fence(text)
    out = [text]
    m = JSON_OBJECT_RE.search(text)
    if m:
        out.append(m.group(0))
    arr = _JSON_ARRAY_RE.search(text)
    if arr:
        out.append(arr.group(0))
    return out
