"""code.trace frame parsing + code.impact's diff shape.

Parsers only need unit coverage here (the CH legs are exercised through the
handler integration path); the properties pinned are the ones agents rely
on: frame ORDER is preserved, stdlib noise is dropped, hunk ranges narrow
whole files to the functions actually edited, and a co-change partner needs
2+ shared PRs before it may be called "missing"."""

from __future__ import annotations

from server.code.app.symbol.diff import _parse_unified_diff_full
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
    ranges, _ = _parse_unified_diff_full(diff)
    assert ranges["src/A.cpp"] == [(12, 17), (58, 58)]
    assert "Gone.cpp" not in str(set(ranges) - {"src/A.cpp"})


def test_diff_parse_tolerates_garbage() -> None:
    assert _parse_unified_diff_full("not a diff\n@@ nonsense @@\n")[0] == {}
