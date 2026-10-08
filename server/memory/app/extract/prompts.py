"""The first pass's prompts: the system prompt for the full or compact
examples with its flag-gated blocks, and the user prompt that layers the date, existing memories, prior turns and the current
messages.
"""

from __future__ import annotations

from server.memory.app.extract.turns import _format_messages
from server.memory.domain.extraction_prompts import (
    _EXTRACTION_SYSTEM_PROMPT,
    _STRUCTURED_ANSWER_PROMPT_BLOCK,
    compact_extraction_prompt,
)
from server.settings import settings


def _use_compact_prompt(messages: list[dict[str, object]]) -> bool:
    """A short input is extracted with the compact system
    prompt. The size is the current messages' content, the part the examples
    must match; prior turns and existing memories are context only."""
    limit = int(settings.memory_extract_compact_prompt_max_chars)
    if limit <= 0:
        return False
    return sum(len(str(m.get("content", "") or "")) for m in messages) <= limit


def _extraction_base_prompt(compact: bool) -> str:
    """The extraction system prompt before the preamble: the full or the
    compact examples, plus the flag-gated blocks."""
    base_prompt = (
        compact_extraction_prompt(_EXTRACTION_SYSTEM_PROMPT)
        if compact
        else (_EXTRACTION_SYSTEM_PROMPT)
    )
    # Flag-gated: append the structured-answer block + Examples 8f/8g so a
    # long/enumerated/positional assistant answer is captured whole. A compact
    # input holds no long answer to capture.
    if settings.memory_capture_structured_answers and not compact:
        base_prompt = base_prompt + "\n\n" + _STRUCTURED_ANSWER_PROMPT_BLOCK
    return base_prompt


def _build_extraction_user_prompt(
    messages: list[dict[str, object]],
    observation_date: str | None,
    existing_memories: list[dict[str, str]] | None,
    last_messages: list[dict[str, object]] | None = None,
) -> str:
    """Assemble the user-prompt blob the extractor sees.

    Layered: observation_date → existing_memories → last_messages →
    current messages. The LLM reads context first (what's already
    known, what the prior conversation said) and applies dedup +
    pronoun-resolution judgement when extracting from `messages`.
    """
    parts: list[str] = []
    if observation_date:
        parts.append(f"observation_date: {observation_date}")
    if existing_memories:
        # Anti-hallucination: present existing memories with short
        # integer indices instead of raw memory_keys. The LLM emits
        # `linked_keys: [3]` referring to index 3; the caller maps
        # back to the real memory_key. This stops the LLM from
        # inventing or misspelling memory_keys when linking (a typo
        # like "user_dog_nam", or a wholesale fabricated key).
        # The existing memories are agent-written text read by the model that
        # writes the next rows, a write-side injection channel. Each body sits
        # between nonce-carrying markers the body cannot forge; the index label
        # stays outside so `linked_keys` is unchanged.
        from server.memory.domain.prompt_fence import fence, new_nonce

        values = [str(m.get("value", "") or "") for m in existing_memories]
        nonce = new_nonce("extract", *values)
        memory_lines = [f"  [{idx}] {fence(str(idx), v, nonce)}" for idx, v in enumerate(values)]
        parts.append(
            "existing_memories (refer to these by integer index when "
            "linking — e.g. `linked_keys: [3]` means the existing "
            "memory at index 3):\n" + "\n".join(memory_lines),
        )
    if last_messages:
        parts.append(
            "prior_turns (context — do NOT re-extract from these; "
            "use them to resolve pronouns and references in the "
            "current messages below):\n" + _format_messages(last_messages),
        )
    if parts:
        parts.append("---")
    parts.append("current_messages (extract from these):")
    parts.append(_format_messages(messages, include_indices=True))
    return "\n".join(parts)
