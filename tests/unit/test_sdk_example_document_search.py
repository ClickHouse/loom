"""sdk/python/examples/document_search.py runs end to end against a scripted server,
deletes both documents even when a step in the middle fails, and exits non-zero
when a check fails."""

from __future__ import annotations

import functools
import importlib.util
import json
from pathlib import Path
from types import ModuleType
from typing import Any

import httpx
import pytest
from loom import ValidationError

EXAMPLE = Path(__file__).resolve().parents[2] / "sdk/python/examples/document_search.py"


def load_example() -> ModuleType:
    spec = importlib.util.spec_from_file_location("document_search_example", EXAMPLE)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _hit(source_uri: str, content: str) -> dict[str, Any]:
    return {
        "source_uri": source_uri,
        "chunk_idx": 0,
        "content": content,
        "tags": [],
        "distance": 0.2,
        "similarity": 0.8,
        "section": "Eligibility",
    }


def _install(
    monkeypatch: pytest.MonkeyPatch,
    *,
    search_fails: bool = False,
    off_topic_hit: bool = False,
) -> tuple[list[str], list[dict[str, Any]]]:
    deleted: list[str] = []
    searches: list[dict[str, Any]] = []
    stored: dict[str, str] = {}

    def server(req: httpx.Request) -> httpx.Response:
        op = req.url.path.removeprefix("/v1/")
        body = json.loads(req.content or b"{}")
        if op == "knowledge.add":
            stored[body["source_uri"]] = body["content"]
            return httpx.Response(200, json={"source_uri": body["source_uri"], "chunks": 2})
        if op == "knowledge.search":
            searches.append(body)
            if search_fails:
                return httpx.Response(422, json={"detail": {"code": "VALIDATION_ERROR"}})
            # The document whose subject the query names answers it.
            uri = (
                "docs://example/refunds.md"
                if "refund" in body["query"]
                else "docs://example/shipping.md"
            )
            return httpx.Response(200, json={"results": [_hit(uri, stored[uri])]})
        if op == "search.query":
            searches.append(body)
            results = [_hit("docs://example/refunds.md", "x")] if off_topic_hit else []
            return httpx.Response(200, json={"knowledge": {"results": results}})
        if op == "knowledge.delete":
            deleted.append(body["source_uri"])
            return httpx.Response(200, json={"deleted": True, "chunks_tombstoned": 2})
        return httpx.Response(200, json={})

    monkeypatch.setenv("LOOM_API_KEY", "k")
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        functools.partial(httpx.AsyncClient, transport=httpx.MockTransport(server)),
    )
    return deleted, searches


async def test_every_check_passes_and_both_documents_are_deleted(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    deleted, searches = _install(monkeypatch)
    example = load_example()
    assert await example.main() == 0
    assert deleted == list(example.DOCS)
    # every search reads only the example's own namespace
    assert len(searches) == 3 and all(s["namespace_scope"] == "this" for s in searches)
    out = capsys.readouterr().out
    assert "[docs://example/refunds.md#0] Eligibility: # Refund policy" in out
    assert "no knowledge passage came back" in out and "all checks passed" in out


async def test_a_failed_step_still_deletes_both_documents(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    deleted, _ = _install(monkeypatch, search_fails=True)
    example = load_example()
    with pytest.raises(ValidationError):
        await example.main()
    assert deleted == list(example.DOCS)


async def test_an_off_topic_passage_fails_the_run(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    deleted, _ = _install(monkeypatch, off_topic_hit=True)
    example = load_example()
    assert await example.main() == 1
    assert deleted == list(example.DOCS)
    assert "1 check(s) failed" in capsys.readouterr().out
