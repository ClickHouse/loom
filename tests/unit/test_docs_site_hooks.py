"""The documentation site's MkDocs hook: meta description, links that leave docs/, and the
Markdown and llms files it writes after the build."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts.codegen.page_summary import LEAD_PAGES

ROOT = Path(__file__).resolve().parents[2]
_SPEC = importlib.util.spec_from_file_location(
    "docs_site_hooks", ROOT / "scripts/docs_site/hooks.py"
)
assert _SPEC and _SPEC.loader
hooks = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(hooks)


_CONFIG = {"site_url": "https://example.com/loom/"}


@pytest.mark.parametrize("homepage", [True, False])
def test_page_metadata_identifies_the_software_or_document(homepage: bool) -> None:
    config = {**_CONFIG, "site_name": "ClickHouse Loom", "site_description": "Agent memory."}
    page = SimpleNamespace(
        title="Memory &amp; <code>code</code>",
        is_homepage=homepage,
        meta={"description": "Memory &amp; code."},
        canonical_url=_CONFIG["site_url"] + ("" if homepage else "concepts/memory/"),
    )
    context = hooks.on_page_context({"existing": True}, page, config, None)
    assert context["existing"] is True
    graph = context["loom_schema"]["@graph"]
    assert graph[0]["description"] == "Memory & code."
    if homepage:
        assert context["loom_title"] == (
            "ClickHouse Loom: agent memory, document search and code index"
        )
        assert graph[0]["@type"] == ["SoftwareApplication", "SoftwareSourceCode"]
        assert graph[0]["codeRepository"] in graph[0]["sameAs"]
        assert graph[0]["@id"] == _CONFIG["site_url"] + "#software"
    else:
        assert context["loom_title"] == "Memory & code - ClickHouse Loom"
        assert graph[0]["headline"] == "Memory & code"
        assert graph[0]["about"]["@id"] == _CONFIG["site_url"] + "#software"
        assert graph[1]["@type"] == "BreadcrumbList"
        assert [item["item"] for item in graph[1]["itemListElement"]] == [
            _CONFIG["site_url"],
            page.canonical_url,
        ]


def test_an_explicit_long_title_stays_intact_without_the_site_suffix() -> None:
    heading = "A page title about " + "code " * 16
    config = {**_CONFIG, "site_name": "ClickHouse Loom", "site_description": "Agent memory."}
    page = SimpleNamespace(
        title="Ignored",
        is_homepage=False,
        meta={"title": heading},
        canonical_url=_CONFIG["site_url"] + "long/",
    )
    context = hooks.on_page_context({}, page, config, None)
    assert context["loom_title"] == heading


def _page(path: Path, src_uri: str, meta: dict[str, str] | None = None) -> SimpleNamespace:
    return SimpleNamespace(
        file=SimpleNamespace(abs_src_path=str(path), src_uri=src_uri), meta=dict(meta or {})
    )


def test_the_description_is_the_first_sentence_escaped_without_backticks(tmp_path: Path) -> None:
    src = tmp_path / "page.md"
    src.write_text('# Page\n\nA run of `loom` is one task, such as "triage 12" & more.\n')
    page = _page(src, "features/page.md")
    hooks.on_page_markdown("", page, _CONFIG, None)
    assert (
        page.meta["description"]
        == "A run of loom is one task, such as &quot;triage 12&quot; &amp; more."
    )


def test_a_long_description_ends_on_a_whole_word_within_the_limit(tmp_path: Path) -> None:
    src = tmp_path / "page.md"
    src.write_text("# Page\n\nA " + "alpha beta gamma " * 20 + "end.\n")
    page = _page(src, "features/page.md")
    hooks.on_page_markdown("", page, _CONFIG, None)
    # A plain 159-character slice would end inside "alpha"; the cut keeps whole words.
    assert page.meta["description"] == "A " + ("alpha beta gamma " * 9).rstrip() + "…"


def test_a_description_set_in_front_matter_is_kept_and_escaped(tmp_path: Path) -> None:
    src = tmp_path / "page.md"
    src.write_text("# Page\n\nThe first sentence.\n")
    page = _page(src, "features/page.md", {"description": 'Set "by" hand.'})
    hooks.on_page_markdown("", page, _CONFIG, None)
    assert page.meta["description"] == "Set &quot;by&quot; hand."


def test_links_outside_docs_point_at_github_and_others_stay(tmp_path: Path) -> None:
    src = tmp_path / "page.md"
    src.write_text("# Page\n\nText.\n")
    markdown = (
        "[a](../../server/x.py#L1) [b](memory/writing.md#key) [c](https://example.com/y) "
        '[d](../../server/y.py "title") ![e](../../frontend/a.png) `[f](../../server/z.py)`\n'
        "```\n[g](../../server/w.py)\n```\n"
        "[`schema.sql`](../../schemas/schema.sql)\n"
        "- item\n\n  ```\n  [h](../../server/v.py)\n  ```\n"
        "~~~\n[i](../../server/u.py)\n~~~\n"
        "[![badge](../../frontend/b.svg)](https://example.com/z)\n"
        "````md\n```python\n[j](../../server/j.py)\n```\n[k](../../server/k.py)\n````\n"
        "[same](#anchor)\n"
    )
    out = hooks.on_page_markdown(markdown, _page(src, "features/runs.md"), _CONFIG, None)
    assert "[a](https://github.com/ClickHouse/loom/blob/main/server/x.py#L1)" in out
    assert "[b](memory/writing.md#key)" in out
    assert "[c](https://example.com/y)" in out
    assert '[d](https://github.com/ClickHouse/loom/blob/main/server/y.py "title")' in out
    assert "![e](https://raw.githubusercontent.com/ClickHouse/loom/main/frontend/a.png)" in out
    assert "`[f](../../server/z.py)`" in out
    assert "[g](../../server/w.py)" in out
    assert "[`schema.sql`](https://github.com/ClickHouse/loom/blob/main/schemas/schema.sql)" in out
    assert "  [h](../../server/v.py)" in out
    assert "[i](../../server/u.py)" in out
    assert "[j](../../server/j.py)" in out and "[k](../../server/k.py)" in out
    assert "[same](#anchor)" in out
    assert (
        "[![badge](https://raw.githubusercontent.com/ClickHouse/loom/main/frontend/b.svg)](https://example.com/z)"
        in out
    )


def test_a_page_without_a_plain_paragraph_gets_no_description(tmp_path: Path) -> None:
    src = tmp_path / "page.md"
    src.write_text("# Page\n\n| a | b |\n\n- a list\n")
    page = _page(src, "features/page.md")
    hooks.on_page_markdown("", page, _CONFIG, None)
    assert "description" not in page.meta


def test_a_description_that_leads_into_a_list_ends_with_a_full_stop(tmp_path: Path) -> None:
    src = tmp_path / "page.md"
    src.write_text("# Page\n\nLoom reports on itself in three layers:\n\n- one\n")
    page = _page(src, "features/page.md")
    hooks.on_page_markdown("", page, _CONFIG, None)
    assert page.meta["description"] == "Loom reports on itself in three layers."


def test_only_a_wholly_generated_page_loses_its_edit_button(tmp_path: Path) -> None:
    src = tmp_path / "page.md"
    src.write_text("# Page\n\nText.\n")
    whole = _page(src, "reference/page.md")
    whole.edit_url = "https://example.com/edit"
    hooks.on_page_markdown(
        "<!-- Generated by `x`; edit the models, not this page. -->\n", whole, _CONFIG, None
    )
    assert whole.edit_url is None
    config_page = _page(src, "reference/configuration.md")
    config_page.edit_url = "https://example.com/edit"
    hooks.on_page_markdown(
        "# Configuration\n\nThis page is generated from the comments.\n", config_page, _CONFIG, None
    )
    assert config_page.edit_url is None
    quoted = _page(src, "guides/page.md")
    quoted.edit_url = "https://example.com/edit"
    hooks.on_page_markdown(
        'A generator page says "edit the models, not this page".\n', quoted, _CONFIG, None
    )
    assert quoted.edit_url == "https://example.com/edit"
    region = _page(src, "reference/other.md")
    region.edit_url = "https://example.com/edit"
    hooks.on_page_markdown(
        "Prose.\n<!-- Generated by `x`; edit the source, not this region. -->\n",
        region,
        _CONFIG,
        None,
    )
    assert region.edit_url == "https://example.com/edit"


def test_top_level_absolute_mailto_and_wrapped_code_span_links(tmp_path: Path) -> None:
    src = tmp_path / "page.md"
    src.write_text("# Page\n\nText.\n")
    markdown = (
        "[r](../README.md) [s](/abs/x) [m](mailto:a@b.c) "
        "see `loom\nmint-token [w](../server/w.py)` and [v](../server/v.py) "
        "``x `[d](../server/d.py)` y``\n"
    )
    out = hooks.on_page_markdown(markdown, _page(src, "index.md"), _CONFIG, None)
    assert "[r](https://github.com/ClickHouse/loom/blob/main/README.md)" in out
    assert "[s](/abs/x)" in out and "[m](mailto:a@b.c)" in out
    assert "[w](../server/w.py)" in out
    assert "``x `[d](../server/d.py)` y``" in out
    assert "[v](https://github.com/ClickHouse/loom/blob/main/server/v.py)" in out


def test_the_sidebar_follows_the_section_order_with_index_and_overview_first() -> None:
    def page(uri: str) -> SimpleNamespace:
        return SimpleNamespace(is_page=True, is_section=False, file=SimpleNamespace(src_uri=uri))

    def section(title: str, *children: SimpleNamespace) -> SimpleNamespace:
        return SimpleNamespace(is_page=False, is_section=True, title=title, children=list(children))

    nav = SimpleNamespace(
        items=[
            page("faq.md"),
            section(
                "Reference",
                section(
                    "Operations",
                    page("reference/operations/b.md"),
                    page("reference/operations/overview.md"),
                ),
                page("reference/cli.md"),
                page("reference/index.md"),
            ),
            page("index.md"),
            section(
                "Getting started",
                page("getting-started/agent-setup.md"),
                page("getting-started/quickstart.md"),
                page("getting-started/overview.md"),
            ),
        ]
    )
    hooks.on_nav(nav, None, None)
    order = [getattr(i, "title", None) or i.file.src_uri for i in nav.items]
    assert order == ["index.md", "Getting started", "Reference", "faq.md"]
    started = [c.file.src_uri for c in nav.items[1].children]
    assert started == [
        "getting-started/overview.md",
        "getting-started/quickstart.md",
        "getting-started/agent-setup.md",
    ]
    reference = [getattr(c, "title", None) or c.file.src_uri for c in nav.items[2].children]
    assert reference == ["reference/index.md", "reference/cli.md", "Operations"]
    # A nested folder is sorted too: its overview.md leads it.
    operations = [c.file.src_uri for c in nav.items[2].children[2].children]
    assert operations == ["reference/operations/overview.md", "reference/operations/b.md"]


def test_previous_and_next_links_follow_the_sorted_sidebar() -> None:
    def page(uri: str) -> SimpleNamespace:
        return SimpleNamespace(
            is_page=True,
            is_section=False,
            file=SimpleNamespace(src_uri=uri),
            previous_page=None,
            next_page=None,
        )

    faq, home, overview = page("faq.md"), page("index.md"), page("getting-started/overview.md")
    started = SimpleNamespace(
        is_page=False, is_section=True, title="Getting started", children=[overview]
    )
    nav = SimpleNamespace(items=[faq, started, home], pages=[faq, overview, home])
    hooks.on_nav(nav, None, None)
    assert nav.pages == [home, overview, faq]
    assert home.previous_page is None and home.next_page is overview
    assert overview.previous_page is home and overview.next_page is faq
    assert faq.previous_page is overview and faq.next_page is None


def _site_tree(tmp_path: Path) -> tuple[Path, Path]:
    root = tmp_path / "repo"
    docs = root / "docs"
    for rel, text in {
        "index.md": "# Loom\n\nThe map. See [overview](getting-started/overview.md).\n",
        "getting-started/agent-setup.md": "# Agent setup\n\nFor an agent.\n",
        "getting-started/overview.md": (
            "# Overview\n\nLoom is a server. [Memory](../concepts/memory.md#keys), "
            "[the README](../../README.md), ![shot](../images/a.png).\n"
        ),
        "getting-started/quickstart.md": "# Quickstart\n\nBoot it. [Below](#boot).\n\n## Boot\n",
        "concepts/memory.md": "# Memory\n\nA memory is a row.\n",
        "use-cases/agent-memory.md": "# Agent memory\n\nMemory for agents.\n",
        "use-cases/index.md": "# Use cases\n\nOne page per need.\n",
        "reference/cli.md": "# CLI\n\nEvery command.\n",
        "reference/operations/memory.md": "# memory\n\nFields.\n",
        "faq.md": "# FAQ\n\nQuestions.\n",
        "glossary.md": "# Glossary\n\nTerms.\n",
    }.items():
        (docs / rel).parent.mkdir(parents=True, exist_ok=True)
        (docs / rel).write_text(text)
    (root / "llms.txt").write_text(
        "# ClickHouse Loom\n\n> Summary.\n\nStart with docs/index.md, or [install](llms-install.md). "
        "Not a page: "
        "https://example.org/docs/x.md, agent-docs/y.md.\n\n## Getting started\n\n"
        "- [Overview](docs/getting-started/overview.md): Loom is a server.\n\n"
        "## Use cases\n\n- [Agent memory](docs/use-cases/agent-memory.md): Memory for agents.\n\n"
        "## Optional\n\n- [CLI](docs/reference/cli.md): Every command.\n"
    )
    (root / "llms-install.md").write_text(
        "# Install\n\nPeople read [the site](https://example.com/loom/getting-started/quickstart/)."
        "\n\nSee [quickstart](docs/getting-started/quickstart.md), "
        "[clients](https://example.com/loom/features/clients/#cursor) and "
        "[the installer](install.sh).\n"
    )
    site = tmp_path / "site"
    site.mkdir()
    return root, site


def test_the_site_serves_each_page_as_markdown_and_llms_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, site = _site_tree(tmp_path)
    monkeypatch.setattr(hooks, "ROOT", root)
    url = _CONFIG["site_url"]
    hooks.on_post_build({"site_dir": str(site), "site_url": url})

    # Each page beside its HTML page and inside its folder, headed by its HTML URL, with
    # links to other pages absolute and links that leave docs/ pointing at GitHub.
    overview = (site / "getting-started/overview.md").read_text()
    assert overview == (site / "getting-started/overview/index.html.md").read_text()
    assert (site / "use-cases/index.html.md").exists()
    # A section index is also at its URL with the slash swapped for `.md`.
    assert (site / "use-cases.md").read_text() == (site / "use-cases/index.md").read_text()
    # The title stays first; the line naming the HTML page is quoted under it.
    assert overview.startswith(
        f"# Overview\n\n> Source: {url}getting-started/overview/ (Markdown: "
    )
    assert f"[Memory]({url}concepts/memory.md#keys)" in overview
    assert "[the README](https://github.com/ClickHouse/loom/blob/main/README.md)" in overview
    assert (site / "reference/operations/memory.md").exists()
    assert (
        (site / "index.md")
        .read_text()
        .startswith(f"# Loom\n\n> Source: {url} (Markdown: {url}index.md)")
    )

    holds = "Getting started, Use cases and Concepts pages, the FAQ and the glossary"
    llms = (site / "llms.txt").read_text()
    assert f"Start with {url}index.md, or [install]({url}llms-install.md)." in llms
    assert "- [Overview](https://example.com/loom/getting-started/overview.md)" in llms
    # Only a docs/ path of its own is rewritten, not the tail of a URL or a longer path.
    assert "https://example.org/docs/x.md, agent-docs/y.md." in llms
    # llms-full.txt is a section of its own, right after the use cases.
    cases = f"- [Agent memory]({url}use-cases/agent-memory.md): Memory for agents.\n\n"
    assert f"{cases}## Full text\n\n- [llms-full.txt]({url}llms-full.txt): The {holds}" in llms
    assert f"The full text of the {holds} is in one file: [llms-full.txt]" in llms.split("##")[0]
    assert llms.index("## Full text") < llms.index("## Optional")
    assert "\n\n\n" not in llms

    install = (site / "llms-install.md").read_text()
    assert f"[quickstart]({url}getting-started/quickstart.md)" in install
    assert "[the installer](https://github.com/ClickHouse/loom/blob/main/install.sh)" in install
    # A link to a site page goes to its Markdown copy, as in llms.txt.
    assert f"[clients]({url}features/clients.md#cursor)" in install
    # An absolute link in the opening paragraph, which is for people, keeps the HTML page.
    assert f"[the site]({url}getting-started/quickstart/)" in install

    full = (site / "llms-full.txt").read_text()
    assert full.startswith("# ClickHouse Loom\n\n> Summary.\n\n")
    # llms.txt's opening paragraph, with where to start and how to install, comes next,
    # without its sentence naming llms-full.txt itself.
    opening = full.split("\n\n")[2]
    assert opening.startswith(f"Start with {url}index.md, or [install]({url}llms-install.md).")
    assert "llms-full.txt" not in opening
    assert f"{url}llms.txt lists the other pages" in full
    assert f"This file holds the {holds} in full" in full
    sources = [line for line in full.splitlines() if line.startswith("> Source: ")]
    # What Loom is for comes first, the setup steps last: fetchers often cut a file short.
    assert sources == [
        f"> Source: {url}{html} (Markdown: {url}{md})"
        for html, md in (
            ("getting-started/overview/", "getting-started/overview.md"),
            ("use-cases/", "use-cases/index.md"),
            ("use-cases/agent-memory/", "use-cases/agent-memory.md"),
            ("faq/", "faq.md"),
            ("glossary/", "glossary.md"),
            ("concepts/memory/", "concepts/memory.md"),
            ("getting-started/quickstart/", "getting-started/quickstart.md"),
            ("getting-started/agent-setup/", "getting-started/agent-setup.md"),
        )
    ]
    # One file holds every page, so links between pages are absolute.
    assert f"[Memory]({url}concepts/memory.md#keys)" in full
    assert f"![shot]({url}images/a.png)" in full
    assert "Every command." not in full
    # A heading on the same page names its page, since the file holds many pages.
    assert f"[Below]({url}getting-started/quickstart.md#boot)" in full
    assert "\n\n\n" not in overview


def test_links_to_the_root_files_the_site_serves_point_at_the_site(tmp_path: Path) -> None:
    src = tmp_path / "page.md"
    src.write_text("# Page\n\nText.\n")
    markdown = "[a](../llms.txt) [b](../llms-install.md) [c](../README.md)\n"
    out = hooks.on_page_markdown(markdown, _page(src, "index.md"), _CONFIG, None)
    assert "[a](https://example.com/loom/llms.txt)" in out
    assert "[b](https://example.com/loom/llms-install.md)" in out
    assert "[c](https://github.com/ClickHouse/loom/blob/main/README.md)" in out


def test_the_pages_named_for_llms_full_and_the_sidebar_exist() -> None:
    docs = ROOT / "docs"
    for entry in (*hooks._FULL_FIRST, *hooks._FULL_LAST, *LEAD_PAGES):
        # A renamed page would otherwise fall back to the default position unnoticed.
        assert (docs / entry).is_dir() if entry.endswith("/") else (docs / entry).is_file(), entry


def test_every_page_named_for_llms_full_is_in_it() -> None:
    # A page `_FULL_FIRST` or `_FULL_LAST` names outside the full sections would be ordered
    # but never included.
    pages = hooks._full_text_pages(ROOT / "docs")
    for entry in (*hooks._FULL_FIRST, *hooks._FULL_LAST):
        if entry.endswith("/"):
            assert any(p.startswith(entry) for p in pages), entry
        else:
            assert entry in pages, entry


@pytest.mark.parametrize("literal", ["script", "style"])
def test_rendered_anchor_inventory_handles_escaped_raw_text_literals(literal):
    rendered = (
        f"<p><code>&lt;{literal}&gt;</code></p>"
        '<a id="ingestion"></a><h2 id="native-heading">Native heading</h2>'
        '<a href="#ingestion">Go</a>'
    )
    page = SimpleNamespace(present_anchor_ids={"native-heading"})
    assert hooks.on_page_content(rendered, page, None, None) == rendered
    assert page.present_anchor_ids == {"ingestion", "native-heading"}


def test_rendered_anchor_inventory_does_not_accept_code_comments_or_script_text():
    rendered = (
        '<pre><code>&lt;a id="code-only"&gt;&lt;/a&gt;</code></pre>'
        '<!-- <a id="comment-only"></a> -->'
        "<script>const text = '<a id=\"script-only\"></a>';</script>"
        '<a href="#missing">Missing</a><a name="legacy"></a><div id="real"></div>'
    )
    page = SimpleNamespace(present_anchor_ids={"code-only", "missing"})
    assert hooks.on_page_content(rendered, page, None, None) == rendered
    assert page.present_anchor_ids == {"legacy", "real"}
