"""Multi-branch code indexing: one namespace per repo@branch.

Every branch of one repo shares the bare clone but indexes in its own
compiler-tier working tree, reads only PRs merged INTO its branch, and
reuses master's chunk vectors when the embedded text is byte-identical.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from server.code.domain.index import repos as r
from server.settings import settings

# The scalar code repository these tests model (none is configured by default).
pytestmark = pytest.mark.usefixtures("scalar_code_repo")


def _cfg(monkeypatch: pytest.MonkeyPatch, entries: list[dict[str, Any]]) -> list[r.RepoConfig]:
    monkeypatch.setattr(settings, "code_repos_json", json.dumps(entries))
    monkeypatch.setattr(settings, "code_namespace", "code-clickhouse")
    monkeypatch.setattr(settings, "code_scip_checkout_dir", "var/code_repos/checkout")
    monkeypatch.setattr(settings, "code_git_dir", "var/code_repos/clickhouse.git")
    return r.configured_repos()


def test_scalar_namespace_keeps_the_scalar_checkout(monkeypatch: pytest.MonkeyPatch) -> None:
    (master,) = _cfg(monkeypatch, [{"namespace": "code-clickhouse"}])
    assert master.scip_checkout_dir == "var/code_repos/checkout"
    assert master.embed_reuse_from == ""


def test_release_branch_defaults_to_sibling_checkout_and_shared_bare(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    master, rel = _cfg(
        monkeypatch,
        [
            {"namespace": "code-clickhouse"},
            {
                "namespace": "code-clickhouse-26-3",
                "git_branch": "26.3",
                "embed_reuse_from": "code-clickhouse",
            },
        ],
    )
    assert rel.git_dir == master.git_dir == "var/code_repos/clickhouse.git"
    assert rel.git_branch == "26.3"
    assert rel.scip_checkout_dir == "var/code_repos/checkout-code-clickhouse-26-3"
    assert rel.embed_reuse_from == "code-clickhouse"


def test_explicit_checkout_dir_wins(monkeypatch: pytest.MonkeyPatch) -> None:
    (_, rel) = _cfg(
        monkeypatch,
        [
            {"namespace": "code-clickhouse"},
            {
                "namespace": "code-clickhouse-26-8",
                "git_branch": "26.8",
                "scip_checkout_dir": "var/code_repos/checkout-26-8",
            },
        ],
    )
    assert rel.scip_checkout_dir == "var/code_repos/checkout-26-8"


@pytest.mark.asyncio
async def test_github_readers_pass_the_base_branch(monkeypatch: pytest.MonkeyPatch) -> None:
    from server.clients import github as gh

    calls: list[dict[str, Any]] = []

    async def _get(url: str, token: str, params: dict[str, Any] | None = None) -> Any:
        calls.append({"url": url, **(params or {})})
        return [] if url.endswith("/pulls") else {"items": []}

    monkeypatch.setattr(gh, "_get", _get)
    await gh.merged_prs_since("o/r", "t", None, base="26.3")
    await gh.merged_prs_window("o/r", "t", "2026-09-01", base="26.3")
    await gh.merged_prs_since("o/r", "t", None)  # no base -> no filter
    assert calls[0]["base"] == "26.3"
    assert " base:26.3" in calls[1]["q"]
    assert "base" not in calls[2]


class _ReuseRepo:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.rows = rows
        self.params: dict[str, Any] = {}

        class _CH:
            async def query_all(_s, sql: str, params: dict[str, Any]) -> list[dict[str, Any]]:
                self.params = params
                return self.rows

        self._ch = _CH()

    from server.code.store.index import CodeRepo

    embeddings_for_texts = CodeRepo.embeddings_for_texts


@pytest.mark.asyncio
async def test_reusable_vectors_match_exact_context_text_only() -> None:
    from server.code.app.index.embed_reuse import reusable_vectors

    repo = _ReuseRepo(
        [
            {"context_text": "src/a.cpp\nint f()\n{ return 1; }", "embedding": [0.1, 0.2]},
            {"context_text": "src/a.cpp\nother text", "embedding": [0.9, 0.9]},
        ]
    )
    got = await reusable_vectors(
        repo,
        "o",
        "code-clickhouse",
        embedding_model="m",
        path="src/a.cpp",
        content_hashes=["h1", "h2", "h1"],
        context_texts=[
            "src/a.cpp\nint f()\n{ return 1; }",  # identical -> reused
            "src/a.cpp\nint f()\n{ return 2; }",  # same hash prefix irrelevant: text differs
        ],
    )
    assert got == {0: [0.1, 0.2]}
    assert repo.params["ns"] == "code-clickhouse" and repo.params["path"] == "src/a.cpp"
    assert repo.params["hashes"] == ["h1", "h2"]  # deduplicated, sorted


@pytest.mark.asyncio
async def test_reusable_vectors_is_empty_without_source_or_on_error() -> None:
    from server.code.app.index.embed_reuse import reusable_vectors

    class _Boom:
        class _CH:
            async def query_all(self, *_: Any, **__: Any) -> Any:
                raise RuntimeError("down")

        _ch = _CH()
        embeddings_for_texts = _ReuseRepo.embeddings_for_texts

    assert (
        await reusable_vectors(
            _Boom(),
            "o",
            "",
            embedding_model="m",
            path="p",
            content_hashes=["h"],
            context_texts=["t"],
        )
        == {}
    )
    assert (
        await reusable_vectors(
            _Boom(),
            "o",
            "src",
            embedding_model="m",
            path="p",
            content_hashes=["h"],
            context_texts=["t"],
        )
        == {}
    )


@pytest.mark.asyncio
async def test_ingest_file_embeds_only_what_it_could_not_reuse(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.code.app.index import ingest

    embedded: list[str] = []

    class _Embedder:
        model_name = "m"

        async def embed_many(self, texts: list[str]) -> list[list[float]]:
            embedded.extend(texts)
            return [[1.0] for _ in texts]

    class _Repo:
        def __init__(self) -> None:
            self.inserts: list[tuple[str, list[dict[str, Any]]]] = []

        async def live_chunks_for_path(self, *_: Any) -> list[dict[str, Any]]:
            return []

        async def live_chunk_index_for_path(self, *_: Any) -> list[dict[str, Any]]:
            return []

        async def chunks_full_by_ids(self, *_: Any) -> list[dict[str, Any]]:
            return []

        async def live_symbols_for_path(self, *_: Any) -> list[dict[str, Any]]:
            return []

        async def live_edges_for_path(self, *_: Any) -> list[dict[str, Any]]:
            return []

        async def name_candidates(self, *_: Any) -> dict[str, Any]:
            return {}

        async def insert_rows(self, table: str, rows: list[dict[str, Any]]) -> None:
            self.inserts.append((table, rows))

    source = b"int f() { return 1; }\nint g() { return f(); }\n"
    # Reuse everything the source namespace has: pretend it has the first chunk.
    seen: dict[str, Any] = {}

    async def _reuse(repo: Any, org: str, src: str, **kw: Any) -> dict[int, list[float]]:
        seen["src"] = src
        return {0: [0.5]}

    monkeypatch.setattr("server.code.app.index.embed_reuse.reusable_vectors", _reuse)
    repo = _Repo()
    stats = await ingest.ingest_file(
        repo,
        "o",
        "code-clickhouse-26-3",
        path="src/a.cpp",
        source=source,
        commit_sha="c" * 40,
        embedding_model="m",
        embedder=_Embedder(),
        embed_reuse_from="code-clickhouse",
    )
    assert seen["src"] == "code-clickhouse"
    assert stats.embed_reused == 1
    chunk_rows = [r for t, rows in repo.inserts if t == "code_chunks" for r in rows]
    assert chunk_rows and all(r["embedding_dim"] > 0 for r in chunk_rows)
    # exactly one fewer provider call than chunks
    assert len(embedded) == len(chunk_rows) - 1
    assert chunk_rows[0]["embedding"] == [0.5]
