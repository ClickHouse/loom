"""A question naming a PR or issue number of the indexed
repository is not "a different technology" — the off-index gate lets it
through to retrieval (whose history route resolves the number)."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from server.code.app import answers as ca
from server.code.app import answers_entry


def test_names_pr_or_issue() -> None:
    assert ca.names_pr_or_issue("Was PR #119663 in ClickHouse/ClickHouse merged?")
    assert ca.names_pr_or_issue("what does issue 119651 report")
    assert ca.names_pr_or_issue("see #12345")
    assert not ca.names_pr_or_issue("how does the merge selector pick parts in 2026")
    assert not ca.names_pr_or_issue("PR 42 fixed it")  # below the repo's range
    assert not ca.names_pr_or_issue("")


class _Embedder:
    model_name = "m"

    async def embed(self, text: str) -> list[float]:
        return [0.0, 0.0, 0.0]


def _run(
    monkeypatch: pytest.MonkeyPatch, question: str, *, matched: bool = False
) -> answers_entry.AskResult:
    async def _none(llm, q):  # the proposer says NONE for both questions
        return ["NONE"]

    monkeypatch.setattr(ca, "_propose_identifiers", _none)

    import server.code.app.search as cs

    async def _search(*a, **k):
        units = (
            [
                SimpleNamespace(
                    path="frontend/src/App.tsx",
                    start_line=1,
                    end_line=20,
                    symbol="frontend.src.App",
                    unit_id="app",
                )
            ]
            if matched
            else []
        )
        return SimpleNamespace(
            abstain=not matched,
            abstain_reason="" if matched else "no strong match in the code index",
            units=units,
        )

    monkeypatch.setattr(cs, "search", _search)
    return asyncio.run(
        ca.ask(
            SimpleNamespace(),
            "o",
            "code-x",
            question=question,
            embedder=_Embedder(),
            llm=object(),
            search_kwargs={},
            allow_synthesis=False,
            no_cache=True,
        )
    )


def test_pr_number_question_passes_the_gate_to_retrieval(monkeypatch) -> None:
    out = _run(monkeypatch, "Was PR #119663 merged, and which issue does it link to?")
    assert out.abstain and "different technology" not in out.abstain_reason
    assert out.abstain_reason == "no strong match in the code index"


def test_off_repo_question_still_abstains_at_the_gate(monkeypatch) -> None:
    out = _run(monkeypatch, "how do I configure a Kafka consumer group in Java")
    assert out.abstain and "different technology" in out.abstain_reason


def test_index_evidence_overrides_a_false_off_repo_proposal(monkeypatch) -> None:
    out = _run(
        monkeypatch,
        "Describe the Loom frontend React and TypeScript architecture",
        matched=True,
    )
    assert not out.abstain
    assert out.citations[0]["path"] == "frontend/src/App.tsx"
