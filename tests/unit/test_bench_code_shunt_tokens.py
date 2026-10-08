"""The shunt token bench prices a failed index call as a failed PR, never as
zero tokens returned."""

from __future__ import annotations

import asyncio
import functools
import json

import httpx
import pytest

from bench import code_shunt_tokens as S


def _handler(request: httpx.Request) -> httpx.Response:
    op = request.url.path.rsplit("/", 1)[-1]
    body = json.loads(request.content or b"{}")
    if op == "code.index_status":
        return httpx.Response(200, json={"path_prefixes": ["src/"]})
    if op == "code.context_packs":
        rows = [
            {"pr_number": 1, "merge_commit_sha": "a"},
            {"pr_number": 2, "merge_commit_sha": "b"},
        ]
        return httpx.Response(200, json={"rows": rows})
    if op == "code.context_pack":
        return httpx.Response(200, json={"tokens_used": 10})
    if op == "code.search" and body["path_prefix"] == "src/b.cpp":
        return httpx.Response(429, json={"detail": "slow down"})
    return httpx.Response(200, json={"tokens_returned": 5, "results": []})


def test_a_rate_limited_search_skips_the_pr_instead_of_pricing_it_at_zero(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(S, "changed_files", lambda _g, sha, _p: [f"src/{sha}.cpp"])
    monkeypatch.setattr(S, "file_blob", lambda _g, _s, _f: b"int x;\n" * 50)
    monkeypatch.setattr(
        S.httpx,
        "AsyncClient",
        functools.partial(httpx.AsyncClient, transport=httpx.MockTransport(_handler)),
    )
    rc = asyncio.run(
        S.main(["--base-url", "http://bench.invalid", "--git-dir", "/g.git", "--json"])
    )
    out = capsys.readouterr().out
    assert rc == 0
    assert "PRs measured: 1 (skipped 1: {'measure': 1})" in out
    rows = json.loads(out.strip().splitlines()[-1])
    assert rows[1]["errors"] and rows[1]["search_tokens"] == 0


def test_the_bench_needs_an_explicit_target() -> None:
    assert asyncio.run(S.main(["--base-url", "", "--git-dir", "/g.git"])) == 2
