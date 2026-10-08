"""The belief-revision lifecycle, driven as a state machine.

Seeded random walks of memory.set (Expand), memory.update with and without
the content-hash precondition (Revise), memory.delete (Contract), memory.pin
and memory.verify (Reinforce), and blacklist.add (Refuse) over one key. They
run against the real handlers and an in-process loom.memory
(`tests.unit.memory_store`). A small model predicts what each call does. After
every step the collapsed table is compared with the model, and the storage
invariants are checked:

- FINAL serves exactly the model's value, pin and attestation state.
- A new version of a key carries every persisted column (a column left to its
  DDL default would overwrite the previous value under FINAL).
- A live memory keeps its memory_id and created_at across revisions.
- memory_versions holds every write, in version order.
- The hash a write returns is the precondition the next update may pass.
"""

from __future__ import annotations

import logging
import random
from collections import Counter
from dataclasses import dataclass, replace
from typing import Any

import pytest
from fastapi import HTTPException

from server.core.hashing import sha256_value
from server.memory.api.models_blacklist import BlacklistAddInput
from server.memory.api.models_crud import (
    MemoryDeleteInput,
    MemorySetInput,
    MemoryUpdateInput,
    MemoryVerifyInput,
)
from server.memory.app import after_write
from server.memory.app import blacklist as bl_handlers
from server.memory.app import handlers as mem
from server.memory.app import update as update_mod
from server.memory.app import verify as mv
from server.memory.app import write as write_mod
from server.memory.domain.trust import verification_state
from server.memory.store import parse_memory_value
from tests.unit.memory_store import MemoryStore, wire_memory_store

ORG, NS, AGENT, KEY = "acme", "shop", "claude-code", "release/cut-day"
ACTOR = "human:alice"
REFUSED = "rm -rf /"
VALUES: list[Any] = [
    "Release branches are cut on Tuesdays",
    "Release branches are cut on Thursdays",
    '{"looks": "like json"}',
    "42",
    "ünïcode ✓ — em dash",
    {"day": "friday", "owners": ["ops", "release"]},
    f"never run {REFUSED} on the build host",
]


@dataclass(frozen=True)
class Model:
    """What a reader of the key should see."""

    live: bool = False
    value: Any = None
    pinned: bool = False
    verified_by: str = ""
    verified_sha: str = ""
    refused: bool = False

    @property
    def sha(self) -> str:
        return sha256_value(self.value) if self.live else ""

    def blocked(self, value: Any) -> bool:
        return self.refused and REFUSED in str(value)


class Walk:
    def __init__(self, store: MemoryStore) -> None:
        self.store = store
        self.model = Model()
        self.memory_id = ""
        self.created_at: Any = None
        self.log: list[str] = []
        self.outcomes: Counter[str] = Counter()

    # --- operations ---------------------------------------------------------

    async def set(self, value: Any, *, pin: bool = False) -> None:
        m = self.model
        inp = MemorySetInput(
            org=ORG, namespace=NS, agent=AGENT, memory_key=KEY, value=value, classify=False
        )
        handler = mem.handle_memory_pin if pin else write_mod.handle_memory_set
        restated = m.live and sha256_value(value) == m.sha
        # The live row's guards (the pin) run before the value's (the blacklist).
        if m.live and m.pinned and not pin and not restated:
            await self._refused(handler(inp), 409, "PINNED_CONFLICT")
            return
        if m.blocked(value) and not restated:
            await self._refused(handler(inp), 422, "BLACKLISTED")
            return
        kind = "pin" if pin else "set"
        self.outcomes[
            f"{kind}:{'restated' if restated else 'revised' if m.live else 'created'}"
        ] += 1
        out = await handler(inp)
        assert out.content_sha256 == sha256_value(value)
        self.model = replace(
            m if m.live else Model(refused=m.refused),
            live=True,
            value=value,
            pinned=pin or (m.live and m.pinned and restated),
        )
        if not m.live:
            self.memory_id, self.created_at = out.memory_id, None

    async def update(self, value: Any, *, precondition: str) -> None:
        m = self.model
        sha = {"current": m.sha, "stale": sha256_value(["stale"]), "none": ""}[precondition]
        inp = MemoryUpdateInput(
            org=ORG, namespace=NS, memory_key=KEY, value=value, if_match_sha256=sha
        )
        if not m.live:
            out = await update_mod.handle_memory_update(inp)
            assert out.updated is False and out.reason == "not_found"
            self.outcomes["update:not_found"] += 1
            return
        if precondition == "stale":
            await self._refused(update_mod.handle_memory_update(inp), 409, "PRECONDITION_FAILED")
            return
        if m.blocked(value):
            await self._refused(update_mod.handle_memory_update(inp), 422, "BLACKLISTED")
            return
        self.outcomes[f"update:{precondition}"] += 1
        out = await update_mod.handle_memory_update(inp)
        assert out.updated is True and out.memory_id == self.memory_id
        assert out.content_sha256 == sha256_value(value)
        self.model = replace(m, value=value)

    async def delete(self) -> None:
        out = await mem.handle_memory_delete(
            MemoryDeleteInput(org=ORG, namespace=NS, agent=AGENT, memory_key=KEY)
        )
        assert out.tombstoned is self.model.live
        assert out.found is self.model.live
        self.outcomes[f"delete:{'tombstoned' if self.model.live else 'absent'}"] += 1
        self.model = Model(refused=self.model.refused)

    async def verify(self, *, revoke: bool = False) -> None:
        out = await mv.handle_memory_verify(
            MemoryVerifyInput(
                org=ORG, namespace=NS, agent=AGENT, memory_key=KEY, revoke=revoke, actor=ACTOR
            )
        )
        await after_write.drain()
        m = self.model
        assert out.found is m.live
        self.outcomes[f"{'revoke' if revoke else 'verify'}:{'found' if m.live else 'absent'}"] += 1
        if not m.live:
            return
        if revoke:
            self.model = replace(m, verified_by="", verified_sha="")
        else:
            self.model = replace(m, verified_by=ACTOR, verified_sha=m.sha)

    async def refuse(self) -> None:
        await bl_handlers.handle_blacklist_add(
            BlacklistAddInput(
                org=ORG, namespace=NS, pattern=REFUSED, scope="namespace", reason="destructive"
            )
        )
        self.model = replace(self.model, refused=True)

    async def _refused(self, call: Any, status: int, code: str) -> None:
        before = len(self.store.versions)
        with pytest.raises(HTTPException) as exc:
            await call
        detail: dict[str, Any] = exc.value.detail  # type: ignore[assignment]
        assert exc.value.status_code == status and detail["code"] == code
        self.outcomes[f"refused:{code}"] += 1
        assert len(self.store.versions) == before, f"a refused write ({code}) landed a row"

    # --- invariants ---------------------------------------------------------

    def check(self) -> None:
        m, store = self.model, self.store
        head = store.head(memory_key=KEY, agent=AGENT)
        if not m.live:
            assert head is None or head["tombstoned_at"] is not None, self.log
            return
        assert head is not None and head["tombstoned_at"] is None, self.log
        assert parse_memory_value(head["value"]) == m.value, self.log
        assert bool(head["pinned"]) is m.pinned, self.log
        assert head["content_sha256"] == m.sha, self.log
        assert str(head["memory_id"]) == self.memory_id, self.log
        assert (head["verified_by"], head["verified_sha256"]) == (m.verified_by, m.verified_sha)
        want = verification_state(m.verified_by, m.verified_sha, m.sha)
        assert verification_state(head["verified_by"], head["verified_sha256"], m.sha) == want
        if self.created_at is None:
            self.created_at = head["created_at"]
        assert head["created_at"] == self.created_at, "a revision moved created_at"
        # A revision is a full row. Only a key's first version, or the first after a
        # delete, may lean on DDL defaults.
        revisions_with_defaults = [p for p in store.partial_inserts if p[2]]
        assert not revisions_with_defaults, revisions_with_defaults
        history = store.history(self.memory_id)
        assert [int(r["version"]) for r in history] == sorted(int(r["version"]) for r in history)
        assert history[-1] is head, "memory_versions misses the head version"


STEPS = 40


def _step(rng: random.Random, walk: Walk) -> tuple[str, Any]:
    op = rng.choice(["set", "set", "pin", "update", "update", "delete", "verify", "revoke"])
    value = rng.choice(VALUES)
    if op == "update":
        return op, (value, rng.choice(["current", "current", "stale", "none"]))
    return op, value


SEEDS = range(12)


async def _walk(monkeypatch: pytest.MonkeyPatch, seed: int) -> Walk:
    store = MemoryStore()
    wire_memory_store(monkeypatch, store)
    rng = random.Random(seed)
    walk = Walk(store)
    refuse_at = rng.randrange(STEPS)
    for i in range(STEPS):
        if i == refuse_at:
            walk.log.append("refuse")
            await walk.refuse()
            walk.check()
        op, arg = _step(rng, walk)
        walk.log.append(f"{op} {arg!r}")
        if op == "set":
            await walk.set(arg)
        elif op == "pin":
            await walk.set(arg, pin=True)
        elif op == "update":
            await walk.update(arg[0], precondition=arg[1])
        elif op == "delete":
            await walk.delete()
        elif op == "verify":
            await walk.verify()
        else:
            await walk.verify(revoke=True)
        walk.check()
    return walk


@pytest.mark.parametrize("seed", SEEDS)
async def test_a_random_walk_keeps_the_table_and_the_model_in_step(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, seed: int
) -> None:
    caplog.set_level(logging.WARNING, logger="server")
    await _walk(monkeypatch, seed)
    # A fail-open path that logged means a collaborator reached past the fakes.
    assert not [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]


async def test_the_walks_reach_every_outcome(monkeypatch: pytest.MonkeyPatch) -> None:
    """Positive control: the seeds above exercise each branch the model predicts."""
    seen: Counter[str] = Counter()
    for seed in SEEDS:
        with monkeypatch.context() as mp:
            seen += (await _walk(mp, seed)).outcomes
    expected = {
        "set:created",
        "set:revised",
        "set:restated",
        "pin:created",
        "pin:revised",
        "update:current",
        "update:none",
        "update:not_found",
        "delete:tombstoned",
        "delete:absent",
        "verify:found",
        "verify:absent",
        "revoke:found",
        "refused:PINNED_CONFLICT",
        "refused:PRECONDITION_FAILED",
        "refused:BLACKLISTED",
    }
    assert expected <= set(seen), sorted(expected - set(seen))


async def test_an_attestation_survives_a_revision_as_stale_and_rebinds_on_verify(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = MemoryStore()
    wire_memory_store(monkeypatch, store)
    walk = Walk(store)
    await walk.set(VALUES[0])
    await walk.verify()
    await walk.update(VALUES[1], precondition="current")
    walk.check()
    head = store.head(memory_key=KEY, agent=AGENT)
    assert head is not None
    assert verification_state(head["verified_by"], head["verified_sha256"], walk.model.sha) == (
        "stale"
    )
    await walk.verify()
    walk.check()
    assert walk.model.verified_sha == sha256_value(VALUES[1])


async def test_a_key_set_again_after_delete_is_a_new_memory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = MemoryStore()
    wire_memory_store(monkeypatch, store)
    walk = Walk(store)
    await walk.set(VALUES[0])
    await walk.verify()
    first_id = walk.memory_id
    await walk.delete()
    await walk.set(VALUES[0])
    walk.check()
    assert walk.memory_id != first_id
    head = store.head(memory_key=KEY, agent=AGENT)
    assert head is not None and head["verified_by"] == "", "a deleted row's attestation came back"
    assert len(store.history(first_id)) == 3, "set, verify and the tombstone stay in history"


async def test_the_hash_a_write_returns_is_the_next_updates_precondition(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = MemoryStore()
    wire_memory_store(monkeypatch, store)
    for value in VALUES[:6]:
        store.versions.clear()
        out = await write_mod.handle_memory_set(
            MemorySetInput(
                org=ORG, namespace=NS, agent=AGENT, memory_key=KEY, value=value, classify=False
            )
        )
        up = await update_mod.handle_memory_update(
            MemoryUpdateInput(
                org=ORG,
                namespace=NS,
                memory_key=KEY,
                value="next",
                if_match_sha256=out.content_sha256,
            )
        )
        assert up.updated is True, f"the returned hash of {value!r} does not match its own row"
