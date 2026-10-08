"""Every version of a code_chunks key carries the same content.

The grep statements read the n-gram skip index under FINAL with
`use_skip_indexes_if_final_exact_mode = 0`: a granule the
index skips may hold other versions of a kept key only if those versions
carry different content, and on this table they cannot — chunk ids are
content-addressed, tombstones mirror the live row, and moved_of() may only
refresh spans and commits. These tests pin the invariant the setting leans on.
"""

from __future__ import annotations

import pytest

from server.code.domain.index.grep_plan import GREP_FINAL_SETTINGS
from server.code.store.index import VERSION_INVARIANT_COLS, CodeRepo


def _live_chunk() -> dict:
    return {
        "org": "o",
        "namespace": "n",
        "embedding_model": "m",
        "path": "src/a.cpp",
        "chunk_id": "c1",
        "content": "void f() { SettingsQuirks::check(); }",
        "content_hash": "ab" * 32,
        "lexemes": "void f settingsquirks check",
        "start_line": 1,
        "end_line": 3,
        "indexed_commit": "0" * 40,
        "version": 1,
    }


def test_tombstone_mirrors_content_and_only_sets_tombstoned_at() -> None:
    live = _live_chunk()
    tomb = CodeRepo.tombstone_of("code_chunks", live)
    for col in VERSION_INVARIANT_COLS["code_chunks"]:
        assert tomb[col] == live[col], col
    assert tomb["tombstoned_at"] is not None
    assert tomb["version"] > live["version"]


def test_moved_of_refreshes_spans_but_refuses_the_invariant_columns() -> None:
    live = _live_chunk()
    moved = CodeRepo.moved_of(
        "code_chunks", live, start_line=10, end_line=12, indexed_commit="1" * 40
    )
    assert (moved["start_line"], moved["end_line"]) == (10, 12)
    assert moved["content"] == live["content"]
    for col in sorted(VERSION_INVARIANT_COLS["code_chunks"]):
        with pytest.raises(ValueError, match="may not change across versions"):
            CodeRepo.moved_of("code_chunks", live, **{col: "x"})


def test_grep_statements_carry_the_final_settings() -> None:
    assert "use_skip_indexes_if_final_exact_mode = 0" in GREP_FINAL_SETTINGS
    assert "query_plan_direct_read_from_text_index = 0" in GREP_FINAL_SETTINGS
