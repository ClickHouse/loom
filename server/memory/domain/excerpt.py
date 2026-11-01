"""The memory excerpt: the clipped "value (#key)" string a memory row is
embedded and lexically searched by, and the clip it is held to. The write
handlers build it; the mentor and the served-duplicate check read the same
limit and shape.
"""

from __future__ import annotations

from server.memory.domain.digest_keys import is_digest_key
from server.settings import settings

EXCERPT_LIMIT = 1200


def build_excerpt(
    serialized_value: str,
    *,
    memory_key: str = "",
    is_answer: bool = False,
) -> str:
    """The string we embed and lexically search against.

    Value-first format `"value (#key)"`: the key gives the embedder the
    relationship framing the value alone often lacks, while the value
    stays in front so short keys don't dominate short queries. Empty
    memory_key falls back to value-only, and so does a digest key
    (`is_digest_key`): its hex frames nothing.

    `is_answer` (extractor set `question_topic`) raises the excerpt
    ceiling to `memory_structured_answer_excerpt_limit` under
    `memory_capture_structured_answers`, so a long/enumerated answer
    survives whole in the excerpt the reader surfaces. Never touches the
    full verbatim value column.
    """
    limit = EXCERPT_LIMIT
    if (
        is_answer
        and settings.memory_capture_structured_answers
        and settings.memory_structured_answer_excerpt_limit > limit
    ):
        limit = settings.memory_structured_answer_excerpt_limit
    if memory_key and not is_digest_key(memory_key):
        suffix = f" (#{memory_key})"
        budget = max(0, limit - len(suffix))
        return serialized_value[:budget] + suffix
    return serialized_value[:limit]
