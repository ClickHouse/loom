"""A model call the switch holds mid-pass (`customer.HOLDS`: the org switched off
after the job selected its work, or the switch unread) is no failure: each
job leaves the row as it was, for the pass after the org is back, with no
error, no attempt, no backoff stamp and no verdict that reads as judged."""

from __future__ import annotations

import asyncio
import contextlib
from datetime import datetime
from types import SimpleNamespace
from typing import Any

import pytest

from server.llm import customer

HELD = [
    customer.ModelCallsDisabled("model calls are switched off for org 'acme'"),
    customer.SwitchUnread("the model-call switch could not be read"),
]
held = pytest.mark.parametrize("error", HELD, ids=lambda e: type(e).__name__)


def _raise(error: Exception) -> Any:
    async def call(*a: Any, **k: Any) -> Any:
        raise error

    return call


class _CH:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.rows = rows
        self.inserted: list[Any] = []

    async def query_all(self, *a: Any, **k: Any) -> list[dict[str, Any]]:
        return [dict(r) for r in self.rows]

    async def insert(self, table: str, rows: list[Any], **k: Any) -> None:
        self.inserted.extend(rows)


@held
def test_the_entity_extractor_leaves_a_held_row_without_a_failure(monkeypatch, error) -> None:
    from server.memory import store as memory_repo
    from server.memory.workers import entity_extractor as ee

    row = {
        "org": "acme",
        "namespace": "ns",
        "agent": "a",
        "user_id": "u",
        "session_id": "-",
        "memory_key": "k1",
        "memory_type": "semantic",
        "value": "User works with Alice at ClickHouse on the staging cluster.",
    }
    applied: list[Any] = []

    async def find(**k: Any) -> list[dict[str, Any]]:
        return [dict(row)]

    async def apply(items: Any, **k: Any) -> dict[str, int]:
        applied.extend(items)
        return {}

    async def under_cap(pairs: Any) -> dict[Any, bool]:
        return dict.fromkeys(pairs, False)

    monkeypatch.setattr(memory_repo, "find_unentitied_memories", find)
    monkeypatch.setattr(memory_repo, "apply_entities_batch", apply)
    monkeypatch.setattr(ee, "namespaces_over_cap", under_cap)
    monkeypatch.setattr(ee, "emit_worker_cost_event_for", lambda **kw: None)
    monkeypatch.setattr(ee, "_extract", _raise(error))
    result = asyncio.run(ee.run_pass(ee.EntityExtractorConfig()))
    assert result.failures == [] and result.skipped_switched_off == 1 and applied == []


@held
def test_profile_consolidation_counts_a_held_identity_as_no_error(monkeypatch, error) -> None:
    from server.memory.workers import profile_consolidation as w

    ch = _CH([{"org": "acme", "namespace": "n", "user_id": "u", "n": 9}])
    monkeypatch.setattr("server.core.ch.get_client", lambda: ch)
    monkeypatch.setattr(w, "_consolidate_identity", _raise(error))
    result = asyncio.run(w.run_pass(w.ProfileConsolidationConfig()))
    assert result.identities_scanned == 1 and result.errors == 0


@held
def test_the_titler_counts_a_held_thread_as_no_error(monkeypatch, error) -> None:
    from server.knowledge.store import conversations
    from server.knowledge.workers import conversation_titler as w

    async def candidates(**k: Any) -> list[dict[str, Any]]:
        return [{"org": "acme", "namespace": "n", "thread_id": "t"}]

    async def stored(keys: Any) -> dict[Any, Any]:
        return {}

    monkeypatch.setattr(conversations, "titler_candidates", candidates)
    monkeypatch.setattr(conversations, "thread_rows", stored)
    monkeypatch.setattr("server.llm.client.get_llm_client", lambda: SimpleNamespace(model="m"))
    monkeypatch.setattr(w, "_make_title", _raise(error))
    result = asyncio.run(w.run_pass(w.ConversationTitlerConfig()))
    assert result.errors == 0 and result.titled == 0


@held
def test_code_summaries_record_no_error_for_a_held_repository_or_file(monkeypatch, error) -> None:
    from server.code.workers import summary as w

    repos = [SimpleNamespace(org="acme", namespace="code-a")]
    monkeypatch.setattr("server.code.domain.index.repos.configured_repos", lambda: repos)
    monkeypatch.setattr(w, "_summarize_repo", _raise(error))
    result = w.CodeSummaryResult()
    asyncio.run(w._run(result))
    assert result.errors == []


@held
@pytest.mark.parametrize("concurrency", [1, 3])
def test_a_summary_pass_held_at_one_file_leaves_the_rest_of_the_repository(
    monkeypatch, error, concurrency
) -> None:
    from server.code.workers import summary as w
    from server.settings import settings

    read: list[str] = []
    paths = [f"f{i}.py" for i in range(20)]

    class _Repo:
        async def live_file_hashes(self, org: str, ns: str) -> dict[str, str]:
            return {p: "h" for p in paths}

        async def live_summary_rows(self, *a: Any, **k: Any) -> list[dict[str, Any]]:
            return []

        async def live_chunks_for_path(self, org: str, ns: str, path: str, model: str) -> list:
            read.append(path)
            return [{"start_line": 1, "scope": "f", "content": "def f(): pass"}]

        async def insert_rows(self, table: str, rows: list[Any]) -> None:
            raise AssertionError("no card is written for a held org")

    async def nothing(*a: Any, **k: Any) -> Any:
        return None

    async def in_order(repo: Any, org: str, ns: str, *a: Any) -> list[str]:
        return list(paths)

    async def under_cap(org: str, ns: str) -> bool:
        return False

    from server.code.store import index as code_index

    class _CodeRepo(code_index.CodeRepo):  # static row helpers stay real for later importers
        def __new__(cls) -> Any:
            return _Repo()

    monkeypatch.setattr(code_index, "CodeRepo", _CodeRepo)
    monkeypatch.setattr("server.observability.budget.namespace_over_cap", under_cap)
    monkeypatch.setattr(
        "server.embedding.embedder.get_embedder", lambda: SimpleNamespace(model_name="m")
    )
    monkeypatch.setattr(  # the real summarise call: the model client refuses
        "server.llm.client.get_llm_client_for_model",
        lambda model=None: SimpleNamespace(complete=_raise(error), model="llm"),
    )
    monkeypatch.setattr("server.workers.cost_event.emit_worker_cost_event_for", lambda **k: None)
    monkeypatch.setattr(w, "_dedupe_models", nothing)
    monkeypatch.setattr(w, "_candidate_order", in_order)
    monkeypatch.setattr(settings, "code_summary_concurrency", concurrency)
    result = w.CodeSummaryResult()
    with pytest.raises(type(error)):  # `_run` skips the repository on it
        asyncio.run(w._summarize_repo("acme", "code-a", result, cap=100))
    assert result.errors == [] and result.cards_written == 0
    assert len(read) <= 2 * concurrency  # not every remaining file of the repository


@held
@pytest.mark.parametrize(
    "paths, queued",
    [
        (["slow.py", "held.py", "later.py"], 3),  # under the queue's limit: the final gather
        (["a.py", "b.py", "c.py", "held.py", "later.py"], 4),  # at the limit: the wait
    ],
)
def test_no_card_task_outlives_a_held_file(error, paths, queued) -> None:
    from server.code.workers import summary as w

    started: list[asyncio.Task[Any]] = []

    async def one(path: str) -> None:
        started.append(asyncio.current_task())  # type: ignore[arg-type]
        if path == "held.py":
            raise error
        await asyncio.sleep(60)

    async def run() -> None:
        with pytest.raises(type(error)):
            await w._run_cards(paths, one, 2, lambda: False)
        assert len(started) == queued and all(t.done() for t in started)

    asyncio.run(run())


@held
def test_a_summary_card_whose_embed_is_held_is_a_stub_with_no_warning(error, caplog) -> None:
    from server.code.workers import summary as w

    embedder = SimpleNamespace(embed=_raise(error), model_name="m", dim=3)
    live = {"org": "acme", "namespace": "code-a", "scope_type": "file", "scope_path": "a.py"}
    row = asyncio.run(w._card_row(embedder, live, "what a.py does", "llm", "h"))
    assert row["summary"] == "what a.py does" and not row.get("embedding")
    assert not [r for r in caplog.records if "embed failed" in r.getMessage()]


@held
def test_tracker_embedding_records_no_error_for_a_held_namespace(monkeypatch, error) -> None:
    from server.tracker.workers import embed as w

    class _Source:
        async def upstream_embeddings_populated(self, org: str, ns: str) -> bool:
            return False

    namespaces = {"t-a": SimpleNamespace(org="acme")}
    monkeypatch.setattr(
        "server.tracker.domain.namespaces.configured_namespaces", lambda: namespaces
    )
    monkeypatch.setattr(w, "embed_namespace", _raise(error))
    result = w.TrackerEmbedResult()
    asyncio.run(
        w._run(
            result,
            repo=object(),
            source_factory=lambda o, n: _Source(),
            embedder=object(),
            state={},
        )
    )
    assert result.errors == [] and result.namespaces == 0


@held
def test_tracker_links_record_no_error_for_a_held_namespace(monkeypatch, error) -> None:
    from server.tracker.workers import links as w

    namespaces = {"t-a": SimpleNamespace(org="acme", restricted=False)}
    monkeypatch.setattr(
        "server.tracker.domain.namespaces.configured_namespaces", lambda: namespaces
    )
    monkeypatch.setattr(w, "score_namespace", _raise(error))
    result = w.TrackerLinksResult()
    asyncio.run(w._run(result, repo=object(), source_factory=lambda org, ns: object()))
    assert result.errors == [] and result.namespaces == 0


@held
def test_session_judging_and_contrasts_record_no_failure_when_held(monkeypatch, error) -> None:
    from server.memory.app import session_outcomes as so

    llm = SimpleNamespace(complete=_raise(error))
    result = so.DistillResult()
    ch = _CH([{"org": "acme", "namespace": "n", "session_id": "s", "summary_text": "x"}])
    asyncio.run(so.label_sessions(ch, llm, so.DistillConfig(), result))
    assert result.failures == [] and ch.inserted == []
    monkeypatch.setattr(so, "_contrast_pair", _raise(error))
    asyncio.run(so.distil_contrasts(ch, llm, so.DistillConfig(), result))
    assert result.failures == [] and result.reflections_written == 0


@held
def test_the_period_digest_records_no_failure_when_its_call_is_held(monkeypatch, error) -> None:
    from server.memory.app import period_digest as pd

    period = pd.Period(org="acme", namespace="n", agent="a", user_id="u", digests=({},))

    async def idents(ch: Any, cfg: Any) -> list[dict[str, Any]]:
        return [{"org": "acme", "namespace": "n", "agent": "a", "user_id": "u"}]

    async def nothing(*a: Any, **k: Any) -> Any:
        return set()

    monkeypatch.setattr(pd, "_fetch_namespaces", idents)
    monkeypatch.setattr(pd, "_fetch_digests", nothing)
    monkeypatch.setattr(pd, "_skipped_period_keys", nothing)
    monkeypatch.setattr(pd, "plan_periods", lambda *a, **k: ([period], 0))
    monkeypatch.setattr(pd, "build_period_prompt", lambda p: ("system", "user"))
    llm = SimpleNamespace(complete=_raise(error))
    result = asyncio.run(pd.run_period_digest(ch=object(), llm=llm, embedder=object()))
    assert result.failures == [] and result.periods_written == 0


@held
def test_the_conflict_resolvers_judge_lets_a_held_call_through(monkeypatch, error) -> None:
    from server.memory.app import conflict_resolver as resolver
    from server.memory.domain.conflict_resolution import Note

    monkeypatch.setattr(resolver, "_client", lambda: SimpleNamespace(complete=_raise(error)))
    new = Note(memory_id="m1", memory_key="k", value="the new value")
    peers = [Note(memory_id="p1", memory_key="k2", value="the old value", similarity=0.9)]
    with pytest.raises(type(error)):
        asyncio.run(resolver.judge(new, peers))


@held
def test_the_conflict_resolver_leaves_a_held_row_owed_with_no_attempt(monkeypatch, error) -> None:
    """Held work spends no attempt; an unread global switch stops the pass."""
    from server.memory.app import conflict_resolver as resolver
    from server.memory.workers import conflict_resolver as w

    async def over_cap(*a: Any, **k: Any) -> bool:
        return False

    async def flush() -> None:
        return None

    monkeypatch.setattr(resolver, "resolve_row", _raise(error))
    monkeypatch.setattr("server.observability.budget.namespace_over_cap", over_cap)
    monkeypatch.setattr(
        "server.observability.events.get_emitter", lambda: SimpleNamespace(flush_now=flush)
    )
    monkeypatch.setattr("server.workers.cost_event.emit_worker_cost_event_for", lambda **k: None)
    monkeypatch.setattr(w, "_state", w._State())
    row = {
        "org": "acme",
        "namespace": "n",
        "agent": "a",
        "user_id": "-",
        "session_id": "-",
        "memory_key": "k",
        "content_sha256": "sha",
    }

    async def one() -> Any:
        p = w._Pass()
        await w._resolve_one(p, row, "manual")
        return p

    p = asyncio.run(one())
    assert p.errors == [] and p.counts.errored == 0 and w._state.attempts == {}
    assert p.stopped == set()
    if isinstance(error, customer.GROUP_HOLDS):
        assert p.unavailable is None
        assert w._HELD.current() == frozenset({w._identity(row)})
    else:
        assert p.unavailable is error
        assert w._HELD.current() == frozenset()


@held
def test_code_ingest_records_no_error_for_a_held_repository_or_leg(monkeypatch, error) -> None:
    from server.code.app.index import ingest_pass as ip

    async def nothing(*a: Any, **k: Any) -> Any:
        return set()

    repos = [SimpleNamespace(org="acme", namespace="code-a", label="a", source="config")]
    monkeypatch.setattr("server.code.app.index.repo_registry.refresh_db_repos", nothing)
    monkeypatch.setattr(ip, "_inactive_namespaces", nothing)
    monkeypatch.setattr(ip, "selected_repos", lambda org, ns: repos)
    monkeypatch.setattr(ip, "_without_unsandboxed", lambda repos, result: repos)
    monkeypatch.setattr(ip, "prefetch_mirrors", nothing)
    monkeypatch.setattr(ip, "ingest_oldest_first", nothing)
    monkeypatch.setattr(ip, "_run_repo", _raise(error))
    result = ip.CodeIngestResult()
    asyncio.run(ip._run(result))
    assert not result.errors
    plan = SimpleNamespace(cfg=repos[0])
    budget = SimpleNamespace(exhausted=lambda: False)
    asyncio.run(ip._run_leg(result, budget, [plan], "walk", _raise(error)))
    assert not result.errors


@held
def test_a_held_commit_ends_the_entrys_pass_with_no_error(monkeypatch, error) -> None:
    from server.code.app.index import pass_stream as ps

    @contextlib.contextmanager
    def no_source(*a: Any) -> Any:
        yield

    async def zero(*a: Any) -> int:
        return 0

    monkeypatch.setattr(ps, "_slice_done", zero)
    monkeypatch.setattr(ps, "git_text_source", no_source)
    monkeypatch.setattr(ps, "_ingest_commit", _raise(error))
    monkeypatch.setattr("server.workers.cost_event.emit_worker_cost_event_for", lambda **k: None)
    result = ps.CodeIngestResult()
    cfg = SimpleNamespace(org="acme", namespace="code-a", git_dir="/x", label="a")
    stream = ps.CommitStream(cfg, object(), "h", "w", ["c1", "c2"], result)
    stream._ctx = {}
    assert asyncio.run(stream.step(clock=None)) is False
    assert not result.errors and stream.remaining == 0  # the watermark stays at "w"


@held
def test_a_held_link_judgement_leaves_the_link_due_with_no_backoff(monkeypatch, error) -> None:
    from server.memory.app import code_link_policy as policy
    from server.memory.domain import code_change

    async def yes(*a: Any, **k: Any) -> bool:
        return True

    async def texts(*a: Any, **k: Any) -> Any:
        return object()

    monkeypatch.setattr(policy, "_judgeable", lambda row: True)
    monkeypatch.setattr(policy.link_pass, "claim_of", lambda value: "the claim")
    monkeypatch.setattr(policy, "fingerprint_version", lambda: 1)
    monkeypatch.setattr(policy, "_texts", texts)
    monkeypatch.setattr(policy, "_unchanged", yes)
    monkeypatch.setattr(policy, "_judge", _raise(error))
    link = {
        "state": "changed",
        "qualified_name": "f",
        "policy_due": 1,
        "path": "p.py",
        "fingerprint_version": 1,
    }
    result = SimpleNamespace(errors=[], suggested=0)
    out = asyncio.run(
        policy._revise(
            "acme",
            {"value": "v"},
            [link],
            code_change.REVISE,
            "head",
            object(),
            SimpleNamespace(judgements=5),
            result,
        )
    )
    assert out is None and result.errors == []  # every link due, none failed


@held
def test_a_held_projection_retry_is_no_error_and_leaves_the_row_open(monkeypatch, error) -> None:
    from server.memory.app import projection_failures as pf

    failure = pf.ProjectionFailureRow(
        failure_id="f1",
        org="acme",
        namespace="n",
        agent="a",
        projection="memory_chunks",
        source_operation="memory.set",
        source_resource_id="",
        memory_keys=["k1"],
        error_type="RuntimeError",
        error_message="boom",
    )
    turns = [
        {"role": "user", "content": "We moved the staging cluster to us-east-2.", "user_id": "u"}
    ]

    class _Refusing:
        model_name, dim = "m", 2
        per_chunk = 0

        async def embed_many(self, texts: list[str]) -> list[list[float]]:
            raise error

        async def embed(self, text: str) -> list[float]:
            self.per_chunk += 1
            raise error

    ch = _CH(turns)
    resolved: list[Any] = []

    async def resolve(**k: Any) -> None:
        resolved.append(k)

    async def not_erasing(*a: Any, **k: Any) -> bool:
        return False

    refusing = _Refusing()
    monkeypatch.setattr("server.embedding.get_embedder", lambda: refusing)
    monkeypatch.setattr("server.memory.store.conflict_resolver.under_erasure", not_erasing)
    monkeypatch.setattr("server.core.ch.get_client", lambda: ch)
    monkeypatch.setattr(pf, "mark_projection_failures_resolved", resolve)
    # The real retry and chunk write: only the embedder refuses.
    (out,) = asyncio.run(pf.retry_failures([failure], ch=ch))
    assert not out.retried and not out.resolved and not out.error
    assert out.reason == "model_calls_held"
    assert resolved == [] and ch.inserted == []  # the row stays open, no chunk written
    assert refusing.per_chunk == 0  # a held batch is not retried chunk by chunk

    class _BatchDown(_Refusing):  # the batch fails on its own; each chunk is then held
        async def embed_many(self, texts: list[str]) -> list[list[float]]:
            raise RuntimeError("provider down")

    monkeypatch.setattr("server.embedding.get_embedder", lambda: _BatchDown())
    (out,) = asyncio.run(pf.retry_failures([failure], ch=ch))
    assert out.reason == "model_calls_held" and resolved == []


@held
def test_a_bulk_write_answers_a_held_conflict_check(monkeypatch, error) -> None:
    from server.memory.app import bulk_set
    from server.memory.app import conflicts as cf

    async def policy(*_a, **_k) -> str:
        return "auto_reinforce"

    async def threshold(*_a, **_k) -> float:
        return 0.85

    monkeypatch.setattr(cf, "check_on_commit", _raise(error))
    # the call's one policy and threshold read, without a ClickHouse
    monkeypatch.setattr(cf, "namespace_conflict_policy", policy)
    monkeypatch.setattr(cf, "_resolve_threshold", threshold)
    entry = SimpleNamespace(memory_type="semantic", memory_key="k", valid_at=None)
    put = SimpleNamespace(org="acme", namespace="n", agent="a", run_id="-", valid_at=None)
    with pytest.raises(type(error)):  # not "unchecked": the org's write is held
        asyncio.run(
            bulk_set._bulk_conflict_verdicts(
                put,
                [{}],
                [(entry, "v", None, None, None)],
                [(0, None, None, None)],
                [[0.1]],
                floors={},
            )
        )


@held
def test_the_duplicate_checks_judge_and_rerank_let_a_held_call_through(error) -> None:
    from server.tracker.app import dup_judge, dup_rerank

    llm = SimpleNamespace(complete=_raise(error))
    cands = [{"number": 7, "title": "t", "body": "b"}]
    with pytest.raises(type(error)):  # the request answers it, not "the fit's verdict stands"
        asyncio.run(dup_judge.judge(llm, "title", "body", cands))
    with pytest.raises(type(error)):  # not "the answer stands" without the rerank
        asyncio.run(dup_rerank.rerank(llm, "title", "body", cands, body_chars=200))


@held
def test_bisect_fusion_lets_a_held_embed_and_rerank_through(monkeypatch, error) -> None:
    from server.code.app import bisect_fusion as bf
    from server.tracker.domain import bisect as bi

    class _Refusing:
        async def embed(self, text: str) -> list[float]:
            raise error

    class _Window:
        async def window_prs(self, *a: Any, **k: Any) -> list[dict[str, Any]]:
            return [{"pr_number": 1}]

    monkeypatch.setattr("server.embedding.embedder.get_embedder", lambda: _Refusing())
    with pytest.raises(type(error)):  # not "window_unavailable" with no vector leg
        asyncio.run(
            bf._read_window(
                _Window(), "acme", "t-a", versions=[], bounds=(None, None), text="crash"
            )
        )
    with pytest.raises(type(error)):  # nor the weak path's wider window: `([], {})`
        asyncio.run(
            bf.wide_extras(
                _Window(),
                "acme",
                "t-a",
                good="",
                bad="",
                master=False,
                versions=[],
                report_at=datetime(2026, 9, 1),
                in_window=set(),
                text="crash",
                paths=[],
            )
        )
    monkeypatch.setattr(
        "server.llm.client.get_llm_client_for_model",
        lambda model: SimpleNamespace(complete=_raise(error)),
    )
    pool = [bi.Candidate(pr_number=1, title="t", author="a", merged_at="", minor_version="")]
    with pytest.raises(type(error)):  # not "failed" with the fused order kept
        asyncio.run(bf.rerank_stage(pool, text="crash", force=True))


@held
def test_code_ingest_lets_a_held_embed_or_description_through(error) -> None:
    from server.code.app.index import describe, ingest

    class _Refusing:
        calls = 0

        async def embed_many(self, texts: list[str]) -> list[list[float]]:
            self.calls += 1
            raise error

    refusing = _Refusing()
    with pytest.raises(type(error)):  # not a stub with a recorded failure
        asyncio.run(ingest._embed_texts(["def f(): pass"], refusing))
    assert refusing.calls == 1  # a held batch is not retried text by text

    class _Short:  # the batch answers short: the per-text fallback is held
        calls = 0

        async def embed_many(self, texts: list[str]) -> list[list[float]]:
            self.calls += 1
            if self.calls == 1:
                return []
            raise error

    with pytest.raises(type(error)):
        asyncio.run(ingest._embed_texts(["def f(): pass"], _Short()))
    chunk = SimpleNamespace(scope="f", kind="function", content="def f(): pass", path="a.py")
    llm = SimpleNamespace(complete=_raise(error))
    with pytest.raises(type(error)):  # not an empty description
        asyncio.run(describe._describe_chunk.__wrapped__(llm, chunk))


@held
def test_a_bootstrap_slice_held_at_a_file_leaves_its_cursor(monkeypatch, error) -> None:
    from server.code.app.index import bootstrap as bs
    from server.settings import settings

    class _Repo:
        state: dict[str, str] = {}

        async def get_state(self, org: str, ns: str, key: str) -> str:
            return ""

        async def set_state(self, org: str, ns: str, key: str, value: str, **k: Any) -> None:
            self.state[key] = value

    async def tree(*a: Any, **k: Any) -> list[str]:
        return ["a.py", "b.py"]

    async def show(*a: Any, **k: Any) -> bytes:
        return b"def f(): pass\n"

    async def no_denylist(*a: Any, **k: Any) -> None:
        return None

    monkeypatch.setattr("server.code.app.index.gitrepo.ls_tree", tree)
    monkeypatch.setattr("server.code.app.index.gitrepo.show_file", show)
    monkeypatch.setattr("server.code.app.index.denylist.load_denylist", no_denylist)
    monkeypatch.setattr("server.code.app.index.ingest_headers.headers_first", lambda p: p)
    monkeypatch.setattr("server.code.app.index.ingest.ingest_file", _raise(error))
    monkeypatch.setattr(
        "server.embedding.embedder.get_embedder", lambda: SimpleNamespace(model_name="m")
    )
    monkeypatch.setattr(bs, "bootstrap_embedder", lambda: object())
    monkeypatch.setattr(settings, "code_ingest_round_files", 0)
    bs.reset_embed_breaker()
    cfg = SimpleNamespace(org="acme", namespace="code-a", git_dir="/g", path_prefixes=[], label="r")
    repo = _Repo()
    with pytest.raises(type(error)):  # the leg's held clause skips the repository
        asyncio.run(bs.index_slice(cfg, repo, "sha"))
    assert bs.CURSOR_KEY not in repo.state  # the slice starts over once the org is back
    assert not bs.embed_breaker_open()  # a held call is no provider outage


class _IngestRepo:
    """Enough of the code repository for the real `heal` and `ingest_file`."""

    def __init__(self) -> None:
        self.state: dict[str, str] = {}
        self.inserts: list[str] = []

    async def all_state(self, org: str, ns: str) -> dict[str, Any]:
        return {}

    async def set_state(self, org: str, ns: str, key: str, value: str, **k: Any) -> None:
        self.state[key] = value

    async def live_file_hashes(self, org: str, ns: str) -> dict[str, str]:
        return {"a.py": "old", "b.py": "old"}

    async def insert_rows(self, table: str, rows: list[dict[str, Any]]) -> None:
        self.inserts.append(table)

    async def name_candidates(self, *a: Any, **k: Any) -> dict[str, Any]:
        return {}

    def __getattr__(self, name: str) -> Any:
        if not name.startswith("live_") and name != "chunks_full_by_ids":
            raise AttributeError(name)

        async def nothing(*a: Any, **k: Any) -> Any:
            return {} if name.endswith("_for_paths") else []

        return nothing


def _heal_through_the_real_ingest(monkeypatch: pytest.MonkeyPatch, error: Exception) -> None:
    """The heals run the real `heal` and `ingest_file` over two files; only the
    embedder refuses."""
    from server.settings import settings

    class _Refusing:
        model_name, dim = "m", 4

        async def embed_many(self, texts: list[str]) -> list[list[float]]:
            raise error

    async def show(*a: Any, **k: Any) -> bytes:
        return b"def f():\n    return 1\n"

    async def no_denylist(*a: Any, **k: Any) -> None:
        return None

    monkeypatch.setattr("server.code.app.index.gitrepo.show_file", show)
    monkeypatch.setattr("server.code.app.index.denylist.load_denylist", no_denylist)
    monkeypatch.setattr("server.embedding.embedder.get_embedder", lambda: _Refusing())
    monkeypatch.setattr(settings, "code_ingest_round_files", 0)


@held
def test_a_held_rechunk_heal_leaves_the_rechunk_cursor(monkeypatch, error) -> None:
    from server.code.app.index import ingest_pass as ip
    from server.code.domain.extract.extractor_stamp import STAGES
    from server.settings import settings

    _heal_through_the_real_ingest(monkeypatch, error)
    monkeypatch.setattr(settings, "code_extractor_rechunk_enabled", True)
    monkeypatch.setattr(ip, "_stage_plan", lambda *a: ("heal", frozenset(STAGES), {"cursor": 0}))
    monkeypatch.setattr(ip, "_heal_languages", lambda *a: set())
    monkeypatch.setattr(ip, "_scope_paths", lambda paths, langs: list(paths))
    cfg = SimpleNamespace(
        org="acme", namespace="code-a", git_dir="/g", label="r", embed_reuse_from=""
    )
    repo, result = _IngestRepo(), ip.CodeIngestResult()
    with pytest.raises(type(error)):  # not a per-file error the cursor moves past
        asyncio.run(ip._maybe_rechunk(repo, cfg, "sha", result))
    assert "extractor_stamp" not in repo.state  # the cursor stays at the held files
    assert "code_chunks" not in repo.inserts and result.errors == []


@held
def test_a_held_reconcile_heal_records_no_reconcile(monkeypatch, error) -> None:
    from server.code.app.index import ingest_pass as ip
    from server.code.app.index import reconcile
    from server.settings import settings

    _heal_through_the_real_ingest(monkeypatch, error)

    async def stale(*a: Any, **k: Any) -> Any:
        return SimpleNamespace(stale=["a.py", "b.py"], stale_count=2)

    async def scope(*a: Any, **k: Any) -> None:
        return None

    monkeypatch.setattr(settings, "code_reconcile_enabled", True)
    monkeypatch.setattr(settings, "code_ingest_dependents_enabled", False)
    monkeypatch.setattr(reconcile, "find_stale_paths", stale)
    monkeypatch.setattr(ip, "_scope_of", scope)
    cfg = SimpleNamespace(
        org="acme", namespace="code-a", git_dir="/g", label="r", embed_reuse_from=""
    )
    repo, result = _IngestRepo(), ip.CodeIngestResult()
    with pytest.raises(type(error)):  # not a heal error counted in a recorded reconcile
        asyncio.run(ip._maybe_reconcile(repo, cfg, "sha", result))
    assert "reconcile" not in repo.state and result.errors == []


async def _held_and_a_slow_peer(error: Exception, run: Any) -> bool:
    """Runs `run(one)` where one file is held and its peer would still be
    running; returns whether the peer was cancelled before `run` raised."""
    peer = asyncio.Event()
    cancelled = False

    async def one(reads: Any, path: str, source: bytes) -> None:
        nonlocal cancelled
        if path == "a.py":
            await asyncio.sleep(0)
            raise error
        try:
            await peer.wait()  # never set: only a cancel ends it
        except asyncio.CancelledError:
            cancelled = True
            raise

    with pytest.raises(type(error)):
        await run(one)
    return cancelled


@held
def test_a_held_file_cancels_its_peers_in_a_heal_round(monkeypatch, error) -> None:
    from server.code.app.index import reconcile
    from server.settings import settings

    monkeypatch.setattr(settings, "code_ingest_round_files", 0)

    async def show(*a: Any, **k: Any) -> bytes:
        return b"x = 1\n"

    async def no_denylist(*a: Any, **k: Any) -> None:
        return None

    monkeypatch.setattr("server.code.app.index.denylist.load_denylist", no_denylist)
    cfg = SimpleNamespace(org="acme", namespace="code-a", git_dir="/g", embed_reuse_from="")

    def run(one: Any) -> Any:
        async def ingest_file(reads: Any, org: str, ns: str, *, path: str, **k: Any) -> Any:
            return await one(reads, path, b"")

        return reconcile.heal(
            _IngestRepo(),
            cfg,
            "sha",
            ["a.py", "b.py"],
            embedder=SimpleNamespace(model_name="m"),
            ingest_file=ingest_file,
            show_file=show,
        )

    assert asyncio.run(_held_and_a_slow_peer(error, run))


@held
def test_a_held_file_cancels_its_peers_in_a_bootstrap_round(monkeypatch, error) -> None:
    from server.code.app.index import bootstrap as bs
    from server.settings import settings

    monkeypatch.setattr(settings, "code_ingest_round_files", 0)

    async def source(path: str) -> tuple[str, bytes]:
        return path, b"x = 1\n"

    def run(one: Any) -> Any:
        return bs._slice_rounds(
            _IngestRepo(),
            "acme",
            "code-a",
            ["a.py", "b.py"],
            source=source,
            one=one,
            denylist=None,
            stop_after=None,
        )

    assert asyncio.run(_held_and_a_slow_peer(error, run))
