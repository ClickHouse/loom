"""Regenerate a bounded batch for a staged embedding transition.

This module never writes ClickHouse rows or changes the process-wide embedder.
The coordinator supplies an explicit target provider and writes the returned
full rows only after the entire batch validates. Source rows stay untouched.
"""

import math
from collections.abc import Mapping, Sequence
from typing import Any

from server.embedding.embedder import Embedder
from server.insights.domain.provider_rebuild import EMBEDDING_SURFACES
from server.llm import customer
from server.memory.domain.embed_text import key_axis_text
from server.memory.domain.sensitivity import passage_reads_high

# Stored text recipes mirror the original write paths. Tracker rows do not
# contain their source text and therefore require explicit hydration by caller.
TEXT_COLUMNS = {
    "chunks": "content",
    "code_answers": "question",
    "code_summaries": "summary",
    "conversation_embeddings": "content_excerpt",
    "entity_summaries": "summary_text",
    "entity_vectors": "entity_text",
    "memory_chunks": "content",
    "memory_embeddings": "content_excerpt",
    "query_nodes": "query",
    "session_summaries": "summary_text",
}
SPECIAL_TEXT_TABLES = frozenset({"categories", "code_chunks", "qa_item_embeddings"})
# Raw passages the write path leaves without a vector when they read as a credential
# (`passage_sensitivity`); a rebuild stages such a row without one too.
PASSAGE_TABLES = frozenset({"conversation_embeddings", "memory_chunks", "session_summaries"})
_CALL_BATCH = 64


class RebuildBatchError(ValueError):
    """A safe status code, never source content or an upstream provider body."""


def _source_text(
    table: str, row: Mapping[str, Any], index: int, source_texts: Mapping[int, str]
) -> str:
    try:
        if table == "qa_item_embeddings":
            value = source_texts[index]
        elif table == "categories":
            value = f"{row['name']}\n{row['description']}".strip()
        elif table == "code_chunks":
            value = row["context_text"] or row["content"]
        else:
            value = row[TEXT_COLUMNS[table]]
    except KeyError:
        raise RebuildBatchError("source_text_missing") from None
    if not isinstance(value, str):
        raise RebuildBatchError("source_text_invalid")
    return value


def _inputs(
    table: str, rows: Sequence[Mapping[str, Any]], source_texts: Mapping[int, str]
) -> tuple[list[str], list[tuple[int, str]]]:
    columns = [s.column for s in EMBEDDING_SURFACES if s.table == table and s.action == "reembed"]
    if not columns:
        raise RebuildBatchError("unsupported_rebuild_table")
    texts: list[str] = []
    slots: list[tuple[int, str]] = []
    for index, row in enumerate(rows):
        if "embedding_model" not in row or any(column not in row for column in columns):
            raise RebuildBatchError("incomplete_source_row")
        if row.get("tombstoned_at") is not None:
            continue
        text = _source_text(table, row, index, source_texts)
        if table in PASSAGE_TABLES and passage_reads_high(row.get("sensitivity"), text):
            continue
        for column in columns:
            value = text
            if column == "memory_key_embedding":
                if "memory_key" not in row:
                    raise RebuildBatchError("source_text_missing")
                value = key_axis_text(row["memory_key"])
            if value.strip():
                texts.append(value)
                slots.append((index, column))
            elif any(row[column]):
                # A real vector cannot be rebuilt without text; an all-zero one is the
                # insert path's placeholder for none (vectors/placeholders.py), and
                # provider_verify accepts it staged empty.
                raise RebuildBatchError("source_text_missing")
    return texts, slots


def _validate_vectors(vectors: list[list[float]], count: int, dim: int) -> None:
    if not isinstance(vectors, list):
        raise RebuildBatchError("embedding_vector_invalid")
    if len(vectors) != count:
        raise RebuildBatchError("embedding_count_mismatch")
    for vector in vectors:
        if not isinstance(vector, list):
            raise RebuildBatchError("embedding_vector_invalid")
        if len(vector) != dim:
            raise RebuildBatchError("embedding_dimension_mismatch")
        try:
            norm = sum(value * value for value in vector)
            valid = (
                all(
                    not isinstance(value, bool)
                    and math.isfinite(value)
                    and abs(value) <= 3.3895313892515355e38  # Largest finite BFloat16.
                    for value in vector
                )
                and math.isfinite(norm)
                and norm > 0
            )
        except (TypeError, ValueError, OverflowError):
            valid = False
        if not valid:
            raise RebuildBatchError("embedding_vector_invalid")


def _empty_target_row(table: str, row: Mapping[str, Any], model: str) -> dict[str, Any]:
    result = dict(row)
    result["embedding_model"] = model
    for surface in EMBEDDING_SURFACES:
        if surface.table == table:
            result[surface.column] = []
    for name, value in {
        "embedding_dim": 0,
        "memory_key_embedding_dim": 0,
        "embed_attempts": 0,
        "embed_last_error": "",
        "embed_failed_at": None,
    }.items():
        if name in result:
            result[name] = value
    return result


async def rebuild_batch(
    table: str,
    rows: Sequence[Mapping[str, Any]],
    target: Embedder,
    *,
    source_texts: Mapping[int, str] | None = None,
    waiting: list[int] | None = None,
) -> list[dict[str, Any]]:
    """Return full staged rows; never publish partial or malformed embeddings.

    A row whose org's model calls are switched off is staged without a vector
    (`embedding_dim = 0`), as a tombstone is, and its index added to `waiting`;
    the rebuild completes and counts it in its progress (`rows_waiting`). Once the
    org is back on, memory_embeddings and code_chunks / code_summaries stubs are
    embedded again by their recovery workers; a row of any other table has no
    vector until a write replaces it or a later rebuild covers it.

    The caller must hydrate tracker text from its source and supply every stored
    column. Tombstones remain tombstones and are never sent to the provider.
    Version, creation time, ownership, provenance and content remain unchanged.
    Prefix twins, caches and historical events have separate inventory actions.
    """
    if not target.model_name or type(target.dim) is not int or not 1 <= target.dim <= 65535:
        raise RebuildBatchError("embedding_target_invalid")
    texts, slots = _inputs(table, rows, source_texts or {})
    # One call carries one org's texts: a model call names a single customer.
    by_org = await customer.by_org(str(rows[index].get("org", "")) for index, _ in slots)
    vectors: list[list[float]] = [[] for _ in texts]
    left: set[int] = set()
    for org, positions in by_org.items():
        if await customer.switched_off(org):
            left.update(positions)
            continue
        for start in range(0, len(positions), _CALL_BATCH):
            part = positions[start : start + _CALL_BATCH]
            try:
                with customer.for_org(org):
                    produced = await target.embed_many([texts[i] for i in part])
            except customer.ModelCallsDisabled:
                left.update(positions)  # switched off during the batch
                break
            except customer.SwitchUnread:
                raise  # not a provider failure: the step waits for the switch to be read
            except Exception:  # noqa: BLE001 — provider errors must not expose keys or source text in status
                raise RebuildBatchError("embedding_provider_failed") from None
            _validate_vectors(produced, len(part), target.dim)
            for i, vector in zip(part, produced, strict=True):
                vectors[i] = vector
    result = [_empty_target_row(table, row, target.model_name) for row in rows]
    for i in sorted(left):
        vectors[i] = []  # staged without a vector, as the row's empty target row has
    if waiting is not None:
        waiting.extend(sorted({slots[i][0] for i in left}))
    for i, ((index, column), vector) in enumerate(zip(slots, vectors, strict=True)):
        if i in left:
            continue
        result[index][column] = vector
        dimension_column = (
            "memory_key_embedding_dim" if column == "memory_key_embedding" else "embedding_dim"
        )
        if dimension_column in result[index]:
            result[index][dimension_column] = target.dim
    return result
