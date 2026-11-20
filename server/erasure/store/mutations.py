"""How an erasure step changes rows: count, then an ALTER UPDATE/DELETE (or a
lightweight DELETE) awaited through system.mutations by its own tag.

The count and the mutation share one filter, so the count can never report
rows the mutation didn't touch.
"""

from __future__ import annotations

import asyncio
import contextlib
import contextvars
import hashlib
import json
import logging
import re
import time
import uuid
from collections.abc import Iterator, Sequence
from datetime import UTC, datetime
from typing import Any, cast

from server.core.ch_names import split_table
from server.core.repo_base import _CHClient, _default_client
from server.erasure.store.predicates import NS_EQ, NS_IN
from server.erasure.store.rows import MutationRow
from server.settings import settings

log = logging.getLogger(__name__)


def _now_naive() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


# A mutation is awaited by state, not by socket. With `mutations_sync = 2`
# the ALTER's HTTP response waits for the rewrite, and one UPDATE over 200
# namespaces of memory_embeddings can outlive the client's 300 s read
# timeout while ClickHouse keeps mutating. So the ALTER returns at once and
# the step polls system.mutations for its own command, found by a tautology
# carrying a per-submission tag — `('erasure:<hex>' = 'erasure:<hex>')`
# survives into system.mutations.command verbatim.
_MUTATION_TAG_PARAM = "erasure_mutation_tag"
_MUTATION_POLL_S = 1.0
_MUTATION_POLL_SLOW_S = 5.0
_MUTATION_POLL_SLOW_AFTER_S = 30.0
# A submitted mutation that never shows up: the table keeps only the last
# `finished_mutations_to_keep` finished commands, so a tiny mutation that
# completed and was evicted between submit and first poll looks the same as
# one that was never registered. Past the grace the residue census is the
# arbiter, as it is for every other step.
_MUTATION_FIRST_SEEN_GRACE_S = 15.0
_MUTATION_FAIL_POLLS = 3


class MutationFailed(RuntimeError):
    """ClickHouse reports `latest_fail_reason` on the saga's mutation and keeps
    retrying it; the saga fails with the reason instead of waiting forever."""


class MutationStillRunning(TimeoutError):
    """The saga's mutation was still running when the wait budget ran out."""


# The saga whose steps are running, for the mutation tag. With the saga id in
# the tag, a step that is re-run — by the recovery worker after a crash, or by
# the CLI after the harness killed it — finds its own still-pending mutation
# in system.mutations and waits for it instead of submitting another.
_SAGA_ID: contextvars.ContextVar[str] = contextvars.ContextVar("loom_erasure_saga_id", default="")
_STEP: contextvars.ContextVar[str] = contextvars.ContextVar("loom_erasure_step", default="")


@contextlib.contextmanager
def saga_scope(erasure_id: str) -> Iterator[None]:
    """Name every mutation submitted inside the block after the saga `erasure_id`."""
    token = _SAGA_ID.set(str(erasure_id or ""))
    try:
        yield
    finally:
        _SAGA_ID.reset(token)


def current_saga() -> tuple[str, str]:
    """The saga id and step the running statement belongs to ("" outside one)."""
    return _SAGA_ID.get(), _STEP.get()


@contextlib.contextmanager
def step_scope(step: str) -> Iterator[None]:
    """Name the saga step every mutation inside the block belongs to (its tag)."""
    token = _STEP.set(step)
    try:
        yield
    finally:
        _STEP.reset(token)


def mutation_tag(
    table: str, mutation: str, where: str, params: dict[str, Any] | None = None
) -> str:
    """Deterministic within a saga: (saga, step, table, statement, bound values) → one
    tag, so a re-run step finds its own mutation. Two statements of one step differ in
    `mutation`/`where` or in the values bound to them (its slices); two steps may submit
    the same statement, and the step keeps their tags apart. Without a saga in context
    the tag is unique per call."""
    saga = _SAGA_ID.get()
    if not saga:
        return f"erasure:{uuid.uuid4().hex}"
    bound = json.dumps(params or {}, sort_keys=True, default=str)
    statement = f"{_STEP.get()}\n{table}\n{mutation}\n{where}\n{bound}"
    digest = hashlib.sha1(statement.encode()).hexdigest()[:12]
    return f"erasure:{saga}:{digest}"


def _mutation_sql(*, pending_only: bool) -> str:
    """The system.mutations read for the command carrying a tag, newest first."""
    pending = "AND is_done = 0 " if pending_only else ""
    return f"""
        SELECT mutation_id, is_done, parts_to_do, latest_fail_reason
        FROM system.mutations
        WHERE database = {{db:String}} AND table = {{t:String}}
          {pending}AND command LIKE {{pat:String}}
        ORDER BY create_time DESC
        LIMIT 1
        """


def _mutation_params(table: str, tag: str) -> dict[str, Any]:
    db, name = split_table(table)
    return {"db": db, "t": name, "pat": f"%{tag}%"}


async def find_pending_mutation(
    table: str, tag: str, *, ch: _CHClient | None = None
) -> MutationRow | None:
    """The still-running mutation carrying `tag` on `table`, if any."""
    ch = ch or _default_client()
    rows = await ch.query_all(_mutation_sql(pending_only=True), params=_mutation_params(table, tag))
    return cast("MutationRow", dict(rows[0])) if rows else None


async def _submit_and_wait(
    table: str,
    *,
    tag: str,
    statement: str,
    params: dict[str, Any],
    ch: _CHClient,
    wait_s: float | None,
) -> MutationRow | None:
    """Submit `statement` (its WHERE already carries the tag tautology) and
    wait for ClickHouse to finish it. Inside a saga the tag is deterministic,
    so a re-run step that finds its mutation still pending waits for it rather
    than submitting a second one."""
    if _SAGA_ID.get():
        pending = await find_pending_mutation(table, tag, ch=ch)
        if pending is not None:
            log.info(
                "erasure: step re-run found its mutation %s on %s still pending "
                "(parts_to_do=%s); waiting instead of resubmitting",
                pending.get("mutation_id"),
                table,
                pending.get("parts_to_do"),
            )
            return await wait_for_mutation(table, tag, ch=ch, wait_s=wait_s)
    await ch.execute(statement, params={**params, _MUTATION_TAG_PARAM: tag})
    return await wait_for_mutation(table, tag, ch=ch, wait_s=wait_s)


async def run_mutation(
    table: str,
    *,
    mutation: str,
    where: str,
    params: dict[str, Any],
    stamp: dict[str, Any] | None = None,
    ch: _CHClient | None = None,
    wait_s: float | None = None,
) -> MutationRow | None:
    """`ALTER TABLE {table} {mutation} WHERE {where}`, submitted asynchronously
    and awaited. Returns the final system.mutations row, or None when the
    mutation was never observed (see the grace note above). `stamp` holds the
    values bound beside `params` but kept out of the tag: a re-run stamps a later
    time on the same rows and must still find its own pending mutation."""
    return await _submit_and_wait(
        table,
        tag=mutation_tag(table, mutation, where, params),
        statement=f"""
        ALTER TABLE {table}
        {mutation}
        WHERE ({where})
          AND ({{{_MUTATION_TAG_PARAM}:String}} = {{{_MUTATION_TAG_PARAM}:String}})
        SETTINGS mutations_sync = 0
        """,
        params={**params, **(stamp or {})},
        ch=ch or _default_client(),
        wait_s=wait_s,
    )


# A purge of a WIDE table is a lightweight DELETE, not a part rewrite.
# `ALTER TABLE … DELETE` rewrites every column of every part the predicate
# touches and rebuilds its skip indexes (a minute per mutation on a large
# `memory_embeddings`). `DELETE FROM` writes only the
# `_row_exists` mask: the rows vanish from every read (FINAL included) at
# once and the bytes leave at the part's next merge
# (`vertical_merge_optimize_lightweight_delete`). The narrow tables keep the
# mutation: their rewrite is cheap and their bytes matter less.
LIGHTWEIGHT_PURGE_TABLES: frozenset[str] = frozenset(
    {
        "loom.memory_embeddings",
        "loom.memory_chunks",
        "loom.code_answers",
        "loom.entity_vectors",
        "loom.chunks",
        "loom.code_chunks",
        "loom.code_summaries",
        "loom.session_summaries",
        "loom.memory_evidence",
        "loom.conversation_embeddings",
        "loom.embedding_cache",
    }
)


async def run_lightweight_delete(
    table: str,
    *,
    where: str,
    params: dict[str, Any],
    ch: _CHClient | None = None,
    wait_s: float | None = None,
) -> MutationRow | None:
    """`DELETE FROM {table} WHERE {where}` with the same per-submission tag
    and the same system.mutations poll as `run_mutation`: ClickHouse runs a
    lightweight delete as a mutation over `_row_exists`, and its command
    carries the WHERE text verbatim, tag included."""
    return await _submit_and_wait(
        table,
        tag=mutation_tag(table, "LIGHTWEIGHT DELETE", where, params),
        statement=f"""
        DELETE FROM {table}
        WHERE ({where})
          AND ({{{_MUTATION_TAG_PARAM}:String}} = {{{_MUTATION_TAG_PARAM}:String}})
        SETTINGS lightweight_deletes_sync = 0
        """,
        params=params,
        ch=ch or _default_client(),
        wait_s=wait_s,
    )


async def run_keeper_delete(
    table: str, *, where: str, params: dict[str, Any], ch: _CHClient | None = None
) -> None:
    """`ALTER TABLE {table} DELETE WHERE {where}` on a KeeperMap table: a key drop
    that Keeper applies at once and that never appears in system.mutations, so
    there is nothing to tag or wait for."""
    await (ch or _default_client()).execute(
        f"ALTER TABLE {table} DELETE WHERE {where}", params=params
    )


async def wait_for_mutation(
    table: str,
    tag: str,
    *,
    ch: _CHClient | None = None,
    wait_s: float | None = None,
) -> MutationRow | None:
    """Poll system.mutations until the command carrying `tag` is done."""
    ch = ch or _default_client()
    budget = float(settings.erasure_mutation_wait_s if wait_s is None else wait_s)
    started = time.monotonic()
    seen = False
    failing = 0
    while True:
        rows = await ch.query_all(
            _mutation_sql(pending_only=False), params=_mutation_params(table, tag)
        )
        row = rows[0] if rows else None
        elapsed = time.monotonic() - started
        if row is None:
            if seen or elapsed >= _MUTATION_FIRST_SEEN_GRACE_S:
                if not seen:
                    log.warning(
                        "erasure: mutation %s on %s never appeared in system.mutations "
                        "within %.0fs; the residue census decides",
                        tag,
                        table,
                        _MUTATION_FIRST_SEEN_GRACE_S,
                    )
                return None
        else:
            seen = True
            if int(row.get("is_done") or 0):
                return cast("MutationRow", dict(row))
            reason = str(row.get("latest_fail_reason") or "")
            failing = failing + 1 if reason else 0
            if failing >= _MUTATION_FAIL_POLLS:
                raise MutationFailed(
                    f"mutation {row.get('mutation_id')} on {table} keeps failing: {reason}"
                )
        if elapsed >= budget:
            raise MutationStillRunning(
                f"mutation {tag} on {table} still running after {elapsed:.0f}s "
                f"(parts_to_do={row.get('parts_to_do') if row else '?'})"
            )
        await asyncio.sleep(
            _MUTATION_POLL_S if elapsed < _MUTATION_POLL_SLOW_AFTER_S else _MUTATION_POLL_SLOW_S
        )


# Set around a saga, and the census that verifies it, for an erasure requested with
# purge=true; read through purge_mode(). A step may switch it off to stamp rows later
# steps still read.
_PURGE: contextvars.ContextVar[bool] = contextvars.ContextVar("loom_erasure_purge", default=False)


def purge_mode() -> bool:
    return bool(_PURGE.get())


@contextlib.contextmanager
def purge_scope(on: bool) -> Iterator[None]:
    """Inside the block the tombstone steps purge (`on`) or tombstone."""
    token = _PURGE.set(bool(on))
    try:
        yield
    finally:
        _PURGE.reset(token)


# The tombstone steps count and mutate live rows only. A purge removes every
# row of the scope, tombstoned ones included — otherwise a re-run in purge mode
# after a tombstoning pass finds "0 live rows" and leaves every byte in the
# parts.
_LIVE_PRED_RE = re.compile(r"\s+AND\s+tombstoned_at IS NULL\b")


def _strip_live_predicate(where: str) -> str:
    return _LIVE_PRED_RE.sub("", where)


def ns_list_predicate(where: str) -> str:
    """`namespace = {ns:String}` → `namespace IN {ns:Array(String)}`, inside a longer
    column name too (`memory_namespace`)."""
    return where.replace(NS_EQ, NS_IN)


def namespaces_of(namespace: str | Sequence[str]) -> list[str]:
    return [namespace] if isinstance(namespace, str) else [str(x) for x in namespace]


async def count_then_mutate(
    table: str,
    *,
    count_where: str,
    mutation: str,
    mutation_where: str,
    params: dict[str, Any],
    final: bool = True,
    set_now_param: str = "now",
    lightweight: bool = False,
    ch: _CHClient | None = None,
) -> int:
    """Generic erasure step: count, then ALTER UPDATE/DELETE.

    `mutation` is the body after `ALTER TABLE {table}` — e.g.
    `UPDATE tombstoned_at = {now:DateTime}` or `DELETE`. Adds `now` to
    params automatically if `set_now_param` is referenced. Returns row count.
    `lightweight` runs a `DELETE` as a lightweight delete in every mode, not
    only under a purge: a wide derived table's rows leave every read at once
    and the bytes leave at the next merge.
    """
    ch = ch or _default_client()
    # A list of namespaces in `ns`: the same predicate, once, over every
    # namespace of the scope — one mutation per table for the whole list
    # instead of one saga per namespace.
    if isinstance(params.get("ns"), (list, tuple)):
        count_where = ns_list_predicate(count_where)
        mutation_where = ns_list_predicate(mutation_where)
        params = {**params, "ns": [str(x) for x in params["ns"]]}
    # Purge: a throwaway namespace's rows leave the parts, not just the
    # readable set — a tombstone keeps the bytes in every FINAL read and
    # merge for good.
    if purge_mode() and mutation.startswith("UPDATE tombstoned_at"):
        mutation = "DELETE"
        count_where = _strip_live_predicate(count_where)
        mutation_where = _strip_live_predicate(mutation_where)
    final_clause = "FINAL" if final else ""
    row = await ch.query_one(
        f"""
        SELECT count() AS n FROM {table} {final_clause}
        WHERE {count_where}
        """,
        params=params,
    )
    n = int(row["n"]) if row else 0
    if n == 0:
        return 0
    if mutation == "DELETE" and (purge_mode() or lightweight) and table in LIGHTWEIGHT_PURGE_TABLES:
        await run_lightweight_delete(table, where=mutation_where, params=params, ch=ch)
    else:
        stamp = {}
        if set_now_param in mutation and set_now_param not in params:
            stamp[set_now_param] = _now_naive()
        await run_mutation(
            table, mutation=mutation, where=mutation_where, params=params, stamp=stamp, ch=ch
        )
    return n


async def tombstone_scoped(
    table: str,
    *,
    org: str,
    scope_where: str,
    params: dict[str, Any],
    ch: _CHClient | None,
    final: bool = True,
) -> int:
    """`final=False` for a plain MergeTree that carries `tombstoned_at`
    (loom.conversations): FINAL is illegal there (code 181) and there is no
    version to collapse — the count reads the parts as they are."""
    where = f"org = {{org:String}} AND tombstoned_at IS NULL AND {scope_where}"
    return await count_then_mutate(
        table,
        count_where=where,
        mutation="UPDATE tombstoned_at = {now:DateTime}",
        mutation_where=where,
        params={"org": org, **params},
        final=final,
        ch=ch,
    )


async def delete_scoped(
    table: str,
    *,
    org: str,
    scope_where: str,
    params: dict[str, Any],
    ch: _CHClient | None,
    lightweight: bool = False,
) -> int:
    where = f"org = {{org:String}} AND {scope_where}"
    return await count_then_mutate(
        table,
        count_where=where,
        mutation="DELETE",
        mutation_where=where,
        params={"org": org, **params},
        final=False,
        lightweight=lightweight,
        ch=ch,
    )
