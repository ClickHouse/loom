"""Unit tests for memory.clear (bulk-tombstone wire op)."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import HTTPException

from tests.unit.clickhouse_local import CLICKHOUSE, EMBEDDINGS, ddl, run


@pytest.mark.asyncio
async def test_clear_with_user_id_tombstones_across_tables(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.memory import store as memory_repo
    from server.memory.api.models_crud import MemoryClearInput
    from server.memory.app.handlers import handle_memory_clear

    captured: dict[str, object] = {}

    async def fake_bulk(*, org, namespace, user_id=None, agent=None, on_tombstoned=None, ch=None):
        captured.update(
            org=org,
            namespace=namespace,
            user_id=user_id,
            agent=agent,
        )
        return {
            "loom.memory": 47,
            "loom.memory_embeddings": 42,
        }

    monkeypatch.setattr(memory_repo, "bulk_tombstone_scope", fake_bulk)

    out = await handle_memory_clear(
        MemoryClearInput(
            org="clickhouse",
            namespace="finance",
            user_id="alice",
            confirm_subject="alice",
        )
    )
    assert out.total == 47 + 42
    assert out.rows_affected["loom.memory"] == 47
    assert out.rows_affected["loom.memory_embeddings"] == 42
    assert captured["user_id"] == "alice"
    assert captured["agent"] is None


@pytest.mark.asyncio
async def test_clear_with_agent_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.memory import store as memory_repo
    from server.memory.api.models_crud import MemoryClearInput
    from server.memory.app.handlers import handle_memory_clear

    async def fake_bulk(*, org, namespace, user_id=None, agent=None, on_tombstoned=None, ch=None):
        return {"loom.memory": 3, "loom.memory_embeddings": 0}

    monkeypatch.setattr(memory_repo, "bulk_tombstone_scope", fake_bulk)

    out = await handle_memory_clear(
        MemoryClearInput(
            org="clickhouse",
            namespace="finance",
            agent="research-bot",
            confirm_subject="research-bot",
        )
    )
    assert out.total == 3


@pytest.mark.asyncio
async def test_clear_refuses_scopeless_call() -> None:
    from server.memory.api.models_crud import MemoryClearInput
    from server.memory.app.handlers import handle_memory_clear

    with pytest.raises(HTTPException) as exc:
        await handle_memory_clear(
            MemoryClearInput(
                org="clickhouse",
                namespace="finance",
                confirm_subject="anything",
            )
        )
    assert exc.value.status_code == 400
    assert exc.value.detail["code"] == "MISSING_SCOPE"
    assert "erasure" in exc.value.detail["message"]


@pytest.mark.asyncio
async def test_clear_refuses_wrong_confirm_subject() -> None:
    from server.memory.api.models_crud import MemoryClearInput
    from server.memory.app.handlers import handle_memory_clear

    with pytest.raises(HTTPException) as exc:
        await handle_memory_clear(
            MemoryClearInput(
                org="clickhouse",
                namespace="finance",
                user_id="alice",
                confirm_subject="bob",  # mismatch
            )
        )
    assert exc.value.status_code == 400
    assert exc.value.detail["code"] == "CONFIRM_SUBJECT_MISMATCH"


@pytest.mark.asyncio
async def test_clear_both_scope_args_accepts_either_confirm(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When both user_id and agent are set, confirm_subject must match
    one of them (either is OK)."""
    from server.memory import store as memory_repo
    from server.memory.api.models_crud import MemoryClearInput
    from server.memory.app.handlers import handle_memory_clear

    async def fake_bulk(**_kw):
        return {"loom.memory": 0, "loom.memory_embeddings": 0}

    monkeypatch.setattr(memory_repo, "bulk_tombstone_scope", fake_bulk)

    # confirm_subject matches user_id → OK
    await handle_memory_clear(
        MemoryClearInput(
            org="a",
            namespace="n",
            user_id="alice",
            agent="bot",
            confirm_subject="alice",
        )
    )
    # confirm_subject matches agent → also OK
    await handle_memory_clear(
        MemoryClearInput(
            org="a",
            namespace="n",
            user_id="alice",
            agent="bot",
            confirm_subject="bot",
        )
    )
    # confirm_subject matches neither → REJECT
    with pytest.raises(HTTPException):
        await handle_memory_clear(
            MemoryClearInput(
                org="a",
                namespace="n",
                user_id="alice",
                agent="bot",
                confirm_subject="charlie",
            )
        )


@pytest.mark.asyncio
async def test_clear_zero_rows_is_well_formed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Clearing a scope with no matching rows returns total=0 without
    raising — same semantics as memory.delete on a missing key."""
    from server.memory import store as memory_repo
    from server.memory.api.models_crud import MemoryClearInput
    from server.memory.app.handlers import handle_memory_clear

    async def fake_bulk(**_kw):
        return {"loom.memory": 0, "loom.memory_embeddings": 0}

    monkeypatch.setattr(memory_repo, "bulk_tombstone_scope", fake_bulk)

    out = await handle_memory_clear(
        MemoryClearInput(
            org="clickhouse",
            namespace="ns",
            user_id="ghost",
            confirm_subject="ghost",
        )
    )
    assert out.total == 0
    assert all(v == 0 for v in out.rows_affected.values())


@pytest.mark.asyncio
async def test_clear_star_agent_tombstones_without_an_agent_filter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The statement a star-agent clear runs filters on the user alone."""
    from server.memory.api.models_crud import MemoryClearInput
    from server.memory.app.handlers import handle_memory_clear

    ch = _clear_ch(monkeypatch, [])
    await handle_memory_clear(
        MemoryClearInput(
            org="acme", namespace="chat", user_id="u1", agent="*", confirm_subject="u1"
        )
    )
    memory_read = ch.statements("SELECT * FROM loom.memory FINAL")[0]
    assert "user_id = {" in memory_read.sql
    assert "agent = {" not in memory_read.sql
    assert "agent" not in memory_read.params


@pytest.mark.asyncio
async def test_clear_star_user_tombstones_without_a_user_filter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The statement a star-user clear runs filters on the agent alone."""
    from server.memory.api.models_crud import MemoryClearInput
    from server.memory.app.handlers import handle_memory_clear

    ch = _clear_ch(monkeypatch, [])
    await handle_memory_clear(
        MemoryClearInput(
            org="acme", namespace="chat", user_id="*", agent="bot", confirm_subject="bot"
        )
    )
    memory_read = ch.statements("SELECT * FROM loom.memory FINAL")[0]
    assert "agent = {" in memory_read.sql
    assert "user_id = {" not in memory_read.sql
    assert "uid" not in memory_read.params


def _setup(rows: list[tuple[str, str, str, str, str]]) -> list[str]:
    """The memory and embedding rows of `rows` ((agent, user, session, key, lock_mode))."""
    cols = "org, namespace, agent, user_id, session_id, memory_key, version"
    return [
        *EMBEDDINGS,
        ddl("memory"),
        *(
            f"INSERT INTO loom.memory ({cols}, memory_id, lock_mode)"
            f" SELECT 'o', 'n', '{a}', '{u}', '{s}', '{k}', 1, generateUUIDv4(), '{mode}'"
            for a, u, s, k, mode in rows
        ),
        *(
            f"INSERT INTO loom.memory_embeddings ({cols}, embedding, memory_key_embedding)"
            f" SELECT 'o', 'n', '{a}', '{u}', '{s}', '{k}', 1,"
            # Both vector columns at the codec's declared dimension.
            " arrayWithConstant(3072, 0.), arrayWithConstant(3072, 0.)"
            for a, u, s, k, _ in rows
        ),
    ]


# (agent, user_id, session_id, memory_key, lock_mode) rows; the first is always locked.
_STAR_AGENT_ROWS = [
    ("A", "u1", "-", "K", "read_only"),
    ("B", "u1", "-", "K", ""),
    ("B", "u1", "-", "J", ""),
    ("B", "u2", "-", "K", ""),  # another user: outside the clear
]
_STAR_USER_ROWS = [
    ("A", "u1", "-", "K", "read_only"),
    ("A", "u2", "-", "K", ""),
    ("A", "u2", "-", "J", ""),
    ("B", "u2", "-", "K", ""),  # another agent: outside the clear
]
_SESSION_ROWS = [
    ("A", "u1", "s1", "K", "read_only"),
    ("A", "u1", "s2", "K", ""),
]


@pytest.mark.asyncio
@pytest.mark.skipif(not CLICKHOUSE, reason="clickhouse binary not installed")
@pytest.mark.parametrize(
    ("user_id", "agent", "rows", "live"),
    [
        ("u1", "*", _STAR_AGENT_ROWS, [("A", "u1", "-", "K"), ("B", "u2", "-", "K")]),
        ("*", "A", _STAR_USER_ROWS, [("A", "u1", "-", "K"), ("B", "u2", "-", "K")]),
        ("u1", "A", _SESSION_ROWS, [("A", "u1", "s1", "K")]),
    ],
    ids=["star-agent", "star-user", "same-key-other-session"],
)
async def test_a_clear_keeps_vectors_only_for_the_locked_identity_in_clickhouse(
    monkeypatch: pytest.MonkeyPatch,
    user_id: str,
    agent: str,
    rows: list[tuple[str, str, str, str, str]],
    live: list[tuple[str, str, str, str]],
) -> None:
    """The embeddings read and mutation a clear builds, run on schema.sql's tables: a key
    locked under one identity keeps that identity's vector, and only that one."""
    from server.memory.api.models_crud import MemoryClearInput
    from server.memory.app.handlers import handle_memory_clear
    from tests.unit.ch_fake import RecordingCH

    setup = _setup(rows)
    ch = (
        RecordingCH()
        .on("SELECT max(version) AS v", [{"v": 0}])
        .on("SELECT * FROM loom.memory FINAL", [])
        .on("SELECT count() AS n FROM loom.memory FINAL", [{"n": 1}], times=1)
    )
    ch.on("SELECT count() AS n", [{"n": 1}])
    # the locked identities are read from the same tables the mutation then runs on
    ch.on(
        "SELECT DISTINCT cityHash64(agent, user_id, session_id, memory_key) AS h",
        lambda sql, params: run(setup, sql, params),
    )
    ch.install(monkeypatch)
    subject = agent if user_id == "*" else user_id
    out = await handle_memory_clear(
        MemoryClearInput(
            org="o", namespace="n", user_id=user_id, agent=agent, confirm_subject=subject
        )
    )
    assert out.locked_skipped == 1
    (emb_alter,) = ch.statements("ALTER TABLE loom.memory_embeddings", kind="execute")
    assert "SELECT" not in emb_alter.sql and "now()" not in emb_alter.sql
    got = run(
        [*setup, emb_alter.sql],
        "SELECT agent, user_id, session_id, memory_key FROM loom.memory_embeddings FINAL"
        " WHERE tombstoned_at IS NULL ORDER BY agent, user_id, session_id, memory_key",
        emb_alter.params,
    )
    assert [(r["agent"], r["user_id"], r["session_id"], r["memory_key"]) for r in got] == live


_BUCKETED_ROWS = [
    *(("A", "u1", f"s{i}", "K", "read_only") for i in range(4)),
    *(("A", "u1", f"s{i}", "K", "") for i in range(4, 7)),
]


@pytest.mark.asyncio
@pytest.mark.skipif(not CLICKHOUSE, reason="clickhouse binary not installed")
async def test_a_bucketed_clear_keeps_every_locked_identitys_vector_in_clickhouse(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Past one mutation's worth of locks each hash bucket is its own mutation: Python's
    split of the locked hashes and the SQL's split of the rows must agree, or a bucket
    tombstones a locked vector it did not name."""
    import server.memory.store.lifecycle as lifecycle
    from server.memory.api.models_crud import MemoryClearInput
    from server.memory.app.handlers import handle_memory_clear
    from tests.unit.ch_fake import RecordingCH

    monkeypatch.setattr(lifecycle, "_LOCKED_PER_MUTATION", 1)
    setup = _setup(_BUCKETED_ROWS)
    run(setup, "SELECT 1", path=tmp_path)
    ch = (
        RecordingCH()
        .on("SELECT max(version) AS v", [{"v": 0}])
        .on("SELECT * FROM loom.memory FINAL", [])
        .on("SELECT count() AS n FROM loom.memory FINAL", [{"n": 4}], times=1)
    )
    ch.on("SELECT count() AS n", [{"n": 1}])
    ch.on(
        "SELECT DISTINCT cityHash64(agent, user_id, session_id, memory_key) AS h",
        lambda sql, params: run([], sql, params, path=tmp_path),
    )
    ch.install(monkeypatch)
    await handle_memory_clear(
        MemoryClearInput(org="o", namespace="n", user_id="u1", agent="A", confirm_subject="u1")
    )
    alters = ch.statements("ALTER TABLE loom.memory_embeddings", kind="execute")
    assert sorted(a.params["bucket"] for a in alters) == [0, 1, 2, 3]
    for a in alters:
        run(["SET mutations_sync = 1"], a.sql, a.params, path=tmp_path)
    got = run(
        [],
        "SELECT session_id FROM loom.memory_embeddings FINAL"
        " WHERE tombstoned_at IS NULL ORDER BY session_id",
        path=tmp_path,
    )
    assert [r["session_id"] for r in got] == ["s0", "s1", "s2", "s3"]


@pytest.mark.asyncio
@pytest.mark.parametrize(("locked_n", "mutations"), [(1, 1), (25_000, 10)])
async def test_a_clear_names_a_bounded_number_of_locked_identities_per_mutation(
    monkeypatch: pytest.MonkeyPatch, locked_n: int, mutations: int
) -> None:
    """However many identities a clear tombstones, the embeddings step is one mutation
    until the locked side passes what one mutation names; then each hash bucket of the
    scope is one mutation naming only its own locked identities."""
    from server.core.ch_params import chunks
    from server.memory.api.models_crud import MemoryClearInput
    from server.memory.app.handlers import handle_memory_clear
    from tests.unit.ch_fake import RecordingCH

    # full-width UInt64s, spread unevenly over the buckets as real hashes are
    hashes = [{"h": 2**63 + (i * 2654435761) % 2**62} for i in range(locked_n)]
    ch = (
        RecordingCH()
        .on("SELECT max(version) AS v", [{"v": 0}])
        .on("SELECT * FROM loom.memory FINAL", [])
        .on("SELECT count() AS n FROM loom.memory FINAL", [{"n": locked_n}], times=1)
    )
    ch.on("SELECT count() AS n", [{"n": 100_000}])
    ch.on("SELECT DISTINCT cityHash64(agent, user_id, session_id, memory_key) AS h", hashes)
    ch.install(monkeypatch)
    await handle_memory_clear(
        MemoryClearInput(org="o", namespace="n", user_id="u1", confirm_subject="u1")
    )
    alters = ch.statements("ALTER TABLE loom.memory_embeddings", kind="execute")
    assert len(alters) == mutations
    assert all("SELECT" not in a.sql for a in alters)
    bound = [a.params["locked"] for a in alters]
    # each list binds as one form field, which ClickHouse caps at 128 KiB
    assert all(len(list(chunks(part, limit=128 << 10))) == 1 for part in bound)
    assert sorted(h for part in bound for h in part) == sorted(r["h"] for r in hashes)


@pytest.mark.asyncio
async def test_clear_refuses_star_as_confirm_subject() -> None:
    from server.memory.api.models_crud import MemoryClearInput
    from server.memory.app.handlers import handle_memory_clear

    with pytest.raises(HTTPException) as exc:
        await handle_memory_clear(
            MemoryClearInput(
                org="clickhouse",
                namespace="finance",
                user_id="alice",
                agent="*",
                confirm_subject="*",
            )
        )
    assert exc.value.status_code == 400
    assert exc.value.detail["code"] == "CONFIRM_SUBJECT_MISMATCH"


@pytest.mark.asyncio
@pytest.mark.parametrize(("user_id", "agent"), [("-", "bot"), ("u1", "-")])
async def test_dash_never_confirms_a_clear(user_id: str, agent: str) -> None:
    """'-' is a value anyone can type, so it never confirms, even as a literal scope."""
    from server.memory.api.models_crud import MemoryClearInput
    from server.memory.app.handlers import handle_memory_clear

    with pytest.raises(HTTPException) as exc:
        await handle_memory_clear(
            MemoryClearInput(
                org="clickhouse",
                namespace="finance",
                user_id=user_id,
                agent=agent,
                confirm_subject="-",
            )
        )
    assert exc.value.status_code == 400
    assert exc.value.detail["code"] == "CONFIRM_SUBJECT_MISMATCH"


@pytest.mark.asyncio
@pytest.mark.parametrize(("user_id", "agent"), [("-", "bot"), ("u1", "-")])
async def test_clear_accepts_dash_beside_a_named_axis(
    monkeypatch: pytest.MonkeyPatch, user_id: str, agent: str
) -> None:
    """'-' beside a concrete value is a literal scope: both filters reach the statement."""
    from server.memory.api.models_crud import MemoryClearInput
    from server.memory.app.handlers import handle_memory_clear

    ch = _clear_ch(monkeypatch, [])
    named = agent if user_id == "-" else user_id
    await handle_memory_clear(
        MemoryClearInput(
            org="acme", namespace="chat", user_id=user_id, agent=agent, confirm_subject=named
        )
    )
    memory_read = ch.statements("SELECT * FROM loom.memory FINAL")[0]
    assert "user_id = {" in memory_read.sql and "agent = {" in memory_read.sql
    assert (memory_read.params["uid"], memory_read.params["agent"]) == (user_id, agent)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("user_id", "agent"),
    [("*", "*"), ("-", "*"), ("*", "-"), ("-", None), (None, "-"), ("-", "-")],
)
async def test_clear_refuses_a_scope_that_names_nobody(
    user_id: str | None, agent: str | None
) -> None:
    """An open axis beside another open one, or beside '-' (no user, no agent), selects most of
    a namespace: erasure's job."""
    from server.memory.api.models_crud import MemoryClearInput
    from server.memory.app.handlers import handle_memory_clear

    with pytest.raises(HTTPException) as exc:
        await handle_memory_clear(
            MemoryClearInput(
                org="clickhouse",
                namespace="finance",
                user_id=user_id,
                agent=agent,
                confirm_subject="-" if "-" in (user_id, agent) else "*",
            )
        )
    assert exc.value.status_code == 400
    assert exc.value.detail["code"] == "MISSING_SCOPE"


def _clear_ch(monkeypatch: pytest.MonkeyPatch, tombstoned: list[dict]):
    from unittest.mock import AsyncMock

    from server.memory.store import lifecycle
    from server.memory.store.rows import MemoryRow, build_memory_version_row
    from tests.unit.ch_fake import RecordingCH

    supplied = tombstoned or [{"memory_key": "a"}, {"memory_key": "b"}]
    rows = [
        build_memory_version_row(
            MemoryRow.from_snapshot(
                {
                    "org": "acme",
                    "namespace": "chat",
                    "agent": "bot",
                    "user_id": "u1",
                    "session_id": "-",
                    "version": 7,
                    **row,
                }
            )
        )
        for row in supplied
    ]

    def page(sql, params):
        after = params.get("after")
        return [r for r in rows if after is None or lifecycle.key_in_namespace(r) > after][
            : params["clear_limit"]
        ]

    ch = (
        RecordingCH()
        .on("SELECT max(version) AS v", [{"v": 0}])
        .on("SELECT count() AS n FROM loom.memory FINAL", [{"n": 0}], times=1)
    )
    ch.on("SELECT count() AS n", [{"n": 2}])
    ch.on("SELECT * FROM loom.memory FINAL", page)
    ch.on("SELECT DISTINCT cityHash64", [])
    ch.on(
        "PREWHERE (org, namespace, agent, user_id, session_id, memory_key)",
        lambda sql, params: [
            r for r in ch.inserted("memory") if lifecycle.identity_key(r) in params["keys"]
        ],
    )
    monkeypatch.setattr(lifecycle, "reemit_embedding_rows_for_keys", AsyncMock(return_value=0))
    return ch.install(monkeypatch)


@pytest.mark.asyncio
async def test_clear_without_subscribers_still_writes_complete_versions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Version history is written even when no webhook subscribes."""
    from server.memory.api.models_crud import MemoryClearInput
    from server.memory.app.handlers import handle_memory_clear

    ch = _clear_ch(monkeypatch, [])
    out = await handle_memory_clear(
        MemoryClearInput(org="acme", namespace="chat", user_id="u1", confirm_subject="u1")
    )
    assert out.rows_affected == {"loom.memory": 2, "loom.memory_embeddings": 2}
    assert len(ch.inserted("memory")) == 2
    assert len(ch.statements("ALTER TABLE", kind="execute")) == 1


async def test_clear_reads_copied_and_protected_heads_under_sequential_consistency(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.core.ch import sequential_reads_active
    from server.memory.store.lifecycle import bulk_tombstone_scope

    ch = _clear_ch(monkeypatch, [])
    query_all, query_one = ch.query_all, ch.query_one
    observed: list[bool] = []

    async def read_all(*args, **kwargs):
        observed.append(sequential_reads_active())
        return await query_all(*args, **kwargs)

    async def read_one(*args, **kwargs):
        observed.append(sequential_reads_active())
        return await query_one(*args, **kwargs)

    monkeypatch.setattr(ch, "query_all", read_all)
    monkeypatch.setattr(ch, "query_one", read_one)
    await bulk_tombstone_scope(org="acme", namespace="chat", user_id="u1", ch=ch)
    assert observed and all(observed)
    assert not sequential_reads_active()


@pytest.mark.asyncio
async def test_a_clear_that_tombstones_nothing_announces_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A subscriber's namespace pays no read-back when the clear stamped no row."""
    from server.memory.api.models_crud import MemoryClearInput
    from server.memory.app import webhooks
    from server.memory.app.handlers import handle_memory_clear
    from tests.unit.ch_fake import RecordingCH

    webhooks._registry._reset_for_tests(
        {("acme", "chat"): (("wh1", frozenset({"memory.tombstoned"})),)},
        read_age_s=0.0,
        due_in_s=float("inf"),
    )
    ch = (
        RecordingCH()
        .on("SELECT max(version) AS v", [{"v": 0}])
        .on("SELECT * FROM loom.memory FINAL", [])
        .on("SELECT DISTINCT cityHash64", [])
        .on("SELECT count() AS n", [{"n": 0}])
        .install(monkeypatch)
    )
    out = await handle_memory_clear(
        MemoryClearInput(org="acme", namespace="chat", user_id="u1", confirm_subject="u1")
    )
    assert out.rows_affected == {"loom.memory": 0, "loom.memory_embeddings": 0}
    assert ch.inserted("memory") == []


@pytest.mark.asyncio
async def test_clear_announces_what_it_tombstoned_a_page_at_a_time(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.memory.api.models_crud import MemoryClearInput
    from server.memory.app import webhooks
    from server.memory.app.handlers import handle_memory_clear
    from server.memory.store import lifecycle

    def row(key: str) -> dict:
        return {
            "org": "acme",
            "namespace": "chat",
            "agent": "bot",
            "user_id": "u1",
            "session_id": "-",
            "memory_key": key,
            "memory_id": f"id-{key}",
            "version": 7,
        }

    webhooks._registry._reset_for_tests(
        {("acme", "chat"): (("wh1", frozenset({"memory.tombstoned"})),)},
        read_age_s=0.0,
        due_in_s=float("inf"),
    )
    monkeypatch.setattr(lifecycle, "CLEAR_PAGE_ROWS", 2)
    ch = _clear_ch(monkeypatch, [row("a"), row("b"), row("c")])
    announced: list[list[str]] = []

    async def announce(changes) -> None:
        announced.append([c["memory_key"] for _, c in changes])

    monkeypatch.setattr(webhooks, "announce", announce)
    await handle_memory_clear(
        MemoryClearInput(org="acme", namespace="chat", user_id="u1", confirm_subject="u1")
    )
    assert announced == [["a", "b"], ["c"]]
    written = ch.inserted("memory")
    assert [r["memory_key"] for r in written] == ["a", "b", "c"]
    assert all(r["tombstoned_at"] is not None and r["version"] > 7 for r in written)
    assert all(
        read.params["clear_limit"] == 2 for read in ch.statements("SELECT * FROM loom.memory")
    )


@pytest.mark.asyncio
async def test_clear_announces_before_the_embeddings_step(monkeypatch: pytest.MonkeyPatch) -> None:
    """A failed embeddings mutation answers 5xx, and a retry finds no live memory
    rows: the tombstoned rows are announced before that step runs."""
    from server.memory.api.models_crud import MemoryClearInput
    from server.memory.app import webhooks
    from server.memory.app.handlers import handle_memory_clear

    webhooks._registry._reset_for_tests(
        {("acme", "chat"): (("wh1", frozenset({"memory.tombstoned"})),)},
        read_age_s=0.0,
        due_in_s=float("inf"),
    )
    cleared = {"agent": "bot", "user_id": "u1", "session_id": "-", "memory_key": "a", "version": 1}
    ch = _clear_ch(monkeypatch, [cleared])

    async def execute(sql, params=None, **_kw):
        if "loom.memory_embeddings" in sql:
            raise TimeoutError("mutation timed out")

    monkeypatch.setattr(ch, "execute", execute)
    announced: list[str] = []

    async def announce(changes) -> None:
        announced.extend(c["memory_key"] for _, c in changes)

    monkeypatch.setattr(webhooks, "announce", announce)
    with pytest.raises(TimeoutError):
        await handle_memory_clear(
            MemoryClearInput(org="acme", namespace="chat", user_id="u1", confirm_subject="u1")
        )
    assert announced == ["a"]


@pytest.mark.asyncio
async def test_clear_with_a_failed_announcement_still_clears(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.memory.api.models_crud import MemoryClearInput
    from server.memory.app import webhooks
    from server.memory.app.handlers import handle_memory_clear
    from server.observability import degradations

    webhooks._registry._reset_for_tests(
        {("acme", "chat"): (("wh1", frozenset({"memory.tombstoned"})),)},
        read_age_s=0.0,
        due_in_s=float("inf"),
    )

    async def fail(changes):
        raise TimeoutError("announcement timed out")

    _clear_ch(monkeypatch, [])
    monkeypatch.setattr(webhooks, "announce", fail)
    noted: list[tuple[str, str]] = []
    monkeypatch.setattr(
        degradations, "note", lambda area, what, e=None, **_: noted.append((area, what))
    )
    out = await handle_memory_clear(
        MemoryClearInput(org="acme", namespace="chat", user_id="u1", confirm_subject="u1")
    )
    assert out.rows_affected == {"loom.memory": 2, "loom.memory_embeddings": 2}
    assert noted == [("memory.webhook", "not_announced")]
