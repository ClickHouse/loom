"""namespace.rollback never writes a stub embedding row for a row that is not
embedded: the stub's excerpt is the value, so a reinstated 'high' row would
reach search. It retires the key's live vectors instead."""

from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

import server.memory.store as store
from server.memory.app import namespace_rollback as rb
from server.memory.domain.rollback import RollbackAction, RollbackPlan
from server.memory.store import MemoryRow

_NOW = datetime.now(UTC).replace(tzinfo=None)


def _row(key: str, version: int, **overrides: Any) -> MemoryRow:
    base: dict[str, Any] = {
        "memory_id": "22222222-2222-2222-2222-222222222222",
        "org": "test",
        "namespace": "personal",
        "agent": "cursor",
        "user_id": "-",
        "session_id": "-",
        "memory_key": key,
        "memory_type": "semantic",
        "value": "a value",
        "confidence": 0.8,
        "version": version,
        "pinned": False,
        "created_at": _NOW - timedelta(days=1),
        "expires_at": None,
        "entities": [],
    }
    base.update(overrides)
    return MemoryRow(**base)


def _action(key: str, action: str, version_to: int) -> RollbackAction:
    return RollbackAction(
        agent="cursor",
        user_id="-",
        session_id="-",
        memory_key=key,
        action=action,
        version_from=1,
        version_to=version_to,
    )


@pytest.fixture
def calls(monkeypatch: pytest.MonkeyPatch) -> dict[str, list[Any]]:
    seen: dict[str, list[Any]] = {
        "stubs": [],
        "retired": [],
        "memory": [],
        "retire_calls": [],
        "fenced": [],
    }
    historical = [
        _row("plain", 10),
        _row("secret", 11, value="token sk-proj-abc", sensitivity="high"),
        _row("ep", 12, memory_type="episodic"),
    ]
    heads = {"gone_high": _row("gone_high", 20, sensitivity="high"), "gone": _row("gone", 21)}

    seen["historical"] = historical

    async def _fetch(org: str, ns: str, pairs: list[Any], **_: Any) -> list[MemoryRow]:
        return historical

    async def _live_heads(keys: list[Any], **_: Any) -> dict[Any, MemoryRow]:
        # a key born since the plan's `as_of` is one of `heads`; any other is live at 30
        return {k: heads.get(k[-1], _row(k[-1], 30)) for k in keys}

    async def _memory(rows: list[dict[str, Any]], **_: Any) -> None:
        seen["memory"].extend(rows)

    async def _stubs(rows: list[dict[str, Any]], **_: Any) -> None:
        seen["stubs"].extend(rows)

    async def _retire(items: list[tuple[Any, int]], **_: Any) -> int:
        seen["retire_calls"].append(items)
        seen["retired"].extend(items)
        return len(items)

    monkeypatch.setattr(store, "fetch_version_rows", _fetch)
    monkeypatch.setattr(store, "memory_rows_by_identity_keys", _live_heads)
    monkeypatch.setattr(store, "insert_embedding_rows", _stubs)

    async def _fence(items: list[tuple[Any, int]], *, model: str, **_: Any) -> int:
        seen["fenced"].extend(items)
        return len(items)

    monkeypatch.setattr(store, "retire_vectors_for_keys", _retire)
    monkeypatch.setattr(store, "fence_vectors", _fence)

    async def _no_chunks(**_k: Any) -> int:
        return 0

    from server.memory.app import high_fence

    monkeypatch.setattr(high_fence, "tombstone_set_chunks", _no_chunks)
    monkeypatch.setattr("server.memory.store.lifecycle.insert_memory_versions", _memory)
    return seen


async def test_unembedded_rows_get_no_stub_and_their_vectors_are_retired(
    calls: dict[str, list[Any]],
) -> None:
    plan = RollbackPlan(
        reinstate=[
            _action("plain", "reinstate", 10),
            _action("secret", "reinstate", 11),
            _action("ep", "reinstate", 12),
        ],
        remove=[_action("gone_high", "remove", 20), _action("gone", "remove", 21)],
    )
    await rb.apply_rollback(
        "test", "personal", plan, applied_at=_NOW, ch=object(), applied=rb.Applied()
    )

    assert sorted(r["memory_key"] for r in calls["stubs"]) == ["gone", "plain"]
    assert not any("sk-proj" in r["content_excerpt"] for r in calls["stubs"])
    assert sorted(ident[5] for ident, _ in calls["retired"]) == ["ep", "gone_high", "secret"]


async def test_a_version_held_redacted_comes_back_with_no_stub_and_its_vectors_retired(
    calls: dict[str, list[Any]],
) -> None:
    calls["historical"][:] = [_row("plain", 10, value="[redacted]", redacted_at=_NOW)]
    plan = RollbackPlan(reinstate=[_action("plain", "reinstate", 10)], remove=[])
    await rb.apply_rollback(
        "test", "personal", plan, applied_at=_NOW, ch=object(), applied=rb.Applied()
    )

    assert calls["stubs"] == []
    assert [ident[5] for ident, _ in calls["retired"]] == ["plain"]
    assert [r["memory_key"] for r in calls["memory"]] == ["plain"]


async def test_vectors_are_retired_in_one_call_a_batch_at_each_rows_own_version(
    calls: dict[str, list[Any]],
) -> None:
    # One retire a batch (entity vectors too), pinned to the version of the memory row
    # that replaces each key, so a concurrent newer embedded write keeps its vectors.
    plan = RollbackPlan(
        reinstate=[_action("secret", "reinstate", 11), _action("ep", "reinstate", 12)],
        remove=[_action("gone_high", "remove", 20)],
    )
    await rb.apply_rollback(
        "test", "personal", plan, applied_at=_NOW, ch=object(), applied=rb.Applied()
    )

    assert len(calls["retire_calls"]) == 2  # the reinstated batch, then the removed one
    written = {r["memory_key"]: r["version"] for r in calls["memory"]}
    assert {ident[5]: v for ident, v in calls["retired"]} == {
        k: written[k] for k in ("secret", "ep", "gone_high")
    }
    assert all(
        ident[:5] == ("test", "personal", "cursor", "-", "-") for ident, _ in calls["retired"]
    )
    # the high keys are fenced at the same versions: a plain write still embedding
    # lands below them
    assert {ident[5]: v for ident, v in calls["fenced"]} == {
        k: written[k] for k in ("secret", "gone_high")
    }


async def test_a_reinstated_stub_carries_its_memory_rows_version(
    calls: dict[str, list[Any]],
) -> None:
    """A stub drawn after the memory row's version would sit above a concurrent high
    write's fence; it takes the memory row's own version instead."""
    plan = RollbackPlan(reinstate=[_action("plain", "reinstate", 10)], remove=[])
    await rb.apply_rollback(
        "test", "personal", plan, applied_at=_NOW, ch=object(), applied=rb.Applied()
    )
    [stub] = calls["stubs"]
    [mem] = calls["memory"]
    assert stub["version"] == mem["version"]


@pytest.mark.parametrize("deleted", [False, True])
async def test_a_reinstated_version_goes_above_a_head_ahead_of_the_clock(
    calls: dict[str, list[Any]], monkeypatch: pytest.MonkeyPatch, deleted: bool
) -> None:
    """A head a replica whose clock runs ahead wrote, a delete included, read when the
    rollback applies, is outranked, so the rollback lands."""
    ahead = store.next_version() + 10**9

    async def _heads(keys: list[Any], **kw: Any) -> dict[Any, MemoryRow]:
        if not keys:
            return {}  # no key to remove
        # current, a delete included: a head written moments ago on another replica
        assert kw["include_tombstoned"] is True and kw["sequential"] is True
        gone = {"tombstoned_at": _NOW} if deleted else {}
        return {k: _row(k[-1], ahead, **gone) for k in keys}

    monkeypatch.setattr(store, "memory_rows_by_identity_keys", _heads)
    plan = RollbackPlan(reinstate=[_action("plain", "reinstate", 10)], remove=[])
    await rb.apply_rollback(
        "test", "personal", plan, applied_at=_NOW, ch=object(), applied=rb.Applied()
    )
    [stub] = calls["stubs"]
    [mem] = calls["memory"]
    assert mem["version"] == stub["version"] == ahead + 2


async def test_a_key_erased_since_the_plan_is_not_brought_back(
    calls: dict[str, list[Any]], monkeypatch: pytest.MonkeyPatch
) -> None:
    async def _none(keys: list[Any], **_: Any) -> dict[Any, MemoryRow]:
        return {}

    monkeypatch.setattr(store, "memory_rows_by_identity_keys", _none)
    plan = RollbackPlan(reinstate=[_action("plain", "reinstate", 10)], remove=[])
    applied = rb.Applied()
    await rb.apply_rollback("test", "personal", plan, applied_at=_NOW, ch=object(), applied=applied)
    assert (applied.reinstated, applied.removed) == (0, 0)
    assert calls["memory"] == [] and calls["stubs"] == []


async def test_under_postgres_the_publish_retires_the_old_vectors(
    calls: dict[str, list[Any]], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The commit marks them (`OLD_VECTORS`) and the publish retires them with no text,
    so the rollback does not copy them into tombstones again; the high fence stays."""
    over: list[Any] = []

    async def _over_heads(
        rows: list[Any],
        read: list[Any],
        *,
        ch: Any,
        then: Any,
        landed: Any,
        on_commit: Any,
        **_: Any,
    ) -> int:
        over.append([h.version for h in read])  # the heads the commit rechecks
        on_commit()
        answer = await then()
        await landed()
        return answer

    _authority_holds(monkeypatch, calls)
    monkeypatch.setattr(store, "insert_versions_over_heads", _over_heads)
    plan = RollbackPlan(reinstate=[_action("secret", "reinstate", 11)], remove=[])
    applied = rb.Applied()
    await rb.apply_rollback("test", "personal", plan, applied_at=_NOW, ch=object(), applied=applied)
    assert (applied.reinstated, applied.removed) == (1, 0) and over == [[30]]
    assert calls["retire_calls"] == [] and len(calls["fenced"]) == 1


async def test_under_postgres_a_batch_whose_follow_ups_fail_is_still_fenced(
    calls: dict[str, list[Any]], monkeypatch: pytest.MonkeyPatch
) -> None:
    async def _over_heads(rows: list[Any], read: list[Any], *, on_commit: Any, **_: Any) -> int:
        on_commit()
        raise ConnectionError("clickhouse refused the stubs")

    _authority_holds(monkeypatch, calls)
    monkeypatch.setattr(store, "insert_versions_over_heads", _over_heads)
    plan = RollbackPlan(reinstate=[_action("secret", "reinstate", 11)], remove=[])
    with pytest.raises(ConnectionError):
        await rb.apply_rollback(
            "test", "personal", plan, applied_at=_NOW, ch=object(), applied=rb.Applied()
        )
    assert [ident[5] for ident, _ in calls["fenced"]] == ["secret"]


async def test_under_postgres_a_batch_whose_commit_answer_was_lost_is_fenced(
    calls: dict[str, list[Any]], monkeypatch: pytest.MonkeyPatch
) -> None:
    from server.core import pg

    written: list[Any] = []

    async def _over_heads(rows: list[Any], read: list[Any], **_: Any) -> int:
        written.extend(rows)
        raise pg.PostgresUnavailableError("lost", outcome_unknown=True)

    _authority_holds(monkeypatch, calls)
    monkeypatch.setattr(store, "insert_versions_over_heads", _over_heads)
    plan = RollbackPlan(reinstate=[_action("secret", "reinstate", 11)], remove=[])
    with pytest.raises(pg.PostgresUnavailableError):
        await rb.apply_rollback(
            "test", "personal", plan, applied_at=_NOW, ch=object(), applied=rb.Applied()
        )
    assert calls["fenced"] == [(store.identity_key(written[0]), int(written[0]["version"]))]


async def test_on_clickhouse_a_failed_retirement_still_fences_the_high_keys(
    calls: dict[str, list[Any]], monkeypatch: pytest.MonkeyPatch
) -> None:
    async def _refused(*_a: Any, **_k: Any) -> int:
        raise ConnectionError("clickhouse refused the retirement")

    monkeypatch.setattr(store, "retire_vectors_for_keys", _refused)
    plan = RollbackPlan(reinstate=[_action("secret", "reinstate", 11)], remove=[])
    with pytest.raises(ConnectionError):
        await rb.apply_rollback(
            "test", "personal", plan, applied_at=_NOW, ch=object(), applied=rb.Applied()
        )
    assert [ident[5] for ident, _ in calls["fenced"]] == ["secret"]


async def test_a_swept_version_comes_back_with_the_sweeps_marker(
    calls: dict[str, list[Any]], monkeypatch: pytest.MonkeyPatch
) -> None:
    # The key's row at T was a blacklist sweep's tombstone: the rollback writes it as it
    # was, so the write fence still reads it as a sweep, not a delete.
    swept = _row("swept", 13, tombstoned_at=_NOW, expire_reason="blacklist.sweep:p1")

    async def _fetch(org: str, ns: str, pairs: list[Any], **_: Any) -> list[MemoryRow]:
        return [swept]

    monkeypatch.setattr(store, "fetch_version_rows", _fetch)
    plan = RollbackPlan(reinstate=[_action("swept", "reinstate", 13)], remove=[])
    await rb.apply_rollback(
        "test", "personal", plan, applied_at=_NOW, ch=object(), applied=rb.Applied()
    )
    (row,) = calls["memory"]
    assert row["expire_reason"] == "blacklist.sweep:p1" and row["tombstoned_at"] == _NOW


async def test_a_live_version_an_older_restore_stamped_comes_back_without_the_marker(
    calls: dict[str, list[Any]], monkeypatch: pytest.MonkeyPatch
) -> None:
    # An older build's memory.restore copied the sweep's marker onto the live row it brought
    # back; the marker belongs only on the sweep's own tombstone.
    restored = _row("swept", 14, expire_reason="blacklist.sweep:p1")

    async def _fetch(org: str, ns: str, pairs: list[Any], **_: Any) -> list[MemoryRow]:
        return [restored]

    monkeypatch.setattr(store, "fetch_version_rows", _fetch)
    plan = RollbackPlan(reinstate=[_action("swept", "reinstate", 14)], remove=[])
    await rb.apply_rollback(
        "test", "personal", plan, applied_at=_NOW, ch=object(), applied=rb.Applied()
    )
    (row,) = calls["memory"]
    assert row["expire_reason"] == "" and row["tombstoned_at"] is None


@pytest.mark.parametrize(
    ("tombstoned", "trace"),
    [(True, "memory.delete_version:12"), (False, "")],
)
async def test_a_delete_version_marker_comes_back_only_on_its_tombstone(
    calls: dict[str, list[Any]], monkeypatch: pytest.MonkeyPatch, tombstoned: bool, trace: str
) -> None:
    # A delete_version tombstone written back keeps its marker, so the write fence still
    # reads it as one; a live row that carried the marker comes back without it.
    version = _row(
        "dv",
        15,
        tombstoned_at=_NOW if tombstoned else None,
        source_trace="memory.delete_version:12",
    )

    async def _fetch(org: str, ns: str, pairs: list[Any], **_: Any) -> list[MemoryRow]:
        return [version]

    monkeypatch.setattr(store, "fetch_version_rows", _fetch)
    plan = RollbackPlan(reinstate=[_action("dv", "reinstate", 15)], remove=[])
    await rb.apply_rollback(
        "test", "personal", plan, applied_at=_NOW, ch=object(), applied=rb.Applied()
    )
    (row,) = calls["memory"]
    assert row["source_trace"] == trace


async def test_a_removed_key_is_tombstoned_above_a_head_ahead_of_the_clock(
    calls: dict[str, list[Any]], monkeypatch: pytest.MonkeyPatch
) -> None:
    ahead = store.next_version() + 10**9

    async def _heads(keys: list[Any], **kw: Any) -> dict[Any, MemoryRow]:
        assert kw["sequential"] is True  # current: a head written moments ago elsewhere
        return {k: _row(k[-1], ahead) for k in keys}

    monkeypatch.setattr(store, "memory_rows_by_identity_keys", _heads)
    plan = RollbackPlan(reinstate=[], remove=[_action("born", "remove", 0)])
    applied = rb.Applied()
    await rb.apply_rollback("test", "personal", plan, applied_at=_NOW, ch=object(), applied=applied)
    assert (applied.reinstated, applied.removed) == (0, 1)
    [mem] = calls["memory"]
    assert mem["version"] == ahead + 2 and mem["tombstoned_at"] == _NOW


def _authority_holds(monkeypatch: pytest.MonkeyPatch, calls: dict[str, list[Any]]) -> None:
    """Under Postgres the reinstated rows come from the revision journal: it holds the
    fixture's historical rows."""
    from server.memory.store import pg_memory

    async def _revisions_at(org: str, ns: str, read: list[Any]) -> dict[Any, Any]:
        held = {(str(h.memory_id), h.version): h for h in calls["historical"]}
        wanted = [(str(r.memory_id), r.version) for r in read]
        return {k: held[k] for k in wanted if k in held}

    monkeypatch.setattr("server.core.pg.enabled", lambda: True)
    monkeypatch.setattr(pg_memory, "revisions_at", _revisions_at)


async def _announced_into(monkeypatch: pytest.MonkeyPatch) -> list[Any]:
    announced: list[Any] = []

    async def _announce(events: list[Any]) -> None:
        announced.extend(e for e, _row in events)

    monkeypatch.setattr(rb.webhooks, "announce", _announce)
    return announced


async def test_under_postgres_a_head_moved_since_its_read_refuses_the_batch(
    calls: dict[str, list[Any]], monkeypatch: pytest.MonkeyPatch
) -> None:
    """503 ADMISSION_CONTENDED with nothing written or announced for the batch."""
    from server.memory.store import pg_memory, pg_publish

    async def _moved(*_a: Any, **_k: Any) -> Any:
        raise pg_memory.HeadMoved()

    announced = await _announced_into(monkeypatch)
    _authority_holds(monkeypatch, calls)
    monkeypatch.setattr(pg_publish, "commit", _moved)
    plan = RollbackPlan(reinstate=[_action("plain", "reinstate", 10)], remove=[])
    with pytest.raises(pg_memory.AdmissionContendedError):
        await rb.apply_rollback(
            "test", "personal", plan, applied_at=_NOW, ch=object(), applied=rb.Applied()
        )
    assert announced == [] and calls["stubs"] == []


async def test_under_postgres_a_reinstated_rows_document_is_owed_until_its_stub_lands(
    calls: dict[str, list[Any]], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The commit owes the search document of the embedded row it reinstates, so the
    delivery worker builds one a lease cut off; the stub's insert settles it."""
    from server.memory.store import pg_memory, pg_publish

    owed: list[Any] = []
    settled: list[Any] = []

    async def _commit(rows: list[Any], *, targets: Any, **_: Any) -> Any:
        owed.extend(targets)
        seqs = [("test", "personal", i) for i in range(len(rows))]
        return pg_memory.Committed(
            rows=rows, audits=[], seqs=seqs, targets=list(targets), deadline=time.monotonic() + 60
        )

    async def _published(*_a: Any, **_k: Any) -> None:
        return None

    async def _complete(seqs: list[Any], done: int, **_: Any) -> None:
        settled.append((list(seqs), done, list(calls["stubs"])))

    _authority_holds(monkeypatch, calls)
    monkeypatch.setattr(pg_publish, "commit", _commit)
    monkeypatch.setattr(pg_publish, "publish", _published)
    monkeypatch.setattr(pg_memory, "complete", _complete)
    plan = RollbackPlan(
        reinstate=[_action("plain", "reinstate", 10), _action("secret", "reinstate", 11)],
        remove=[],
    )
    await rb.apply_rollback(
        "test", "personal", plan, applied_at=_NOW, ch=object(), applied=rb.Applied()
    )
    plain = pg_memory.MEMORY_ROW | pg_memory.AUDIT_ROW
    assert owed == [plain | pg_memory.SEARCH_DOC, plain]  # 'secret' is high: no document
    [(seqs, done, stubs_then)] = settled
    assert seqs == [("test", "personal", 0)] and done == pg_memory.SEARCH_DOC
    assert [s["memory_key"] for s in stubs_then] == ["plain"]  # after the stub landed


async def test_a_batch_whose_stub_fails_is_still_announced(
    calls: dict[str, list[Any]], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The rows are in: their webhook events, and the count the op audits, are not lost
    to a later step's failure."""
    announced = await _announced_into(monkeypatch)

    async def _refused(*_a: Any, **_k: Any) -> None:
        raise ConnectionError("clickhouse refused the stubs")

    monkeypatch.setattr(store, "insert_embedding_rows", _refused)
    plan = RollbackPlan(reinstate=[_action("plain", "reinstate", 10)], remove=[])
    applied = rb.Applied()
    with pytest.raises(ConnectionError):
        await rb.apply_rollback(
            "test", "personal", plan, applied_at=_NOW, ch=object(), applied=applied
        )
    assert announced == ["memory.revised"] and len(calls["memory"]) == 1
    assert applied.reinstated == 1


async def test_under_postgres_a_version_redacted_since_comes_back_as_the_authority_holds_it(
    calls: dict[str, list[Any]], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A redaction scrubs Postgres before ClickHouse's copies: the rollback reinstates
    the scrubbed revision, never the text ClickHouse still serves while the scrub waits."""
    from server.memory.store import pg_memory

    async def _over_heads(
        rows: list[Any], read: list[Any], *, ch: Any, then: Any, landed: Any, **_: Any
    ) -> int:
        calls["memory"].extend(rows)
        await landed()
        return await then()

    _authority_holds(monkeypatch, calls)
    plain = next(h for h in calls["historical"] if h.memory_key == "plain")
    scrubbed = plain.model_copy(update={"value": "[REDACTED]", "redacted_at": _NOW})

    async def _revisions_at(org: str, ns: str, read: list[Any]) -> dict[Any, Any]:
        return {(str(plain.memory_id), plain.version): scrubbed}

    monkeypatch.setattr(pg_memory, "revisions_at", _revisions_at)
    monkeypatch.setattr(store, "insert_versions_over_heads", _over_heads)
    plan = RollbackPlan(reinstate=[_action("plain", "reinstate", 10)], remove=[])
    await rb.apply_rollback(
        "test", "personal", plan, applied_at=_NOW, ch=object(), applied=rb.Applied()
    )
    [row] = calls["memory"]
    assert "a value" not in str(row["value"]) and row["redacted_at"] is not None


async def test_under_postgres_a_batch_whose_lease_ran_out_is_announced_and_left_owed(
    calls: dict[str, list[Any]], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The follow-ups never start: the batch is announced all the same, and its document
    stays owed for the delivery worker."""
    from server.memory.store import pg_memory, pg_publish

    settled: list[Any] = []

    async def _commit(rows: list[Any], *, targets: Any, **_: Any) -> Any:
        seqs = [("test", "personal", i) for i in range(len(rows))]
        return pg_memory.Committed(
            rows=rows, audits=[], seqs=seqs, targets=list(targets), deadline=time.monotonic() - 1
        )

    async def _published(*_a: Any, **_k: Any) -> None:
        return None

    async def _complete(seqs: list[Any], done: int, **_: Any) -> None:
        settled.append(seqs)

    announced = await _announced_into(monkeypatch)
    _authority_holds(monkeypatch, calls)
    monkeypatch.setattr(pg_publish, "commit", _commit)
    monkeypatch.setattr(pg_publish, "publish", _published)
    monkeypatch.setattr(pg_memory, "complete", _complete)
    plan = RollbackPlan(reinstate=[_action("plain", "reinstate", 10)], remove=[])
    await rb.apply_rollback(
        "test", "personal", plan, applied_at=_NOW, ch=object(), applied=rb.Applied()
    )
    assert announced == ["memory.revised"] and calls["stubs"] == [] and settled == []


async def test_under_postgres_a_reinstated_revision_is_rechecked_as_unredacted(
    calls: dict[str, list[Any]], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The commit refuses a revision a redaction scrubbed since it was read; one the
    authority already holds redacted comes back as it is, with no search document."""
    from server.memory.store import pg_memory

    sources: list[Any] = []

    async def _over_heads(
        rows: list[Any], read: list[Any], *, ch: Any, then: Any, landed: Any, **kw: Any
    ) -> int:
        sources.append(list(kw["sources"]))
        await landed()
        return await then()

    _authority_holds(monkeypatch, calls)
    plain = next(h for h in calls["historical"] if h.memory_key == "plain")
    ep = next(h for h in calls["historical"] if h.memory_key == "ep")

    async def _revisions_at(org: str, ns: str, read: list[Any]) -> dict[Any, Any]:
        scrubbed = plain.model_copy(update={"value": "[REDACTED]", "redacted_at": _NOW})
        return {
            (str(plain.memory_id), plain.version): scrubbed,
            (str(ep.memory_id), ep.version): ep,
        }

    monkeypatch.setattr(pg_memory, "revisions_at", _revisions_at)
    monkeypatch.setattr(store, "insert_versions_over_heads", _over_heads)
    plan = RollbackPlan(
        reinstate=[_action("plain", "reinstate", 10), _action("ep", "reinstate", 12)], remove=[]
    )
    await rb.apply_rollback(
        "test", "personal", plan, applied_at=_NOW, ch=object(), applied=rb.Applied()
    )
    assert sources == [[(str(ep.memory_id), ep.version)]]
    assert calls["stubs"] == []  # 'plain' is held redacted, 'ep' is not embedded
