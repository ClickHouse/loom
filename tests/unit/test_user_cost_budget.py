"""Named-user cost caps remain independent of the namespace ceiling."""

from __future__ import annotations

from typing import Any

import pytest

from server.core.event_source import ALL_EVENTS
from server.insights.api.models_cost import BudgetStatusInput
from server.insights.app import cost
from server.observability import budget


class BudgetCH:
    def __init__(self, *, namespace_cap: float = 0, user_cap: float = 2, spent: float = 3) -> None:
        self.namespace_cap, self.user_cap, self.spent = namespace_cap, user_cap, spent
        self.reads: list[dict[str, Any]] = []
        self.inbox = ""
        self.marked_spent = 0.0

    async def query_one(self, sql: str, params: dict[str, Any]) -> dict[str, Any]:
        self.reads.append(params)
        if "FROM loom.namespaces" in sql:
            return {
                "daily_cost_cap_usd": self.namespace_cap,
                "daily_user_cost_cap_usd": self.user_cap,
                "budget_alert_inbox": self.inbox,
            }
        assert f"FROM {ALL_EVENTS}" in sql
        assert "WHERE" not in ALL_EVENTS.upper(), "the source must include every event"
        assert "mirror_marked" not in sql.replace(ALL_EVENTS, "event_rows"), (
            "budget predicates and aggregates count marked spend too"
        )
        return {
            "spent": 10.0,
            "own": 0.0,
            "user_spent": self.spent + self.marked_spent if params.get("uid") == "alice" else 0.0,
            "user_own": 0.0,
        }


@pytest.fixture
def ch(monkeypatch: pytest.MonkeyPatch) -> BudgetCH:
    from server.settings import settings

    client = BudgetCH()
    monkeypatch.setattr("server.core.ch.get_client", lambda: client)
    monkeypatch.setattr(cost, "get_client", lambda: client)
    monkeypatch.setattr(settings, "default_daily_cost_cap_usd", 0)
    monkeypatch.setattr("server.auth.context.current_auth", lambda: None)
    return client


async def test_user_cap_applies_with_an_uncapped_namespace(ch: BudgetCH) -> None:
    assert await budget.namespace_over_cap("o", "n", user_id="alice") is True
    assert await budget.namespace_over_cap("o", "n", user_id="bob") is False


async def test_user_cap_controls_budget_pressure_routing(ch: BudgetCH) -> None:
    assert await budget.namespace_budget_ratio("o", "n", user_id="alice") == 1.5
    assert await budget.namespace_budget_ratio("o", "n", user_id="bob") == 0.0


async def test_namespace_cap_still_stops_a_user_under_their_own_cap(ch: BudgetCH) -> None:
    ch.namespace_cap = 5
    assert await budget.namespace_over_cap("o", "n", user_id="bob") is True
    assert await budget.namespace_budget_ratio("o", "n", user_id="bob") == 2.0


async def test_an_unattributed_check_does_not_guess_a_user(ch: BudgetCH) -> None:
    assert await budget.namespace_over_cap("o", "n") is False
    assert await budget.namespace_budget_ratio("o", "n") == 0.0
    assert len(ch.reads) == 2, "uncapped anonymous checks need only their cap reads"


async def test_user_budget_status_uses_the_named_users_spend(
    ch: BudgetCH, monkeypatch: pytest.MonkeyPatch
) -> None:
    from server.auth.jwt import AuthInfo
    from server.operations.base import Role

    auth = AuthInfo(
        subject="operator",
        org="o",
        namespaces=("*",),
        roles=(Role.PLATFORM_ADMIN,),
        is_service_account=False,
    )
    monkeypatch.setattr("server.auth.context.current_auth", lambda: auth)
    out = await cost.handle_budget_status(
        BudgetStatusInput(org="o", namespace="n", user_id="alice")
    )
    assert out.user_id == "alice"
    assert (out.cap_usd, out.spent_24h_usd, out.cap_source) == (2.0, 3.0, "namespace_user")
    assert out.over_cap is True and out.pct_of_cap == 1.0


@pytest.mark.parametrize(
    "principal", ["none", "viewer", "org_admin", "narrow_operator", "operator"]
)
@pytest.mark.parametrize("marked_spent", [0.0, 2.0])
async def test_user_spend_is_operator_only_even_when_every_event_is_ordinary(
    ch: BudgetCH, monkeypatch: pytest.MonkeyPatch, principal: str, marked_spent: float
) -> None:
    from server.auth.jwt import AuthInfo
    from server.observability import budget_notifications
    from server.operations.base import Role
    from server.settings import settings

    role = {"viewer": Role.VIEWER, "org_admin": Role.ORG_ADMIN}.get(principal, Role.PLATFORM_ADMIN)
    auth = (
        None
        if principal == "none"
        else AuthInfo(
            subject="reader",
            org="o",
            namespaces=("n",),
            roles=(role,),
            is_service_account=False,
            ns_roles=(("n", "admin"),) if principal == "narrow_operator" else (),
        )
    )
    assert auth is None or not auth.read_user_id
    monkeypatch.setattr("server.auth.context.current_auth", lambda: auth)
    warnings: list[budget_notifications.BudgetWarning] = []
    monkeypatch.setattr(budget_notifications, "_observer", warnings.append)
    monkeypatch.setattr(settings, "cost_budget_alert_share", 0.8)
    ch.inbox = "warnings"
    ch.spent, ch.marked_spent = 1.0, marked_spent
    out = await cost.handle_budget_status(
        BudgetStatusInput(org="o", namespace="n", user_id="alice")
    )
    real_spent = 1.0 + marked_spent
    assert out.spent_24h_usd == (real_spent if principal == "operator" else None)
    assert out.pct_of_cap == (min(1.0, real_spent / 2) if principal == "operator" else None)
    assert out.cap_usd == 2 and out.cap_source == "namespace_user"
    assert out.over_cap is (real_spent >= 2) and out.namespace_over_cap is False
    assert len(warnings) == (1 if marked_spent else 0)
    if warnings:
        assert warnings[0].user_id == "alice" and warnings[0].spent == real_spent
    snapshot = await budget.budget_snapshot("o", "n", user_id="alice", read_uncapped=True, ch=ch)
    assert snapshot.user_spent == real_spent
    assert await budget.namespace_over_cap("o", "n", user_id="alice") is (real_spent >= 2)
    assert await budget.namespace_budget_ratio("o", "n", user_id="alice") == real_spent / 2
    total = await cost.handle_budget_status(BudgetStatusInput(org="o", namespace="n"))
    assert total.spent_24h_usd == 10 and total.pct_of_cap is None
    ch.namespace_cap = 5
    out = await cost.handle_budget_status(
        BudgetStatusInput(org="o", namespace="n", user_id="alice")
    )
    assert out.namespace_over_cap is True and out.over_cap is (real_spent >= 2)


async def test_user_status_reports_the_namespace_gate_too(ch: BudgetCH) -> None:
    ch.namespace_cap = 5
    out = await cost.handle_budget_status(BudgetStatusInput(org="o", namespace="n", user_id="bob"))
    assert out.over_cap is False and out.namespace_over_cap is True


async def test_global_user_cap_is_the_fallback(
    ch: BudgetCH, monkeypatch: pytest.MonkeyPatch
) -> None:
    from server.settings import settings

    ch.user_cap = 0
    monkeypatch.setattr(settings, "default_daily_user_cost_cap_usd", 1)
    out = await cost.handle_budget_status(
        BudgetStatusInput(org="o", namespace="n", user_id="alice")
    )
    assert out.over_cap and out.cap_source == "global_user" and out.cap_usd == 1


async def test_check_time_warning_threshold_and_scope(
    ch: BudgetCH, monkeypatch: pytest.MonkeyPatch
) -> None:
    from server.observability import budget_notifications
    from server.settings import settings

    warnings: list[budget_notifications.BudgetWarning] = []
    monkeypatch.setattr(budget_notifications, "_observer", warnings.append)
    monkeypatch.setattr(settings, "cost_budget_alert_share", 0.8)
    ch.inbox, ch.namespace_cap, ch.spent = "warnings", 20, 1.6
    assert not await budget.namespace_over_cap("o", "n", user_id="alice")
    assert len(warnings) == 1 and warnings[0].user_id == "alice"
    warnings.clear()
    await budget.namespace_over_cap("o", "n")
    assert warnings == [], "an unscoped check does not discover users"
    ch.namespace_cap = 10
    await budget.namespace_over_cap("o", "n")
    assert len(warnings) == 1 and warnings[0].user_id == ""


async def test_notification_failure_cannot_change_the_cap_decision(
    ch: BudgetCH, monkeypatch: pytest.MonkeyPatch
) -> None:
    from server.observability import budget_notifications

    def unavailable(warning: budget_notifications.BudgetWarning) -> None:
        raise RuntimeError("inbox delivery unavailable")

    ch.inbox = "warnings"
    monkeypatch.setattr(budget_notifications, "_observer", unavailable)
    assert await budget.namespace_over_cap("o", "n", user_id="alice")


async def test_user_status_requires_a_namespace(ch: BudgetCH) -> None:
    from server.core.errors import ValidationError

    with pytest.raises(ValidationError, match="namespace is required"):
        await cost.handle_budget_status(BudgetStatusInput(org="o", user_id="alice"))


@pytest.mark.parametrize("cascade", [False, True])
async def test_ask_admission_uses_the_same_named_user_as_its_ledger(
    ch: BudgetCH, monkeypatch: pytest.MonkeyPatch, cascade: bool
) -> None:
    from server.memory.api.models_ask import MemoryAskInput
    from server.memory.app.ask.handler import _ask_clients
    from server.settings import settings

    monkeypatch.setattr(settings, "memory_ask_synth_model_cheap", "fake-cheap" if cascade else "")
    state = await _ask_clients(
        MemoryAskInput(
            org="o", namespace="n", user_id="alice", query="explain", allow_synthesis=True
        ),
        settings,
    )
    assert state[4] is True
    assert state[:4] == (None, None, None, None)
    assert ch.reads[-1]["uid"] == "alice"


@pytest.mark.parametrize("query", ["summarize the report", "overview of the report"])
@pytest.mark.parametrize("user_id", ["alice", "bob"])
async def test_document_route_honors_the_named_users_cap(
    ch: BudgetCH, monkeypatch: pytest.MonkeyPatch, query: str, user_id: str
) -> None:
    from types import SimpleNamespace

    from server.knowledge.api.models_knowledge import KnowledgeSearchInput
    from server.knowledge.app import knowledge_search, synthesize
    from server.knowledge.app.knowledge_search import SearchRequest
    from server.knowledge.app.source_access import ReadPlan
    from server.memory.api.models_ask import MemoryAskInput
    from server.memory.app.ask.handler import _maybe_route_to_synthesize
    from server.memory.app.ask.source_context import PreparedKnowledge
    from server.memory.domain.corpus_synthesis import CorpusChunk

    calls: list[str] = []

    prepared = PreparedKnowledge(
        SearchRequest(KnowledgeSearchInput(org="o", namespace="n", query=query), ("n",), ()),
        ReadPlan("o"),
    )

    async def search(request: SearchRequest, plan: ReadPlan) -> Any:
        assert request.namespaces == ("n",) and plan is prepared.plan

        async def current() -> Any:
            return SimpleNamespace(
                results=[SimpleNamespace(source_uri="doc://report", tags=[]) for _ in range(2)]
            )

        return SimpleNamespace(current=current, proofs=None)

    class LLM:
        model = "fake"

        async def complete(self, *args: Any, **kwargs: Any) -> Any:
            calls.append(user_id)
            return SimpleNamespace(text="The report describes a deployment. [doc://report#0]")

    async def chunks(inp: Any) -> Any:
        return [CorpusChunk(source_uri="doc://report", idx=0, text="Deployment report.")], 1

    from server.settings import settings

    monkeypatch.setattr(settings, "memory_ask_synth_model", "")
    monkeypatch.setattr("server.llm.client.get_llm_client", LLM)
    monkeypatch.setattr(knowledge_search, "run_checked_search", search)
    monkeypatch.setattr(synthesize, "_corpus_chunks", chunks)
    out = await _maybe_route_to_synthesize(
        MemoryAskInput(org="o", namespace="n", user_id=user_id, query=query), prepared
    )
    if user_id == "alice":
        assert out is None, "a capped user must fall through to retrieval without synthesis"
        assert calls == []
    else:
        assert out is not None and out.source == "synthesized"
        assert calls == ["bob", "bob"]


async def test_direct_synthesis_checks_the_token_pinned_users_budget(
    ch: BudgetCH, monkeypatch: pytest.MonkeyPatch
) -> None:
    from server.identity.domain.read_scope import ReadFilters, with_read_identity
    from server.knowledge.api.models_knowledge import KnowledgeSynthesizeInput
    from server.knowledge.app.synthesize import handle_knowledge_synthesize

    inp = with_read_identity(
        KnowledgeSynthesizeInput(
            org="o", namespace="n", source_uris=["doc://report"], task="Summarize"
        ),
        ReadFilters("own_user", "alice", None),
    )
    out = await handle_knowledge_synthesize(inp)
    assert out.abstain and out.abstain_reason == "budget_capped"
    assert ch.reads[-1]["uid"] == "alice"


@pytest.mark.parametrize("user_id", ["alice", "bob"])
async def test_code_synthesis_checks_its_ledger_user_before_building_a_client(
    ch: BudgetCH, monkeypatch: pytest.MonkeyPatch, user_id: str
) -> None:
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from server.code.api.models_search import CodeAskInput
    from server.code.app import answers
    from server.code.app import ask as code_ask
    from server.settings import settings

    class Retrieved(Exception):
        pass

    clients: list[str] = []
    selected: list[Any] = []

    def client() -> Any:
        clients.append(user_id)
        return SimpleNamespace(model="fake")

    async def retrieve(*args: Any, **kwargs: Any) -> Any:
        selected.append(kwargs["llm"])
        raise Retrieved

    monkeypatch.setattr(code_ask, "require_code_intel", lambda: None)
    monkeypatch.setattr(code_ask, "refuse_synthesis", lambda *args: None)
    monkeypatch.setattr(code_ask, "resolve_code_identity", lambda org, ns: (org, ns))
    monkeypatch.setattr(code_ask, "CodeRepo", lambda: object())
    monkeypatch.setattr(code_ask, "resolve_code_thresholds", AsyncMock(return_value=(0.5, 0.1)))
    monkeypatch.setattr(code_ask, "index_as_of", AsyncMock(return_value={}))
    monkeypatch.setattr("server.embedding.embedder.get_embedder", lambda: object())
    monkeypatch.setattr("server.llm.client.get_llm_client", client)
    monkeypatch.setattr(settings, "code_ask_llm_model", "")
    monkeypatch.setattr(answers, "ask", retrieve)
    with pytest.raises(Retrieved):
        await code_ask.handle_code_ask(
            CodeAskInput(org="o", namespace="n", user_id=user_id, question="Explain this function")
        )
    assert clients == ([] if user_id == "alice" else ["bob"])
    assert (selected[0] is None) == (user_id == "alice")
    assert ch.reads[-1]["uid"] == user_id


@pytest.mark.parametrize("user_id", ["alice", "bob"])
async def test_code_query_expansion_checks_its_named_users_cap(
    ch: BudgetCH, monkeypatch: pytest.MonkeyPatch, user_id: str
) -> None:
    from unittest.mock import AsyncMock

    from server.code.api.models_search import CodeSearchInput
    from server.code.app import answers
    from server.code.app.search import handler

    class Retrieved(Exception):
        pass

    expanded: list[str] = []

    async def expand(*args: Any) -> list[str]:
        expanded.append(user_id)
        return ["retention"]

    def retrieve(*args: Any) -> Any:
        raise Retrieved

    monkeypatch.setattr(handler, "require_code_intel", lambda: None)
    monkeypatch.setattr(handler, "resolve_code_identity", lambda org, ns: (org, ns))
    monkeypatch.setattr(handler, "CodeRepo", lambda: object())
    monkeypatch.setattr(handler, "resolve_code_thresholds", AsyncMock(return_value=(0.5, 0.1)))
    monkeypatch.setattr(handler, "_not_covered_output", lambda *args: None)
    monkeypatch.setattr(handler, "_search_embedder", lambda inp: (None, "fake"))
    monkeypatch.setattr(handler, "_user_recent_dirs", AsyncMock(return_value=[]))
    monkeypatch.setattr(handler, "reads_llm_free", lambda: False)
    monkeypatch.setattr(handler, "_budget_allows", lambda *args: True)
    monkeypatch.setattr(handler, "page_plan", retrieve)
    monkeypatch.setattr(answers, "_propose_identifiers", expand)
    monkeypatch.setattr("server.llm.client.get_llm_client", lambda: object())
    with pytest.raises(Retrieved):
        await handler.handle_code_search(
            CodeSearchInput(org="o", namespace="n", user_id=user_id, query="retention", expand=True)
        )
    assert expanded == ([] if user_id == "alice" else ["bob"])
    assert ch.reads[-1]["uid"] == user_id
