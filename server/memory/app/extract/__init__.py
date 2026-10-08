"""LLM-based memory extraction from raw messages.

The caller passes `messages: list[{role, content}]`; the first pass is one
LLM completion with a focused prompt (retried once when its reply does not
parse, is cut off, or finds nothing in substantive input), optionally
followed by the coverage sweep, the relation backfill and the judge — each
one more call. The result is a list of `ExtractedFact` ready to fan out via
`handle_memory_bulk_set`.

Fail-closed: a parse error or LLM error bubbles up as an exception rather
than silently writing zero memories — losing an extracted transcript is
not recoverable from the caller's perspective.

Modules, each importing only from those listed before it:

- `models`: the fact, record and result shapes, the error, the prompt version.
- `client`: the client an extraction's calls share, and usage accounting.
- `turns`: how message turns are shown to the model.
- `keys`: deterministic memory-key rewrites.
- `parse`: the reply parser and truncated-reply salvage.
- `speakers`: speaker tags and assistant attribution.
- `dates`: the date passes (anchors, backstop, immediacy, grounding).
- `enrich`: rows the model left out, citation repair, code facets.
- `judge`, `relations`: the write-time judge and the relation backfill calls.
- `prompts`: the first pass's system and user prompts.
- `coverage`: lexical coverage measures over a first pass.
- `sweep`: the coverage sweep's prompts and call.
- `extract`: `extract_from_messages`.
- `typed`: `extract_typed_record_iterative`, schema-mode extraction.

This module exports what callers outside the package use, including the
prompt strings, date resolver and code-facet extractor those callers read
through it from their leaf modules; everything else is imported from the
submodule that defines it.
"""

from __future__ import annotations

from server.code.domain.facets import extract_code_facets
from server.memory.app.extract.client import extractor_llm_cache_on
from server.memory.app.extract.coverage import statement_dense_turns
from server.memory.app.extract.extract import extract_from_messages
from server.memory.app.extract.models import (
    EXTRACTION_PROMPT_VERSION,
    MAX_DERIVED_PER_FACT,
    ExtractedFact,
    ExtractedRecord,
    ExtractionError,
    ExtractionResult,
)
from server.memory.app.extract.prompts import _use_compact_prompt
from server.memory.app.extract.relations import _RELATION_BACKFILL_SYSTEM_PROMPT
from server.memory.app.extract.turns import _message_stamp
from server.memory.app.extract.typed import extract_typed_record_iterative
from server.memory.domain.extraction_prompts import (
    _EXTRACTION_SYSTEM_PROMPT,
    _STRUCTURED_ANSWER_PROMPT_BLOCK,
    with_strict_examples_preamble,
)
from server.memory.domain.extraction_temporal import resolve_relative_date

__all__ = [
    "EXTRACTION_PROMPT_VERSION",
    "MAX_DERIVED_PER_FACT",
    "_EXTRACTION_SYSTEM_PROMPT",
    "_RELATION_BACKFILL_SYSTEM_PROMPT",
    "_STRUCTURED_ANSWER_PROMPT_BLOCK",
    "ExtractedFact",
    "ExtractedRecord",
    "ExtractionError",
    "ExtractionResult",
    "_message_stamp",
    "_use_compact_prompt",
    "extract_code_facets",
    "extract_from_messages",
    "extract_typed_record_iterative",
    "extractor_llm_cache_on",
    "resolve_relative_date",
    "statement_dense_turns",
    "with_strict_examples_preamble",
]
