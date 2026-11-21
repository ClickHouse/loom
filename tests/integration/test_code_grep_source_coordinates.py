"""Stored chunk presentation text cannot become a source hit or file line."""

from __future__ import annotations

import uuid
from typing import Any

import pytest

from server.code.api.models_grep import CodeGrepInput
from server.code.app import message_grep
from server.code.app.grep import _matching_lines
from server.code.app.symbol.uses import _uses_lines
from server.code.store.index import CodeRepo
from server.core.ch import get_client
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
