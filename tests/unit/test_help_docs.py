"""help.search and help.get serve the docs this build ships: split at their headings,
found by their words, read back by path and section, and admitted for every token that
may read the agent guide."""

from __future__ import annotations

import asyncio
import threading
from pathlib import Path

import pytest

from server.auth.authz import Role
from server.auth.jwt import AuthInfo
from server.core.paths import DOCS_DIR, REPO_ROOT
from server.help.api.models import HelpGetInput, HelpSearchInput
from server.help.app import docs
from server.help.domain import index as docs_index
from server.operations.registry import OPERATIONS_BY_NAME
from server.operations.toolsets import MCP_ASSISTANT_OPS, listed_under_all, op_toolsets
from server.transport import gates

_PAGE = """# Sharing

Opening text about sharing.

## Collections

A collection is shared with a team by granting roles.

```bash
# not a heading
loom grant
```

### Grant a role

Run grants.create with the role.

## Collections

The same heading again.
"""


def test_a_page_splits_at_its_headings_and_skips_fenced_ones() -> None:
    sections = docs_index.split_page("features/sharing.md", _PAGE)
    assert [(s.breadcrumb, s.anchor) for s in sections] == [
        (("Sharing",), "sharing"),
        (("Sharing", "Collections"), "collections"),
        (("Sharing", "Collections", "Grant a role"), "grant-a-role"),
        (("Sharing", "Collections"), "collections-1"),
    ]
    assert all(s.title == "Sharing" for s in sections)
    assert "# not a heading" in sections[1].text
    assert sections[0].audience == "user"
    assert docs_index.split_page("operations/x.md", _PAGE)[0].audience == "operator"


def test_anchors_are_the_ones_the_doc_link_check_resolves() -> None:
    assert docs_index.slug("Read `help.get`, then **share**!") == "read-helpget-then-share"


def test_a_page_with_no_heading_is_one_section_titled_by_its_file() -> None:
    (only,) = docs_index.split_page("guides/notes.md", "Just text.\n")
    assert (only.title, only.breadcrumb, only.text) == ("notes", (), "Just text.")


def test_an_exact_identifier_finds_the_line_that_names_it() -> None:
    page = "# Settings\n\n## Models\n\n- `LOOM_LLM_MODEL` sets the model.\n- `LOOM_ASSISTANT_MODEL` sets the assistant's model.\n"
    ix = docs_index.build([("reference/settings.md", page)])
    assert "loom_assistant_model" in docs_index.query_terms("What does LOOM_ASSISTANT_MODEL do?")
    (hit,) = docs_index.search(ix, "LOOM_ASSISTANT_MODEL", 1)
    assert hit.snippet.startswith("- `LOOM_ASSISTANT_MODEL`")


def test_search_finds_a_section_by_its_words_folded_and_without_question_words() -> None:
    ix = docs_index.build([("features/sharing.md", _PAGE), ("other.md", "# Other\n\nNothing.")])
    hits = docs_index.search(ix, "How do I grant roles to my team?", 3)
    # The heading names it, which outweighs the text of the section above it.
    assert [h.section.breadcrumb[-1] for h in hits[:2]] == ["Grant a role", "Collections"]
    assert "granting roles" in hits[1].snippet
    # `collections` and `collection` are one term; a question of stop words still searches.
    assert docs_index.query_terms("Collections sharing") == docs_index.query_terms(
        "collection shared"
    )
    assert docs_index.query_terms("what is it") == ["what", "is", "it"]
    assert docs_index.search(ix, "zebra", 3) == []


def test_section_text_holds_its_subsections_and_the_empty_breadcrumb_is_the_page() -> None:
    ix = docs_index.build([("features/sharing.md", _PAGE)])
    first = docs_index.section_text(ix, "features/sharing.md", ("Sharing", "Collections"))
    assert first is not None and "Run grants.create" in first.text
    assert "The same heading again" not in first.text
    page = docs_index.section_text(ix, "features/sharing.md", ())
    assert page is not None and "The same heading again" in page.text
    assert docs_index.section_text(ix, "features/sharing.md", ("Nope",)) is None
    assert docs_index.section_text(ix, "../secrets.md", ()) is None


@pytest.fixture
def shipped_docs(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    (tmp_path / "features").mkdir()
    (tmp_path / "features" / "sharing.md").write_text(_PAGE)
    (tmp_path / "development").mkdir()
    (tmp_path / "development" / "testing.md").write_text("# Testing\n\nRun grants tests.\n")
    monkeypatch.setattr(docs, "DOCS_DIR", tmp_path)
    docs.reset()
    yield tmp_path
    docs.reset()


def test_search_and_get_read_the_shipped_docs_but_not_the_contributor_pages(shipped_docs) -> None:
    out = asyncio.run(docs.handle_help_search(HelpSearchInput(query="grant a role", limit=5)))
    assert out.available
    assert {h.path for h in out.hits} == {"features/sharing.md"}
    top = out.hits[0]
    assert (top.section, top.anchor, top.title) == (
        "Sharing > Collections > Grant a role",
        "grant-a-role",
        "Sharing",
    )
    got = asyncio.run(
        docs.handle_help_get(HelpGetInput(path="docs/features/sharing.md", section=top.section))
    )
    assert got.found and got.path == "features/sharing.md"
    assert got.text.startswith("### Grant a role")
    page = asyncio.run(docs.handle_help_get(HelpGetInput(path="features/sharing.md")))
    assert [e.anchor for e in page.sections] == [
        "sharing",
        "collections",
        "grant-a-role",
        "collections-1",
    ]
    missing = asyncio.run(docs.handle_help_get(HelpGetInput(path="development/testing.md")))
    assert missing.available and not missing.found


def test_a_long_section_is_cut_and_marked(shipped_docs, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(docs, "MAX_TEXT_CHARS", 20)
    got = asyncio.run(docs.handle_help_get(HelpGetInput(path="features/sharing.md")))
    assert got.truncated and len(got.text) == 20
    # A cut section read carries the page's outline, for finding another section.
    cut = HelpGetInput(path="features/sharing.md", section="Sharing > Collections")
    got = asyncio.run(docs.handle_help_get(cut))
    assert got.truncated and got.sections


def test_no_docs_answers_unavailable_for_the_life_of_the_process(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(docs, "DOCS_DIR", tmp_path / "absent")
    docs.reset()
    out = asyncio.run(docs.handle_help_search(HelpSearchInput(query="sharing")))
    assert not out.available and out.hits == []
    # The docs change only with the build: no call reads them again.
    (tmp_path / "absent").mkdir()
    (tmp_path / "absent" / "a.md").write_text("# A\n\nsharing\n")
    assert not asyncio.run(docs.handle_help_search(HelpSearchInput(query="sharing"))).available
    docs.reset()


def test_an_index_that_fails_to_build_answers_unavailable(shipped_docs, monkeypatch) -> None:
    def broken(pages):
        raise ValueError("bad page")

    monkeypatch.setattr(docs_index, "build", broken)
    out = asyncio.run(docs.handle_help_search(HelpSearchInput(query="grant")))
    assert not out.available


def test_a_page_that_cannot_be_read_is_left_out(tmp_path: Path) -> None:
    (tmp_path / "good.md").write_text("# Good\n\nsharing\n")
    (tmp_path / "bad.md").write_bytes(b"# Bad\n\n\xff\xfe sharing\n")
    assert [p for p, _ in docs.pages(tmp_path)] == ["good.md"]


def test_a_section_read_carries_no_outline(shipped_docs) -> None:
    got = asyncio.run(
        docs.handle_help_get(
            HelpGetInput(path="features/sharing.md", section="Sharing > Collections")
        )
    )
    assert got.found and got.sections == []


def test_the_index_is_built_once_off_the_event_loop(shipped_docs, monkeypatch) -> None:
    builds: list[int] = []
    real = docs_index.build
    monkeypatch.setattr(docs_index, "build", lambda pages: builds.append(1) or real(pages))

    async def many() -> None:
        await asyncio.gather(*(docs.index() for _ in range(8)))

    asyncio.run(many())
    assert builds == [1]


def test_a_call_during_the_first_build_waits_for_it(shipped_docs, monkeypatch) -> None:
    started, release = threading.Event(), threading.Event()
    real = docs_index.build

    def slow(pages):
        started.set()
        release.wait(5)
        return real(pages)

    monkeypatch.setattr(docs_index, "build", slow)

    async def two() -> list[bool]:
        first = asyncio.create_task(docs.handle_help_search(HelpSearchInput(query="grant")))
        await asyncio.to_thread(started.wait, 5)
        second = asyncio.create_task(docs.handle_help_search(HelpSearchInput(query="grant")))
        await asyncio.sleep(0.05)
        release.set()
        return [(await first).available, (await second).available]

    assert asyncio.run(two()) == [True, True]


def test_the_repository_docs_index_and_name_their_sections() -> None:
    ix = docs_index.build(docs.pages(DOCS_DIR))
    assert not any(p.startswith("development/") for p in ix.pages)
    hits = docs_index.search(ix, "revoke and rotate a token", 3)
    assert ("guides/tokens-and-grants.md", ("Tokens and grants", "Revoke and rotate")) in {
        (h.section.path, h.section.breadcrumb) for h in hits
    }


def test_the_image_ships_the_docs_without_their_screenshots() -> None:
    assert "COPY docs ./docs" in (REPO_ROOT / "Dockerfile").read_text()
    assert "docs/images/" in (REPO_ROOT / ".dockerignore").read_text().splitlines()


def test_the_help_ops_are_read_only_listed_for_the_assistant_and_in_no_curated_roster() -> None:
    for name in ("help.search", "help.get"):
        op = OPERATIONS_BY_NAME[name]
        assert op.read_only and op.deployment_scoped and op.maturity == "experimental"
        assert name in MCP_ASSISTANT_OPS
        assert name in gates.END_USER_ORG_OPS
        assert not listed_under_all(op)
        assert op_toolsets(name) == ["assistant"]


_CALLERS = {
    "viewer": AuthInfo(
        subject="sa:bot",
        org="acme",
        namespaces=("n",),
        roles=(Role.VIEWER,),
        is_service_account=True,
    ),
    "narrowed-to-one-user": AuthInfo(
        subject="sa:bot",
        org="acme",
        namespaces=("n",),
        roles=(Role.VIEWER,),
        is_service_account=True,
        read_scope="own_user",
        read_user_id="u1",
    ),
    "end-user": AuthInfo(
        subject="user:u1",
        org="acme",
        namespaces=(),
        roles=(Role.VIEWER,),
        is_service_account=False,
        read_scope="own_user",
        read_user_id="u1",
    ),
}


@pytest.mark.parametrize("caller", sorted(_CALLERS))
@pytest.mark.parametrize("name", ["help.search", "help.get"])
def test_every_token_that_may_read_the_guide_may_read_the_docs(caller: str, name: str) -> None:
    auth = _CALLERS[caller]
    op = OPERATIONS_BY_NAME[name]
    target = gates.authz_target(op, org="acme", namespace=None, auth=auth)
    asyncio.run(gates.authorize(op, auth, target, transport="http"))
    body = HelpSearchInput(query="x") if name == "help.search" else HelpGetInput(path="x.md")
    gates.read_scope(op, auth, body)


def test_a_repeated_heading_is_read_back_by_the_anchor_its_hit_names(shipped_docs) -> None:
    out = asyncio.run(docs.handle_help_search(HelpSearchInput(query="the same heading again")))
    hit = out.hits[0]
    assert (hit.section, hit.anchor) == ("Sharing > Collections", "collections-1")
    got = asyncio.run(
        docs.handle_help_get(HelpGetInput(path=hit.path, section=hit.section, anchor=hit.anchor))
    )
    assert got.anchor == "collections-1" and "The same heading again" in got.text
    assert "granting roles" not in got.text and got.sections == []
    unknown = HelpGetInput(path=hit.path, anchor="nope")
    assert not asyncio.run(docs.handle_help_get(unknown)).found


def test_plurals_and_suffixes_fold_to_one_term() -> None:
    for a, b in [
        ("keys", "key"),
        ("rows", "row"),
        ("aliases", "alias"),
        ("biases", "bias"),
        ("settings", "setting"),
        ("embeddings", "embedding"),
        ("embeds", "embed"),
        ("embedded", "embed"),
        ("warnings", "warning"),
        ("branches", "branch"),
        ("policies", "policy"),
    ]:
        assert docs_index.query_terms(a) == docs_index.query_terms(b), (a, b)


def test_a_long_read_is_read_to_its_end_by_offset(shipped_docs, monkeypatch) -> None:
    monkeypatch.setattr(docs, "MAX_TEXT_CHARS", 20)
    ix = asyncio.run(docs.index())
    assert ix is not None
    expected = docs_index.section_text(ix, "features/sharing.md", ())
    assert expected is not None
    read, offset = "", 0
    while True:
        body = HelpGetInput(path="features/sharing.md", offset=offset)
        got = asyncio.run(docs.handle_help_get(body))
        read += got.text
        if not got.truncated:
            assert got.next_offset == 0
            break
        offset = got.next_offset
    assert read == expected.text


def test_every_shipped_section_longer_than_one_read_is_read_to_its_end() -> None:
    # A section past the cap with no subsection to read instead is read on by offset.
    docs.reset()
    ix = asyncio.run(docs.index())
    assert ix is not None
    long = [
        whole
        for s in ix.sections
        if (whole := docs_index.section_text(ix, s.path, s.breadcrumb, s.anchor))
        and len(whole.text) > docs.MAX_TEXT_CHARS
    ]
    assert long, "no shipped section is longer than one read"
    for whole in long:
        read, offset = "", 0
        while True:
            body = HelpGetInput(path=whole.path, anchor=whole.anchor, offset=offset)
            got = asyncio.run(docs.handle_help_get(body))
            read += got.text
            if not got.truncated:
                break
            offset = got.next_offset
        assert read == whole.text, (whole.path, whole.anchor)
    docs.reset()


def test_a_page_read_names_each_heading_as_the_page_writes_it(shipped_docs) -> None:
    (shipped_docs / "features" / "links.md").write_text(
        "# Links\n\nIntro.\n\n## See [the roles](roles.md)\n\nText.\n\nSetext\n------\n\nMore.\n"
    )
    docs.reset()
    page = asyncio.run(docs.handle_help_get(HelpGetInput(path="features/links.md")))
    assert [(e.heading, e.anchor) for e in page.sections] == [
        ("Links", "links"),
        ("See [the roles](roles.md)", "see-the-rolesrolesmd"),
        ("Setext", "setext"),
    ]
