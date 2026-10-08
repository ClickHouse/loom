"""A bare name answers its top-level declaration before same-named members,
whatever kind the tier wrote; and the lessons leg reads a per-process corpus
with a token index instead of a memory_embeddings FINAL scan on every
code.symbol call."""

from __future__ import annotations

import time
from unittest.mock import AsyncMock

import pytest

from server.code.app import enrich as ce
from server.code.app.ttl_cache import clear_all
from server.code.store.index import (
    demote_generated_paths,
    demote_member_rows,
    demote_noise_paths,
)
from server.settings import settings

# ---------------------------------------------------------------- top-level first


def _row(qn: str, path: str, kind: str = "function", tier: str = "scip") -> dict:
    return {"qualified_name": qn, "path": path, "kind": kind, "tier": tier}


def test_top_level_declaration_outranks_a_same_named_field_method_or_enum_member() -> None:
    # a struct field, a method and a field must not beat the generic function
    # / the type of the same bare name under the kind rule
    rows = [
        _row(
            "clickhouse-operator.metrics.resourcecount::Registration::ForObject",
            "clickhouse-operator/internal/metrics/resourcecount/registry.go",
            kind="class",
        ),
        _row(
            "clickhouse-autoscaler.internal.cachewait.cachewait::ForObject",
            "clickhouse-autoscaler/internal/cachewait/cachewait.go",
        ),
    ]
    out = demote_member_rows(rows)
    assert [r["path"].split("/")[0] for r in out] == [
        "clickhouse-autoscaler",
        "clickhouse-operator",
    ]
    rows = [
        _row(
            "capacity-controller.api.v1.v1::AWSCapacityReservationList::ListMeta", "a.go", "field"
        ),
        _row("stresshouse.sdk.api.http.v2.v2::ListMeta", "b.go", "struct"),
    ]
    assert demote_member_rows(rows)[0]["path"] == "b.go"


def test_partition_is_stable_and_keeps_equal_depth_order() -> None:
    rows = [
        _row("a::X", "1.go"),
        _row("b::T::X", "2.go"),
        _row("c::X", "3.go"),
        _row("d::U::X", "4.go"),
    ]
    assert [r["path"] for r in demote_member_rows(rows)] == ["1.go", "3.go", "2.go", "4.go"]
    assert demote_member_rows([]) == []
    only_members = [_row("a::T::X", "1.go"), _row("b::U::X", "2.go")]
    assert demote_member_rows(only_members) == only_members  # nothing shallower: untouched


def test_member_partition_composes_with_the_generated_and_noise_partitions() -> None:
    # a hand-written member still beats a generated top-level row,
    # and a top-level row in tests/ still sits last (noise)
    rows = [
        _row("gen.v1::X", "api/v1/x.pb.go"),
        _row("pkg.svc::T::X", "svc/t.go"),
        _row("pkg.svc::X", "svc/tests/x_test.go"),
    ]
    out = demote_generated_paths(demote_noise_paths(demote_member_rows(rows)))
    assert [r["path"] for r in out] == ["svc/t.go", "api/v1/x.pb.go", "svc/tests/x_test.go"]


# ---------------------------------------------------------------- lessons corpus


@pytest.fixture(autouse=True)
def _clear_cache():
    clear_all()
    yield
    clear_all()


_LESSONS = [
    {
        "memory_key": "l1",
        "namespace": "clickgap-public",
        "content_excerpt": "Reserve double counts quota",
        "content_excerpt_lc": "reserve double counts quota",
        "files": [],
    },
    {
        "memory_key": "l2",
        "namespace": "clickgap-public",
        "content_excerpt": "parse_quota_key rejects empty",
        "content_excerpt_lc": "parse_quota_key rejects empty",
        "files": ["src/quota.go"],
    },
    {
        "memory_key": "l3",
        "namespace": "clickgap-public",
        "content_excerpt": "unrelated lesson",
        "content_excerpt_lc": "unrelated lesson",
        "files": [],
    },
]


def _client(rows=_LESSONS):
    client = AsyncMock()
    client.query_all.return_value = rows
    return client


@pytest.mark.asyncio
async def test_lessons_come_from_the_cached_corpus_and_the_scan_runs_once(monkeypatch) -> None:
    client = _client()
    monkeypatch.setattr("server.core.ch.get_client", lambda: client)
    out = await ce.lessons_for_symbol(
        "acme", ["clickgap-public"], symbol_name="quota::QuotaStore::Reserve", paths=[]
    )
    assert [o["memory_key"] for o in out] == ["l1"] and out[0]["excerpt"].startswith("Reserve")
    out = await ce.lessons_for_symbol(
        "acme", ["clickgap-public"], symbol_name="Nothing", paths=["src/quota.go"]
    )
    assert [o["memory_key"] for o in out] == ["l2"]  # the files facet still matches
    assert client.query_all.await_count == 1  # second call served from the corpus
    sql = client.query_all.await_args.args[0]
    assert "memory_embeddings FINAL" in sql and "hasToken" not in sql


@pytest.mark.asyncio
async def test_snake_case_names_match_on_every_token_and_the_corpus_refreshes_after_the_ttl(
    monkeypatch,
) -> None:
    client = _client()
    monkeypatch.setattr("server.core.ch.get_client", lambda: client)
    monkeypatch.setattr(settings, "code_lesson_corpus_ttl_seconds", 60, raising=False)
    out = await ce.lessons_for_symbol(
        "acme", ["clickgap-public"], symbol_name="parse_quota_key", paths=[]
    )
    assert [o["memory_key"] for o in out] == [
        "l2"
    ]  # hasToken would split on `_`: every token must hit
    assert (
        await ce.lessons_for_symbol("acme", ["clickgap-public"], symbol_name="quota", paths=[])
        and client.query_all.await_count == 1
    )
    real = time.monotonic
    monkeypatch.setattr(time, "monotonic", lambda: real() + 120)
    await ce.lessons_for_symbol("acme", ["clickgap-public"], symbol_name="quota", paths=[])
    assert client.query_all.await_count == 2  # TTL passed: one reload


@pytest.mark.asyncio
async def test_a_failed_corpus_load_falls_back_to_the_scan(monkeypatch) -> None:
    client = AsyncMock()
    client.query_all.side_effect = [RuntimeError("down"), [dict(_LESSONS[0])]]
    monkeypatch.setattr("server.core.ch.get_client", lambda: client)
    out = await ce.lessons_for_symbol("acme", ["clickgap-public"], symbol_name="Reserve", paths=[])
    assert [o["memory_key"] for o in out] == ["l1"]
    assert "hasToken" in client.query_all.await_args.args[0]  # the scan answered
    assert await ce.lessons_for_symbol("acme", [], symbol_name="Reserve", paths=[]) == []
