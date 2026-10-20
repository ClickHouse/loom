"""The structured-profile pass: how it shares its budget, where it resumes,
which orgs and users it passes over, and what it never writes."""

from __future__ import annotations

import json
from typing import Any

import pytest

from server.memory.store import profile_schemas, profile_sources
from server.memory.workers import structured_profile as w
from server.workers import pass_context

_SCHEMA = json.dumps(
    {"type": "object", "properties": {"tz": {"type": "string", "description": "time zone"}}}
)
_SPACES = [("o1", "a"), ("o1", "b"), ("o2", "a")]


@pytest.fixture
def world(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Three namespaces with a schema and five users behind in each; records
    what the pass asked for and built."""
    state: dict[str, Any] = {"off": set(), "asked": [], "built": [], "fail": set()}
    w._backoff.clear()

    async def switched_off_orgs() -> set[str]:
        return set(state["off"])

    async def list_active(
        *, exclude_orgs: list[str], after: Any, limit: int, only: Any = None, ch: Any = None
    ) -> list[dict[str, Any]]:
        keys = [k for k in _SPACES if k[0] not in exclude_orgs and (after is None or k > after)]
        keys = [k for k in keys if only is None or k == only][:limit]
        return [
            {"org": o, "namespace": n, "schema_json": _SCHEMA, "schema_sha256": "h" * 64}
            for o, n in keys
        ]

    async def users_behind(**kw: Any) -> list[str]:
        state["asked"].append((kw["org"], kw["namespace"], kw["limit"], kw["skip"]))
        users = [f"u{i}" for i in range(5) if f"u{i}" not in kw["skip"]]
        return users[: kw["limit"]]

    async def build(schema: dict[str, Any], user_id: str, cfg: Any) -> int | None:
        key = (schema["org"], schema["namespace"], user_id)
        if key in state["fail"]:
            raise ValueError("the profile model's answer is not a JSON object")
        state["built"].append(key)
        return 0

    monkeypatch.setattr(w.customer, "switched_off_orgs", switched_off_orgs)
    monkeypatch.setattr(profile_schemas, "list_active", list_active)
    monkeypatch.setattr(profile_sources, "users_behind", users_behind)
    monkeypatch.setattr(w, "build_profile", build)
    yield state
    w._backoff.clear()
    pass_context.clear()


def _cfg(budget: int) -> w.StructuredProfileConfig:
    return w.StructuredProfileConfig(max_users_per_pass=budget)


@pytest.mark.asyncio
async def test_each_namespace_gets_an_equal_share_of_the_budget(world: dict[str, Any]) -> None:
    result = await w.run_pass(_cfg(4))
    # ceil(4 / 3) = 2 each, until the budget runs out; the next pass starts at o2/a.
    assert [(o, n, lim) for o, n, lim, _ in world["asked"]] == [("o1", "a", 2), ("o1", "b", 2)]
    assert len(world["built"]) == result.profiles_written == 4
    per_space = {(o, n): sum(1 for b in world["built"] if b[:2] == (o, n)) for o, n in _SPACES}
    assert per_space == {("o1", "a"): 2, ("o1", "b"): 2, ("o2", "a"): 0}


@pytest.mark.asyncio
async def test_a_pass_resumes_after_the_last_namespace_and_wraps_around(
    world: dict[str, Any],
) -> None:
    ctx = pass_context.begin("structured_profile", resume_cursor=json.dumps(["o1", "a"]))
    await w.run_pass(_cfg(3))
    assert [(o, n) for o, n, _, _ in world["asked"]] == [("o1", "b"), ("o2", "a"), ("o1", "a")]
    assert json.loads(ctx.cursor) == ["o1", "a"]

    world["asked"].clear()
    ctx = pass_context.begin("structured_profile", resume_cursor=json.dumps(["o1", "b"]))
    await w.run_pass(_cfg(1))
    assert [(o, n) for o, n, _, _ in world["asked"]] == [("o2", "a")]
    assert json.loads(ctx.cursor) == ["o2", "a"]


@pytest.mark.asyncio
async def test_a_switched_off_org_is_passed_over(world: dict[str, Any]) -> None:
    world["off"] = {"o1"}
    await w.run_pass(_cfg(10))
    assert {b[0] for b in world["built"]} == {"o2"}


@pytest.mark.asyncio
async def test_a_failed_build_is_counted_and_backs_off_until_it_succeeds(
    world: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    world["fail"] = {("o2", "a", "u0")}
    result = await w.run_pass(w.StructuredProfileConfig(max_users_per_pass=10, only=("o2", "a")))
    assert result.errors == 1 and result.profiles_written == 4
    assert w.backing_off("o2", "a") == ("u0",)

    # The next pass skips the user: the budget goes to the others.
    world["asked"].clear()
    await w.run_pass(w.StructuredProfileConfig(max_users_per_pass=10, only=("o2", "a")))
    assert world["asked"][0][3] == ("u0",)

    # Each failure doubles the wait, up to the cap.
    first = w._backoff[("o2", "a", "u0")][1]
    w._note_failure(("o2", "a", "u0"))
    assert w._backoff[("o2", "a", "u0")][1] == 2 * first
    for _ in range(20):
        w._note_failure(("o2", "a", "u0"))
    assert w._backoff[("o2", "a", "u0")][1] == w.BACKOFF_CAP_SECONDS

    # Once the wait is over the user is tried again, and a success clears it.
    monkeypatch.setattr(w.time, "monotonic", lambda: 1e12)
    world["fail"] = set()
    assert w.backing_off("o2", "a") == ()
    await w.run_pass(w.StructuredProfileConfig(max_users_per_pass=10, only=("o2", "a")))
    assert ("o2", "a", "u0") not in w._backoff


@pytest.mark.asyncio
async def test_a_user_erased_while_the_model_ran_gets_no_profile(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.memory.app import profile_schema as app

    digests = iter(["d1", ""])  # before the model call, then after the erasure

    async def sources_digest(**kw: Any) -> str:
        return next(digests)

    async def user_facts(**kw: Any) -> list[dict[str, Any]]:
        return [{"memory_id": "m1", "value": '"My tz is UTC."', "since": "2026-01-01"}]

    written: list[Any] = []

    async def write(**kw: Any) -> None:
        written.append(kw)

    async def ask(*a: Any) -> dict[str, Any]:
        return {"tz": "UTC"}

    monkeypatch.setattr(profile_sources, "sources_digest", sources_digest)
    monkeypatch.setattr(profile_sources, "user_facts", user_facts)
    monkeypatch.setattr(app, "write_structured_profile", write)
    monkeypatch.setattr(w, "_ask", ask)
    schema = {"org": "o", "namespace": "n", "schema_json": _SCHEMA, "schema_sha256": "h" * 64}
    assert await w.build_profile(schema, "u1", w.StructuredProfileConfig()) is None
    assert written == []
