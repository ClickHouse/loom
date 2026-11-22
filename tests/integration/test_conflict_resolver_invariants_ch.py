"""The conflict resolver's invariants, against ClickHouse, with a scripted judge that
answers at random (per seed) while memory.set, memory.pin, memory.delete, the Undo
and an erasure request hit the same keys. After each seed:

- no user write or pin is lost: each key holds what its last user write left;
- every retired row has a `memory.auto_supersede` record and can be undone;
- no model action below 0.90; dead flags clear without a model judgement;
- no text of another user or another tenant reaches a prompt;
- nothing is written in an org whose erasure was requested while a call was in flight;
- once the writes stop and a pass drains, the next pass makes no call.
"""

from __future__ import annotations

import asyncio
import json
import random
import re
from typing import Any

import pytest

import tests.integration.test_conflict_resolver_ch as base
from tests.integration._resources import unique_org
from tests.integration.test_conflict_resolver_ch import (
    NS,
    USER,
    _audit,
    _pair,
    _place,
    _request_erasure,
    _set,
    _vec,
)

# The resolver tests' fixtures: a client, and the scripted, org-scoped worker.
ch = base.ch
wired = base.wired

# (older key, newer key, axis): each newer row is owed, near its older one.
_PAIRS = [
    ("office_city", "office_location", 100),
    ("team_lead", "lead_name", 110),
    ("deploy_day", "deploy_day_now", 120),
    ("budget", "budget_next", 130),
]
_SECRET = "the vault code of u2 is 4471"


def _judge(seed: int, calls: list[str], writer: _Writer, in_call: Any) -> Any:
    rng = random.Random(seed)

    async def complete(system: str, user: str, **kw: Any) -> Any:
        from types import SimpleNamespace

        calls.append(user)
        await asyncio.sleep(rng.uniform(0.0, 0.3))
        entries = [
            {
                "peer_id": p,
                "reason": "scripted",
                "same_entity": rng.random() > 0.1,
                "same_attribute": True,
                "verdict": rng.choice(["update", "update", "update", "contradiction", "related"]),
                "confidence": rng.choice([0.6, 0.92, 0.97, 0.99, 0.99]),
            }
            for p in re.findall(r"^\[(p\d+)\]", user, re.M)
        ]
        await in_call(entries)
        # A user write starts as the answer comes back: its version is drawn before the
        # resolver acts, and it lands while or after it acts.
        writer.spawn(re.findall(r"^\[p\d+\] \(key ([^,]+),", user, re.M))
        return SimpleNamespace(text=json.dumps({"peers": entries}))

    return complete


async def _heads(ch: Any, org: str) -> dict[tuple[str, str], dict[str, Any]]:
    rows = await ch.query_all(
        "SELECT memory_key, session_id, toString(memory_id) AS id, toString(value) AS value,"
        " pinned, status, superseded_by, tombstoned_at, version FROM loom.memory FINAL"
        " WHERE org = {org:String} AND namespace = {ns:String}"
        " SETTINGS select_sequential_consistency = 1",
        params={"org": org, "ns": NS},
    )
    return {(str(r["memory_key"]), str(r["session_id"])): dict(r) for r in rows}


def _text(stored: str) -> str:
    try:
        return str(json.loads(stored))
    except ValueError:
        return stored


class _Writer:
    """Random user writes on the pairs' keys (set, pin, delete, Undo), one at a time per
    key, and what each key must hold after them: ("value", text, pinned) or
    ("deleted", None, False)."""

    def __init__(self, ch: Any, org: str, seed: int) -> None:
        self.ch, self.org, self.rng = ch, org, random.Random(seed * 7919)
        self.want: dict[tuple[str, str], tuple[str, Any, bool]] = {}
        self.versions: dict[tuple[str, str], int] = {}
        self.tasks: list[asyncio.Task[None]] = []
        self.locks: dict[tuple[str, str], asyncio.Lock] = {}
        self.steps = 0
        # Closed after the concurrent phase: the later passes' calls start no write.
        self.open = True

    def spawn(self, keys: list[str] | None = None) -> None:
        if self.open:
            self.tasks.append(asyncio.create_task(self.write(keys or [])))

    async def write(self, keys: list[str]) -> None:
        from server.memory.api.models_crud import (
            MemoryDeleteInput,
            MemoryRestoreInput,
            MemorySetInput,
        )
        from server.memory.app.handlers import handle_memory_delete, handle_memory_pin
        from server.memory.app.restore import handle_memory_restore
        from server.memory.app.write import handle_memory_set

        self.steps += 1
        step = self.steps
        sessions = {k: s for old, new, _ in _PAIRS for k, s in ((old, "s1"), (new, "s2"))}
        key = self.rng.choice([k for k in keys if k in sessions] or sorted(sessions))
        session = sessions[key]
        op = self.rng.choice(["set", "set", "pin", "delete", "undo"])
        ident = {"org": self.org, "namespace": NS, "agent": "bot", "user_id": USER}
        async with self.locks.setdefault((key, session), asyncio.Lock()):
            head = (await _heads(self.ch, self.org)).get((key, session))
            if op == "set" and not (head is not None and head["pinned"]):
                # A pinned key refuses a write that does not pin.
                value = f"{key} changed by the user at step {step}"
                out = await handle_memory_set(
                    MemorySetInput(**ident, session_id=session, memory_key=key, value=value)
                )
                self.wrote(key, session, out.version, ("value", value, False))
            elif op == "pin" and head is not None and head["tombstoned_at"] is None:
                value = _text(head["value"])
                from server.core.errors import ValidationError

                try:
                    out = await handle_memory_pin(
                        MemorySetInput(**ident, session_id=session, memory_key=key, value=value)
                    )
                except ValidationError as error:
                    if error.extra.get("reason") != "forgotten":
                        raise
                else:
                    self.wrote(key, session, out.version, ("value", value, True))
            elif op == "delete":
                from server.memory.store.pg_memory import AdmissionContendedError

                request = MemoryDeleteInput(**ident, session_id=session, memory_key=key)
                for attempt in range(3):
                    try:
                        gone = await handle_memory_delete(request)
                    except AdmissionContendedError:
                        if attempt == 2:
                            raise
                        # Retry the same logical delete from a fresh head after its 503 hint.
                        await asyncio.sleep(1)
                    else:
                        if gone.tombstoned:
                            self.wrote(key, session, gone.version, ("deleted", None, False))
                        break
            elif op == "undo" and head is not None and head["status"] == "superseded":
                await handle_memory_restore(
                    MemoryRestoreInput(
                        **ident,
                        session_id=session,
                        memory_key=key,
                        if_superseded_by=str(head["superseded_by"]),
                    )
                )

    def wrote(self, key: str, session: str, version: int, want: tuple[str, Any, bool]) -> None:
        """The user's write that must hold: the one at the highest version."""
        if version > self.versions.get((key, session), 0):
            self.versions[(key, session)] = version
            self.want[(key, session)] = want

    async def run(self, n: int) -> None:
        for _ in range(n):
            await asyncio.sleep(self.rng.uniform(0.0, 0.2))
            self.spawn()
        while not all(t.done() for t in self.tasks):
            await asyncio.gather(*self.tasks)


async def _settle_resolver(run_pass: Any, calls: list[str], max_attempts: int) -> None:
    """Allow configured post-answer attempts, then require a quiet confirming pass."""
    assert max_attempts >= 1
    outcomes = []
    for _ in range(max_attempts + 1):
        before = len(calls)
        outcomes.append(await run_pass())
        if len(calls) == before:
            return
    raise AssertionError(f"resolver did not settle within {max_attempts} attempts: {outcomes}")


class _DeleteBarrier:
    """Pause the first tombstone after its read and observe only that delete task."""

    def __init__(self, monkeypatch: Any) -> None:
        from server.memory.app import handlers
        from server.memory.store import refusals

        self.read, self.resume = asyncio.Event(), asyncio.Event()
        self.attempts = 0
        self.refused = 0
        self.recorded = 0
        self.deleting: asyncio.Task[Any] | None = None
        self._tombstone = handlers._write_tombstone_row
        self._record = refusals.record_clickhouse
        monkeypatch.setattr(handlers, "_write_tombstone_row", self.tombstone)
        monkeypatch.setattr(refusals, "record_clickhouse", self.record)

    async def tombstone(self, *args: Any, **kwargs: Any) -> None:
        from server.memory.store.pg_memory import AdmissionContendedError

        self.attempts += 1
        if self.attempts == 1:
            self.read.set()
            await self.resume.wait()
        try:
            await self._tombstone(*args, **kwargs)
        except AdmissionContendedError:
            self.refused += 1
            raise

    async def record(self, *args: Any, **kwargs: Any) -> None:
        if asyncio.current_task() is self.deleting:
            self.recorded += 1
        await self._record(*args, **kwargs)


@pytest.mark.parametrize("retry", [False, True])
async def test_delete_contention_refuses_stale_head_and_writer_retries(
    ch, wired, monkeypatch, retry: bool
) -> None:
    from server.memory.api.models_crud import MemoryDeleteInput
    from server.memory.app.handlers import handle_memory_delete
    from server.memory.store.pg_memory import AdmissionContendedError

    org, _ = wired
    key, session = "office_city", "s1"
    await _set(org, session, key, "before competing write")
    barrier = _DeleteBarrier(monkeypatch)
    writer = _Writer(ch, org, 2)
    monkeypatch.setattr(
        writer.rng, "choice", lambda choices: "delete" if "delete" in choices else key
    )
    request = MemoryDeleteInput(
        org=org, namespace=NS, agent="bot", user_id=USER, session_id=session, memory_key=key
    )
    deleting = barrier.deleting = asyncio.create_task(
        writer.write([key]) if retry else handle_memory_delete(request)
    )
    try:
        async with asyncio.timeout(20):
            await barrier.read.wait()
            await _set(org, session, key, "after competing write")
            competing = (await _heads(ch, org))[(key, session)]
            barrier.resume.set()
            if retry:
                await deleting
            else:
                with pytest.raises(AdmissionContendedError) as raised:
                    await deleting
                assert raised.value.code == "ADMISSION_CONTENDED"
                assert raised.value.extra["retry_after_s"] == 1
    finally:
        barrier.resume.set()
        if not deleting.done():
            deleting.cancel()
        await asyncio.gather(deleting, return_exceptions=True)
    assert barrier.refused == 1
    head = (await _heads(ch, org))[(key, session)]
    if retry:
        assert barrier.attempts == 2 and barrier.recorded == 1
        assert head["tombstoned_at"] is not None and _text(head["value"]) == "after competing write"
        assert head["version"] > competing["version"]
        assert writer.steps == 1
        assert writer.want == {(key, session): ("deleted", None, False)}
        assert writer.versions == {(key, session): head["version"]}
    else:
        assert barrier.attempts == 1 and barrier.recorded == 0
        assert head == competing
        assert not writer.want and writer.steps == 0


@pytest.mark.parametrize("stamp_mode", ["write", "raise", "no_write"])
async def test_a_fixed_refused_answer_settles_only_after_its_terminal_stamp(
    ch, wired, monkeypatch, stamp_mode: str
) -> None:
    from types import SimpleNamespace

    from server.core.errors import ValidationError
    from server.memory.app import conflict_resolver as resolver
    from server.memory.store import conflict_resolver as queue
    from server.memory.workers import conflict_resolver as worker
    from server.settings import settings

    org, llm = wired
    limit = settings.memory_conflict_resolver_max_attempts
    calls: list[str] = []
    attempts: list[int] = []
    stamp_counts: list[int] = []
    stamped: list[tuple[str, ...]] = []
    real_stamp = queue.stamp_judged

    async def judge(system: str, user: str, **kwargs: Any) -> Any:
        calls.append(user)
        peers = [
            {
                "peer_id": peer,
                "reason": "scripted",
                "same_entity": True,
                "same_attribute": True,
                "verdict": "update",
                "confidence": 0.99,
            }
            for peer in re.findall(r"^\[(p\d+)\]", user, re.M)
        ]
        return SimpleNamespace(text=json.dumps({"peers": peers}))

    async def refuse(*args: Any, **kwargs: Any) -> None:
        # The real judge has answered; the unchanged row's action is refused.
        raise ValidationError("The value is refused by a previous deletion", reason="forgotten")

    async def observe_stamp(row: Any) -> None:
        stamp_counts.append(worker._state.attempts[worker._attempt_key(row)][0])
        if stamp_mode == "raise":
            raise OSError("Synthetic terminal stamp refusal")
        if stamp_mode == "write":
            await real_stamp(row)
            stamped.append(worker._attempt_key(row))

    async def observed_pass() -> dict[str, Any]:
        outcome = await worker.run_pass()
        attempts.append(max((count for count, _ in worker._state.attempts.values()), default=0))
        return outcome

    monkeypatch.setattr(llm, "complete", judge)
    monkeypatch.setattr(resolver, "_act_on_verdicts", refuse)
    monkeypatch.setattr(queue, "stamp_judged", observe_stamp)
    await _pair(ch, org, "refused_old", "refused_new", 140)
    before = await _heads(ch, org)
    if stamp_mode == "write":
        await _settle_resolver(observed_pass, calls, limit)
        assert len(calls) == limit and len(stamped) == 1
        assert stamp_counts == [limit]
        assert attempts == [*range(1, limit), 0, 0]
        assert not await queue.owed_rows(limit=10)
        drained = len(calls)
        await worker.run_pass()
        assert len(calls) == drained, "the terminal stamp stops further judge calls"
    else:
        with pytest.raises(AssertionError, match="did not settle"):
            await _settle_resolver(observed_pass, calls, limit)
        assert len(calls) == limit + 1
        assert not stamped and await queue.owed_rows(limit=10)
        assert stamp_counts == ([limit, limit + 1] if stamp_mode == "raise" else [limit])
    assert await _heads(ch, org) == before, "the refused action must not change either head"


# Every table a resolver write lands in.
_WRITTEN = (
    "memory",
    "memory_versions",
    "memory_embeddings",
    "operator_edges",
    "audit_log",
    "memory_projection_failures",
)


async def _stored(ch: Any, org: str) -> dict[str, set[int]]:
    """A hash of every stored row of the org, per table. A merge only drops rows, so a
    row the snapshot lacks is a write made after it."""
    out: dict[str, set[int]] = {}
    for table in _WRITTEN:
        rows = await ch.query_all(
            f"SELECT cityHash64(formatRow('TSV', *)) AS h FROM loom.{table} WHERE org = {{org:String}}"
            " SETTINGS select_sequential_consistency = 1",
            params={"org": org},
        )
        out[table] = {int(r["h"]) for r in rows}
    return out


@pytest.mark.parametrize("seed", [1, 2, 3])
async def test_the_resolver_keeps_its_invariants_under_concurrent_writes(
    ch, wired, monkeypatch, seed: int
) -> None:
    from server.memory.api.models_crud import MemoryRestoreInput
    from server.memory.app import after_write, write
    from server.memory.app.restore import handle_memory_restore
    from server.memory.store import conflict_resolver as queue
    from server.memory.workers.conflict_resolver import run_pass
    from server.settings import settings

    org, llm = wired
    calls: list[str] = []
    writer = _Writer(ch, org, seed)
    erasing = seed % 3 == 0
    stored: dict[str, set[int]] = {}
    dead_peers: set[str] = set()
    read_dead_flags = queue.dead_flags

    async def observed_dead_flags(
        read_org: str,
        namespace: str,
        identity: tuple[str, str, str, str],
        *,
        ch: Any = None,
    ) -> list[str]:
        peers = await read_dead_flags(read_org, namespace, identity, ch=ch)
        if (read_org, namespace) == (org, NS):
            dead_peers.update(peers)
        return peers

    monkeypatch.setattr(queue, "dead_flags", observed_dead_flags)

    async def in_call(entries: list[dict[str, Any]]) -> None:
        acts = any(e["confidence"] >= 0.9 and e["same_entity"] for e in entries)
        if erasing and acts and not stored:
            # The erasure is requested while a call whose answer acts is in flight:
            # from here on, nothing the resolver does may write in the org. The user's
            # writes stop first, so none changes the pairs this answer acts on.
            writer.open = False
            await writer.run(0)
            await after_write.drain()
            stored.update(await _stored(ch, org))
            await _request_erasure(ch, org)

    if erasing:
        # One row at a time: no other row's write is in flight beside the call.
        monkeypatch.setattr(settings, "memory_conflict_resolver_concurrency", 1)
    monkeypatch.setattr(llm, "complete", _judge(seed, calls, writer, in_call))
    # A point write draws its version first and inserts after its embedding: a slow
    # embedding is the window in which the resolver can act on the old row.
    embed, slow = write._set_embed, random.Random(seed * 31)

    async def slow_embed(w: Any, embedder: Any = None) -> Any:
        await asyncio.sleep(slow.uniform(0.0, 0.5))
        return await embed(w, embedder)

    monkeypatch.setattr(write, "_set_embed", slow_embed)
    for old, new, axis in _PAIRS:
        await _pair(ch, org, old, new, axis, gap=old != "budget")
    # Another user's row, and another tenant's, right next to the first pair.
    await _set(org, "s9", "office_u2", _SECRET, user="u2")
    await _place(ch, org, "s9", "office_u2", _vec((100, 1.0), (101, 0.01)), owed=False, user="u2")
    other = unique_org("resolver-other")
    await _set(other, "s1", "office_city", _SECRET)
    await _place(ch, other, "s1", "office_city", _vec((100, 1.0)), owed=True)

    resolving = asyncio.create_task(run_pass())
    await writer.run(6)
    await resolving
    await writer.run(0)
    writer.open = False
    want = writer.want
    await _settle_resolver(run_pass, calls, settings.memory_conflict_resolver_max_attempts)
    drained = len(calls)
    await run_pass()
    assert len(calls) == drained, "a pass after the queue drained makes no call"
    if erasing:
        assert stored, "the erasure was requested during a call"
        after = await _stored(ch, org)
        assert {t: after[t] - stored[t] for t in _WRITTEN} == {t: set() for t in _WRITTEN}

    heads = await _heads(ch, org)
    for (key, session), (kind, value, pinned) in want.items():
        head = heads[(key, session)]
        if kind == "deleted":
            assert head["tombstoned_at"] is not None, (key, session)
            continue
        assert head["tombstoned_at"] is None and _text(head["value"]) == value, (key, head)
        if pinned:
            assert head["pinned"] and head["status"] != "superseded", (key, head)

    assert not any(_SECRET in prompt for prompt in calls), "another user's or tenant's text"
    for record in await _audit(ch, org, "memory.conflict_resolver"):
        assert record["pairs"], record
        if record["model"]:
            assert all(p["confidence"] >= 0.90 for p in record["pairs"]), record
        else:
            for pair in record["pairs"]:
                assert pair == {
                    "peer_id": pair["peer_id"],
                    "action": "clear",
                    "reason": "peer_not_live",
                }, record
                assert pair["peer_id"] in dead_peers, record

    recorded = {
        e["memory_id"] for r in await _audit(ch, org, "memory.auto_supersede") for e in r["retired"]
    }
    for (key, session), head in heads.items():
        if head["status"] != "superseded" or head["tombstoned_at"] is not None:
            continue
        assert head["id"] in recorded, (key, "retired without a record")
        if erasing:
            continue  # the org is under erasure
        undone = await handle_memory_restore(
            MemoryRestoreInput(
                org=org,
                namespace=NS,
                agent="bot",
                user_id=USER,
                session_id=session,
                memory_key=key,
                if_superseded_by=str(head["superseded_by"]),
            )
        )
        assert undone.restored, key
