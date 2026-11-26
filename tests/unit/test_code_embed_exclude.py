"""An embed-excluded path is indexed without a description or a vector.

The entry's `embed_exclude` globs (`LOOM_CODE_EMBED_EXCLUDE` for the scalar
entry) keep a test file's chunks, lexemes and symbols in the index but never
send them to the LLM or the embedder; the rows carry the shared
`unembedded by design` marker, so the embed-recovery read does not take them
for stubs and the coverage counts leave them out of the denominator.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from server.code.app.index import denylist as dl
from server.code.app.index import ingest
from server.code.domain.extract import lang_reference as lr
from server.code.domain.index import repos
from server.settings import settings

pytestmark = pytest.mark.usefixtures("scalar_code_repo")

SOURCE = b"def helper(x):\n    return x + 1\n\n\ndef test_helper():\n    assert helper(1) == 2\n"


class _Repo:
    """Every read answers empty; inserts are kept."""

    describe_hints = None
    embed_hints = None

    def __init__(self) -> None:
        self.inserts: list[tuple[str, list[dict[str, Any]]]] = []

    async def insert_rows(self, table: str, rows: list[dict[str, Any]]) -> None:
        self.inserts.append((table, rows))

    async def name_candidates(self, *_: Any, **__: Any) -> dict[str, Any]:
        return {}

    def __getattr__(self, name: str) -> Any:
        async def _empty(*_: Any, **__: Any) -> list[Any]:
            return []

        return _empty

    def rows(self, table: str) -> list[dict[str, Any]]:
        return [r for t, rows in self.inserts for r in rows if t == table]


class _Embedder:
    model_name = "m"

    def __init__(self) -> None:
        self.texts = 0

    async def embed_many(self, texts: list[str]) -> list[list[float]]:
        self.texts += len(texts)
        return [[0.1, 0.2] for _ in texts]


class _LLM:
    def __init__(self) -> None:
        self.calls = 0

    async def complete(self, *_: Any, **__: Any) -> Any:
        self.calls += 1
        raise RuntimeError("no model")


RULES = dl.Denylist(embed_patterns=("test_*.py", "fixtures/"))


def _ingest(path: str, rules: dl.Denylist | None = RULES) -> tuple[_Repo, _Embedder, _LLM, Any]:
    repo, emb, llm = _Repo(), _Embedder(), _LLM()
    stats = asyncio.run(
        ingest.ingest_file(
            repo,  # type: ignore[arg-type]
            "o",
            "n",
            path=path,
            source=SOURCE,
            commit_sha="c" * 40,
            embedding_model="m",
            embedder=emb,
            llm=llm,
            describe=True,
            denylist=rules,
        )
    )
    return repo, emb, llm, stats


def test_embed_excluded_matches_the_exclude_glob_semantics() -> None:
    assert RULES.embed_excluded("pkg/test_x.py")
    assert RULES.embed_excluded("a/fixtures/data.py")
    assert not RULES.embed_excluded("pkg/x.py")
    assert RULES.reason("pkg/test_x.py") == "", "an embed-excluded path is still indexed"
    assert not dl.Denylist().embed_excluded("pkg/test_x.py")


def test_an_embed_excluded_file_is_chunked_but_never_described_or_embedded() -> None:
    repo, emb, llm, stats = _ingest("pkg/test_x.py")
    assert emb.texts == 0 and llm.calls == 0
    chunks = repo.rows("code_chunks")
    assert chunks and stats.chunks_new == len(chunks) and stats.embedded == 0
    for row in chunks:
        assert row["embedding_dim"] == 0 and row["embedding"] == []
        assert row["embed_last_error"] == lr.UNEMBEDDED_EMBED_EXCLUDE
        assert row["embed_attempts"] == 0 and row["context_source"] == "structural"
    assert any("helper" in r["lexemes"] for r in chunks), "the lexical leg can find it"
    assert stats.embed_stubs == 0, "not a stub the recovery loop should count"
    assert {s["name"] for s in repo.rows("code_symbols")} >= {"helper", "test_helper"}


def test_a_path_outside_the_globs_is_embedded_as_before() -> None:
    repo, emb, _llm, stats = _ingest("pkg/x.py")
    chunks = repo.rows("code_chunks")
    assert emb.texts == len(chunks) and stats.embedded == len(chunks)
    assert all(r["embedding_dim"] == 2 and r["embed_last_error"] == "" for r in chunks)


def test_the_scalar_setting_and_a_repo_entry_carry_the_globs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "code_embed_exclude", "*.test.ts, tests/")
    assert dl.default_denylist().embed_patterns == ("*.test.ts", "tests/")
    repo, emb, _llm, _stats = _ingest("tests/test_x.py", rules=None)
    assert emb.texts == 0 and repo.rows("code_chunks")
    entry = {"namespace": "code-x", "org": "o", "repo": "o/x", "embed_exclude": ["*.spec.ts"]}
    monkeypatch.setattr(settings, "code_repos_json", json.dumps([entry]))
    (cfg,) = repos._configured_from_env(settings, {})
    assert cfg.embed_exclude == ("*.spec.ts",)
    assert dl.denylist_for(cfg).embed_excluded("ui/app.spec.ts")


def test_recovery_and_coverage_match_every_unembedded_marker() -> None:
    from server.code.store.index import CodeRepo

    assert lr.UNEMBEDDED_BY_DESIGN.startswith(lr.UNEMBEDDED_PREFIX)
    assert lr.UNEMBEDDED_EMBED_EXCLUDE.startswith(lr.UNEMBEDDED_PREFIX)

    class _CH:
        def __init__(self) -> None:
            self.params: list[dict[str, Any]] = []

        async def query_all(self, _sql: str, params: dict[str, Any]) -> list[dict[str, Any]]:
            self.params.append(params)
            return []

        async def query_one(self, _sql: str, params: dict[str, Any]) -> dict[str, Any]:
            self.params.append(params)
            return {"n": 0, "embedded": 0, "embeddable": 0}

    ch = _CH()
    repo = CodeRepo(ch)  # type: ignore[arg-type]
    asyncio.run(repo.pending_embed_chunks("o", "n", "m", 3, 10))
    asyncio.run(repo.chunk_embed_coverage("o", "n"))
    assert ch.params[0]["unembedded"] == lr.UNEMBEDDED_PREFIX
    assert ch.params[-1]["unembedded"] == lr.UNEMBEDDED_PREFIX
