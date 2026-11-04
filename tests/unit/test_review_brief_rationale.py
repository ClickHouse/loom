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
    hunk = "@@ -1,0 +1,1 @@\n+++counter_value;\n"
    assert rb.names_in(rb.added_text(hunk), ["counter_value"]) == ["counter_value"]


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
    assert rb.added_text(diff) == "+++ added line\nnew"
    assert rb.names_in(rb.added_text(diff), ["DB::SpillingHashSet"]) == []


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
    # nothing to check needs no diff, and the section says there is nothing to check
    sec = rb.rationale_section([_row("design/plain", "Decided Z.", ["decision"])], None)
    assert sec["verdict"] == "none" and sec["rows"][0]["verdict"] == ""
    assert sec["status"] == rb.SERVED and sec["causes"] == []
    sec = rb.rationale_section([], "", paths_left=3)
    assert sec["verdict"] == "none" and sec["status"] == rb.EMPTY
    assert sec["causes"] == ["paths_not_read:3"]


def test_a_cut_diff_or_a_cut_value_that_shows_no_name_is_unknown() -> None:
    spill = _row("design/spill", DECISION, ["decision"])
    # the name lies past the cut: the part read adds nothing the row names
    cut = DIFF.split("+    SpillingHashSet")[0]
    sec = rb.rationale_section([spill], cut, diff_cut=True)
    assert sec["rows"][0]["verdict"] == "unknown" and sec["verdict"] == "unknown"
    assert sec["causes"] == ["diff_cut"]
    # a name found before the cut still says check
    assert rb.rationale_section([spill], DIFF, diff_cut=True)["verdict"] == "check"
    # a value cut at the read may name code past the cut, even with no name before it
    cut_row = _row("design/long", "Decided Z because W. Rejected: a plain…", ["decision"])
    sec = rb.rationale_section([{**cut_row, "value_truncated": True}], DIFF)
    assert sec["rows"][0]["verdict"] == "unknown" and sec["causes"] == ["value_cut"]


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

    async def _dir(cfg: Any) -> str:
        return "/m.git" if cfg is not None else ""

    async def _run(git_dir: str, *args: str, max_bytes: int, **kw: Any) -> tuple[str, bool]:
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

    monkeypatch.setattr(for_file, "handle_memory_for_file", _for_file)

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
        assert got == [] and reads == []
        set_current_auth(_token("code-widgets-release", "code-widgets"))
        await code_plane_reads.decisions_for_paths("acme", "code-widgets", ["src/a.cpp"], limit=5)
        assert reads == ["code-widgets"]
    finally:
        set_current_auth(None)
        nc.reset()
