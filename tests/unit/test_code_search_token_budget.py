"""Code search has no token cap unless the deployment or request supplies one."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from server.code.api.models_search import CodeSearchInput
from server.code.app.search.shared import page_plan
from server.code.domain.units import (
    CodeUnit,
    budget_units,
    estimate_unit_tokens,
    remaining_token_budget,
)
from server.settings import Settings, settings
from server.transport.mcp_defaults import mcp_default_arguments


def test_unlimited_keeps_every_whole_body_and_counts_tokens() -> None:
    units = [CodeUnit(unit_id=str(i), path=f"src/{i}.py", body="x " * 20000) for i in range(5)]
    served, used = budget_units(units, 0)
    assert served == units and all(not u.elided for u in served)
    assert used == sum(estimate_unit_tokens(u) for u in units)
    assert used > 12000


def test_unlimited_still_honours_bodyless_mode() -> None:
    units = [CodeUnit(path=f"src/{i}.py", body="x " * 20000) for i in range(25)]
    served, used = budget_units(units, 0, bodies="none")
    assert len(served) == len(units)
    assert all(u.elided and u.body == "" for u in served)
    assert used == sum(estimate_unit_tokens(u) for u in served)


def test_fanout_uses_the_same_unlimited_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    from server.code.app.search_fanout import budget

    monkeypatch.setattr(settings, "code_search_token_budget", 0)
    units = [CodeUnit(path=f"src/{i}.py", body="x " * 20000) for i in range(5)]
    served, used = budget(units, CodeSearchInput(query="retry transport", bodies="auto"))
    assert served == units and used > 12000
    monkeypatch.setattr(settings, "code_search_token_budget", 500)
    served, used = budget(units, CodeSearchInput(query="retry transport", bodies="auto"))
    assert used <= 500 and all(u.elided for u in served)


@pytest.mark.parametrize("overhead", [0, 300, 5000])
def test_overhead_preserves_unlimited_and_never_turns_finite_into_unlimited(overhead: int) -> None:
    assert remaining_token_budget(0, overhead) == 0
    assert remaining_token_budget(300, overhead) == max(1, 300 - overhead)


def test_environment_budget_applies_to_http_and_mcp(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LOOM_CODE_SEARCH_TOKEN_BUDGET", "20000")
    configured = Settings()
    assert configured.code_search_token_budget == 20000
    monkeypatch.setattr(settings, "code_search_token_budget", configured.code_search_token_budget)
    for args in ({"query": "how are requests retried"}, {"query": "RetryHandler::run"}):
        for supplied in (args, mcp_default_arguments("code.search", args)):
            assert page_plan(CodeSearchInput.model_validate(supplied)).token_budget == 20000
    assert page_plan(CodeSearchInput(query="retry", token_budget=0)).token_budget == 0
    assert page_plan(CodeSearchInput(query="retry", token_budget=500)).token_budget == 500


def test_default_is_unlimited_and_negative_budgets_are_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("LOOM_CODE_SEARCH_TOKEN_BUDGET", raising=False)
    assert Settings().code_search_token_budget == 0
    with pytest.raises(ValidationError):
        CodeSearchInput(query="retry", token_budget=-1)
    monkeypatch.setenv("LOOM_CODE_SEARCH_TOKEN_BUDGET", "-1")
    with pytest.raises(ValidationError):
        Settings()


@pytest.mark.parametrize("budget,annotate", [(0, True), (300, False), (539, False), (540, True)])
def test_annotations_leave_room_for_results(
    monkeypatch: pytest.MonkeyPatch, budget: int, annotate: bool
) -> None:
    monkeypatch.setattr(settings, "code_risk_annotations", True)
    plan = page_plan(CodeSearchInput(query="retry transport", top_k=5, token_budget=budget))
    assert plan.annotate is annotate
    assert bool(plan.reserve) is annotate
