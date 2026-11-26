"""Package import spellings link to definitions rather than pending file-only anchors."""

from __future__ import annotations

from server.code.domain.anchor import Definition, mentions
from server.memory.app.code_links import _plan


def _definition(root: str) -> Definition:
    return Definition(
        path=f"{root}/pkg/json/__init__.py",
        qualified_name=f"{root}.pkg.json.__init__::dumps",
        name="dumps",
        body_hash="body",
    )


def test_package_name_produces_a_symbol_link_without_a_pending_or_file_link() -> None:
    target = _definition("src")
    linked, waiting, complete = _plan(
        mentions("`pkg.json.dumps` follows the active provider's options."),
        {"dumps": [target]},
        [target.path],
        {target.path: target.path},
    )
    assert linked == [target]
    assert waiting == []
    assert complete


def test_ambiguous_package_names_wait_for_no_nonexistent_definition() -> None:
    targets = [_definition("src"), _definition("other")]
    names = mentions("`pkg.json.dumps` follows the active provider's options.")
    linked, waiting, complete = _plan(names, {"dumps": targets}, [], {})
    assert linked == []
    assert waiting == []
    assert complete
    assert _plan(names, {"dumps": targets}, [targets[0].path], {})[0] == targets[:1]
