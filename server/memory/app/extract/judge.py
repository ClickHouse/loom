"""The write-time judge: a second call grades each fact CORRECT, UNCERTAIN
or WRONG against the source messages. WRONG is dropped before the write;
UNCERTAIN survives, flagged for review.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from server.llm import customer
from server.llm.client import LLMResult, get_llm_client
from server.llm.json_text import strip_code_fence
from server.memory.app.extract.models import ExtractedFact
from server.memory.app.extract.turns import _format_messages
from server.memory.domain.extraction_prompts import (
    _JUDGE_SYSTEM_PROMPT,
    with_strict_examples_preamble,
)
from server.observability.genai import staged

log = logging.getLogger("server.memory.app.extract")


@staged("judge_facts")
async def _judge_facts(
    messages: list[dict[str, object]],
    facts: list[ExtractedFact],
    *,
    max_tokens: int = 512,
    observation_date: str | None = None,
    client: Any | None = None,
) -> tuple[dict[str, str], LLMResult | None]:
    """Grade each extracted fact against the source messages. Returns
    ({memory_key: verdict}, the call's result for the usage accounting) where
    verdict is "CORRECT" / "UNCERTAIN" / "WRONG". No verdicts when the call or
    its reply fails (caller treats as all-CORRECT fallback — fail-open, never
    silently drop a fact because the judge misfired); the result is None only
    when no call was made or it raised."""
    if not facts:
        return {}, None
    formatted_facts = "\n".join(f"  - key={f.memory_key!r}, value={f.value!r}" for f in facts)
    obs_block = f"observation_date: {observation_date}\n\n" if observation_date else ""
    user_prompt = (
        f"{obs_block}"
        f"Source messages:\n{_format_messages(messages)}\n\n"
        f"Extracted facts to judge:\n{formatted_facts}\n\n"
        'Return the JSON {"verdicts": [...]} object now.'
    )
    client = client if client is not None else get_llm_client()
    try:
        result = await client.complete(
            system=with_strict_examples_preamble(_JUDGE_SYSTEM_PROMPT),
            user=user_prompt,
            max_tokens=max_tokens,
        )
    except customer.HOLDS:
        raise  # a held call is no verdict to keep every fact on: the extract waits
    except Exception as e:  # noqa: BLE001 — a failed judge call means no verdicts, not a failed extract
        log.warning("extraction judge call failed (every fact kept): %s", e)
        return {}, None
    try:
        candidate = strip_code_fence(result.text)
        start = candidate.find("{")
        end = candidate.rfind("}")
        if start == -1 or end == -1:
            raise ValueError("no JSON object")
        payload = json.loads(candidate[start : end + 1])
        verdicts = payload.get("verdicts", [])
        if not isinstance(verdicts, list):
            raise ValueError("verdicts is not a list")
    except (json.JSONDecodeError, ValueError, AttributeError) as e:
        log.warning("extraction judge reply unreadable (every fact kept): %s", e)
        return {}, result

    out: dict[str, str] = {}
    valid = {"CORRECT", "UNCERTAIN", "WRONG"}
    for v in verdicts:
        if not isinstance(v, dict):
            continue
        key = str(v.get("key", "")).strip()
        verdict = str(v.get("verdict", "")).strip().upper()
        if key and verdict in valid:
            out[key] = verdict
    return out, result
