"""Every background job leaves an org whose model calls are switched off out
when it picks its work: in the query, so the org's rows spend no attempt and
fill no batch ahead of another org's, which still runs."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest

from server.llm import customer


def _off(*orgs: str) -> None:
    customer._reset_for_tests(frozenset(orgs))


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
    _off("acme")
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
    _off("acme")
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

    async def scan(*, limit: int, exclude_orgs: frozenset[str]) -> list[Any]:
        seen.append(exclude_orgs)
        return []

    monkeypatch.setattr(projection_failures, "scan_active_projection_failures", scan)
    _off("acme")
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
    _off("acme")
    asyncio.run(w.run_pass(w.ConversationTitlerConfig()))
    assert seen == [frozenset({"acme"})]


def test_reflection_distillation_judges_and_contrasts_no_switched_off_org() -> None:
    from server.memory.app import session_outcomes as so

    ch = _CH()
    cfg = so.DistillConfig()
    result = so.DistillResult()
    _off("acme")
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
    monkeypatch.setattr("server.code.store.index.CodeRepo", lambda: object())
    monkeypatch.setattr("server.embedding.embedder.get_embedder", lambda: object())
    _off("acme")
    asyncio.run(w._run(w.CodeEmbedRecoveryResult()))
    assert scanned and all(r == [("beta", "code-b")] for r in scanned)


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
    _off("acme")
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
    _off("acme")
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
        _off("acme")
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
    _off()
    result = asyncio.run(w.run_pass(w.EmbeddingWorkerConfig()))
    assert {r["org"] for r in written} == {"beta"}
    assert all(r["embedding_dim"] == 2 and r["embed_attempts"] == 2 for r in written)
    assert result.embedded_count == 1 and result.failed_count == 0


def test_the_period_digest_selects_no_identity_of_a_switched_off_org() -> None:
    from server.memory.app import period_digest as pd

    ch = _CH(
        [
            {"org": "acme", "namespace": "n", "agent": "a", "user_id": "u", "sessions": 9},
            {"org": "beta", "namespace": "n", "agent": "a", "user_id": "u", "sessions": 5},
        ]
    )
    _off("acme")
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

    repos = [
        SimpleNamespace(org="acme", namespace="code-a", label="a"),
        SimpleNamespace(org="beta", namespace="code-b", label="b"),
    ]
    monkeypatch.setattr("server.code.app.index.repo_registry.refresh_db_repos", nothing)
    monkeypatch.setattr(ip, "_inactive_namespaces", nothing)
    monkeypatch.setattr(ip, "selected_repos", lambda org, ns: repos)
    monkeypatch.setattr(ip, "_without_unsandboxed", lambda repos, result: repos)
    monkeypatch.setattr(ip, "prefetch_mirrors", nothing)
    monkeypatch.setattr(ip, "ingest_oldest_first", nothing)
    monkeypatch.setattr(ip, "_run_repo", run_repo)
    _off("acme")
    result = ip.CodeIngestResult()
    asyncio.run(ip._run(result))
    assert ran == ["beta"] and not result.errors  # acme's watermark is never read


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
    _off("acme")
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
    _off("acme")
    asyncio.run(rj._judge_one(item))  # the refusal drops the sample; nothing raises
    assert sent == [] and scored == []
    asyncio.run(rj._judge_one({**item, "org": "beta"}))
    assert sent == ["beta"] and scored


def test_the_code_link_policy_leaves_a_switched_off_orgs_links_due(monkeypatch) -> None:
    from server.memory.app import code_link_policy as policy

    read: list[str] = []

    async def due(*a: Any, **k: Any) -> list[str]:
        read.append("policy_due")
        return []

    monkeypatch.setattr(policy.store, "policy_due", due)
    budget = SimpleNamespace(judgements=5)
    _off("acme")
    asyncio.run(policy.apply_policies("acme", "code", "head", object(), budget, object()))
    assert read == [] and budget.judgements == 5  # nothing read, judged or written
    asyncio.run(policy.apply_policies("beta", "code", "head", object(), budget, object()))
    assert read == ["policy_due"]


def test_the_conflict_sweep_rejudges_no_mark_of_a_switched_off_org() -> None:
    from server.memory.store import conflict_marks
    from server.memory.workers import conflict_sweep as w

    ch = _CH()
    asyncio.run(
        conflict_marks.stale_pending_marks(
            grace_s=1, lookback_s=3600, limit=5, exclude_orgs=frozenset({"acme"}), ch=ch
        )
    )
    assert _excludes(ch, "acme")
    ch = _CH()
    _off("acme")
    asyncio.run(w.run_pass(ch=ch))
    assert _excludes(ch, "acme")


async def test_a_stub_whose_org_is_switched_off_mid_pass_is_left_unwritten() -> None:
    from server.memory.workers import embedding as w

    class _E:
        async def embed_many(self, texts: list[str]) -> list[list[float]]:
            await customer.require()
            return [[1.0] for _ in texts]

    rows = [{"org": "acme", "content_excerpt": "a"}, {"org": "beta", "content_excerpt": "b"}]
    _off("acme")  # switched off after the stubs were picked
    out = await w._embed_rows(_E(), rows)
    assert out == [(None, w.SWITCHED_OFF), ([1.0], "")]
