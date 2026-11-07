"""The structured-profile pass: how it shares its budget, where it resumes,
which orgs and users it passes over, and what it never writes."""

from __future__ import annotations

import json
import sys
from collections.abc import Callable, Iterator
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
from tests.unit.clock_fake import ManualClock

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

    async def build(schema: w.ActiveSchema, user_id: str) -> int | None:
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


def _cfg(only: tuple[str, str] | None = None) -> w.StructuredProfileConfig:
    """A pass over every namespace, or only `only`."""
    return w.StructuredProfileConfig(only=only)


def _spaces_built(built: list[tuple[str, str, str]]) -> list[tuple[str, str]]:
    """The namespaces in the order the pass first built in them."""
    return list(dict.fromkeys(b[:2] for b in built))


def _per_space(built: list[tuple[str, str, str]]) -> dict[tuple[str, str], int]:
    return {k: sum(1 for b in built if b[:2] == k) for k in _SPACES}


@pytest.mark.asyncio
async def test_each_namespace_gets_an_equal_share_of_the_budget(
    world: dict[str, Any], settings_override: Callable[..., None]
) -> None:
    settings_override(structured_profile_max_users_per_pass=4)
    result = await w.run_pass(_cfg())
    # ceil(4 / 3) = 2 each, until the budget runs out.
    assert _per_space(world["built"]) == {("o1", "a"): 2, ("o1", "b"): 2, ("o2", "a"): 0}
    assert result.profiles_written == 4


@pytest.mark.asyncio
async def test_a_pass_resumes_after_the_last_namespace_and_wraps_around(
    world: dict[str, Any],
    settings_override: Callable[..., None],
) -> None:
    ctx = pass_context.begin("structured_profile", resume_cursor=json.dumps(["o1", "a"]))
    settings_override(structured_profile_max_users_per_pass=3)
    await w.run_pass(_cfg())
    assert _spaces_built(world["built"]) == [("o1", "b"), ("o2", "a"), ("o1", "a")]
    assert json.loads(ctx.cursor) == ["o1", "a"]

    world["built"].clear()
    ctx = pass_context.begin("structured_profile", resume_cursor=json.dumps(["o1", "b"]))
    settings_override(structured_profile_max_users_per_pass=1)
    await w.run_pass(_cfg())
    assert _spaces_built(world["built"]) == [("o2", "a")]
    assert json.loads(ctx.cursor) == ["o2", "a"]


@pytest.mark.asyncio
async def test_a_failing_namespace_is_counted_and_the_pass_moves_on(
    world: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    settings_override: Callable[..., None],
) -> None:
    fine = profile_sources.users_behind

    async def users_behind(**kw: Any) -> list[Behind]:
        if (kw["org"], kw["namespace"]) == ("o1", "a"):
            raise RuntimeError("the scan failed")
        return await fine(**kw)

    monkeypatch.setattr(profile_sources, "users_behind", users_behind)
    ctx = pass_context.begin("structured_profile")
    settings_override(structured_profile_max_users_per_pass=3)
    result = await w.run_pass(_cfg())
    assert result.errors == 1 and ctx.count == 1
    assert _spaces_built(world["built"]) == [("o1", "b"), ("o2", "a")]
    # The cursor moved past it: the next pass starts after, and comes back to it on the wrap.
    assert json.loads(ctx.cursor) == ["o2", "a"]


@pytest.mark.asyncio
async def test_a_pass_stopped_between_users_builds_no_more(
    world: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    settings_override: Callable[..., None],
) -> None:
    ctx = pass_context.begin("structured_profile")
    real_build = w.build_profile

    async def build_then_stop(schema: w.ActiveSchema, user_id: str) -> int | None:
        out = await real_build(schema, user_id)
        ctx.stopped.set()  # the supervisor stops the pass after this build
        return out

    monkeypatch.setattr(w, "build_profile", build_then_stop)
    settings_override(structured_profile_max_users_per_pass=10)
    with pytest.raises(pass_context.PassStopped):
        await w.run_pass(_cfg(("o2", "a")))
    assert world["built"] == [("o2", "a", "u0")]


@pytest.mark.asyncio
async def test_a_slow_pass_stops_taking_users_halfway_to_its_deadline(
    world: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    settings_override: Callable[..., None],
) -> None:
    clock = ManualClock(t=1000.0)
    monkeypatch.setattr(w.time, "monotonic", clock.monotonic)
    real_build = w.build_profile

    async def slow_build(schema: w.ActiveSchema, user_id: str) -> int | None:
        clock.advance(30)  # each build takes 30 of the pass's 100 seconds
        return await real_build(schema, user_id)

    monkeypatch.setattr(w, "build_profile", slow_build)
    settings_override(
        structured_profile_max_users_per_pass=5, structured_profile_max_pass_seconds=100
    )
    ctx = pass_context.begin("structured_profile")
    await w.run_pass(_cfg(("o2", "a")))
    # The third build would start at 60 s, past half the deadline.
    assert world["built"] == [("o2", "a", "u0"), ("o2", "a", "u1")]
    assert [b.user_id for b in w._scans[("o2", "a")].behind] == ["u2", "u3", "u4"]
    assert not ctx.idle

    # The next pass starts with the users the last one left, without a new scan.
    await w.run_pass(_cfg(("o2", "a")))
    assert world["built"][2:] == [("o2", "a", "u2"), ("o2", "a", "u3")]
    assert world["scans"] == 1


@pytest.mark.asyncio
async def test_a_switched_off_org_is_passed_over(
    world: dict[str, Any], settings_override: Callable[..., None]
) -> None:
    world["off"] = {"o1"}
    settings_override(structured_profile_max_users_per_pass=10)
    await w.run_pass(_cfg())
    assert {b[0] for b in world["built"]} == {"o2"}


@pytest.mark.asyncio
async def test_a_failed_build_is_counted_and_backs_off_until_it_succeeds(
    world: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    settings_override: Callable[..., None],
) -> None:
    clock = ManualClock(t=1000.0)
    monkeypatch.setattr(w.time, "monotonic", clock.monotonic)
    key = ("o2", "a", "u0")
    world["fail"] = {key}
    settings_override(structured_profile_max_users_per_pass=10)
    result = await w.run_pass(_cfg(("o2", "a")))
    assert (result.errors, result.profiles_written) == (1, 4)

    # The next pass leaves the user out and builds the others.
    world["built"].clear()
    result = await w.run_pass(_cfg(("o2", "a")))
    assert key not in world["built"] and result.errors == 0

    # Each failure doubles the wait, up to the cap.
    first = w._backoff[key].wait
    w._note_failure(key)
    assert w._backoff[key].wait == 2 * first
    for _ in range(20):
        w._note_failure(key)
    assert w._backoff[key].wait == settings.structured_profile_backoff_cap_seconds

    # Once the wait is over the user is built again, and the success clears it. The
    # wait outlasts the last scan, so the pass scans again.
    clock.advance(settings.structured_profile_backoff_cap_seconds)
    world["fail"] = set()
    world["built"].clear()
    await w.run_pass(_cfg(("o2", "a")))
    assert key in world["built"] and key not in w._backoff


@pytest.mark.asyncio
async def test_a_refused_build_waits_then_is_tried_again_when_the_refusal_lapses(
    world: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    settings_override: Callable[..., None],
) -> None:
    clock = ManualClock(t=1000.0)
    monkeypatch.setattr(w.time, "monotonic", clock.monotonic)
    key = ("o2", "a", "u0")
    world["refuse"] = {key}
    settings_override(structured_profile_max_users_per_pass=10)
    result = await w.run_pass(_cfg(("o2", "a")))
    assert (result.refused, result.errors, result.profiles_written) == (1, 0, 4)
    assert w.refused_at("o2", "a", "h" * 64) == {"u0": ("d1", 3)}
    # A new schema is another build: the refusal does not hold under it.
    assert w.refused_at("o2", "a", "g" * 64) == {}

    # While it holds the user is not tried; a lock that ran out with no write is
    # tried again once the refusal lapses.
    world["refuse"] = set()
    world["built"].clear()
    await w.run_pass(_cfg(("o2", "a")))
    assert key not in world["built"]
    # The refusal outlasts the last scan, so the pass scans again.
    clock.advance(settings.structured_profile_refusal_retry_seconds)
    await w.run_pass(_cfg(("o2", "a")))
    assert key in world["built"] and key not in w._refused


def test_waits_that_are_over_are_pruned() -> None:
    w._backoff.clear()
    w._refused.clear()
    cap = settings.structured_profile_backoff_cap_seconds
    w._backoff[("o", "n", "old")] = w._Wait(until=-2 * cap, wait=cap)
    w._backoff[("o", "n", "new")] = w._Wait(until=1e18, wait=300.0)
    w._refused[("o", "n", "lapsed")] = w._Refusal("h", digest="d", profile_version=1, lapses=0.0)
    w._refused[("o", "n", "held")] = w._Refusal("h", digest="d", profile_version=1, lapses=1e18)
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
    high = {"pinned": 0, "lock_mode": "", "lock_until": None, "tags": []}
    assert w._refuses_a_write({**high, "sensitivity": "high"})
    assert w._refuses_a_write({**high, "sensitivity": "", "tags": ["sensitivity:high"]})
    assert not w._refuses_a_write({**high, "sensitivity": "low"})


def _one_user(
    monkeypatch: pytest.MonkeyPatch,
    digest_after: str = "d1",
    existing: dict[str, Any] | None = None,
    head: dict[str, Any] | None = None,
) -> list[Any]:
    """A build of one user who has facts m1 and m2, whose sources digest is "d1" when
    the build starts and `digest_after` once the model has answered; `head` is the
    key's head when it is not the live row (redacted or expired). Returns what was
    written."""
    digests = ["d1", digest_after]

    async def sources_digest(**kw: Any) -> tuple[str, int]:
        return (digests.pop(0), 2)

    async def profile_row(**kw: Any) -> dict[str, Any] | None:
        return existing

    async def profile_head_hold(**kw: Any) -> dict[str, Any] | None:
        return existing or head

    async def user_facts(**kw: Any) -> list[dict[str, Any]]:
        return [
            {"memory_id": "m1", "value": '"My tz is UTC."', "since": "2026-01-01"},
            {"memory_id": "m2", "value": '"I speak en."', "since": "2026-01-02"},
        ]

    async def ask(*a: Any) -> dict[str, Any]:
        return {"tz": "UTC"}

    written: list[Any] = []

    async def write(**kw: Any) -> None:
        written.append(kw)

    monkeypatch.setattr(profile_sources, "sources_digest", sources_digest)
    monkeypatch.setattr(profile_sources, "profile_row", profile_row)
    monkeypatch.setattr(profile_sources, "profile_head_hold", profile_head_hold)
    monkeypatch.setattr(profile_sources, "user_facts", user_facts)
    monkeypatch.setattr(schema_app, "write_structured_profile", write)
    monkeypatch.setattr(w, "_ask", ask)
    return written


_ONE = _schema("o", "n")
_real_write = schema_app.write_structured_profile


@pytest.mark.asyncio
async def test_a_fact_erased_while_the_model_ran_stops_the_write(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    written = _one_user(monkeypatch, digest_after="d2")  # m2 erased during the model call
    assert await w.build_profile(_ONE, "u1") is None
    assert written == []


@pytest.mark.asyncio
async def test_a_fact_edited_while_the_model_ran_stops_the_write(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """m1 moves to a new version (its old one then redacted) during the model call:
    the model read text that is gone, so nothing is written."""
    written = _one_user(monkeypatch)
    calls = 0

    async def sources_digest(**kw: Any) -> tuple[str, int]:
        # Before the call m1 is at version 1, after it at version 2.
        return ("d1" if calls == 0 else "d-m1-v2", 2)

    async def ask(*a: Any) -> dict[str, Any]:
        nonlocal calls
        calls += 1
        return {"tz": "UTC"}

    monkeypatch.setattr(profile_sources, "sources_digest", sources_digest)
    monkeypatch.setattr(w, "_ask", ask)
    assert await w.build_profile(_ONE, "u1") is None
    assert calls == 1 and written == []


@pytest.mark.asyncio
async def test_a_build_reads_only_the_facts_the_authority_serves(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The digest counts m1 and m2 but the authority serves only m1 (m2 changed under
    Postgres, ClickHouse not yet holding it): the profile is built from m1 and tagged
    SHORT, which no digest takes, so it reads as behind."""
    written = _one_user(monkeypatch)
    asked: list[Any] = []

    async def user_facts(**kw: Any) -> list[dict[str, Any]]:
        return [{"memory_id": "m1", "value": '"My tz is UTC."', "since": "2026-01-01"}]

    async def ask(*a: Any) -> dict[str, Any]:
        asked.append(a)
        return {"tz": "UTC"}

    monkeypatch.setattr(profile_sources, "user_facts", user_facts)
    monkeypatch.setattr(w, "_ask", ask)
    assert await w.build_profile(_ONE, "u1") == 0
    assert "I speak en" not in str(asked) and written[0]["memory_ids"] == ["m1"]
    assert written[0]["sources_digest"] == profile_sources.SHORT


@pytest.mark.parametrize(
    ("served", "digest_after", "writes"),
    [
        pytest.param(False, "d1", False, id="none-served"),
        pytest.param(True, "d1", True, id="profile-served"),
        pytest.param(True, "d2", False, id="profile-served-sources-moved"),
    ],
)
@pytest.mark.asyncio
async def test_a_build_with_none_of_its_counted_facts_served(
    monkeypatch: pytest.MonkeyPatch, served: bool, digest_after: str, writes: bool
) -> None:
    """Every fact counted has a change ClickHouse has not published. With no profile
    served nothing is written; a served profile is replaced by the all-null one, never
    kept, since it may carry a source that has left since; sources that moved meanwhile
    stop that write as any other. No model call either way."""
    stale = {
        "version": 1,
        "tags": profile_sources.profile_tags(_ONE.sha256, "d0", 2),
        "pinned": 0,
        "lock_mode": "",
        "lock_until": None,
        "sensitivity": "",
    }
    written = _one_user(monkeypatch, digest_after=digest_after, existing=stale if served else None)
    asked: list[Any] = []

    async def user_facts(**kw: Any) -> list[dict[str, Any]]:
        return []

    async def ask(*a: Any) -> dict[str, Any]:
        asked.append(a)
        return {}

    monkeypatch.setattr(profile_sources, "user_facts", user_facts)
    monkeypatch.setattr(w, "_ask", ask)
    dropped = await w.build_profile(_ONE, "u1")
    assert asked == []
    if writes:
        assert dropped == 0
        (row,) = written
        assert row["value"] == {"tz": None}
        assert row["memory_ids"] == [] and row["source_count"] == 2
        assert row["sources_digest"] == profile_sources.SHORT
    else:
        assert dropped is None and written == []


@pytest.mark.parametrize(("count", "limit"), [(2, 10), (5, 2)])
@pytest.mark.asyncio
async def test_a_build_that_read_every_fact_it_could_carries_the_digest(
    monkeypatch: pytest.MonkeyPatch, count: int, limit: int
) -> None:
    """Two facts read: all of them, or as many as the limit lets a build read."""
    written = _one_user(monkeypatch)

    async def sources_digest(**kw: Any) -> tuple[str, int]:
        return ("d1", count)

    monkeypatch.setattr(profile_sources, "sources_digest", sources_digest)
    monkeypatch.setattr(settings, "structured_profile_memory_limit", limit)
    assert await w.build_profile(_ONE, "u1") == 0
    assert written[0]["sources_digest"] == "d1"


@pytest.mark.asyncio
async def test_the_user_facts_are_those_the_authority_serves(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.core import pg
    from server.identity.domain.read_scope import WHOLE_NAMESPACE
    from server.memory.store import pg_memory

    def _fact(mid: str, key: str) -> dict[str, Any]:
        return {
            "memory_id": mid,
            "org": "o",
            "namespace": "n",
            "agent": "a",
            "user_id": "u1",
            "session_id": "-",
            "memory_key": key,
            "value": "v",
            "version": 1,
            "redacted_at": None,
        }

    class _CH:
        async def query_all(self, sql: str, params: Any = None) -> list[dict[str, Any]]:
            return [_fact("m1", "user_tz"), _fact("m2", "user_lang")]

    async def heads(keys: list[Any], **_k: Any) -> dict[Any, Any]:
        return {k: {"version": 1, "redacted_at": None} for k in keys if k[5] == "user_tz"}

    monkeypatch.setattr(pg, "enabled", lambda: True)
    monkeypatch.setattr(pg_memory, "heads_by_keys", heads)
    rows = await profile_sources.user_facts(
        org="o",
        namespace="n",
        user_id="u1",
        types=profile_sources.STRUCTURED_TYPES,
        limit=10,
        scope=WHOLE_NAMESPACE,
        ch=_CH(),
    )
    assert [r["memory_id"] for r in rows] == ["m1"]


@pytest.mark.asyncio
async def test_a_build_writes_its_label_tags_and_sources(monkeypatch: pytest.MonkeyPatch) -> None:
    from server.settings import settings

    _one_user(monkeypatch)
    monkeypatch.setattr(schema_app, "write_structured_profile", _real_write)
    sections: list[Any] = []

    async def write_section(mset: Any) -> None:
        sections.append(mset)

    monkeypatch.setattr(schema_app, "write_section", write_section)
    monkeypatch.setattr(settings, "structured_profile_label", "my_profile")
    assert await w.build_profile(_ONE, "u1") == 0
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
    written = _one_user(monkeypatch, existing=current)
    assert await w.build_profile(_ONE, "u1") is None
    assert written == []


@pytest.mark.asyncio
async def test_a_pinned_profile_is_refused_before_the_model_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pinned = {"version": 7, "pinned": 1, "lock_mode": "", "lock_until": None, "tags": []}
    written = _one_user(monkeypatch, existing=pinned)

    async def no_call(*a: Any) -> dict[str, Any]:
        raise AssertionError("no model call for a refused build")

    monkeypatch.setattr(w, "_ask", no_call)
    with pytest.raises(w.Refused) as e:
        await w.build_profile(_ONE, "u1")
    assert (e.value.digest, e.value.profile_version) == ("d1", 7) and written == []


@pytest.mark.asyncio
async def test_a_pin_on_a_redacted_head_is_refused_before_the_model_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A redacted head reads as no profile, but its pin still refuses the write, at the
    # head's version.
    pinned = {"version": 4, "pinned": 1, "lock_mode": "", "lock_until": None}
    written = _one_user(monkeypatch, head=pinned)

    async def no_call(*a: Any) -> dict[str, Any]:
        raise AssertionError("no model call for a refused build")

    monkeypatch.setattr(w, "_ask", no_call)
    with pytest.raises(w.Refused) as e:
        await w.build_profile(_ONE, "u1")
    assert (e.value.digest, e.value.profile_version) == ("d1", 4) and written == []


@pytest.mark.asyncio
async def test_a_high_profile_keeps_its_label(monkeypatch: pytest.MonkeyPatch) -> None:
    # A high head reads as no profile; a rebuild would write an unlabelled row over it,
    # so it holds like a pin, at the head's version, and nothing is written.
    high = {
        "version": 3,
        "pinned": 0,
        "lock_mode": "",
        "lock_until": None,
        "sensitivity": "high",
        "tags": [],
    }
    written = _one_user(monkeypatch, head=high)

    async def no_call(*a: Any) -> dict[str, Any]:
        raise AssertionError("no model call for a high profile")

    monkeypatch.setattr(w, "_ask", no_call)
    with pytest.raises(w.Refused) as e:
        await w.build_profile(_ONE, "u1")
    assert (e.value.digest, e.value.profile_version) == ("d1", 3) and written == []


@pytest.mark.asyncio
async def test_a_write_the_row_refuses_is_a_refusal_carrying_the_digest(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _one_user(monkeypatch)

    async def refuse(**kw: Any) -> None:
        raise HTTPException(status_code=422, detail={"code": "CHAR_LIMIT_EXCEEDED"})

    monkeypatch.setattr(schema_app, "write_structured_profile", refuse)
    with pytest.raises(w.Refused) as e:
        await w.build_profile(_ONE, "u1")
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
        answer = await w._ask("o", "n", _PROPS, "- [2026-01-01] My tz is UTC.")
    assert asked == ["profile-model"] and answer == {"tz": "My tz is UTC"}


@pytest.mark.asyncio
async def test_the_largest_accepted_schema_gets_room_for_a_full_answer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The cap is the schema's estimate of a full answer, so a schema at the answer
    bound is not cut off at a fixed cap."""
    from server.llm import customer
    from server.llm.fake import FakeLLMClient
    from server.settings.budgets import llm_max_tokens

    caps: list[int] = []

    class Recording(FakeLLMClient):
        async def complete(self, system: str, user: str, max_tokens: int = 1024, **kw: Any) -> Any:
            caps.append(max_tokens)
            return await super().complete(system, user, max_tokens, **kw)

    tags = {"type": "array", "items": {"type": "string"}, "description": "tags"}
    arrays = ps.MAX_ANSWER_VALUES // ps.MAX_ITEMS
    singles = ps.MAX_ANSWER_VALUES - arrays * ps.MAX_ITEMS
    props = ps.parse(
        {
            "type": "object",
            "properties": {
                **{f"a{i}": tags for i in range(arrays)},
                **{f"s{i}": {"type": "string", "description": "s"} for i in range(singles)},
            },
        }
    )
    assert ps.answer_values(props) == ps.MAX_ANSWER_VALUES
    monkeypatch.setattr(settings, "structured_profile_model", "")
    monkeypatch.setattr(w.llm_client, "get_llm_client", Recording)
    monkeypatch.setattr(w, "emit_worker_cost_event_for", lambda **kw: None)
    with customer.for_org("o"):
        await w._ask("o", "n", props, "- [2026-01-01] My tz is UTC.")
    assert caps == [llm_max_tokens("structured_profile") + ps.answer_tokens(props)]
    assert caps[0] > ps.MAX_ANSWER_VALUES * ps.STRING_TOKENS


async def _cap_for(monkeypatch: pytest.MonkeyPatch, schema: dict[str, Any]) -> int:
    """The output cap `_ask` asks for under `schema`."""
    from server.llm import customer
    from server.llm.fake import FakeLLMClient

    caps: list[int] = []

    class Recording(FakeLLMClient):
        async def complete(self, system: str, user: str, max_tokens: int = 1024, **kw: Any) -> Any:
            caps.append(max_tokens)
            return await super().complete(system, user, max_tokens, **kw)

    monkeypatch.setattr(settings, "structured_profile_model", "")
    monkeypatch.setattr(w.llm_client, "get_llm_client", Recording)
    monkeypatch.setattr(w, "emit_worker_cost_event_for", lambda **kw: None)
    with customer.for_org("o"):
        await w._ask("o", "n", ps.parse(schema), "- [2026-01-01] I grew up in Lyon.")
    return caps[0]


@pytest.mark.asyncio
async def test_a_free_text_property_gets_room_for_its_max_length(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A long string value fits: the cap pays for the key and its punctuation as well as
    a value of maxLength characters, and gives a string with no maxLength a default."""
    summary = {"type": "string", "description": "A short summary of the user's background"}
    bounded = {"type": "object", "properties": {"summary": {**summary, "maxLength": 600}}}
    # A value of 600 characters, at a low estimate of 2 characters per token, and the
    # tokens of `{"summary": "` and `"}` around it.
    assert await _cap_for(monkeypatch, bounded) >= 600 // 2 + 10
    free = {"type": "object", "properties": {"summary": summary}}
    assert await _cap_for(monkeypatch, free) >= ps.STRING_TOKENS + 10


@pytest.mark.asyncio
async def test_an_answer_cut_off_at_the_cap_is_refused_not_retried(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The same facts under the same schema are cut off again, so a cut-off answer is a
    refusal held until the user's memories or the schema change, never a backoff."""
    from server.llm import customer
    from server.llm.client import LLMResult

    class CutOff:
        async def complete(self, system: str, user: str, max_tokens: int = 1024, **kw: Any) -> Any:
            return LLMResult('{"tz": "Eur', 10, max_tokens, "fake", "m", stop_reason="length")

    monkeypatch.setattr(settings, "structured_profile_model", "")
    monkeypatch.setattr(w.llm_client, "get_llm_client", CutOff)
    monkeypatch.setattr(w, "emit_worker_cost_event_for", lambda **kw: None)
    with customer.for_org("o"), pytest.raises(w.CutOff):
        await w._ask("o", "n", _PROPS, "- [2026-01-01] My tz is Europe/Berlin.")

    written = _one_user(monkeypatch)

    async def ask(*a: Any) -> dict[str, Any]:
        raise w.CutOff("the answer was cut off at 32 tokens")

    monkeypatch.setattr(w, "_ask", ask)
    with pytest.raises(w.Refused, match="cut off"):
        await w.build_profile(_ONE, "u1")
    assert written == []


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
    _one_user(monkeypatch)

    async def answer(**kw: Any) -> None:
        raise HTTPException(status_code=status, detail={"code": code})

    monkeypatch.setattr(schema_app, "write_structured_profile", answer)
    with pytest.raises(w.Refused if refused else HTTPException):
        await w.build_profile(_ONE, "u1")


@pytest.mark.asyncio
async def test_one_scan_serves_the_passes_after_it(
    world: dict[str, Any], settings_override: Callable[..., None]
) -> None:
    settings_override(structured_profile_max_users_per_pass=1)
    cfg = _cfg(("o2", "a"))
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
    world: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    settings_override: Callable[..., None],
) -> None:
    monkeypatch.setattr(w, "SCAN_FACTOR", 1)  # the scan finds as many as it asks for
    settings_override(structured_profile_max_users_per_pass=1)
    cfg = _cfg(("o2", "a"))
    await w.run_pass(cfg)
    await w.run_pass(cfg)
    assert world["scans"] == 2 and len(world["built"]) == 2


@pytest.mark.asyncio
async def test_a_failing_namespace_backs_off_without_keeping_the_pass_busy(
    world: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    settings_override: Callable[..., None],
) -> None:
    async def failing(**kw: Any) -> list[Behind]:
        world["scans"] += 1
        raise RuntimeError("the scan failed")

    clock = ManualClock(t=1000.0)
    monkeypatch.setattr(w.time, "monotonic", clock.monotonic)
    monkeypatch.setattr(profile_sources, "users_behind", failing)
    settings_override(structured_profile_max_users_per_pass=10)
    cfg = _cfg(("o2", "a"))
    assert (await w.run_pass(cfg)).errors == 1
    # Backing off, the namespace is not scanned and the pass is idle.
    ctx = pass_context.begin("structured_profile")
    result = await w.run_pass(cfg)
    assert world["scans"] == 1 and result.errors == 0 and ctx.idle
    # Its first wait is the base backoff, and it is scanned again once that is over.
    clock.advance(settings.structured_profile_backoff_base_seconds - 1)
    await w.run_pass(cfg)
    assert world["scans"] == 1
    clock.advance(1)
    await w.run_pass(cfg)
    assert world["scans"] == 2


@pytest.mark.asyncio
async def test_a_schema_change_or_an_old_scan_scans_again(
    world: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    settings_override: Callable[..., None],
) -> None:
    clock = ManualClock(t=1000.0)
    monkeypatch.setattr(w.time, "monotonic", clock.monotonic)
    settings_override(structured_profile_max_users_per_pass=1)
    cfg = _cfg(("o2", "a"))
    await w.run_pass(cfg)
    clock.advance(settings.structured_profile_scan_max_age_seconds + 1)
    await w.run_pass(cfg)
    assert world["scans"] == 2
    w._scans[("o2", "a")].schema_sha256 = "x" * 64
    await w.run_pass(cfg)
    assert world["scans"] == 3


@pytest.mark.asyncio
async def test_the_leftover_budget_takes_from_the_scan_already_made(
    world: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    settings_override: Callable[..., None],
) -> None:
    async def users_behind(**kw: Any) -> list[Behind]:
        world["scans"] += 1
        behind = {("o1", "a"): ["u0"], ("o1", "b"): _USERS, ("o2", "a"): []}
        return [Behind(u, "d1", 0) for u in behind[(kw["org"], kw["namespace"])][: kw["limit"]]]

    monkeypatch.setattr(profile_sources, "users_behind", users_behind)
    settings_override(structured_profile_max_users_per_pass=6)
    await w.run_pass(_cfg())
    assert _per_space(world["built"]) == {("o1", "a"): 1, ("o1", "b"): 5, ("o2", "a"): 0}
    assert world["scans"] == 3  # one per namespace, none for the leftover round


@pytest.mark.asyncio
async def test_a_schema_that_does_not_parse_is_counted_and_passed_over(
    world: dict[str, Any],
    settings_override: Callable[..., None],
) -> None:
    world["bad"] = {("o1", "a")}
    ctx = pass_context.begin("structured_profile")
    settings_override(structured_profile_max_users_per_pass=3)
    result = await w.run_pass(_cfg())
    assert result.errors == 1 and ctx.count == 1
    assert ("o1", "a") not in _spaces_built(world["built"])
    assert ("o1", "a") in w._namespace_backoff  # it backs off like a failing scan


@pytest.mark.asyncio
async def test_a_waiting_namespace_does_not_take_the_only_place_in_a_pass(
    world: dict[str, Any],
    settings_override: Callable[..., None],
) -> None:
    w._note_namespace_failure("o1", "a", "h" * 64)
    settings_override(structured_profile_max_users_per_pass=1)
    await w.run_pass(_cfg())
    assert _spaces_built(world["built"]) == [("o1", "b")]


@pytest.mark.asyncio
async def test_a_new_schema_ends_a_namespace_wait(
    world: dict[str, Any], settings_override: Callable[..., None]
) -> None:
    w._note_namespace_failure("o1", "a", "x" * 64)  # it failed under an older schema
    settings_override(structured_profile_max_users_per_pass=1)
    await w.run_pass(_cfg())
    assert _spaces_built(world["built"]) == [("o1", "a")]


@pytest.mark.asyncio
async def test_a_pass_with_nothing_to_build_reaches_every_namespace_and_is_idle(
    world: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    settings_override: Callable[..., None],
) -> None:
    async def nobody_behind(**kw: Any) -> list[Behind]:
        world["scans"] += 1
        return []

    monkeypatch.setattr(profile_sources, "users_behind", nobody_behind)
    # Three namespaces and a budget of two: the pass reads on past its first page.
    settings_override(structured_profile_max_users_per_pass=2)
    ctx = pass_context.begin("structured_profile")
    result = await w.run_pass(_cfg())
    assert result.namespaces_reached == 3 and world["scans"] == 3
    assert ctx.idle


@pytest.mark.asyncio
async def test_a_pass_reads_past_namespaces_with_nothing_to_build(
    world: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    settings_override: Callable[..., None],
) -> None:
    spaces = [("o", f"n{i:02d}") for i in range(30)]
    monkeypatch.setattr(sys.modules[__name__], "_SPACES", spaces)

    async def only_the_25th_behind(**kw: Any) -> list[Behind]:
        behind = ["u0"] if (kw["org"], kw["namespace"]) == spaces[24] else []
        return [Behind(u, "d1", 0) for u in behind]

    monkeypatch.setattr(profile_sources, "users_behind", only_the_25th_behind)
    settings_override(structured_profile_max_users_per_pass=5)
    ctx = pass_context.begin("structured_profile")
    result = await w.run_pass(_cfg())
    assert world["built"] == [("o", "n24", "u0")]
    assert result.namespaces_reached == 30 and not ctx.idle


@pytest.mark.asyncio
async def test_a_pass_that_spent_its_budget_on_current_profiles_is_not_idle(
    world: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    settings_override: Callable[..., None],
) -> None:
    async def already_current(schema: w.ActiveSchema, user_id: str) -> int | None:
        return None

    monkeypatch.setattr(w, "build_profile", already_current)
    # The page holds all three namespaces, but the first two use the budget of four.
    settings_override(structured_profile_max_users_per_pass=4)
    ctx = pass_context.begin("structured_profile")
    result = await w.run_pass(_cfg())
    assert result.namespaces_reached == 2 and not result.profiles_written
    assert not ctx.idle


@pytest.mark.asyncio
async def test_a_pass_that_took_users_and_wrote_none_is_not_idle(
    world: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    settings_override: Callable[..., None],
) -> None:
    async def sources_moved(schema: w.ActiveSchema, user_id: str) -> int | None:
        return None

    monkeypatch.setattr(w, "build_profile", sources_moved)
    # One namespace, reached whole: users are still behind in its scan list.
    settings_override(structured_profile_max_users_per_pass=2)
    ctx = pass_context.begin("structured_profile")
    result = await w.run_pass(_cfg(only=("o1", "a")))
    assert result.namespaces_reached == 1 and not result.profiles_written
    assert not ctx.idle


@pytest.mark.asyncio
async def test_a_user_with_no_facts_and_no_profile_gets_no_row(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A user erased after a scan listed them: the cached entry writes nothing."""
    written = _one_user(monkeypatch)

    async def nothing(**kw: Any) -> tuple[str, int]:
        return ("", 0)

    async def no_facts(**kw: Any) -> list[dict[str, Any]]:
        raise AssertionError("no facts are read for a user with no sources")

    monkeypatch.setattr(profile_sources, "sources_digest", nothing)
    monkeypatch.setattr(profile_sources, "user_facts", no_facts)
    assert await w.build_profile(_ONE, "u1") is None
    assert written == []
