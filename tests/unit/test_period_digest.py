"""Period digests: one derived episodic memory over K
consecutive session digests of one identity — the scale between a session
and the namespace. The plan is pure and idempotent; the write goes through
the canonical row builders and links `derived_from` to the memories the
digests compressed."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

import pytest

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


def _digest(
    n: int, day: int, text: str = "", ns: str = "ns", agent: str = "a", uid: str = "-"
) -> dict:
    return {
        "org": "o",
        "namespace": ns,
        "agent": agent,
        "user_id": uid,
        "session_id": f"{ns}:session_{n}",
        "summary_text": text or f"On 2023-07-{day:02d}, something happened in session {n}.",
        "derived_from": [f"fact_{n}_a", f"fact_{n}_b"],
        "at": datetime(2023, 7, day),
    }


def test_period_key_uses_session_tails() -> None:
    assert period_key("ns:session_1", "ns:session_5") == f"{PERIOD_KEY_PREFIX}session_1_session_5"
    assert period_key("", "") == f"{PERIOD_KEY_PREFIX}s_s"


def test_plan_cuts_complete_windows_in_time_order_and_skips_existing() -> None:
    # 12 digests, out of order, one identity: K=5 gives two complete windows and a
    # trailing pair that waits for a later pass.
    digests = [
        _digest(n, day)
        for n, day in [
            (3, 3),
            (1, 1),
            (2, 2),
            (5, 5),
            (4, 4),
            (7, 7),
            (6, 6),
            (9, 9),
            (8, 8),
            (10, 10),
            (11, 11),
            (12, 12),
        ]
    ]
    periods, present = plan_periods(digests, sessions_per_period=5)
    assert present == 0
    assert [p.memory_key for p in periods] == [
        f"{PERIOD_KEY_PREFIX}session_1_session_5",
        f"{PERIOD_KEY_PREFIX}session_6_session_10",
    ]
    assert [d["session_id"] for d in periods[0].digests] == [f"ns:session_{n}" for n in range(1, 6)]
    # a window already written is counted, not re-planned
    periods2, present2 = plan_periods(
        digests, sessions_per_period=5, existing_keys={f"{PERIOD_KEY_PREFIX}session_1_session_5"}
    )
    assert present2 == 1 and [p.memory_key for p in periods2] == [
        f"{PERIOD_KEY_PREFIX}session_6_session_10"
    ]


def test_plan_keeps_identities_apart_and_needs_k_sessions() -> None:
    digests = [_digest(n, n) for n in range(1, 6)] + [_digest(n, n, agent="b") for n in range(1, 4)]
    periods, _ = plan_periods(digests, sessions_per_period=5)
    assert len(periods) == 1 and periods[0].agent == "a"


def test_prompt_dates_every_digest_and_states_the_span() -> None:
    period = Period("o", "ns", "a", "-", tuple(_digest(n, n) for n in range(1, 4)))
    system, user = build_period_prompt(period)
    assert "never add, shift or sharpen a date" in system
    assert "Period: 2023-07-01 → 2023-07-03" in user
    assert "[2023-07-02] On 2023-07-02" in user


def test_rows_are_derived_semantic_memories_anchored_at_the_first_session() -> None:
    period = Period("o", "ns", "a", "-", tuple(_digest(n, n) for n in range(1, 6)))
    memory_row, embedding_row = build_period_memory_rows(
        period,
        text="Between 2023-07-01 and 2023-07-05, things happened.",
        embedding=[0.1, 0.2],
        embedding_model="m",
        embedding_dim=2,
        version=7,
    )
    assert memory_row["memory_key"] == f"{PERIOD_KEY_PREFIX}session_1_session_5"
    # semantic, not episodic: episodic is the non-searchable type (memory_types.py)
    assert memory_row["memory_type"] == "semantic" and embedding_row["memory_type"] == "semantic"
    assert memory_row["source_class"] == "derived"
    assert PERIOD_DIGEST_PROMPT_VERSION in str(memory_row["source_trace"])
    assert PERIOD_TAG in memory_row["tags"] and "src:summary:ns:session_3" in memory_row["tags"]
    assert memory_row["valid_at"] == datetime(2023, 7, 1)
    assert embedding_row["memory_key"] == memory_row["memory_key"]


def _rows_for(period: Period, version: int) -> dict[str, Any]:
    return build_period_memory_rows(
        period, text="t", embedding=[0.1], embedding_model="m", embedding_dim=1, version=version
    )[0]


def test_a_period_digest_row_carries_its_own_memory_id() -> None:
    # A blank memory_id stored as the zero UUID would make every digest share
    # one id, so their derived_from links would point at no row.
    first = Period("o", "ns", "a", "-", tuple(_digest(n, n) for n in range(1, 6)))
    second = Period("o", "ns", "a", "-", tuple(_digest(n, n) for n in range(6, 11)))
    assert first.memory_key != second.memory_key
    ids = {uuid.UUID(str(_rows_for(p, 1)["memory_id"])) for p in (first, second)}
    assert len(ids) == 2 and all(i.int != 0 for i in ids)


def test_rewriting_a_period_keeps_its_memory_id() -> None:
    period = Period("o", "ns", "a", "-", tuple(_digest(n, n) for n in range(1, 6)))
    assert _rows_for(period, 1)["memory_id"] == _rows_for(period, 2)["memory_id"]


def test_the_same_period_key_in_another_namespace_gets_another_id() -> None:
    digests = tuple(_digest(n, n) for n in range(1, 6))
    ids = {_rows_for(Period("o", ns, "a", "-", digests), 1)["memory_id"] for ns in ("ns", "ns2")}
    assert len(ids) == 2


class _FakeCH:
    def __init__(self, digests: list[dict[str, Any]], existing: set[str] | None = None) -> None:
        self._digests = digests
        self._existing = existing or set()
        self.inserts: list[tuple[str, list[dict[str, Any]]]] = []
        self.link_calls: list[dict[str, Any]] = []

    async def query_all(self, sql: str, params: Any = None) -> list[dict[str, Any]]:
        if "uniqExact(s.session_id)" in sql:
            return [{"org": "o", "namespace": "ns", "agent": "a", "user_id": "-", "sessions": 5}]
        if "FROM loom.session_summaries FINAL" in sql:
            return list(self._digests)
        if "startsWith(memory_key" in sql:
            return [{"memory_key": k} for k in self._existing]
        if "memory_key IN" in sql:
            return [
                {"memory_id": f"id-{k}", "entities": ["Caroline", k.split("_")[0]]}
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


@pytest.mark.asyncio
async def test_run_writes_one_period_with_lineage_and_is_idempotent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    linked: list[dict[str, Any]] = []

    async def _fake_link(**kwargs: Any) -> None:
        linked.append(kwargs)

    monkeypatch.setattr("server.memory.app.evidence.link_belief_sources", _fake_link)
    ch = _FakeCH([_digest(n, n) for n in range(1, 6)])
    llm = _FakeLLM()
    cfg = PeriodDigestConfig(sessions_per_period=5, namespace_filter="ns", require_opt_in=False)
    res = await run_period_digest(cfg, ch=ch, llm=llm, embedder=_FakeEmbedder())
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
    ch2 = _FakeCH(
        [_digest(n, n) for n in range(1, 6)], existing={f"{PERIOD_KEY_PREFIX}session_1_session_5"}
    )
    res2 = await run_period_digest(cfg, ch=ch2, llm=llm, embedder=_FakeEmbedder())
    assert res2.periods_written == 0 and res2.periods_existing == 1 and ch2.inserts == []


@pytest.mark.asyncio
async def test_dry_run_plans_without_llm_or_writes() -> None:
    ch = _FakeCH([_digest(n, n) for n in range(1, 11)])
    cfg = PeriodDigestConfig(sessions_per_period=5, namespace_filter="ns", require_opt_in=False)
    res = await run_period_digest(cfg, ch=ch, dry_run=True)
    assert res.periods_planned == 2 and res.periods_written == 0 and ch.inserts == []


@pytest.mark.asyncio
async def test_rewrite_existing_replans_written_windows() -> None:
    ch = _FakeCH(
        [_digest(n, n) for n in range(1, 6)], existing={f"{PERIOD_KEY_PREFIX}session_1_session_5"}
    )
    cfg = PeriodDigestConfig(
        sessions_per_period=5, namespace_filter="ns", require_opt_in=False, rewrite_existing=True
    )
    res = await run_period_digest(cfg, ch=ch, llm=_FakeLLM(), embedder=_FakeEmbedder())
    assert res.periods_written == 1 and res.periods_existing == 0


def test_rows_carry_entities_deduped_and_capped() -> None:
    period = Period("o", "ns", "a", "-", tuple(_digest(n, n) for n in range(1, 6)))
    memory_row, embedding_row = build_period_memory_rows(
        period,
        text="Between 2023-07-01 and 2023-07-05, things happened.",
        embedding=[0.1],
        embedding_model="m",
        embedding_dim=1,
        version=1,
        entities=["Melanie", " melanie ", "Caroline", ""] + [f"e{i}" for i in range(40)],
    )
    assert memory_row["entities"][:2] == ["caroline", "e0"] and len(memory_row["entities"]) == 32
    assert "melanie" in memory_row["entities"] or len(memory_row["entities"]) == 32
    assert embedding_row["entities"] == memory_row["entities"]
