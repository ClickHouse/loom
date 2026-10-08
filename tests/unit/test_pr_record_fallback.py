"""A PR question the memory pool cannot answer is answered
from the merged-PR record, linked issues included."""

from __future__ import annotations

import asyncio
from datetime import datetime
from types import SimpleNamespace
from typing import Any

from server.memory.app import code_grounding as g
from server.memory.app.ask import records


def test_describe_pr_renders_state_title_and_linked_issues() -> None:
    row = {
        "author": "dev-river",
        "title": "Disable space-filling curve analysis for UInt64",
        "merged_at": datetime(2026, 9, 14, 19, 58),
        "linked_issues": [119651],
    }
    text = g.describe_pr(119663, row)
    assert "PR #119663" in text and "@dev-river" in text
    assert "merged on 2026-09-14" in text and "links issue #119651" in text
    # as-of before the merge: not merged yet
    early = g.describe_pr(119663, row, datetime(2026, 9, 1).date())
    assert "NOT merged yet as of 2026-09-01" in early
    assert g.describe_pr(1, {}) == ""
    assert g._linked_issue_numbers({"linked_issues": ["5", None, 0, 7]}) == [5, 7]


def test_injected_pr_fact_carries_linked_issues(monkeypatch) -> None:
    class _Repo:
        async def prs_by_number(
            self, org: str, ns: str, nums: list[int], **k: Any
        ) -> dict[int, Any]:
            return {
                119663: {
                    "author": "a",
                    "title": "t",
                    "merged_at": datetime(2026, 9, 14),
                    "linked_issues": [119651, 119652],
                }
            }

        async def items_by_number(self, *a: Any, **k: Any) -> dict[int, Any]:
            return {}

    import server.code.store.index as cr

    monkeypatch.setattr(cr, "CodeRepo", _Repo)
    monkeypatch.setattr(g, "_resolve_code_target", lambda si, st: ("clickhouse", "code-clickhouse"))
    hits = asyncio.run(
        g._inject_pr_facts("was PR #119663 merged?", [], SimpleNamespace(), SimpleNamespace())
    )
    assert len(hits) == 1
    assert "links issues #119651, #119652" in hits[0].content_excerpt


def test_fallback_answers_from_the_pr_record_before_the_items_row(monkeypatch) -> None:
    class _Repo:
        async def items_by_number(self, org: str, ns: str, nums: list[int]) -> dict[int, Any]:
            return {
                119663: {
                    "kind": "pr",
                    "state": "open",
                    "author": "a",
                    "title": "t",
                    "created_at": datetime(2026, 9, 1),
                }
            }

        async def prs_by_number(
            self, org: str, ns: str, nums: list[int], **k: Any
        ) -> dict[int, Any]:
            return {
                119663: {
                    "author": "dev-river",
                    "title": "t",
                    "merged_at": datetime(2026, 9, 14),
                    "linked_issues": [119651],
                }
            }

    import server.code.store.index as cr

    monkeypatch.setattr(cr, "CodeRepo", _Repo)
    monkeypatch.setattr(g, "_resolve_code_target", lambda si, st: ("clickhouse", "code-clickhouse"))
    from server.settings import settings

    out = asyncio.run(
        records._gh_items_fallback_answer(
            "Was PR #119663 merged, and which issue does it link to?",
            SimpleNamespace(as_of=None),
            settings,
            [],
            "m",
            None,
        )
    )
    assert out is not None and out.source == "record" and not out.abstain
    assert "merged on 2026-09-14" in out.answer and "#119651" in out.answer
    assert out.memory_keys == ["code_pr:code-clickhouse:119663"]


def test_fallback_default_is_on() -> None:
    pass
