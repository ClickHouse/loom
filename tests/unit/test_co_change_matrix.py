from server.code.app.index.matrix import (
    Pair,
    filter_pairs,
    is_trivial_pair,
    render_matrix,
)


def test_header_implementation_pairs_and_docs_are_dropped() -> None:
    assert is_trivial_pair("src/A/X.h", "src/A/X.cpp")
    assert not is_trivial_pair("src/A/X.h", "src/B/X.cpp")
    assert not is_trivial_pair("src/Core/Settings.cpp", "src/Core/SettingsChangesHistory.cpp")
    pairs = [
        Pair("src/A/X.h", "src/A/X.cpp", 20, 20, 1.0),
        Pair("src/Core/Settings.cpp", "src/Core/SettingsChangesHistory.cpp", 79, 146, 0.54),
        Pair("src/Core/Settings.cpp", "docs/en/settings.md", 10, 146, 0.5),
        Pair("tests/queries/x.sql", "src/Core/Settings.cpp", 8, 8, 1.0),
    ]
    kept = filter_pairs(pairs)
    assert [(p.a, p.b) for p in kept] == [
        ("src/Core/Settings.cpp", "src/Core/SettingsChangesHistory.cpp")
    ]


def test_render_groups_by_source_with_inline_evidence() -> None:
    pairs = [
        Pair("src/Core/Settings.cpp", "src/Core/SettingsChangesHistory.cpp", 79, 146, 0.54),
        Pair("src/Core/Settings.cpp", "src/Interpreters/Context.cpp", 31, 146, 0.51),
        Pair("src/Access/SettingsConstraints.cpp", "src/Interpreters/Context.cpp", 20, 20, 1.0),
    ]
    out = render_matrix(pairs, prs=3000, head="a10d277eb30d")
    assert out.startswith("## Maintenance matrix")
    assert "last 3,000 merged PRs as of a10d277eb30d" in out
    rows = [ln for ln in out.splitlines() if ln.startswith("| `")]
    # most-evidenced source first, partners ordered by support
    assert rows[0].startswith("| `src/Core/Settings.cpp` |")
    assert "`src/Core/SettingsChangesHistory.cpp`<br>`src/Interpreters/Context.cpp`" in rows[0]
    assert "79/146 (54%)<br>31/146 (51%)" in rows[0]
    assert rows[1].startswith("| `src/Access/SettingsConstraints.cpp` |")
    assert "20/20 (100%)" in rows[1]


def test_render_empty_is_explicit() -> None:
    assert "_no pair above the floors_" in render_matrix([], prs=100)


def test_render_caps_rows_and_says_how_many_it_left_out() -> None:
    pairs = [Pair(f"src/f{i}.cpp", f"src/g{i}.cpp", 10 - i, 20, 0.5) for i in range(6)]
    out = render_matrix(pairs, prs=100, max_rows=2)
    rows = [ln for ln in out.splitlines() if ln.startswith("| `")]
    assert [r.split("`")[1] for r in rows] == ["src/f0.cpp", "src/f1.cpp"]
    assert "_4 more source files below the row cap" in out
    assert "more source files" not in render_matrix(pairs, prs=100, max_rows=0)
