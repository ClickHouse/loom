from __future__ import annotations

from typing import Any

import pytest

from server.code.api.models_grep import CodeGrepInput, GrepDependency
from server.code.app.grep_dependencies import _allowed, _one_helper
from server.code.domain.search_scope import build_scope


def test_related_definition_uses_grep_glob_semantics_for_newline_paths() -> None:
    path = "pkg/lib\nhi.py"
    args = CodeGrepInput(pattern="call(obj.attr)")
    assert _allowed(path, args, build_scope(path_globs=["pkg/**"]))
    assert not _allowed(path, args, build_scope(exclude_globs=["pkg/**"]))


@pytest.mark.asyncio
async def test_scip_property_reference_cannot_witness_a_separate_same_name_call() -> None:
    revision = "a" * 40
    source = GrepDependency(
        qualified_name="pkg.source::value",
        path="pkg/source.py",
        start_line=1,
        end_line=2,
        body="def value():\n    return other.helper + helper()",
    )

    class Repo:
        async def edges_for_symbols(self, *args, **kwargs):
            return [
                {
                    "tier": "scip",
                    "path": source.path,
                    "line": 2,
                    "indexed_commit": revision,
                    "from_qualified_name": source.qualified_name,
                    "to_qualified_name": "pkg.other::Other::helper",
                    "to_symbol_id": "property",
                }
            ]

    repo: Any = Repo()
    assert await _one_helper(repo, "o", "n", {"symbol_id": "source"}, source, revision) is None
