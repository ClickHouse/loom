"""Exact symbol search returns retained source without a second lookup."""

from __future__ import annotations

from typing import Any

import pytest

from server.code.app import search as code_search


class SourceRepo:
    def __init__(
        self,
        name: str,
        text: str,
        *,
        missing: bool = False,
        failed: bool = False,
        path: str = "module.py",
    ):
        self.name, self.text = name, text
        self.missing, self.failed = missing, failed
        self.path = path
        self.reads: list[tuple[str, int, int, str]] = []

    async def symbols_by_name(self, org: str, ns: str, name: str) -> list[dict[str, Any]]:
        assert (org, ns) == ("org", "code-repo")
        if name != self.name:
            return []
        return [
            {
                "symbol_id": "definition",
                "qualified_name": name,
                "path": self.path,
                "start_line": 10,
                "end_line": 9 + len(self.text.splitlines()),
                "signature": "()",
                "kind": "function",
                "tier": "treesitter",
            }
        ]

    async def source_chunks_for_span(
        self, org: str, ns: str, path: str, start: int, end: int, model: str
    ) -> list[dict[str, Any]]:
        assert (org, ns) == ("org", "code-repo")
        self.reads.append((path, start, end, model))
        if self.failed:
            raise RuntimeError("source reader unavailable")
        if self.missing:
            return []
        return [
            {
                "path": path,
                "start_line": start,
                "end_line": end,
                "content": self.text,
                "kind": "function",
            }
        ]


async def lookup(repo: SourceRepo, **kwargs: Any) -> Any:
    return await code_search.search(
        repo,
        "org",
        "code-repo",
        query=repo.name,
        embedding_model="selected-source-label",
        **kwargs,
    )


@pytest.mark.parametrize("name", ["make_url", "Blueprint.register"])
async def test_exact_search_returns_complete_source_at_the_selected_model(name: str) -> None:
    text = "def make_url():\n    return '" + "x" * 12000 + "'\n"
    repo = SourceRepo(name, text)
    result = await lookup(repo)
    assert result.route == "symbol_exact"
    assert len(result.units) == 1 and result.units[0].body == text
    assert not result.units[0].elided
    assert repo.reads == [("module.py", 10, 11, "selected-source-label")]
    assert result.retrieval_work["embedding_calls"] == 0


async def test_bodyless_search_reads_no_source() -> None:
    repo = SourceRepo("make_url", "def make_url():\n    return 1\n")
    result = await lookup(repo, bodies="none")
    assert result.units and not result.units[0].body
    assert result.units[0].elided and result.units[0].granularity == "card"
    assert result.units[0].next == "code.symbol name=make_url"
    assert repo.reads == []


async def test_explicit_budget_elides_the_whole_definition() -> None:
    repo = SourceRepo("make_url", "def make_url():\n    return '" + "x" * 12000 + "'\n")
    result = await lookup(repo, token_budget=200)
    assert result.units and result.units[0].body == ""
    assert result.units[0].elided and result.units[0].next
    assert result.tokens_returned <= 200


@pytest.mark.parametrize("failed", [False, True])
async def test_unavailable_source_keeps_the_definition_and_discloses_elision(failed: bool) -> None:
    repo = SourceRepo("make_url", "def make_url():\n    return 1\n", missing=True, failed=failed)
    result = await lookup(repo)
    assert result.units and result.units[0].symbol == "make_url"
    assert result.units[0].body == "" and result.units[0].elided
    assert result.units[0].granularity == "card"
    assert result.units[0].next == "Read module.py offset=10 limit=2"


async def test_missing_source_with_a_long_pointer_respects_an_explicit_budget() -> None:
    repo = SourceRepo(
        "make_url",
        "def make_url():\n    return 1\n",
        missing=True,
        path="nested/" * 150 + "module.py",
    )
    result = await lookup(repo, token_budget=400)
    assert not result.units and result.tokens_returned == 0
