"""A code vector table is compacted when its copies outnumber its keys.

`loom.code_chunks` and its vector twins are `ReplacingMergeTree(version)` keyed `(org,
namespace, embedding_model, path, chunk_id)`. A bulk pass that re-emits unchanged rows
(a promotion, a re-describe, an embed phase) leaves one extra physical copy per key
until a merge collapses it, and big parts rarely merge, so the vector leg's stage-1
shortlist reads every copy: a table at two copies per key costs that read twice over
until one OPTIMIZE FINAL collapses it.

`copies_per_key` is the measure: physical active rows from `system.parts`
(metadata only) over `uniq` of the key tuple (reads the five key columns,
no vectors). `maybe_compact` runs `OPTIMIZE TABLE ... FINAL` when the ratio
reaches the threshold and waits for the merge to land: on SharedMergeTree
the statement returns before the merge completes, so the wait polls
`system.merges` on every replica and the active part count.

`compact_after_bulk` is the hook the bulk CLI passes call at their end. It
never raises and never touches `loom.code_chunks` (the largest table): that table is
compacted only by `loom code compact --table code_chunks --apply`, in an announced
window.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass
from typing import Any

from server.code.store.index import compaction as compaction_store
from server.settings import settings

logger = logging.getLogger(__name__)

PREFIX_TABLE = "loom.code_chunks_prefix"
CHUNKS_TABLE = "loom.code_chunks"
DEFAULT_POLL_S = 15.0


@dataclass
class CompactResult:
    table: str
    ran: bool = False
    reason: str = ""
    parts_before: int = 0
    rows_before: int = 0
    keys: int = 0
    ratio: float = 0.0
    parts_after: int = 0
    rows_after: int = 0
    ratio_after: float = 0.0
    waited_s: float = 0.0
    timed_out: bool = False
    error: str = ""

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def allowed_tables() -> set[str]:
    """The tables compaction may name: the chunk table, the prefix twin, and
    every table `LOOM_CODE_VECTOR_TABLES` maps (validated there)."""
    from server.core.vectors.code_tables import mapping

    return {CHUNKS_TABLE, PREFIX_TABLE} | {t for t, _dim in mapping().values()}


def resolve_table(table: str) -> str:
    """`code_chunks_prefix` or `loom.code_chunks_prefix` → the qualified name;
    anything outside the allowlist raises ValueError (the name is
    interpolated into SQL)."""
    name = (table or "").strip()
    if name and "." not in name:
        name = f"loom.{name}"
    if name not in allowed_tables():
        raise ValueError(f"not a code vector table: {table!r}")
    return name


async def copies_per_key(ch: Any, table: str) -> CompactResult:
    """Physical rows over distinct keys for one allowlisted table."""
    t = resolve_table(table)
    parts, rows = await compaction_store.active_parts(ch, t)
    keys = await compaction_store.distinct_keys(ch, t) if rows else 0
    return CompactResult(
        table=t,
        parts_before=parts,
        rows_before=rows,
        keys=keys,
        ratio=round(rows / keys, 3) if keys else 0.0,
    )


async def wait_for_merge(
    ch: Any,
    table: str,
    *,
    parts_before: int,
    rows_before: int,
    timeout_s: float,
    poll_s: float = DEFAULT_POLL_S,
    sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> tuple[int, int, float, bool]:
    """Poll until no replica merges `table` and its active parts (or rows)
    dropped, or `timeout_s` passes. Returns (parts, rows, waited_s, timed_out)."""
    started = clock()
    parts, rows = parts_before, rows_before
    while True:
        await sleep(poll_s)
        merging = await compaction_store.merges_running(ch, table)
        parts, rows = await compaction_store.active_parts(ch, table)
        waited = clock() - started
        if merging == 0 and (parts < parts_before or rows < rows_before):
            return parts, rows, waited, False
        if waited >= timeout_s:
            return parts, rows, waited, True


async def maybe_compact(
    ch: Any,
    table: str,
    *,
    min_ratio: float = 1.5,
    dry_run: bool = False,
    timeout_s: float | None = None,
    poll_s: float = DEFAULT_POLL_S,
    sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> CompactResult:
    """OPTIMIZE ... FINAL `table` when its copies per key reach `min_ratio`,
    then wait for the merge to land. Raises only on a table outside the
    allowlist; a CH failure is reported in `error`."""
    if timeout_s is None:
        timeout_s = settings.code_compaction_timeout_s
    res = await copies_per_key(ch, table)
    if res.keys == 0 or res.ratio < min_ratio:
        res.reason = f"ratio {res.ratio} < {min_ratio}"
        return res
    if dry_run:
        res.reason = f"dry run: ratio {res.ratio} >= {min_ratio}"
        return res
    from server.core.ch import statement_timeout

    try:
        with statement_timeout(max(timeout_s, 300.0)):
            await compaction_store.optimize_final(ch, res.table)
    except Exception as e:  # noqa: BLE001 — reported, never raised
        res.error = f"{type(e).__name__}: {e}"[:300]
        return res
    res.ran = True
    res.parts_after, res.rows_after, res.waited_s, res.timed_out = await wait_for_merge(
        ch,
        res.table,
        parts_before=res.parts_before,
        rows_before=res.rows_before,
        timeout_s=timeout_s,
        poll_s=poll_s,
        sleep=sleep,
        clock=clock,
    )
    res.ratio_after = round(res.rows_after / res.keys, 3) if res.keys else 0.0
    res.reason = "timed out waiting for the merge" if res.timed_out else "compacted"
    return res


def bulk_tables(model: str) -> list[str]:
    """What a bulk pass under `model` compacts: the prefix twin, the model's configured
    twins, and the model's own table when it is not `loom.code_chunks` (a second-model
    table)."""
    from server.code.domain.index.prefix_twin import prefix_twins_for
    from server.core.vectors.code_tables import code_vector_table

    out = [PREFIX_TABLE] + [t for _label, _dim, t in prefix_twins_for(model)]
    own = code_vector_table(model) if model else CHUNKS_TABLE
    if own != CHUNKS_TABLE:
        out.append(own)
    seen: list[str] = []
    for t in out:
        if t not in seen and t != CHUNKS_TABLE:
            seen.append(t)
    return seen


async def compact_after_bulk(
    ch: Any,
    rows_written: int,
    model: str,
    **kw: Any,
) -> list[CompactResult]:
    """The bulk passes' end hook: when the pass wrote at least
    `code_compact_after_bulk_rows` rows (0 = off), compact each of
    `bulk_tables(model)` whose ratio reaches `code_compact_min_ratio`.
    Never raises."""
    from server.settings import settings

    threshold = int(settings.code_compact_after_bulk_rows)
    if threshold <= 0 or rows_written < threshold:
        return []
    min_ratio = float(settings.code_compact_min_ratio)
    out: list[CompactResult] = []
    try:
        tables = bulk_tables(model)
    except Exception as e:  # noqa: BLE001 — the pass already succeeded; never raises
        logger.warning("compaction: table set for %r unavailable: %s", model, e)
        return out
    for t in tables:
        try:
            res = await maybe_compact(ch, t, min_ratio=min_ratio, **kw)
        except Exception as e:  # noqa: BLE001 — the pass already succeeded
            logger.warning("compaction after bulk pass: %s failed: %s", t, e)
            continue
        logger.info("compaction after bulk pass (%d rows): %s", rows_written, res.as_dict())
        out.append(res)
    return out


def describe(res: CompactResult) -> str:
    """One operator line for a result."""
    line = (
        f"{res.table}: parts={res.parts_before} rows={res.rows_before} keys~{res.keys} "
        f"copies/key={res.ratio}"
    )
    if res.ran:
        line += (
            f" -> parts={res.parts_after} rows={res.rows_after} copies/key={res.ratio_after}"
            f" ({res.waited_s:.0f}s{', TIMED OUT' if res.timed_out else ''})"
        )
    if res.error:
        line += f" error: {res.error}"
    elif res.reason and not res.ran:
        line += f" ({res.reason})"
    return line
