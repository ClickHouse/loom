"""The generated regions of docs/reference/ match the code they are rendered from."""

from __future__ import annotations

import re

import pytest

from scripts.codegen import docs_reference as gen

PAGES = sorted(p for p in gen.REFERENCE.glob("*.md") if gen.REGION.search(p.read_text()))


def test_every_renderer_has_a_page() -> None:
    names = {m.group("name") for p in PAGES for m in gen.REGION.finditer(p.read_text())}
    assert names == set(gen.RENDERERS), names ^ set(gen.RENDERERS)


@pytest.mark.parametrize("page", PAGES, ids=lambda p: p.name)
def test_generated_regions_are_current(page) -> None:
    text = page.read_text(encoding="utf-8")
    assert gen.render_page(text, {}) == text, (
        f"docs/reference/{page.name} is stale: run {gen.COMMAND}"
    )


def test_every_operation_has_a_row() -> None:
    from server.operations import registry

    text = (gen.REFERENCE / "operations.md").read_text(encoding="utf-8")
    missing = [op.name for op in registry.ALL_OPERATIONS if f"| [`{op.name}`](" not in text]
    assert not missing, missing


def test_every_group_field_page_is_current() -> None:
    pages = gen.render_group_pages()
    on_disk = {p.name for p in gen.GROUP_DIR.glob("*.md")}
    assert on_disk == set(pages), on_disk ^ set(pages)
    stale = [n for n, text in pages.items() if (gen.GROUP_DIR / n).read_text() != text]
    assert not stale, f"stale field pages {stale}: run {gen.COMMAND}"


def test_llms_txt_is_current() -> None:
    assert gen.LLMS_TXT.read_text(encoding="utf-8") == gen.render_llms_txt(), (
        f"llms.txt is stale: run {gen.COMMAND}"
    )


def test_listed_under_all_column_matches_the_mcp_listing(monkeypatch) -> None:
    from server.operations import registry
    from server.operations.toolsets import _MCP_HIDDEN_OPS, SYNTHETIC_TOOL_OPS
    from server.settings import settings
    from server.transport.mcp_tools import listed_ops

    monkeypatch.setattr(settings, "mcp_code_tools", True)
    listed = {op.name for op in listed_ops("all")} | (SYNTHETIC_TOOL_OPS - _MCP_HIDDEN_OPS)
    wrong = [
        op.name
        for op in registry.ALL_OPERATIONS
        if gen._listed_under_all(op) != (op.name in listed)
    ]
    assert not wrong, wrong


@pytest.mark.parametrize(
    ("text", "first"),
    [
        ("Turns it on. Read the guide.", "Turns it on."),
        ("Sets a label (e.g. `iso:x`). Every process sets one.", "Sets a label (e.g. `iso:x`)."),
        ("E.g. `ci` or `iso`. Every process sets one.", "E.g. `ci` or `iso`."),
        ("Reads one value, i.e. `x`. Then more.", "Reads one value, i.e. `x`."),
        ("Rows, keys, etc. Writes stay grant-driven.", "Rows, keys, etc."),
    ],
)
def test_first_sentence_skips_e_g_and_i_e_but_may_end_at_etc(text, first) -> None:
    assert gen._first_sentence(text) == first


def test_status_switches_list_what_loads_false_not_what_is_declared_false(monkeypatch) -> None:
    """The conflict resolver is declared false and resolved on at load with a real
    provider; the env of the process that renders the page changes nothing."""
    monkeypatch.setenv("LOOM_MEMORY_CONFLICT_RESOLVER_ENABLED", "false")
    monkeypatch.setenv("LOOM_CODE_ORG_READABLE", "true")
    # Built under this env, not taken from a cache another test filled.
    gen._loaded_defaults.cache_clear()
    try:
        rendered = gen.render_status_switches()
    finally:
        gen._loaded_defaults.cache_clear()
    assert "`LOOM_MEMORY_CONFLICT_RESOLVER_ENABLED`" not in rendered
    assert "| `LOOM_CODE_ORG_READABLE` |" in rendered


def test_status_ops_lists_each_experimental_op_once_and_counts_the_rest() -> None:
    from server.operations import registry

    groups = gen._op_groups()
    rows = {}
    for line in gen.render_status_ops().splitlines():
        m = re.match(r"\| `([a-z_]+)` \| (\d+) \| (.*) \|$", line)
        if m:
            rows[m.group(1)] = (int(m.group(2)), re.findall(r"\[`([^`]+)`\]", m.group(3)))
    assert set(rows) == set(groups)
    for group, (stable, experimental) in rows.items():
        want = [op.name for op in groups[group] if op.maturity == "experimental"]
        assert experimental == want, group
        assert stable == len(groups[group]) - len(want), group
    listed = [name for _, names in rows.values() for name in names]
    assert sum(stable for stable, _ in rows.values()) + len(listed) == len(registry.ALL_OPERATIONS)
