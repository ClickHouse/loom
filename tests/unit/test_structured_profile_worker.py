"""The structured-profile pass: how it shares its budget, where it resumes,
which orgs and users it passes over, and what it never writes."""

from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any

import pytest
from fastapi import HTTPException

from server.memory.store import profile_schemas, profile_sources
from server.memory.workers import structured_profile as w
from server.workers import pass_context

_SCHEMA = json.dumps(
    {"type": "object", "properties": {"tz": {"type": "string", "description": "time zone"}}}
)
_SPACES = [("o1", "a"), ("o1", "b"), ("o2", "a")]
_USERS = [f"u{i}" for i in range(5)]


@pytest.fixture
def world(monkeypatch: pytest.MonkeyPatch) -> Iterator[dict[str, Any]]:
    """Three namespaces with a schema and five users behind in each; records
    which (org, namespace, user) the pass built, in order."""
    state: dict[str, Any] = {"off": set(), "built": [], "fail": set(), "refuse": set()}
    w._backoff.clear()
    w._refused.clear()

    async def switched_off_orgs() -> set[str]:
        return set(state["off"])

    async def list_active(
        *, exclude_orgs: list[str], after: Any, limit: int, only: Any = None, ch: Any = None
    ) -> list[dict[str, Any]]:
        keys = [k for k in _SPACES if k[0] not in exclude_orgs and (only is None or k == only)]
        # After the cursor first, then from the start: the store's order.
        keys.sort(key=lambda k: (after is not None and k <= after, k))
        return [
            {"org": o, "namespace": n, "schema_json": _SCHEMA, "schema_sha256": "h" * 64}
            for o, n in keys[:limit]
        ]

    async def users_behind(**kw: Any) -> list[str]:
        users = [u for u in _USERS if u not in kw["skip"] and u not in kw["refused"]]
        return users[: kw["limit"]]

    async def build(schema: dict[str, Any], user_id: str, cfg: Any) -> int | None:
        key = (schema["org"], schema["namespace"], user_id)
        if key in state["fail"]:
            raise ValueError("the profile model's answer is not a JSON object")
        if key in state["refuse"]:
            raise w.Refused("d1")
        state["built"].append(key)
        return 0

    monkeypatch.setattr(w.customer, "switched_off_orgs", switched_off_orgs)
    monkeypatch.setattr(profile_schemas, "list_active", list_active)
    monkeypatch.setattr(profile_sources, "users_behind", users_behind)
    monkeypatch.setattr(w, "build_profile", build)
    yield state
    w._backoff.clear()
    w._refused.clear()
    pass_context.clear()


def _cfg(budget: int, only: tuple[str, str] | None = None) -> w.StructuredProfileConfig:
    return w.StructuredProfileConfig(max_users_per_pass=budget, only=only)


def _spaces_built(built: list[tuple[str, str, str]]) -> list[tuple[str, str]]:
    """The namespaces in the order the pass first built in them."""
    return list(dict.fromkeys(b[:2] for b in built))


@pytest.mark.asyncio
async def test_each_namespace_gets_an_equal_share_of_the_budget(world: dict[str, Any]) -> None:
    result = await w.run_pass(_cfg(4))
    # ceil(4 / 3) = 2 each, until the budget runs out.
    per_space = {k: sum(1 for b in world["built"] if b[:2] == k) for k in _SPACES}
    assert per_space == {("o1", "a"): 2, ("o1", "b"): 2, ("o2", "a"): 0}
    assert result.profiles_written == 4


@pytest.mark.asyncio
async def test_a_pass_resumes_after_the_last_namespace_and_wraps_around(
    world: dict[str, Any],
) -> None:
    ctx = pass_context.begin("structured_profile", resume_cursor=json.dumps(["o1", "a"]))
    await w.run_pass(_cfg(3))
    assert _spaces_built(world["built"]) == [("o1", "b"), ("o2", "a"), ("o1", "a")]
    assert json.loads(ctx.cursor) == ["o1", "a"]

    world["built"].clear()
    ctx = pass_context.begin("structured_profile", resume_cursor=json.dumps(["o1", "b"]))
    await w.run_pass(_cfg(1))
    assert _spaces_built(world["built"]) == [("o2", "a")]
    assert json.loads(ctx.cursor) == ["o2", "a"]


@pytest.mark.asyncio
async def test_a_switched_off_org_is_passed_over(world: dict[str, Any]) -> None:
    world["off"] = {"o1"}
    await w.run_pass(_cfg(10))
    assert {b[0] for b in world["built"]} == {"o2"}


@pytest.mark.asyncio
async def test_a_failed_build_is_counted_and_backs_off_until_it_succeeds(
    world: dict[str, Any],
) -> None:
    key = ("o2", "a", "u0")
    world["fail"] = {key}
    result = await w.run_pass(_cfg(10, ("o2", "a")))
    assert (result.errors, result.profiles_written) == (1, 4)

    # The next pass leaves the user out and builds the others.
    world["built"].clear()
    result = await w.run_pass(_cfg(10, ("o2", "a")))
    assert key not in world["built"] and result.errors == 0

    # Each failure doubles the wait, up to the cap.
    first = w._backoff[key][1]
    w._note_failure(key)
    assert w._backoff[key][1] == 2 * first
    for _ in range(20):
        w._note_failure(key)
    assert w._backoff[key][1] == w.BACKOFF_CAP_SECONDS

    # Once the wait is over the user is built again, and the success clears it.
    w._backoff[key] = (0.0, w._backoff[key][1])
    world["fail"] = set()
    world["built"].clear()
    await w.run_pass(_cfg(10, ("o2", "a")))
    assert key in world["built"] and key not in w._backoff


@pytest.mark.asyncio
async def test_a_refused_build_waits_for_the_users_memories_to_change(
    world: dict[str, Any],
) -> None:
    key = ("o2", "a", "u0")
    world["refuse"] = {key}
    result = await w.run_pass(_cfg(10, ("o2", "a")))
    assert (result.refused, result.errors, result.profiles_written) == (1, 0, 4)
    # The store leaves the user out while their digest is still the refused one.
    assert w.refused_digests("o2", "a") == {"u0": "d1"}


def test_a_pinned_or_locked_profile_refuses_a_write() -> None:
    assert not w._refuses_a_write(None)
    assert not w._refuses_a_write({"pinned": 0, "lock_mode": "", "lock_until": None})
    assert w._refuses_a_write({"pinned": 1, "lock_mode": "", "lock_until": None})
    assert w._refuses_a_write({"pinned": 0, "lock_mode": "read_only", "lock_until": None})
    assert not w._refuses_a_write({"pinned": 0, "lock_mode": "no_delete", "lock_until": None})


def _one_user(monkeypatch: pytest.MonkeyPatch, live_after: set[str]) -> list[Any]:
    """A build of one user who has facts m1 and m2, of which `live_after` are still
    current once the model has answered; returns what was written."""
    from server.memory.app import profile_schema as app

    async def sources_digest(**kw: Any) -> tuple[str, int]:
        return ("d1", 2)

    async def profile_row(**kw: Any) -> None:
        return None

    async def user_facts(**kw: Any) -> list[dict[str, Any]]:
        return [
            {"memory_id": "m1", "value": '"My tz is UTC."', "since": "2026-01-01"},
            {"memory_id": "m2", "value": '"I speak en."', "since": "2026-01-02"},
        ]

    async def live_ids(**kw: Any) -> set[str]:
        return set(live_after)

    async def ask(*a: Any) -> dict[str, Any]:
        return {"tz": "UTC"}

    written: list[Any] = []

    async def write(**kw: Any) -> None:
        written.append(kw)

    monkeypatch.setattr(profile_sources, "sources_digest", sources_digest)
    monkeypatch.setattr(profile_sources, "profile_row", profile_row)
    monkeypatch.setattr(profile_sources, "user_facts", user_facts)
    monkeypatch.setattr(profile_sources, "live_ids", live_ids)
    monkeypatch.setattr(app, "write_structured_profile", write)
    monkeypatch.setattr(w, "_ask", ask)
    return written


_ONE = {"org": "o", "namespace": "n", "schema_json": _SCHEMA, "schema_sha256": "h" * 64}


@pytest.mark.asyncio
async def test_a_fact_erased_while_the_model_ran_stops_the_write(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    written = _one_user(monkeypatch, {"m1"})  # m2 erased during the model call
    assert await w.build_profile(_ONE, "u1", w.StructuredProfileConfig()) is None
    assert written == []


@pytest.mark.asyncio
async def test_a_build_writes_its_label_tags_and_sources(monkeypatch: pytest.MonkeyPatch) -> None:
    written = _one_user(monkeypatch, {"m1", "m2"})
    cfg = w.StructuredProfileConfig(label="my_profile")
    assert await w.build_profile(_ONE, "u1", cfg) == 0
    (row,) = written
    assert (row["label"], row["value"], row["memory_ids"]) == (
        "my_profile",
        {"tz": "UTC"},
        ["m1", "m2"],
    )
    assert (row["sources_digest"], row["source_count"]) == ("d1", 2)


@pytest.mark.asyncio
async def test_a_write_the_row_refuses_is_a_refusal_carrying_the_digest(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.memory.app import profile_schema as app

    _one_user(monkeypatch, {"m1", "m2"})

    async def refuse(**kw: Any) -> None:
        raise HTTPException(status_code=422, detail={"code": "CHAR_LIMIT_EXCEEDED"})

    monkeypatch.setattr(app, "write_structured_profile", refuse)
    with pytest.raises(w.Refused) as e:
        await w.build_profile(_ONE, "u1", w.StructuredProfileConfig())
    assert str(e.value) == "d1"


def test_the_config_reads_its_settings_at_each_pass(monkeypatch: pytest.MonkeyPatch) -> None:
    from server.settings import settings

    values = {
        "structured_profile_max_users_per_pass": 7,
        "structured_profile_memory_limit": 30,
        "structured_profile_cooldown_seconds": 60,
        "structured_profile_label": "my_profile",
        "structured_profile_model": "m",
    }
    for name, value in values.items():
        monkeypatch.setattr(settings, name, value)
    cfg = w.StructuredProfileConfig()
    assert (cfg.max_users_per_pass, cfg.memory_limit, cfg.cooldown_seconds) == (7, 30, 60)
    assert (cfg.label, cfg.model) == ("my_profile", "m")


def test_the_worker_is_registered_on_its_switch_and_interval() -> None:
    from server.workers.registry import _WORKERS

    row = next(r for r in _WORKERS if r.name == "structured_profile")
    assert (row.enabled, row.interval) == (
        "structured_profile_enabled",
        "structured_profile_interval_seconds",
    )


@pytest.mark.asyncio
async def test_a_failing_namespace_is_counted_and_the_pass_moves_on(
    world: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    fine = profile_sources.users_behind

    async def users_behind(**kw: Any) -> list[str]:
        if (kw["org"], kw["namespace"]) == ("o1", "a"):
            raise RuntimeError("the scan failed")
        return await fine(**kw)

    monkeypatch.setattr(profile_sources, "users_behind", users_behind)
    ctx = pass_context.begin("structured_profile")
    result = await w.run_pass(_cfg(3))
    assert result.errors == 1 and ctx.count == 1
    assert _spaces_built(world["built"]) == [("o1", "b"), ("o2", "a")]
    # The cursor moved past it: the next pass starts after, and comes back to it on the wrap.
    assert json.loads(ctx.cursor) == ["o2", "a"]


@pytest.mark.asyncio
async def test_the_budget_a_namespace_leaves_goes_to_one_still_behind(
    world: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    async def users_behind(**kw: Any) -> list[str]:
        # o1/a has one user behind; o1/b has many; o2/a none.
        behind = {("o1", "a"): ["u0"], ("o1", "b"): _USERS, ("o2", "a"): []}
        users = [u for u in behind[(kw["org"], kw["namespace"])] if u not in kw["skip"]]
        return users[: kw["limit"]]

    monkeypatch.setattr(profile_sources, "users_behind", users_behind)
    await w.run_pass(_cfg(6))
    per_space = {k: sum(1 for b in world["built"] if b[:2] == k) for k in _SPACES}
    # Shares of 2: o1/a builds 1 and o2/a none, so o1/b gets the 3 left over too.
    assert per_space == {("o1", "a"): 1, ("o1", "b"): 5, ("o2", "a"): 0}
