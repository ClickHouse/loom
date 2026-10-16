"""A deferred conflict judge lost with its replica is re-run by the leader.

The deferred write's embedding row carries the pending mark in
`conflict_checked_at`; the settle stamps it. `memory.get` derives `pending`
from that mark (any replica answers the same), and `conflict_sweep` re-judges
rows still marked past the grace period.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from types import SimpleNamespace as NS
from typing import Any

import pytest

from server.core.datetimes import naive_utc_now
from server.memory.app import conflict_deferred as cd
from server.memory.app import conflicts as cf
from server.memory.domain.conflicts import ConflictPeer, ConflictResult, PendingJudge
from server.memory.store import conflict_marks
from server.memory.workers import conflict_sweep

_P1 = "11111111-1111-1111-1111-111111111111"
_MID = "22222222-2222-2222-2222-222222222222"


def test_checked_at_marks_only_a_deferred_judge() -> None:
    assert cd.checked_at_for(None) is None
    assert cd.checked_at_for(ConflictResult(verdict="", peers=[])) is None
    assert cd.checked_at_for(ConflictResult(verdict="pending", peers=[])) == (
        conflict_marks.PENDING_MARK
    )
    inline = cd.checked_at_for(ConflictResult(verdict="clear", peers=[]))
    assert isinstance(inline, datetime) and not conflict_marks.is_mark(inline)


def _row(**kw: Any) -> NS:
    base = dict(
        org="o",
        namespace="n",
        agent="a",
        user_id="u",
        session_id="-",
        memory_key="k",
        status="active",
        updated_at=naive_utc_now(),
        created_at=naive_utc_now(),
    )
    base.update(kw)
    return NS(**base)


@pytest.fixture
def judge_on(monkeypatch: pytest.MonkeyPatch) -> None:
    from server.settings import settings

    monkeypatch.setattr(settings, "memory_conflict_judge", True)


@pytest.mark.asyncio
@pytest.mark.usefixtures("judge_on")
async def test_pending_is_read_from_the_row_not_process_memory(monkeypatch) -> None:
    asked: list[tuple[str, ...]] = []

    async def has_mark(*key: str, **_: Any) -> bool:
        asked.append(key)
        return True

    monkeypatch.setattr(conflict_marks, "key_has_pending_mark", has_mark)
    cd._reset_for_tests()
    assert await cd.row_pending(_row()) is True, "another replica's judge is visible"
    assert asked == [("o", "n", "a", "u", "-", "k")]
    assert await cd.row_pending(_row(status="conflicted")) is False
    old = naive_utc_now() - timedelta(minutes=10)
    assert await cd.row_pending(_row(updated_at=old)) is False, "an old mark reads as settled"
    assert len(asked) == 1, "neither read touched ClickHouse"


@pytest.mark.asyncio
@pytest.mark.usefixtures("judge_on")
async def test_this_processs_own_judge_answers_without_a_read(monkeypatch) -> None:
    async def has_mark(*_: Any, **__: Any) -> bool:
        raise AssertionError("no ClickHouse read for a judge this process runs")

    monkeypatch.setattr(conflict_marks, "key_has_pending_mark", has_mark)
    cd._reset_for_tests()
    monkeypatch.setitem(cd._PENDING, _MID, 0.0)
    assert await cd.row_pending(_row(memory_id=_MID)) is True


@pytest.mark.asyncio
async def test_with_the_judge_off_nothing_is_pending_and_nothing_is_read(monkeypatch) -> None:
    from server.settings import settings

    async def has_mark(*_: Any, **__: Any) -> bool:
        raise AssertionError("no judge, no mark to read")

    monkeypatch.setattr(settings, "memory_conflict_judge", False)
    monkeypatch.setattr(conflict_marks, "key_has_pending_mark", has_mark)
    assert await cd.row_pending(_row()) is False


@pytest.mark.asyncio
@pytest.mark.usefixtures("judge_on")
async def test_a_failed_pending_read_does_not_fail_memory_get(monkeypatch) -> None:
    async def boom(*_: Any, **__: Any) -> bool:
        raise RuntimeError("down")

    monkeypatch.setattr(conflict_marks, "key_has_pending_mark", boom)
    assert await cd.row_pending(_row()) is False


def test_the_mark_reads_back_as_epoch() -> None:
    assert conflict_marks.is_mark(datetime(1970, 1, 1))
    assert not conflict_marks.is_mark(None)
    assert not conflict_marks.is_mark(naive_utc_now())


_KEY = ("o", "n", "a", "u", "-", "k")
_WRITTEN = datetime(2026, 1, 1, 12, 0, 0, 250_000)


class _Embeddings:
    """memory_embeddings under FINAL for one key: the highest version per
    (scope, embedding_model) wins; the head read answers a normal-sensitivity row."""

    def __init__(self, *rows: dict[str, Any]) -> None:
        self.rows = list(rows)

    async def query_all(self, sql: str, params: Any = None) -> list[dict[str, Any]]:
        if "FROM loom.memory FINAL" in sql:
            return [dict(zip(_COLS, _KEY, strict=True), sensitivity="normal")]
        live: dict[tuple[str, str], dict[str, Any]] = {}
        for r in self.rows:
            slot = (r["scope"], r["embedding_model"])
            if slot not in live or r["version"] > live[slot]["version"]:
                live[slot] = r
        return [dict(r) for r in live.values() if r["tombstoned_at"] is None]

    async def insert(self, table: str, rows: list[dict[str, Any]], settings: Any = None) -> None:
        self.rows += rows

    def head(self) -> dict[str, Any]:
        return max(self.rows, key=lambda r: r["version"])


_COLS = ("org", "namespace", "agent", "user_id", "session_id", "memory_key")


def _vector_row(
    *, version: int, created_at: datetime, model: str = "m", sha: str = ""
) -> dict[str, Any]:
    from server.memory.store.rows import build_embedding_row

    return build_embedding_row(
        **dict(zip(_COLS, _KEY, strict=True)),
        memory_type="semantic",
        scope="private",
        embedding_model=model,
        embedding_dim=2,
        embedding=[0.1, 0.2],
        tombstoned_at=None,
        # what ClickHouse hands back: DateTime keeps whole seconds
        created_at=created_at.replace(microsecond=0),
        conflict_checked_at=conflict_marks.PENDING_MARK,
        version=version,
        content_sha256=sha,
    )


def _head_row() -> NS:
    return NS(**dict(zip(_COLS, _KEY, strict=True)), memory_type="semantic", sensitivity="normal")


async def _touch(ch: _Embeddings, monkeypatch: pytest.MonkeyPatch) -> None:
    from server.memory.store import embeddings

    expiry = {"expires_at": naive_utc_now() + timedelta(days=1)}
    await embeddings.reemit_embedding_rows_for_key(*_KEY, ch=ch, **expiry)
    await embeddings.reemit_embedding_rows_for_keys([(_KEY, expiry)], ch=ch)


async def _verify(ch: _Embeddings, monkeypatch: pytest.MonkeyPatch) -> None:
    from server.memory.app import verify

    monkeypatch.setattr(verify, "get_client", lambda: ch)
    await verify._mirror_verified_to_embeddings(_head_row(), verified=True)


async def _apply(ch: _Embeddings, monkeypatch: pytest.MonkeyPatch) -> None:
    from server.memory.app import apply
    from server.memory.store import embeddings

    monkeypatch.setattr(embeddings, "_default_client", lambda: ch)
    await apply._mirror(_head_row(), confidence=0.4)


@pytest.mark.asyncio
@pytest.mark.parametrize("reemit", [_touch, _verify, _apply], ids=["touch", "verify", "apply"])
async def test_a_settle_stamps_its_write_after_a_metadata_reemit(reemit: Any, monkeypatch) -> None:
    ch = _Embeddings(_vector_row(version=100, created_at=_WRITTEN))
    await reemit(ch, monkeypatch)
    assert ch.head()["version"] > 100 and conflict_marks.is_mark(ch.head()["conflict_checked_at"])
    stamped = await conflict_marks.stamp_checked(*_KEY, version=100, created_at=_WRITTEN, ch=ch)
    assert stamped == 1
    assert not conflict_marks.is_mark(ch.head()["conflict_checked_at"])


@pytest.mark.asyncio
async def test_a_settle_leaves_a_newer_write_still_pending() -> None:
    newer_at = _WRITTEN + timedelta(seconds=3)
    ch = _Embeddings(_vector_row(version=200, created_at=newer_at))
    assert await conflict_marks.stamp_checked(*_KEY, version=100, created_at=_WRITTEN, ch=ch) == 0
    assert len(ch.rows) == 1, "the newer write keeps its pending mark"
    assert await conflict_marks.stamp_checked(*_KEY, version=200, created_at=newer_at, ch=ch) == 1
    assert not conflict_marks.is_mark(ch.head()["conflict_checked_at"])
    assert ch.head()["version"] == 201


@pytest.mark.asyncio
async def test_a_settle_never_stamps_a_newer_revision_written_in_the_same_second() -> None:
    """created_at keeps whole seconds: two revisions of one key in the same second
    are told apart by the content hash their rows carry."""
    ch = _Embeddings(_vector_row(version=200, created_at=_WRITTEN, sha="b" * 64))
    stamp = conflict_marks.stamp_checked
    assert await stamp(*_KEY, version=100, created_at=_WRITTEN, content_sha256="a" * 64, ch=ch) == 0
    assert conflict_marks.is_mark(ch.head()["conflict_checked_at"]), "the newer write still owes"
    assert await stamp(*_KEY, version=200, created_at=_WRITTEN, content_sha256="b" * 64, ch=ch) == 1


# --- the sweep ---------------------------------------------------------------


def _marked() -> dict[str, Any]:
    return {
        "org": "o",
        "namespace": "n",
        "agent": "a",
        "user_id": "u",
        "session_id": "-",
        "memory_key": "k",
        "memory_type": "semantic",
        "embedding": [0.1, 0.2],
        "created_at": naive_utc_now(),
        "version": 7,
        "content_sha256": "a" * 64,
    }


def _mem(status: str = "active") -> NS:
    return NS(
        memory_id=_MID,
        memory_type="semantic",
        status=status,
        run_id="-",
        valid_at=None,
        value_text='"Deploys happen on Thursday"',
        value="Deploys happen on Thursday",
        content_sha256="a" * 64,
    )


def _peer() -> ConflictPeer:
    return ConflictPeer(
        memory_id=_P1,
        memory_key="deploy_day",
        similarity=0.91,
        pinned=False,
        canonical="deploys happen on tuesday",
        value='"Deploys happen on Tuesday"',
        valid_at=None,
    )


@pytest.fixture
def sweep(monkeypatch: pytest.MonkeyPatch) -> NS:
    import server.memory.store as memory_repo

    e = NS(
        mem=_mem(),
        checks=[],
        settled=[],
        applied=[],
        stamped=[],
        scopes=[],
        verdict=None,
        during_judge=None,
    )

    async def rows_by_keys(keys: Any, **_: Any) -> dict[Any, Any]:
        return {tuple(keys[0]): e.mem} if e.mem is not None else {}

    async def check(**kw: Any) -> ConflictResult:
        from server.identity.app.request_scope import request_scope

        e.checks.append(kw)
        e.scopes.append(request_scope())
        if e.during_judge is not None:
            e.mem = e.during_judge
        return e.verdict

    async def settle(ident: Any, pj: Any, *, memory_id: str, version: int, **kw: Any) -> str:
        e.settled.append((ident.memory_key, memory_id, version, kw["content_sha256"]))
        return "conflict"

    async def apply(ident: Any, result: Any, *, memory_id: str, **_: Any) -> str:
        e.applied.append((result.verdict, memory_id))
        return result.verdict

    async def stamp(ident: Any, *, version: int, created_at: datetime, **_: Any) -> int:
        e.stamped.append((ident.memory_key, version))
        return 1

    monkeypatch.setattr(memory_repo, "memory_rows_by_identity_keys", rows_by_keys)
    monkeypatch.setattr(cf, "check_on_commit", check)
    monkeypatch.setattr(cd, "settle", settle)
    monkeypatch.setattr(cd, "apply", apply)
    monkeypatch.setattr(cd, "stamp_checked", stamp)
    return e


@pytest.mark.asyncio
async def test_a_lost_judge_is_run_again_on_the_stored_value_and_vector(sweep: NS) -> None:
    pj = PendingJudge("o", "n", '"Deploys happen on Thursday"', [_peer()], [_peer()], "conflict")
    sweep.verdict = ConflictResult(verdict="pending", peers=[_peer()], pending_judge=pj)
    assert await conflict_sweep.rejudge(_marked(), ch=object()) == "conflict"
    (kw,) = sweep.checks
    assert kw["new_value"] == '"Deploys happen on Thursday"' and kw["new_embedding"] == [0.1, 0.2]
    assert kw["defer_judge"] is True and kw["memory_key"] == "k"
    assert sweep.settled == [("k", _MID, 7, "a" * 64)], "the settle lands it and stamps the row"
    assert sweep.stamped == []
    scope = sweep.scopes[0]
    assert scope.scope == "own_user" and scope.user_id == "u", "never wider than the writer"


@pytest.mark.asyncio
async def test_a_session_scoped_write_is_judged_within_its_session(sweep: NS) -> None:
    """A write checked under own_session saw only its session's peers: the sweep
    must not judge it against the same user's other sessions."""
    sweep.verdict = ConflictResult(verdict="clear", peers=[])
    row = {**_marked(), "session_id": "private-session-1"}
    await conflict_sweep.rejudge(row, ch=object())
    scope = sweep.scopes[0]
    assert (scope.scope, scope.user_id, scope.session_id) == (
        "own_session",
        "u",
        "private-session-1",
    )


@pytest.mark.asyncio
async def test_a_deterministic_verdict_is_applied_and_stamped(sweep: NS) -> None:
    sweep.verdict = ConflictResult(verdict="duplicate", peers=[_peer()], duplicate_of_id=_P1)
    assert await conflict_sweep.rejudge(_marked(), ch=object()) == "duplicate"
    assert sweep.applied == [("duplicate", _MID)] and sweep.stamped == [("k", 7)]


@pytest.mark.asyncio
async def test_a_vector_older_than_its_memory_row_is_not_judged(sweep: NS) -> None:
    """An update landed between the scan and the fetch: the new value with the old
    vector is no write at all. The new write's vector replaces the row."""
    sweep.mem = NS(**{**vars(_mem()), "content_sha256": "b" * 64})
    sweep.verdict = ConflictResult(verdict="clear", peers=[])
    assert await conflict_sweep.rejudge(_marked(), ch=object()) == cd.MOVED
    assert sweep.checks == [] and sweep.applied == [] and sweep.stamped == []


@pytest.mark.asyncio
async def test_a_verdict_is_not_applied_once_an_update_lands_during_the_judge(sweep: NS) -> None:
    sweep.verdict = ConflictResult(verdict="duplicate", peers=[_peer()], duplicate_of_id=_P1)
    sweep.during_judge = NS(**{**vars(_mem()), "content_sha256": "b" * 64})
    assert await conflict_sweep.rejudge(_marked(), ch=object()) == cd.MOVED
    assert len(sweep.checks) == 1 and sweep.applied == [] and sweep.stamped == []


@pytest.mark.asyncio
async def test_a_row_no_longer_live_is_stamped_without_a_check(sweep: NS) -> None:
    sweep.mem = None
    assert await conflict_sweep.rejudge(_marked(), ch=object()) == "gone"
    sweep.mem = _mem(status="conflicted")
    assert await conflict_sweep.rejudge(_marked(), ch=object()) == "gone"
    assert sweep.checks == [] and sweep.stamped == [("k", 7), ("k", 7)]


@pytest.mark.asyncio
async def test_the_pass_reads_marks_past_the_grace_and_survives_a_bad_row(
    sweep: NS, monkeypatch
) -> None:
    seen: dict[str, Any] = {}

    async def stale(**kw: Any) -> list[dict[str, Any]]:
        seen.update(kw)
        return [_marked(), {**_marked(), "memory_key": "bad"}]

    calls = {"n": 0}
    real = conflict_sweep.rejudge

    async def rejudge(r: dict[str, Any], *, ch: Any) -> str:
        calls["n"] += 1
        if r["memory_key"] == "bad":
            raise RuntimeError("judge down")
        return await real(r, ch=ch)

    monkeypatch.setattr(conflict_marks, "stale_pending_marks", stale)
    monkeypatch.setattr(conflict_sweep, "rejudge", rejudge)
    sweep.verdict = ConflictResult(verdict="clear", peers=[])
    out = await conflict_sweep.run_pass(ch=object())
    assert seen["grace_s"] == conflict_marks.GRACE_SECONDS
    assert seen["lookback_s"] == conflict_marks.LOOKBACK_SECONDS
    assert out == {"marked": 2, "verdicts": {"clear": 1}, "errors": 1} and calls["n"] == 2


def test_the_sweep_is_a_registered_worker() -> None:
    from server.workers import registry

    names = [s.name for s in registry.enabled_workers()]
    assert "conflict_sweep" in names
