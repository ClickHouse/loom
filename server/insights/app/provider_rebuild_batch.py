"""Regenerate a bounded batch for a staged embedding transition.

This module never writes ClickHouse rows or changes the process-wide embedder.
The coordinator supplies an explicit target provider and writes the returned
full rows only after the entire batch validates. Source rows stay untouched.
"""

import asyncio
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from server.core.repo_base import _CHClient
from server.embedding.embedder import Embedder
from server.insights.domain.provider_rebuild import EMBEDDING_SURFACES
from server.insights.store.provider_staging import session_chunks
from server.knowledge.domain.chunking import embedding_text
from server.llm import customer
from server.memory.app.model_attribution import attribution_for, memory_writers
from server.memory.domain.embed_text import key_axis_text
from server.memory.domain.import_keys import is_import_key
from server.memory.domain.okf_evidence import IMPORT_MAX_ITEMS
from server.memory.domain.sensitivity import joined_overlaps_high, reads_high
from server.settings import settings
from server.workers.holds import UNREAD_HOLD_S, hold_seconds

# Stored text recipes mirror the original write paths. Tracker rows do not
# contain their source text and therefore require explicit hydration by caller.
TEXT_COLUMNS = {
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
SPECIAL_TEXT_TABLES = frozenset({"categories", "chunks", "code_chunks", "qa_item_embeddings"})
# Raw-passage tables: a row whose text reads as high is staged without a vector (see rebuild_batch).
PASSAGE_TABLES = frozenset({"conversation_embeddings", "memory_chunks", "session_summaries"})
_CALL_BATCH = 64


class RebuildBatchError(ValueError):
    """A safe status code, never source content or an upstream provider body."""


@dataclass(frozen=True)
class RebuildBatchResult:
    rows: list[dict[str, Any]]
    rows_waiting: int
    retry_after_s: float = 0.0


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
        elif table == "chunks":
            # As the write path embedded it: the context, then the text. A chunk with
            # neither stays unindexed; an older build's titled empty chunk fails until
            # rechunked.
            value = embedding_text(row["context"], row["content"])
        else:
            value = row[TEXT_COLUMNS[table]]
    except KeyError:
        raise RebuildBatchError("source_text_missing") from None
    if not isinstance(value, str):
        raise RebuildBatchError("source_text_invalid")
    return value


def _has_vector(row: Mapping[str, Any], columns: Sequence[str]) -> bool:
    return any(any(row[column]) for column in columns)


def _reembed_columns(table: str) -> list[str]:
    return [s.column for s in EMBEDDING_SURFACES if s.table == table and s.action == "reembed"]


def _held_run(by_index: Mapping[int, Sequence[Mapping[str, Any]]], at: int) -> set[int]:
    """The indexes of the stored chunks around `at` that a key held at `at` could span:
    outward from it while a chunk has no vector (`embedding_dim = 0`), plus the first chunk
    with one on each side. The write path holds every chunk a key covers and those chunks
    are contiguous, so the key's header is inside this run."""
    run: set[int] = set()
    for step in (-1, 1):
        n = at + step
        while n in by_index:
            run.add(n)
            if any(int(chunk["embedding_dim"]) for chunk in by_index[n]):
                break
            n += step
    return run


def _held_in_session(
    stored: Sequence[Mapping[str, Any]], page: Sequence[tuple[int, str]]
) -> list[bool]:
    """For each `(chunk_index, content)` page row of one session, whether it overlaps a
    high shape in the text it makes with the runs of stored chunks around the page's rows
    (`_held_run`). The runs are joined and read once (`joined_overlaps_high`) for the whole
    session, so a long session is not read again for every row."""
    by_index: dict[int, list[Mapping[str, Any]]] = {}
    for chunk in stored:
        by_index.setdefault(int(chunk["chunk_index"]), []).append(chunk)
    around: set[int] = set()
    for at, _ in page:
        around |= _held_run(by_index, at)
    chunks = [("s", n, str(chunk["content"])) for n in sorted(around) for chunk in by_index[n]]
    chunks.extend(("s", at, text) for at, text in page)
    return joined_overlaps_high(chunks)[len(chunks) - len(page) :]


async def _held_chunks(rows: Sequence[Mapping[str, Any]], ch: _CHClient | None) -> set[int]:
    """The indexes of the memory chunks stored without a vector that are part of a key the
    write path held (`_held_in_session`). Only a plain chunk without a vector needs its
    session read, once per session on the page."""
    columns = _reembed_columns("memory_chunks")
    by_session: dict[tuple[str, str, str], list[int]] = {}
    for index, row in enumerate(rows):
        if (
            row.get("tombstoned_at") is None
            and all(column in row for column in columns)
            and not _has_vector(row, columns)
            and str(row.get("content") or "").strip()
            and not reads_high(str(row["content"]))
        ):
            session = (str(row["org"]), str(row["namespace"]), str(row["session_id"]))
            by_session.setdefault(session, []).append(index)
    # One write call stores at most this many chunks, so a key spans no more.
    cap = max(
        settings.memory_chunks_max_per_call,
        settings.memory_set_chunk_max_chunks,
        IMPORT_MAX_ITEMS,
    )
    held: set[int] = set()
    for session, indexes in by_session.items():
        page = [(int(rows[i]["chunk_index"]), str(rows[i]["content"])) for i in indexes]
        ats = [at for at, _ in page]
        stored = await session_chunks(*session, max(0, min(ats) - cap), max(ats) + cap, ch=ch)
        # Reading a long session's text is CPU work; it runs off the event loop.
        flags = await asyncio.to_thread(_held_in_session, stored, page)
        held.update(index for index, hit in zip(indexes, flags, strict=True) if hit)
    return held


def _inputs(
    table: str,
    rows: Sequence[Mapping[str, Any]],
    source_texts: Mapping[int, str],
    held: set[int],
) -> tuple[list[str], list[tuple[int, str]]]:
    columns = _reembed_columns(table)
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
        if table in PASSAGE_TABLES and (index in held or reads_high(text)):
            continue
        for column in columns:
            value = text
            if column == "memory_key_embedding":
                if "memory_key" not in row:
                    raise RebuildBatchError("source_text_missing")
                value = key_axis_text(row["memory_key"])
                if is_import_key(str(row["memory_key"])) and any(row[column]):
                    # An import key with a key vector from an older build: rebuilt from
                    # the raw key; `loom reembed --key-only` clears it.
                    value = str(row["memory_key"])
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
    ch: _CHClient | None = None,
) -> RebuildBatchResult:
    """Return staged rows and the retry delay for model calls held on this page.

    Model holds leave empty vectors until the coordinator retries this page.
    Verification prevents publishing those rows before their vectors are rebuilt.

    A raw passage (`PASSAGE_TABLES`) whose text reads as high is staged
    without a vector and never sent to the provider; verification reads its text
    and accepts it. A memory chunk stored without a vector is staged without one
    too when a key touches it in the text it makes with the run of its session's
    chunks without a vector around it: the write path held it as part of a key
    cut across chunks. Any other row stored without a vector (a failed embed, or a row a
    rebuild staged while its org's model calls were off) is embedded.

    The caller must hydrate tracker text from its source and supply every stored
    column. Tombstones remain tombstones and are never sent to the provider.
    Version, creation time, ownership, provenance and content remain unchanged.
    Prefix twins, caches and historical events have separate inventory actions.
    """
    if not target.model_name or type(target.dim) is not int or not 1 <= target.dim <= 65535:
        raise RebuildBatchError("embedding_target_invalid")
    held = await _held_chunks(rows, ch) if table == "memory_chunks" else set()
    texts, slots = _inputs(table, rows, source_texts or {}, held)
    # One call carries one org's texts, of one identity: a model call names a single
    # customer and identity.
    writers = await memory_writers([rows[index] for index, _ in slots])
    groups = await customer.groups(attribution_for(rows[index], writers) for index, _ in slots)
    vectors: list[list[float]] = [[] for _ in texts]
    left: set[int] = set()
    retry_after_s = 0.0
    for who, positions in groups.items():
        if await customer.switched_off(who.org):
            left.update(positions)
            retry_after_s = max(retry_after_s, UNREAD_HOLD_S)
            continue
        for start in range(0, len(positions), _CALL_BATCH):
            part = positions[start : start + _CALL_BATCH]
            try:
                with customer.attributed(who):
                    produced = await target.embed_many([texts[i] for i in part])
            except customer.GROUP_HOLDS as error:
                # Switched off during the batch, or the identity refused, asked to wait
                # or unread: its rows wait.
                left.update(positions)
                retry_after_s = max(retry_after_s, hold_seconds(error))
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
    for i, ((index, column), vector) in enumerate(zip(slots, vectors, strict=True)):
        if i in left:
            continue
        result[index][column] = vector
        dimension_column = (
            "memory_key_embedding_dim" if column == "memory_key_embedding" else "embedding_dim"
        )
        if dimension_column in result[index]:
            result[index][dimension_column] = target.dim
    return RebuildBatchResult(result, len({slots[i][0] for i in left}), retry_after_s)
