"""An embed-excluded path is indexed without a description or a vector.

The entry's `embed_exclude` globs (`LOOM_CODE_EMBED_EXCLUDE` for the scalar
entry) keep a test file's chunks, lexemes and symbols in the index but never
send them to the LLM or the embedder; the rows carry the shared
`unembedded by design` marker, so the embed-recovery read does not take them
for stubs and the coverage counts leave them out of the denominator.
"""

from __future__ import annotations

import ast
import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from server.code.app.index import denylist as dl
from server.code.app.index import ingest
from server.code.domain.extract import lang_reference as lr
from server.code.domain.index import repos
from server.settings import settings
from tests.unit.code_fixtures import repo_config

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


def test_reembed_copies_unembedded_rows_without_a_vector() -> None:
    from server.code.app.index import reembed_model as rm
    from server.code.store.index import CodeRepo

    def row(path: str, err: str) -> dict[str, Any]:
        return {
            **{c: "" for c in rm._COPY_COLS},
            "path": path,
            "chunk_id": f"{path}#0",
            "start_line": 1,
            "end_line": 2,
            "content": f"content of {path}",
            "context_text": f"text of {path}",
            "symbol_ids": [],
            "embed_last_error": err,
        }

    source = [
        row("src/a.py", ""),
        row("src/a_test.py", lr.UNEMBEDDED_EMBED_EXCLUDE),
        row("tests/q.reference", lr.UNEMBEDDED_BY_DESIGN),
    ]

    class _CH:
        async def query_all(self, sql: str, params: dict[str, Any]) -> list[dict[str, Any]]:
            if "SELECT path, chunk_id FROM" in sql:
                return []
            return [r for r in source if r["path"] > params["after"]]

    class _Repo(CodeRepo):
        def __init__(self) -> None:
            super().__init__(_CH())  # type: ignore[arg-type]
            self.written: list[dict[str, Any]] = []

        async def insert_rows(self, table: str, rows: list[dict[str, Any]]) -> None:
            self.written.extend(rows)

    class _Embedder:
        model_name = "second-model"
        dim = 2

        def __init__(self) -> None:
            self.texts: list[str] = []

        async def embed_many(self, texts: list[str]) -> list[list[float]]:
            self.texts.extend(texts)
            return [[0.1, 0.2] for _ in texts]

    repo, emb = _Repo(), _Embedder()
    stats = asyncio.run(
        rm.reembed_namespace(
            repo,
            "o",
            "n",
            source_model="served-model",
            embedder=emb,
        )
    )
    assert emb.texts == ["text of src/a.py"]
    by_path = {r["path"]: r for r in repo.written}
    assert set(by_path) == {r["path"] for r in source}
    assert by_path["src/a.py"]["embedding_dim"] == 2
    assert by_path["src/a.py"]["embed_last_error"] == ""
    for path, marker in (
        ("src/a_test.py", lr.UNEMBEDDED_EMBED_EXCLUDE),
        ("tests/q.reference", lr.UNEMBEDDED_BY_DESIGN),
    ):
        assert by_path[path]["embedding_dim"] == 0 and by_path[path]["embedding"] == []
        assert by_path[path]["embed_last_error"] == marker
        assert by_path[path]["embedding_model"] == "second-model"
    assert stats.rows_written == 3


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

    class _Reads:
        hits = misses = 0

        def __init__(self, *_: Any) -> None: ...

        async def prefetch(self, *_: Any) -> None: ...

        async def flush(self) -> None: ...

    async def prefetch_models(
        _reads: Any, _org: str, _ns: str, sources: dict[str, bytes], **_: Any
    ) -> None:
        prefetched.append(set(sources))

    async def nothing(*_: Any, **__: Any) -> None: ...

    class _Git:
        async def show_file(self, *_: Any, **__: Any) -> bytes:
            return SOURCE

    monkeypatch.setattr(settings, "code_ingest_round_files", 10)
    monkeypatch.setattr(round_reads, "RoundReads", _Reads)
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
            gitrepo=_Git(),
            ingest_file=nothing,
            tombstone_file=nothing,
        )
    )
    assert prefetched == [{"pkg/x.py"}]


def test_loom_code_describe_leaves_a_marked_chunk_alone(monkeypatch: pytest.MonkeyPatch) -> None:
    from server.cli import code_maintenance as cm

    described: list[str] = []
    written: list[dict[str, Any]] = []

    def chunk(path: str, err: str) -> dict[str, Any]:
        return {
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

    chunks = {
        "pkg/x.py": chunk("pkg/x.py", ""),
        "pkg/test_x.py": chunk("pkg/test_x.py", lr.UNEMBEDDED_EMBED_EXCLUDE),
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


def test_a_provider_rebuild_keeps_marked_rows_without_a_vector() -> None:
    from server.insights.app.provider_rebuild_batch import rebuild_batch

    texts: list[str] = []

    async def embed_many(batch: list[str]) -> list[list[float]]:
        texts.extend(batch)
        return [[0.6, 0.8] for _ in batch]

    target = SimpleNamespace(model_name="target-model", dim=2, embed_many=embed_many)

    def chunk(path: str, err: str) -> dict[str, Any]:
        return {
            "org": "o",
            "namespace": "n",
            "embedding_model": "old-model",
            "path": path,
            "content": "",
            "context_text": f"text of {path}",
            "embedding": [],
            "embedding_dim": 0,
            "embed_attempts": 0,
            "embed_last_error": err,
            "embed_failed_at": None,
            "tombstoned_at": None,
        }

    rows = [
        chunk("src/a.py", ""),
        chunk("src/a_test.py", lr.UNEMBEDDED_EMBED_EXCLUDE),
        chunk("tests/q.reference", lr.UNEMBEDDED_BY_DESIGN),
    ]
    out = asyncio.run(rebuild_batch("code_chunks", rows, target)).rows
    assert texts == ["text of src/a.py"]
    assert [r["embedding_dim"] for r in out] == [2, 0, 0]
    assert [r["embed_last_error"] for r in out] == [
        "",
        lr.UNEMBEDDED_EMBED_EXCLUDE,
        lr.UNEMBEDDED_BY_DESIGN,
    ]
    card = {**chunk("src/a_test.py", lr.UNEMBEDDED_EMBED_EXCLUDE), "summary": ""}
    (staged,) = asyncio.run(rebuild_batch("code_summaries", [card], target)).rows
    assert (
        staged["embed_last_error"] == lr.UNEMBEDDED_EMBED_EXCLUDE and staged["embedding_dim"] == 0
    )


def test_the_card_pass_writes_no_card_for_an_embed_excluded_file(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No card is written or carried for an excluded file, and one written before its
    glob is cleared: no text, no vector, the marker, its rank kept."""
    from server.code.store import index as code_index
    from server.code.store.index import build_row
    from server.code.workers import summary as w

    def card(path: str, model: str, summary: str, inputs_hash: str, dim: int) -> dict[str, Any]:
        return build_row(
            "code_summaries",
            org="o",
            namespace="n",
            embedding_model=model,
            scope_type="file",
            scope_path=path,
            summary=summary,
            inputs_hash=inputs_hash,
            pagerank=0.5,
            embedding=[0.1] * dim,
            embedding_dim=dim,
        )

    hashes = {"src/a.py": "h1", "tests/test_a.py": "h2", "tests/test_b.py": "h3"}
    active = [
        card("src/a.py", "m", "", "", 0),
        card("tests/test_a.py", "m", "an old card", "h2", 2),  # written before the glob
        card("tests/test_b.py", "m", "", "", 0),  # a rank placeholder
    ]
    twin = card("tests/test_b.py", "old-model", "a twin card", "h3", 2)  # would be carried
    prompts: list[str] = []
    embedded: list[str] = []
    written: list[dict[str, Any]] = []

    class _Repo:
        async def live_file_hashes(self, *_: Any) -> dict[str, str]:
            return hashes

        async def live_summary_rows(self, *_: Any, **__: Any) -> list[dict[str, Any]]:
            return [dict(r) for r in active]

        async def live_summary_rows_other_models(self, *_: Any, **__: Any) -> list[dict[str, Any]]:
            return [dict(twin)]

        async def pr_churn_ranked(self, *_: Any, **__: Any) -> list[Any]:
            return []

        async def live_chunks_for_path(self, *_: Any) -> list[dict[str, Any]]:
            return [{"start_line": 1, "scope": "f", "content": "def f(): pass"}]

        async def insert_rows(self, _table: str, rows: list[dict[str, Any]]) -> None:
            written.extend(rows)

    class _CodeRepo(code_index.CodeRepo):  # the static row helpers stay real
        def __new__(cls) -> Any:
            return _Repo()

    class _Embedder:
        model_name = "m"

        async def embed(self, text: str) -> list[float]:
            embedded.append(text)
            return [0.1, 0.2]

    class _Llm:
        model = "llm"

        async def complete(self, _system: str, user: str, **_: Any) -> Any:
            prompts.append(user.splitlines()[0])
            return type("R", (), {"text": "a card"})()

    async def no(*_: Any, **__: Any) -> bool:
        return False

    cfg = repo_config(org="o", namespace="n", embed_exclude=("tests/",))
    monkeypatch.setattr(repos, "configured_repos", lambda: [cfg])
    monkeypatch.setattr(code_index, "CodeRepo", _CodeRepo)
    monkeypatch.setattr("server.llm.customer.switched_off", no)
    monkeypatch.setattr("server.observability.budget.namespace_over_cap", no)
    monkeypatch.setattr("server.embedding.embedder.get_embedder", _Embedder)
    monkeypatch.setattr("server.llm.client.get_llm_client_for_model", lambda _m=None: _Llm())
    monkeypatch.setattr("server.workers.cost_event.emit_worker_cost_event_for", lambda **_: None)
    result = w.CodeSummaryResult()
    asyncio.run(w._run(result))
    assert result.errors == []
    assert prompts == ["File: src/a.py"] and embedded == ["a card"]
    live = {r["scope_path"]: r for r in written if r["tombstoned_at"] is None}
    assert set(live) == {"src/a.py", "tests/test_a.py"}
    cleared = live["tests/test_a.py"]
    assert cleared["summary"] == "" and cleared["embedding_dim"] == 0 and cleared["embedding"] == []
    assert cleared["embed_last_error"] == lr.UNEMBEDDED_EMBED_EXCLUDE and cleared["pagerank"] == 0.5
    (gone,) = [r for r in written if r["tombstoned_at"] is not None]
    assert (gone["scope_path"], gone["embedding_model"]) == ("tests/test_b.py", "old-model")
    assert result.cards_written == 1 and result.cards_cleared == 1


# Every code-plane module that calls an embedder or an LLM, or a helper that does. A
# writer makes a file's vector or description, and names the modules whose code keeps an
# embed-excluded path out of it; a reader prompts or embeds with a query. A new caller
# fails `test_every_model_caller_is_declared` until it is listed here.
_WRITERS: dict[str, tuple[str, ...]] = {
    "server/cli/code_maintenance.py": ("server/cli/code_maintenance.py",),
    "server/code/app/index/describe.py": (
        "server/code/app/index/ingest.py",
        "server/cli/code_maintenance.py",
    ),
    "server/code/app/index/embed_reuse.py": ("server/code/app/index/ingest.py",),
    "server/code/app/index/embed_text.py": (
        "server/code/app/index/ingest.py",
        "server/code/store/index/embed_queue.py",
    ),
    "server/code/app/index/ingest.py": ("server/code/app/index/ingest.py",),
    "server/code/app/index/pass_commit.py": ("server/code/app/index/pass_commit.py",),
    "server/code/app/index/reembed_model.py": ("server/code/app/index/reembed_model.py",),
    "server/code/app/index/round_models.py": ("server/code/app/index/pass_commit.py",),
    "server/code/workers/embed_recovery.py": ("server/code/store/index/embed_queue.py",),
    "server/code/workers/summary.py": ("server/code/workers/summary.py",),
    "server/insights/app/provider_rebuild_batch.py": (
        "server/insights/app/provider_rebuild_batch.py",
    ),
}
_READERS: dict[str, str] = {
    "server/code/app/answers.py": "embeds the question and answers from what search returned",
    "server/code/app/bisect_fusion.py": "embeds the report and reranks the window's PRs",
    "server/code/app/search/cards.py": "expands the query into concept terms",
    "server/code/app/search/hyde.py": "embeds a passage written for the query",
    "server/code/app/search/vector.py": "embeds the query",
}
_MODEL_CALLS = frozenset(
    {
        "complete",
        "document_vector",
        "document_vectors",
        "embed",
        "embed_many",
        "embed_shrinking",
        "embeddings_for_texts",
        "prefetch_models",
        "reusable_vectors",
        "_describe_chunk",
    }
)
_MARKER_NAMES = frozenset({"UNEMBEDDED_PREFIX", "embed_excluded", "unembedded_by_design"})
_ROOT = Path(__file__).resolve().parents[2]


def _names(path: str) -> tuple[set[str], set[str], set[str]]:
    """The call names, the identifiers and the string constants of one module."""
    tree = ast.parse((_ROOT / path).read_text(encoding="utf-8"))
    calls, names, strings = set(), set(), set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            fn = node.func
            calls.add(fn.attr if isinstance(fn, ast.Attribute) else getattr(fn, "id", ""))
        elif isinstance(node, ast.Name):
            names.add(node.id)
        elif isinstance(node, ast.Attribute):
            names.add(node.attr)
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            strings.add(node.value)
    return calls, names, strings


def _model_callers() -> set[str]:
    """Modules of the code plane (or naming its vector tables) that call a model."""
    found = set()
    for file in (_ROOT / "server").rglob("*.py"):
        path = file.relative_to(_ROOT).as_posix()
        calls, _, strings = _names(path)
        code_plane = path.startswith(("server/code/", "server/cli/code"))
        if calls & _MODEL_CALLS and (code_plane or strings & {"code_chunks", "code_summaries"}):
            found.add(path)
    return found


def test_every_model_caller_is_declared() -> None:
    assert _model_callers() == set(_WRITERS) | set(_READERS)


@pytest.mark.parametrize("writer", sorted(_WRITERS))
def test_every_writer_is_gated_on_the_marker_or_the_path_rule(writer: str) -> None:
    for gate in _WRITERS[writer]:
        assert _names(gate)[1] & _MARKER_NAMES, f"{gate} does not read the marker or the globs"
