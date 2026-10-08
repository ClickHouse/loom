"""The backfill says what the entry's path prefixes leave
outside the index, and a Markdown section never outranks a definition."""

from __future__ import annotations

import inspect

from server.cli.code_backfill import prefix_coverage_note
from server.code.store.index import CodeRepo

LUMEN_SHAPED = (
    [f"src/f{i}.cpp" for i in range(311)]
    + [f"docs/d{i}.md" for i in range(125)]
    + [f"include/lumen/h{i}.h" for i in range(52)]
    + [f"contrib/c{i}" for i in range(14)]
    + [f"tla/T{i}.tla" for i in range(13)]
    + [f"ci/s{i}.sh" for i in range(10)]
    + ["CMakeLists.txt", "README.md", "bb"]
)
CLICKHOUSE_DEFAULT = ("src/", "base/", "programs/", "tests/", "ci/")


def test_default_prefixes_on_an_lumen_shaped_tree_name_what_they_drop() -> None:
    note = prefix_coverage_note(LUMEN_SHAPED, CLICKHOUSE_DEFAULT)
    assert note.startswith("prefixes leave 207 of 528 files outside the index: ")
    # largest first, root files as their own bucket, the remedy named
    assert "docs/ 125, include/ 52, contrib/ 14, tla/ 13, (root files) 3" in note
    assert "path_prefixes" in note


def test_full_coverage_and_no_prefixes_say_nothing() -> None:
    assert prefix_coverage_note(LUMEN_SHAPED, ()) == ""
    assert prefix_coverage_note(["src/a.cpp", "src/b.cpp"], ("src/",)) == ""


def test_the_long_tail_is_counted_not_listed() -> None:
    paths = [f"d{i}/x.py" for i in range(9)] + ["src/a.cpp"]
    note = prefix_coverage_note(paths, ("src/",), top=6)
    assert note.startswith("prefixes leave 9 of 10 files outside the index: ")
    assert ", +3 more" in note


def test_symbols_by_name_sorts_doc_sections_after_definitions() -> None:
    src = inspect.getsource(CodeRepo.symbols_by_name)
    assert "kind IN ('suite', 'statement', 'section') ASC" in src
