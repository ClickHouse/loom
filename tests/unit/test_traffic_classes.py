"""Traffic classes — benches never degrade real users.

Pure: fake drivers, a FakeClock limiter and monkeypatched settings; no
ClickHouse. Covers class resolution precedence, the request hint, the
registry cache, bucket keying in both limiter backends, the per-class
ClickHouse settings composer, the LLM tier policy, the ledger column, the
cost.summary dimension, the token claim and the registry row.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from server.core import ch as ch_mod
from server.core import ch_class_settings, ratelimit
from server.core.ratelimit import DEFAULT_LIMITS, Limit, Limiter
from server.identity.app import traffic_class_registry as reg
from server.identity.domain import traffic_class_policy as policy
from server.observability import traffic_class as tc


class FakeClock:
    def __init__(self) -> None:
        self._t = 0.0

    def __call__(self) -> float:
        return self._t

    def advance(self, dt: float) -> None:
        self._t += dt


@pytest.fixture(autouse=True)
def _clean_context() -> Any:
    tc.set_current_traffic_class("")
    reg.forget_all()
    # a neighbour's query tag / sequential-read scope must not leak into the
    # settings the composer is asserted on (random order)
    tag_token = ch_mod.query_tag_var.set("")
    seq_token = ch_mod.sequential_reads_var.set(False)
    yield
    ch_mod.query_tag_var.reset(tag_token)
    ch_mod.sequential_reads_var.reset(seq_token)
    tc.set_current_traffic_class("")
    reg.forget_all()


# ---------------------------------------------------------------- resolution


def test_normalize_and_tags() -> None:
    assert tc.normalize_class(" Bench ") == "bench"
    assert tc.normalize_class("prod") == ""
    assert tc.normalize_class(True) == ""  # type: ignore[arg-type]
    assert tc.class_from_tags(["consumer:x", "traffic_class:interactive"]) == "interactive"
    assert tc.class_from_tags(["traffic_class:nope"]) == ""
    assert tc.class_from_tags("traffic_class:bench") == ""  # type: ignore[arg-type]
    # the last valid tag wins (a re-registration that appended one supersedes)
    assert tc.class_from_tags(["traffic_class:bench", "traffic_class:production"]) == "production"


def test_registry_row_class() -> None:
    assert tc.class_from_registry_row(None) == ""
    assert tc.class_from_registry_row({"kind": "bench", "tags": []}) == "bench"
    assert (
        tc.class_from_registry_row({"kind": "memory", "tags": ["auto-registered"]}) == "production"
    )
    assert tc.class_from_registry_row({"kind": "code", "tags": []}) == "production"
    # a tag on the row wins over the kind
    assert (
        tc.class_from_registry_row({"kind": "memory", "tags": ["traffic_class:interactive"]})
        == "interactive"
    )
    assert (
        tc.class_from_registry_row({"kind": "bench", "tags": ["traffic_class:production"]})
        == "production"
    )


def test_request_hint_field_then_tag_then_consumer() -> None:
    assert tc.request_hint({"traffic_class": "bench"}) == "bench"
    assert (
        tc.request_hint({"traffic_class": "bench", "tags": ["traffic_class:production"]}) == "bench"
    )
    assert tc.request_hint({"tags": ["traffic_class:production"]}) == "production"
    assert tc.request_hint({"consumer": "membench"}) == "bench"
    assert tc.request_hint({"consumer": "kcurve-bench"}) == "bench"
    assert tc.request_hint({"consumer": "support-bot"}) == ""
    assert tc.request_hint({}) == "" and tc.request_hint(None) == ""


def test_namespace_prefix_hint_follows_the_bench_prefix(settings_override) -> None:
    settings_override(namespace_bench_prefixes="lme-, kcurve-")
    assert reg.namespace_prefix_hint("kcurve-3") == "bench"
    assert reg.namespace_prefix_hint("clickgap-public") == ""
    assert reg.namespace_prefix_hint(None) == ""


def test_resolution_precedence_claim_then_row_then_hint_then_interactive() -> None:
    assert tc.resolve_traffic_class(claim="production", registry_class="bench", hint="bench") == (
        "production",
        tc.SOURCE_CLAIM,
    )
    assert tc.resolve_traffic_class(claim="", registry_class="bench", hint="production") == (
        "bench",
        tc.SOURCE_NAMESPACE,
    )
    assert tc.resolve_traffic_class(claim="", registry_class="", hint="bench") == (
        "bench",
        tc.SOURCE_HINT,
    )
    assert tc.resolve_traffic_class() == ("interactive", tc.SOURCE_DEFAULT)
    # an unknown claim value is not a claim
    assert tc.resolve_traffic_class(claim="vip", registry_class="production") == (
        "production",
        tc.SOURCE_NAMESPACE,
    )


def test_context_var_and_header() -> None:
    assert tc.current_traffic_class() == "" and tc.header_value() == ""
    tc.set_current_traffic_class("bench", tc.SOURCE_NAMESPACE)
    assert tc.current_traffic_class() == "bench"
    assert tc.current_traffic_class_source() == "namespace"
    assert tc.header_value() == "bench;source=namespace"
    tc.set_current_traffic_class("nonsense")
    assert tc.current_traffic_class() == ""


def test_background_is_a_bound_class_with_its_own_ch_settings(monkeypatch) -> None:
    """Worker passes and operator CLIs are classified: unclassified they would
    share the base bucket, priority 0, competing with production reads on equal
    terms during a heal's large scans. `bind_background` classes the task; every
    read statement then carries the class's settings, writes carry none;
    nothing resolves to it from a request."""
    from server.core import ch as ch_mod
    from server.settings import settings

    assert tc.normalize_class("background") == tc.BACKGROUND and tc.BACKGROUND in tc.TRAFFIC_CLASSES
    # a request never lands in it: claim / row / hint are the only doors, and they don't say background
    assert tc.resolve_traffic_class(hint="background") == (
        "background",
        tc.SOURCE_HINT,
    )  # explicit hint is honoured
    assert tc.resolve_traffic_class() == ("interactive", tc.SOURCE_DEFAULT)
    tc.bind_background("worker:code_ingest")
    assert tc.current_traffic_class() == "background"
    assert tc.current_traffic_class_source() == "worker:code_ingest"
    monkeypatch.setattr(ch_class_settings, "_CLASS_SETTINGS_RAW", None)
    got = ch_class_settings.traffic_class_settings("background")
    assert got["priority"] == 5 and got["max_threads"] == 3
    assert got["read_from_filesystem_cache_if_exists_otherwise_bypass_cache"] == 1
    read = ch_mod.statement_settings("SELECT count() FROM loom.code_chunks")
    assert read["priority"] == 5 and read["max_threads"] == 3
    write = ch_mod.statement_settings("INSERT INTO loom.code_chunks (a) VALUES (1)")
    assert "priority" not in write and "max_threads" not in write
    # a caller's own value still wins
    assert ch_mod.statement_settings("SELECT 1", {"max_threads": 1})["max_threads"] == 1
    assert settings.ch_settings_by_traffic_class.count('"background"') == 1


def test_bind_background_is_undone_by_its_token_and_bound_per_worker_task() -> None:
    """The CLI and a test undo the binding; the supervisor binds in
    the worker's own task, so a direct `_supervise` call leaves the caller as it was."""
    import asyncio

    from server.workers import supervisor as sv

    before = tc.current_traffic_class()
    token = tc.bind_background("cli:code")
    assert (
        tc.current_traffic_class() == "background"
        and tc.current_traffic_class_source() == "cli:code"
    )
    tc.reset_traffic_class(token)
    assert tc.current_traffic_class() == before

    seen: list[str] = []

    class _Spec:
        name = "fake_pass"

    async def fake_supervise(self, spec):
        seen.append(tc.current_traffic_class())

    original = sv.WorkerSupervisor._supervise
    sv.WorkerSupervisor._supervise = fake_supervise  # type: ignore[method-assign]
    try:

        async def run() -> None:
            await asyncio.create_task(sv.WorkerSupervisor()._supervise_as_background(_Spec()))
            seen.append(tc.current_traffic_class())  # the creating task is untouched

        asyncio.run(run())
    finally:
        sv.WorkerSupervisor._supervise = original  # type: ignore[method-assign]
    assert seen == ["background", before]


# ---------------------------------------------------------------- registry cache


class _RegistryCH:
    def __init__(self, row: dict[str, Any] | None) -> None:
        self.row = row
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def query_one(
        self, sql: str, params: dict[str, Any] | None = None
    ) -> dict[str, Any] | None:
        self.calls.append((sql, params or {}))
        return self.row


@pytest.mark.asyncio
async def test_registry_class_reads_once_and_caches_a_miss_too() -> None:
    ch = _RegistryCH({"kind": "bench", "tags": []})
    assert await reg.registry_class("o", "kcurve-0", ch=ch) == "bench"
    assert await reg.registry_class("o", "kcurve-0", ch=ch) == "bench"
    assert len(ch.calls) == 1
    assert "FROM loom.namespaces FINAL" in ch.calls[0][0]
    assert ch.calls[0][1] == {"org": "o", "ns": "kcurve-0"}
    assert reg.cached_class("o", "kcurve-0") == "bench"
    miss = _RegistryCH(None)
    assert await reg.registry_class("o", "fresh", ch=miss) == ""
    assert await reg.registry_class("o", "fresh", ch=miss) == ""
    assert len(miss.calls) == 1 and reg.cached_class("o", "fresh") == ""
    # missing / wildcard identity is never read
    assert await reg.registry_class("o", "*", ch=miss) == "" and len(miss.calls) == 1
    assert await reg.registry_class(None, "x", ch=miss) == ""


@pytest.mark.asyncio
async def test_registry_cache_expires_and_note_registered_fills_it(
    settings_override, monkeypatch
) -> None:
    settings_override(traffic_class_registry_ttl_seconds=10)
    now = [1000.0]
    monkeypatch.setattr(reg.time, "monotonic", lambda: now[0])
    ch = _RegistryCH({"kind": "memory", "tags": []})
    assert await reg.registry_class("o", "n", ch=ch) == "production"
    now[0] += 11
    assert reg.cached_class("o", "n") is None
    assert await reg.registry_class("o", "n", ch=ch) == "production" and len(ch.calls) == 2
    reg.note_registered("o", "fresh", {"kind": "bench", "tags": ["auto-registered", "bench"]})
    assert reg.cached_class("o", "fresh") == "bench"


@pytest.mark.asyncio
async def test_registry_read_failure_is_unclassified_not_an_error() -> None:
    class Boom:
        async def query_one(self, sql: str, params: Any = None) -> Any:
            raise RuntimeError("down")

    assert await reg.registry_class("o", "n", ch=Boom()) == ""
    assert reg.cached_class("o", "n") is None  # retried next time


@pytest.mark.asyncio
async def test_classify_cached_then_classify_read_after_authorization(monkeypatch) -> None:
    ch = _RegistryCH({"kind": "bench", "tags": []})
    monkeypatch.setattr("server.core.ch.get_client", lambda: ch)
    auth = SimpleNamespace(traffic_class="")
    # before authorization: nothing cached, no hint → interactive, no read
    assert reg.classify_cached(auth, "o", "somens", {}) == ("interactive", tc.SOURCE_DEFAULT)
    assert ch.calls == []
    # after authorization: the row is read once and the class is the row's
    assert await reg.classify(auth, "o", "somens", {}) == ("bench", tc.SOURCE_NAMESPACE)
    assert len(ch.calls) == 1
    # now the pre-authorization view answers from the cache
    assert reg.classify_cached(auth, "o", "somens", {}) == ("bench", tc.SOURCE_NAMESPACE)
    # a claim settles it without a read
    claimed = SimpleNamespace(traffic_class="production")
    assert await reg.classify(claimed, "o", "other", {}) == ("production", tc.SOURCE_CLAIM)
    assert len(ch.calls) == 1
    # the request hint counts before the row is known
    assert reg.classify_cached(auth, "o", "third", {"consumer": "membench"}) == (
        "bench",
        tc.SOURCE_HINT,
    )


# ---------------------------------------------------------------- rate buckets


@pytest.fixture
def fractions(settings_override):
    def _set(raw: str) -> None:
        settings_override(ratelimit_class_fractions=raw)

    _set('{"bench": 0.5}')
    return _set


def test_bucket_class_and_fraction_parsing(fractions, caplog) -> None:
    assert ratelimit.bucket_class("") == "" and ratelimit.bucket_class("production") == ""
    # interactive shares the base bucket: an unregistered namespace is bound by
    # the caps the operator set, never given a budget beside them
    assert ratelimit.bucket_class("interactive") == ""
    assert ratelimit.bucket_class("Bench") == "bench"
    assert ratelimit.CARVED_CLASSES == {"bench"}
    assert ratelimit.class_fractions() == {"bench": 0.5}
    fractions('{"bench": 0.25, "interactive": 0.8}')
    assert ratelimit.class_fractions() == {"bench": 0.25, "interactive": 0.8}
    fractions("not json")
    assert ratelimit.class_fractions() == {}
    fractions('{"bench": 0}')
    assert ratelimit.class_fractions() == {}
    fractions("")
    assert ratelimit.class_fractions() == {}


def test_bench_bucket_is_separate_smaller_and_never_touches_production(fractions) -> None:
    lim = Limiter(clock=FakeClock())
    lim.set_namespace_limit("ns", "memory", Limit(capacity=4, rate=0))
    # bench: half the cap, its own bucket
    for _ in range(2):
        assert lim.check("ns", "memory.search", traffic_class="bench").allowed
    refused = lim.check("ns", "memory.search", traffic_class="bench")
    assert not refused.allowed and refused.limit == 2.0
    # production still has its full four
    for _ in range(4):
        r = lim.check("ns", "memory.search", traffic_class="production")
        assert r.allowed and r.limit == 4.0
    assert not lim.check("ns", "memory.search", traffic_class="production").allowed
    # the unclassified check IS the base bucket (the key the limiter always had)
    assert not lim.check("ns", "memory.search").allowed
    # interactive shares the base bucket: drained by production, it is refused
    # too — a class is never a way around the configured cap
    r = lim.check("ns", "memory.search", traffic_class="interactive")
    assert not r.allowed and r.limit == 4.0
    keys = set(lim._ns_buckets)
    assert keys == {("", "ns", "memory"), ("", "ns", "memory", "bench")}


def test_org_and_actor_tiers_key_by_class_too(fractions, settings_override) -> None:
    settings_override(ratelimit_actor_fraction=0.5)
    lim = Limiter(clock=FakeClock())
    lim.set_namespace_limit("ns", "memory", Limit(capacity=8, rate=0))
    lim.set_org_limit("org", "memory", Limit(capacity=8, rate=0))
    # bench: ns 4, org 4, actor 2 (half of the bench ns bucket)
    for _ in range(2):
        assert lim.check("ns", "memory.set", org="org", actor="sa:b", traffic_class="bench").allowed
    r = lim.check("ns", "memory.set", org="org", actor="sa:b", traffic_class="bench")
    assert not r.allowed and r.limit == 2.0
    assert ("org", "ns", "sa:b", "memory", "bench") in lim._actor_buckets
    assert ("org", "memory", "bench") in lim._org_buckets
    # production's org bucket is untouched: eight production calls pass
    for _ in range(8):
        assert lim.check("ns", "memory.set", org="org", traffic_class="production").allowed
    assert not lim.check("ns", "memory.set", org="org", traffic_class="production").allowed


def test_interactive_is_bound_by_the_base_caps_from_the_first_request(fractions) -> None:
    """An unregistered namespace
    resolves to interactive, which must draw from the org and namespace
    buckets the operator capped, not from a fresh budget beside them."""
    fractions('{"bench": 0.5, "interactive": 0.8}')  # a fraction for a non-carved class is inert
    lim = Limiter(clock=FakeClock())
    lim.set_namespace_limit("ns", "memory", Limit(capacity=3, rate=0))
    lim.set_org_limit("org", "memory", Limit(capacity=5, rate=0))
    served = 0
    for i in range(9):
        r = lim.check(
            f"ns{i % 2}" if i else "ns", "memory.set", org="org", traffic_class="interactive"
        )
        served += int(r.allowed)
    assert served == 5  # the org cap of 5 holds across interactive requests
    lim2 = Limiter(clock=FakeClock())
    lim2.set_namespace_limit("ns", "memory", Limit(capacity=3, rate=0))
    assert (
        sum(
            int(lim2.check("ns", "memory.set", traffic_class="interactive").allowed)
            for _ in range(4)
        )
        == 3
    )
    assert ("", "ns", "memory") in lim2._ns_buckets and len(lim2._ns_buckets) == 1


def test_default_fraction_map_and_floor(fractions) -> None:
    fractions('{"bench": 0.001}')
    lim = Limiter(clock=FakeClock())
    lim.set_namespace_limit("ns", "memory", Limit(capacity=10, rate=0))
    r = lim.check("ns", "memory.set", traffic_class="bench")
    assert r.allowed and r.limit == 1.0  # capacity floored at one token
    assert not lim.check("ns", "memory.set", traffic_class="bench").allowed


def test_override_resizes_every_class_bucket_it_reaches(fractions) -> None:
    lim = Limiter(clock=FakeClock())
    lim.check("ns", "memory.set")
    lim.check("ns", "memory.set", traffic_class="bench")
    lim.set_namespace_limit("ns", "memory", Limit(capacity=40, rate=1))
    assert lim._ns_buckets[("", "ns", "memory")].capacity == 40
    assert lim._ns_buckets[("", "ns", "memory", "bench")].capacity == 20
    assert lim._ns_buckets[("", "ns", "memory", "bench")].rate == 0.5


def test_shared_store_keys_carry_the_class(fractions, settings_override, tmp_path: Path) -> None:
    from server.core.ratelimit_shared import VERSION, SharedBucketStore, key_hash

    assert VERSION == 2  # bucket keys gained the class (file format unchanged)
    path = str(tmp_path / "rl.buckets")
    settings_override(ratelimit_shared="on", ratelimit_shared_path=path, ratelimit_shared_slots=256)
    clk = FakeClock()
    lim = Limiter(clock=clk)
    assert lim.shared_active
    lim.set_namespace_limit("ns", "memory", Limit(capacity=4, rate=0))
    for _ in range(2):
        assert lim.check("ns", "memory.set", traffic_class="bench").allowed
    assert not lim.check("ns", "memory.set", traffic_class="bench").allowed
    for _ in range(4):
        assert lim.check("ns", "memory.set", traffic_class="production").allowed
    assert not lim.check("ns", "memory.set").allowed
    # the production key is the classless key; bench has its own slot
    store = SharedBucketStore(path, 256, clock=clk)
    assert key_hash("ns\0\0ns\0memory") != key_hash("ns\0\0ns\0memory\0bench")
    remaining = store.check([("ns\0\0ns\0memory\0bench", 2.0, 0.0)])
    assert not remaining.allowed  # the bench slot is the one the limiter drained
    store.close()
    lim._store().close()  # type: ignore[union-attr]


def test_health_status_names_the_fractions(fractions) -> None:
    lim = Limiter(clock=FakeClock())
    assert lim.backend_status()["class_fractions"] == {"bench": 0.5}


def test_default_limits_untouched_for_production() -> None:
    lim = Limiter(clock=FakeClock())
    r = lim.check("ns", "memory.search", traffic_class="production")
    assert r.limit == DEFAULT_LIMITS["memory"].capacity


# ---------------------------------------------------------------- ClickHouse settings


@pytest.fixture
def class_settings(settings_override):
    def _set(raw: str) -> None:
        settings_override(ch_settings_by_traffic_class=raw)

    return _set


def _composed(sql: str, extra: dict[str, Any] | None = None) -> dict[str, Any]:
    """`statement_settings` minus the per-statement deadline,
    which is orthogonal to the class and asserted by its own tests."""
    out = ch_mod.statement_settings(sql, extra)
    out.pop("max_execution_time", None)
    return out


def test_statement_settings_per_class_reads_only(class_settings) -> None:
    class_settings('{"production": {"priority": 1}, "bench": {"priority": 10, "max_threads": 4}}')
    sql = "SELECT 1 FROM loom.memory_embeddings"
    assert _composed(sql) == {}  # unclassified: nothing
    tc.set_current_traffic_class("bench")
    assert _composed(sql) == {"priority": 10, "max_threads": 4}
    assert _composed("  -- lead\n  WITH x AS (SELECT 1) SELECT * FROM x") == {
        "priority": 10,
        "max_threads": 4,
    }
    # a write / DDL under the class carries none of it
    assert _composed("INSERT INTO loom.agent_events VALUES") == {}
    assert _composed("ALTER TABLE loom.x ADD COLUMN y String") == {}
    # the caller's explicit setting wins
    assert _composed(sql, {"max_threads": 8}) == {"max_threads": 8, "priority": 10}
    tc.set_current_traffic_class("production")
    assert _composed(sql) == {"priority": 1}
    tc.set_current_traffic_class("interactive")
    assert _composed(sql) == {}  # unlisted class: nothing
    # query_settings (inserts) never sees the class
    tc.set_current_traffic_class("bench")
    assert ch_mod.query_settings() == {}


def test_class_settings_malformed_is_ignored_once(class_settings) -> None:
    class_settings("{bad")
    tc.set_current_traffic_class("bench")
    assert _composed("SELECT 1") == {}
    class_settings('{"bench": ["not", "a", "map"]}')
    assert _composed("SELECT 1") == {}
    class_settings('{"bench": {"priority": 10, "nested": {"x": 1}}}')
    assert _composed("SELECT 1") == {"priority": 10}


def test_shipped_default_class_settings(class_settings) -> None:
    import json

    from server.settings import Settings

    raw = Settings.model_fields["ch_settings_by_traffic_class"].default
    obj = json.loads(raw)
    assert obj["bench"] == {"priority": 10, "max_threads": 4}
    class_settings(raw)
    assert ch_class_settings.traffic_class_settings("bench") == {"priority": 10, "max_threads": 4}
    assert obj["production"] == {"priority": 1}
    assert obj["interactive"] == {"priority": 2}
    assert json.loads(Settings.model_fields["ratelimit_class_fractions"].default) == {"bench": 0.5}
    assert json.loads(Settings.model_fields["llm_policy_by_traffic_class"].default) == {
        "bench": {"llm_cache": True}
    }


# ---------------------------------------------------------------- LLM tier policy


def test_llm_policy_parsing_and_neutral_default(settings_override) -> None:
    assert policy.llm_policy_for("") is policy.NEUTRAL
    assert policy.llm_policy_for("production") == policy.NEUTRAL
    bench = policy.llm_policy_for("bench")
    assert bench.llm_cache is True and bench.strong and bench.mid
    settings_override(llm_policy_by_traffic_class='{"bench": {"llm_cache": true, "strong": false}}')
    bench = policy.llm_policy_for("bench")
    assert bench.strong is False and bench.tier_allowed("strong") is False
    assert bench.tier_allowed("mid") and bench.tier_allowed("cheap")
    settings_override(llm_policy_by_traffic_class='{"bench": {"strong": "yes"}}')
    assert policy.llm_policy_for("bench") == policy.NEUTRAL
    settings_override(llm_policy_by_traffic_class="[]")
    assert policy.llm_policy_for("bench") == policy.NEUTRAL


def test_ask_llm_cache_default_follows_the_class_policy(settings_override) -> None:
    from server.memory.app.ask.handler import _llm_cache_on
    from server.settings import settings

    inp = SimpleNamespace(namespace="kcurve-0", llm_cache=None)
    settings_override(memory_ask_llm_cache_default=False, memory_ask_llm_cache_namespaces="")
    assert _llm_cache_on(inp, settings) is False  # deployment default off …
    tc.set_current_traffic_class("bench")
    assert _llm_cache_on(inp, settings) is True  # … but bench turns it on
    tc.set_current_traffic_class("production")
    assert _llm_cache_on(inp, settings) is False
    settings_override(
        llm_policy_by_traffic_class='{"production": {"llm_cache": false}}',
        memory_ask_llm_cache_default=True,
    )
    assert _llm_cache_on(inp, settings) is False  # class says off although the deployment says on
    # the per-request pin wins over everything
    assert _llm_cache_on(SimpleNamespace(namespace="kcurve-0", llm_cache=True), settings) is True
    tc.set_current_traffic_class("bench")
    assert _llm_cache_on(SimpleNamespace(namespace="kcurve-0", llm_cache=False), settings) is False


# ---------------------------------------------------------------- ledger column


def test_agent_event_row_carries_the_class_before_the_stamps() -> None:
    from server.observability.events import AgentEvent

    row = AgentEvent(
        ts=datetime.now(UTC),
        org="o",
        namespace="n",
        agent="a",
        user_id="u",
        session_id="s",
        operation="memory.get",
        request_id="r",
        latency_ms=1,
        success=True,
        traffic_class="bench",
    ).to_row()
    assert row["traffic_class"] == "bench"
    assert list(row)[-5:] == [
        "consumer",
        "traffic_class",
        "build_commit",
        "instance",
        "instance_label",
    ]


def test_emit_agent_event_reads_the_class_from_the_request_context(monkeypatch) -> None:
    from server.observability import events as ev
    from server.observability import ledger

    captured: list[Any] = []
    monkeypatch.setattr(ev, "get_emitter", lambda: SimpleNamespace(emit=captured.append))
    monkeypatch.setattr(ledger, "get_emitter", lambda: SimpleNamespace(emit=captured.append))
    ident = {"org": "o", "namespace": "n", "agent": "a", "user_id": "-", "session_id": "-"}
    common = dict(
        op_name="memory.get",
        identity=ident,
        request_id="r",
        latency_ms=3,
        success=True,
        error_code="",
    )
    tc.set_current_traffic_class("bench", "namespace")
    ledger.emit_agent_event(raw_args={"traffic_class": "production", "memory_key": "k"}, **common)
    tc.set_current_traffic_class("")
    ledger.emit_agent_event(raw_args={"memory_key": "k"}, **common)
    ledger.emit_agent_event(raw_args=None, traffic_class="interactive", **common)
    assert [e.traffic_class for e in captured] == ["bench", "", "interactive"]
    # the request's own hint still rides `args` (a hint is not the class)
    assert captured[0].args.get("traffic_class") == "production"


def test_the_dump_carries_the_column() -> None:
    from tests.unit.schema_ddl import column_names, ddl

    assert "`traffic_class` LowCardinality(String) DEFAULT ''" in ddl("agent_events")
    assert {"traffic_class", "consumer"} <= set(column_names("agent_events"))


# ---------------------------------------------------------------- cost.summary


class _CostCH:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def query_all(
        self, sql: str, params: dict[str, Any] | None = None
    ) -> list[dict[str, Any]]:
        self.calls.append((sql, params or {}))
        return [
            {
                "traffic_class": "bench",
                "cost_usd": 1.5,
                "tokens": 10,
                "embed_tokens": 0,
                "ops": 3,
                "errors": 1,
                "lat_q": (1.0, 2.0, 3.0),
            },
        ]


@pytest.mark.asyncio
async def test_cost_summary_groups_and_filters_by_class(monkeypatch) -> None:
    from datetime import date

    from server.insights.api.models_cost import CostSummaryInput
    from server.insights.app import cost as cost_h

    ch = _CostCH()
    monkeypatch.setattr(cost_h, "get_client", lambda: ch)
    out = await cost_h.handle_cost_summary(
        CostSummaryInput(
            org="o",
            namespace="n",
            from_date=date(2026, 9, 1),
            to_date=date(2026, 9, 14),
            group_by=["traffic_class"],
            traffic_class="bench",
        )
    )
    sql, params = ch.calls[0]
    assert "FROM loom.agent_events" in sql  # the events scan, like `model`
    assert "traffic_class AS traffic_class" in sql and "GROUP BY traffic_class" in sql
    assert "AND traffic_class = {tclass:String}" in sql and params["tclass"] == "bench"
    assert out.rows[0].traffic_class == "bench" and out.rows[0].ops == 3


# ---------------------------------------------------------------- token claim, CLI, registry row


def test_token_claim_round_trips_and_an_unknown_value_is_ignored() -> None:
    import jwt as pyjwt

    from server.auth.jwt import TokenError, mint_token, verify_token

    tok = mint_token("sa:bench", "o", ["*"], ["writer"], traffic_class="bench", secret="s3" * 16)
    assert verify_token(tok, secret="s3" * 16).traffic_class == "bench"
    plain = mint_token("sa:x", "o", ["*"], ["writer"], secret="s3" * 16)
    assert verify_token(plain, secret="s3" * 16).traffic_class == ""
    with pytest.raises(TokenError):
        mint_token("sa:x", "o", ["*"], ["writer"], traffic_class="vip", secret="s3" * 16)
    decoded = pyjwt.decode(tok, "s3" * 16, algorithms=["HS256"], options={"verify_aud": False})
    decoded["traffic_class"] = "vip"
    forged = pyjwt.encode(decoded, "s3" * 16, algorithm="HS256", headers={"kid": "override"})
    assert verify_token(forged, secret="s3" * 16).traffic_class == ""


def test_cli_mint_token_takes_traffic_class(monkeypatch, capsys, settings_override) -> None:
    import asyncio
    import json

    from server import cli
    from server.auth.jwt import verify_token
    from server.cli import access
    from server.settings import settings

    class _NoKeys:
        async def query_all(self, sql: str, params: Any = None) -> list[dict[str, Any]]:
            return []

    monkeypatch.setattr(
        access, "run_connected", lambda run, **_declared: asyncio.run(run(_NoKeys()))
    )
    secret = settings.jwt_secret  # an empty keyring signs with the root secret
    rc = cli.main(
        [
            "mint-token",
            "--subject",
            "sa:h",
            "--org",
            "o",
            "--namespaces",
            "n",
            "--traffic-class",
            "bench",
            "--json",
        ]
    )
    assert rc == 0
    summary = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert summary["traffic_class"] == "bench"
    assert verify_token(summary["token"], secret=secret).traffic_class == "bench"


def test_registry_row_carries_the_class_as_a_tag_and_bench_sets_the_kind() -> None:
    import server.operations  # noqa: F401
    from server.identity.api.models import NamespaceRegisterInput
    from server.identity.app.namespace import registry_row

    now = datetime(2026, 9, 15)
    row = registry_row(
        NamespaceRegisterInput(
            org="o",
            namespace="n",
            owner_email="me",
            tags=["consumer:x", "traffic_class:production"],
            traffic_class="bench",
        ),
        None,
        now,
    )
    assert row["kind"] == "bench" and row["tags"] == ["consumer:x", "traffic_class:bench"]
    assert tc.class_from_registry_row(row) == "bench"
    row2 = registry_row(
        NamespaceRegisterInput(
            org="o", namespace="n", owner_email="me", kind="memory", traffic_class="interactive"
        ),
        None,
        now,
    )
    assert row2["kind"] == "memory" and row2["tags"] == ["traffic_class:interactive"]
    assert tc.class_from_registry_row(row2) == "interactive"
    plain = registry_row(
        NamespaceRegisterInput(org="o", namespace="n", owner_email="me"), None, now
    )
    assert plain["kind"] == "memory" and plain["tags"] == []
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        NamespaceRegisterInput(org="o", namespace="n", owner_email="me", traffic_class="vip")  # type: ignore[arg-type]


def test_capabilities_list_the_register_field() -> None:
    import server.operations  # noqa: F401
    from server.operations.capabilities import capability_snapshot

    snap = capability_snapshot()
    assert "traffic_class" in snap["ops"]["namespace.register"]["request_fields"]
    assert "traffic_class" in snap["ops"]["cost.summary"]["request_fields"]
