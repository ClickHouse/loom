"""A background job's model call is for the writer of the row it works on, and for
the agent whose namespace holds the row only when the row records no writer: a
member writing into an agent's namespace, an agent writing into another's or into
a shared namespace is billed as itself. A pipeline's own actor is no writer."""

from __future__ import annotations

import ast
import asyncio
import dataclasses
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from server.llm import customer
from server.memory.app import model_attribution
from server.memory.domain import provenance, trust
from server.settings import settings
from tests.unit.switch_state import switch_off
from tests.unit.test_model_call_identity import AGENT_NS, _Directory, _stub, directory  # noqa: F401

AGENT_8 = "sa:cell-t1:agent-8"
NS_8 = "cell-t1-agent-8"
SHARED = "t-1-shared"
_TEMPLATES = '{"X-Tenant": "{tenant}", "X-Agent": "{agent}", "X-Actor": "{email}"}'

# Whom each writer's calls name, as these headers render them.
AS_AGENT_7 = {"X-Tenant": "t1", "X-Agent": "agent-7"}
AS_AGENT_8 = {"X-Tenant": "t1", "X-Agent": "agent-8"}
AS_ALICE = {"X-Actor": "alice@example.com"}


@pytest.fixture
def writers(directory: _Directory, monkeypatch: pytest.MonkeyPatch) -> _Directory:  # noqa: F811
    monkeypatch.setattr(settings, "inference_identity_headers_json", _TEMPLATES)
    directory.owners[("acme", NS_8)] = AGENT_8
    directory.owners[("acme", SHARED)] = ""  # registered by an admin: no agent's own
    return directory


async def _sent() -> dict[str, str]:
    return customer.headers(await customer.require())


def test_a_pipelines_own_actor_names_no_principal() -> None:
    assert trust.subject_of(f"process:{AGENT_8}") == AGENT_8
    assert trust.subject_of("human:sso-alice") == "sso-alice"
    assert trust.subject_of("process:ci-bot") == "ci-bot"  # a token's own subject
    for pipeline in (provenance.EXTRACTION_WORKER_ACTOR, provenance.cli_actor("import"), ""):
        assert trust.subject_of(pipeline) == ""
    assert trust.subject_of("sa:cell-t1:agent-8") == ""  # no kind: not an actor string


def test_every_process_actor_the_server_writes_is_a_registered_pipeline() -> None:
    root = Path(__file__).resolve().parents[2] / "server"
    found = set()
    for path in root.rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                if node.value.startswith("process:") and node.value != trust.PROCESS_PREFIX:
                    found.add(node.value)
    assert found, "the scan found no pipeline actor"
    assert {a for a in found if not provenance.is_pipeline_actor(a)} == set()


def test_the_embedding_worker_calls_for_each_stubs_writer(writers: _Directory) -> None:
    from server.memory.workers import embedding

    sent: dict[str, dict[str, str]] = {}

    class _Embedder:
        model_name, dim = "fake", 2

        async def embed_many(self, texts: list[str]) -> list[list[float]]:
            for t in texts:
                sent[t] = await _sent()
            return [[1.0, 0.0] for _ in texts]

    rows = [
        _stub(AGENT_NS, "by-alice", "a member in agent-7's namespace"),
        _stub(AGENT_NS, "by-8", "agent-8 in agent-7's namespace"),
        _stub(SHARED, "shared-by-8", "agent-8 in the shared namespace"),
        _stub(AGENT_NS, "by-nobody", "no writer recorded"),
        _stub(AGENT_NS, "by-pipeline", "a pipeline's row"),
    ]
    for key, actor in (
        ("by-alice", "human:sso-alice"),
        ("by-8", f"process:{AGENT_8}"),
        ("shared-by-8", f"process:{AGENT_8}"),
        ("by-pipeline", provenance.EXTRACTION_WORKER_ACTOR),
    ):
        row = next(r for r in rows if r["memory_key"] == key)
        ident = tuple(str(row[c]) for c in model_attribution.IDENTITY_COLUMNS)
        writers.writers[ident] = actor  # type: ignore[index]
    asyncio.run(embedding._embed_rows(_Embedder(), rows))
    assert sent == {
        "a member in agent-7's namespace": AS_ALICE,
        "agent-8 in agent-7's namespace": AS_AGENT_8,
        "agent-8 in the shared namespace": AS_AGENT_8,
        "no writer recorded": AS_AGENT_7,  # the namespace's agent: no writer
        "a pipeline's row": AS_AGENT_7,  # a pipeline is no writer: the namespace's agent
    }


def test_observe_extract_calls_for_the_agent_that_recorded_the_session(
    writers: _Directory, monkeypatch: pytest.MonkeyPatch
) -> None:
    from server.memory.workers import observe_extract

    seen: list[dict[str, str]] = []
    session = {
        "org": "acme",
        "namespace": AGENT_NS,
        "agent": "a",
        "user_id": "u1",
        "session_id": "s1",
    }

    async def sessions(cfg: Any, ch: Any) -> list[dict[str, Any]]:
        return [session]

    async def observations(session: Any, cfg: Any, ch: Any) -> list[Any]:
        return [
            SimpleNamespace(actor=provenance.EXTRACTION_WORKER_ACTOR),
            SimpleNamespace(actor=f"process:{AGENT_8}"),
            SimpleNamespace(actor=provenance.cli_actor("observe")),
        ]

    async def distill(session: Any, observations: Any) -> Any:
        seen.append(await _sent())
        return SimpleNamespace(written=1, observations=1, events=0, extra_failures=[])

    monkeypatch.setattr("server.core.ch.get_client", lambda: None)
    monkeypatch.setattr(observe_extract, "find_ready_sessions", sessions)
    monkeypatch.setattr(observe_extract, "_load_session_observations", observations)
    monkeypatch.setattr(observe_extract, "_distill_session", distill)
    asyncio.run(observe_extract.run_pass())
    assert seen == [AS_AGENT_8]


def test_the_conflict_sweep_settles_for_the_writes_writer(monkeypatch: pytest.MonkeyPatch) -> None:
    import server.memory.store as memory_repo
    from server.memory.app import conflict_deferred as cd
    from server.memory.app import conflicts as cf
    from server.memory.workers import conflict_sweep

    key = ("acme", AGENT_NS, "a", "u1", "-", "k1")
    mem = SimpleNamespace(
        status="active",
        content_sha256="",
        memory_type="semantic",
        run_id="-",
        memory_id="m1",
        valid_at=None,
        actor=f"process:{AGENT_8}",
    )
    settled: list[str] = []

    async def rows(keys: Any, **_: Any) -> dict[Any, Any]:
        return {key: mem}

    async def check(**_: Any) -> Any:
        return SimpleNamespace(verdict=cd.PENDING, pending_judge=object())

    async def settle(*_: Any, subject: str = "", **__: Any) -> str:
        settled.append(subject)
        return "clear"

    monkeypatch.setattr(memory_repo, "memory_rows_by_identity_keys", rows)
    monkeypatch.setattr(memory_repo, "stored_value_text", lambda m: "v")
    monkeypatch.setattr(cf, "check_on_commit", check)
    monkeypatch.setattr(cd, "settle", settle)
    r = dict(
        zip(("org", "namespace", "agent", "user_id", "session_id", "memory_key"), key, strict=True)
    )
    asyncio.run(conflict_sweep.rejudge({**r, "version": 1, "created_at": None}, ch=object()))  # type: ignore[arg-type]
    assert settled == [AGENT_8]


def test_a_settle_calls_for_the_subject_it_is_given(writers: _Directory) -> None:
    from datetime import UTC, datetime

    from server.memory.app import conflict_deferred as cd
    from server.memory.app import conflicts as cf

    seen: list[dict[str, str]] = []

    async def judge(pj: Any) -> Any:
        seen.append(await _sent())
        return None

    async def moved(*a: Any, **k: Any) -> bool:
        return False

    ident = SimpleNamespace(org="acme", namespace=AGENT_NS, memory_key="k", user_id="u1")
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(cf, "settle_deferred_judgement", judge)
        mp.setattr(cd, "still_current", moved)
        mp.setattr(cd, "emit_settle_row", lambda *a, **k: None)
        asyncio.run(
            cd.settle(
                ident,
                None,
                memory_id="m1",
                version=1,
                created_at=datetime.now(UTC),
                subject=AGENT_8,
            )
        )
    assert seen == [AS_AGENT_8]


def test_the_profile_fold_calls_for_its_facts_newest_writer(
    writers: _Directory, monkeypatch
) -> None:
    from server.llm.fake import FakeLLMClient
    from server.memory.app import profile
    from server.memory.workers import profile_consolidation as pc

    seen: list[dict[str, str]] = []

    class _CH:
        async def query_all(self, sql: str, params: Any = None) -> list[dict[str, Any]]:
            return [{"value": "prefers tea", "since": "2026-01-01", "memory_id": "fact-1"}]

    class _LLM(FakeLLMClient):
        async def complete(self, *a: Any, **k: Any) -> Any:
            seen.append(await _sent())
            return await super().complete(*a, **k)

    async def write_section(**kw: Any) -> None:
        return None

    monkeypatch.setattr("server.llm.client.get_llm_client", lambda: _LLM())
    monkeypatch.setattr(pc, "emit_worker_cost_event_for", lambda **kw: None)
    monkeypatch.setattr(profile, "write_folded_section", write_section)
    row = {"org": "acme", "namespace": AGENT_NS, "user_id": "u1", "writer": f"process:{AGENT_8}"}
    assert asyncio.run(
        pc._consolidate_identity(_CH(), row, pc.ProfileConsolidationConfig(model=""))
    )
    assert seen == [AS_AGENT_8]


def test_a_reflection_judges_a_session_for_its_writer(writers: _Directory, monkeypatch) -> None:
    from server.memory.app import session_outcomes as so

    seen: list[dict[str, str]] = []
    summary = {
        "org": "acme",
        "namespace": SHARED,
        "agent": "a",
        "user_id": "u1",
        "session_id": "s1",
        "summary_text": "did things",
        "key_facts": [],
    }

    class _CH:
        async def query_all(self, sql: str, params: Any = None) -> list[dict[str, Any]]:
            return [summary]

    async def session_writers(keys: Any, *, ch: Any = None) -> dict[Any, str]:
        assert list(keys) == [("acme", SHARED, "a", "u1", "s1")]
        return {("acme", SHARED, "a", "u1", "s1"): f"process:{AGENT_8}"}

    class _LLM:
        async def complete(self, *a: Any, **k: Any) -> Any:
            seen.append(await _sent())
            return SimpleNamespace(text='{"outcome": "success", "confidence": 0.9, "reason": "r"}')

    async def insert(rows: Any, **_: Any) -> None:
        return None

    monkeypatch.setattr(so, "session_writers", session_writers)
    monkeypatch.setattr(so, "insert_outcomes", insert)
    asyncio.run(so.label_sessions(_CH(), _LLM(), so.DistillConfig(), so.DistillResult()))
    assert seen == [AS_AGENT_8]


def test_a_period_digest_calls_for_its_sessions_newest_writer(
    writers: _Directory, monkeypatch
) -> None:
    from server.memory.app import period_digest as pd

    async def session_writers(keys: Any, *, ch: Any = None) -> dict[Any, str]:
        return {("acme", SHARED, "a", "u1", "s2"): f"process:{AGENT_8}"}

    monkeypatch.setattr(pd, "session_writers", session_writers)
    period = pd.Period(
        org="acme",
        namespace=SHARED,
        agent="a",
        user_id="u1",
        digests=({"session_id": "s1"}, {"session_id": "s2"}, {"session_id": "s3"}),
    )
    assert asyncio.run(pd._period_writer(object(), period)) == AGENT_8


def test_the_code_link_judge_calls_for_the_memorys_writer(writers: _Directory, monkeypatch) -> None:
    from server.code.domain.anchor import Definition
    from server.memory.app import code_link_judge

    seen: list[dict[str, str]] = []

    async def judge_claim(claim: str, old: str, new: str) -> tuple[str, str]:
        seen.append(await _sent())
        return "unsure", ""

    monkeypatch.setattr(code_link_judge, "judge_claim", judge_claim)
    link: Any = {"org": "acme", "namespace": AGENT_NS, "user_id": "u1"}
    asyncio.run(
        code_link_judge.judge(
            "f adds",
            link,
            "old",
            "new",
            Definition("a.py", "a.f", "f"),
            "h",
            writer="human:sso-alice",
        )
    )
    assert seen == [AS_ALICE]


def test_projection_repair_replays_for_the_memories_writer(
    writers: _Directory, monkeypatch
) -> None:
    from server.memory.app import projection_failures as pf
    from server.memory.workers import projection_repair
    from tests.unit.test_memory_repair_workers import _failure

    seen: list[dict[str, str]] = []
    failure = dataclasses.replace(
        _failure(1, "memory_chunks"), namespace=AGENT_NS, memory_keys=["k1"]
    )
    writers.writers[("acme", AGENT_NS, failure.agent, "u1", "-", "k1")] = f"process:{AGENT_8}"

    async def scan(**k: Any) -> list[Any]:
        return [failure]

    async def retry(f: Any, **k: Any) -> Any:
        seen.append(await _sent())
        return pf.ProjectionFailureRetryResult(f.failure_id, f.projection, True, True)

    monkeypatch.setattr(pf, "scan_active_projection_failures", scan)
    monkeypatch.setattr(pf, "retry_projection_failure", retry)
    asyncio.run(projection_repair.run_pass())
    assert seen == [AS_AGENT_8]


def test_a_deferred_extraction_by_a_member_is_the_members(writers: _Directory, monkeypatch) -> None:
    import server.memory.app.extract.handler as mx
    from server.identity.domain.read_scope import ReadFilters
    from server.memory.api.models_bulk import MemorySetFromMessagesOutput
    from server.memory.app import extraction_queue as queue
    from tests.unit.test_extraction_queue import _input

    seen: list[dict[str, str]] = []

    async def handler(inp: Any) -> Any:
        seen.append(await _sent())
        return MemorySetFromMessagesOutput()

    monkeypatch.setattr(mx, "handle_memory_set_from_messages", handler)
    scope = ReadFilters(scope="own_user", user_id="u1", session_id=None)
    for enqueuer in ("human:sso-alice", provenance.EXTRACTION_WORKER_ACTOR):
        payload, _, _ = queue.payload_for(
            _input(org="acme", namespace=AGENT_NS, user_id="u1"), scope=scope, enqueued_by=enqueuer
        )
        row = {"org": "acme", "namespace": AGENT_NS, "user_id": "u1", "payload": payload}
        asyncio.run(queue.replay_row(row))
    assert seen == [AS_ALICE, AS_AGENT_7]  # a pipeline enqueuer is no writer: the owner


def test_code_refresh_runs_the_ingest_pass_as_its_caller(monkeypatch) -> None:
    """The poller's ingest calls are the namespace's agent's; the same pass run by
    `code.refresh` is the caller's, as any request's calls are."""
    from server.auth.context import current_auth_var
    from server.auth.jwt import AuthInfo
    from server.code.app.index import ingest_pass as ip

    seen: list[tuple[str, str]] = []

    def record() -> None:
        who = customer.attribution()
        seen.append((who.namespace, who.subject))

    async def run_repo(cfg: Any, result: Any, budget: Any) -> Any:
        record()
        return SimpleNamespace(cfg=cfg, bootstrap=True, walk=False)

    async def leg(plan: Any, result: Any, budget: Any) -> None:
        record()

    async def nothing(*a: Any, **k: Any) -> Any:
        return set()

    repo = SimpleNamespace(org="acme", namespace="code-a", label="a")
    monkeypatch.setattr("server.code.app.index.repo_registry.refresh_db_repos", nothing)
    monkeypatch.setattr(ip, "_inactive_namespaces", nothing)
    monkeypatch.setattr(ip, "selected_repos", lambda org, ns: [repo])
    monkeypatch.setattr(ip, "_without_unsandboxed", lambda repos, result: repos)
    monkeypatch.setattr(ip, "prefetch_mirrors", nothing)
    monkeypatch.setattr(ip, "ingest_oldest_first", nothing)
    monkeypatch.setattr(ip, "_run_repo", run_repo)
    monkeypatch.setattr(ip, "_bootstrap_leg", leg)
    monkeypatch.setattr(ip, "_post_steps", nothing)
    switch_off()
    asyncio.run(ip._run(ip.CodeIngestResult()))  # the poller: no caller
    caller = AuthInfo(
        subject="sa:t:ci-agent",
        org="acme",
        namespaces=("code-a",),
        roles=(),
        is_service_account=True,
    )
    token = current_auth_var.set(caller)
    try:
        asyncio.run(ip._run(ip.CodeIngestResult(), "acme", "code-a"))  # code.refresh
    finally:
        current_auth_var.reset(token)
    # each pass: the entry's commits, then its bootstrap leg
    assert seen == [("code-a", "")] * 2 + [("code-a", caller.subject)] * 2


@pytest.mark.parametrize(
    ("setting", "value", "named"),
    [
        ("inference_identity_headers_json", '{"X-Agent": "{agent}"}', "IDENTITY_HEADERS"),
        ("inference_customer_header", "X-Customer", "CUSTOMER_HEADER"),
        ("inference_gateway_required", True, "GATEWAY_REQUIRED"),
    ],
)
def test_reembed_in_database_is_refused_when_calls_must_name_whom_they_are_for(
    monkeypatch, setting: str, value: Any, named: str
) -> None:
    from server.memory.app import reembed

    monkeypatch.setattr(settings, "inference_gateway_hosts", "gateway.example")
    for field in ("inference_identity_headers_json", "inference_customer_header"):
        monkeypatch.setattr(settings, field, "")
    monkeypatch.setattr(settings, "inference_gateway_required", False)
    assert reembed.in_database_refusal() == ""
    monkeypatch.setattr(settings, setting, value)

    class _Embedder:
        model_name, dim = "m", 2

    with pytest.raises(ValueError, match=named) as refused:
        asyncio.run(reembed.reembed_all(None, _Embedder(), in_database="ai_creds"))
    assert "re-embed without --in-database" in str(refused.value)


def test_a_sessions_writer_is_its_newest_live_versions() -> None:
    """Read through FINAL and by `version`: a deleted memory or a replaced version
    names no one, and an update (which keeps `created_at`) names its own writer."""
    from server.memory.store import reads

    class _CH:
        sent: list[tuple[str, dict[str, Any]]] = []

        async def query_all(self, sql: str, params: Any = None) -> list[Any]:
            self.sent.append((sql, params or {}))
            return []

    ch = _CH()
    asyncio.run(reads.session_writers([("acme", "ns", "a", "-", "s1")], ch=ch))  # type: ignore[arg-type]
    sql, params = ch.sent[0]
    assert "FROM loom.memory FINAL" in sql and "argMaxIf(actor, version," in sql
    assert "tombstoned_at IS NULL" in sql and params["keys"] == [("acme", "ns", "a", "-", "s1")]


def test_a_judge_scheduled_inside_a_job_is_for_the_jobs_writer(monkeypatch) -> None:
    """A write a job makes for a row's writer (an extraction replay, say) schedules its
    deferred judge for that writer; a request's, for its caller; a pipeline's own write
    whose principal the provenance names, for that principal."""
    from datetime import UTC, datetime

    from server.memory.app import conflict_deferred as cd
    from server.memory.domain.provenance import pipeline_actor

    settled: list[str] = []

    async def settle(*_: Any, subject: str = "", **__: Any) -> str:
        settled.append(subject)
        return "clear"

    monkeypatch.setattr(cd, "settle", settle)
    pending = SimpleNamespace(verdict=cd.PENDING, pending_judge=object())

    async def schedule() -> None:
        task = cd.schedule_settle(
            SimpleNamespace(org="acme"),
            pending,
            memory_id="m1",
            version=1,
            created_at=datetime.now(UTC),
        )
        assert task is not None
        await task

    with customer.for_org("acme", namespace=AGENT_NS, subject=AGENT_8):
        asyncio.run(schedule())  # no request behind it: the job named the writer
    with pipeline_actor(f"process:{AGENT_8}"), customer.for_org("acme", namespace=AGENT_NS):
        asyncio.run(schedule())  # the write's principal, from its provenance
    assert settled == [AGENT_8, AGENT_8]


def _structured_build(monkeypatch: pytest.MonkeyPatch, facts: list[dict[str, Any]]) -> list[Any]:
    """One structured profile build in agent-7's namespace over `facts`, whose model call
    records the identity headers it carries."""
    from server.memory.app import profile_schema as schema_app
    from server.memory.domain import profile_schema as ps
    from server.memory.store import profile_sources
    from server.memory.workers import structured_profile as sp

    seen: list[Any] = []

    async def digest(**kw: Any) -> tuple[str, int]:
        return ("d1", len(facts))

    async def nothing(**kw: Any) -> None:
        return None

    async def user_facts(**kw: Any) -> list[dict[str, Any]]:
        return [dict(f) for f in facts]

    async def ask(*a: Any) -> dict[str, Any]:
        seen.append(await _sent())
        return {"tz": "UTC"}

    monkeypatch.setattr(profile_sources, "sources_digest", digest)
    monkeypatch.setattr(profile_sources, "profile_row", nothing)
    monkeypatch.setattr(profile_sources, "profile_head_hold", nothing)
    monkeypatch.setattr(profile_sources, "user_facts", user_facts)
    monkeypatch.setattr(schema_app, "write_structured_profile", nothing)
    monkeypatch.setattr(sp, "_ask", ask)
    tz = {"type": "string", "description": "time zone"}
    props = ps.parse({"type": "object", "properties": {"tz": tz}})
    schema = sp.ActiveSchema(org="acme", namespace=AGENT_NS, props=props, sha256="h" * 64)
    asyncio.run(sp.build_profile(schema, "u1"))
    return seen


def test_a_structured_profile_build_calls_for_its_facts_newest_writer(
    writers: _Directory, monkeypatch
) -> None:
    """As the profile fold's: the newest principal that wrote the facts it reads (a
    pipeline's newer write names none), else the agent whose namespace it is."""
    facts = [
        {"memory_id": "m1", "value": '"tz UTC"', "since": "2026-01-01", "version": 2},
        {"memory_id": "m2", "value": '"en"', "since": "2026-01-02", "version": 3},
    ]
    by_agent_8 = [
        {**facts[0], "actor": f"process:{AGENT_8}"},
        {**facts[1], "actor": provenance.EXTRACTION_WORKER_ACTOR},
    ]
    assert _structured_build(monkeypatch, by_agent_8) == [AS_AGENT_8]
    by_no_one = [{**f, "actor": "worker:structured-profile"} for f in facts]
    assert _structured_build(monkeypatch, by_no_one) == [AS_AGENT_7]


def test_a_held_identity_waits_out_its_hold_in_the_structured_profile_pass(monkeypatch) -> None:
    """Like the fold's: a held build is no failure, the pass goes on with the next user,
    and the held user is left out until its hold ends."""
    import time
    from collections import deque

    from server.core.errors import ModelCallRefused
    from server.memory.store.profile_sources import Behind
    from server.memory.workers import structured_profile as sp

    async def build(schema: Any, user_id: str) -> int | None:
        if user_id == "u1":
            raise ModelCallRefused("refused", hold_s=600.0, gateway_status=429)
        return 0

    key = ("acme", AGENT_NS, "u1")
    # u1's last build failed, its wait over: the hold leaves its next failure wait as it was.
    monkeypatch.setattr(sp, "_backoff", {key: sp._Wait(time.monotonic() - 1, 120.0)})
    monkeypatch.setattr(sp, "_refused", {})
    monkeypatch.setattr(sp, "build_profile", build)
    schema = sp.ActiveSchema(org="acme", namespace=AGENT_NS, props=[], sha256="h" * 64)
    result = sp.StructuredProfileResult()
    users = [Behind("u1", "d1", 0), Behind("u2", "d1", 0)]
    asyncio.run(sp._build_users(schema, users, result, stop_at=time.monotonic() + 600))
    assert (result.errors, result.profiles_written) == (0, 1)
    assert sp.backing_off("acme", AGENT_NS) == ("u1",)
    wait = sp._backoff[key]
    assert 599 < wait.until - time.monotonic() <= 600 and wait.wait == 120.0
    # The next pass takes the namespace's other users and leaves u1 out.
    behind = deque([Behind("u1", "d1", 0), Behind("u3", "d1", 0)])
    scanned = sp._Scanned(schema.sha256, time.monotonic(), behind, complete=True)
    monkeypatch.setattr(sp, "_scans", {("acme", AGENT_NS): scanned})
    assert [b.user_id for b in sp._take(schema, 10) or []] == ["u3"]
