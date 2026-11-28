"""code.trace frame parsing + code.impact's diff shape.

Parsers only need unit coverage here (the CH legs are exercised through the
handler integration path); the properties pinned are the ones agents rely
on: frame ORDER is preserved, stdlib noise is dropped, hunk ranges narrow
whole files to the functions actually edited, and a co-change partner needs
2+ shared PRs before it may be called "missing"."""

from __future__ import annotations

from server.code.app.symbol.diff import (
    Hunk,
    Insertion,
    _parse_unified_diff_full,
    _touched_rows_for_diff,
)
from server.code.app.symbol.trace import _parse_trace_frames

_GDB_TRACE = """
#0  0x00007f in __GI_raise (sig=6) at raise.c:50
#1  0x000055 in DB::MemoryTracker::allocImpl(long, bool) ()
#2  0x000055 in DB::ProcessList::insert(...) at src/Interpreters/ProcessList.cpp:221
#3  0x000055 in std::__1::vector<int>::push_back(int const&) ()
"""

_FATAL_LOG = """
2026.09.07 12:00:00.000 [ 42 ] {} <Fatal> BaseDaemon: ########################################
3. DB::Exception::Exception(...) @ 0x000000001 in /usr/bin/clickhouse
4. DB::InterpreterSelectQuery::execute() @ 0x000000002 in /usr/bin/clickhouse
"""


def test_gdb_frames_ordered_and_filtered() -> None:
    frames = _parse_trace_frames(_GDB_TRACE, 10)
    names = [f["frame"] for f in frames]
    # stdlib/raise noise dropped; symbol order preserved; file:line kept
    assert names[0] == "DB::MemoryTracker::allocImpl"
    assert names[1] == "DB::ProcessList::insert"
    assert "src/Interpreters/ProcessList.cpp:221" in names
    assert not any(n.startswith("std::") for n in names)


def test_fatal_log_frames() -> None:
    names = [f["frame"] for f in _parse_trace_frames(_FATAL_LOG, 10)]
    assert names == ["DB::Exception::Exception", "DB::InterpreterSelectQuery::execute"]


def test_frames_dedupe_and_cap() -> None:
    trace = "\n".join(["at DB::A::f() from DB::B::g()"] * 30)
    frames = _parse_trace_frames(trace, 3)
    assert [f["frame"] for f in frames] == ["DB::A::f", "DB::B::g"]


def test_diff_parse_new_side_ranges() -> None:
    diff = """\
--- a/src/A.cpp
+++ b/src/A.cpp
@@ -10,4 +12,6 @@ void f()
 ctx
+added
@@ -50 +58 @@
-x
+y
--- a/src/Gone.cpp
+++ /dev/null
@@ -1,3 +0,0 @@
"""
    ranges, _, _ = _parse_unified_diff_full(diff)
    assert ranges["src/A.cpp"] == [(12, 17), (58, 58)]
    assert "Gone.cpp" not in str(set(ranges) - {"src/A.cpp"})


def test_a_hunk_shorter_than_its_counts_does_not_swallow_the_next_file() -> None:
    """A hunk with fewer lines than its header counts ends at the next file's header."""
    one = "--- a/{p}\n+++ b/{p}\n@@ -{n},2 +{n},3 @@ f()\n+x\n"
    diff = one.format(p="src/A.cpp", n=12) + one.format(p="tests/t.sql", n=1) + "+++ b/src/B.cpp\n"
    ranges, _, changes = _parse_unified_diff_full(diff)
    assert list(ranges) == ["src/A.cpp", "tests/t.sql", "src/B.cpp"]
    assert changes["src/A.cpp"] == [Hunk("f", (), (Insertion(11),))]
    assert changes["src/B.cpp"] == []


def test_diff_parse_tolerates_garbage() -> None:
    assert _parse_unified_diff_full("not a diff\n@@ nonsense @@\n")[0] == {}


_CONTEXT_DIFF = """\
diff --git a/pkg/m.py b/pkg/m.py
--- a/pkg/m.py
+++ b/pkg/m.py
@@ -8,9 +8,10 @@ def before(x):
     return x
 
 
 def edited(y):
-    a = 1
-    b = 2
+    a = 2
     return a
+
+NEW = 1
 
 
@@ -40,3 +41,3 @@ def other(z):
 def decorated():
--- a removed line that reads like a file header
+++ and its replacement
     pass
"""


def test_diff_parse_old_side_changes_skip_context_lines() -> None:
    """The index holds the revision a diff starts from, so the changed lines are counted
    on the old side: a replaced run is its removed lines, an added run with nothing
    removed is an insertion after the old line above it, context lines are neither."""
    _, names, changes = _parse_unified_diff_full(_CONTEXT_DIFF)
    first, second = changes["pkg/m.py"]
    assert names["pkg/m.py"] == {"before", "other"}
    assert first == Hunk("before", ((12, 13),), (Insertion(14),))
    assert second == Hunk("other", ((41, 41),), ())


def test_a_hunk_sent_without_its_lines_changes_what_its_header_says() -> None:
    """The hooks send headers only, from a zero-context diff: `-12,3` replaces lines 12 to
    14 and `-20,0` inserts after line 20, which is what the same hunks say with lines."""
    headers = (
        "+++ b/a.py\n@@ -12,3 +12 @@ f(\n@@ -20,0 +19,2 @@\n--- a/b.py\n+++ b/b.py\n@@ -7 +7 @@\n"
    )
    whole = (
        "+++ b/a.py\n@@ -12,3 +12 @@ f(\n-a\n-b\n-c\n+d\n@@ -20,0 +19,2 @@\n+e\n+f\n"
        "--- a/b.py\n+++ b/b.py\n@@ -7 +7 @@\n-g\n+h\n"
    )
    _, _, sent = _parse_unified_diff_full(headers)
    assert sent == {
        "a.py": [Hunk("f", ((12, 14),), ()), Hunk("", (), (Insertion(20),))],
        "b.py": [Hunk("", ((7, 7),), ())],
    }
    assert _parse_unified_diff_full(whole)[2] == sent


def test_added_text_says_whether_it_decorates_or_continues_the_line_above() -> None:
    """With its lines, a hunk says what an insertion is: decorators only, or text indented
    as deep as the old line above it. A hunk sent without its lines says neither."""
    diff = """\
+++ b/pkg/m.py
@@ -9,2 +9,3 @@
 
+@cached
 def f():
@@ -20,2 +21,4 @@ def f():
     return 1
+    # appended to f
+    x = 2
 
@@ -29,2 +32,5 @@
 
+@cached
+def new():
+    pass
 def g():
"""
    _, _, changes = _parse_unified_diff_full(diff)
    assert [h.inserted for h in changes["pkg/m.py"]] == [
        (Insertion(9, decorates=True),),
        (Insertion(20, continues=True),),
        # a decorated function added above g is not a decorator of g
        (Insertion(29),),
    ]


def _row(sid: str, name: str, start: int, end: int, kind: str = "function") -> dict:
    return {
        "symbol_id": sid,
        "qualified_name": name,
        "path": "pkg/m.py",
        "start_line": start,
        "end_line": end,
        "tier": "treesitter",
        "kind": kind,
    }


_ROWS = [
    _row("c", "m::C", 10, 60, "class"),
    _row("a", "m::C::as_sql", 12, 20),
    _row("b", "m::C::other", 22, 30),
    _row("n", "m::C::other::inner", 24, 26),
    _row("d", "m::D", 70, 90, "class"),
    _row("e", "m::D::as_sql", 72, 80),
]


def _touched(*hunks: Hunk) -> tuple[list[str], int]:
    rows, _, unmapped = _touched_rows_for_diff(_ROWS, {"pkg/m.py": list(hunks)})
    return [r["qualified_name"] for r in rows], unmapped


def test_a_changed_line_touches_the_innermost_definition_only() -> None:
    # a header naming `as_sql` selects neither same-named method: the line decides
    assert _touched(Hunk("as_sql", ((25, 25),), ())) == (["m::C::other::inner"], 0)
    assert _touched(Hunk("as_sql", ((23, 23),), ())) == (["m::C::other"], 0)
    # a class attribute between two methods is the class's own line
    assert _touched(Hunk("as_sql", ((21, 21),), ())) == (["m::C"], 0)
    # a removed range is each of its lines: the method's tail, the class line, the next method
    assert _touched(Hunk("", ((19, 22),), ())) == (["m::C", "m::C::as_sql", "m::C::other"], 0)


def test_an_insertion_sits_in_the_definition_that_holds_both_lines_around_it() -> None:
    assert _touched(Hunk("", (), (Insertion(15),))) == (["m::C::as_sql"], 0)
    # after a method's last line: between that method and the next, so the class
    assert _touched(Hunk("", (), (Insertion(20),))) == (["m::C"], 0)
    # above a method, the line is the class's: a method added there is not the one below
    assert _touched(Hunk("", (), (Insertion(11),))) == (["m::C"], 0)
    # directly above a top-level definition and inside no other: a new definition the
    # index does not hold, so the one below is not touched
    assert _touched(Hunk("", (), (Insertion(69),))) == ([], 1)


def test_added_text_places_an_insertion_the_line_numbers_cannot() -> None:
    # decorators edit the definition that starts on the next line
    assert _touched(Hunk("", (), (Insertion(69, decorates=True),))) == (["m::D"], 0)
    assert _touched(Hunk("", (), (Insertion(11, decorates=True),))) == (["m::C::as_sql"], 0)
    # Python text as deep as the line above continues the definition that line ends: the
    # method, and for two definitions ending on one line the inner one
    assert _touched(Hunk("", (), (Insertion(20, continues=True),))) == (["m::C::as_sql"], 0)
    assert _touched(Hunk("", (), (Insertion(26, continues=True),))) == (["m::C::other::inner"], 0)
    # where indentation is not the block structure it says nothing
    rows = [{**r, "path": "src/m.cpp"} for r in _ROWS]
    touched, _, _ = _touched_rows_for_diff(
        rows, {"src/m.cpp": [Hunk("", (), (Insertion(20, continues=True),))]}
    )
    assert [r["qualified_name"] for r in touched] == ["m::C"]


def test_lines_in_no_definition_are_counted_and_a_header_does_not_claim_them() -> None:
    # module level, after C: the header names the function above, whose body ended
    assert _touched(Hunk("as_sql", ((64, 65),), (Insertion(66),))) == ([], 3)
    # a header may claim any range: only the lines inside the file's definitions are walked
    names, unmapped = _touched(Hunk("", ((1, 10**9),), ()))
    assert names == [r["qualified_name"] for r in _ROWS] and unmapped == 10**9 - 72


def test_overlapping_hunks_cost_one_walk_of_the_file() -> None:
    """A headers-only diff may hold thousands of hunks that each claim most of a large
    file. Their ranges are merged before the walk: the touched set is what one hunk over
    the same lines gives, and each line is counted once."""
    rows = [_row(f"f{i}", f"m::f{i}", 1 + 50 * i, 40 + 50 * i) for i in range(2_000)]
    hunks = [Hunk("", ((1 + i, 100_000),), ()) for i in range(10_000)]
    touched, _, unmapped = _touched_rows_for_diff(rows, {"pkg/m.py": hunks})
    assert len(touched) == 2_000 and unmapped == 100_000 - 2_000 * 40
    # a hunk whose lines sit in no definition does not hide behind the others
    gap = Hunk("", ((41, 50),), (Insertion(45),))
    assert _touched_rows_for_diff(rows, {"pkg/m.py": [gap] * 3})[2] == 11
