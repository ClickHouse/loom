"""Stored chunk presentation text cannot become a source hit or file line."""

from __future__ import annotations

import uuid
from typing import Any

import pytest

from server.code.api.models_grep import CodeGrepInput
from server.code.app import message_grep
from server.code.app.grep import _matching_lines, handle_code_grep
from server.code.app.index.ingest import ingest_file
from server.code.app.symbol.uses import _uses_lines
from server.code.store.index import CodeRepo
from server.core.ch import get_client
from server.embedding.fake import FakeNGramEmbedder
from server.settings import settings
from tests.integration._resources import unique_org


@pytest.mark.asyncio
async def test_stored_chunks_retain_kind_and_physical_source_lines(ch_client: Any) -> None:
    org, ns = unique_org("grep-source-lines"), "source-lines"
    ch_client.insert(
        "loom.code_chunks",
        [
            [
                org,
                ns,
                "m",
                "helpers.py",
                uuid.uuid4(),
                "function",
                "response",
                10,
                12,
                'Documents target.\ndef response():\n    """Use target."""\n    return target()\n',
                [0.0] * settings.embedding_dim,
                1,
            ],
            [
                org,
                ns,
                "m",
                "container.py",
                uuid.uuid4(),
                "class_skeleton",
                "Container",
                1,
                100,
                "class Container:\n    target = 1\n",
                [0.0] * settings.embedding_dim,
                1,
            ],
            [
                org,
                ns,
                "m",
                "endpoint.cpp",
                uuid.uuid4(),
                "function",
                "bind",
                20,
                22,
                'Documents binding.\nvoid bind() {\n    throw Exception("Cannot bind endpoint");\n}\n',
                [0.0] * settings.embedding_dim,
                1,
            ],
        ],
        column_names=[
            "org",
            "namespace",
            "embedding_model",
            "path",
            "chunk_id",
            "kind",
            "scope",
            "start_line",
            "end_line",
            "content",
            "embedding",
            "version",
        ],
    )
    await get_client().connect()
    repo = CodeRepo()
    chunks = await repo.grep_chunks(org, ns, "target")
    assert {row["kind"] for row in chunks} == {"function", "class_skeleton"}
    matches, capped = _matching_lines(chunks, {}, CodeGrepInput(pattern="target"), None, 20)
    expected = [("helpers.py", 11), ("helpers.py", 12)]
    assert not capped
    assert [(hit["path"], hit["line"]) for hit in matches] == expected
    assert [(hit["path"], hit["line"]) for hit in _uses_lines(chunks, ["target"], {})] == expected

    message_chunks, admitted = await repo.grep_message_chunks(org, ns, ["Cannot bind"])
    assert admitted == 1
    assert [row["kind"] for row in message_chunks] == ["function"]
    ranked = message_grep.rank_statements(
        message_grep.split_message("Cannot bind endpoint"), message_chunks
    )
    assert [(statement.path, statement.line) for statement in ranked] == [("endpoint.cpp", 21)]


@pytest.mark.asyncio
async def test_related_grep_returns_the_bound_argument_and_helper(ch_client, monkeypatch) -> None:
    org, ns = unique_org("grep-dependencies"), "source"
    monkeypatch.setattr(settings, "code_intel_enabled", True)
    monkeypatch.setattr(settings, "code_search_cursor_enabled", True)
    monkeypatch.setattr(settings, "jwt_secret", "grep-test-cursor-signing-value")
    embedder = FakeNGramEmbedder(dim=settings.embedding_dim)
    monkeypatch.setattr(settings, "embedding_model", embedder.model_name)
    await get_client().connect()
    repo = CodeRepo()
    await ingest_file(
        repo,
        org,
        ns,
        path="src/client.py",
        source=b"""def helper():
    return 7

class Client:
    @property
    def option(self):
        return helper()

def use(client: Client):
    # A short excerpt for the definition does not reach the call.
    # A related match can still be in the same function.

    return consume(client.option, extra=True)
""",
        commit_sha="a" * 40,
        embedding_model=embedder.model_name,
        embedder=embedder,
    )
    args = CodeGrepInput(
        org=org,
        namespace=ns,
        pattern="consume(client.option)",
        related_on_miss=True,
        include_dependencies=True,
        include_body=False,
    )
    out = await handle_code_grep(args)
    assert not out.matches and out.related and out.related.matches
    assert [d.qualified_name for d in out.related.dependencies] == [
        "src.client::Client::option",
        "src.client::helper",
    ]
    assert [d.body.splitlines()[-1].strip() for d in out.related.dependencies] == [
        "return helper()",
        "return 7",
    ]
    locations = await handle_code_grep(args.model_copy(update={"context_lines": 0}))
    assert locations.related and locations.related.matches == out.related.matches
    assert not locations.related.dependencies
    batch = args.model_copy(update={"pattern": ["consume(client.option)", "client.option"]})
    exact_batch = await handle_code_grep(batch.model_copy(update={"include_dependencies": False}))
    enriched_batch = await handle_code_grep(batch)
    assert enriched_batch.matches == exact_batch.matches == out.related.matches
    assert enriched_batch.related and not enriched_batch.related.matches
    assert enriched_batch.related.dependencies == out.related.dependencies
    ambiguous = await handle_code_grep(
        batch.model_copy(update={"pattern": [*batch.pattern, "consume(other.option)"]})
    )
    assert ambiguous.matches == exact_batch.matches and ambiguous.related is None
    await ingest_file(
        repo,
        org,
        ns,
        path="tests/test_client.py",
        source=b"def test_option():\n    assert True\n",
        commit_sha="a" * 40,
        embedding_model=embedder.model_name,
        embedder=embedder,
    )
    mixed = args.model_copy(
        update={"pattern": ["consume(client.option)", "def test_option"], "include_body": True}
    )
    exact = await handle_code_grep(mixed.model_copy(update={"related_on_miss": False}))
    enriched = await handle_code_grep(mixed)
    assert enriched.matches == exact.matches
    assert [hit["path"] for hit in enriched.matches] == ["tests/test_client.py"]
    assert enriched.definitions == exact.definitions
    assert enriched.related and enriched.related.matches == out.related.matches
    assert "consume(client.option, extra=True)" in enriched.related.definitions[0]["body"]
    assert enriched.related.dependencies == out.related.dependencies
    scoped = await handle_code_grep(mixed.model_copy(update={"path_prefix": "tests/"}))
    assert scoped.matches == exact.matches and scoped.related is None
    same_scope = args.model_copy(
        update={"pattern": ["consume(client.option)", "def use(client:"], "context_lines": 2}
    )
    excerpts = await handle_code_grep(same_scope)
    assert not any("client.option" in row["text"] for row in excerpts.contexts)
    assert excerpts.related and excerpts.related.matches == out.related.matches
    assert excerpts.related.dependencies == out.related.dependencies
    full = await handle_code_grep(same_scope.model_copy(update={"include_body": True}))
    assert full.related and full.related.matches == out.related.matches
    assert "consume(client.option, extra=True)" in full.definitions[0]["body"]
    assert not full.related.definitions
    wide = args.model_copy(
        update={"pattern": ["consume(client.option", "def "], "context_lines": 3, "limit": 1}
    )
    first = await handle_code_grep(wide)
    assert len(first.matches) == 1 and first.next_cursor and first.truncated
    assert first.related is None
    focused = await handle_code_grep(wide.model_copy(update={"related_on_prefix": True}))
    assert focused.matches == first.matches and focused.next_cursor and focused.truncated
    assert focused.related and focused.related.matches == out.related.matches
    assert focused.related.dependencies == out.related.dependencies
    next_exact = await handle_code_grep(wide.model_copy(update={"cursor": first.next_cursor}))
    next_focused = await handle_code_grep(
        wide.model_copy(update={"cursor": focused.next_cursor, "related_on_prefix": True})
    )
    assert next_focused.matches == next_exact.matches and next_focused.related is None
