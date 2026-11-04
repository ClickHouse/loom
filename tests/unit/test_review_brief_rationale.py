"""code.review_brief's `rationale` section: the token check names what a diff adds of a
row's rejected options and a constraint's settings, the section orders constraints and
`check` rows first within its own budget, and `decisions_for_paths` reads the trunk's
decision rows per path through `memory.for_file`, gated on the trunk namespace."""

from __future__ import annotations

from typing import Any

import pytest

from server.code.app.index import review_brief as rb
from server.memory.api.models_for_file import MemoryForFileOutput, _MemoryForFileHit
from server.memory.app import code_plane_reads
from server.settings import settings

DECISION = (
    "Decided spill into sorted runs because a set on disk costs a seek per row. "
    "Rejected: a plain `DB::SpillingHashSet` — one seek per row. "
    "Applies to `DB::ExternalDistinctTransform`, src/Processors/ExternalDistinctTransform.h."
)
CONSTRAINT = (
    "Keep `distinct_spill_ratio` on by default: the fan-in relies on it. "
    "Applies to `DB::ExternalDistinctTransform`."
)
DIFF = """diff --git a/src/Processors/ExternalDistinctTransform.cpp b/src/Processors/ExternalDistinctTransform.cpp
--- a/src/Processors/ExternalDistinctTransform.cpp
+++ b/src/Processors/ExternalDistinctTransform.cpp
@@ -1,2 +1,2 @@
-    ExternalDistinctTransform::flush();
+    SpillingHashSet set;
 x
"""


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


def test_checked_names_read_rejected_options_and_a_constraints_settings() -> None:
    assert rb.checked_names(DECISION, constraint=False) == ["DB::SpillingHashSet"]
    # the governed code (`Applies to`) is never a checked name
    assert rb.checked_names(CONSTRAINT, constraint=True) == ["distinct_spill_ratio"]
    assert rb.checked_names(CONSTRAINT, constraint=False) == []
    assert rb.checked_names("Decided X because Y.", constraint=False) == []


def test_names_in_matches_added_lines_as_whole_words() -> None:
    added = rb.added_text(DIFF)
    assert "flush" not in added and "+++" not in added
    assert rb.names_in(added, ["DB::SpillingHashSet"]) == ["DB::SpillingHashSet"]
    assert rb.names_in(added, ["DB::HashSet", "ExternalDistinctTransform"]) == []
    # an added line that starts with `++` is still an added line
    assert rb.names_in(rb.added_text("+++counter_value;\n"), ["counter_value"]) == ["counter_value"]


def test_section_says_check_for_a_row_whose_rejected_option_the_diff_adds() -> None:
    rows = [
        _row("design/spill", DECISION, ["decision"], pinned=True, code_state="changed"),
        _row("design/ratio", CONSTRAINT, ["decision", "constraint"]),
        _row("design/plain", "Decided Z because W.", ["decision"]),
    ]
    sec = rb.rationale_section(rows, DIFF)
    assert sec["verdict"] == "check" and sec["epistemic"] == "lower_bound"
    assert [(r["memory_key"], r["kind"], r["verdict"]) for r in sec["rows"]] == [
        ("design/ratio", "constraint", "clear"),
        ("design/spill", "decision", "check"),
        ("design/plain", "decision", ""),
    ]
    spill = sec["rows"][1]
    assert spill["names"] == ["DB::SpillingHashSet"] and spill["pinned"] is True
    assert spill["code_state"] == "changed" and spill["line"].startswith("- design/spill")
    assert sec["rows_total"] == 3 and not sec["truncated"] and sec["causes"] == []


def test_section_without_a_diff_is_unknown_and_without_rows_none() -> None:
    sec = rb.rationale_section([_row("design/spill", DECISION, ["decision"])], None)
    assert sec["verdict"] == "unknown" and sec["causes"] == ["diff_unread"]
    # nothing to check needs no diff
    sec = rb.rationale_section([_row("design/plain", "Decided Z.", ["decision"])], None)
    assert sec["verdict"] == "clear" and sec["rows"][0]["verdict"] == ""
    sec = rb.rationale_section([], "", paths_left=3)
    assert sec["verdict"] == "none" and sec["status"] == rb.EMPTY
    assert sec["causes"] == ["paths_not_read:3"]


def test_section_keeps_whole_rows_within_its_budget_and_the_verdict_reads_every_row() -> None:
    long = "- " + "x" * 300
    rows = [_row(f"design/d{i}", "Decided Z.", ["decision"], line=long) for i in range(6)]
    rows.append(_row("design/spill", DECISION, ["decision"]))
    sec = rb.rationale_section(rows, DIFF, diff_cut=True)
    assert sec["truncated"] and sec["rows_total"] == 7
    assert rb.estimate_tokens(sec["rows"]) <= settings.code_review_brief_rationale_tokens
    # the `check` row sorts first, so the trim keeps it
    assert sec["rows"][0]["memory_key"] == "design/spill"
    assert sec["verdict"] == "check" and sec["causes"] == ["diff_cut"]


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
    monkeypatch.setattr(for_file, "handle_memory_for_file", _for_file)
    rows = await code_plane_reads.decisions_for_paths(
        "acme", "code-widgets", ["src/a.cpp", "src/b.cpp", "src/a.cpp"], limit=5
    )
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
    assert all(i.tags == ["decision", "constraint"] and not i.include_conflicted for i in seen)


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
    monkeypatch.setattr(for_file, "handle_memory_for_file", _for_file)
    assert await code_plane_reads.decisions_for_paths("acme", "code-widgets", ["a"], limit=5) == []
    assert await code_plane_reads.decisions_for_paths("acme", "", ["a"], limit=5) == []


@pytest.mark.asyncio
async def test_pr_diff_reads_the_posted_diff_else_the_mirror(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import server.code.app.review_brief as h
    from server.code.app.index import gitrepo
    from server.code.app.index import pr_overlay as po

    runs: list[tuple[str, ...]] = []

    async def _dir(cfg: Any, **kw: Any) -> tuple[str, str]:
        return ("/m.git", "") if cfg is not None else ("", "")

    async def _run(git_dir: str, *args: str, max_bytes: int, **kw: Any) -> tuple[str, bool]:
        runs.append((git_dir, *args))
        return DIFF, False

    monkeypatch.setattr(h, "history_dir", _dir)
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
    assert len(runs) == 2
    # no repository here: nothing to read
    monkeypatch.setattr(repos_mod, "repo_for_namespace", lambda o, n: None)
    assert await h._pr_diff("o", "n", merged, pr_number=7, is_open=False) == (None, False)
