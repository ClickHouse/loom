"""A model call the switch holds mid-pass (`customer.HOLDS`: the org switched off
after the job selected its work, or the switch unread) is no failure: each
job leaves the row as it was, for the pass after the org is back, with no
error, no attempt, no backoff stamp and no verdict that reads as judged."""

from __future__ import annotations

import asyncio
import contextlib
from datetime import UTC, datetime
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
    w._note_file_error(result, "a.py", error)
    assert result.errors == []  # a file whose call was held waits too
    w._note_file_error(result, "a.py", RuntimeError("boom"))
    assert result.errors == ["a.py: RuntimeError: boom"]


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
def test_a_held_dupcheck_refit_is_no_failed_refit(monkeypatch, error) -> None:
    from server.tracker.app import dupcheck_fit as fit

    async def none(*a: Any, **k: Any) -> Any:
        return None

    async def gold(*a: Any, **k: Any) -> list[dict[str, Any]]:
        return [{"number": n, "label": 1} for n in range(fit.MIN_POSITIVES)]

    monkeypatch.setattr(fit, "enabled", lambda: True)
    monkeypatch.setattr(fit, "stored_fit", none)
    monkeypatch.setattr(fit, "gather_gold", gold)
    monkeypatch.setattr(fit, "replay_query", _raise(error))  # the replay embeds the query
    monkeypatch.setattr(fit, "PACE_S", 0)
    with pytest.raises(type(error)):  # not a `failed` outcome: the links pass skips the org
        asyncio.run(fit.refit_namespace(object(), object(), "acme", "t-a", force=True))


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
    monkeypatch.setattr(pd, "_existing_period_keys", nothing)
    monkeypatch.setattr(pd, "plan_periods", lambda *a, **k: ([period], 0))
    monkeypatch.setattr(pd, "build_period_prompt", lambda p: ("system", "user"))
    llm = SimpleNamespace(complete=_raise(error))
    result = asyncio.run(pd.run_period_digest(ch=object(), llm=llm, embedder=object()))
    assert result.failures == [] and result.periods_written == 0


@held
def test_a_held_deferred_judge_lands_no_verdict_and_stamps_nothing(monkeypatch, error) -> None:
    from server.memory.app import conflict_deferred as cd
    from server.memory.app import conflicts as cf

    stamped: list[Any] = []
    drops: list[str] = []

    async def stamp(*a: Any, **k: Any) -> None:
        stamped.append(a)

    monkeypatch.setattr(cf, "settle_deferred_judgement", _raise(error))
    monkeypatch.setattr(cd, "stamp_checked", stamp)
    monkeypatch.setattr(cd, "emit_settle_row", lambda *a, **k: None)
    monkeypatch.setattr(
        "server.memory.app.drops.record_drop",
        lambda org, ns, stage, reason, **k: drops.append(reason),
    )
    ident = SimpleNamespace(org="acme", namespace="ns", memory_key="k")
    verdict = asyncio.run(
        cd.settle(ident, None, memory_id="m1", version=1, created_at=datetime.now(UTC))
    )
    assert verdict == cd.HELD and stamped == [] and drops == [cd.HELD]


@held
def test_the_conflict_judge_lets_a_held_call_through(error) -> None:
    from server.memory.app import conflict_judge

    llm = SimpleNamespace(complete=_raise(error))
    peers = [SimpleNamespace(memory_id="p1", value="the old value")]
    with pytest.raises(type(error)):
        asyncio.run(conflict_judge.judge(llm, "the new value", peers))


@held
def test_the_conflict_sweep_records_no_error_for_a_held_mark(monkeypatch, error) -> None:
    from server.memory.store import conflict_marks
    from server.memory.workers import conflict_sweep as w

    async def marks(**k: Any) -> list[dict[str, Any]]:
        return [{"org": "acme", "namespace": "n", "memory_key": "k"}]

    monkeypatch.setattr(conflict_marks, "stale_pending_marks", marks)
    monkeypatch.setattr(w, "rejudge", _raise(error))
    outcome = asyncio.run(w.run_pass(ch=object()))
    assert outcome["errors"] == 0 and outcome["verdicts"] == {}


@held
def test_code_ingest_records_no_error_for_a_held_repository_or_leg(monkeypatch, error) -> None:
    from server.code.app.index import ingest_pass as ip

    async def nothing(*a: Any, **k: Any) -> Any:
        return set()

    repos = [SimpleNamespace(org="acme", namespace="code-a", label="a")]
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
    monkeypatch.setattr(pf, "_retry_memory_chunks", _raise(error))
    out = asyncio.run(pf.retry_projection_failure(failure, ch=object()))
    assert not out.retried and not out.resolved and not out.error
    assert out.reason == "model_calls_held"


@held
def test_a_bulk_write_answers_a_held_conflict_check(monkeypatch, error) -> None:
    from server.memory.app import bulk_set
    from server.memory.app import conflicts as cf

    monkeypatch.setattr(cf, "check_on_commit", _raise(error))
    entry = SimpleNamespace(memory_type="semantic", memory_key="k", valid_at=None)
    put = SimpleNamespace(org="acme", namespace="n", agent="a", run_id="-", valid_at=None)
    with pytest.raises(type(error)):  # not "unchecked": the org's write is held
        asyncio.run(
            bulk_set._bulk_conflict_verdicts(
                put, [{}], [(entry, "v", None, None, None)], [(0, None, None, None)], [[0.1]]
            )
        )
