"""Erasure of the Postgres memory authority: heads, revisions, their deliveries and
the replay answers, the first step of a saga so nothing is published again from
what it removes.

The step deletes inside one transaction that holds the scope's generation rows (so
no commit of the scope lands meanwhile, and a semantic commit whose verdict links a
memory it removed reads its peers anew: `pg_memory.PeerErased`); a user's scope takes
the consolidated rows the user's memories fed in their own namespace. It then waits out
every publisher that may still be sending what it deleted: a publisher stops sending
when its lease is about to end, an insert it sent before then starts within the
transit allowance of the lease's end (`pg_lease.leased`), and it runs for at most
the write limit (`ch.write_limit_s`, `pg.wait_out`). The ClickHouse steps that follow
then find every copy that arrives within that bound.

`pg_knowledge.delete` does the same for knowledge documents: it removes the scope's
heads and deliveries under the namespaces' document lock, keeps the document ids of
every version of the scope's documents as evidence that refuses a write built from
them (`loom.erased_documents`), and waits out the removed deliveries' publishers.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from typing import Any

from server.core import pg
from server.settings import settings

# The consolidated rows a user's memories fed, under another identity: tagged
# `consolidated` and `src:<key>` with a key the user wrote in the row's own namespace
# (`predicates.CONSOLIDATED_IN`). A row of another namespace built from a key of the same
# name is not the user's.
_CONSOLIDATED_IDS = """
WITH mine AS (
    SELECT namespace, 'src:' || k AS tag FROM (
        SELECT namespace, memory_key AS k FROM loom.memory_current WHERE org = $1 AND user_id = $2
        UNION SELECT namespace, coalesce(memory_key, row->>'memory_key') FROM loom.memory_revisions
        WHERE org = $1 AND user_id = $2) AS keys),
hits AS (
    SELECT DISTINCT c.namespace, c.memory_id FROM loom.memory_current c
    CROSS JOIN LATERAL jsonb_array_elements_text(c.row->'tags') AS t(tag)
    JOIN mine ON (mine.namespace, mine.tag) = (c.namespace, t.tag)
    WHERE c.org = $1 AND c.consolidated AND c.namespace IN (SELECT namespace FROM mine))
SELECT coalesce(array_agg(namespace), '{}') AS namespaces,
       coalesce(array_agg(memory_id), '{}') AS ids
FROM hits
"""


def _evidence(quiet_at: str = "now()") -> str:
    """The CTE keeping each id `gone` removed as deletion evidence, with `quiet_at`: when
    no publisher of what it removed can still land a row. Each column keeps the later of
    what is stored and what this statement stamps, so the heads' statement never moves
    the revisions' back, and an id erased again later restarts its evidence."""
    return (
        "evidence AS (INSERT INTO loom.erased_memories AS e (org, namespace, memory_id, "
        f"quiet_at) SELECT DISTINCT org, namespace, memory_id, {quiet_at} FROM gone "
        "ON CONFLICT (org, namespace, memory_id) DO UPDATE SET "
        "erased_at = GREATEST(e.erased_at, EXCLUDED.erased_at), "
        "quiet_at = GREATEST(e.quiet_at, EXCLUDED.quiet_at) "
        "WHERE e.erased_at < EXCLUDED.erased_at OR e.quiet_at < EXCLUDED.quiet_at)"
    )


# `memory_deliveries.targets`' audit-row bit: a copy of `pg_memory.AUDIT_ROW`, which this
# layer cannot import (pinned equal in test_pg_authority).
_AUDIT_ROW = 4
# The order every locker of heads takes them in: a copy of `pg_memory._KEY_ORDER`,
# unaliased (pinned equal in test_pg_authority).
_KEY_ORDER = "ORDER BY org, namespace, agent, user_id, session_id, memory_key"

# The order every locker of knowledge heads takes them in: a copy of
# `pg_documents._KEY_ORDER`, which this layer cannot import (pinned equal in
# test_pg_authority).
_KNOWLEDGE_KEY_ORDER = "ORDER BY org, namespace, source_uri"

_BUMP_SCOPES = (
    "INSERT INTO loom.scope_generations AS g (org, scope, generation) "
    'SELECT $1, ns, 1 FROM unnest($2::text[]) AS ns ORDER BY ns COLLATE "C" '
    "ON CONFLICT (org, scope) DO UPDATE SET generation = g.generation + 1"
)


async def _erase(org: str, namespaces: list[str] | None, user_id: str | None) -> int:
    """The scope's rows out of Postgres, then the wait for their publishers."""
    removed, leases = await _after_deadlocks(_erase_once, org, namespaces, user_id)
    # A delivery another holder finished is kept settled until the insert bound has
    # passed after its lease and any fence (`pg_memory.sweep_settled`): its lease is
    # among these.
    await pg.wait_out(leases)
    return removed


async def _after_deadlocks(
    once: Callable[..., Awaitable[tuple[int, list[float]]]], *args: Any
) -> tuple[int, list[float]]:
    """`once(*args)`, run again after a deadlock: a user's scope can grow by a namespace
    that sorts before one it holds, against a concurrent erasure's order, and Postgres
    then aborts one side. The third attempt's deadlock is raised."""
    import asyncpg

    for _ in range(2):
        try:
            return await once(*args)
        except asyncpg.exceptions.DeadlockDetectedError:
            pass
    return await once(*args)


async def _earlier_run(
    conn: Any, org: str, namespaces: list[str] | None, table: str = "loom.erased_memories"
) -> list[float]:
    """An earlier run of this erasure that removed rows and stopped before its wait, as a
    lease: its evidence (`table`) says until when a publisher of them may still land one
    (`quiet_at`), and this run, which finds them gone, waits as long. Read before this run
    deletes anything. A user's scope has no column to read the evidence by: the org's
    stands for it, another erasure's wait in the org included."""
    where = "org = $1 AND quiet_at > now()"
    params: list[Any] = [org]
    if namespaces is not None:
        where += " AND namespace = ANY($2::text[])"
        params.append(namespaces)
    left_s = await conn.fetchval(
        f"SELECT extract(epoch FROM max(quiet_at) - now()) FROM {table} WHERE {where}",
        *params,
    )
    # `pg.wait_out` adds the insert bound to a lease; `quiet_at` holds it already.
    return [] if left_s is None else [float(left_s) - pg.insert_bound_s()]


async def _erase_once(
    org: str, namespaces: list[str] | None, user_id: str | None
) -> tuple[int, list[float]]:
    # A large scope takes longer than a request's statement budget, on the client's
    # timeout as on the server's: its own connection, with neither. The scope's writes wait.
    async with pg.dedicated() as conn, conn.transaction():
        # Advancing each generation locks it: the scope's commits wait for this
        # transaction, and an admission that captured the old one checks again.
        scoped: list[str] | None = None  # a user's scope reads the org's evidence
        if user_id is None:
            args: list[Any] = [org, sorted(set(namespaces or []))]
            scoped = args[1]
            where_r = "r.org = $1 AND r.namespace = ANY($2::text[])"
            where_c = "org = $1 AND namespace = ANY($2::text[])"
            await conn.execute(_BUMP_SCOPES, org, args[1])
        else:
            # A dependent by (namespace, id): a restored namespace keeps the ids. The id arm
            # comes first so each statement reads the (org, memory_id) index.
            pair = "IN (SELECT * FROM unnest($3::text[], $4::uuid[]))"
            where_r = (
                "r.org = $1 AND (r.user_id = $2 OR (r.memory_id = ANY($4::uuid[]) "
                f"AND (r.namespace, r.memory_id) {pair}))"
            )
            where_c = (
                "org = $1 AND (user_id = $2 OR (memory_id = ANY($4::uuid[]) "
                f"AND (namespace, memory_id) {pair}))"
            )
            args = await _lock_user_scope(conn, org, user_id, where_c, where_r)
        # Locks in a commit's order: generations, then heads by key (`_KEY_ORDER`), then
        # the scope's deliveries by seq, locked before their leases are read so a claim
        # cannot take one between the read and the delete (`claim_due` skips locked rows).
        await conn.execute(
            f"SELECT 1 FROM loom.memory_current WHERE {where_c} {_KEY_ORDER} FOR UPDATE",
            *args,
        )
        await conn.execute(
            "SELECT 1 FROM loom.memory_deliveries d JOIN loom.memory_revisions r "
            f"USING (org, namespace, seq) WHERE {where_r} "
            "ORDER BY d.org, d.namespace, d.seq FOR UPDATE OF d",
            *args,
        )
        # The deliveries go with their revisions (ON DELETE CASCADE). This statement locks
        # them and reads their leases, any committed since the locks above included, and
        # moves the audit rows ClickHouse lacks to the outbox: the audit trail outlives the
        # memory. Each removed id is kept as deletion evidence, refused at a commit.
        bound = f"${len(args) + 1}"
        # With no delivery removed, no publisher is left to wait for.
        quiet_at = (
            "coalesce((SELECT now() + make_interval(secs => max(left_s) + "
            f"{bound}) FROM held HAVING count(*) > 0), now())"
        )
        earlier = await _earlier_run(conn, org, scoped)
        removed = await conn.fetchrow(
            f"WITH gone AS (DELETE FROM loom.memory_revisions r WHERE {where_r} "
            "RETURNING org, namespace, seq, memory_id, audit), "
            f"held AS (SELECT {pg.LEASE_LEFT_S} AS left_s, d.targets, gone.org, "
            "  gone.namespace, gone.audit FROM loom.memory_deliveries d "
            f"  JOIN gone USING (org, namespace, seq) FOR UPDATE OF d), {_evidence(quiet_at)}, "
            "outbox AS (INSERT INTO loom.audit_outbox (org, namespace, row_hash, audit, due_at) "
            "  SELECT org, namespace, audit->>'row_hash', audit, "
            f"    now() + make_interval(secs => greatest(left_s, 0) + {bound}) "
            f"  FROM held WHERE targets & {_AUDIT_ROW} <> 0 ON CONFLICT DO NOTHING) "
            "SELECT (SELECT count(*) FROM gone) AS n, "
            "  coalesce((SELECT array_agg(left_s) FROM held), '{}') AS left_s",
            *args,
            pg.insert_bound_s(),
        )
        revisions, leases = removed["n"], [*removed["left_s"], *earlier]
        heads = await conn.fetchval(
            f"WITH gone AS (DELETE FROM loom.memory_current WHERE {where_c} "
            f"RETURNING org, namespace, memory_id), {_evidence()} SELECT count(*) FROM gone",
            *args,
        )
    return int(revisions or 0) + int(heads or 0), [float(x) for x in leases]


async def _lock_user_scope(
    conn: Any, org: str, user_id: str, where_c: str, where_r: str
) -> list[Any]:
    """Lock every namespace a user's scope reaches, reading the scope again once they
    are locked until it reaches no namespace more: a consolidated row committed
    meanwhile is in it. Returns the statements' arguments."""
    locked: set[str] = set()
    while True:
        dependents = await conn.fetchrow(_CONSOLIDATED_IDS, org, user_id)
        args = [org, user_id, list(dependents["namespaces"]), list(dependents["ids"])]
        scoped = {
            r["namespace"]
            for r in await conn.fetch(
                f"SELECT namespace FROM loom.memory_current WHERE {where_c} "
                f"UNION SELECT namespace FROM loom.memory_revisions r WHERE {where_r}",
                *args,
            )
        }
        if scoped <= locked:
            return args
        await conn.execute(_BUMP_SCOPES, org, sorted(scoped - locked))
        locked |= scoped


async def delete_pg_memory_by_namespace(org: str, namespace: str | Sequence[str]) -> int:
    # A name Postgres cannot store holds no row there.
    names = [
        n
        for n in ([namespace] if isinstance(namespace, str) else namespace)
        if not pg.unstorable(n)
    ]
    if pg.unstorable(org) or not names:
        return 0
    return await _erase(org, names, None)


async def delete_pg_memory_by_user(org: str, user_id: str) -> int:
    if pg.unstorable((org, user_id)):
        return 0  # no row holds what Postgres cannot store
    return await _erase(org, None, user_id)


async def _erase_knowledge(org: str, namespaces: list[str] | None, user_id: str | None) -> int:
    """The scope's knowledge heads and deliveries (`loom.knowledge_heads`,
    `loom.knowledge_deliveries`) out of Postgres, their documents' ids kept as deletion
    evidence, then the wait for the publishers of those deliveries, so the documents
    step below finds every copy they could land."""
    removed, leases = await _after_deadlocks(_erase_knowledge_once, org, namespaces, user_id)
    await pg.wait_out(leases)
    return removed


async def _erase_knowledge_once(
    org: str, namespaces: list[str] | None, user_id: str | None
) -> tuple[int, list[float]]:
    # A large scope outlasts a request's statement budget: its own connection, with none.
    async with pg.dedicated() as conn, conn.transaction():
        # Idle in this transaction while each ClickHouse evidence read runs.
        await conn.execute(f"SET LOCAL idle_in_transaction_session_timeout = {_evidence_idle_ms()}")
        arg: list[str] | str  # the namespaces, or the user
        scoped: list[str] | None  # None: a user's scope reads the org's evidence
        if user_id is None:
            names = sorted(set(namespaces or []))
            where, arg, scoped = "org = $1 AND namespace = ANY($2::text[])", names, names
            await _lock_knowledge(conn, org, names)
        else:
            where, arg, scoped = "org = $1 AND user_id = $2", user_id, None
            await _lock_user_knowledge(conn, org, user_id)
        earlier = await _earlier_run(conn, org, scoped, "loom.erased_documents")
        # Locks in a commit's order: heads by key, then the deliveries.
        await conn.execute(
            f"SELECT 1 FROM loom.knowledge_heads WHERE {where} {_KNOWLEDGE_KEY_ORDER} FOR UPDATE",
            org,
            arg,
        )
        # A parked delivery's last send is covered by its `fenced_until`, and a settled
        # one's (taken over and finished) replaced request by its lease.
        leases = await conn.fetch(
            f"DELETE FROM loom.knowledge_deliveries d WHERE {where} RETURNING d.namespace, "
            f"d.row->>'doc_id' AS doc_id, {pg.LEASE_LEFT_S} AS left_s",
            org,
            arg,
        )
        heads = await conn.fetch(
            f"DELETE FROM loom.knowledge_heads WHERE {where} "
            "RETURNING namespace, row->>'doc_id' AS doc_id",
            org,
            arg,
        )
        left = [float(r["left_s"]) for r in leases]
        # Every version's doc_id, ClickHouse's too, read under the namespaces' locks: a
        # write built from a read of any of them, one a later write replaced included,
        # keeps its id and is refused. A write that reads after the erasure carries none
        # (a new version takes a fresh id, and a deleted document lends none).
        quiet_s = max(left) + pg.insert_bound_s() if left else 0.0
        removed = sorted({(r["namespace"], r["doc_id"]) for r in [*leases, *heads] if r["doc_id"]})
        await _keep_evidence(conn, org, removed, quiet_s)
        async for page in documents_in_clickhouse(org, scoped, user_id):
            await _keep_evidence(conn, org, page, quiet_s)
    return len(heads) + len(leases), [*left, *earlier]


async def documents_in_clickhouse(
    org: str, namespaces: list[str] | None, user_id: str | None, *, ch: Any = None
) -> AsyncIterator[list[tuple[str, str]]]:
    """The (namespace, doc_id) of every loom.documents version of the scope ClickHouse
    holds, merged-away ones aside, page by page along the table's key (each namespace
    by source URI), each read sequentially consistent: a version another replica
    acknowledged is among them. A user's scope first reads the namespaces holding the
    user's rows."""
    from server.core.ch import get_client, sequential_reads

    ch = ch or get_client()
    size = settings.pg_erasure_evidence_page_rows
    mine = "" if user_id is None else " AND user_id = {uid:String}"
    if user_id is None:
        names = sorted(namespaces or [])
    else:
        with sequential_reads():
            rows = await ch.query_all(
                "SELECT DISTINCT namespace FROM loom.documents "
                "WHERE org = {org:String} AND user_id = {uid:String} ORDER BY namespace",
                params={"org": org, "uid": user_id},
            )
        names = [str(r["namespace"]) for r in rows]
    for ns in names:
        params: dict[str, Any] = {
            "org": org,
            "ns": ns,
            "uid": user_id,
            "n": size,
            "uri": "",
            "id": "",
        }
        while True:
            with sequential_reads():
                rows = await ch.query_all(
                    "SELECT DISTINCT source_uri, toString(doc_id) AS id FROM loom.documents "
                    f"WHERE org = {{org:String}} AND namespace = {{ns:String}}{mine} "
                    # The plain bound is what the key analysis uses (it skips the
                    # granules before it); the tuple orders ids within a source.
                    "AND source_uri >= {uri:String} "
                    "AND (source_uri, id) > ({uri:String}, {id:String}) "
                    "ORDER BY source_uri, id LIMIT {n:UInt32}",
                    params=params,
                )
            page = [(str(r["source_uri"]), str(r["id"])) for r in rows]
            if page:
                yield [(ns, doc_id) for _, doc_id in page]
            if len(page) < size:
                break
            params["uri"], params["id"] = page[-1]


def _evidence_idle_ms() -> int:
    """How long the erasure's transaction may sit idle while one ClickHouse evidence read
    runs: every attempt the client makes of it (`ch_replica_bound_retries` more after the
    first, each up to the read timeout) and 10 s for its reconnects, never below the
    pool's own limit; 0 (none) when reads are unbounded."""
    from server.core.ch import class_timeout_s

    read_s = class_timeout_s("read")
    if read_s <= 0:
        return 0
    attempts = 1 + settings.ch_replica_bound_retries
    return max(int(pg.idle_timeout_ms()), int((read_s * attempts + 10) * 1000))


async def _keep_evidence(conn: Any, org: str, gone: list[tuple[str, str]], quiet_s: float) -> None:
    if not gone:
        return
    await conn.execute(
        "INSERT INTO loom.erased_documents AS e (org, namespace, doc_id, quiet_at) "
        "SELECT $1, ns, id, now() + make_interval(secs => $4) "
        "FROM unnest($2::text[], $3::text[]) AS g(ns, id) "
        "ON CONFLICT (org, namespace, doc_id) DO UPDATE SET erased_at = now(), "
        "quiet_at = GREATEST(e.quiet_at, EXCLUDED.quiet_at)",
        org,
        [ns for ns, _ in gone],
        [doc_id for _, doc_id in gone],
        quiet_s,
    )


async def _lock_knowledge(conn: Any, org: str, namespaces: list[str]) -> None:
    """These namespaces' document lock alone (`pg.KNOWLEDGE_LOCK_KEY`), in the sorted
    order the caller passes: their commits wait for this transaction."""
    for ns in namespaces:
        await conn.execute(f"SELECT pg_advisory_xact_lock({pg.KNOWLEDGE_LOCK_KEY})", org, ns)


async def _lock_user_knowledge(conn: Any, org: str, user_id: str) -> None:
    """Lock every namespace holding the user's documents, reading again once they are
    locked until no namespace more holds one: a commit landed meanwhile is in scope."""
    locked: set[str] = set()
    while True:
        held = {
            r["namespace"]
            for r in await conn.fetch(
                "SELECT namespace FROM loom.knowledge_heads WHERE org = $1 AND user_id = $2 "
                "UNION SELECT namespace FROM loom.knowledge_deliveries "
                "WHERE org = $1 AND user_id = $2",
                org,
                user_id,
            )
        }
        if held <= locked:
            return
        await _lock_knowledge(conn, org, sorted(held - locked))
        locked |= held


async def delete_pg_knowledge_by_namespace(org: str, namespace: str | Sequence[str]) -> int:
    names = [
        n
        for n in ([namespace] if isinstance(namespace, str) else namespace)
        if not pg.unstorable(n)
    ]
    if pg.unstorable(org) or not names:
        return 0
    return await _erase_knowledge(org, names, None)


async def delete_pg_knowledge_by_user(org: str, user_id: str) -> int:
    """The knowledge the user wrote, in any namespace of the org."""
    if pg.unstorable((org, user_id)):
        return 0
    return await _erase_knowledge(org, None, user_id)


async def delete_pg_idempotency(org: str) -> int:
    """The org's replay answers: as loom.idempotency's step, the scope is the org (no
    column names the namespace or the end user). A claim a request in flight holds is
    fenced instead, its token advanced and its lease ended: that request's commit no
    longer matches it and is refused (`pg_memory.IdempotencyFenced`), so no answer
    it built from erased rows is stored, and a retry takes the claim over above it."""
    if not pg.enabled() or pg.unstorable(org):
        return 0
    # Alone under the org's claim lock for the fence only: every claim is fenced here or
    # stamped after `fenced_at` (`idempotency.check_owned`).
    async with pg.transaction() as conn:
        await pg.fence_lock(conn, org)
        await conn.execute(
            "UPDATE loom.idempotency SET lease_token = lease_token + 1, lease_until = now() "
            "WHERE org = $1 AND state = 'running'",
            org,
        )
        fenced_at = await conn.fetchval(
            "INSERT INTO loom.idempotency_fences AS f (org, fenced_at) "
            "VALUES ($1, clock_timestamp()) "
            "ON CONFLICT (org) DO UPDATE SET fenced_at = GREATEST(f.fenced_at, EXCLUDED.fenced_at) "
            "RETURNING fenced_at",
            org,
        )
    # Its own connection, with no statement timeout: an org keeps a day of keys.
    async with pg.dedicated() as conn:
        n = await conn.fetchval(
            "WITH gone AS (DELETE FROM loom.idempotency WHERE org = $1 AND state <> 'running' "
            "AND created_at <= $2 RETURNING 1) SELECT count(*) FROM gone",
            org,
            fenced_at,
        )
    return int(n or 0)


# The census of the Postgres tables that can hold memory content: what a saga
# must leave at zero in its scope, and the step that owns each.
PG_CENSUS: dict[str, str] = {
    "pg:loom.memory_current": "pg_memory.delete",
    "pg:loom.memory_revisions": "pg_memory.delete",
    "pg:loom.idempotency": "idempotency.delete",
    "pg:loom.knowledge_heads": "pg_knowledge.delete",
    "pg:loom.knowledge_deliveries": "pg_knowledge.delete",
}
# The Postgres tables that hold none, with the reason.
PG_NO_MEMORY_CONTENT: dict[str, str] = {
    "loom.authority": "the deployment's recorded authority and epoch; no tenant data.",
    "loom.scope_generations": "a counter per (org, scope); no content.",
    "loom.memory_deliveries": (
        "which targets a revision still owes, by (org, namespace, seq); deleted with "
        "the revision it references (ON DELETE CASCADE)."
    ),
    "loom.pg_migrations": "the migration ledger; no tenant data.",
    "loom.idempotency_fences": (
        "when an erasure last removed an org's replay answers, the census's bound; no content."
    ),
    "loom.erased_memories": (
        "deletion evidence: the namespace and id of each erased memory, when, and when "
        "its publishers are quiet; no content; swept after LOOM_PG_ERASURE_EVIDENCE_HOURS."
    ),
    "loom.erased_documents": (
        "deletion evidence: the namespace and doc_id of each version of an erased knowledge "
        "document, when, and when its publishers are quiet; no content; swept after "
        "LOOM_PG_ERASURE_EVIDENCE_HOURS."
    ),
    "loom.audit_outbox": (
        "audit rows on their way to loom.audit_log, which outlives an erasure: keys and "
        "ids, never values; deleted once published, or parked with their last_error."
    ),
}


# The Postgres tables a namespace's memory or knowledge documents live in, which the
# in-use check reads.
PG_IN_USE_TABLES = (
    "loom.memory_current",
    "loom.memory_revisions",
    "loom.knowledge_heads",
    "loom.knowledge_deliveries",
)


async def pg_holds_namespace(org: str, namespace: str) -> bool:
    """Whether the Postgres authority holds a memory head or revision, or a knowledge
    document head or delivery, of (org, namespace): one committed but not yet delivered
    to ClickHouse is in no census table there."""
    if not pg.enabled() or pg.unstorable(org) or pg.unstorable(namespace):
        return False
    branches = [f"SELECT 1 FROM {t} WHERE org = $1 AND namespace = $2" for t in PG_IN_USE_TABLES]
    row = await pg.fetchrow(" UNION ALL ".join(branches) + " LIMIT 1", org, namespace)
    return row is not None


async def pg_residue(org: str, scope_type: str, scope_value: str | Sequence[str]) -> dict[str, int]:
    """Rows still in scope per Postgres substrate; no substrate without the Postgres
    authority."""
    if not pg.enabled():
        return {}
    arg: Any
    if scope_type == "namespace":
        names = [scope_value] if isinstance(scope_value, str) else scope_value
        arg = [n for n in names if not pg.unstorable(n)]
        pred = "namespace = ANY($2::text[])"
        held = bool(arg)
    else:
        pred, arg = "user_id = $2", scope_value
        held = not pg.unstorable(scope_value)
    if pg.unstorable(org) or not held:
        return dict.fromkeys(PG_CENSUS, 0)  # no row holds what Postgres cannot store
    out: dict[str, int] = {}
    # Its own connection, with no statement timeout: the evidence of a large erasure
    # takes as long to count back as the erasure took to write.
    # A memory the erasure removed under another identity (a user's consolidated
    # dependents) cannot come back while its evidence is kept: a commit of the id into its
    # namespace is refused (`pg_memory.MemoryErased`), so the scope's own rows are the
    # residue.
    async with pg.dedicated() as conn:
        for table in PG_IN_USE_TABLES:
            in_scope = await conn.fetchval(
                f"SELECT count(*) FROM {table} WHERE org = $1 AND {pred}", org, arg
            )
            out["pg:" + table] = int(in_scope or 0)
        out["pg:loom.idempotency"] = int(
            await conn.fetchval(
                # Answers claimed before the step last ran (a takeover restarts
                # `created_at`); every answer when it never ran.
                "SELECT count(*) FROM loom.idempotency WHERE org = $1 AND state <> 'running' "
                "AND created_at <= COALESCE("
                "(SELECT fenced_at FROM loom.idempotency_fences WHERE org = $1), 'infinity')",
                org,
            )
            or 0
        )
    return out
