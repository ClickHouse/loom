"""A tracker op addressed to a code namespace answers the QA
namespace bound to it (through the binding gate); tracker.item carries the files the
fixing PRs touched."""

from __future__ import annotations

from typing import Any

import pytest

import server.tracker.app.binding as binding
import server.tracker.domain.namespaces as qns
from server.tracker.api.models import TrackerItemInput
from server.tracker.app import common as tcommon
from server.tracker.app import handlers as h
from server.tracker.app import source as tsrc

ORG = "clickhouse"


@pytest.fixture
def bound(monkeypatch):
    """code-* binds to tracker-clickhouse for a caller who reads the code namespace;
    tracker-clickhouse is a tracker namespace; code-private is bound but refused."""
    monkeypatch.setattr(
        qns,
        "tracker_namespace",
        lambda org, ns: object() if ns.startswith(("tracker-", "qa-")) else None,
    )

    async def _authorized(org, code_ns, *, hides_answer=True):
        return (
            "tracker-clickhouse"
            if code_ns.startswith("code-clickhouse") and "private" not in code_ns
            else ""
        )

    monkeypatch.setattr(binding, "authorized_tracker_namespace", _authorized)


@pytest.mark.asyncio
async def test_a_code_namespace_is_rebound_to_its_tracker_namespace(bound) -> None:
    out = await tcommon._bound_input(
        TrackerItemInput(org=ORG, namespace="code-clickhouse-26-8", numbers=[1])
    )
    assert out.namespace == "tracker-clickhouse" and out.numbers == [1]


@pytest.mark.asyncio
async def test_a_tracker_namespace_answers_itself_and_a_refused_binding_stays_as_addressed(
    bound,
) -> None:
    out = await tcommon._bound_input(
        TrackerItemInput(org=ORG, namespace="tracker-clickhouse", numbers=[1])
    )
    assert out.namespace == "tracker-clickhouse"
    out = await tcommon._bound_input(
        TrackerItemInput(org=ORG, namespace="code-example-private", numbers=[1])
    )
    assert out.namespace == "code-example-private", (
        "refused: nothing is named, the op answers tracker_unavailable"
    )


class _Src:
    def __init__(self) -> None:
        self.seen: list[str] = []

    async def items_by_number(self, org, ns, nums):
        self.seen.append(ns)
        return {
            n: {
                "repo": "ClickHouse/ClickHouse",
                "source_id": f"ClickHouse/ClickHouse#{n}",
                "number": n,
                "title": f"issue {n}",
                "state": "open",
                "kind": "issue",
            }
            for n in nums
        }

    async def edges_from(self, org, ns, refs, **_):
        return [
            {
                "src_ref": "ClickHouse/ClickHouse#7",
                "rel_type": "fixes",
                "direction": "in",
                "dst_ref": "ClickHouse/ClickHouse#900",
            }
        ]

    async def pr_files(self, org, ns, nums, **_):
        self.seen.append(f"files:{ns}:{nums}")
        return {900: ["src/Storages/StorageLog.cpp", "src/Storages/StorageLog.h"]}


@pytest.mark.asyncio
async def test_tracker_item_via_a_code_namespace_reads_the_bound_tracker_namespace_and_carries_fixed_by_files(
    bound, monkeypatch
) -> None:
    src = _Src()
    monkeypatch.setattr(tsrc, "tracker_source", lambda org, ns: src)
    out = await h.handle_tracker_item(
        TrackerItemInput(org=ORG, namespace="code-clickhouse", numbers=[7])
    )
    assert src.seen[0] == "tracker-clickhouse", (
        "the source was asked on the bound tracker namespace"
    )
    item = out.items[0]
    assert item["title"] == "issue 7"
    assert item["tracker"].get("fixed_by") == [900], item["tracker"]
    assert item["fixed_by_files"] == {
        "900": ["src/Storages/StorageLog.cpp", "src/Storages/StorageLog.h"]
    }
    assert src.seen[-1] == "files:tracker-clickhouse:[900]"


@pytest.mark.asyncio
async def test_pr_files_outage_leaves_the_field_empty(bound, monkeypatch) -> None:
    src = _Src()

    async def _boom(org, ns, nums, **_):
        raise RuntimeError("down")

    src.pr_files = _boom  # type: ignore[method-assign]
    monkeypatch.setattr(tsrc, "tracker_source", lambda org, ns: src)
    out = await h.handle_tracker_item(
        TrackerItemInput(org=ORG, namespace="tracker-clickhouse", numbers=[7])
    )
    assert out.items[0]["fixed_by_files"] == {} and not out.tracker_unavailable


@pytest.mark.asyncio
async def test_pr_files_statement_shape(monkeypatch) -> None:
    from server.tracker.store.source import TrackerSource

    src = TrackerSource.__new__(TrackerSource)
    src.db = "qa"
    seen: dict[str, Any] = {}

    async def _rows(
        tag, sql, params, *, cache_key, budget_ms=None, ttl_s=None, query_settings=None
    ):
        seen["sql"], seen["params"] = sql, params
        return [{"pr_number": 900, "paths": ["a.cpp"]}]

    monkeypatch.setattr(src, "_rows", _rows)
    monkeypatch.setattr(src, "repos", lambda: ["ClickHouse/ClickHouse"])
    monkeypatch.setattr(src, "_privacy", lambda: "")
    out = await src.pr_files(ORG, "tracker-clickhouse", [900, 900, 0])
    assert out == {900: ["a.cpp"]} and seen["params"]["nums"] == [900]
    assert (
        "arraySlice(affected_file_paths" in seen["sql"]
        and "pr_number IN {nums:Array(Int64)}" in seen["sql"]
    )
