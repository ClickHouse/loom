"""SDK CodeClient sends exactly the wire fields the code ops define, and never
an org/namespace of its own (code ops default to the deployment's code plane)."""

from __future__ import annotations

from typing import Any

import pytest
from loom.code import CodeClient


class _Transport:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def post(
        self, op_path: str, payload: dict[str, Any], *a: Any, **k: Any
    ) -> dict[str, Any]:
        self.calls.append((op_path, payload))
        return {"ok": True}


class _Agent:
    def __init__(self) -> None:
        self.transport = _Transport()


@pytest.mark.asyncio
async def test_impact_diff_shape_only_sends_given_fields() -> None:
    ag = _Agent()
    c = CodeClient(ag)  # type: ignore[arg-type]
    await c.impact(files=["src/a.cpp"], diff="--- a\n+++ b\n")
    op, body = ag.transport.calls[0]
    assert op == "/v1/code.impact"
    assert body["files"] == ["src/a.cpp"] and body["diff"].startswith("---")
    assert "consumer" not in body and "org" not in body and "namespace" not in body
    await c.impact("Foo::bar")
    assert "files" not in ag.transport.calls[1][1] and "diff" not in ag.transport.calls[1][1]


@pytest.mark.asyncio
async def test_search_leaves_budget_and_bodies_to_the_server_unless_given() -> None:
    ag = _Agent()
    c = CodeClient(ag)  # type: ignore[arg-type]
    await c.search("q")
    await c.search("q", token_budget=900, bodies="auto")
    unset, given = (body for _, body in ag.transport.calls)
    assert "token_budget" not in unset and "bodies" not in unset
    assert given["token_budget"] == 900 and given["bodies"] == "auto"


@pytest.mark.asyncio
async def test_search_grep_trace_verify_coverage_wire_fields() -> None:
    ag = _Agent()
    c = CodeClient(ag)  # type: ignore[arg-type]
    await c.search("q", bodies="none", annotate=False, path_prefix="src/")
    await c.grep("LOGICAL_ERROR", regex=False, path_prefix="src/Core")
    await c.trace("#0 DB::foo()\n#1 DB::bar()")
    await c.verify("calls", "DB::A", obj="DB::B", depth=3)
    await c.coverage()
    ops = [o for o, _ in ag.transport.calls]
    assert ops == [
        "/v1/code.search",
        "/v1/code.grep",
        "/v1/code.trace",
        "/v1/code.verify",
        "/v1/code.coverage",
    ]
    search = ag.transport.calls[0][1]
    assert search["bodies"] == "none" and search["annotate"] is False
    verify = ag.transport.calls[3][1]
    assert verify == {
        "kind": "calls",
        "subject": "DB::A",
        "object": "DB::B",
        "depth": 3,
    }


@pytest.mark.asyncio
async def test_blame_range_sends_only_the_named_window_ends() -> None:
    ag = _Agent()
    c = CodeClient(ag)  # type: ignore[arg-type]
    await c.blame_range("src/a.cpp", 10, 20, from_version="26.2", to_version="26.3")
    op, body = ag.transport.calls[0]
    assert op == "/v1/code.blame_range"
    assert body == {
        "path": "src/a.cpp",
        "start_line": 10,
        "end_line": 20,
        "limit": 10,
        "from_version": "26.2",
        "to_version": "26.3",
    }
    await c.blame_range("src/a.cpp", 1, 1, to_sha="e" * 40)
    body = ag.transport.calls[1][1]
    assert body["to_sha"] == "e" * 40 and "from_sha" not in body and "from_version" not in body
    assert "org" not in body and "namespace" not in body


@pytest.mark.asyncio
async def test_verify_citations_sends_text_and_optional_commit_fields() -> None:
    ag = _Agent()
    c = CodeClient(ag)  # type: ignore[arg-type]
    await c.verify_citations("see src/a.cpp:12 and #5")
    path, body = ag.transport.calls[0]
    assert path == "/v1/code.verify_citations"
    assert body == {"text": "see src/a.cpp:12 and #5", "limit": 200}
    await c.verify_citations("x", at_commit="abc123", base_namespace="code-clickhouse", limit=50)
    body = ag.transport.calls[1][1]
    assert body["at_commit"] == "abc123" and body["base_namespace"] == "code-clickhouse"
    assert body["limit"] == 50


async def test_review_brief_sends_tier_and_only_the_named_optionals() -> None:
    ag = _Agent()
    c = CodeClient(ag)  # type: ignore[arg-type]
    await c.review_brief(117354, tier="full", sections=["tests", "blame"])
    op, body = ag.transport.calls[0]
    assert op == "/v1/code.review_brief"
    assert body == {
        "pr_number": 117354,
        "tier": "full",
        "sections": ["tests", "blame"],
    }
    await c.review_brief(merge_sha="a" * 40, token_budget=900)
    body = ag.transport.calls[1][1]
    assert body["pr_number"] == 0 and body["merge_sha"] == "a" * 40 and body["token_budget"] == 900
    assert body["tier"] == "standard" and "consumer" not in body


@pytest.mark.asyncio
async def test_conventions_sends_only_the_given_source_and_rules() -> None:
    ag = _Agent()
    c = CodeClient(ag)  # type: ignore[arg-type]
    await c.conventions(117354, rules=["setting_history"])
    op, body = ag.transport.calls[0]
    assert op == "/v1/code.conventions"
    assert body == {"pr_number": 117354, "rules": ["setting_history"]}
    await c.conventions(files=["src/a.cpp"], diff="diff --git a/src/a.cpp b/src/a.cpp\n")
    body = ag.transport.calls[1][1]
    assert body["files"] == ["src/a.cpp"] and body["diff"].startswith("diff --git")
    assert "pr_number" not in body and "head_sha" not in body and "org" not in body


@pytest.mark.asyncio
async def test_each_code_read_with_lessons_sends_the_opt_out_only_when_asked() -> None:
    ag = _Agent()
    c = CodeClient(ag)  # type: ignore[arg-type]
    calls = [
        (c.search, ("q",)),
        (c.outline, ("a.py",)),
        (c.callers, ("f",)),
        (c.callees, ("f",)),
        (c.impact, ("f",)),
    ]
    for fn, args in calls:
        await fn(*args)
        await fn(*args, include_lessons=False)
    bodies = [body for _, body in ag.transport.calls]
    assert all("include_lessons" not in b for b in bodies[0::2])  # the server's default
    assert all(b["include_lessons"] is False for b in bodies[1::2])
