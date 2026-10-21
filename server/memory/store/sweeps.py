"""Worker scans over the memory tables: expired rows for the expiry sweep,
the embedding queue — stubs to embed and memories with no embedding row —
and the live entity_vectors rows a dead key's re-emit tombstones.
"""

from __future__ import annotations

from typing import Any

from server.core.repo_base import _CHClient, _default_client
from server.memory.domain.memory_types import NON_SEARCHABLE_TYPES
from server.memory.store.rows import MemoryRow, _hydrate
from server.memory.store.sql import _MEMORY_FINAL_PROJECTION, _SEQUENTIAL, attested_sql

_NOT_TTL_MANAGED = (
    " AND (org, namespace) NOT IN"
    " (SELECT org, namespace FROM loom.namespaces FINAL WHERE retention_days > 0)"
)

# A lock in force that refuses the row's post-action (memory_lock.REFUSES:
# read_only refuses every action, no_delete the tombstone — the action for
# an empty or unknown `on_expire`) keeps the row out of the batch. Filtered
# BEFORE the LIMIT: the sweep skips such a row, so a batch of them would be
# read again every pass and starve every other tenant's expiries.
_NOT_LOCKED_AGAINST_EXPIRY = (
    " AND NOT (lock_mode != '' AND (lock_until IS NULL OR lock_until > now())"
    " AND (lock_mode = 'read_only'"
    " OR (lock_mode = 'no_delete' AND on_expire NOT IN ('demote', 'anonymize'))))"
)


async def find_expired_candidates(
    *,
    batch_size: int = 500,
    org_filter: str | None = None,
    namespace_filter: str | None = None,
    ch: _CHClient | None = None,
) -> list[MemoryRow]:
    """Live rows whose `expires_at` has passed, oldest expiry first — the
    expiry sweep's input — outside the namespaces whose table TTL deletes
    them. An expiry is only ever the writer's
    own (`ttl_seconds` / `expires_at`, a type's or a namespace's configured
    window): loom sets none by default. Full projection so the sweep can
    write a version row without a second read. The read filter already
    hides these rows from get/search; the sweep gives them their post-action.
    """
    ch = ch or _default_client()
    # A namespace with `retention_days` is deleted by its own table TTL
    # (`storage.retention_overrides`, keyed on
    # coalesce(expires_at, created_at + N)); a version row the sweep writes
    # there is already past that TTL and the next merge drops it, so the live
    # row stays and every pass rewrites it. Those rows are the TTL's.
    extra = _NOT_TTL_MANAGED + _NOT_LOCKED_AGAINST_EXPIRY
    params: dict[str, Any] = {"lim": int(batch_size)}
    if org_filter:
        extra += " AND org = {org_filter:String}"
        params["org_filter"] = org_filter
    if namespace_filter:
        extra += " AND namespace = {ns_filter:String}"
        params["ns_filter"] = namespace_filter
    # Ask whether anything has expired before reading the sixty-column
    # projection under FINAL. The probe is narrow (two
    # columns) and not FINAL: a live version that qualifies is a physical
    # row, so an empty probe means an empty sweep; a superseded version that
    # still qualifies only costs the full read that follows.
    probe = await ch.query_all(
        f"""
        SELECT 1
        FROM loom.memory
        WHERE tombstoned_at IS NULL
          AND expires_at IS NOT NULL
          AND expires_at <= now(){extra}
        LIMIT 1
        """,
        params={k: v for k, v in params.items() if k != "lim"},
    )
    if not probe:
        return []
    rows = await ch.query_all(
        _MEMORY_FINAL_PROJECTION
        + f"""
        WHERE tombstoned_at IS NULL
          AND expires_at IS NOT NULL
          AND expires_at <= now(){extra}
        ORDER BY expires_at ASC
        LIMIT {{lim:UInt32}}
        {_SEQUENTIAL}
        """,
        params=params,
    )
    return [_hydrate(r) for r in rows]


async def find_embedding_stubs(
    *,
    batch_size: int,
    max_attempts: int,
    org_filter: str | None = None,
    namespace_filter: str | None = None,
    exclude_orgs: frozenset[str] = frozenset(),
    ch: _CHClient | None = None,
) -> list[dict[str, Any]]:
    """memory_embeddings rows with embedding_dim=0 (stubs) that haven't
    been poisoned. Used by the embedding_worker. `exclude_orgs` (orgs whose
    model calls are switched off) leaves their stubs where they are. Held stubs
    wait until `embed_retry_at` before taking a slot.

    `version` rides along so the worker can write its replacement at
    stub_version + 1: newer than the stub it read, older than any row a
    later memory.set wrote in the meantime (fence, see _carry_signals)."""
    from server.core.ch_params import chunks

    ch = ch or _default_client()
    extra_where = ""
    params: dict[str, Any] = {"lim": int(batch_size), "ma": int(max_attempts)}
    if org_filter is not None:
        extra_where += " AND org = {org_filter:String}"
        params["org_filter"] = org_filter
    if namespace_filter is not None:
        extra_where += " AND namespace = {ns_filter:String}"
        params["ns_filter"] = namespace_filter
    if exclude_orgs:
        extra_where += " AND org NOT IN {exclude_orgs:Array(String)}"
        params["exclude_orgs"] = sorted(exclude_orgs)
    # Two phases. Under FINAL the WHERE is applied after the merge, so a
    # single query would read every selected column of every row (3.1 GB and
    # 488k rows per pass) to find an empty queue. Phase 1 has no FINAL:
    # PREWHERE on the UInt16 `embedding_dim` alone touches ~30 MB and yields
    # candidate KEYS (some may be stale pre-merge versions, which is fine:
    # they only narrow phase 2). Phase 2 is the FINAL read, restricted by the
    # full sort key of the candidates so the primary index prunes to their
    # granules.
    # Newest stub version first: stale versions of since-embedded stubs and
    # old versions of poisoned ones are old, so they cannot fill the window
    # ahead of a fresh stub while they wait for a merge (such keys can
    # outnumber the window).
    candidates = await ch.query_all(
        f"""
        SELECT org, namespace, agent, user_id, session_id,
               scope, embedding_model, memory_key
        FROM loom.memory_embeddings
        PREWHERE embedding_dim = 0
        WHERE tombstoned_at IS NULL
          AND embed_failed_at IS NULL
          AND embed_attempts < {{ma:UInt16}}
          {extra_where}
        GROUP BY org, namespace, agent, user_id, session_id,
                 scope, embedding_model, memory_key
        HAVING argMax(embed_retry_at IS NULL OR embed_retry_at <= now64(3), version) = 1
        ORDER BY max(version) DESC
        LIMIT {{cand_lim:UInt32}}
        """,
        params={**params, "cand_lim": int(batch_size) * 4},
    )
    if not candidates:
        return []
    # The candidate keys go to the next reads in parameters that each fit one form
    # field (server/core/ch_params.py): a few hundred cell-shaped keys exceed it.
    confirmed: list[dict[str, Any]] = []
    for part in chunks([_stub_key(r) for r in candidates]):
        confirmed.extend(await _confirmed_stubs(ch, part, params, extra_where))
    if not confirmed:
        return []
    parts = list(chunks([_stub_key(r) for r in confirmed]))
    rows: list[dict[str, Any]] = []
    for part in parts:
        rows.extend(await _stub_rows(ch, part, params, extra_where))
    if len(parts) > 1:
        rows.sort(key=lambda r: (r.get("created_at") is None, r.get("created_at") or 0))
    return rows[: int(batch_size)]


def _stub_key(r: dict[str, Any]) -> tuple[str, ...]:
    return (
        str(r["org"]),
        str(r["namespace"]),
        str(r["agent"]),
        str(r["user_id"]),
        str(r["session_id"]),
        str(r["scope"]),
        str(r["embedding_model"]),
        str(r["memory_key"]),
    )


async def _confirmed_stubs(
    ch: _CHClient, keys: list[tuple[str, ...]], params: dict[str, Any], extra_where: str
) -> list[dict[str, Any]]:
    """Phase 1b. A stub row that was later embedded keeps its stub VERSION in the
    table until a merge collapses it, so phase 1 keeps returning it, and the FINAL
    read would read every column of every granule those keys touch, the two vectors
    included (1.17 GB a tick for a few hundred stale stubs), to return nothing. Each
    candidate is confirmed on its newest version first: a keyed GROUP BY without
    FINAL reads the narrow columns of the same granules only."""
    rows: list[dict[str, Any]] = await ch.query_all(
        f"""
        SELECT org, namespace, agent, user_id, session_id, scope,
               embedding_model, memory_key
        FROM loom.memory_embeddings
        WHERE (org, namespace, agent, user_id, session_id, scope,
               embedding_model, memory_key)
              IN {{keys:Array(Tuple(String, String, String, String,
                                    String, String, String, String))}}
          {extra_where}
        GROUP BY org, namespace, agent, user_id, session_id, scope,
                 embedding_model, memory_key
        HAVING argMax(embedding_dim, version) = 0
           -- argMax skips NULLs: test IS NULL inside it, or an older tombstoned
           -- row (a high write's fence) hides the newer live stub above it
           AND argMax(tombstoned_at IS NULL, version) = 1
           AND argMax(embed_failed_at IS NULL, version) = 1
           AND argMax(embed_attempts, version) < {{ma:UInt16}}
           AND argMax(embed_retry_at IS NULL OR embed_retry_at <= now64(3), version) = 1
        """,
        params={**params, "keys": keys},
    )
    return rows


async def _stub_rows(
    ch: _CHClient,
    keys: list[tuple[str, ...]],
    params: dict[str, Any],
    extra_where: str,
) -> list[dict[str, Any]]:
    """Read confirmed stubs oldest first, rechecking their retry time under FINAL."""
    rows: list[dict[str, Any]] = await ch.query_all(
        f"""
        SELECT org, namespace, agent, user_id, session_id, memory_key,
               memory_type, scope, content_excerpt, embedding_model, created_at,
               embed_attempts, run_id, entities, tags, files, concepts,
               valid_at, valid_to, invalid_at, expires_at,
               temporal_anchor, temporal_confidence,
               reinforcement_count, last_reinforced_at,
               mentioned_at, source_resource_id, status, confidence,
               memory_key_embedding, memory_key_embedding_dim,
               source_class, verified, conflict_checked_at, content_sha256,
               version
        FROM loom.memory_embeddings FINAL
        WHERE (org, namespace, agent, user_id, session_id, scope,
               embedding_model, memory_key)
              IN {{keys:Array(Tuple(String, String, String, String,
                                    String, String, String, String))}}
          AND embedding_dim = 0
          AND tombstoned_at IS NULL
          AND embed_failed_at IS NULL
          AND embed_attempts < {{ma:UInt16}}
          AND (embed_retry_at IS NULL OR embed_retry_at <= now64(3))
          {extra_where}
        ORDER BY created_at ASC
        LIMIT {{lim:UInt32}}
        """,
        params={**params, "keys": keys},
    )
    return rows


# The loom.memory columns an embedding row mirrors, named as
# loom.memory_embeddings names them, for `build_embedding_row(**row)`: only
# columns that exist on loom.memory (`build_embedding_row` supplies `scope`'s
# default). `status`, `invalid_at` and `verified` are carried: a default here
# would revive a superseded fact, or drop a current attestation, for every
# reader of the embedding row. `verified` is the attestation's current state
# (trust.verification_state human/process), as memory.verify mirrors it.
# `content_excerpt` is the whole stored value; a caller trims it.
_EMBEDDING_MIRROR_COLUMNS = f"""m.org, m.namespace, m.agent, m.user_id, m.session_id, m.memory_key,
               m.memory_type, m.value AS content_excerpt, m.run_id,
               m.entities, m.tags, m.files, m.concepts,
               m.valid_at, m.valid_to, m.expires_at,
               m.temporal_anchor, m.temporal_confidence,
               m.reinforcement_count, m.last_reinforced_at,
               m.mentioned_at, m.source_resource_id, m.status, m.confidence,
               m.source_class, m.invalid_at,
               toUInt8({attested_sql("m.")}) AS verified,
               m.content_sha256, m.created_at"""


async def find_memories_to_reindex(
    org: str,
    namespace: str,
    *,
    embedding_model: str,
    force: bool,
    limit: int,
    ch: _CHClient | None = None,
) -> list[dict[str, Any]]:
    """memory.reindex's input: the namespace's live memories, newest first,
    as embedding-mirror rows (`_EMBEDDING_MIRROR_COLUMNS`). Without `force`,
    only keys with no live embedding row under `embedding_model` (the model
    is part of the embeddings sort key, so an old-model row coexists with the
    new one). The rows memory.set never embeds are never returned: a
    'high'-sensitivity row stays out of every search leg by having no
    embedding row, and episodic/profile rows are unsearchable."""
    ch = ch or _default_client()
    missing_only = (
        ""
        if force
        else """
          AND m.memory_key NOT IN (
              SELECT memory_key FROM loom.memory_embeddings FINAL
              WHERE org = {org:String} AND namespace = {ns:String}
                AND embedding_model = {model:String}
                AND tombstoned_at IS NULL
          )"""
    )
    rows = await ch.query_all(
        f"""
        SELECT {_EMBEDDING_MIRROR_COLUMNS}, m.version AS version
        FROM loom.memory AS m FINAL
        WHERE m.org = {{org:String}} AND m.namespace = {{ns:String}}
          AND m.tombstoned_at IS NULL
          AND m.sensitivity != 'high'
          AND m.memory_type NOT IN {{unsearchable:Array(String)}}{missing_only}
        ORDER BY m.created_at DESC
        LIMIT {{lim:UInt32}}
        """,
        params={
            "org": org,
            "ns": namespace,
            "model": embedding_model,
            "lim": int(limit),
            "unsearchable": sorted(NON_SEARCHABLE_TYPES),
        },
    )
    if not rows:
        return []
    # Each row is written one above the newest of its memory row and its key's rows of
    # this model (an embedding-only re-emit or a stub tombstone can sit above the
    # memory row), not at a version drawn after the embed call: a 'high' write that
    # lands meanwhile fences above it. One keyed read for the batch. That read can
    # also see a high write's fence landing after the row read; the reindex re-reads
    # the heads after its insert for that (`memory.reindex`).
    keys: list[tuple[str, str, str, str]] = [
        (str(r["agent"]), str(r["user_id"]), str(r["session_id"]), str(r["memory_key"]))
        for r in rows
    ]
    newest = await ch.query_all(
        """
        SELECT agent, user_id, session_id, memory_key, max(version) AS ev
        FROM loom.memory_embeddings
        WHERE org = {org:String} AND namespace = {ns:String}
          AND embedding_model = {model:String}
          AND (agent, user_id, session_id, memory_key)
              IN {keys:Array(Tuple(String, String, String, String))}
        GROUP BY agent, user_id, session_id, memory_key
        """,
        params={"org": org, "ns": namespace, "model": embedding_model, "keys": keys},
    )
    ev = {
        (str(n["agent"]), str(n["user_id"]), str(n["session_id"]), str(n["memory_key"])): int(
            n["ev"]
        )
        for n in newest
    }
    return [
        {
            **r,
            "memory_version": int(r["version"]),  # the head it read; the caller re-checks it
            "version": max(int(r["version"]), ev.get(k, 0)) + 1,
        }
        for r, k in zip(rows, keys, strict=True)
    ]


async def find_memories_without_embedding_rows(
    *,
    batch_size: int,
    org_filter: str | None = None,
    namespace_filter: str | None = None,
    since_seconds: int | None = None,
    ch: _CHClient | None = None,
) -> list[dict[str, Any]]:
    """Live memories with NO `memory_embeddings` row at all — unfindable by search.

    `since_seconds` narrows the scan to memories created in
    that window and to the embedding keys of the namespaces those memories
    live in — the shape the worker runs on every pass; the full anti-join
    (None) runs on its own slow clock. The recent-namespace set is read
    without FINAL: a version row of any age names the namespace, and a
    superset there only widens the pruned side.

    Distinct from `find_embedding_stubs`, which returns rows with
    `embedding_dim = 0`: those are queued and the worker drains them. These have no
    row to queue, so nothing would ever pick them up: stored, permanently
    unsearchable, while the embed backlog reads 0.

    Reconciliation rather than chasing write paths: any current or future path that
    inserts a memory without its embedding sibling is repaired here, instead of
    needing every writer to remember. The worker turns these into ordinary stubs and
    the existing embed loop does the rest.
    """
    ch = ch or _default_client()
    extra_where = ""
    params: dict[str, Any] = {"lim": int(batch_size)}
    if org_filter is not None:
        extra_where += " AND m.org = {org_filter:String}"
        params["org_filter"] = org_filter
    if namespace_filter is not None:
        extra_where += " AND m.namespace = {ns_filter:String}"
        params["ns_filter"] = namespace_filter
    # Two reads. The anti-join has to see every live memory key against every
    # live embedding key, and under FINAL every selected column of every row
    # is read — selecting the content columns here would cost 233 MB a pass
    # to return 3 rows. Phase 1 joins the sort
    # keys only; phase 2 reads the full rows of the few keys that came back,
    # pruned by the primary index. `length(value) > 0` moves to phase 2: it
    # would have forced the value column into phase 1.
    #
    # Even keys-only, the full join is 110 MB a pass (every live embedding key
    # on the service) for a handful of rows. With a
    # window, the left side is the memories of the last `since_seconds` and
    # the right side only the embedding keys of the namespaces they live in
    # — the primary index prunes the rest (2.5 MB for a ten-minute window).
    with_recent = ""
    e_where = ""
    if since_seconds is not None and since_seconds > 0:
        params["since_s"] = int(since_seconds)
        with_recent = """
        WITH recent AS (
            SELECT DISTINCT org, namespace
            FROM loom.memory
            WHERE created_at > now() - toIntervalSecond({since_s:UInt32})
              AND tombstoned_at IS NULL
        )"""
        e_where = " AND (org, namespace) IN recent"
    # The window clause belongs to phase 1 only: `recent` is a CTE of that
    # statement, and phase 2 addresses exact keys, so a window there would be
    # redundant at best and at worst a reference to a CTE that statement does
    # not define (code 47).
    window_where = (
        " AND (m.org, m.namespace) IN recent"
        " AND m.created_at > now() - toIntervalSecond({since_s:UInt32})"
        if with_recent
        else ""
    )
    orphan_keys = await ch.query_all(
        f"""{with_recent}
        SELECT m.org, m.namespace, m.agent, m.user_id, m.session_id, m.memory_key
        FROM loom.memory AS m FINAL
        LEFT ANTI JOIN (
            SELECT org, namespace, agent, user_id, session_id, memory_key
            FROM loom.memory_embeddings FINAL
            WHERE tombstoned_at IS NULL{e_where}
        ) AS e
        ON  m.org        = e.org
        AND m.namespace  = e.namespace
        AND m.agent      = e.agent
        AND m.user_id    = e.user_id
        AND m.session_id = e.session_id
        AND m.memory_key = e.memory_key
        WHERE m.tombstoned_at IS NULL
          -- A 'high' row has no embedding row BY DESIGN; without this filter
          -- the reconcile would re-embed one right after the sensitivity
          -- sweep tombstoned its vector. Exclude it here, not downstream:
          -- the stub alone would already be a live embedding row.
          AND m.sensitivity != 'high'
          -- Episodic and profile rows are never embedded (is_embedded()); a
          -- missing embedding row is their normal state, not an orphan.
          AND m.memory_type NOT IN {{unsearchable:Array(String)}}
          {extra_where}{window_where}
        ORDER BY m.created_at ASC
        LIMIT {{cand_lim:UInt32}}
        """,
        params={
            **params,
            "cand_lim": int(batch_size) * 2,
            "unsearchable": sorted(NON_SEARCHABLE_TYPES),
        },
    )
    if not orphan_keys:
        return []
    keys = [
        (
            str(r["org"]),
            str(r["namespace"]),
            str(r["agent"]),
            str(r["user_id"]),
            str(r["session_id"]),
            str(r["memory_key"]),
        )
        for r in orphan_keys
    ]
    # The memory row's version rides along as the stub's: a 'high' write that lands
    # after this read fences above it, so the stub (and its fill at + 1) stays hidden.
    return await ch.query_all(
        f"""
        SELECT {_EMBEDDING_MIRROR_COLUMNS}, m.version AS version
        FROM loom.memory AS m FINAL
        WHERE (m.org, m.namespace, m.agent, m.user_id, m.session_id, m.memory_key)
              IN {{keys:Array(Tuple(String, String, String, String, String, String))}}
          AND m.tombstoned_at IS NULL
          AND m.sensitivity != 'high'
          AND length(m.value) > 0
          {extra_where}
        ORDER BY m.created_at ASC
        LIMIT {{lim:UInt32}}
        """,
        params={**{k: v for k, v in params.items() if k != "since_s"}, "keys": keys},
    )


async def memory_mirror_rows_for_keys(
    keys: list[tuple[str, str, str, str, str, str]],
    *,
    ch: _CHClient | None = None,
    sequential: bool = False,
) -> list[dict[str, Any]]:
    """The live, embeddable memory rows of the given identity keys as embedding-mirror
    rows (`_EMBEDDING_MIRROR_COLUMNS` plus `version`): what a stub for the embed worker
    is built from."""
    if not keys:
        return []
    ch = ch or _default_client()
    return await ch.query_all(
        f"""
        SELECT {_EMBEDDING_MIRROR_COLUMNS}, m.version AS version
        FROM loom.memory AS m FINAL
        WHERE (m.org, m.namespace, m.agent, m.user_id, m.session_id, m.memory_key)
              IN {{keys:Array(Tuple(String, String, String, String, String, String))}}
          AND m.tombstoned_at IS NULL
          AND m.sensitivity != 'high'
          AND m.memory_type NOT IN {{unsearchable:Array(String)}}
          AND length(m.value) > 0
        {_SEQUENTIAL if sequential else ""}
        """,
        params={"keys": [tuple(k) for k in keys], "unsearchable": sorted(NON_SEARCHABLE_TYPES)},
    )


def entity_vectors_live_sql() -> str:
    """Live entity_vectors rows for a batch of dead keys in one namespace.
    `FINAL` so an already re-emitted tombstone is not selected again; every
    column, because the re-emit must carry the sort key and the vector."""
    return """
        SELECT * FROM loom.entity_vectors FINAL
        WHERE org = {org:String} AND namespace = {ns:String}
          AND memory_key IN {keys:Array(String)}
          AND tombstoned_at IS NULL
    """
