"""An embed-excluded path is indexed without a description or a vector, under a marker
the previous build's recovery and coverage reads skip too."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from typing import Any

import pytest

from server.code.app.index import denylist as dl
from server.code.app.index import ingest
from server.code.domain.extract import lang_reference as lr
from server.code.domain.index import repos
from server.settings import settings

pytestmark = pytest.mark.usefixtures("scalar_code_repo")

SOURCE = b"def helper(x):\n    return x + 1\n\n\ndef test_helper():\n    assert helper(1) == 2\n"
# The literal the build before `embed_exclude` binds to its recovery and coverage reads,
# `NOT startsWith(embed_last_error, {unembedded:String})`, written out on purpose.
PREVIOUS_BUILD_UNEMBEDDED = "unembedded by design: reference"


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

    async def embed(self, text: str) -> list[float]:
        self.texts += 1
        return [0.1, 0.2]


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
    repo, emb, _llm, stats = _ingest("pkg/x.py")  # outside the globs: embedded as before
    chunks = repo.rows("code_chunks")
    assert emb.texts == len(chunks) == stats.embedded
    assert all(r["embedding_dim"] == 2 and r["embed_last_error"] == "" for r in chunks)


def test_the_scalar_setting_and_a_repo_entry_carry_the_globs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert RULES.embed_excluded("pkg/test_x.py") and RULES.embed_excluded("a/fixtures/d.py")
    assert not RULES.embed_excluded("pkg/x.py")
    assert RULES.reason("pkg/test_x.py") == "", "an embed-excluded path is still indexed"
    monkeypatch.setattr(settings, "code_embed_exclude", "*.test.ts, tests/")
    assert dl.default_denylist().embed_patterns == ("*.test.ts", "tests/")
    repo, emb, _llm, _stats = _ingest("tests/test_x.py", rules=None)
    assert emb.texts == 0 and repo.rows("code_chunks")
    entry = {"namespace": "code-x", "org": "o", "repo": "o/x", "embed_exclude": ["*.spec.ts"]}
    monkeypatch.setattr(settings, "code_repos_json", json.dumps([entry]))
    (cfg,) = repos._configured_from_env(settings, {})
    assert cfg.embed_exclude == ("*.spec.ts",)
    assert dl.denylist_for(cfg).embed_excluded("ui/app.spec.ts")


def test_the_reference_marker_is_the_previous_builds_literal() -> None:
    assert lr.UNEMBEDDED_BY_DESIGN == PREVIOUS_BUILD_UNEMBEDDED


def test_the_recovery_of_either_build_skips_a_stored_embed_excluded_row() -> None:
    """Mixed versions: the row this build stores is not a stub to the previous build's
    recovery and coverage predicate, nor to this build's."""
    from server.code.store.index import CodeRepo

    class _CH:
        params: dict[str, Any] = {}

        async def query_all(self, _sql: str, params: dict[str, Any]) -> list[Any]:
            self.params = params
            return []

    ch = _CH()
    asyncio.run(CodeRepo(ch).pending_embed_chunks("o", "n", "m", 3, 10))  # type: ignore[arg-type]
    stored = {r["embed_last_error"] for r in _ingest("pkg/test_x.py")[0].rows("code_chunks")}
    for unembedded in (PREVIOUS_BUILD_UNEMBEDDED, ch.params["unembedded"]):
        assert all(err.startswith(unembedded) for err in stored), unembedded
    assert all(lr.unembedded_by_design(err) for err in stored)


def test_a_reference_fixture_keeps_its_marker_and_exclude_wins() -> None:
    rules = dl.Denylist(patterns=("gone/",), embed_patterns=("tests/", "gone/"))
    fixture = "tests/queries/0_stateless/05138_x.reference"
    assert rules.embed_excluded(fixture)
    repo, emb, _llm, _stats = _ingest(fixture, rules)
    chunks = repo.rows("code_chunks")
    assert chunks and emb.texts == 0
    assert {r["embed_last_error"] for r in chunks} == {lr.UNEMBEDDED_BY_DESIGN}
    repo, emb, _llm, stats = _ingest("gone/test_x.py", rules)
    assert stats.excluded and not repo.rows("code_chunks") and emb.texts == 0


def test_the_round_prefetch_is_not_given_an_embed_excluded_file(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.code.app.index import pass_commit, round_reads
    from server.code.app.index.pass_budget import CodeIngestResult

    prefetched: list[set[str]] = []

    async def prefetch_models(
        _r: Any, _o: str, _n: str, sources: dict[str, bytes], **_: Any
    ) -> None:
        prefetched.append(set(sources))

    async def nothing(*_: Any, **__: Any) -> None: ...

    async def show_file(*_: Any, **__: Any) -> bytes:
        return SOURCE

    reads = SimpleNamespace(hits=0, misses=0, prefetch=nothing, flush=nothing)
    monkeypatch.setattr(settings, "code_ingest_round_files", 10)
    monkeypatch.setattr(round_reads, "RoundReads", lambda *_: reads)
    monkeypatch.setattr(pass_commit, "_prefetch_round_models", prefetch_models)
    monkeypatch.setattr(pass_commit, "_ingest_round_files", nothing)
    entries = [SimpleNamespace(path=p, status="M") for p in ("pkg/x.py", "pkg/test_x.py")]
    asyncio.run(
        pass_commit._ingest_commit_files(
            _Repo(),
            "o",
            "n",
            "/git",
            "c" * 40,
            entries,
            embedder=_Embedder(),
            llm=_LLM(),
            describe=True,
            embed_reuse_from="",
            denylist=RULES,
            result=CodeIngestResult(),
            gitrepo=SimpleNamespace(show_file=show_file),
            ingest_file=nothing,
            tombstone_file=nothing,
        )
    )
    assert prefetched == [{"pkg/x.py"}]


def test_loom_code_describe_leaves_a_marked_chunk_alone(monkeypatch: pytest.MonkeyPatch) -> None:
    from server.cli import code_maintenance as cm

    described: list[str] = []
    written: list[dict[str, Any]] = []
    chunks = {
        path: {
            "path": path,
            "scope": "helper",
            "kind": "function",
            "start_line": 1,
            "end_line": 2,
            "content": "def helper(x): ...",
            "context_text": "",
            "context_source": "structural",
            "embed_last_error": err,
        }
        for path, err in (("pkg/x.py", ""), ("pkg/test_x.py", lr.UNEMBEDDED_EMBED_EXCLUDE))
    }

    class _CodeRepo:
        async def top_summaries(self, *_: Any, **__: Any) -> list[dict[str, Any]]:
            return [{"scope_path": p} for p in chunks]

        async def live_chunks_for_path(self, _o: str, _n: str, path: str, _m: str) -> list[Any]:
            return [chunks[path]]

        async def insert_rows(self, _table: str, rows: list[dict[str, Any]]) -> None:
            written.extend(rows)

        @staticmethod
        def moved_of(_table: str, live: dict[str, Any], **updates: Any) -> dict[str, Any]:
            return {**live, **updates}

    async def describe_chunk(_llm: Any, c: Any, card: str = "") -> str:
        described.append(c.path)
        return "adds one"

    async def nothing(*_: Any, **__: Any) -> Any:
        return ""

    monkeypatch.setattr(cm, "connect_code_plane", nothing)
    monkeypatch.setattr(cm, "_compact_after_bulk", nothing)
    monkeypatch.setattr(cm, "_identity", lambda _args: ("o", "n"))
    monkeypatch.setattr(cm, "_note", lambda **_: None)
    monkeypatch.setattr("server.core.ch.get_client", lambda: None)
    monkeypatch.setattr("server.code.store.index.CodeRepo", _CodeRepo)
    monkeypatch.setattr("server.embedding.embedder.get_embedder", _Embedder)
    monkeypatch.setattr("server.llm.client.get_llm_client", _LLM)
    monkeypatch.setattr("server.code.app.index.describe._describe_chunk", describe_chunk)
    monkeypatch.setattr("server.code.app.index.describe._card_for", nothing)
    args = SimpleNamespace(top_pct=100.0, suffix="", concurrency=2, limit=0)
    assert asyncio.run(cm._describe(args)) == 0  # type: ignore[arg-type]
    assert described == ["pkg/x.py"]
    assert [r["path"] for r in written] == ["pkg/x.py"]
