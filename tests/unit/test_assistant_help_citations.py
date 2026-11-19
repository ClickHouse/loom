"""An answer's help citations, checked against the documentation this build ships, and the
op and setting names it writes that the build neither serves nor documents."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from server.assistant.app import citations, evidence
from server.assistant.app.tools import Receipt
from server.assistant.domain import names
from server.assistant.domain.citations import parse
from server.help.api.models import HelpGetInput
from server.help.app import docs

_PAGE = """# Sharing

Opening.

## Collections

Grant with `grants.create`. Events: `grant.created`. Set `LOOM_SHARING_MODE`.
"""


@pytest.fixture
def shipped(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    (tmp_path / "features").mkdir()
    (tmp_path / "features" / "sharing.md").write_text(_PAGE)
    monkeypatch.setattr(docs, "DOCS_DIR", tmp_path)
    docs.reset()
    yield tmp_path
    docs.reset()


def _verify(text: str) -> list[dict[str, Any]]:
    async def read():
        receipts = []
        for path in {c.path.removeprefix("docs/") for c in parse(text) if c.kind == "help"}:
            result = await docs.handle_help_get(HelpGetInput(path=path))
            receipts.append(
                Receipt("help.get", {"path": path}, result.model_dump(), "model_tool", 0)
            )
        return evidence.verify(text, receipts)

    return [c.model_dump() for c in asyncio.run(read())]


def test_a_help_citation_is_parsed_with_its_page_and_section() -> None:
    (page, section) = parse(
        "See [help:features/sharing.md] and [help:docs/features/sharing.md § Sharing > Collections]."
    )
    assert (page.kind, page.ref, page.section) == ("help", "features/sharing.md", ())
    assert (section.path, section.section) == (
        "docs/features/sharing.md",
        ("Sharing", "Collections"),
    )


def test_help_citations_are_checked_against_the_shipped_pages(shipped) -> None:
    text = (
        "[help:features/sharing.md] [help:docs/features/sharing.md § Sharing > Collections] "
        "[help:features/sharing.md § Sharing > Groups] [help:features/absent.md]"
    )
    got = [(c["verified"], c["ref"], c["anchor"]) for c in _verify(text)]
    assert got == [
        (True, "features/sharing.md", ""),
        (True, "features/sharing.md", "collections"),
        (False, "features/sharing.md", ""),
        (False, "features/absent.md", ""),
    ]


def test_with_no_docs_a_help_citation_is_unchecked_and_no_name_is_called_unknown(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(docs, "DOCS_DIR", tmp_path / "absent")
    docs.reset()
    (only,) = _verify("[help:features/sharing.md]")
    assert not only["verified"] and only["reason"] == "this answer did not read this source"
    assert asyncio.run(citations.unknown_names("`memory.frobnicate`")) == []
    docs.reset()


def test_a_name_is_unknown_only_when_the_build_neither_serves_nor_documents_it(shipped) -> None:
    answer = (
        "Call `memory.set`, `grants.create(subject=...)` or `memory.frobnicate`; "
        "`grant.created` is documented, `loom.agent_events` and `agent.yaml` are not ops. "
        "Set LOOM_LLM_MODEL, LOOM_BASE_URL, LOOM_SHARING_MODE, LOOM_MADE_UP or a LOOM_SHARING_ one."
    )
    assert asyncio.run(citations.unknown_names(answer)) == ["memory.frobnicate", "LOOM_MADE_UP"]


def test_candidates_are_code_spans_of_op_shape_and_every_loom_variable() -> None:
    text = "`a.b` `run.begin(goal=1)` `not a name` memory.set LOOM_X and `LOOM_Y`"
    assert names.candidates(text) == ["a.b", "run.begin", "LOOM_X", "LOOM_Y"]


def test_a_section_is_found_without_its_page_title_or_by_a_unique_tail(shipped) -> None:
    (shipped / "features" / "two.md").write_text(
        "# Two\n\n## Alpha\n\n### Setup\n\na\n\n## Beta\n\n### Setup\n\nb\n\n### Only here\n\nc\n"
    )
    docs.reset()
    (shipped / "features" / "three.md").write_text("# Three\n\n## A\n\nx\n\n## B\n\n### A\n\ny\n")
    docs.reset()
    text = (
        "[help:features/sharing.md § Collections] [help:features/two.md § Beta > Setup] "
        "[help:features/two.md § Only here] [help:features/two.md § Setup] "
        "[help:features/three.md § A]"
    )
    got = [(c["verified"], c["anchor"]) for c in _verify(text)]
    # The title left off; a tail that names one section; a tail two sections share; and the
    # title left off before a tail another section also ends with.
    assert got == [
        (True, "collections"),
        (True, "setup-1"),
        (True, "only-here"),
        (False, ""),
        (True, "a"),
    ]
