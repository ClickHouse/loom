"""code.review_brief's `rationale` section: the token check names what a diff adds of a
row's rejected options and a constraint's settings, the section orders constraints and
`check` rows first within its own budget, and `decisions_for_paths` reads the trunk's
decision rows per path through `memory.for_file`, gated on the trunk namespace."""

from __future__ import annotations

import json
from typing import Any

import pytest

from server.code.app.index import review_brief as rb
from server.memory.api.models_for_file import MemoryForFileOutput, _MemoryForFileHit
from server.memory.app import code_plane_reads

DECISION = (
    "Decided spill into sorted runs because a set on disk costs a seek per row. "
    "Rejected: a plain `DB::SpillingHashSet` — one seek per row."
)
CONSTRAINT = (
    "Keep `distinct_spill_ratio` on by default: `DB::ExternalDistinctTransform` relies on it."
)
DIFF = """diff --git a/src/Processors/ExternalDistinctTransform.cpp b/src/Processors/ExternalDistinctTransform.cpp
--- a/src/Processors/ExternalDistinctTransform.cpp
+++ b/src/Processors/ExternalDistinctTransform.cpp
@@ -1,2 +1,2 @@
-    ExternalDistinctTransform::flush();
+    SpillingHashSet set;
 x
"""


BUDGET = 250


def _section(*args: Any, **kw: Any) -> dict[str, Any]:
    return rb.rationale_section(*args, budget_tokens=BUDGET, **kw)


def _row(key: str, text: str, tags: list[str], **kw: Any) -> dict[str, Any]:
    return {
        "memory_key": key,
        "tags": tags,
        "pinned": False,
        "code_state": "",
        "line": f"- {key}: {text[:40]}",
        "text": text,
        **kw,
    }


def test_checked_names_reads_every_name_past_the_mentions_cap() -> None:
    names = [f"`DB::Option{i:02d}`" for i in range(20)]
    text = "Decided X because Y. Rejected: " + "; ".join(names) + "."
    assert rb.checked_names(text, constraint=False) == [f"DB::Option{i:02d}" for i in range(20)]


def test_checked_names_stop_the_rejected_clause_at_the_realized_outcome() -> None:
    from server.memory.domain.decision_statement import memory_value

    value = memory_value(
        {
            "outcome": "spill into sorted runs",
            "reasoning": "a set on disk costs a seek per row",
            "alternatives": ["a plain `DB::SpillingHashSet`"],
            "realized_outcome": "runs merged with `DB::MergingSortedTransform`",
            "success": True,
        }
    )
    assert "Realized:" in value
    assert rb.checked_names(value, constraint=False) == ["DB::SpillingHashSet"]


def test_a_scenario_that_starts_applies_to_still_checks_the_rejected_option() -> None:
    from server.memory.domain.decision_statement import memory_value

    value = memory_value(
        {
            "scenario": "Applies to how the final DISTINCT spills under `distinct_spill_ratio`",
            "outcome": "spill into sorted runs",
            "reasoning": "a set on disk costs a seek per row",
            "alternatives": ["a plain `DB::SpillingHashSet`"],
        }
    )
    assert value.startswith("Applies to")
    assert rb.checked_names(value, constraint=False) == ["DB::SpillingHashSet"]
    assert rb.checked_names(value, constraint=True) == [
        "DB::SpillingHashSet",
        "distinct_spill_ratio",
    ]


def test_a_constraints_setting_counts_however_it_is_qualified() -> None:
    rule = "Keep `Setting::distinct_spill_ratio` on: the fan-in relies on it."
    assert rb.checked_names(rule, constraint=True) == ["Setting::distinct_spill_ratio"]
    diff = (
        "--- a/src/a.cpp\n+++ b/src/a.cpp\n@@ -1,0 +1,1 @@\n"
        "+    settings[Setting::distinct_spill_ratio] = 0;\n"
    )
    row = _row("design/ratio", rule, ["constraint"])
    sec = _section([row], diff)
    assert sec["verdict"] == "check"
    assert sec["rows"][0]["names"] == ["Setting::distinct_spill_ratio"]


def test_checked_names_read_rejected_options_and_a_constraints_settings() -> None:
    assert rb.checked_names(DECISION, constraint=False) == ["DB::SpillingHashSet"]
    # a constraint's snake_case names are checked, its other code is not
    assert rb.checked_names(CONSTRAINT, constraint=True) == ["distinct_spill_ratio"]
    assert rb.checked_names(CONSTRAINT, constraint=False) == []
    assert rb.checked_names("Decided X because Y.", constraint=False) == []


def test_names_in_matches_added_lines_as_whole_words() -> None:
    added, malformed = rb.added_text(DIFF)
    assert not malformed
    assert "flush" not in added and "+++" not in added
    assert rb.names_in(added, ["DB::SpillingHashSet"]) == ["DB::SpillingHashSet"]
    assert rb.names_in(added, ["DB::HashSet", "ExternalDistinctTransform"]) == []
    # an added line that starts with `++` is still an added line
    hunk = "@@ -1,0 +1,1 @@\n+++counter_value;\n"
    assert rb.names_in(rb.added_text(hunk)[0], ["counter_value"]) == ["counter_value"]


def test_added_text_reads_hunks_only_whatever_the_header_prefixes() -> None:
    # `git diff --no-prefix`: the headers name the path bare, and are not added lines
    diff = (
        "diff --git src/SpillingHashSet.h src/SpillingHashSet.h\n"
        "--- src/SpillingHashSet.h\n"
        "+++ src/SpillingHashSet.h\n"
        "@@ -1,2 +1,2 @@\n"
        " keep\n"
        "--- removed line\n"
        "++++ added line\n"
        "\\ No newline at end of file\n"
        "diff --git src/b.h src/b.h\n"
        "--- /dev/null\n"
        "+++ src/b.h\n"
        "@@ -0,0 +1 @@\n"
        "+new\n"
    )
    assert rb.added_text(diff) == ("+++ added line\nnew", False)
    assert rb.names_in(rb.added_text(diff)[0], ["DB::SpillingHashSet"]) == []


HEAD = "diff --git a/src/a.cpp b/src/a.cpp\n--- a/src/a.cpp\n+++ b/src/a.cpp\n"


def test_a_hunk_header_that_miscounts_is_malformed() -> None:
    # undercount: the header counts one added line, two follow
    under = HEAD + "@@ -1,0 +1,1 @@\n+first\n+SpillingHashSet second;\n"
    assert rb.added_text(under) == ("first", True)
    # overcount: the header counts three, the next file's header comes after two
    over = HEAD + "@@ -1,0 +1,3 @@\n+one\n+two\n" + HEAD + "@@ -1,0 +1,1 @@\n+three\n"
    assert rb.added_text(over) == ("one\ntwo\nthree", True)
    # overcount at the end of the diff
    assert rb.added_text(HEAD + "@@ -1,0 +1,2 @@\n+one\n") == ("one", True)
    # a blank line after the counts ran out is a line the header did not count
    assert rb.added_text(HEAD + "@@ -1,0 +1,1 @@\n+one\n\n+SpillingHashSet s;\n")[1] is True
    # a well-counted diff of two files is not
    good = HEAD + "@@ -1,0 +1,1 @@\n+one\n" + HEAD + "@@ -1,1 +1,1 @@\n-x\n+two\n"
    assert rb.added_text(good) == ("one\ntwo", False)


def test_a_plain_multi_file_diff_without_diff_lines_is_well_formed() -> None:
    plain = (
        "--- a/src/a.cpp\n+++ b/src/a.cpp\n@@ -1,1 +1,1 @@\n-x\n+one\n"
        "--- a/src/b.cpp\n+++ b/src/b.cpp\n@@ -1,0 +1,1 @@\n+two\n"
    )
    assert rb.added_text(plain) == ("one\ntwo", False)
    assert rb.read_hunks(plain).paths == ["src/a.cpp", "src/b.cpp"]


def test_a_blank_line_after_a_hunk_makes_the_check_unknown() -> None:
    spill = _row("design/spill", DECISION, ["decision"])
    blank = HEAD + "@@ -1,0 +1,1 @@\n+one\n\n+SpillingHashSet s;\n"
    sec = _section([spill], blank)
    assert sec["verdict"] == "unknown" and sec["causes"] == ["diff_malformed"]


def test_a_form_feed_inside_an_added_line_does_not_split_it() -> None:
    diff = HEAD + "@@ -1,0 +1,2 @@\n+page\x0cbreak\n+SpillingHashSet s;\n"
    assert rb.added_text(diff) == ("page\x0cbreak\nSpillingHashSet s;", False)


def test_parse_diff_reads_a_removed_sql_comment_as_a_removed_line() -> None:
    # removing `-- note` gives `--- note`: a line of the hunk, not the next file's header
    path = "tests/queries/0_stateless/03999_x.sql"
    diff = (
        f"diff --git a/{path} b/{path}\n--- a/{path}\n+++ b/{path}\n"
        "@@ -3,2 +3,1 @@\n--- note\n SET max_threads = 1;\n"
    )
    facts = rb.parse_diff(diff)
    assert facts.paths == [path] and facts.deleted == {path: [(3, 3)]}


def test_parse_diff_still_reads_the_lines_past_an_undercounting_header() -> None:
    """The brief's settings, tests and blame read those lines as before; only the token
    check treats the diff as malformed."""
    path = "tests/queries/0_stateless/03999_x.sql"
    diff = (
        f"diff --git a/{path} b/{path}\n--- a/{path}\n+++ b/{path}\n"
        "@@ -1,1 +1,1 @@\n-SELECT 0;\n+SELECT 1;\n-SELECT 2;\n+SET max_threads = 1;\n"
    )
    facts = rb.parse_diff(diff)
    assert dict(facts.settings) == {"max_threads": "test_uses"}
    assert facts.deleted == {path: [(1, 2)]}
    added, malformed = rb.added_text(diff)
    assert added == "SELECT 1;" and malformed


def test_a_malformed_diff_that_shows_no_name_is_unknown() -> None:
    spill = _row("design/spill", DECISION, ["decision"])
    under = HEAD + "@@ -1,0 +1,1 @@\n+first\n+SpillingHashSet second;\n"
    sec = _section([spill], under)
    assert sec["verdict"] == "unknown" and sec["causes"] == ["diff_malformed"]


def test_section_says_check_for_a_row_whose_rejected_option_the_diff_adds() -> None:
    rows = [
        _row("design/spill", DECISION, ["decision"], pinned=True, code_state="changed"),
        _row("design/ratio", CONSTRAINT, ["decision", "constraint"]),
        _row("design/plain", "Decided Z because W.", ["decision"]),
    ]
    sec = _section(rows, DIFF)
    assert sec["verdict"] == "check" and sec["epistemic"] == "lower_bound"
    # `check` rows first, then constraints, then the rest
    assert [(r["memory_key"], r["kind"], r["verdict"]) for r in sec["rows"]] == [
        ("design/spill", "decision", "check"),
        ("design/ratio", "constraint", "clear"),
        ("design/plain", "decision", ""),
    ]
    spill = sec["rows"][0]
    assert spill["names"] == ["DB::SpillingHashSet"] and spill["pinned"] is True
    assert spill["code_state"] == "changed" and spill["line"].startswith("- design/spill")
    assert sec["rows_total"] == 3 and not sec["truncated"] and sec["causes"] == []


def test_section_without_a_diff_is_unknown_and_without_rows_none() -> None:
    sec = _section([_row("design/spill", DECISION, ["decision"])], None)
    assert sec["verdict"] == "unknown" and sec["causes"] == ["diff_unread"]
    # nothing to check needs no diff, and the section says there is nothing to check
    sec = _section([_row("design/plain", "Decided Z.", ["decision"])], None)
    assert sec["verdict"] == "none" and sec["rows"][0]["verdict"] == ""
    assert sec["status"] == rb.SERVED and sec["causes"] == []
    sec = _section([], "", paths_left=3)
    # an unread path may hold a row to check: never `none` then
    assert sec["verdict"] == "unknown" and sec["status"] == rb.EMPTY
    assert sec["causes"] == ["paths_not_read:3"]


def test_a_cut_diff_or_a_cut_value_that_shows_no_name_is_unknown() -> None:
    spill = _row("design/spill", DECISION, ["decision"])
    # the name lies past the cut: the part read adds nothing the row names
    cut = DIFF.split("+    SpillingHashSet")[0]
    sec = _section([spill], cut, diff_cut=True)
    assert sec["rows"][0]["verdict"] == "unknown" and sec["verdict"] == "unknown"
    assert sec["causes"] == ["diff_cut"]
    # a name found before the cut still says check
    assert _section([spill], DIFF, diff_cut=True)["verdict"] == "check"
    # a value cut at the read may name code past the cut, even with no name before it
    cut_row = _row("design/long", "Decided Z because W. Rejected: a plain…", ["decision"])
    sec = _section([{**cut_row, "value_truncated": True}], DIFF)
    assert sec["rows"][0]["verdict"] == "unknown" and sec["causes"] == ["value_cut"]


def test_a_check_row_is_never_trimmed_behind_long_constraints() -> None:
    long = "- " + "x" * 300
    rows = [
        _row(f"design/c{i}", CONSTRAINT, ["decision", "constraint"], line=long) for i in range(3)
    ]
    rows.append(_row("design/spill", DECISION, ["decision"], line=long))
    sec = _section(rows, DIFF)
    assert sec["truncated"] and sec["rows"][0]["memory_key"] == "design/spill"
    assert sec["rows"][0]["verdict"] == "check"


def test_section_keeps_whole_rows_within_its_budget_and_the_verdict_reads_every_row() -> None:
    long = "- " + "x" * 300
    rows = [_row(f"design/d{i}", "Decided Z.", ["decision"], line=long) for i in range(6)]
    rows.append(_row("design/spill", DECISION, ["decision"]))
    sec = _section(rows, DIFF, diff_cut=True)
    assert sec["truncated"] and sec["rows_total"] == 7
    assert rb.estimate_tokens(sec["rows"]) <= BUDGET
    # the `check` row sorts first, so the trim keeps it
    assert sec["rows"][0]["memory_key"] == "design/spill"
    assert sec["verdict"] == "check" and sec["causes"] == ["diff_cut"]


def _listing(for_file_stub: Any) -> Any:
    """A `listing_rows` over a stub of the memory.for_file handler: its hits as dicts with
    a memory_id (the writer and key), the code-links read not skipped."""

    async def _rows(inp: Any, *, whole_only: bool = False) -> tuple[list[dict[str, Any]], bool]:
        assert whole_only  # decisions and constraints only, typed or tagged
        out = await for_file_stub(inp)
        hits = [h.model_dump() | {"memory_id": f"{h.agent}/{h.memory_key}"} for h in out.hits]
        return hits, False

    return _rows


def _hit(key: str, **kw: Any) -> _MemoryForFileHit:
    return _MemoryForFileHit(
        memory_key=key,
        memory_type="decision",
        value=f'"Decided {key}."',
        tags=kw.pop("tags", ["decision"]),
        namespace="code-widgets",
        **kw,
    )


@pytest.mark.asyncio
async def test_decisions_for_paths_merges_paths_worst_state_and_orders_constraints_first(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.memory.app import code_link_reads, for_file

    seen: list[Any] = []

    async def _readable(org: str, nss: set[str]) -> set[str]:
        return nss

    async def _for_file(inp: Any) -> MemoryForFileOutput:
        seen.append(inp)
        hits = {
            "src/a.cpp": [_hit("design/a", code_state="fresh", confidence=0.9)],
            "src/b.cpp": [
                _hit("design/a", code_state="changed", confidence=0.9),
                _hit("design/rule", tags=["decision", "constraint"], confidence=0.5),
            ],
        }[inp.path]
        return MemoryForFileOutput(path=inp.path, total=len(hits), hits=hits)

    monkeypatch.setattr(code_link_reads, "readable_namespaces", _readable)
    monkeypatch.setattr(for_file, "listing_rows", _listing(_for_file))
    rows, left, full, _, _ = await code_plane_reads.decisions_for_paths(
        "acme", "code-widgets", ["src/a.cpp", "src/b.cpp", "src/a.cpp"], limit=5
    )
    assert left == [] and full == 0
    assert [r["memory_key"] for r in rows] == ["design/rule", "design/a"]
    assert rows[1]["code_state"] == "changed"  # worst over both paths
    assert rows[1]["text"] == "Decided design/a."
    assert rows[1]["line"].startswith("- design/a [decision]")
    assert "(its code changed" in rows[1]["line"]
    # one read per path, in the trunk, active decision and constraint rows only
    assert [i.path for i in seen] == ["src/a.cpp", "src/b.cpp"]
    assert all(
        i.namespace == "code-widgets" and i.extra_namespaces == ["code-widgets"] for i in seen
    )
    # conflicted rows are live: memory.for_file shows them, so the check reads them too
    assert all(i.tags == [] and i.include_conflicted for i in seen)
    # each path reads up to memory.for_file's ceiling; the cut is made after the merge
    assert all(i.top_k == 50 for i in seen)


@pytest.mark.asyncio
async def test_a_row_to_check_is_kept_ahead_of_the_row_cap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.memory.app import code_link_reads, for_file

    async def _readable(org: str, nss: set[str]) -> set[str]:
        return nss

    rule = "Never block the fan-in of `DB::ExternalDistinctTransform`."
    hits = [
        _MemoryForFileHit(
            memory_key=f"design/rule{i}",
            memory_type="decision",
            value=f'"{rule}"',
            tags=["decision", "constraint"],
            pinned=True,
            namespace="code-widgets",
        )
        for i in range(8)
    ]
    hits.append(
        _MemoryForFileHit(
            memory_key="design/spill",
            memory_type="decision",
            value=json.dumps(DECISION),
            tags=["decision"],
            confidence=0.5,
            namespace="code-widgets",
        )
    )

    async def _for_file(inp: Any) -> MemoryForFileOutput:
        return MemoryForFileOutput(path=inp.path, total=len(hits), hits=hits)

    monkeypatch.setattr(code_link_reads, "readable_namespaces", _readable)
    monkeypatch.setattr(for_file, "listing_rows", _listing(_for_file))
    rows, left, full, _, _ = await code_plane_reads.decisions_for_paths(
        "acme", "code-widgets", ["src/a.cpp"], limit=8, first=rb.has_check
    )
    assert rows[0]["memory_key"] == "design/spill" and len(rows) == 8 and len(left) == 1
    assert full == 0
    sec = _section(rows, DIFF, left_out=left)
    assert sec["verdict"] == "check" and sec["rows"][0]["memory_key"] == "design/spill"
    # the constraint left out has nothing to check: no cause names it
    assert sec["causes"] == []


def test_the_per_path_row_ceiling_is_memory_for_files_own() -> None:
    from server.memory.api.models_for_file import MemoryForFileInput

    le = [m.le for m in MemoryForFileInput.model_fields["top_k"].metadata if hasattr(m, "le")]
    assert le == [code_plane_reads.PER_PATH_ROWS]


@pytest.mark.asyncio
async def test_a_path_whose_read_reached_its_ceiling_is_never_clear(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.memory.app import code_link_reads, for_file

    async def _readable(org: str, nss: set[str]) -> set[str]:
        return nss

    rule = "Never block the fan-in of `DB::ExternalDistinctTransform`."
    # 50 pinned constraints fill the read; the checkable decision may lie past them
    hits = [
        _MemoryForFileHit(
            memory_key=f"design/rule{i}",
            memory_type="decision",
            value=json.dumps(rule),
            tags=["decision", "constraint"],
            pinned=True,
            namespace="code-widgets",
        )
        for i in range(code_plane_reads.PER_PATH_ROWS)
    ]
    other = [
        _MemoryForFileHit(
            memory_key="design/spill",
            memory_type="decision",
            value=json.dumps(DECISION),
            tags=["decision"],
            namespace="code-widgets",
        )
    ]

    async def _for_file(inp: Any) -> MemoryForFileOutput:
        got = hits if inp.path == "src/a.cpp" else other
        return MemoryForFileOutput(path=inp.path, total=len(got), hits=got)

    monkeypatch.setattr(code_link_reads, "readable_namespaces", _readable)
    monkeypatch.setattr(for_file, "listing_rows", _listing(_for_file))
    rows, left, full, _, _ = await code_plane_reads.decisions_for_paths(
        "acme", "code-widgets", ["src/a.cpp", "src/b.cpp"], limit=8, first=rb.has_check
    )
    assert full == 1 and rows[0]["memory_key"] == "design/spill"
    # the decision is checked and the diff does not add its rejected option: `clear` alone,
    # but the full path may hold a matching row past its ceiling
    assert _section(rows[:1], "")["verdict"] == "clear"
    sec = _section(rows, "", left_out=left, paths_full=full)
    # the left-out rows have nothing to check, so only the ceiling is named
    assert sec["verdict"] == "unknown" and sec["causes"] == ["paths_at_row_ceiling:1"]


def test_a_left_out_row_with_something_to_check_keeps_the_section_from_clear() -> None:
    ratio = _row("design/ratio", CONSTRAINT, ["decision", "constraint"])
    spill = _row("design/spill", DECISION, ["decision"])
    plain = _row("design/plain", "Decided Z.", ["decision"])
    sec = _section([ratio], DIFF, left_out=[spill, spill, plain])
    assert sec["rows"][0]["verdict"] == "clear" and sec["verdict"] == "unknown"
    assert sec["causes"] == ["rows_not_read:2"]
    # left-out rows with nothing to check leave `clear` standing, and are not named
    sec = _section([ratio], DIFF, left_out=[plain])
    assert sec["verdict"] == "clear" and sec["causes"] == []


@pytest.mark.asyncio
async def test_decisions_for_paths_reads_nothing_from_a_trunk_the_caller_cannot_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.memory.app import code_link_reads, for_file

    async def _readable(org: str, nss: set[str]) -> set[str]:
        return set()

    async def _for_file(inp: Any) -> MemoryForFileOutput:
        raise AssertionError("read a namespace the caller may not read")

    monkeypatch.setattr(code_link_reads, "readable_namespaces", _readable)
    monkeypatch.setattr(for_file, "listing_rows", _listing(_for_file))
    refused = await code_plane_reads.decisions_for_paths("acme", "code-widgets", ["a"], limit=5)
    assert refused.refused and refused.served == [] and refused.left_out == []
    nothing = await code_plane_reads.decisions_for_paths("acme", "", ["a"], limit=5)
    assert not nothing.refused and nothing.served == []
    # a refused read is never `clear` or `none`
    sec = _section(refused.served, None, refused=refused.refused)
    assert sec["verdict"] == "unknown" and sec["causes"] == ["trunk_not_readable"]


@pytest.mark.asyncio
async def test_pr_diff_reads_the_posted_diff_else_the_mirror(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import server.code.app.review_brief as h
    from server.code.app.index import gitrepo
    from server.code.app.index import pr_overlay as po

    runs: list[tuple[str, ...]] = []

    async def _dir(cfg: Any) -> str:
        return "/m.git" if cfg is not None else ""

    async def _run(git_dir: str, *args: str, max_bytes: int, **kw: Any) -> tuple[str, bool]:
        assert kw.get("check") is True  # a diff git failed part way is not read as whole
        runs.append((git_dir, *args))
        return DIFF, False

    from server.code.app.index import mirrors

    monkeypatch.setattr(mirrors, "present_dir", _dir)
    monkeypatch.setattr(gitrepo, "run_bounded", _run)
    import server.code.domain.index.repos as repos_mod

    monkeypatch.setattr(repos_mod, "repo_for_namespace", lambda o, n: object())
    merged = {"merge_sha": "ab12"}
    assert await h._pr_diff("o", "n", merged, pr_number=7, is_open=False) == (DIFF, False)
    opened = {"overlay": True, "base_sha": "b0", "head_sha": "c1"}
    assert await h._pr_diff("o", "n", opened, pr_number=7, is_open=True) == (DIFF, False)
    assert runs == [
        ("/m.git", "diff", "--no-renames", "ab12^1", "ab12"),
        ("/m.git", "diff", "--no-renames", "b0", "c1"),
    ]
    # a posted post-image is the diff; git is not read
    token = po._POSTED.set(po.PostImage(pr_number=7, files={}, diff="+x\n"))
    try:
        assert await h._pr_diff("o", "n", opened, pr_number=7, is_open=True) == ("+x\n", False)
    finally:
        po._POSTED.reset(token)
    # a posted diff is cut where the mirror's would be
    monkeypatch.setattr(rb, "MAX_DIFF_CHARS", 3)
    token = po._POSTED.set(po.PostImage(pr_number=7, files={}, diff="+abcdef\n"))
    try:
        assert await h._pr_diff("o", "n", opened, pr_number=7, is_open=True) == ("+ab", True)
    finally:
        po._POSTED.reset(token)
    assert len(runs) == 2
    # no repository here: nothing to read
    monkeypatch.setattr(repos_mod, "repo_for_namespace", lambda o, n: None)
    assert await h._pr_diff("o", "n", merged, pr_number=7, is_open=False) == (None, False)

    # a lookup that fails is a diff that cannot be read
    def _boom(o: str, n: str) -> Any:
        raise RuntimeError("repos unreadable")

    monkeypatch.setattr(repos_mod, "repo_for_namespace", _boom)
    assert await h._pr_diff("o", "n", merged, pr_number=7, is_open=False) == (None, False)


@pytest.mark.asyncio
async def test_decisions_for_paths_refuses_a_token_that_grants_the_branch_but_not_the_trunk(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The real scope check: a token naming only the branch namespace reads nothing from
    the trunk, and the pre-edit read never runs; the same token naming the trunk does."""
    import server.auth.authz as authz
    import server.code.domain.index.repos as repos
    import server.identity.app.namespace_classification as nc
    from server.auth.context import set_current_auth
    from server.auth.jwt import AuthInfo
    from server.memory.app import for_file
    from server.operations.base import Role

    async def _none_restricted(org: str) -> frozenset[str]:
        return frozenset()

    async def _no_grant(org: str, ns: str, subject: str) -> None:
        return None

    monkeypatch.setattr(nc, "restricted_set", _none_restricted)
    monkeypatch.setattr(authz, "grant_role_for", _no_grant)
    monkeypatch.setattr(repos, "repo_for_namespace", lambda org, ns: None)
    reads: list[str] = []

    async def _for_file(inp: Any) -> MemoryForFileOutput:
        reads.append(inp.namespace)
        return MemoryForFileOutput(path=inp.path, total=0, hits=[])

    monkeypatch.setattr(for_file, "listing_rows", _listing(_for_file))

    def _token(*namespaces: str) -> AuthInfo:
        return AuthInfo(
            subject="alice@x",
            org="acme",
            namespaces=namespaces,
            roles=(Role.VIEWER,),
            is_service_account=False,
        )

    nc.reset()
    try:
        set_current_auth(_token("code-widgets-release"))
        got = await code_plane_reads.decisions_for_paths(
            "acme", "code-widgets", ["src/a.cpp"], limit=5
        )
        assert got.refused and got.served == [] and reads == []
        set_current_auth(_token("code-widgets-release", "code-widgets"))
        await code_plane_reads.decisions_for_paths("acme", "code-widgets", ["src/a.cpp"], limit=5)
        assert reads == ["code-widgets"]
    finally:
        set_current_auth(None)
        nc.reset()


@pytest.mark.asyncio
async def test_rows_sharing_a_writer_and_key_stay_two_rows_and_a_failed_links_read_is_counted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.memory.app import code_link_reads, for_file

    async def _readable(org: str, nss: set[str]) -> set[str]:
        return nss

    def _row_of(mid: str, user: str) -> dict[str, Any]:
        hit = _hit("design/spill", agent="loom-decisions")
        return hit.model_dump() | {"memory_id": mid, "user_id": user}

    async def _rows(inp: Any, **kw: Any) -> tuple[list[dict[str, Any]], bool]:
        # two users' rows under one writer and key; the second path's links read failed
        if inp.path == "src/a.cpp":
            return [_row_of("m1", "alice"), _row_of("m2", "bob")], False
        return [_row_of("m1", "alice")], True

    monkeypatch.setattr(code_link_reads, "readable_namespaces", _readable)
    monkeypatch.setattr(for_file, "listing_rows", _rows)
    read = await code_plane_reads.decisions_for_paths(
        "acme", "code-widgets", ["src/a.cpp", "src/b.cpp"], limit=8
    )
    assert sorted(r["memory_id"] for r in read.served) == ["m1", "m2"]
    assert read.links_skipped == 1
    sec = _section(read.served, "", links_skipped=read.links_skipped)
    assert sec["verdict"] == "unknown" and sec["causes"] == ["links_not_read:1"]


@pytest.mark.asyncio
async def test_a_linked_row_is_read_with_the_whole_only_filter_bound(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A trunk with a code link reaches memory.for_file's linked-rows statement, which
    carries the same decisions-and-constraints filter as the files match."""
    from server.memory.app import code_link_reads
    from server.memory.store import code_links as store_links
    from tests.unit.ch_fake import RecordingCH

    linked = {
        "memory_id": "m-linked",
        "memory_key": "design/linked",
        "memory_type": "decision",
        "value": json.dumps(DECISION),
        "tags": [],
        "confidence": 1.0,
        "pinned": 0,
        "agent": "alice",
        "matched_files": [],
    }
    ch = RecordingCH().on("memory_id IN", [linked]).on("FROM loom.memory FINAL", [])
    ch.install(monkeypatch, "server.memory.app.for_file")

    async def one_link(*a: Any, **k: Any) -> list[dict[str, Any]]:
        return [{"memory_id": "m-linked", "code_namespace": "code-widgets", "state": "fresh"}]

    async def current(org: str, links: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return links

    async def _readable(org: str, nss: set[str]) -> set[str]:
        return set(nss)

    monkeypatch.setattr(store_links, "for_paths", one_link)
    monkeypatch.setattr(code_link_reads, "current_links", current)
    monkeypatch.setattr(code_link_reads, "readable_namespaces", _readable)
    monkeypatch.setattr(
        "server.code.app.memory_links.trunk_namespace", lambda org, ns: "code-widgets"
    )
    read = await code_plane_reads.decisions_for_paths(
        "acme", "code-widgets", ["src/a.cpp"], limit=8
    )
    (stmt,) = ch.statements("memory_id IN")
    assert stmt.params["whole_only"] == 1 and stmt.params["linked"] == ["m-linked"]
    assert "(memory_type = 'decision' OR hasAny(tags, ['decision', 'constraint']))" in stmt.sql
    assert [(r["memory_key"], r["code_state"]) for r in read.served] == [("design/linked", "fresh")]


@pytest.mark.asyncio
@pytest.mark.parametrize("check", [True, False])
async def test_run_bounded_with_check_refuses_output_from_a_failed_git(
    monkeypatch: pytest.MonkeyPatch, check: bool
) -> None:
    """git that writes part of a diff and then exits non-zero: with `check` the output is
    an error (the brief reads the diff as unread), without it the output stands."""
    import asyncio

    from server.code.app.index import gitrepo

    real = asyncio.create_subprocess_exec

    async def _failing_git(*args: Any, **kw: Any) -> Any:
        return await real("sh", "-c", "printf '+partial'; exit 3", **kw)

    monkeypatch.setattr(gitrepo.asyncio, "create_subprocess_exec", _failing_git)
    if check:
        with pytest.raises(gitrepo.GitError):
            await gitrepo.run_bounded("/m.git", "diff", max_bytes=100, check=True)
    else:
        assert await gitrepo.run_bounded("/m.git", "diff", max_bytes=100) == ("+partial", False)


def test_the_impact_seed_count_is_code_impacts_own_file_cap() -> None:
    from server.code.api.models_symbol import CodeImpactInput

    caps = [
        m.max_length
        for m in CodeImpactInput.model_fields["files"].metadata
        if hasattr(m, "max_length")
    ]
    assert caps == [rb.IMPACT_SEED_FILES]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("tags", "status"),
    [
        # memory.set takes any memory_type: a row typed `decision` with no tag is still a
        # decision the pre-edit block prints whole
        ([], "active"),
        # a conflicted row is live: the pre-edit block shows it
        (["decision"], "conflicted"),
    ],
)
async def test_a_typed_untagged_or_conflicted_decision_is_read_and_checked(
    monkeypatch: pytest.MonkeyPatch, tags: list[str], status: str
) -> None:
    from server.memory.app import code_link_reads
    from tests.unit.ch_fake import RecordingCH

    row = {
        "memory_id": "m1",
        "memory_key": "design/spill",
        "memory_type": "decision",
        "value": json.dumps(DECISION),
        "tags": tags,
        "confidence": 1.0,
        "pinned": 0,
        "status_text": status,
        "agent": "alice",
        "matched_files": ["src/a.cpp"],
    }
    ch = RecordingCH().on("FROM loom.memory FINAL", [row])
    ch.install(monkeypatch, "server.memory.app.for_file")

    async def _readable(org: str, nss: set[str]) -> set[str]:
        return nss

    monkeypatch.setattr(code_link_reads, "readable_namespaces", _readable)
    monkeypatch.setattr("server.code.app.memory_links.trunk_namespace", lambda org, ns: "")
    read = await code_plane_reads.decisions_for_paths(
        "acme", "code-widgets", ["src/a.cpp"], limit=8, first=rb.has_check
    )
    # selected by type or tag, not by tag alone, among active and conflicted rows
    (stmt,) = ch.statements("FROM loom.memory FINAL")
    assert stmt.params["whole_only"] == 1 and stmt.params["tags"] == []
    assert stmt.params["statuses"] == ["active", "conflicted"]
    assert "(memory_type = 'decision' OR hasAny(tags, ['decision', 'constraint']))" in stmt.sql
    assert [(r["memory_key"], r["status"]) for r in read.served] == [("design/spill", status)]
    sec = _section(read.served, DIFF)
    assert sec["verdict"] == "check" and sec["rows"][0]["names"] == ["DB::SpillingHashSet"]
