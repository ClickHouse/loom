"""Every background job leaves an org whose model calls are switched off out
when it picks its work: in the query, so the org's rows spend no attempt and
fill no batch ahead of another org's, which still runs."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest

from server.llm import customer
from server.settings import settings
from tests.unit.switch_state import switch_off, switch_stale


class _CH:
    """Records each statement and its parameters; answers `rows` filtered by
    the statement's switched-off orgs, as ClickHouse would."""

    def __init__(self, rows: list[dict[str, Any]] | None = None) -> None:
        self.sent: list[tuple[str, dict[str, Any]]] = []
        self.rows = rows or []

    async def query_all(self, sql: str, params: dict[str, Any] | None = None, **_: Any) -> Any:
        params = params or {}
        self.sent.append((sql, params))
        off = set(params.get("off") or [])
        return [r for r in self.rows if r["org"] not in off]

    async def query_one(self, *a: Any, **k: Any) -> Any:
        return None

    async def insert(self, *a: Any, **k: Any) -> None:
        return None


def _excludes(ch: _CH, org: str) -> bool:
    return bool(ch.sent) and all(
        "{off:Array(String)}" in sql and org in params["off"] for sql, params in ch.sent
    )


def test_profile_consolidation_selects_no_identity_of_a_switched_off_org(monkeypatch) -> None:
    from server.memory.workers import profile_consolidation as w

    ch = _CH(
        [
            {"org": "acme", "namespace": "n", "user_id": "u", "n": 9},
            {"org": "beta", "namespace": "n", "user_id": "u", "n": 3},
        ]
    )
    folded: list[str] = []

    async def fold(ch: Any, row: dict[str, Any], cfg: Any) -> bool:
        folded.append(row["org"])
        return True

    monkeypatch.setattr("server.core.ch.get_client", lambda: ch)
    monkeypatch.setattr(w, "_consolidate_identity", fold)
    switch_off("acme")
    asyncio.run(w.run_pass(w.ProfileConsolidationConfig(max_identities_per_pass=1)))
    assert _excludes(ch, "acme") and folded == ["beta"]  # acme, the larger, filled no slot


def test_observe_extract_selects_no_session_of_a_switched_off_org() -> None:
    from server.memory.workers import observe_extract as w

    ch = _CH(
        [
            {"org": "acme", "namespace": "n", "agent": "a", "user_id": "u", "session_id": "s"},
            {"org": "beta", "namespace": "n", "agent": "a", "user_id": "u", "session_id": "s"},
        ]
    )
    switch_off("acme")
    sessions = asyncio.run(
        w.find_ready_sessions(w.ObserveExtractConfig(max_sessions_per_pass=1), ch=ch)
    )
    assert _excludes(ch, "acme") and [s["org"] for s in sessions] == ["beta"]


def test_projection_repair_scans_no_failure_of_a_switched_off_org(monkeypatch) -> None:
    from server.memory.app import projection_failures
    from server.memory.workers import projection_repair as w

    ch = _CH()
    asyncio.run(
        projection_failures.scan_active_projection_failures(
            limit=5, exclude_orgs=frozenset({"acme"}), ch=ch
        )
    )
    assert _excludes(ch, "acme")
    seen: list[frozenset[str]] = []

    async def scan(*, limit: int, exclude_orgs: frozenset[str], after: Any) -> list[Any]:
        seen.append(exclude_orgs)
        return []

    monkeypatch.setattr(projection_failures, "scan_active_projection_failures", scan)
    switch_off("acme")
    asyncio.run(w.run_pass(w.ProjectionRepairConfig()))
    assert seen == [frozenset({"acme"})]


def test_the_titler_selects_no_thread_of_a_switched_off_org(monkeypatch) -> None:
    from server.knowledge.store import conversations
    from server.knowledge.workers import conversation_titler as w

    ch = _CH()
    asyncio.run(
        conversations.titler_candidates(
            window_hours=1, min_turns=1, limit=5, exclude_orgs=frozenset({"acme"}), ch=ch
        )
    )
    assert _excludes(ch, "acme")
    seen: list[frozenset[str]] = []

    async def candidates(**kw: Any) -> list[Any]:
        seen.append(kw["exclude_orgs"])
        return []

    class _LLM:
        model = "gpt-4o-mini"

    monkeypatch.setattr(conversations, "titler_candidates", candidates)
    monkeypatch.setattr("server.llm.client.get_llm_client", lambda: _LLM())
    switch_off("acme")
    asyncio.run(w.run_pass(w.ConversationTitlerConfig()))
    assert seen == [frozenset({"acme"})]


def test_reflection_distillation_judges_and_contrasts_no_switched_off_org() -> None:
    from server.memory.app import session_outcomes as so

    ch = _CH()
    cfg = so.DistillConfig()
    result = so.DistillResult()
    switch_off("acme")
    asyncio.run(so.label_sessions(ch, object(), cfg, result))
    asyncio.run(so.distil_contrasts(ch, object(), cfg, result))
    assert len(ch.sent) == 2 and _excludes(ch, "acme")


def test_code_embed_recovery_scans_no_repository_of_a_switched_off_org(monkeypatch) -> None:
    from server.code.workers import embed_recovery as w

    scanned: list[list[tuple[str, str]]] = []

    async def recover(table: str, repos: list[tuple[str, str]], *a: Any, **k: Any) -> int:
        scanned.append(repos)
        return 0

    repos = [
        SimpleNamespace(org="acme", namespace="code-a"),
        SimpleNamespace(org="beta", namespace="code-b"),
    ]
    monkeypatch.setattr("server.code.domain.index.repos.configured_repos", lambda: repos)
    monkeypatch.setattr(w, "_recover_table", recover)
    from server.code.store import index as code_index

    class _CodeRepo(code_index.CodeRepo):  # static row helpers stay real for later importers
        def __new__(cls) -> Any:
            return object()

    monkeypatch.setattr(code_index, "CodeRepo", _CodeRepo)
    monkeypatch.setattr("server.embedding.embedder.get_embedder", lambda: object())
    switch_off("acme")
    asyncio.run(w._run(w.CodeEmbedRecoveryResult()))
    assert scanned and all(r == [("beta", "code-b")] for r in scanned)


def test_an_org_switched_off_mid_code_recovery_pass_keeps_its_stubs_and_attempts() -> None:
    """acme is switched off after its stubs were read: its embed call is refused,
    and its stubs are neither re-inserted nor charged an attempt; beta's are
    embedded in the same pass."""
    from server.code.workers import embed_recovery as w

    class _Embedder:
        async def embed_many(self, texts: list[str]) -> list[list[float]]:
            await customer.require()
            return [[0.5, 0.5] for _ in texts]

        async def embed(self, text: str) -> list[float]:
            await customer.require()
            return [0.5, 0.5]

    class _Repo:
        rows: list[Any] = []

        def tombstone_of(self, table: str, live: dict[str, Any]) -> dict[str, Any]:
            return dict(live)

        async def insert_rows(self, table: str, rows: list[Any]) -> None:
            self.rows = self.rows + list(rows)

    repo = _Repo()
    rows = [
        {"org": org, "namespace": f"code-{org}", "text": "def f(): pass", "embed_attempts": 4}
        for org in ("acme", "beta")
    ]
    result = w.CodeEmbedRecoveryResult()
    switch_off("acme")
    embedded = asyncio.run(
        w._embed_rows(
            repo,
            "code_chunks",
            rows,
            _Embedder(),
            text_of=lambda r: r["text"],
            max_attempts=5,
            result=result,
        )
    )
    assert embedded == 1 and result.failed == 0 and result.poisoned == 0
    assert result.switched_off == 1
    assert [r["org"] for r in repo.rows] == ["beta"]


def test_code_summaries_and_ingest_skip_a_switched_off_orgs_repositories(monkeypatch) -> None:
    from server.code.workers import summary as w

    summarized: list[str] = []

    async def summarize(org: str, ns: str, result: Any, *, cap: int) -> None:
        summarized.append(org)

    repos = [
        SimpleNamespace(org="acme", namespace="code-a"),
        SimpleNamespace(org="beta", namespace="code-b"),
    ]
    monkeypatch.setattr("server.code.domain.index.repos.configured_repos", lambda: repos)
    monkeypatch.setattr(w, "_summarize_repo", summarize)
    switch_off("acme")
    asyncio.run(w._run(w.CodeSummaryResult()))
    assert summarized == ["beta"]


def test_tracker_embedding_leaves_a_switched_off_orgs_cursor_where_it_is(monkeypatch) -> None:
    from server.tracker.workers import embed as w

    reached: list[str] = []

    def make(org: str, ns: str) -> Any:
        reached.append(org)
        raise RuntimeError("stop here: the source is all this test needs")

    namespaces = {"t-a": SimpleNamespace(org="acme"), "t-b": SimpleNamespace(org="beta")}
    monkeypatch.setattr(
        "server.tracker.domain.namespaces.configured_namespaces", lambda: namespaces
    )
    switch_off("acme")
    with pytest.raises(RuntimeError):
        asyncio.run(
            w._run(
                w.TrackerEmbedResult(),
                repo=object(),
                source_factory=make,
                embedder=object(),
                state={},
            )
        )
    assert reached == ["beta"]  # acme's source, page and cursor were never touched


def test_an_org_switched_off_mid_pass_keeps_its_stub_and_its_attempts(monkeypatch) -> None:
    """The stubs are picked with both orgs on; acme is switched off before the
    embed. acme's stub is neither re-inserted nor charged an attempt; beta's is
    embedded."""
    import server.memory.store as memory_repo
    from server.memory.workers import embedding as w

    stubs = [
        memory_repo.build_embedding_row(
            org=org,
            namespace="n",
            memory_key=f"{org}-k",
            content_excerpt="text",
            embedding_model="m",
            embedding_dim=0,
            embed_attempts=2,
        )
        for org in ("acme", "beta")
    ]
    written: list[dict[str, Any]] = []

    class _Embedder:
        model_name, dim = "m", 2

        async def embed_many(self, texts: list[str]) -> list[list[float]]:
            await customer.require()
            return [[0.5, 0.5] for _ in texts]

    async def none(*a: Any, **k: Any) -> list[Any]:
        return []

    async def picked(**kw: Any) -> list[dict[str, Any]]:
        assert kw["exclude_orgs"] == frozenset()  # both on when the stubs are read
        switch_off("acme")
        return [dict(s) for s in stubs]

    async def served(rows: list[dict[str, Any]], *a: Any) -> tuple[list[Any], list[Any]]:
        return rows, []

    async def insert(rows: list[dict[str, Any]], **_: Any) -> None:
        written.extend(rows)

    async def nothing(*a: Any, **k: Any) -> None:
        return None

    monkeypatch.setattr(w, "get_embedder", lambda: _Embedder())
    monkeypatch.setattr(memory_repo, "find_memories_without_embedding_rows", none)
    monkeypatch.setattr(memory_repo, "find_embedding_stubs", picked)
    monkeypatch.setattr(memory_repo, "insert_embedding_rows", insert)
    monkeypatch.setattr(memory_repo, "retire_copies_under_unembedded_heads", nothing)
    monkeypatch.setattr(memory_repo, "index_late_rows", nothing)
    monkeypatch.setattr(w, "_stubs_still_served", served)
    monkeypatch.setattr(w, "_refresh_entities", nothing)
    switch_off()
    result = asyncio.run(w.run_pass(w.EmbeddingWorkerConfig()))
    assert {r["org"] for r in written} == {"beta"}
    assert all(r["embedding_dim"] == 2 and r["embed_attempts"] == 2 for r in written)
    assert result.embedded_count == 1 and result.failed_count == 0


def test_an_org_switched_off_mid_observe_extract_leaves_its_observations_to_distil(
    monkeypatch,
) -> None:
    """The extras' refused call propagates, so a switch-off between the facts and
    the extras leaves the session un-DONE, its events and notes not lost."""
    import server.memory.store as memory_repo
    from server.memory.workers import observe_extract as ox

    done: list[Any] = []

    async def replay(*a: Any, **k: Any) -> int:
        return 1

    async def context(*a: Any, **k: Any) -> list[Any]:
        return []

    class _LLM:
        async def complete(self, *a: Any, **k: Any) -> Any:
            await customer.require()
            return SimpleNamespace(text="")

    async def extras(*a: Any, **k: Any) -> Any:
        from server.memory.app import trajectory_notes as tn

        switch_off("acme")  # switched off while the extras ran
        return await tn.distill_notes(a[0], [_useful], llm=_LLM())

    async def insert(rows: list[Any], **_: Any) -> None:
        done.extend(rows)

    from server.memory.app import trajectory_notes as tn

    monkeypatch.setattr(settings, "observe_extract_notes_min_observations", 0)
    _useful = tn.Observation("o1", "command_run", "Bash", "pytest -q", "", (), None, None)
    monkeypatch.setattr(ox, "_extract_replay", replay)
    monkeypatch.setattr(ox, "_load_session_context", context)
    monkeypatch.setattr(ox, "get_client_for_context", lambda: None)
    monkeypatch.setattr(ox, "_distill_extras", extras)
    monkeypatch.setattr("server.core.ch.get_client", lambda: None)
    monkeypatch.setattr(memory_repo, "build_memory_version_row", lambda o, **_k: o)
    monkeypatch.setattr(memory_repo, "insert_memory_versions", insert)
    session = {"org": "acme", "namespace": "n", "agent": "a", "user_id": "-", "session_id": "s"}
    obs = SimpleNamespace(
        value={"narrative": "ran the tests"}, sensitivity="", tags=[], valid_at=None
    )
    switch_off()
    with customer.for_org("acme"), pytest.raises(customer.ModelCallsDisabled):  # as run_pass
        asyncio.run(ox._distill_session(session, [obs]))
    assert done == []  # not marked DONE: distilled again once the org is back on


def test_a_stale_switch_stops_the_embedding_worker_before_any_write(monkeypatch) -> None:
    """With no customer header too: the pass writes nothing rather than embed
    for an org it cannot tell is off."""
    import server.memory.store as memory_repo
    from server.core.errors import BackendUnavailableError
    from server.memory.workers import embedding as w

    written: list[Any] = []

    async def insert(rows: list[dict[str, Any]], **_: Any) -> None:
        written.extend(rows)

    async def none(*a: Any, **k: Any) -> list[Any]:
        return []

    monkeypatch.setattr(memory_repo, "find_memories_without_embedding_rows", none)
    monkeypatch.setattr(memory_repo, "insert_embedding_rows", insert)
    switch_stale()
    with pytest.raises(BackendUnavailableError):
        asyncio.run(w.run_pass(w.EmbeddingWorkerConfig()))
    assert written == []


def test_a_switch_gone_stale_mid_embedding_pass_writes_nothing_and_spends_no_attempt(
    monkeypatch,
) -> None:
    """With the customer header set the batch is split without reading the
    switch, so the copy is first found stale inside the embed call: the pass
    stops before its write, not with a provider failure on every stub."""
    import server.memory.store as memory_repo
    from server.memory.workers import embedding as w

    monkeypatch.setattr(settings, "inference_customer_header", "X-Customer")
    stubs = [
        memory_repo.build_embedding_row(
            org="acme",
            namespace="n",
            memory_key="acme-k",
            content_excerpt="text",
            embedding_model="m",
            embedding_dim=0,
            embed_attempts=4,
        )
    ]
    written: list[dict[str, Any]] = []

    class _Embedder:
        model_name, dim = "m", 2

        async def embed_many(self, texts: list[str]) -> list[list[float]]:
            await customer.require()
            return [[0.5, 0.5] for _ in texts]

    async def none(*a: Any, **k: Any) -> list[Any]:
        return []

    async def picked(**kw: Any) -> list[dict[str, Any]]:
        switch_stale()  # fresh when the stubs are read, stale by the embed
        return [dict(s) for s in stubs]

    async def served(rows: list[dict[str, Any]], *a: Any) -> tuple[list[Any], list[Any]]:
        return rows, []

    async def insert(rows: list[dict[str, Any]], **_: Any) -> None:
        written.extend(rows)

    async def nothing(*a: Any, **k: Any) -> None:
        return None

    monkeypatch.setattr(w, "get_embedder", lambda: _Embedder())
    monkeypatch.setattr(memory_repo, "find_memories_without_embedding_rows", none)
    monkeypatch.setattr(memory_repo, "find_embedding_stubs", picked)
    monkeypatch.setattr(memory_repo, "insert_embedding_rows", insert)
    monkeypatch.setattr(memory_repo, "retire_copies_under_unembedded_heads", nothing)
    monkeypatch.setattr(memory_repo, "index_late_rows", nothing)
    monkeypatch.setattr(w, "_stubs_still_served", served)
    monkeypatch.setattr(w, "_refresh_entities", nothing)
    switch_off()
    with pytest.raises(customer.SwitchUnread):
        asyncio.run(w.run_pass(w.EmbeddingWorkerConfig(max_attempts=5)))
    assert written == []


def test_a_switch_gone_stale_mid_code_recovery_pass_writes_nothing() -> None:
    from server.code.workers import embed_recovery as w

    class _Embedder:
        async def embed_many(self, texts: list[str]) -> list[list[float]]:
            await customer.require()
            return [[0.5, 0.5] for _ in texts]

        async def embed(self, text: str) -> list[float]:
            await customer.require()
            return [0.5, 0.5]

    class _Repo:
        rows: list[Any] = []

        def tombstone_of(self, table: str, live: dict[str, Any]) -> dict[str, Any]:
            return dict(live)

        async def insert_rows(self, table: str, rows: list[Any]) -> None:
            self.rows = rows

    repo = _Repo()
    rows = [{"org": "acme", "namespace": "code-a", "text": "def f(): pass", "embed_attempts": 4}]
    result = w.CodeEmbedRecoveryResult()
    switch_stale()
    with pytest.raises(customer.SwitchUnread):
        asyncio.run(
            w._embed_rows(
                repo,
                "code_chunks",
                rows,
                _Embedder(),
                text_of=lambda r: r["text"],
                max_attempts=5,
                result=result,
            )
        )
    assert repo.rows == [] and result.failed == 0 and result.poisoned == 0


def test_code_backfill_stops_on_a_switched_off_org_and_records_its_pass(monkeypatch) -> None:
    """The org's rows stay as they are, so without the stop the next page is the
    same one, forever."""
    import argparse

    from server.cli import code_backfill

    pages: list[int] = []
    recorded: list[dict[str, Any]] = []

    class _Embedder:
        model_name, dim = "m", 2

        async def embed_many(self, texts: list[str]) -> list[list[float]]:
            await customer.require()
            return [[0.5, 0.5] for _ in texts]

    class _Repo:
        async def pending_embed_chunks(self, *_a: Any, **_k: Any) -> list[dict[str, Any]]:
            pages.append(1)
            assert len(pages) < 3, "the backfill re-read the same page"
            return [{"org": "acme", "namespace": "code-a", "content": "def f(): pass"}]

        def tombstone_of(self, table: str, live: dict[str, Any]) -> dict[str, Any]:
            return dict(live)

        async def insert_rows(self, table: str, rows: list[Any]) -> None:
            return None

    async def record(*a: Any, **k: Any) -> None:
        recorded.append(k)

    async def compact(*a: Any, **k: Any) -> None:
        return None

    monkeypatch.setattr("server.embedding.embedder.get_embedder", lambda: _Embedder())
    monkeypatch.setattr(code_backfill, "_record_manual_pass", record)
    monkeypatch.setattr(code_backfill, "_compact_after_bulk", compact)
    switch_off("acme")
    rc = asyncio.run(code_backfill._backfill_embed(_Repo(), "acme", "code-a", argparse.Namespace()))
    assert rc == 1 and pages == [1]
    assert len(recorded) == 1 and recorded[0]["phase"] == "embed"


def test_the_code_backfill_index_run_sends_nothing_for_a_switched_off_org(monkeypatch) -> None:
    """The run's model calls are the org's: with no customer header the switch
    still refuses them, and the run stops with its cursor where it was, its
    pass compacted and recorded like its other exits."""
    import argparse

    from server.cli import code_backfill

    sent: list[str] = []

    class _Embedder:
        model_name, dim = "m", 2

        async def embed_many(self, texts: list[str]) -> list[list[float]]:
            await customer.require()  # as every sender does
            sent.extend(texts)
            return [[0.5, 0.5] for _ in texts]

    class _Repo:
        state: dict[str, str] = {}

        async def get_state(self, org: str, ns: str, key: str) -> str:
            return ""

        async def all_state(self, org: str, ns: str) -> dict[str, Any]:
            return {}

        async def set_state(self, org: str, ns: str, key: str, value: str, **k: Any) -> None:
            self.state[key] = value

        async def insert_rows(self, table: str, rows: list[Any]) -> None:
            return None

        async def name_candidates(self, *a: Any, **k: Any) -> dict[str, Any]:
            return {}

        def __getattr__(self, name: str) -> Any:
            if not name.startswith("live_") and name != "chunks_full_by_ids":
                raise AttributeError(name)

            async def nothing(*a: Any, **k: Any) -> Any:
                return {} if name.endswith("_for_paths") else []

            return nothing

    async def connect() -> None:
        return None

    async def head(*a: Any) -> str:
        return "sha"

    async def tree(*a: Any) -> list[str]:
        return ["a.py"]

    async def show(*a: Any) -> bytes:
        return b"def f():\n    return 1\n"

    async def no_denylist(*a: Any) -> None:
        return None

    recorded: list[dict[str, Any]] = []
    compacted: list[Any] = []

    async def record(*a: Any, **k: Any) -> None:
        recorded.append(k)

    async def compact(*a: Any) -> None:
        compacted.append(a)

    repo = _Repo()
    cfg = SimpleNamespace(
        org="acme",
        namespace="code-a",
        git_dir="/g",
        git_branch="main",
        path_prefixes=("",),
        embed_reuse_from="",
    )
    monkeypatch.setattr("server.core.ch.get_client", lambda: SimpleNamespace(connect=connect))
    from server.code.store import index as code_index

    class _CodeRepo(code_index.CodeRepo):  # static row helpers stay real for later importers
        def __new__(cls) -> Any:
            return repo

    monkeypatch.setattr(code_index, "CodeRepo", _CodeRepo)
    monkeypatch.setattr(code_backfill, "_repo_cfg", lambda args: cfg)
    monkeypatch.setattr(code_backfill.os.path, "isdir", lambda p: True)
    monkeypatch.setattr(code_backfill, "_backfill_head", head)
    monkeypatch.setattr(code_backfill, "_tree_paths_with_coverage", tree)
    monkeypatch.setattr("server.code.app.index.gitrepo.show_file", show)
    monkeypatch.setattr("server.code.app.index.denylist.load_denylist", no_denylist)
    monkeypatch.setattr("server.embedding.embedder.get_embedder", lambda: _Embedder())
    monkeypatch.setattr(settings, "code_ingest_round_files", 0)
    monkeypatch.setattr(code_backfill, "_record_manual_pass", record)
    monkeypatch.setattr(code_backfill, "_compact_after_bulk", compact)
    assert settings.inference_customer_header == ""
    switch_off("acme")
    args = argparse.Namespace(
        org="acme",
        namespace="code-a",
        phase="index",
        embed=True,
        allow_fake_embedder=True,
        limit=0,
        paths_file="",
        concurrency=1,
    )
    assert asyncio.run(code_backfill._backfill(args)) == 1
    assert sent == []  # nothing of the switched-off org's code went out
    assert "backfill_cursor:index" not in repo.state
    assert len(compacted) == 1 and [r["phase"] for r in recorded] == ["index"]


def test_a_switch_gone_stale_mid_reembed_stops_the_page_before_its_write() -> None:
    from server.core.errors import BackendUnavailableError
    from server.memory.app import reembed

    table = "conversation_embeddings"
    text = reembed._TABLES[table]["text"]

    class _Emb:
        model_name, dim = "m", 2

        async def embed_many(self, texts: list[str]) -> list[list[float]]:
            raise AssertionError("no model call on a stale switch")

    class _CH:
        rows: list[Any] = []

        async def insert(self, table: str, rows: list[Any], **_: Any) -> None:
            self.rows = rows

    ch = _CH()
    switch_stale()
    with pytest.raises(BackendUnavailableError):
        asyncio.run(
            reembed._rewrite_page(ch, _Emb(), table, [{"org": "acme", text: "a"}], key_only=False)
        )
    assert ch.rows == []


def test_the_period_digest_selects_no_identity_of_a_switched_off_org() -> None:
    from server.memory.app import period_digest as pd

    ch = _CH(
        [
            {"org": "acme", "namespace": "n", "agent": "a", "user_id": "u", "sessions": 9},
            {"org": "beta", "namespace": "n", "agent": "a", "user_id": "u", "sessions": 5},
        ]
    )
    switch_off("acme")
    rows = asyncio.run(pd._fetch_namespaces(ch, pd.PeriodDigestConfig()))
    assert [r["org"] for r in rows] == ["beta"] and _excludes(ch, "acme")
    assert "NOT has({off:Array(String)}, s.org)" in ch.sent[0][0]


def test_code_ingest_runs_no_repository_of_a_switched_off_org(monkeypatch) -> None:
    from server.code.app.index import ingest_pass as ip

    ran: list[str] = []

    async def run_repo(cfg: Any, result: Any, budget: Any) -> None:
        ran.append(cfg.org)

    async def nothing(*a: Any, **k: Any) -> Any:
        return set()

    synced: list[str] = []

    async def sync(org: str, ns: str, **_: Any) -> None:
        synced.append(ns)

    # acme's restricted page branch: its maintainers are synced though its org is off.
    page = {"source": "registry", "classification": "restricted", "repo": "Org/a"}
    repos = [
        SimpleNamespace(org="acme", namespace="code-a", label="a", **page),
        SimpleNamespace(org="beta", namespace="code-b", label="b", source="config"),
    ]
    monkeypatch.setattr("server.code.app.index.maintainers.sync_quietly", sync)

    async def no_rows(org: str, namespaces: list[str], **_: Any) -> dict[str, str]:
        return {}

    monkeypatch.setattr("server.identity.app.registration.classifications", no_rows)
    monkeypatch.setattr("server.code.domain.index.repos.configured_repos", lambda: repos)
    monkeypatch.setattr("server.code.app.index.repo_registry.refresh_db_repos", nothing)
    monkeypatch.setattr(ip, "_inactive_namespaces", nothing)
    monkeypatch.setattr(ip, "selected_repos", lambda org, ns: repos)
    monkeypatch.setattr(ip, "_without_unsandboxed", lambda repos, result: repos)
    monkeypatch.setattr(ip, "prefetch_mirrors", nothing)
    monkeypatch.setattr(ip, "ingest_oldest_first", nothing)
    monkeypatch.setattr(ip, "_run_repo", run_repo)
    switch_off("acme")
    result = ip.CodeIngestResult()
    asyncio.run(ip._run(result))
    assert ran == ["beta"] and not result.errors  # acme's watermark is never read
    assert synced == ["code-a"]


def test_tracker_links_scores_no_namespace_of_a_switched_off_org(monkeypatch) -> None:
    from server.tracker.workers import links as w

    scored: list[str] = []

    async def score(src: Any, repo: Any, org: str, ns: str, **_: Any) -> None:
        scored.append(org)

    async def refit(*a: Any, **k: Any) -> None:
        return None

    namespaces = {
        "t-a": SimpleNamespace(org="acme", restricted=False),
        "t-b": SimpleNamespace(org="beta", restricted=False),
    }
    monkeypatch.setattr(
        "server.tracker.domain.namespaces.configured_namespaces", lambda: namespaces
    )
    monkeypatch.setattr(w, "score_namespace", score)
    monkeypatch.setattr(w, "refit_dupcheck", refit)
    switch_off("acme")
    result = w.TrackerLinksResult()
    asyncio.run(w._run(result, repo=object(), source_factory=lambda org, ns: object()))
    assert scored == ["beta"] and result.namespaces == 1 and not result.errors


def test_the_retrieval_judge_drops_a_switched_off_orgs_sample(monkeypatch) -> None:
    from server.observability import retrieval_judge as rj

    sent: list[str] = []
    scored: list[str] = []

    class _Client:
        async def complete(self, *a: Any, **k: Any) -> Any:
            await customer.require()
            sent.append(customer.current())
            return SimpleNamespace(text='{"on_target": true, "useful": [1], "reason": "r"}')

    monkeypatch.setattr("server.llm.client.get_llm_client", lambda: _Client())
    monkeypatch.setattr(rj.settings, "retrieval_judge_model", "")
    monkeypatch.setattr(
        "server.observability.langfuse_scores.post_score", lambda **k: scored.append(k["name"])
    )
    item = {"org": "acme", "namespace": "n", "query": "q", "excerpts": ["x"], "trace_id": "t"}
    switch_off("acme")
    asyncio.run(rj._judge_one(item))  # the refusal drops the sample; nothing raises
    assert sent == [] and scored == []
    asyncio.run(rj._judge_one({**item, "org": "beta"}))
    assert sent == ["beta"] and scored
    sent.clear()
    scored.clear()
    switch_stale()
    asyncio.run(rj._judge_one({**item, "org": "beta"}))  # maybe off: dropped, nothing raises
    assert sent == [] and scored == []


def test_the_code_link_policy_leaves_a_switched_off_orgs_links_due(monkeypatch) -> None:
    from server.memory.app import code_link_policy as policy

    read: list[str] = []

    async def due(*a: Any, **k: Any) -> list[str]:
        read.append("policy_due")
        return []

    monkeypatch.setattr(policy.store, "policy_due", due)
    budget = SimpleNamespace(judgements=5)
    switch_off("acme")
    asyncio.run(policy.apply_policies("acme", "code", "head", object(), budget, object()))
    assert read == [] and budget.judgements == 5  # nothing read, judged or written
    asyncio.run(policy.apply_policies("beta", "code", "head", object(), budget, object()))
    assert read == ["policy_due"]


def test_the_conflict_resolver_reads_no_owed_row_of_a_switched_off_org(monkeypatch) -> None:
    from server.memory.store import conflict_resolver as queue
    from server.memory.workers import conflict_resolver as w

    capped: list[Any] = []

    async def namespaces(**k: Any) -> list[tuple[str, str]]:
        return [("acme", "n"), ("beta", "n")]

    async def under_cap(pairs: Any, **k: Any) -> dict[Any, bool]:
        return {pair: False for pair in pairs}

    async def owed(**k: Any) -> list[dict[str, Any]]:
        capped.append(k["capped"])
        return []

    monkeypatch.setattr(queue, "queued_namespaces", namespaces)
    monkeypatch.setattr("server.observability.budget.namespaces_over_cap", under_cap)
    monkeypatch.setattr(queue, "owed_rows", owed)
    monkeypatch.setattr(settings, "memory_conflict_resolver_recheck_batch", 0)
    switch_off("acme")
    asyncio.run(w._queued(w._Pass()))
    assert capped == [[("acme", "n")]]


async def test_a_stub_whose_org_is_switched_off_mid_pass_is_left_unwritten() -> None:
    from server.memory.workers import embedding as w

    class _E:
        async def embed_many(self, texts: list[str]) -> list[list[float]]:
            await customer.require()
            return [[1.0] for _ in texts]

    rows = [{"org": "acme", "content_excerpt": "a"}, {"org": "beta", "content_excerpt": "b"}]
    switch_off("acme")  # switched off after the stubs were picked
    out = await w._embed_rows(_E(), rows)
    assert out == [(None, w.HELD), ([1.0], "")]
