"""The structured-profile pass: how it shares its budget, where it resumes,
which orgs and users it passes over, and what it never writes."""

from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any

import pytest
from fastapi import HTTPException

from server.memory.app import profile_schema as schema_app
from server.memory.domain import profile_schema as ps
from server.memory.store import profile_schemas, profile_sources
from server.memory.store.profile_sources import Behind
from server.memory.workers import structured_profile as w
from server.settings import settings
from server.workers import pass_context

_PROPS = ps.parse(
    {"type": "object", "properties": {"tz": {"type": "string", "description": "time zone"}}}
)
_SPACES = [("o1", "a"), ("o1", "b"), ("o2", "a")]
_USERS = [f"u{i}" for i in range(5)]


_SCHEMA_JSON = ps.stored_form(
    {"type": "object", "properties": {"tz": {"type": "string", "description": "time zone"}}}
)


def _schema(org: str, ns: str) -> w.ActiveSchema:
    return w.ActiveSchema(org=org, namespace=ns, props=_PROPS, sha256="h" * 64)


def _row(org: str, ns: str, schema_json: str = _SCHEMA_JSON) -> dict[str, Any]:
    return {"org": org, "namespace": ns, "schema_json": schema_json, "schema_sha256": "h" * 64}


@pytest.fixture
def world(monkeypatch: pytest.MonkeyPatch) -> Iterator[dict[str, Any]]:
    """Three namespaces with a schema and five users behind in each; records
    which (org, namespace, user) the pass built, in order."""
    state: dict[str, Any] = {
        "off": set(),
        "built": [],
        "fail": set(),
        "refuse": set(),
        "scans": 0,
        "bad": set(),
    }
    w._backoff.clear()
    w._namespace_backoff.clear()
    w._refused.clear()
    w._scans.clear()

    async def switched_off_orgs() -> set[str]:
        return set(state["off"])

    async def list_active(
        *,
        exclude_orgs: list[str],
        after: Any,
        limit: int,
        only: Any = None,
        waiting: Any = None,
        ch: Any = None,
    ) -> list[dict[str, Any]]:
        held = {(o, n) for o, n, sha in waiting or [] if sha == "h" * 64}
        keys = [
            k
            for k in _SPACES
            if k[0] not in exclude_orgs and (only is None or k == only) and k not in held
        ]
        # After the cursor first, then from the start: the store's order.
        keys.sort(key=lambda k: (after is not None and k <= after, k))
        return [
            _row(o, n, "{}" if (o, n) in state["bad"] else _SCHEMA_JSON) for o, n in keys[:limit]
        ]

    async def users_behind(**kw: Any) -> list[Behind]:
        state["scans"] += 1
        users = [u for u in _USERS if u not in kw["skip"] and u not in kw["refused"]]
        return [Behind(u, "d1", 0) for u in users[: kw["limit"]]]

    async def build(schema: w.ActiveSchema, user_id: str, cfg: Any) -> int | None:
        key = (schema.org, schema.namespace, user_id)
        if key in state["fail"]:
            raise ValueError("the profile model's answer is not a JSON object")
        if key in state["refuse"]:
            raise w.Refused("d1", 3, 0)
        state["built"].append(key)
        return 0

    monkeypatch.setattr(w.customer, "switched_off_orgs", switched_off_orgs)
    monkeypatch.setattr(profile_schemas, "list_active", list_active)
    monkeypatch.setattr(profile_sources, "users_behind", users_behind)
    monkeypatch.setattr(w, "build_profile", build)
    yield state
    w._backoff.clear()
    w._namespace_backoff.clear()
    w._refused.clear()
    w._scans.clear()
    pass_context.clear()


def _cfg(budget: int, only: tuple[str, str] | None = None) -> w.StructuredProfileConfig:
    return w.StructuredProfileConfig(max_users_per_pass=budget, only=only)


def _spaces_built(built: list[tuple[str, str, str]]) -> list[tuple[str, str]]:
    """The namespaces in the order the pass first built in them."""
    return list(dict.fromkeys(b[:2] for b in built))


def _per_space(built: list[tuple[str, str, str]]) -> dict[tuple[str, str], int]:
    return {k: sum(1 for b in built if b[:2] == k) for k in _SPACES}


@pytest.mark.asyncio
async def test_each_namespace_gets_an_equal_share_of_the_budget(world: dict[str, Any]) -> None:
    result = await w.run_pass(_cfg(4))
    # ceil(4 / 3) = 2 each, until the budget runs out.
    assert _per_space(world["built"]) == {("o1", "a"): 2, ("o1", "b"): 2, ("o2", "a"): 0}
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
async def test_a_failing_namespace_is_counted_and_the_pass_moves_on(
    world: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    fine = profile_sources.users_behind

    async def users_behind(**kw: Any) -> list[Behind]:
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
async def test_a_pass_stopped_between_users_builds_no_more(
    world: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    ctx = pass_context.begin("structured_profile")
    real_build = w.build_profile

    async def build_then_stop(schema: w.ActiveSchema, user_id: str, cfg: Any) -> int | None:
        out = await real_build(schema, user_id, cfg)
        ctx.stopped.set()  # the supervisor stops the pass after this build
        return out

    monkeypatch.setattr(w, "build_profile", build_then_stop)
    with pytest.raises(pass_context.PassStopped):
        await w.run_pass(_cfg(10, ("o2", "a")))
    assert world["built"] == [("o2", "a", "u0")]


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
    assert w._backoff[key][1] == settings.structured_profile_backoff_cap_seconds

    # Once the wait is over the user is built again, and the success clears it.
    w._backoff[key] = (0.0, w._backoff[key][1])
    world["fail"] = set()
    world["built"].clear()
    w._scans.clear()  # and the last scan is old enough to run again
    await w.run_pass(_cfg(10, ("o2", "a")))
    assert key in world["built"] and key not in w._backoff


@pytest.mark.asyncio
async def test_a_refused_build_waits_then_is_tried_again_when_the_refusal_lapses(
    world: dict[str, Any],
) -> None:
    key = ("o2", "a", "u0")
    world["refuse"] = {key}
    result = await w.run_pass(_cfg(10, ("o2", "a")))
    assert (result.refused, result.errors, result.profiles_written) == (1, 0, 4)
    assert w.refused_at("o2", "a") == {"u0": ("d1", 3)}

    # While it holds the user is not tried; a lock that ran out with no write is
    # tried again once the refusal lapses.
    world["refuse"] = set()
    world["built"].clear()
    await w.run_pass(_cfg(10, ("o2", "a")))
    assert key not in world["built"]
    digest, version, _ = w._refused[key]
    w._refused[key] = (digest, version, 0.0)
    w._scans.clear()  # and the last scan is old enough to run again
    await w.run_pass(_cfg(10, ("o2", "a")))
    assert key in world["built"] and key not in w._refused


def test_waits_that_are_over_are_pruned() -> None:
    w._backoff.clear()
    w._refused.clear()
    cap = settings.structured_profile_backoff_cap_seconds
    w._backoff[("o", "n", "old")] = (-2 * cap, cap)
    w._backoff[("o", "n", "new")] = (1e18, 300.0)
    w._refused[("o", "n", "lapsed")] = ("d", 1, 0.0)
    w._refused[("o", "n", "held")] = ("d", 1, 1e18)
    w._prune()
    assert list(w._backoff) == [("o", "n", "new")] and list(w._refused) == [("o", "n", "held")]
    w._backoff.clear()
    w._refused.clear()


def test_a_pinned_or_locked_profile_refuses_a_write() -> None:
    assert not w._refuses_a_write(None)
    assert not w._refuses_a_write({"pinned": 0, "lock_mode": "", "lock_until": None})
    assert w._refuses_a_write({"pinned": 1, "lock_mode": "", "lock_until": None})
    assert w._refuses_a_write({"pinned": 0, "lock_mode": "read_only", "lock_until": None})
    assert not w._refuses_a_write({"pinned": 0, "lock_mode": "no_delete", "lock_until": None})


def _one_user(
    monkeypatch: pytest.MonkeyPatch, live_after: set[str], existing: dict[str, Any] | None = None
) -> list[Any]:
    """A build of one user who has facts m1 and m2, of which `live_after` are still
    current once the model has answered; returns what was written."""

    async def sources_digest(**kw: Any) -> tuple[str, int]:
        return ("d1", 2)

    async def profile_row(**kw: Any) -> dict[str, Any] | None:
        return existing

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
    monkeypatch.setattr(schema_app, "write_structured_profile", write)
    monkeypatch.setattr(w, "_ask", ask)
    return written


_ONE = _schema("o", "n")
_real_write = schema_app.write_structured_profile


@pytest.mark.asyncio
async def test_a_fact_erased_while_the_model_ran_stops_the_write(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    written = _one_user(monkeypatch, {"m1"})  # m2 erased during the model call
    assert await w.build_profile(_ONE, "u1", w.StructuredProfileConfig()) is None
    assert written == []


@pytest.mark.asyncio
async def test_a_build_writes_its_label_tags_and_sources(monkeypatch: pytest.MonkeyPatch) -> None:
    from server.settings import settings

    _one_user(monkeypatch, {"m1", "m2"})
    monkeypatch.setattr(schema_app, "write_structured_profile", _real_write)
    sections: list[Any] = []

    async def write_section(mset: Any) -> None:
        sections.append(mset)

    monkeypatch.setattr(schema_app, "write_section", write_section)
    monkeypatch.setattr(settings, "structured_profile_label", "my_profile")
    assert await w.build_profile(_ONE, "u1", w.StructuredProfileConfig()) == 0
    (mset,) = sections
    assert (mset.memory_key, mset.memory_type, mset.agent, mset.value) == (
        "my_profile",
        "profile",
        "-",
        {"tz": "UTC"},
    )
    assert mset.tags == ["profile_schema:" + "h" * 16, "profile_sources:d1", "profile_count:2"]
    assert mset.source_trace == "profile.fold:my_profile:m1,m2"


@pytest.mark.asyncio
async def test_a_profile_already_current_is_not_built_again(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    current = {
        "version": 4,
        "pinned": 0,
        "lock_mode": "",
        "lock_until": None,
        "tags": ["profile_schema:" + "h" * 16, "profile_sources:d1", "profile_count:2"],
    }
    written = _one_user(monkeypatch, {"m1", "m2"}, existing=current)
    assert await w.build_profile(_ONE, "u1", w.StructuredProfileConfig()) is None
    assert written == []


@pytest.mark.asyncio
async def test_a_pinned_profile_is_refused_before_the_model_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pinned = {"version": 7, "pinned": 1, "lock_mode": "", "lock_until": None, "tags": []}
    written = _one_user(monkeypatch, {"m1", "m2"}, existing=pinned)

    async def no_call(*a: Any) -> dict[str, Any]:
        raise AssertionError("no model call for a refused build")

    monkeypatch.setattr(w, "_ask", no_call)
    with pytest.raises(w.Refused) as e:
        await w.build_profile(_ONE, "u1", w.StructuredProfileConfig())
    assert (e.value.digest, e.value.profile_version) == ("d1", 7) and written == []


@pytest.mark.asyncio
async def test_a_write_the_row_refuses_is_a_refusal_carrying_the_digest(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _one_user(monkeypatch, {"m1", "m2"})

    async def refuse(**kw: Any) -> None:
        raise HTTPException(status_code=422, detail={"code": "CHAR_LIMIT_EXCEEDED"})

    monkeypatch.setattr(schema_app, "write_structured_profile", refuse)
    with pytest.raises(w.Refused) as e:
        await w.build_profile(_ONE, "u1", w.StructuredProfileConfig())
    assert (e.value.digest, e.value.profile_version) == ("d1", 0)


@pytest.mark.asyncio
async def test_a_pass_builds_at_most_the_configured_number_of_profiles(
    world: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    from server.settings import settings

    monkeypatch.setattr(settings, "structured_profile_max_users_per_pass", 2)
    result = await w.run_pass()
    assert len(world["built"]) == result.profiles_written == 2


@pytest.mark.asyncio
async def test_the_configured_model_answers(monkeypatch: pytest.MonkeyPatch) -> None:
    from server.llm import customer
    from server.llm.fake import FakeLLMClient
    from server.settings import settings

    asked: list[str] = []

    def for_model(model: str) -> FakeLLMClient:
        asked.append(model)
        return FakeLLMClient(model)

    monkeypatch.setattr(settings, "structured_profile_model", "profile-model")
    monkeypatch.setattr(w.llm_client, "get_llm_client_for_model", for_model)
    monkeypatch.setattr(w, "emit_worker_cost_event_for", lambda **kw: None)
    with customer.for_org("o"):
        answer = await w._ask(
            "o", "n", _PROPS, "- [2026-01-01] My tz is UTC.", w.StructuredProfileConfig()
        )
    assert asked == ["profile-model"] and answer == {"tz": "My tz is UTC"}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "code", "refused"),
    [
        (409, "PINNED_CONFLICT", True),
        (423, "LOCKED", True),
        (422, "CHAR_LIMIT_EXCEEDED", True),
        (422, "BLACKLISTED", True),
        (500, "INTERNAL", False),
        (503, "BACKEND_UNAVAILABLE", False),
    ],
)
async def test_what_the_write_answers_decides_refusal_or_failure(
    monkeypatch: pytest.MonkeyPatch, status: int, code: str, refused: bool
) -> None:
    _one_user(monkeypatch, {"m1", "m2"})

    async def answer(**kw: Any) -> None:
        raise HTTPException(status_code=status, detail={"code": code})

    monkeypatch.setattr(schema_app, "write_structured_profile", answer)
    with pytest.raises(w.Refused if refused else HTTPException):
        await w.build_profile(_ONE, "u1", w.StructuredProfileConfig())


@pytest.mark.asyncio
async def test_one_scan_serves_the_passes_after_it(world: dict[str, Any]) -> None:
    cfg = _cfg(1, ("o2", "a"))
    for _ in range(5):
        await w.run_pass(cfg)
    # The first scan found up to SCAN_FACTOR users; the next four passes took from it.
    assert world["scans"] == 1 and len(world["built"]) == 5
    # It found every user behind (5 of the 10 it asked for): drained, it is not
    # scanned again until it is `structured_profile_scan_max_age_seconds` old.
    await w.run_pass(cfg)
    assert world["scans"] == 1
    w._scans[("o2", "a")].scanned_at -= settings.structured_profile_scan_max_age_seconds + 1
    await w.run_pass(cfg)
    assert world["scans"] == 2


@pytest.mark.asyncio
async def test_a_drained_incomplete_scan_is_scanned_again_at_once(
    world: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(w, "SCAN_FACTOR", 1)  # the scan finds as many as it asks for
    cfg = _cfg(1, ("o2", "a"))
    await w.run_pass(cfg)
    await w.run_pass(cfg)
    assert world["scans"] == 2 and len(world["built"]) == 2


@pytest.mark.asyncio
async def test_a_failing_namespace_backs_off_without_keeping_the_pass_busy(
    world: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    async def failing(**kw: Any) -> list[Behind]:
        world["scans"] += 1
        raise RuntimeError("the scan failed")

    monkeypatch.setattr(profile_sources, "users_behind", failing)
    cfg = _cfg(10, ("o2", "a"))
    assert (await w.run_pass(cfg)).errors == 1
    # Backing off, the namespace is not scanned and the pass is idle.
    ctx = pass_context.begin("structured_profile")
    result = await w.run_pass(cfg)
    assert world["scans"] == 1 and result.errors == 0 and ctx.idle
    # Once its wait is over it is scanned again.
    _, wait, sha = w._namespace_backoff[("o2", "a")]
    w._namespace_backoff[("o2", "a")] = (0.0, wait, sha)
    await w.run_pass(cfg)
    assert world["scans"] == 2


@pytest.mark.asyncio
async def test_a_schema_change_or_an_old_scan_scans_again(
    world: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = _cfg(1, ("o2", "a"))
    await w.run_pass(cfg)
    w._scans[("o2", "a")].scanned_at -= settings.structured_profile_scan_max_age_seconds + 1
    await w.run_pass(cfg)
    assert world["scans"] == 2
    w._scans[("o2", "a")].schema_sha256 = "x" * 64
    await w.run_pass(cfg)
    assert world["scans"] == 3


@pytest.mark.asyncio
async def test_the_leftover_budget_takes_from_the_scan_already_made(
    world: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    async def users_behind(**kw: Any) -> list[Behind]:
        world["scans"] += 1
        behind = {("o1", "a"): ["u0"], ("o1", "b"): _USERS, ("o2", "a"): []}
        return [Behind(u, "d1", 0) for u in behind[(kw["org"], kw["namespace"])][: kw["limit"]]]

    monkeypatch.setattr(profile_sources, "users_behind", users_behind)
    await w.run_pass(_cfg(6))
    assert _per_space(world["built"]) == {("o1", "a"): 1, ("o1", "b"): 5, ("o2", "a"): 0}
    assert world["scans"] == 3  # one per namespace, none for the leftover round


@pytest.mark.asyncio
async def test_a_schema_that_does_not_parse_is_counted_and_passed_over(
    world: dict[str, Any],
) -> None:
    world["bad"] = {("o1", "a")}
    ctx = pass_context.begin("structured_profile")
    result = await w.run_pass(_cfg(3))
    assert result.errors == 1 and ctx.count == 1
    assert ("o1", "a") not in _spaces_built(world["built"])
    assert ("o1", "a") in w._namespace_backoff  # it backs off like a failing scan


@pytest.mark.asyncio
async def test_a_waiting_namespace_does_not_take_the_only_place_in_a_pass(
    world: dict[str, Any],
) -> None:
    w._note_namespace_failure("o1", "a", "h" * 64)
    await w.run_pass(_cfg(1))
    assert _spaces_built(world["built"]) == [("o1", "b")]


@pytest.mark.asyncio
async def test_a_new_schema_ends_a_namespace_wait(world: dict[str, Any]) -> None:
    w._note_namespace_failure("o1", "a", "x" * 64)  # it failed under an older schema
    await w.run_pass(_cfg(1))
    assert _spaces_built(world["built"]) == [("o1", "a")]
