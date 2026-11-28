"""The embed phase copies vectors from a namespace's `embed_reuse_from` before it
embeds. A branch namespace indexed with `--phase index` and then embedded with
`--phase embed` reuses every chunk whose path, content hash and stored context
text match a live, embedded chunk in the source, and sends only the rest to the
embedder, as an inline (`--embed`) ingest already does."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest

from server.code.domain.index import repos
from server.code.workers import embed_recovery as w
from server.llm import customer

MODEL = "test-model"
SOURCE = [
    {"path": "a.py", "content_hash": "h1", "context_text": "a.py def f", "embedding": [1.0, 0.0]},
    {"path": "a.py", "content_hash": "h2", "context_text": "a.py def g", "embedding": [0.0, 1.0]},
]


class _Repo:
    def __init__(self) -> None:
        self.lookups: list[tuple[str, str, str, list[str]]] = []
        self.written: list[dict[str, Any]] = []

    async def embeddings_for_texts(
        self, org: str, ns: str, *, embedding_model: str, path: str, content_hashes: list[str]
    ) -> list[dict[str, Any]]:
        self.lookups.append((ns, embedding_model, path, sorted(content_hashes)))
        if ns != "code-main" or embedding_model != MODEL:
            return []
        return [r for r in SOURCE if r["path"] == path and r["content_hash"] in content_hashes]

    def tombstone_of(self, table: str, live: dict[str, Any]) -> dict[str, Any]:
        return dict(live)

    async def insert_rows(self, table: str, rows: list[Any]) -> None:
        self.written = rows


class _Embedder:
    def __init__(self) -> None:
        self.sent: list[str] = []

    async def embed_many(self, texts: list[str]) -> list[list[float]]:
        self.sent.extend(texts)
        return [[0.5, 0.5] for _ in texts]


def _stub(ns: str, content_hash: str, context_text: str) -> dict[str, Any]:
    return {
        "org": "acme",
        "namespace": ns,
        "path": "a.py",
        "content_hash": content_hash,
        "context_text": context_text,
        "embedding_model": MODEL,
        "embed_attempts": 0,
    }


@pytest.fixture
def branch_reuses_main(monkeypatch: pytest.MonkeyPatch) -> None:
    def _config(org: str, ns: str) -> Any:
        return SimpleNamespace(embed_reuse_from="code-main") if ns == "code-rel" else None

    monkeypatch.setattr(repos, "repo_for_namespace", _config)


def _run(
    repo: _Repo, rows: list[dict[str, Any]], emb: _Embedder
) -> tuple[int, w.CodeEmbedRecoveryResult]:
    result = w.CodeEmbedRecoveryResult()
    embedded = asyncio.run(
        w._embed_rows(
            repo,
            "code_chunks",
            rows,
            emb,
            text_of=lambda r: str(r["context_text"]),
            max_attempts=5,
            result=result,
        )
    )
    return embedded, result


def test_identical_chunks_copy_the_source_vector_and_only_the_rest_embed(
    branch_reuses_main,
) -> None:
    repo, emb = _Repo(), _Embedder()
    rows = [
        _stub("code-rel", "h1", "a.py def f"),
        _stub("code-rel", "h2", "a.py def g"),
        # Same content, different context text: the source vector does not apply.
        _stub("code-rel", "h1", "a.py def f (edited summary)"),
    ]
    embedded, result = _run(repo, rows, emb)
    assert embedded == 3 and result.reused == 2
    assert emb.sent == ["a.py def f (edited summary)"]
    assert repo.lookups == [("code-main", MODEL, "a.py", ["h1", "h1", "h2"])]
    assert [r["embedding"] for r in repo.written] == [[1.0, 0.0], [0.0, 1.0], [0.5, 0.5]]
    assert all(r["embedding_dim"] == 2 for r in repo.written)


def test_a_namespace_without_embed_reuse_from_embeds_everything(branch_reuses_main) -> None:
    repo, emb = _Repo(), _Embedder()
    embedded, result = _run(repo, [_stub("code-main", "h1", "a.py def f")], emb)
    assert embedded == 1 and result.reused == 0
    assert emb.sent == ["a.py def f"] and repo.lookups == []


def test_a_switched_off_org_still_lands_the_copied_vectors(
    branch_reuses_main, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, emb = _Repo(), _Embedder()

    def _off() -> bool:
        raise customer.GROUP_HOLDS[0]("model calls are off for this org")

    monkeypatch.setattr(customer, "names_one", _off)
    rows = [_stub("code-rel", "h1", "a.py def f"), _stub("code-rel", "h3", "a.py def h")]
    embedded, result = _run(repo, rows, emb)
    assert embedded == 1 and result.reused == 1 and result.switched_off == 1
    assert emb.sent == []
    # The stub that needed the embedder is left as it was: no attempt spent.
    assert [r["content_hash"] for r in repo.written] == ["h1"]
