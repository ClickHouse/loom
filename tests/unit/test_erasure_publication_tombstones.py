"""Delayed retirement cannot recreate plaintext or marker rows after erasure."""

from datetime import datetime

import pytest

from server.core.errors import BackendUnavailableError, ValidationError
from server.core.hashing import sha256_value
from server.memory.domain.refusals import fingerprint
from server.memory.store import refusals
from server.memory.store.inserts import insert_embedding_rows, insert_memory_versions
from server.memory.store.rows import build_embedding_row, build_memory_version_row
from tests.unit.test_memory_verify import _row


class CH:
    def __init__(self, head, scope="", *, active=False, failure=False):
        self.head = head
        self.scope = scope
        self.active = active
        self.failure = failure
        self.inserts = []
        self.deletes = []
        self.patterns = set(fingerprint("retired copper lark"))

    async def query_all(self, sql, params=None):
        if "FROM loom.memory FINAL" in sql:
            return [build_memory_version_row(self.head)] if self.head else []
        assert "memory_refusals" in sql
        if self.failure:
            raise BackendUnavailableError("policy unavailable")
        if "SELECT DISTINCT" in sql:
            return [{"units": -1 if self.active else 3}]
        erasure_only = "('identity','erased_identity')" not in sql
        match = self.scope and (not erasure_only or self.scope != "identity")
        return [{"found": 1}] if match and self.patterns.intersection(params["candidates"]) else []

    async def insert(self, table, rows, **kwargs):
        self.inserts.extend(rows)

    async def execute(self, sql, params=None, *, settings=None):
        assert sql.startswith("DELETE FROM loom.memory_embeddings")
        assert "created_at" not in sql
        assert settings == {"lightweight_deletes_sync": 2}
        self.deletes.append(params)


def vector(head):
    return build_embedding_row(
        **{
            c: getattr(head, c)
            for c in ("org", "namespace", "agent", "user_id", "session_id", "memory_key")
        },
        content_excerpt="retired copper lark",
        content_sha256=sha256_value("retired copper lark"),
        embedding_model="test",
        embedding_dim=1,
        embedding=[0.5],
        created_at=datetime(2026, 1, 1),
        version=200,
        tombstoned_at=datetime(2026, 1, 2),
    )


@pytest.mark.parametrize("scope", ["", "identity", "namespace", "user_id", "erased_identity"])
async def test_retirement_preserves_only_permitted_cache_content(scope):
    head = _row(value="retired copper lark", tombstoned_at=datetime(2026, 1, 2))
    ch = CH(head, scope)
    row = vector(head)
    acknowledged = await insert_embedding_rows([row], ch=ch)
    assert len(acknowledged) == 1
    if scope in {"namespace", "user_id", "erased_identity"}:
        assert not ch.inserts and len(ch.deletes) == 1
    else:
        assert not ch.deletes and len(ch.inserts) == 1
        assert ch.inserts[0]["content_excerpt"] == (
            "" if scope == "identity" else row["content_excerpt"]
        )


@pytest.mark.parametrize("scope", ["", "identity", "user_id"])
async def test_missing_source_is_cleaned_without_inserting_even_a_bare_marker(scope):
    ch = CH(None, scope)
    row = vector(_row(value="retired copper lark"))
    assert await insert_embedding_rows([row], ch=ch) == [row]
    assert not ch.inserts and len(ch.deletes) == 1
    assert ch.deletes[0]["version"] == 200
    assert "created_at" not in ch.deletes[0]


@pytest.mark.parametrize("active,failure", [(True, False), (False, True)])
async def test_missing_source_still_requires_available_policy_and_no_active_erasure(
    active, failure
):
    ch = CH(None, active=active, failure=failure)
    row = vector(_row(value="retired copper lark"))
    with pytest.raises(BackendUnavailableError):
        await insert_embedding_rows([row], ch=ch)
    assert not ch.inserts and not ch.deletes


@pytest.mark.parametrize("scope", ["identity", "namespace", "user_id", "erased_identity"])
async def test_canonical_tombstone_preserves_point_history_but_cannot_republish_erased_text(scope):
    head = _row(value="retired copper lark", tombstoned_at=datetime(2026, 1, 2))
    ch = CH(head, scope)
    rows = [build_memory_version_row(head)]
    if scope == "identity":
        await refusals.check_clickhouse(ch, rows)
    else:
        with pytest.raises(ValidationError, match="refused"):
            await refusals.check_clickhouse(ch, rows)


@pytest.mark.parametrize("scope", ["", "identity", "namespace", "user_id", "erased_identity"])
async def test_native_committed_history_delivers_after_point_deletion_but_not_erasure(
    monkeypatch, scope
):
    import json
    from contextlib import asynccontextmanager

    from server.core import pg
    from server.memory.app import extraction_queue

    class Connection:
        async def execute(self, *args):
            return None

        async def fetch(self, *args):
            return [{"units": 3}]

        async def fetchval(self, sql, *args):
            erasure_only = "('identity','erased_identity')" not in sql
            return bool(scope and (not erasure_only or scope != "identity"))

    conn = Connection()

    @asynccontextmanager
    async def transaction():
        yield conn

    monkeypatch.setattr(pg, "enabled", lambda: True)
    monkeypatch.setattr(pg, "transaction", transaction)
    rows = [build_memory_version_row(_row(value="retired copper lark"))]

    async def publish():
        ch = CH(None)
        assert await insert_memory_versions(rows, ch=ch, published=True) == 1
        assert ch.inserts == rows

    if scope in {"", "identity"}:
        await publish()
    else:
        with pytest.raises(ValidationError, match="refused"):
            await publish()
    # A new authority commit still checks point refusals.
    if scope:
        with pytest.raises(ValidationError, match="refused"):
            await refusals.check_postgres(conn, rows)
        with pytest.raises(ValidationError, match="refused"):
            async with refusals.publication(CH(None), rows):
                pytest.fail("Uncommitted content bypassed its refusal")
        raw = {
            **rows[0],
            "content": "retired copper lark",
            "payload": json.dumps(
                {"messages": [{"role": "user", "content": "retired copper lark"}]}
            ),
        }
        for table in ("loom.memory_evidence", "loom.memory_chunks", "queue"):
            ch = CH(None)
            with pytest.raises(ValidationError, match="refused"):
                if table == "queue":
                    await extraction_queue._insert_rows(ch, [raw], skip_refused=False, create=True)
                else:
                    await refusals.insert_content_rows(ch, table, [raw])
            assert ch.inserts == []
    else:
        await refusals.check_postgres(conn, rows)


@pytest.mark.parametrize("scope", ["private", "shared"])
async def test_orphan_cleanup_preserves_newer_vectors_and_removes_older_months(scope):
    head = _row(value="retired copper lark")
    requested = vector(head) | {"scope": scope}
    old = requested | {"version": 50, "created_at": datetime(2025, 12, 1)}
    recent = requested | {"version": 100, "created_at": datetime(2026, 1, 1)}
    newer = requested | {"version": 250}

    class State(CH):
        def __init__(self):
            super().__init__(None)
            self.rows = [old, recent, newer]

        async def execute(self, sql, params=None, *, settings=None):
            await super().execute(sql, params, settings=settings)
            self.rows = [r for r in self.rows if r["version"] >= params["version"]]

    ch = State()
    await insert_embedding_rows([requested], ch=ch)
    assert ch.rows == [newer]
    assert not ch.inserts


@pytest.mark.parametrize(
    "durable_present,current_present", [(True, False), (False, True), (False, False)]
)
async def test_native_retirement_rechecks_on_its_only_connection(
    monkeypatch, durable_present, current_present
):
    from contextlib import asynccontextmanager

    from server.core import pg
    from server.memory.store import pg_memory
    from server.memory.store.rows import identity_key

    head = _row(value="retired copper lark", tombstoned_at=datetime(2026, 1, 2))
    raw = build_memory_version_row(head)
    heads = {identity_key(raw): raw}
    borrowed = False

    class Connection:
        async def execute(self, *args):
            assert borrowed

        async def fetch(self, *args):
            assert borrowed
            return []

        async def fetchval(self, *args):
            assert borrowed
            return None

    connection = Connection()

    async def durable(*args, **kwargs):
        assert not borrowed and kwargs["durable"]
        return heads if durable_present else {}

    async def current(conn, *args, **kwargs):
        assert borrowed and conn is connection and kwargs["lock"]
        return heads if current_present else {}

    @asynccontextmanager
    async def transaction():
        nonlocal borrowed
        assert not borrowed
        borrowed = True
        try:
            yield connection
        finally:
            borrowed = False

    monkeypatch.setattr(pg, "enabled", lambda: True)
    monkeypatch.setattr(pg, "transaction", transaction)
    monkeypatch.setattr(pg_memory, "heads_by_keys", durable)
    monkeypatch.setattr(pg_memory, "heads_on_connection", current)
    ch = CH(None)
    acknowledged = await insert_embedding_rows([vector(head)], ch=ch)
    assert not ch.inserts
    assert len(acknowledged) == len(ch.deletes) == int(not durable_present and not current_present)


async def test_a_purged_queue_source_is_not_recreated_by_a_state_transition():
    from server.memory.app import extraction_queue
    from tests.unit.test_extraction_queue import _stored_row

    class Missing:
        async def query_all(self, sql, params=None):
            return []

        async def insert(self, *args, **kwargs):
            pytest.fail("A queue mirror recreated its missing source")

    await extraction_queue.mark_done(_stored_row(), written=1, ch=Missing())


@pytest.mark.parametrize("scope", ["", "identity", "namespace", "user_id", "erased_identity"])
@pytest.mark.parametrize("present", [False, True])
@pytest.mark.parametrize("native", [False, True])
@pytest.mark.parametrize("user", ["alice", "alice/team", ""])
async def test_shared_canonical_retirement_cannot_recreate_erased_plaintext(
    monkeypatch, scope, present, native, user
):
    from server.memory.store import shared

    head = _row(value="retired copper lark", user_id=user)
    row = {
        **dict.fromkeys(shared.SHARED_PERSISTED_COLS),
        "org": head.org,
        "namespace": head.namespace,
        "memory_key": head.memory_key,
        "value": '"retired copper lark"',
        "version": 200,
        "tombstoned_at": datetime(2026, 1, 2),
        "promoted_from": [
            "/".join(
                str(getattr(head, c))
                for c in ("org", "namespace", "agent", "user_id", "session_id", "memory_key")
            )
        ],
    }

    class SharedCH(CH):
        async def query_all(self, sql, params=None):
            if params["uid"] != (user or "-"):
                return []
            return await super().query_all(sql, params=params)

        async def query_one(self, sql, params=None):
            assert "FROM loom.shared" in sql and "select_sequential_consistency" in sql
            return {"version": 50} if present else None

        async def execute(self, sql, params=None, *, settings=None):
            assert sql.startswith("DELETE FROM loom.shared")
            assert "version <" in sql and params["version"] == 200
            assert settings == {"lightweight_deletes_sync": 2}
            self.deletes.append(params)

    if native:
        from contextlib import asynccontextmanager

        from server.core import pg

        borrowed = False

        class Connection:
            async def execute(self, *args):
                assert borrowed

            async def fetch(self, *args):
                assert borrowed
                return [{"units": 3}] if args[3] == (user or "-") else []

            async def fetchval(self, *args):
                assert borrowed
                return args[3] == (user or "-") and scope in {
                    "namespace",
                    "user_id",
                    "erased_identity",
                }

        @asynccontextmanager
        async def transaction():
            nonlocal borrowed
            assert not borrowed
            borrowed = True
            try:
                yield Connection()
            finally:
                borrowed = False

        monkeypatch.setattr(pg, "enabled", lambda: True)
        monkeypatch.setattr(pg, "transaction", transaction)

    ch = SharedCH(head, scope)
    await shared.insert_shared_row(row, ch=ch)
    purged = not present or scope in {"namespace", "user_id", "erased_identity"}
    assert bool(ch.deletes) == purged
    assert ch.inserts == ([] if purged else [row])


async def test_shared_repair_cannot_borrow_an_ambiguous_neighbors_point_consent():
    import json

    from server.memory.domain.excerpt import build_excerpt
    from server.memory.store import shared

    source = _row(
        org="o",
        namespace="n",
        agent="a/alice",
        user_id="team",
        session_id="s",
        memory_key="k",
        value="retired copper lark",
    )
    row = vector(source) | {
        "agent": "_shared",
        "user_id": "-",
        "session_id": "-",
        "scope": "shared",
        "tombstoned_at": None,
        "content_excerpt": build_excerpt(json.dumps(source.value), memory_key="k"),
    }
    canonical = {
        "value": json.dumps(source.value),
        "tombstoned_at": None,
        "promoted_from": ["o/n/a/alice/team/s/k"],
        "version": 100,
    }

    class Shared(CH):
        async def query_one(self, sql, params=None):
            return canonical

        async def query_all(self, sql, params=None):
            if "memory_refusals" in sql and params["uid"] != "alice/team":
                return []
            return await super().query_all(sql, params=params)

    ch = Shared(source, "identity")
    with pytest.raises(ValidationError, match="previous deletion"):
        await shared.repair_shared_embeddings([row], ch=ch)
    assert not ch.inserts
