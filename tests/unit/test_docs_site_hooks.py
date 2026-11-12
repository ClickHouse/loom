"""The documentation site's MkDocs hook: meta description and links that leave docs/."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[2]
_SPEC = importlib.util.spec_from_file_location(
    "docs_site_hooks", ROOT / "scripts/docs_site/hooks.py"
)
assert _SPEC and _SPEC.loader
hooks = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(hooks)


def _page(path: Path, src_uri: str, meta: dict[str, str] | None = None) -> SimpleNamespace:
    return SimpleNamespace(
        file=SimpleNamespace(abs_src_path=str(path), src_uri=src_uri), meta=dict(meta or {})
    )


def test_the_description_is_the_first_sentence_escaped_without_backticks(tmp_path: Path) -> None:
    src = tmp_path / "page.md"
    src.write_text('# Page\n\nA run of `loom` is one task, such as "triage 12" & more.\n')
    page = _page(src, "features/page.md")
    hooks.on_page_markdown("", page, None, None)
    assert (
        page.meta["description"]
        == "A run of loom is one task, such as &quot;triage 12&quot; &amp; more."
    )


def test_a_description_set_in_front_matter_is_kept_and_escaped(tmp_path: Path) -> None:
    src = tmp_path / "page.md"
    src.write_text("# Page\n\nThe first sentence.\n")
    page = _page(src, "features/page.md", {"description": 'Set "by" hand.'})
    hooks.on_page_markdown("", page, None, None)
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
    )
    out = hooks.on_page_markdown(markdown, _page(src, "features/runs.md"), None, None)
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
    assert (
        "[![badge](https://raw.githubusercontent.com/ClickHouse/loom/main/frontend/b.svg)](https://example.com/z)"
        in out
    )
