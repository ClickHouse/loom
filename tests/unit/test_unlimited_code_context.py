"""Selected code context is complete unless the caller requests a token cap."""

from __future__ import annotations

import pytest

from server.code.api.models_context import CodeContextPackInput, CodeMapInput
from server.code.api.models_navigation import CodeEnclosingInput, CodeOutlineInput, CodeTraceInput
from server.code.api.models_review import CodeReviewBriefInput
from server.code.api.models_search import CodeAskInput
from server.code.api.models_symbol import CodeSymbolInput
from server.code.app.answers_entry import token_budget_bucket
from server.code.app.ask import _ask_search_kwargs
from server.code.app.history import fit_section_budget
from server.code.app.index.packs import fit_pack_budget
from server.code.app.symbol.outline import budget_outline
from server.settings import settings


@pytest.mark.parametrize(
    "model",
    [
        CodeSymbolInput,
        CodeEnclosingInput,
        CodeOutlineInput,
        CodeTraceInput,
        CodeContextPackInput,
        CodeMapInput,
        CodeReviewBriefInput,
    ],
)
def test_code_context_defaults_to_unlimited(model):
    assert model.model_fields["token_budget"].default == 0


def test_unlimited_precomputed_pack_keeps_every_section():
    pack = "# Context\n" + "\n## Evidence\n" + "retained context\n" * 5000
    assert fit_pack_budget(pack, 0) == (pack, False)
    bounded, truncated = fit_pack_budget(pack, 200)
    assert truncated and len(bounded) < len(pack)


def test_unlimited_outline_keeps_complete_signatures():
    rows = [
        dict(
            qualified_name=f"m.f{i}",
            name=f"f{i}",
            kind="function",
            signature="x" * 600,
            start_line=i + 1,
            end_line=i + 1,
        )
        for i in range(30)
    ]
    entries, used, next_step = budget_outline(rows, 0)
    assert len(entries) == len(rows) and used > 1500 and not next_step
    limited, tokens, next_step = budget_outline(rows, 200)
    assert len(limited) < len(rows) and tokens <= 200 and next_step


def test_history_obeys_an_explicit_cap_and_unlimited_keeps_all_rows():
    section = {"intent": "number", "prs": [{"title": "Long history " * 200}], "items": []}
    assert fit_section_budget(section, 1) == {}
    kept = fit_section_budget(section, 0)
    assert kept["prs"] == section["prs"] and kept["tokens"] > 1
    assert fit_section_budget(section, kept["tokens"] + 20)["prs"] == section["prs"]


def test_explicit_unlimited_ask_overrides_the_deployment_cap(monkeypatch):
    monkeypatch.setattr(settings, "code_ask_token_budget", 4000)
    assert token_budget_bucket(None) == 4000
    assert token_budget_bucket(0) == 0
    assert token_budget_bucket(20000) == 20000

    assert (
        _ask_search_kwargs(
            CodeAskInput(question="Where is the handler?", token_budget=0), 0.5, 0.1
        )["token_budget"]
        == 0
    )
