"""A repository is ready when it is searchable.

The bootstrap embeds inline when a real provider is up, the last slice
writes `embedding` until the vector leg covers the threshold, every surface
carries the coverage, code.search says "lexical only" below it, and the
stage detail carries an ETA while indexing.
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from typing import Any

import pytest

from server.code.app.index import bootstrap as bs
from server.settings import settings
from tests.unit.code_fixtures import repo_config

ORG, NS = "o", "code-widgets"


@pytest.fixture(autouse=True)
def _clean(monkeypatch: pytest.MonkeyPatch):
    bs.reset_embed_breaker()
    monkeypatch.setattr(settings, "code_ready_embed_coverage", 0.95)
    yield
    bs.reset_embed_breaker()


# ---- coverage math and the threshold ---------------------------------------


def test_coverage_is_embedded_over_embeddable_and_full_when_nothing_waits() -> None:
    assert bs.embedding_coverage(0, 0) == 1.0
    assert bs.embedding_coverage(100, 41) == pytest.approx(0.41)
    assert bs.embedding_coverage(10, 12) == 1.0  # clamped, never > 1
    assert bs.embedding_coverage(10, -1) == 0.0


def test_ready_threshold_is_the_setting(monkeypatch: pytest.MonkeyPatch) -> None:
    assert bs.is_searchable(0.95) and bs.is_searchable(1.0)
    assert not bs.is_searchable(0.949)
    monkeypatch.setattr(settings, "code_ready_embed_coverage", 0.5)
    assert bs.is_searchable(0.5) and not bs.is_searchable(0.41)


# ---- ETA ---------------------------------------------------------------------


@pytest.mark.parametrize(
    ("remaining", "rate", "expected"),
    [
        (1000, 0.0, ""),  # no rate: nothing to divide by
        (0, 7.0, ""),  # nothing left
        (38 * 60 * 7, 7.0, "~38 min left"),
        (5, 7.0, "~1 min left"),  # never "0 min"
        (2 * 3600 * 7, 7.0, "~2 h left"),
        ((2 * 3600 + 30 * 60) * 7, 7.0, "~2 h 30 min left"),
    ],
)
def test_eta_text(remaining: int, rate: float, expected: str) -> None:
    assert bs.eta_text(remaining, rate) == expected


# ---- which embedder a slice uses --------------------------------------------


class _Real:
    model_name = "text-embedding-3-small"

    async def embed(self, text: str) -> list[float]:
        return [1.0]


def test_bootstrap_embedder_is_none_for_the_fake_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    from server.embedding import embedder as emb
    from server.embedding.fake import FakeNGramEmbedder

    monkeypatch.setattr(emb, "get_embedder", lambda: FakeNGramEmbedder(dim=8))
    assert bs.bootstrap_embedder() is None


def test_bootstrap_embedder_is_the_real_one_unless_the_breaker_is_open(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.embedding import embedder as emb

    real = _Real()
    monkeypatch.setattr(emb, "get_embedder", lambda: real)
    assert bs.bootstrap_embedder() is real
    bs.open_embed_breaker()
    assert bs.bootstrap_embedder() is None
    bs.reset_embed_breaker()
    assert bs.bootstrap_embedder() is real


def test_breaker_closes_by_itself_after_the_hold() -> None:
    bs.open_embed_breaker(now=1000.0)
    assert bs.embed_breaker_open(now=1000.0 + settings.code_bootstrap_embed_breaker_hold_s - 1)
    assert not bs.embed_breaker_open(now=1000.0 + settings.code_bootstrap_embed_breaker_hold_s)


# ---- index_slice end to end over fakes ---------------------------------------


class _Repo:
    def __init__(self, embeddable: int = 0, embedded: int = 0) -> None:
        self.state: dict[str, str] = {}
        self.cov = (embeddable, embedded)
        self.stages: list[tuple[str, dict[str, Any]]] = []

    async def get_state(self, org: str, ns: str, key: str) -> str:
        return self.state.get(key, "")

    async def set_state(self, org: str, ns: str, key: str, value: str, detail: str = "") -> None:
        self.state[key] = value
        if key == bs.STAGE_KEY:
            self.stages.append((value, json.loads(detail) if detail else {}))

    async def chunk_embed_coverage(self, org: str, ns: str) -> tuple[int, int]:
        return self.cov


def _cfg() -> Any:
    return repo_config(
        org=ORG,
        namespace=NS,
        git_dir="/nowhere.git",
        path_prefixes=(),
    )


def _wire(
    monkeypatch: pytest.MonkeyPatch,
    paths: list[str],
    embedder: Any,
    *,
    provider_answers: bool = True,
) -> list[dict[str, Any]]:
    """Fake the git reads, the denylist, ingest_file (recording its kwargs)
    and the stamps; `provider_answers=False` makes every chunk a stub."""
    from server.code.app.index import denylist as dl
    from server.code.app.index import gitrepo, ingest_headers
    from server.code.app.index import ingest as ing
    from server.embedding import embedder as emb

    calls: list[dict[str, Any]] = []

    async def ls_tree(git_dir: str, head: str, prefixes: Any) -> list[str]:
        return list(paths)

    async def show_file(git_dir: str, head: str, path: str) -> bytes:
        return b"int x;"

    async def load_denylist(cfg: Any, head: str, show: Any) -> None:
        return None

    async def ingest_file(repo: Any, org: str, ns: str, **kw: Any) -> Any:
        calls.append(kw)
        embedded = 1 if kw["embedder"] is not None and provider_answers else 0
        return SimpleNamespace(embedded=embedded, embed_stubs=1 - embedded)

    async def stamps(repo: Any, org: str, ns: str) -> None:
        repo.state["stamps"] = "recorded"

    monkeypatch.setattr(gitrepo, "ls_tree", ls_tree)
    monkeypatch.setattr(gitrepo, "show_file", show_file)
    monkeypatch.setattr(dl, "load_denylist", load_denylist)
    monkeypatch.setattr(ing, "ingest_file", ingest_file)
    monkeypatch.setattr(ingest_headers, "headers_first", lambda ps: list(ps))
    monkeypatch.setattr(emb, "get_embedder", lambda: embedder)
    monkeypatch.setattr(bs, "record_backfill_stamps", stamps)
    return calls


def test_a_slice_embeds_with_the_real_provider_and_writes_the_eta(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real = _Real()
    calls = _wire(monkeypatch, ["a.cpp", "b.cpp", "c.cpp"], real)
    repo = _Repo()
    progress = asyncio.run(bs.index_slice(_cfg(), repo, "f" * 40, max_files=2))
    assert progress == {"done": 2, "total": 3, "stage": "indexing"}
    assert len(calls) == 2 and all(c["embedder"] is real for c in calls)
    assert all(c["embedding_model"] == real.model_name for c in calls)
    stage, detail = repo.stages[-1]
    assert stage == "indexing"
    assert detail["done"] == 2 and detail["total"] == 3 and detail["embedded"] == 2
    assert detail["eta"] == "~1 min left"  # 1 file left at an instant rate
    assert repo.state[bs.CURSOR_KEY] == "2"
    assert not bs.embed_breaker_open()  # the provider answered


def test_a_slice_stores_stubs_for_the_fake_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    from server.embedding.fake import FakeNGramEmbedder

    calls = _wire(monkeypatch, ["a.cpp", "b.cpp"], FakeNGramEmbedder(dim=8))
    asyncio.run(bs.index_slice(_cfg(), _Repo(), "f" * 40, max_files=1))
    assert calls and all(c["embedder"] is None for c in calls)


def test_a_slice_stores_stubs_while_the_breaker_is_open(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _wire(monkeypatch, ["a.cpp", "b.cpp"], _Real())
    bs.open_embed_breaker()
    asyncio.run(bs.index_slice(_cfg(), _Repo(), "f" * 40, max_files=1))
    assert calls and all(c["embedder"] is None for c in calls)


def test_a_slice_the_provider_answered_nothing_opens_the_breaker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _wire(monkeypatch, ["a.cpp", "b.cpp", "c.cpp"], _Real(), provider_answers=False)
    repo = _Repo()
    asyncio.run(bs.index_slice(_cfg(), repo, "f" * 40, max_files=2))
    assert all(c["embedder"] is not None for c in calls)  # this slice tried
    assert bs.embed_breaker_open()
    assert repo.stages[-1][1]["embed_stubs"] == 2


def test_the_last_slice_is_embedding_below_the_threshold_and_ready_above(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _wire(monkeypatch, ["a.cpp"], _Real())
    behind = _Repo(embeddable=100, embedded=41)
    progress = asyncio.run(bs.index_slice(_cfg(), behind, "f" * 40))
    assert progress["stage"] == "embedding"
    stage, detail = behind.stages[-1]
    assert stage == "embedding"
    assert detail["chunks"] == 100 and detail["embedded"] == 41
    assert detail["embedding_coverage"] == pytest.approx(0.41)
    assert detail["total"] == 1 and detail["head"] == "f" * 12
    assert behind.state["git_head"] == "f" * 40 and behind.state["stamps"] == "recorded"
    assert behind.state[bs.CURSOR_KEY] == ""

    covered = _Repo(embeddable=100, embedded=96)
    progress = asyncio.run(bs.index_slice(_cfg(), covered, "f" * 40))
    assert progress["stage"] == "ready"
    assert covered.stages[-1][0] == "ready"


def test_the_ingest_pass_moves_embedding_to_ready_when_covered() -> None:
    repo = _Repo(embeddable=100, embedded=50)
    repo.state[bs.STAGE_KEY] = "embedding"
    assert asyncio.run(bs.maybe_finish_embedding(_cfg(), repo)) == "embedding"
    assert repo.stages[-1][1]["embedding_coverage"] == pytest.approx(0.5)  # refreshed
    repo.cov = (100, 95)
    assert asyncio.run(bs.maybe_finish_embedding(_cfg(), repo)) == "ready"
    assert repo.state[bs.STAGE_KEY] == "ready"
    # not in that stage: nothing read, nothing written
    n = len(repo.stages)
    assert asyncio.run(bs.maybe_finish_embedding(_cfg(), repo)) == ""
    assert len(repo.stages) == n


# ---- recovery batch ------------------------------------------------------------


def test_recovery_batch_is_the_setting(monkeypatch: pytest.MonkeyPatch) -> None:
    from server.code.workers import embed_recovery as rec

    monkeypatch.setattr(settings, "code_embed_recovery_batch", 250)
    assert rec._batch() == 250


# ---- coverage on the polled surfaces and the search note --------------------------


def test_coverage_fields_come_from_the_counts_read() -> None:
    from server.code.app.index_status import coverage_fields

    f = coverage_fields(
        {"code_chunks": 120, "code_chunks_embedded": 41, "code_chunks_embeddable": 100}
    )
    assert f == {"chunks_total": 120, "chunks_embedded": 41, "embedding_coverage": 0.41}
    # a stored counts row without the coverage keys: fully covered, no note
    assert coverage_fields({"code_chunks": 5})["embedding_coverage"] == 1.0
    assert coverage_fields({})["embedding_coverage"] == 1.0


def test_counts_cache_is_short_while_building_and_long_otherwise() -> None:
    from server.code.app.index_status import _counts_max_age_s

    assert _counts_max_age_s({bs.STAGE_KEY: {"value": "embedding"}}) == 60.0
    assert _counts_max_age_s({bs.STAGE_KEY: {"value": "indexing"}}) == 60.0
    assert _counts_max_age_s({bs.STAGE_KEY: {"value": "ready"}}) == 600.0
    assert _counts_max_age_s({}) == 600.0


class _CountsRepo:
    def __init__(self, counts: dict[str, int] | None) -> None:
        self.counts = counts

    async def all_state(self, org: str, ns: str) -> dict[str, Any]:
        return {}

    async def table_counts(self, org: str, ns: str, **kw: Any) -> dict[str, int]:
        if self.counts is None:
            raise RuntimeError("counts unavailable")
        return self.counts


def _note(counts: dict[str, int] | None) -> str:
    from server.code.app import common as cc

    cc._COVERAGE_NOTE_CACHE.clear()
    return asyncio.run(cc.embedding_coverage_note(_CountsRepo(counts), ORG, NS))  # type: ignore[arg-type]


def test_search_note_appears_below_the_threshold_and_not_above() -> None:
    behind = {"code_chunks": 100, "code_chunks_embedded": 41, "code_chunks_embeddable": 100}
    assert _note(behind) == "lexical only until embeddings finish (41% embedded)"
    covered = {"code_chunks": 100, "code_chunks_embedded": 96, "code_chunks_embeddable": 100}
    assert _note(covered) == ""
    assert _note(None) == ""  # fails open: a note never sinks an answer


def test_search_note_is_cached_per_namespace() -> None:
    from server.code.app import common as cc

    behind = {"code_chunks": 100, "code_chunks_embedded": 41, "code_chunks_embeddable": 100}
    assert _note(behind).startswith("lexical only")
    # a second call inside the TTL does not read again (the repo would raise)
    again = asyncio.run(cc.embedding_coverage_note(_CountsRepo(None), ORG, NS))  # type: ignore[arg-type]
    assert again.startswith("lexical only")
    cc._COVERAGE_NOTE_CACHE.clear()


def test_memo_replay_carries_this_calls_note(monkeypatch: pytest.MonkeyPatch) -> None:
    """A search stored while the index was lexical-only must not replay the
    stale sentence once the vector leg is filled, and vice versa."""
    from server.code.api.models_search import CodeSearchInput, CodeSearchOutput
    from server.code.app import common as cc
    from server.code.app.search import handler as h

    monkeypatch.setattr(settings, "code_trace_log_enabled", False)
    stored = CodeSearchOutput(
        results=[], route="hybrid", note="lexical only until embeddings finish (10% embedded)"
    )
    row = {"payload": stored.model_dump_json(), "indexed_commit": "a" * 40}

    async def _as_of(repo: Any, org: str, ns: str) -> dict[str, Any]:
        return {"commit": "a" * 40}

    async def _covered(repo: Any, org: str, ns: str) -> str:
        return ""

    monkeypatch.setattr(h, "index_as_of", _as_of)
    monkeypatch.setattr(h, "embedding_coverage_note", _covered)
    monkeypatch.setattr(cc, "index_as_of", _as_of)
    inp = CodeSearchInput(org=ORG, namespace=NS, query="q")
    out = asyncio.run(
        h._replay_memo_hit(row, inp, ORG, NS, object(), embedding_model="m", started=0.0)  # type: ignore[arg-type]
    )
    assert out.route == "hybrid+memo" and out.note == ""


def test_fanout_note_names_the_namespace_that_is_still_embedding() -> None:
    from server.code.api.models_search import CodeSearchInput, CodeSearchOutput
    from server.code.app import search_fanout as fo

    a = CodeSearchOutput(
        results=[], route="hybrid", note="lexical only until embeddings finish (41% embedded)"
    )
    b = CodeSearchOutput(results=[], route="hybrid")
    answered: Any = {"code-a": (a, 10), "code-b": (b, 12)}
    inp = CodeSearchInput(org=ORG, namespace="code-a", query="q", namespaces=["code-a", "code-b"])
    merged = fo.assemble(
        inp,
        ["code-a", "code-b"],
        answered,
        [],
        [],
        0,
        timeout_ms=1000,
        output_model=CodeSearchOutput,
    )
    assert merged.note == "code-a: lexical only until embeddings finish (41% embedded)"


# ---- code.repos carries the server's verdict ---------------------------------


@pytest.mark.asyncio
async def test_code_repos_rows_carry_searchable_at_the_configured_threshold(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The Code view reads `searchable` instead of copying the threshold: under an
    operator's 0.8, a repository at 0.85 is searchable and one at 0.5 is not."""
    import server.auth.authz as authz
    import server.code.domain.index.repos as repos
    import server.identity.app.namespace_classification as nc
    from server.auth.context import set_current_auth
    from server.auth.jwt import AuthInfo
    from server.code.api.models_repos import CodeReposInput
    from server.code.app import index_status
    from server.code.app import repos as repos_mod
    from server.operations.base import Role

    monkeypatch.setattr(settings, "code_ready_embed_coverage", 0.8)
    coverage = {"code-above": 0.85, "code-below": 0.5}
    entries = [
        repo_config(
            org=ORG,
            namespace=ns,
            repo=f"acme/{ns}",
            git_branch="main",
            classification="standard",
            org_readable=True,
        )
        for ns in coverage
    ]
    monkeypatch.setattr(repos, "configured_repos", lambda: entries)
    monkeypatch.setattr(index_status, "require_code_intel", lambda: None)

    async def _refresh() -> None:
        return None

    monkeypatch.setattr("server.code.app.index.repo_registry.refresh_db_repos", _refresh)

    async def _status(cfg: Any) -> Any:
        return SimpleNamespace(
            org=cfg.org,
            namespace=cfg.namespace,
            repo=cfg.repo,
            branch=cfg.git_branch,
            git_head="h",
            clone_head="h",
            fast_tier_lag=0,
            scip_lag=0,
            scip_py_lag=0,
            tier_health=[],
            state={},
            last_refresh_at="",
            counts={},
            chunks_total=100,
            chunks_embedded=int(coverage[cfg.namespace] * 100),
            embedding_coverage=coverage[cfg.namespace],
        )

    monkeypatch.setattr(index_status, "_repo_status", _status)

    async def _allow(
        auth: Any, role: Any, org: Any = None, namespace: Any = None, **_kw: Any
    ) -> None:
        return None

    monkeypatch.setattr(authz, "check_authorized", _allow)

    async def _state(auth: Any) -> Any:
        return {}, {}, {}, None

    monkeypatch.setattr(repos_mod, "_repo_directory_state", _state)
    index_status.reset_repo_status_cache()
    nc.reset()
    set_current_auth(
        AuthInfo(
            subject="u",
            org=ORG,
            namespaces=tuple(coverage),
            roles=(Role.VIEWER,),
            is_service_account=False,
        )
    )
    try:
        out = await repos_mod.handle_code_repos(CodeReposInput(org=ORG))
    finally:
        set_current_auth(None)
        index_status.reset_repo_status_cache()
        nc.reset()
    assert {r.namespace: r.searchable for r in out.repos} == {
        "code-above": True,
        "code-below": False,
    }
