"""Pure statement builders and worker passes outside the search path:
reconcile / sweep / usage-fold workers, the recall builder, the
integrity and census constants, the response cache, the admin readers."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from tests.analyzer.cases._common import NS, ORG, rows_by_substring
from tests.analyzer.corpus import Case, RecordingCH


async def _pure_builders(ch: RecordingCH, c: dict[str, Any]) -> None:
    from server.identity.app.traffic_class_registry import REGISTRY_CLASS_SQL
    from server.identity.store import namespace_census as nc
    from server.insights.app.ops import _read_schema_sql
    from server.insights.store.admin import budget_violations_sql
    from server.llm.cache import read_sql
    from server.memory.app import chunk_links as cl
    from server.memory.app import sensitivity_sweep as sw
    from server.memory.app.reinforcement import reinforcement_lookup_sql
    from server.memory.store import integrity as ir
    from server.memory.store import sensitivity_sweep_sql as sweep_sql
    from server.memory.store.sweeps import entity_vectors_live_sql
    from server.memory.workers.orphan_vector_reconcile import orphan_identities_sql
    from server.memory.workers.usage_fold import ACTED_STATEMENT
    from server.observability import grep_replay_probe as gp

    scoped = c["scoped"]
    # a bound on no column, on one key, and past (or up to the end of) one agent
    bounds = ((), sweep_sql.SORT_KEY, sweep_sql.AGENT_KEY)
    for sql in (
        read_sql(),
        _read_schema_sql(),
        ir.integrity_counts_sql(),
        ir.DIM0_WITH_VECTOR_SQL,
        ir.TOMBSTONE_STATUS_DRIFT_SQL,
        ir.ACTIVE_PAST_VALID_TO_SQL,
        ir.HIGH_WITH_EMBEDDING_SQL,
        nc.UNREGISTERED_LIVE_SQL,
        nc.BENCH_GC_CANDIDATES_SQL,
        budget_violations_sql("AND org = {org:String}" if scoped else ""),
        cl.KEYS_SQL,
        *(
            build(
                org=ORG if scoped else None,
                namespace=NS if scoped else None,
                since_days=7 if scoped else None,
                after=after,
            )
            for build in (sweep_sql.keys_sql, sweep_sql.scan_sql)
            for after in bounds
        ),
        *(  # a pass that went back to the first key, or rereads an agent it moved past
            sweep_sql.keys_sql(
                org=ORG if scoped else None,
                namespace=NS if scoped else None,
                since_days=7 if scoped else None,
                after=after,
                stop=stop,
            )
            for after in bounds
            for stop in bounds[1:]
        ),
        sw.HIGH_WITH_EMBEDDING_SQL,
        reinforcement_lookup_sql(),
        REGISTRY_CLASS_SQL,
        entity_vectors_live_sql(),
        orphan_identities_sql(),
        ACTED_STATEMENT,
        gp.LEDGER_SQL,
        gp.RECOUNT_SQL,
    ):
        await ch.query_all(sql, {})


async def _recall(ch: RecordingCH, c: dict[str, Any]) -> None:
    from server.memory.domain.recall import RecallFilter, build_count_sql, build_recall_sql

    f = RecallFilter(
        tokens=["deploy", "window"],
        match="all" if c["filters"] else "any",
        since=datetime(2026, 1, 1, tzinfo=UTC) if c["filters"] else None,
        until=datetime(2026, 2, 1, tzinfo=UTC) if c["filters"] else None,
        role="user" if c["filters"] else "",
        agent="bot" if c["filters"] else "*",
        user_id="alice" if c["user_id"] else None,
        session_id="s1" if c["user_id"] else None,
    )
    scope = {"org": ORG, "ns": NS}
    sql, params = build_recall_sql(
        f,
        order=c["order"],
        cursor=(datetime(2026, 1, 15, tzinfo=UTC), "k1") if c["cursor"] else None,
        offset=0 if c["cursor"] else 10,
        limit=20,
    )
    await ch.query_all(sql, {**scope, **params})
    sql, params = build_count_sql(f)
    await ch.query_one(sql, {**scope, **params})


async def _worker_passes(ch: RecordingCH, c: dict[str, Any]) -> None:
    from server.insights.store import admin as admin_repo
    from server.memory.app import sensitivity_sweep as sw
    from server.memory.store import integrity as ir
    from server.memory.workers import orphan_vector_reconcile as ovr

    org = ORG if c["scoped"] else None
    await ovr.run_pass(ch=ch)
    await sw.scan(
        org=org,
        namespace=NS if c["scoped"] else None,
        since_days=7 if c["scoped"] else None,
        limit=10,
        page_size=10,
        pages=1,
        ch=ch,
    )
    await ir.integrity_counts(ch=ch)
    await admin_repo.list_loom_tables(ch=ch)
    await admin_repo.budget_violations(3600, 50, org=org, ch=ch)
    # The worker_status reads the admin handler and the
    # supervisor make (latest row per key, the resume cursor).
    from server.workers import status_store

    await status_store.latest_rows(ch=ch)
    await status_store.resume_cursor("embedding_worker", ch=ch)
    # The cost-cap verdict for a pass's namespaces in one statement.
    from server.observability import budget

    await budget.namespaces_over_cap([(ORG, NS), (ORG, "n2")], ch=ch)
    # The usage fold's lightweight UPDATE of loom.memory's counters.
    from server.memory.workers import usage_fold as uf

    now = datetime(2026, 1, 1, tzinfo=UTC)
    target = uf._Target(
        memory_id="00000000-0000-0000-0000-000000000001",
        org=ORG,
        namespace=NS,
        agent="a",
        user_id="-",
        memory_key="k1",
        evidence=1,
        recall=1,
        last_recalled=now,
        reinforcement=None,
        last_activity=now,
    )
    await uf._fold_memory_rows(ch, [target], 10)


async def _admin_readers(ch: RecordingCH, c: dict[str, Any]) -> None:
    import inspect

    from server.insights.store import admin as admin_repo

    org = ORG if c["scoped"] else None
    for name, fn in inspect.getmembers(admin_repo, inspect.iscoroutinefunction):
        if name.startswith("_") or name in ("list_loom_tables", "budget_violations"):
            continue
        sig = inspect.signature(fn)
        kwargs: dict[str, Any] = {}
        args: list[Any] = []
        for pname, p in sig.parameters.items():
            if pname == "ch":
                kwargs["ch"] = ch
                continue
            if p.default is not inspect.Parameter.empty:
                if pname in ("org", "namespace") and org is not None:
                    kwargs[pname] = org if pname == "org" else NS
                continue
            value: Any
            if pname == "group_by":
                value = "agent"
            elif pname == "org":
                value = ORG
            elif pname in ("namespace", "ns"):
                value = NS
            elif p.annotation in ("int", int):
                value = 10
            elif p.annotation in ("float", float):
                value = 1.0
            elif p.annotation in ("bool", bool):
                value = c["scoped"]
            elif "list" in str(p.annotation):
                value = ["x"]
            elif "datetime" in str(p.annotation):
                value = datetime(2026, 1, 1, tzinfo=UTC)
            else:
                value = "x"
            if p.kind is inspect.Parameter.KEYWORD_ONLY:
                kwargs[pname] = value
            else:
                args.append(value)
        try:
            await fn(*args, **kwargs)
        except Exception:  # noqa: BLE001 — a reader whose stub answer does not fit still rendered its statement
            continue


_ADMIN_ROWS = rows_by_substring(
    {"system.columns": [{"name": "consumer", "n": 1}], "count()": [{"n": 1}]}
)


async def _first_pass(ch: RecordingCH, c: dict[str, Any]) -> None:
    """The empty-table first-pass read, one statement per
    owned table, for every table a worker declares today."""
    from server.tracker.workers.links import OWNED_TABLES as _links_tables
    from server.tracker.workers.test_daily import OWNED_TABLES as _daily_tables
    from server.workers import first_pass as fp

    for owned in (*_links_tables, *_daily_tables, fp.OwnedTable("loom.code_symbol_stats")):
        await fp.held_namespaces(owned, [(ORG, NS)], ch=ch)


async def _profile_fold(ch: RecordingCH, c: dict[str, Any]) -> None:
    """The profile fold's scan, with an identity held (its NOT IN) and without."""
    from server.memory.workers import profile_consolidation as pc
    from server.workers import holds

    if c["held"]:
        pc._HELD.hold([(ORG, NS, "alice")], 600)
    try:
        await pc.run_pass(pc.ProfileConsolidationConfig(max_identities_per_pass=2))
    finally:
        holds._reset_for_tests()


async def _observe_extract(ch: RecordingCH, c: dict[str, Any]) -> None:
    """A session's READY observations and the whole-session context the notes are
    written from (a buffer trigger only binds a parameter)."""
    from server.memory.workers import observe_extract as ox

    session = {
        "org": ORG,
        "namespace": NS,
        "agent": "bot",
        "user_id": "alice",
        "session_id": "s1",
    }
    await ox._load_session_observations(session, ox.ObserveExtractConfig(), ch)
    await ox._load_session_context(session, ch=ch)


async def _resolver_cluster(ch: RecordingCH, c: dict[str, Any]) -> None:
    """A queue row's cluster: its nearest rows, its key's other sessions, their rows."""
    from server.memory.store import conflict_resolver as queue

    row = {
        "org": ORG,
        "namespace": NS,
        "agent": "bot",
        "user_id": "alice",
        "session_id": "s1",
        "memory_key": "office_city",
        "memory_type": "semantic",
        "embedding": [0.1, 0.2, 0.3],
        "embedding_model": "fake",
    }
    await queue.cluster_peers(row, floor=0.5, limit=4, ch=ch)


# A candidate row for each of the cluster's reads, so the peer-row read runs.
_CLUSTER_ROWS = rows_by_substring(
    {
        "dist": [
            {"agent": "bot", "user_id": "alice", "session_id": "s2", "memory_key": "k", "dist": 0.1}
        ]
    }
)


async def _period_sources(ch: RecordingCH, c: dict[str, Any]) -> None:
    """The facts a period's digests compressed: their ids and entities."""
    from types import SimpleNamespace

    from server.memory.app import period_digest

    period = SimpleNamespace(
        org=ORG, namespace=NS, agent="bot", user_id="alice", digests=[{"derived_from": ["k"]}]
    )
    await period_digest._source_memories(ch, period)


async def _period_keys(ch: RecordingCH, c: dict[str, Any]) -> None:
    """The period keys a pass skips, a deleted one included."""
    from server.memory.app import period_digest

    ident = {"org": ORG, "namespace": NS, "agent": "bot", "user_id": "alice"}
    await period_digest._skipped_period_keys(ch, ident, rewrite=False)


async def _conflict_resolver(ch: RecordingCH, c: dict[str, Any]) -> None:
    from server.memory.store import conflict_resolver as queue

    held = [(ORG, NS, "bot", "alice", "s1", "fact")] if c["held"] else []
    await queue.owed_rows(limit=10, held=held, ch=ch)
    await queue.unjudged_conflicted_rows(limit=10, held=held, ch=ch)


async def _audit_publish(ch: RecordingCH, c: dict[str, Any]) -> None:
    """The delivery pass's retried audit insert: the read of what already landed."""
    from server.memory.store import pg_publish

    ts = datetime(2026, 1, 1, tzinfo=UTC)
    row = {"org": ORG, "namespace": NS, "ts": ts, "row_hash": "a" * 64}
    await pg_publish.insert_audit_rows([row], ch=ch, retried=True)


async def _authority_record(ch: RecordingCH, c: dict[str, Any]) -> None:
    """The boot's read of the recorded authority, and of whether ClickHouse holds memory."""
    from server.core import pg_profile

    await pg_profile._ch_record(ch)
    await pg_profile._ch_holds_memory(ch)


async def _assistant_service(ch: RecordingCH, c: dict[str, Any]) -> None:
    """The boot's read of the assistant's service row."""
    from server.assistant.store import service

    await service.read(ch=ch)


async def _answer_digests(ch: RecordingCH, c: dict[str, Any]) -> None:
    """The assistant's read of the statements its answer's insights.query calls ran."""
    from server.assistant.store import ledger

    await ledger.query_digests(ORG, NS, "chatcmpl-1", "alice", ch=ch)


async def _answer_keys(ch: RecordingCH, c: dict[str, Any]) -> None:
    """The assistant's read of the memory keys its answer's tool calls were returned."""
    from server.assistant.store import ledger

    await ledger.returned_keys(ORG, NS, "chatcmpl-1", "alice", ["policy/deploy/freeze"], ch=ch)


async def _held_documents(ch: RecordingCH, c: dict[str, Any]) -> None:
    """The delivery pass's read of the search documents ClickHouse already holds."""
    from server.memory.store import pg_publish

    await pg_publish.held_documents(ORG, NS, [("a", "-", "-", "k1", 7)], ch=ch)


async def _vector_retirement(ch: RecordingCH, c: dict[str, Any]) -> None:
    """The keyed reads of a key's live vectors before an unembedded row retires them: the
    whole rows a copy needs, or only the sort key a bare tombstone keeps."""
    from server.memory.store.embeddings import retire_vectors_for_keys

    await retire_vectors_for_keys([((ORG, NS, "a", "-", "-", "k1"), 7)], ch=ch, bare=c["bare"])


async def _decision_records(ch: RecordingCH, c: dict[str, Any]) -> None:
    """A decision's record retired with its writer's row, and the record a live writer
    holds; under Postgres, against the live writers the authority holds (one, and none:
    an empty typed array) and by the server's send deadline."""
    from server.core import rmt_carry
    from server.memory.store import pg_memory, records

    async def _deadline(*_: Any) -> float:
        return 1_700_000_000.0

    real, deadline = pg_memory.live_decision_writers, rmt_carry.measured_send_deadline
    rmt_carry.measured_send_deadline = _deadline  # type: ignore[assignment]
    try:
        for live in ([("k1", "a", "-", "-")], []):

            async def _writers(*_: Any, live: list[Any] = live) -> list[Any]:
                return live

            pg_memory.live_decision_writers = _writers  # type: ignore[assignment]
            await records.tombstone_decision_records(ORG, NS, ["k1"], version=7, ch=ch)
            await records.live_writer_record(ORG, NS, "k1", ch=ch)
    finally:
        pg_memory.live_decision_writers = real  # type: ignore[assignment]
        rmt_carry.measured_send_deadline = deadline  # type: ignore[assignment]


def cases() -> list[Case]:
    return [
        Case(
            "workers.decision_records",
            "server.memory.store.records",
            _decision_records,
            {"storage_authority": ("clickhouse", "postgres")},
        ),
        Case("workers.audit_publish", "server.memory.store.pg_publish", _audit_publish, {}),
        Case("workers.held_documents", "server.memory.store.pg_publish", _held_documents, {}),
        Case(
            "workers.vector_retirement",
            "server.memory.store.embeddings",
            _vector_retirement,
            {"bare": (False, True)},
        ),
        Case("boot.authority_record", "server.core.pg_profile", _authority_record, {}),
        Case("boot.assistant_service", "server.assistant.store.service", _assistant_service, {}),
        Case("assistant.answer_digests", "server.assistant.store.ledger", _answer_digests, {}),
        Case("assistant.answer_keys", "server.assistant.store.ledger", _answer_keys, {}),
        Case(
            "workers.conflict_resolver",
            "server.memory.store.conflict_resolver",
            _conflict_resolver,
            {"held": (False, True)},
        ),
        Case("workers.first_pass", "server.workers.first_pass", _first_pass, {}),
        Case(
            "workers.resolver_cluster",
            "server.memory.store.conflict_resolver",
            _resolver_cluster,
            {},
            rows=_CLUSTER_ROWS,
        ),
        Case("workers.period_sources", "server.memory.app.period_digest", _period_sources, {}),
        Case("workers.period_keys", "server.memory.app.period_digest", _period_keys, {}),
        Case(
            "workers.observe_extract",
            "server.memory.workers.observe_extract",
            _observe_extract,
            {},
        ),
        Case(
            "workers.profile_fold",
            "server.memory.workers.profile_consolidation",
            _profile_fold,
            {"held": (False, True)},
        ),
        Case(
            "builders.pure",
            "server.memory.store.integrity",
            _pure_builders,
            {"scoped": (False, True)},
        ),
        Case(
            "memory_recall.builder",
            "server.memory.domain.recall",
            _recall,
            {
                "user_id": (False, True),
                "filters": (False, True),
                "order": ("recent", "relevance"),
                "cursor": (False, True),
            },
        ),
        Case(
            "workers.passes",
            "server.memory.workers.orphan_vector_reconcile",
            _worker_passes,
            {"scoped": (False, True)},
        ),
        Case(
            "admin_repo.readers",
            "server.insights.store.admin",
            _admin_readers,
            {"scoped": (False, True)},
            rows=_ADMIN_ROWS,
        ),
    ]
