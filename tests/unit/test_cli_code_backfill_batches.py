"""`loom code backfill`'s cursor batches: 200 files each, each with its first index,
and the headers `headers_first` put first end their own batch, so no batch holds both
a header and a source."""

from __future__ import annotations

from server.cli.code_backfill import _cursor_batches


def _shape(batches: list[tuple[int, list[str]]]) -> list[tuple[int, int]]:
    return [(b, len(chunk)) for b, chunk in batches]


def test_no_batch_holds_both_a_header_and_a_source() -> None:
    paths = [f"h{i}.h" for i in range(250)] + [f"s{i}.cpp" for i in range(300)]
    batches = _cursor_batches(paths)
    assert _shape(batches) == [(0, 200), (200, 50), (250, 200), (450, 100)]
    for _b, chunk in batches:
        assert len({p.endswith(".h") for p in chunk}) == 1
    assert [p for _b, chunk in batches for p in chunk] == paths


def test_a_resumed_run_starts_at_its_cursor_wherever_an_earlier_build_cut() -> None:
    paths = [f"h{i}.h" for i in range(250)] + [f"s{i}.cpp" for i in range(300)]
    # 400 is where 200-file batches cut before the boundary was a cut too
    assert _shape(_cursor_batches(paths, 400)) == [(400, 50), (450, 100)]
    assert _shape(_cursor_batches(paths, 250)) == [(250, 200), (450, 100)]
    assert _cursor_batches(paths, 550) == []


def test_a_tree_without_headers_is_cut_every_200_files() -> None:
    paths = [f"f{i}.py" for i in range(450)]
    assert _shape(_cursor_batches(paths)) == [(0, 200), (200, 200), (400, 50)]
    assert _shape(_cursor_batches([f"h{i}.h" for i in range(3)])) == [(0, 3)]
