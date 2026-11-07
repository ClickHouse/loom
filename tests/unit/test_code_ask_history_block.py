"""code.ask reads the history route's PR / issue records."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from server.code.app import answers as ca
from server.code.app import answers_entry


def test_history_lines_render_prs_and_items() -> None:
    hist = {
        "prs": [
            {
                "pr_number": 119663,
                "author": "contrib1",
                "merged_at": "2026-09-14 19:58:04",
                "title": "Disable space-filling curve analysis for UInt64",
                "linked_issues": [119651],
                "role": "pr",
            },
            {
                "pr_number": 5,
                "author": "x",
                "merged_at": "",
                "title": "t",
                "role": "fixes #7",
                "is_revert": True,
            },
        ],
        "items": [
            {
                "number": 119651,
                "kind": "issue",
                "state": "closed",
                "state_reason": "completed",
                "opened_at": "2026-09-13 10:00:00",
                "author": "reporter",
                "title": "Slow analysis",
            },
            {"number": 42, "kind": "unknown"},
        ],
    }
    lines = ca.history_lines(hist)
    assert lines[0] == (
        "PR #119663 by @contrib1 merged 2026-09-14 — Disable space-filling curve "
        "analysis for UInt64 links issue #119651"
    )
    assert lines[1] == "PR #5 by @x merged — t (fixes #7) (a revert)"
    assert (
        lines[2]
        == "#119651 is an issue (closed, completed) opened 2026-09-13 by @reporter — Slow analysis"
    )
    assert len(lines) == 3
    assert ca.history_lines({}) == []


class _Embedder:
    model_name = "m"

    async def embed(self, text: str) -> list[float]:
        return [0.0, 0.0, 0.0]


class _LLM:
    model = "fake"

    def __init__(self) -> None:
        self.prompts: list[str] = []

    async def complete(self, system: str, user: str, max_tokens: int = 0):
        self.prompts.append(user)
        return SimpleNamespace(
            text="PR #119663 was merged on 2026-09-14 and links issue #119651 [history].\nCOVERAGE: none"
        )


def _ask(
    monkeypatch: pytest.MonkeyPatch, question: str, *, units, history, abstain=False
) -> tuple[answers_entry.AskResult, _LLM]:
    async def _propose(llm, q):
        return []

    monkeypatch.setattr(ca, "_propose_identifiers", _propose)

    async def _mem(q):
        return ca.MemoryContext()

    monkeypatch.setattr(ca, "_inject_memory_context", _mem)
    import server.code.app.search as cs

    async def _search(*a, **k):
        return SimpleNamespace(
            abstain=abstain,
            abstain_reason="no strong match in the code index",
            units=units,
            history=history,
            retrieval_confidence=0.0,
        )

    monkeypatch.setattr(cs, "search", _search)
    llm = _LLM()
    out = asyncio.run(
        ca.ask(
            SimpleNamespace(),
            "o",
            "code-x",
            question=question,
            embedder=_Embedder(),
            llm=llm,
            search_kwargs={},
            no_cache=True,
        )
    )
    return out, llm


def test_pr_question_with_no_code_units_is_answered_from_history(monkeypatch) -> None:
    hist = {
        "prs": [
            {
                "pr_number": 119663,
                "author": "contrib1",
                "merged_at": "2026-09-14",
                "title": "t",
                "linked_issues": [119651],
                "role": "pr",
            }
        ],
        "items": [],
    }
    out, llm = _ask(
        monkeypatch,
        "Was PR #119663 merged, and which issue does it link to?",
        units=[],
        history=hist,
        abstain=True,
    )
    assert not out.abstain and out.source == "synthesized"
    assert (
        "Repository history" in llm.prompts[0]
        and "PR #119663 by @contrib1 merged 2026-09-14" in llm.prompts[0]
    )
    assert "Code excerpts:\n\n(none)" in llm.prompts[0]


def test_no_units_and_no_history_still_abstains(monkeypatch) -> None:
    out, llm = _ask(
        monkeypatch, "how does the merge selector work", units=[], history={}, abstain=True
    )
    assert out.abstain and llm.prompts == []
