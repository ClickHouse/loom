"""The conflict resolver's invariants, against ClickHouse, with a scripted judge that
answers at random (per seed) while memory.set, memory.pin, memory.delete, the Undo
and an erasure request hit the same keys. After each seed:

- no user write or pin is lost: each key holds what its last user write left;
- every retired row has a `memory.auto_supersede` record and can be undone;
- no flag, clear or retirement below 0.90;
- no text of another user or another tenant reaches a prompt;
- nothing is written in an org under erasure;
- once the writes stop and a pass drains, the next pass makes no call.
"""

from __future__ import annotations

import asyncio
import json
import random
import re
import uuid
from datetime import UTC, datetime
from typing import Any

import pytest

import tests.integration.test_conflict_resolver_ch as base
from tests.integration._resources import unique_org
from tests.integration.test_conflict_resolver_ch import NS, USER, _audit, _pair, _place, _set, _vec

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


def _judge(seed: int, calls: list[str], writer: _Writer) -> Any:
    rng = random.Random(seed)

    async def complete(system: str, user: str, **kw: Any) -> Any:
        from types import SimpleNamespace

        calls.append(user)
        await asyncio.sleep(rng.uniform(0.0, 0.3))
        # A user write starts as the answer comes back: its version is drawn before the
        # resolver acts, and it lands while or after it acts.
        writer.spawn(re.findall(r"^\[p\d+\] \(key ([^,]+),", user, re.M))
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
        self.tasks: list[asyncio.Task[None]] = []
        self.locks: dict[tuple[str, str], asyncio.Lock] = {}
        self.steps = 0

    def spawn(self, keys: list[str] | None = None) -> None:
        self.tasks.append(asyncio.create_task(self.write(keys or [])))

    async def write(self, keys: list[str]) -> None:
        from server.memory.api.models_crud import (
            MemoryDeleteInput,
            MemoryRestoreInput,
            MemorySetInput,
        )
        from server.memory.app.handlers import handle_memory_delete, handle_memory_pin
        from server.memory.app.restore import handle_memory_restore

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
                await _set(self.org, session, key, value)
                self.want[(key, session)] = ("value", value, False)
            elif op == "pin" and head is not None and head["tombstoned_at"] is None:
                value = _text(head["value"])
                await handle_memory_pin(
                    MemorySetInput(**ident, session_id=session, memory_key=key, value=value)
                )
                self.want[(key, session)] = ("value", value, True)
            elif op == "delete":
                await handle_memory_delete(
                    MemoryDeleteInput(**ident, session_id=session, memory_key=key)
                )
                self.want[(key, session)] = ("deleted", None, False)
            elif op == "undo" and head is not None and head["status"] == "superseded":
                await handle_memory_restore(
                    MemoryRestoreInput(
                        **ident,
                        session_id=session,
                        memory_key=key,
                        if_superseded_by=str(head["superseded_by"]),
                    )
                )

    async def run(self, n: int) -> None:
        for _ in range(n):
            await asyncio.sleep(self.rng.uniform(0.0, 0.2))
            self.spawn()
        while not all(t.done() for t in self.tasks):
            await asyncio.gather(*self.tasks)


async def _erase(ch: Any, org: str) -> None:
    now = datetime.now(UTC).replace(tzinfo=None, microsecond=0)
    await ch.insert(
        "loom.erasure_requests",
        [
            {
                "id": str(uuid.uuid4()),
                "org": org,
                "scope_type": "org",
                "scope_value": org,
                "requester": "test",
                "status": "queued",
                "failure_reason": "",
                "requested_at": now,
                "last_checkpoint_at": now,
                "version": 1,
            }
        ],
    )


async def _versions_after(ch: Any, org: str, version: int) -> int:
    n = 0
    for table in ("memory_versions", "memory_embeddings"):
        rows = await ch.query_all(
            f"SELECT count() AS n FROM loom.{table} WHERE org = {{org:String}}"
            " AND version > {v:UInt64} SETTINGS select_sequential_consistency = 1",
            params={"org": org, "v": version},
        )
        n += int(rows[0]["n"])
    return n


@pytest.mark.parametrize("seed", [1, 2, 3, 4, 5, 6])
async def test_the_resolver_keeps_its_invariants_under_concurrent_writes(
    ch, wired, monkeypatch, seed: int
) -> None:
    import server.memory.store as M
    from server.memory.api.models_crud import MemoryRestoreInput
    from server.memory.app import write
    from server.memory.app.restore import handle_memory_restore
    from server.memory.workers.conflict_resolver import run_pass

    org, llm = wired
    calls: list[str] = []
    writer = _Writer(ch, org, seed)
    monkeypatch.setattr(llm, "complete", _judge(seed, calls, writer))
    # A point write draws its version first and inserts after its embedding: a slow
    # embedding is the window in which the resolver can act on the old row.
    embed, slow = write._set_embed, random.Random(seed * 31)

    async def slow_embed(w: Any) -> Any:
        await asyncio.sleep(slow.uniform(0.0, 0.5))
        return await embed(w)

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
    want = writer.want
    if seed % 3 == 0:
        # An erasure requested now: nothing the resolver does may write in the org.
        await _erase(ch, org)
        before = M.next_version()
        await run_pass()
        assert await _versions_after(ch, org, before) == 0
    await run_pass()  # what the writes marked
    drained = len(calls)
    await run_pass()
    assert len(calls) == drained, "a pass after the queue drained makes no call"

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
        assert all(p["confidence"] >= 0.90 for p in record["pairs"]), record

    recorded = {
        e["memory_id"] for r in await _audit(ch, org, "memory.auto_supersede") for e in r["retired"]
    }
    for (key, session), head in heads.items():
        if head["status"] != "superseded" or head["tombstoned_at"] is not None:
            continue
        assert head["id"] in recorded, (key, "retired without a record")
        if seed % 3 == 0:
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
