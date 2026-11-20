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


@pytest.mark.parametrize("section", ["", "Sharing > Collections"])
def test_truncated_help_outline_requires_a_read_of_the_cited_section(shipped, section):
    (shipped / "features" / "sharing.md").write_text(
        "# Sharing\n\n## Collections\n\n"
        + "bounded content " * 2000
        + "\n\n## Unread sibling\n\nThis section needs its own read.\n"
    )
    docs.reset()

    async def check():
        first = HelpGetInput(path="features/sharing.md", section=section)
        result = await docs.handle_help_get(first)
        assert result.truncated and "This section needs its own read." not in result.text
        assert any(row.anchor == "unread-sibling" for row in result.sections)
        receipts = [Receipt("help.get", first.model_dump(), result.model_dump(), "model_tool", 1)]
        citation = "[help:features/sharing.md § Sharing > Unread sibling]"
        assert not evidence.verify(citation, receipts)[0].verified
        target = HelpGetInput(path="features/sharing.md", section="Sharing > Unread sibling")
        read = await docs.handle_help_get(target)
        receipts.append(
            Receipt("help.get", target.model_dump(), read.model_dump(), "model_tool", 2)
        )
        verified = evidence.verify(citation, receipts)[0]
        assert verified.verified and verified.anchor == "unread-sibling"

    asyncio.run(check())


def test_a_continuation_outline_does_not_verify_the_whole_section(shipped):
    async def check():
        page = await docs.handle_help_get(HelpGetInput(path="features/sharing.md"))
        args = HelpGetInput(path="features/sharing.md", offset=len(page.text) - 5)
        result = await docs.handle_help_get(args)
        assert not result.truncated and len(result.text) == 5 and result.sections
        receipt = Receipt("help.get", args.model_dump(), result.model_dump(), "model_tool", 1)
        citation = "[help:features/sharing.md § Sharing > Collections]"
        assert not evidence.verify(citation, [receipt])[0].verified

    asyncio.run(check())


def test_an_empty_help_read_does_not_verify_a_page(shipped):
    async def check():
        args = HelpGetInput(path="features/sharing.md", offset=len(_PAGE) + 10)
        result = await docs.handle_help_get(args)
        assert result.found and not result.text
        receipt = Receipt("help.get", args.model_dump(), result.model_dump(), "model_tool", 1)
        assert not evidence.verify("[help:features/sharing.md]", [receipt])[0].verified

    asyncio.run(check())
