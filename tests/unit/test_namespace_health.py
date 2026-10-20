"""A namespace's critical usage findings reach the agent at
session start (`context.pack` section `namespace_health`) and end (the
plugin's Stop hook) without being asked for.

The service runs only the four cheap, decisive audit checks over the two
summary reads they need, caches per (org, namespace) by TTL, returns None on
a failure and never raises; the pack renders a capped block for an unhealthy
namespace, nothing for a healthy one, and survives the service failing.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from server.identity.app import namespace_audit as na
from server.memory.app import namespace_health as nh
from server.settings import settings


class _CH:
    """Answers `query_all` / `query_one` by the first marker found in the SQL."""

    def __init__(self, by_marker: dict[str, Any] | None = None):
        self.by_marker = by_marker or {}
        self.sqls: list[str] = []
        self.params: list[dict[str, Any]] = []

    def _answer(self, sql: str) -> Any:
        for marker, rows in self.by_marker.items():
            if marker in sql:
                if isinstance(rows, Exception):
                    raise rows
                return rows
        return None

    async def query_all(self, sql: str, params: dict[str, Any] | None = None):
        self.sqls.append(sql)
        self.params.append(params or {})
        rows = self._answer(sql)
        return list(rows) if rows else []

    async def query_one(self, sql: str, params: dict[str, Any] | None = None):
        self.sqls.append(sql)
        self.params.append(params or {})
        rows = self._answer(sql)
        if isinstance(rows, list):
            return rows[0] if rows else None
        return rows


M_LIVE = "countIf(memory_type = 'episodic') AS episodic_rows"
M_EMB = "AS excerpt_bytes"
M_LEDGER = "AS sets"
M_HOT = "HAVING reads >="
M_TRACE = "FROM loom.search_trace_log"
M_INDEX = "substring(value, 1, {index_clip:UInt32})"
M_DOUBLE = "AND JSONType(value) = 'String' AND JSONType(JSONExtractString(value))"
M_UNEMB = "AS error_class"
M_HASHKEY = "match(memory_key, {hash_key_re:String})"
M_COORD = "hasAny(splitByChar('/', lower(m.memory_key))"
M_VER = "FROM loom.memory_versions FINAL"
M_PREFIX = "length(value) > {clip:UInt32}"
M_OVER = "length(value) > {advisory:UInt32}"
M_EPI = "length(value) > {episodic_chars:UInt32}"

# An unhealthy namespace: an object store.
# The coordination read embeds the hot-key subquery, so its marker goes first.
_SICK: dict[str, Any] = {
    M_COORD: [
        {"total": 1, "total_bytes": 59, "memory_key": "sre/lease", "value_chars": 59, "reads": 116}
    ],
    M_LIVE: [
        {"live_rows": 786, "live_bytes": 21_500_000, "searchable_rows": 786, "episodic_rows": 0}
    ],
    M_LEDGER: [{"gets": 6606, "searches": 52, "sets": 900}],
    M_HOT: [{"memory_key": "sre/lease", "reads": 116}],
    M_TRACE: [{"query": f"sha256:{'ab' * 32}/3", "src": 1} for _ in range(52)],
    M_DOUBLE: [{"total": 760, "total_bytes": 20_000_000, "memory_key": "k", "value_chars": 9}],
    M_UNEMB: [{"total": 179, "memory_key": "k2", "error_class": "BadRequest"}],
}
_WELL: dict[str, Any] = {
    M_LIVE: [
        {"live_rows": 1000, "live_bytes": 500_000, "searchable_rows": 1000, "episodic_rows": 0}
    ],
    M_LEDGER: [{"gets": 40, "searches": 400, "sets": 90}],
}


@pytest.fixture(autouse=True)
def _fresh(monkeypatch: pytest.MonkeyPatch):
    nh.reset_cache()
    monkeypatch.setattr(settings, "namespace_health_ttl_seconds", 3600)
    yield
    nh.reset_cache()


def _wire(monkeypatch: pytest.MonkeyPatch, by_marker: dict[str, Any]) -> _CH:
    ch = _CH(by_marker)
    monkeypatch.setattr(na, "get_client", lambda: ch)
    return ch


# --- the service -------------------------------------------------------------


def test_service_runs_only_the_four_checks_and_their_two_summary_reads(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ch = _wire(monkeypatch, _SICK)
    health = asyncio.run(nh.namespace_health("o", "ns"))
    assert health is not None
    assert {f.code for f in health.findings} <= set(nh.HEALTH_CHECKS)
    assert health.criticals == 2 and health.warnings == 2
    assert {f.code for f in health.findings if f.severity == "critical"} == {
        "exact_key_store",
        "double_encoded_values",
    }
    # Two summary reads + four checks, and none of the reads the full op adds
    # — the trace-log union of `hash_shaped_queries` among them: the ledger's
    # query arg is a redaction, so that check reads 100 % everywhere.
    joined = "\n".join(ch.sqls)
    assert len(ch.sqls) == 6
    for absent in (M_EMB, M_TRACE, M_INDEX, M_HASHKEY, M_VER, M_PREFIX, M_OVER, M_EPI):
        assert absent not in joined
    # No samples ride the summary; a finding carries only the compact fields.
    assert all(
        set(vars(f)) == {"code", "severity", "count", "share", "remedy"} for f in health.findings
    )
    # sample_limit=0 still reads one row so `count() OVER ()` totals survive.
    assert all(p.get("lim") == 1 for p in ch.params if "lim" in p)


def test_full_audit_with_sample_limit_zero_keeps_totals_and_drops_samples(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.identity.api.models_namespace_catalog import NamespaceAuditInput

    _wire(monkeypatch, _SICK)
    out = asyncio.run(
        na.handle_namespace_audit(NamespaceAuditInput(org="o", namespace="ns", sample_limit=0))
    )
    dbl = next(f for f in out.findings if f.code == "double_encoded_values")
    assert dbl.count == 760 and dbl.samples == []
    assert out.criticals >= 2 and not out.healthy


def test_service_caches_by_ttl_with_an_injected_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    ch = _wire(monkeypatch, _SICK)
    t = [1000.0]
    clock = lambda: t[0]  # noqa: E731
    first = asyncio.run(nh.namespace_health("o", "ns", clock=clock))
    n = len(ch.sqls)
    t[0] += 3599.0
    again = asyncio.run(nh.namespace_health("o", "ns", clock=clock))
    assert again is first and len(ch.sqls) == n  # served from the cache
    t[0] += 2.0
    third = asyncio.run(nh.namespace_health("o", "ns", clock=clock))
    assert third is not first and len(ch.sqls) == 2 * n  # TTL passed: recomputed
    # Another namespace is its own entry.
    asyncio.run(nh.namespace_health("o", "other", clock=clock))
    assert len(ch.sqls) == 3 * n


def test_zero_ttl_disables_without_touching_the_store(monkeypatch: pytest.MonkeyPatch) -> None:
    ch = _wire(monkeypatch, _SICK)
    monkeypatch.setattr(settings, "namespace_health_ttl_seconds", 0)
    assert asyncio.run(nh.namespace_health("o", "ns")) is None
    assert ch.sqls == []


def test_lookup_failure_returns_none_never_raises_and_is_held_briefly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ch = _wire(monkeypatch, {M_LIVE: RuntimeError("ch down")})
    t = [0.0]
    clock = lambda: t[0]  # noqa: E731
    assert asyncio.run(nh.namespace_health("o", "ns", clock=clock)) is None
    n = len(ch.sqls)
    t[0] += settings.memory_namespace_health_failure_ttl_s - 1
    assert asyncio.run(nh.namespace_health("o", "ns", clock=clock)) is None
    assert len(ch.sqls) == n  # the failure is remembered, not retried on every pack
    t[0] += 2
    asyncio.run(nh.namespace_health("o", "ns", clock=clock))
    assert len(ch.sqls) > n

    # A client that cannot even be built is the same answer.
    def _boom() -> Any:
        raise RuntimeError("no client")

    nh.reset_cache()
    monkeypatch.setattr(na, "get_client", _boom)
    assert asyncio.run(nh.namespace_health("o", "ns")) is None


def test_cache_is_size_capped(monkeypatch: pytest.MonkeyPatch) -> None:
    _wire(monkeypatch, _WELL)
    monkeypatch.setattr(nh, "CACHE_MAX_ENTRIES", 3)
    for i in range(5):
        asyncio.run(nh.namespace_health("o", f"ns{i}"))
    assert len(nh._cache) == 3


def test_healthy_namespace_renders_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    _wire(monkeypatch, _WELL)
    health = asyncio.run(nh.namespace_health("o", "ns"))
    assert health is not None and health.criticals == 0 and health.warnings == 0
    assert not health.unhealthy
    assert nh.render_health("ns", health) == ""


def test_render_is_capped_and_names_the_op_and_the_skill() -> None:
    remedy = "Write readable keys and recall with `memory.search` (tags carry the facets); rest."
    findings = tuple(
        nh.HealthFinding(code=c, severity="critical", count=6606, share=0.99, remedy=remedy)
        for c in ("exact_key_store", "double_encoded_values")
    ) + tuple(
        nh.HealthFinding(code=c, severity="warning", count=179, share=0.23, remedy=remedy)
        for c in ("unembedded_rows", "coordination_as_memory")
    )
    health = nh.NamespaceHealth(criticals=2, warnings=2, findings=findings)
    text = nh.render_health("clickhouse/ops-alerts", health)
    assert len(text) <= nh.RENDER_CHARS
    assert text.startswith("Namespace clickhouse/ops-alerts: 2 critical, 2 warning — ")
    assert (
        "exact_key_store: 6606 (99%) → Write readable keys and recall with `memory.search`" in text
    )
    assert text.endswith(nh._TAIL)
    # Room for everything: nothing dropped.
    assert nh.render_health("ns", health, cap=5000).count("→") == 4
    # One warning alone is silence; two are a line.
    one = nh.NamespaceHealth(criticals=0, warnings=1, findings=findings[2:3])
    assert nh.render_health("ns", one) == ""
    two = nh.NamespaceHealth(criticals=0, warnings=2, findings=findings[2:])
    assert nh.render_health("ns", two).startswith("Namespace ns: 0 critical, 2 warning")


# --- the pack section ---------------------------------------------------------


def _pack(monkeypatch: pytest.MonkeyPatch, **kw: Any) -> Any:
    from server.memory.api.models_context import ContextPackInput
    from server.memory.app.context_pack import handle_context_pack

    # Every other section is empty: the pack is the health block or nothing.
    async def _none(*a: Any, **k: Any) -> Any:
        raise RuntimeError("section off in this test")

    for target in (
        "server.memory.app.working_memory.handle_working_memory_render",
        "server.memory.app.stack.handle_memory_stack",
        "server.memory.app.profile.handle_profile_render",
        "server.memory.store.list_by_namespace",
    ):
        monkeypatch.setattr(target, _none)
    inp = ContextPackInput(org="o", namespace="ns", agent="a", mode="startup", **kw)
    return asyncio.run(handle_context_pack(inp))


def test_pack_renders_the_section_for_a_critical_namespace(monkeypatch: pytest.MonkeyPatch) -> None:
    _wire(monkeypatch, _SICK)
    out = _pack(monkeypatch)
    body = out.sections["namespace_health"]
    assert body.startswith("Namespace ns: 2 critical, 2 warning")
    assert len(body) <= nh.RENDER_CHARS
    assert out.section_meta["namespace_health"]["criticals"] == 2
    assert "exact_key_store" in out.section_meta["namespace_health"]["codes"]
    assert out.rendered.split("\n\n", 1)[1].startswith("<namespace_health>\n")
    assert [b.section for b in out.blocks] == ["namespace_health"]
    assert out.blocks[0].label == "core.namespace_health"
    # Query-independent: part of the stable prefix like the profile.
    assert out.stable_sections == ["namespace_health"]
    # A caller may pin it or leave it out like any section.
    only = _pack(monkeypatch, only_sections=["namespace_health"])
    assert "namespace_health" in only.sections
    without = _pack(monkeypatch, only_sections=["memories"])
    assert "namespace_health" not in without.sections


def test_pack_stays_silent_for_a_healthy_namespace(monkeypatch: pytest.MonkeyPatch) -> None:
    _wire(monkeypatch, _WELL)
    out = _pack(monkeypatch)
    assert "namespace_health" not in out.sections
    assert out.section_count == 0


def test_pack_drops_the_section_when_the_service_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _boom(*a: Any, **k: Any) -> Any:
        raise RuntimeError("service down")

    monkeypatch.setattr(nh, "namespace_health", _boom)
    out = _pack(monkeypatch)
    assert "namespace_health" not in out.sections
    assert out.rendered == ""


def test_pack_respects_the_character_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    long = "x" * 600
    health = nh.NamespaceHealth(
        criticals=1,
        warnings=0,
        findings=(nh.HealthFinding("double_encoded_values", "critical", 760, 0.97, long),),
    )

    async def _health(*a: Any, **k: Any) -> Any:
        return health

    monkeypatch.setattr(nh, "namespace_health", _health)
    out = _pack(monkeypatch)
    body = out.sections["namespace_health"]
    assert len(body) <= nh.RENDER_CHARS
    assert body.endswith(nh._TAIL)  # the finding was dropped, the pointer kept
    assert out.section_meta["namespace_health"]["truncated"] is False


def test_pack_ttl_zero_means_no_section(monkeypatch: pytest.MonkeyPatch) -> None:
    ch = _wire(monkeypatch, _SICK)
    monkeypatch.setattr(settings, "namespace_health_ttl_seconds", 0)
    out = _pack(monkeypatch)
    assert "namespace_health" not in out.sections
    assert ch.sqls == []
