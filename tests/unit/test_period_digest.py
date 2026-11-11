"""Period digests: one derived semantic memory over K
consecutive session digests of one identity — the scale between a session
and the namespace. The plan is pure and idempotent; the write goes through
the canonical row builders and links `derived_from` to the memories the
digests compressed."""

from __future__ import annotations

import uuid
from dataclasses import replace
from datetime import datetime
from typing import Any

import pytest

from server.core import pg_lease
from server.memory.app.period_digest import (
    PERIOD_DIGEST_PROMPT_VERSION,
    PERIOD_KEY_PREFIX,
    PERIOD_TAG,
    Period,
    PeriodDigestConfig,
    build_period_memory_rows,
    build_period_prompt,
    period_key,
    plan_periods,
    run_period_digest,
)


def _digest(n: int, agent: str = "a") -> dict[str, Any]:
    return {
        "org": "o",
        "namespace": "ns",
        "agent": agent,
        "user_id": "-",
        "session_id": f"ns:session_{n}",
        "summary_text": f"On 2023-07-{n:02d}, something happened in session {n}.",
        "derived_from": [f"fact_{n}_a", f"fact_{n}_b"],
        "at": datetime(2023, 7, n),
    }


def _digests(first: int, last: int) -> list[dict[str, Any]]:
    return [_digest(n) for n in range(first, last + 1)]


def _period(first: int, last: int) -> Period:
    return Period("o", "ns", "a", "-", tuple(_digests(first, last)))


_KEY_1_5 = f"{PERIOD_KEY_PREFIX}session_1_session_5"
_KEY_6_10 = f"{PERIOD_KEY_PREFIX}session_6_session_10"
_KEY_11_15 = f"{PERIOD_KEY_PREFIX}session_11_session_15"
_CFG = PeriodDigestConfig(sessions_per_period=5, namespace_filter="ns", require_opt_in=False)


def _rows_for(
    period: Period, version: int = 1, entities: list[str] | None = None
) -> tuple[dict[str, Any], dict[str, Any]]:
    return build_period_memory_rows(
        period,
        text="t",
        embedding=[0.1],
        embedding_model="m",
        embedding_dim=1,
        version=version,
        entities=entities,
    )


def test_period_key_uses_session_tails() -> None:
    assert period_key("ns:session_1", "ns:session_5") == f"{PERIOD_KEY_PREFIX}session_1_session_5"
    assert period_key("", "") == f"{PERIOD_KEY_PREFIX}s_s"


def test_plan_cuts_complete_windows_in_time_order_and_skips_existing() -> None:
    # 12 digests, out of order, one identity: K=5 gives two complete windows and a
    # trailing pair that waits for a later pass.
    digests = [_digest(n) for n in (3, 1, 2, 5, 4, 7, 6, 9, 8, 10, 11, 12)]
    periods, skipped = plan_periods(digests, sessions_per_period=5)
    assert skipped == 0
    assert [p.memory_key for p in periods] == [_KEY_1_5, _KEY_6_10]
    assert [d["session_id"] for d in periods[0].digests] == [f"ns:session_{n}" for n in range(1, 6)]
    # a window already written is counted, not re-planned
    periods2, skipped2 = plan_periods(digests, sessions_per_period=5, skip_keys={_KEY_1_5})
    assert skipped2 == 1 and [p.memory_key for p in periods2] == [_KEY_6_10]


def test_plan_keeps_identities_apart_and_needs_k_sessions() -> None:
    digests = _digests(1, 5) + [_digest(n, agent="b") for n in range(1, 4)]
    periods, _ = plan_periods(digests, sessions_per_period=5)
    assert len(periods) == 1 and periods[0].agent == "a"


def test_prompt_dates_every_digest_and_states_the_span() -> None:
    period = _period(1, 3)
    system, user = build_period_prompt(period)
    assert "never add, shift or sharpen a date" in system
    assert "Period: 2023-07-01 → 2023-07-03" in user
    assert "[2023-07-02] On 2023-07-02" in user


def test_rows_are_derived_semantic_memories_anchored_at_the_first_session() -> None:
    period = _period(1, 5)
    memory_row, embedding_row = build_period_memory_rows(
        period,
        text="Between 2023-07-01 and 2023-07-05, things happened.",
        embedding=[0.1, 0.2],
        embedding_model="m",
        embedding_dim=2,
        version=7,
    )
    assert memory_row["memory_key"] == _KEY_1_5
    # semantic, not episodic: episodic is the non-searchable type (memory_types.py)
    assert memory_row["memory_type"] == "semantic" and embedding_row["memory_type"] == "semantic"
    assert memory_row["source_class"] == "derived"
    assert PERIOD_DIGEST_PROMPT_VERSION in str(memory_row["source_trace"])
    assert PERIOD_TAG in memory_row["tags"] and "src:summary:ns:session_3" in memory_row["tags"]
    assert memory_row["valid_at"] == datetime(2023, 7, 1)
    assert embedding_row["memory_key"] == memory_row["memory_key"]


def test_a_period_digest_row_carries_its_own_memory_id() -> None:
    # A blank memory_id stored as the zero UUID would make every digest share
    # one id, so their derived_from links would point at no row.
    first = _period(1, 5)
    second = _period(6, 10)
    assert first.memory_key != second.memory_key
    ids = {uuid.UUID(str(_rows_for(p)[0]["memory_id"])) for p in (first, second)}
    assert len(ids) == 2 and all(i.int != 0 for i in ids)


def test_rewriting_a_period_keeps_its_memory_id() -> None:
    period = _period(1, 5)
    assert _rows_for(period, 1)[0]["memory_id"] == _rows_for(period, 2)[0]["memory_id"]


def test_the_same_period_key_in_another_namespace_gets_another_id() -> None:
    ids = {_rows_for(replace(_period(1, 5), namespace=ns))[0]["memory_id"] for ns in ("ns", "ns2")}
    assert len(ids) == 2


class _FakeCH:
    def __init__(self, digests: list[dict[str, Any]], existing: set[str] | None = None) -> None:
        self._digests = digests
        self._existing = existing or set()
        self.inserts: list[tuple[str, list[dict[str, Any]]]] = []
        self.link_calls: list[dict[str, Any]] = []

    async def query_all(self, sql: str, params: Any = None) -> list[dict[str, Any]]:
        if "uniqExact(s.session_id)" in sql:
            sessions = len({d["session_id"] for d in self._digests})
            return [
                {"org": "o", "namespace": "ns", "agent": "a", "user_id": "-", "sessions": sessions}
            ]
        if "FROM loom.session_summaries FINAL" in sql:
            return list(self._digests)
        if "startsWith(memory_key" in sql:
            return [{"memory_key": k, "taken_out": False} for k in self._existing]
        if "memory_key IN" in sql:
            return [
                {
                    "memory_id": f"id-{k}",
                    "org": "o",
                    "namespace": "ns",
                    "agent": "a",
                    "user_id": "-",
                    "session_id": "-",
                    "memory_key": k,
                    "version": 1,
                    "redacted_at": None,
                    "content_sha256": f"sha-{k}",
                    "entities": ["Caroline", k.split("_")[0]],
                }
                for k in params["keys"]
            ]
        return []

    async def query_one(self, sql: str, params: Any = None) -> dict[str, Any] | None:
        return None

    async def insert(self, table: str, rows: list[dict[str, Any]], settings: Any = None) -> None:
        self.inserts.append((table, rows))


class _FakeLLM:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    async def complete(self, system: str, user: str, max_tokens: int = 0, **_: Any) -> Any:
        self.calls.append((system, user))

        class _Out:
            text = "Between 2023-07-01 and 2023-07-05, Caroline joined a group and Melanie painted."
            tokens_in = 300
            tokens_out = 40

        return _Out()


class _FakeEmbedder:
    model_name = "fake"
    dim = 3

    async def embed(self, text: str) -> list[float]:
        return [0.1, 0.2, 0.3]


def _written_keys(ch: _FakeCH) -> list[str]:
    return [r["memory_key"] for t, rows in ch.inserts if t == "loom.memory" for r in rows]


async def _noop(**_: Any) -> None: ...


@pytest.mark.asyncio
async def test_run_writes_one_period_with_lineage_and_is_idempotent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    linked: list[dict[str, Any]] = []

    async def _fake_link(**kwargs: Any) -> None:
        linked.append(kwargs)

    monkeypatch.setattr("server.memory.app.evidence.link_belief_sources", _fake_link)
    ch = _FakeCH(_digests(1, 5))
    llm = _FakeLLM()
    res = await run_period_digest(_CFG, ch=ch, llm=llm, embedder=_FakeEmbedder())
    assert res.periods_written == 1 and res.digests_covered == 5 and not res.failures
    assert [t for t, _ in ch.inserts] == ["loom.memory", "loom.memory_embeddings"]
    assert linked and linked[0]["support_type"] == "derived_from"
    # the row carries the compressed facts' entities, lower-cased and deduped
    memory_row = ch.inserts[0][1][0]
    assert memory_row["entities"] == ["caroline", "fact"]
    assert ch.inserts[1][1][0]["entities"] == ["caroline", "fact"]
    assert sorted(linked[0]["evidence_ids"]) == sorted(
        f"id-fact_{n}_{s}" for n in range(1, 6) for s in "ab"
    )
    assert len(llm.calls) == 1 and "Sessions: 5" in llm.calls[0][1]
    # second pass: the key exists, nothing is written
    ch2 = _FakeCH(_digests(1, 5), existing={_KEY_1_5})
    res2 = await run_period_digest(_CFG, ch=ch2, llm=llm, embedder=_FakeEmbedder())
    assert res2.periods_written == 0 and res2.periods_skipped == 1 and ch2.inserts == []


@pytest.mark.asyncio
async def test_a_failed_authority_read_writes_nothing_and_a_later_pass_writes_it_whole(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.core import pg
    from server.memory.app import period_digest
    from server.memory.store import pg_memory

    linked: list[dict[str, Any]] = []

    async def _link(**kwargs: Any) -> None:
        linked.append(kwargs)

    async def _down(keys: Any, **_: Any) -> Any:
        raise pg.PostgresUnavailableError("Postgres did not answer")

    monkeypatch.setattr("server.memory.app.evidence.link_belief_sources", _link)
    monkeypatch.setattr(pg, "enabled", lambda: True)
    monkeypatch.setattr(pg_memory, "heads_by_keys", _down)
    ch = _FakeCH(_digests(1, 5))
    llm = _FakeLLM()
    res = await run_period_digest(_CFG, ch=ch, llm=llm, embedder=_FakeEmbedder())
    assert res.periods_written == 0 and ch.inserts == [] and llm.calls == []
    # the authority is read at planning, so the identity's pass fails before any period
    assert res.failures == ["o/ns: PostgresUnavailableError status 503"]

    async def _heads(keys: Any, **_: Any) -> Any:  # the sources, not yet the period
        return {k: {"version": 1, "redacted_at": None} for k in keys if "fact_" in k[-1]}

    async def _commit(rows: Any, documents: Any, *, ch: Any) -> bool:
        ch.inserts.append(("loom.memory", rows))  # the authority commit, recorded
        return True

    monkeypatch.setattr(pg_memory, "heads_by_keys", _heads)
    monkeypatch.setattr(period_digest, "insert_documented_versions", _commit)
    res = await run_period_digest(_CFG, ch=ch, llm=llm, embedder=_FakeEmbedder())
    assert res.periods_written == 1 and len(llm.calls) == 1
    assert ch.inserts[0][1][0]["entities"] == ["caroline", "fact"]
    assert sorted(linked[0]["evidence_ids"]) == sorted(
        f"id-fact_{n}_{s}" for n in range(1, 6) for s in "ab"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("change", "lends"),
    [
        ({}, True),
        ({"memory_id": "another"}, False),
        ({"content_sha256": "another"}, False),
        ({"status": "superseded"}, False),
        ({"sensitivity": "high"}, False),
    ],
    ids=["copy", "another-memory", "rewritten", "superseded", "high"],
)
async def test_a_source_changed_since_its_read_lends_its_entities_only_as_a_copy(
    monkeypatch: pytest.MonkeyPatch, change: dict[str, Any], lends: bool
) -> None:
    from server.core import pg
    from server.memory.app import period_digest
    from server.memory.store import pg_memory

    async def _heads(keys: Any, **_: Any) -> Any:  # every source has a newer head
        return {
            k: {
                "memory_id": f"id-{k[-1]}",
                "version": 2,
                "redacted_at": None,
                "content_sha256": f"sha-{k[-1]}",
                "status": "active",
                "sensitivity": "normal",
                **change,
            }
            for k in keys
            if "fact_" in k[-1]
        }

    monkeypatch.setattr(pg, "enabled", lambda: True)
    monkeypatch.setattr(pg_memory, "heads_by_keys", _heads)
    ids, ents = await period_digest._source_memories(_FakeCH([]), _period(1, 2))
    assert sorted(ids) == sorted(f"id-fact_{n}_{s}" for n in (1, 2) for s in "ab")
    assert ents == (["caroline", "fact"] if lends else [])


@pytest.mark.asyncio
async def test_a_failed_source_read_fails_the_period_before_its_model_call() -> None:
    class _SourcesDown(_FakeCH):
        async def query_all(self, sql: str, params: Any = None) -> list[dict[str, Any]]:
            if "memory_key IN" in sql:
                raise ConnectionError("clickhouse unreachable")
            return await super().query_all(sql, params)

    ch = _SourcesDown(_digests(1, 5))
    llm = _FakeLLM()
    res = await run_period_digest(_CFG, ch=ch, llm=llm, embedder=_FakeEmbedder())
    assert res.periods_written == 0 and ch.inserts == [] and llm.calls == []
    assert res.failures == [f"sources {_KEY_1_5}: ConnectionError"]


@pytest.mark.asyncio
async def test_a_failed_lineage_write_is_recorded_and_the_row_stands(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _link_down(**_: Any) -> None:
        raise ConnectionError("clickhouse unreachable")

    monkeypatch.setattr("server.memory.app.evidence.link_belief_sources", _link_down)
    ch = _FakeCH(_digests(1, 5))
    res = await run_period_digest(_CFG, ch=ch, llm=_FakeLLM(), embedder=_FakeEmbedder())
    assert res.periods_written == 1
    assert [t for t, _ in ch.inserts] == ["loom.memory", "loom.memory_embeddings"]
    assert res.failures == [f"links {_KEY_1_5}: ConnectionError"]


@pytest.mark.asyncio
async def test_a_failed_embedding_fails_only_its_period() -> None:
    class _FirstEmbedDown(_FakeEmbedder):
        calls = 0

        async def embed(self, text: str) -> list[float]:
            self.calls += 1
            if self.calls == 1:
                raise ConnectionError("embedder unreachable")
            return await super().embed(text)

    ch = _FakeCH(_digests(1, 10))
    res = await run_period_digest(_CFG, ch=ch, llm=_FakeLLM(), embedder=_FirstEmbedDown())
    assert res.periods_written == 1
    assert res.failures == [f"embed {_KEY_1_5}: ConnectionError"]
    assert _written_keys(ch) == [_KEY_6_10]


@pytest.mark.asyncio
async def test_a_failed_writer_lookup_fails_only_its_period_before_its_model_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.memory.app import period_digest

    real = period_digest._period_writer

    async def _first_down(ch: Any, period: Any) -> Any:
        if period.memory_key == _KEY_1_5:
            raise ConnectionError("clickhouse unreachable")
        return await real(ch, period)

    monkeypatch.setattr(period_digest, "_period_writer", _first_down)
    ch = _FakeCH(_digests(1, 10))
    llm = _FakeLLM()
    res = await run_period_digest(_CFG, ch=ch, llm=llm, embedder=_FakeEmbedder())
    assert res.periods_written == 1 and len(llm.calls) == 1
    assert res.failures == [f"writer {_KEY_1_5}: ConnectionError"]
    assert _written_keys(ch) == [_KEY_6_10]


@pytest.mark.asyncio
async def test_a_failed_embedding_insert_on_clickhouse_keeps_the_row_and_its_lineage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    linked: list[dict[str, Any]] = []

    async def _link(**kwargs: Any) -> None:
        linked.append(kwargs)

    class _VectorsDown(_FakeCH):
        async def insert(self, table: str, rows: Any, settings: Any = None) -> None:
            if table == "loom.memory_embeddings":
                raise ConnectionError("clickhouse unreachable")
            await super().insert(table, rows, settings)

    monkeypatch.setattr("server.memory.app.evidence.link_belief_sources", _link)
    ch = _VectorsDown(_digests(1, 5))
    res = await run_period_digest(_CFG, ch=ch, llm=_FakeLLM(), embedder=_FakeEmbedder())
    # the row stands without its vector; the embedding worker stubs and embeds it
    assert res.periods_written == 1 and not res.failures
    assert [t for t, _ in ch.inserts] == ["loom.memory"] and len(linked) == 1


@pytest.mark.asyncio
async def test_a_pending_publication_writes_no_lineage_and_is_recorded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.memory.app import period_digest

    linked: list[dict[str, Any]] = []

    async def _link(**kwargs: Any) -> None:
        linked.append(kwargs)

    async def _pending(rows: Any, documents: Any, *, ch: Any) -> bool:
        return False

    monkeypatch.setattr("server.memory.app.evidence.link_belief_sources", _link)
    monkeypatch.setattr(period_digest, "insert_documented_versions", _pending)
    ch = _FakeCH(_digests(1, 5))
    res = await run_period_digest(_CFG, ch=ch, llm=_FakeLLM(), embedder=_FakeEmbedder())
    assert res.periods_written == 1 and linked == []
    assert res.failures == [f"links {_KEY_1_5}: publication pending"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("head", "rewrite", "skipped"),
    [
        ({"status": "active", "tombstoned_at": None}, False, True),
        ({"status": "active", "tombstoned_at": "2026-01-01 00:00:00"}, False, True),
        ({"status": "active", "tombstoned_at": "2026-01-01 00:00:00"}, True, True),
        ({"status": "rejected", "tombstoned_at": None}, True, True),
        (
            {"status": "active", "tombstoned_at": None, "redacted_at": "2026-01-01 00:00:00"},
            True,
            True,
        ),
        ({"status": "active", "tombstoned_at": None}, True, False),
        (None, False, False),
    ],
    ids=[
        "committed",
        "deleted",
        "deleted-rewrite",
        "rejected-rewrite",
        "redacted-rewrite",
        "live-rewrite",
        "none",
    ],
)
async def test_under_postgres_the_authority_decides_which_periods_are_written(
    monkeypatch: pytest.MonkeyPatch, head: dict[str, Any] | None, rewrite: bool, skipped: bool
) -> None:
    from server.core import pg
    from server.memory.store import pg_memory

    async def _heads(keys: Any, *, include_tombstoned: bool = False) -> Any:
        assert include_tombstoned
        return {} if head is None else {k: head for k in keys}

    class _NotRead(_FakeCH):  # ClickHouse holds the key live; under Postgres it is not read
        async def query_all(self, sql: str, params: Any = None) -> list[dict[str, Any]]:
            assert "startsWith(memory_key" not in sql, "ClickHouse's period keys were read"
            return await super().query_all(sql, params)

    monkeypatch.setattr(pg, "enabled", lambda: True)
    monkeypatch.setattr(pg_memory, "heads_by_keys", _heads)
    cfg = replace(_CFG, rewrite_existing=rewrite)
    ch = _NotRead(_digests(1, 5), existing={_KEY_1_5})
    res = await run_period_digest(cfg, ch=ch, dry_run=True)
    assert not res.failures
    assert (res.periods_planned, res.periods_skipped) == ((0, 1) if skipped else (1, 0))


@pytest.mark.asyncio
async def test_a_period_the_authority_holds_takes_no_place_in_the_cap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.core import pg
    from server.memory.store import pg_memory

    async def _heads(keys: Any, **_: Any) -> Any:  # the first window committed, not yet published
        return {k: {"status": "active", "tombstoned_at": None} for k in keys if k[-1] == _KEY_1_5}

    monkeypatch.setattr(pg, "enabled", lambda: True)
    monkeypatch.setattr(pg_memory, "heads_by_keys", _heads)
    cfg = replace(_CFG, max_periods_per_identity=1)
    ch = _FakeCH(_digests(1, 10))
    res = await run_period_digest(cfg, ch=ch, dry_run=True)
    assert (res.periods_planned, res.periods_skipped) == (1, 1)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("written", "planned"),
    [
        (_KEY_1_5, _KEY_6_10),
        (_KEY_11_15, _KEY_1_5),
    ],
    ids=["first-written", "last-written"],
)
async def test_on_clickhouse_a_written_window_frees_its_place_in_the_cap_and_is_counted(
    monkeypatch: pytest.MonkeyPatch, written: str, planned: str
) -> None:
    monkeypatch.setattr("server.memory.app.evidence.link_belief_sources", _noop)
    cfg = replace(_CFG, max_periods_per_identity=1)
    ch = _FakeCH(_digests(1, 15), existing={written})
    res = await run_period_digest(cfg, ch=ch, llm=_FakeLLM(), embedder=_FakeEmbedder())
    assert (res.periods_written, res.periods_skipped) == (1, 1)
    assert _written_keys(ch) == [planned]


@pytest.mark.asyncio
@pytest.mark.parametrize("postgres", [False, True], ids=["clickhouse", "postgres"])
async def test_a_negative_cap_plans_nothing(
    monkeypatch: pytest.MonkeyPatch, postgres: bool
) -> None:
    from server.core import pg
    from server.memory.store import pg_memory

    async def _none(keys: Any, **_: Any) -> Any:
        return {}

    monkeypatch.setattr(pg, "enabled", lambda: postgres)
    if postgres:
        monkeypatch.setattr(pg_memory, "heads_by_keys", _none)
    cfg = replace(_CFG, max_periods_per_identity=-1)
    res = await run_period_digest(cfg, ch=_FakeCH(_digests(1, 10)), dry_run=True)
    assert res.periods_planned == 0


@pytest.mark.asyncio
async def test_a_held_embedding_holds_the_identity_without_a_failure() -> None:
    from server.llm import customer

    class _Held(_FakeEmbedder):
        async def embed(self, text: str) -> list[float]:
            raise customer.HOLDS[0]("held")

    ch = _FakeCH(_digests(1, 5))
    res = await run_period_digest(_CFG, ch=ch, llm=_FakeLLM(), embedder=_Held())
    assert res.periods_written == 0 and ch.inserts == [] and not res.failures


@pytest.mark.asyncio
@pytest.mark.parametrize("published", [True, False], ids=["written", "publication-pending"])
async def test_a_documented_version_owes_its_document_and_publishes_it_with_its_row(
    monkeypatch: pytest.MonkeyPatch, published: bool
) -> None:
    """The commit owes the document, which goes to the publication batch with the row;
    whether the row reached ClickHouse is the publication's outcome."""
    import time

    from server.core import pg
    from server.memory.store import lifecycle, pg_memory, pg_publish

    row, document = _rows_for(_period(1, 5))
    sent: list[Any] = []
    owed = pg_memory.MEMORY_ROW | pg_memory.SEARCH_DOC | pg_memory.AUDIT_ROW

    async def _commit(rows: Any, *, targets: int, **_: Any) -> Any:
        assert targets == owed
        return pg_memory.Committed(
            rows=rows,
            audits=[],
            seqs=[("o", "ns", 1)],
            targets=[targets],
            deadline=time.monotonic() + 60,
        )

    async def _publish(committed: Any, *, documents: Any, ch: Any) -> None:
        sent.append(documents)
        if not published:
            raise pg_lease.PublicationPending()

    monkeypatch.setattr(pg, "enabled", lambda: True)
    monkeypatch.setattr(pg_publish, "commit", _commit)
    monkeypatch.setattr(pg_publish, "publish", _publish)
    assert await lifecycle.insert_documented_versions([row], [document], ch=object()) is published
    assert sent == [[document]]


@pytest.mark.asyncio
async def test_a_documented_version_needs_one_document_per_row() -> None:
    from server.memory.store import lifecycle

    with pytest.raises(ValueError):
        await lifecycle.insert_documented_versions([{}], [])


@pytest.mark.asyncio
async def test_dry_run_plans_without_llm_or_writes() -> None:
    ch = _FakeCH(_digests(1, 10))
    res = await run_period_digest(_CFG, ch=ch, dry_run=True)
    assert res.periods_planned == 2 and res.periods_written == 0 and ch.inserts == []


@pytest.mark.asyncio
async def test_rewrite_existing_replans_written_windows() -> None:
    ch = _FakeCH(_digests(1, 5), existing={_KEY_1_5})
    cfg = replace(_CFG, rewrite_existing=True)
    res = await run_period_digest(cfg, ch=ch, llm=_FakeLLM(), embedder=_FakeEmbedder())
    assert res.periods_written == 1 and res.periods_skipped == 0


def test_rows_carry_entities_deduped_and_capped() -> None:
    period = _period(1, 5)
    memory_row, embedding_row = _rows_for(period, entities=["Melanie", " melanie ", "Caroline", ""])
    assert memory_row["entities"] == ["caroline", "melanie"]
    assert embedding_row["entities"] == memory_row["entities"]
    # the cap counts distinct names and keeps the alphabetically first 32, whatever the input order
    names = [e for i in reversed(range(40)) for e in (f"E{i:02d} ", f"e{i:02d}")]
    memory_row, embedding_row = _rows_for(period, entities=names)
    assert memory_row["entities"] == [f"e{i:02d}" for i in range(32)]
    assert embedding_row["entities"] == memory_row["entities"]


@pytest.mark.asyncio
async def test_the_digest_keeps_its_callers_actor(monkeypatch: pytest.MonkeyPatch) -> None:
    """It sets no actor of its own: its row and its commit's writer are the caller's."""
    from server.memory.app import period_digest
    from server.memory.app.provenance import writer_actor
    from server.memory.domain.provenance import cli_actor, pipeline_actor

    written: list[tuple[str, str]] = []

    async def _insert(rows: list[dict[str, Any]], documents: Any, **_: Any) -> bool:
        written.append((rows[0]["actor"], writer_actor()))
        return True

    monkeypatch.setattr(period_digest, "insert_documented_versions", _insert)
    monkeypatch.setattr("server.memory.app.evidence.link_belief_sources", _noop)
    ch = _FakeCH(_digests(1, 5))
    with pipeline_actor(cli_actor("period-digest")):
        await run_period_digest(_CFG, ch=ch, llm=_FakeLLM(), embedder=_FakeEmbedder())
    actor = cli_actor("period-digest")
    assert written == [(actor, actor)]


def test_loom_period_digest_writes_as_its_command(monkeypatch: pytest.MonkeyPatch) -> None:
    """The CLI dispatch binds the command's actor around the digest, its only caller."""
    import argparse
    import asyncio

    from server.cli import app, maintenance
    from server.memory.app import period_digest
    from server.memory.app.provenance import writer_actor

    seen: list[str] = []

    async def _run(cfg: Any, **_: Any) -> Any:
        seen.append(writer_actor())
        return period_digest.PeriodDigestResult()

    monkeypatch.setattr(period_digest, "run_period_digest", _run)
    monkeypatch.setattr(
        maintenance, "run_connected", lambda go, **_declared: asyncio.run(go(object()))
    )
    args = argparse.Namespace(
        cmd="period-digest",
        func=maintenance._cmd_period_digest,
        sessions=5,
        max_identities=1,
        max_periods=1,
        org="o",
        namespace="ns",
        rewrite=False,
        dry_run=True,
    )
    assert app._dispatch(args) == 0
    assert seen == ["process:cli:period-digest"]
