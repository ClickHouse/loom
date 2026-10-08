"""`CodeSearchInput.embedding_model` is a request-only
override of the label the legs read — the twin label's A/B without a
redeploy. Default empty = the embedder's model name, as before."""

from __future__ import annotations

from typing import Any

import pytest

from server.code.api.models_search import CodeSearchInput
from server.code.app import search as code_search


class _Repo:
    def __init__(self) -> None:
        self.models: dict[str, str] = {}

    async def code_corpus_stats(self, org, ns, tokens):
        return {}, 10, 100.0

    async def lexical_search_chunks(self, org, ns, model, tokens, idfs, **kw):
        self.models["lexical"] = model
        return []

    async def vector_search_chunks(self, org, ns, model, qvec, **kw):
        self.models["vector"] = model
        return []

    async def symbols_by_name(self, *a, **k):
        return []

    async def symbols_fuzzy(self, *a, **k):
        return []

    async def paths_under_prefix(self, org, ns, model, prefix, **kw):
        self.models["track"] = model
        return []

    async def track_path_chunks(self, org, ns, model, *a, **kw):
        return []

    def __getattr__(self, name: str) -> Any:  # any other read: nothing found
        async def _none(*a: Any, **k: Any) -> Any:
            return []

        return _none


class _Embedder:
    model_name = "text-embedding-3-large"
    dim = 4

    async def embed(self, text: str) -> list[float]:
        return [0.5, 0.5, 0.5, 0.5]


def test_input_field_defaults_empty_and_is_bounded() -> None:
    assert CodeSearchInput.model_fields["embedding_model"].default == ""
    assert CodeSearchInput(query="q", embedding_model="text-embedding-3-large+ctx2").embedding_model


@pytest.mark.asyncio
async def test_default_reads_the_embedder_label_and_override_reads_the_twin() -> None:
    repo = _Repo()
    await code_search.search(
        repo,
        "o",
        "n",
        query="how are quotas enforced",
        embedder=_Embedder(),  # type: ignore[arg-type]
    )
    assert repo.models["lexical"] == "text-embedding-3-large"
    assert repo.models["vector"] == "text-embedding-3-large"
    repo = _Repo()
    await code_search.search(
        repo,
        "o",
        "n",
        query="how are quotas enforced",
        embedder=_Embedder(),  # type: ignore[arg-type]
        embedding_model="text-embedding-3-large+ctx2",
        track_path="src/Access/Quota.cpp",
    )
    assert repo.models == {
        "lexical": "text-embedding-3-large+ctx2",
        "vector": "text-embedding-3-large+ctx2",
        "track": "text-embedding-3-large+ctx2",
    }, "every chunk read, the track_path diagnostic included, sees the twin label"
