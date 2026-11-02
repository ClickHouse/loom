"""memory.for_file — memories anchored to a file, rendered for a pre-edit hook.

Both stored shapes (bare filename, repo-relative path) match either request
shape; constraints sort first; the rendered block respects the token budget
and says how many it left out; every caller value is a bound parameter.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime
from typing import Any

import pytest

from server.memory.api.models_for_file import MemoryForFileInput
from server.memory.app import for_file as mff


class _CH:
    def __init__(self, rows: list[dict[str, Any]]):
        self.rows = rows
        self.sql = ""
        self.params: dict[str, Any] = {}

    async def query_all(self, sql: str, params: dict[str, Any] | None = None):
        self.sql = sql
        self.params = params or {}
        return self.rows


def _row(key: str, value: str, **over: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "memory_id": f"id-{key}",
        "memory_key": key,
        "memory_type": "semantic",
        "value": value,
        "tags": [],
        "confidence": 1.0,
        "pinned": 0,
        "status": "active",
        "agent": "claude-code",
        "updated_at": datetime(2026, 9, 11, 12, 0),
        "matched_files": ["src/a.cpp"],
    }
    base.update(over)
    return base


@pytest.mark.asyncio
async def test_path_and_basename_are_bound_and_both_shapes_are_matched(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ch = _CH([])
    monkeypatch.setattr(mff, "get_client", lambda: ch)
    out = await mff.handle_memory_for_file(
        MemoryForFileInput(
            org="o", namespace="ns", path="./src/Storages/MergeTreeData.cpp", tags=["project:ch"]
        )
    )
    assert out.total == 0 and out.rendered == "" and out.hits == []
    assert ch.params["p"] == "src/Storages/MergeTreeData.cpp"
    assert ch.params["b"] == "MergeTreeData.cpp"
    assert ch.params["tags"] == ["project:ch"] and ch.params["lim"] == 8
    assert ch.params["statuses"] == ["active", "conflicted"]
    # bare stored name vs repo-relative request, and the reverse
    assert "endsWith({p:String}, concat('/', f))" in ch.sql
    assert "endsWith(f, concat('/', {b:String}))" in ch.sql
    # the files[] facet is the one route to a file
    assert "arrayExists(f ->" in ch.sql
    # constraints first
    assert "ORDER BY has(tags, 'constraint') DESC, pinned DESC, confidence DESC" in ch.sql
    # no interpolation of caller values into SQL text
    assert "MergeTreeData" not in ch.sql and "project:ch" not in ch.sql
    # an expired row is never served, and a credential is never pushed into a prompt
    assert "(expires_at IS NULL OR expires_at > now())" in ch.sql
    assert "sensitivity != 'high'" in ch.sql
    # the attestation columns and the cut flag the verification state is derived from
    for column in ("verified_by", "verified_sha256", "content_sha256"):
        assert column in ch.sql
    assert f"value_len > {mff._VALUE_BYTES} AS value_truncated" in ch.sql


@pytest.mark.asyncio
async def test_hits_carry_their_matched_files_and_render_within_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rows = [
        _row(
            "never_allocate_here",
            "Do not call allocate() in this file; the arena owns it.",
            tags=["constraint", "memory"],
            pinned=1,
        ),
        _row("mergetree_data_history", "x" * 900, matched_files=["a.cpp"]),
        _row("third", "Third fact about the file.", matched_files=["MergeTreeData.cpp"]),
    ]
    ch = _CH(rows)
    monkeypatch.setattr(mff, "get_client", lambda: ch)
    out = await mff.handle_memory_for_file(
        MemoryForFileInput(org="o", namespace="ns", path="src/a.cpp", token_budget=90)
    )
    assert out.total == 3
    assert out.hits[0].matched_files == ["src/a.cpp"]
    assert out.hits[1].matched_files == ["a.cpp"] and out.hits[1].pinned is False
    assert out.hits[2].matched_files == ["MergeTreeData.cpp"]
    assert out.rendered.startswith("Loom: 3 memories anchored to src/a.cpp")
    assert "- never_allocate_here [constraint] (pinned): Do not call allocate()" in out.rendered
    assert out.truncated is True
    assert 'more; memory.for_file(path="src/a.cpp") lists them.' in out.rendered
    assert out.tokens_estimate <= 90
    assert "- third: Third fact about the file." in out.rendered
    # The hit carries the stored value whole; only the rendered block is cut.
    assert out.hits[1].value == "x" * 900
    assert all(len(line) <= mff._LINE_CHARS + 80 for line in out.rendered.splitlines())


def test_render_block_folds_a_value_to_one_capped_line() -> None:
    value = "first line\nsecond line " + "y" * 600
    text, _, truncated = mff.render_block("a.py", [_row("long", value)], 10_000)
    (line,) = text.splitlines()[1:]
    assert truncated is False
    assert line.startswith("- long: first line second line ")
    assert len(line) <= len("- long: ") + mff._LINE_CHARS


def test_render_block_singular_and_empty() -> None:
    text, tokens, truncated = mff.render_block("x.py", [], 300)
    assert (text, tokens, truncated) == ("", 0, False)
    text, tokens, truncated = mff.render_block(
        "x.py", [{"memory_key": "k", "value": "v", "tags": [], "pinned": False}], 300
    )
    assert text == "Loom: 1 memory anchored to x.py — read before editing:\n- k: v"
    assert truncated is False and tokens == (len(text) + 3) // 4


def test_registered_read_only_and_callable_by_name() -> None:
    from server.operations import toolsets
    from server.operations.registry import OPERATIONS_BY_NAME
    from server.transport import mcp_descriptions

    op = OPERATIONS_BY_NAME["memory.for_file"]
    assert op.required_role.name == "VIEWER" and op.idempotent and op.audit is False
    assert "memory.for_file" not in toolsets._MCP_HIDDEN_OPS
    assert (
        "before editing" in mcp_descriptions.TOOL_DESCRIPTIONS["memory.for_file"].lower()
        or "before an edit" in mcp_descriptions.TOOL_DESCRIPTIONS["memory.for_file"].lower()
    )


@pytest.mark.asyncio
async def test_a_hot_files_links_are_bound_in_slices_and_the_best_of_all_are_kept(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import uuid

    linked = {str(uuid.UUID(int=i)): ["fresh"] for i in range(5000)}
    bound: list[list[str]] = []
    file_scans = 0

    class _Sliced:
        async def query_all(self, sql: str, params: dict[str, Any] | None = None):
            nonlocal file_scans
            if "arrayExists" in sql:
                file_scans += 1
                assert "linked" not in (params or {})
                return [_row("named", "v", confidence=0.99)]
            part = (params or {})["linked"]
            bound.append(part)
            # each slice's own best, one stronger the later the slice
            return [_row(f"k{len(bound)}", "v", memory_id=part[0], confidence=len(bound) / 10)]

    monkeypatch.setattr(mff, "get_client", lambda: _Sliced())
    request = MemoryForFileInput(org="o", namespace="ns", path="src/a.cpp", top_k=3)

    async def links() -> dict[str, list[str]]:
        return linked

    task = asyncio.ensure_future(links())
    rows = await mff._rows_for(request, "ns", "src/a.cpp", "a.cpp", [], task)
    assert file_scans == 1  # the files match runs once, not once per slice
    assert len(bound) > 1 and sorted(m for part in bound for m in part) == sorted(linked)
    assert [r["memory_key"] for r in rows] == ["named", f"k{len(bound)}", f"k{len(bound) - 1}"]


@pytest.mark.asyncio
async def test_a_link_read_that_fails_leaves_the_file_match_standing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.memory.app import code_link_reads as app_reads
    from server.memory.store import code_links as store_links

    async def one_link(*a: Any, **k: Any) -> list[dict[str, Any]]:
        return [{"memory_id": "m", "code_namespace": "code-a", "state": "fresh"}]

    async def down(*a: Any, **k: Any) -> Any:
        raise ConnectionError("clickhouse")

    monkeypatch.setattr(store_links, "for_paths", one_link)
    monkeypatch.setattr(app_reads, "current_links", down)
    monkeypatch.setattr("server.code.app.memory_links.trunk_namespace", lambda org, ns: "code-a")
    request = MemoryForFileInput(org="o", namespace="ns", path="src/a.cpp", extra_namespaces=["x"])
    assert await mff._linked(request, "src/a.cpp", ["ns"]) == {}


# --- decision rows ------------------------------------------------------------

_NOW = datetime(2026, 9, 25, 12, 0)
_SOURCE = "knowledge:design-docs/spilling.pdf#Slide 2 > Final distinct"


def _decision(key: str, value: str = "Decided X because Y.", **over: Any) -> dict[str, Any]:
    """A decision row as `loom.memory` holds it: the value JSON-encoded."""
    value = json.dumps(value)
    fields: dict[str, Any] = {
        "memory_type": "decision",
        "tags": ["decision"],
        "pinned": True,
        "namespace": "code-repo",
        "actor": "human:alice",
        "updated_at": datetime(2026, 9, 22, 12, 0),
        "source_resource_id": _SOURCE,
    }
    return _row(key, value, **(fields | over))


def _lines(hits: list[dict[str, Any]]) -> list[str]:
    text, _, _ = mff.render_block("src/a.cpp", hits, 10_000, home="me", now=_NOW)
    return text.splitlines()


def test_a_team_decision_is_headed_decision_and_names_its_source() -> None:
    assert _lines([_decision("design/spill")])[1] == (
        "- Decision (by alice, 3 days ago) (pinned): Decided X because Y."
        f" — source: {_SOURCE} [id: design/spill]"
    )


def test_a_decisions_source_survives_the_value_cap() -> None:
    line = _lines([_decision("design/long", "z" * 900)])[1]
    assert f"… — source: {_SOURCE} [id: design/long]" in line
    assert line.count("z") == mff._LINE_CHARS - 1


def test_a_decision_without_a_source_has_no_suffix() -> None:
    line = _lines([_decision("design/bare", source_resource_id="")])[1]
    assert line.endswith("Decided X because Y. [id: design/bare]") and "source:" not in line


def test_an_unpinned_decision_is_unconfirmed_in_either_namespace() -> None:
    team, own = _lines(
        [_decision("design/a", pinned=False), _decision("design/b", pinned=0, namespace="me")]
    )[1:3]
    assert team.startswith("- Decision (by alice, 3 days ago) (unconfirmed): ")
    assert own == f"- design/b [decision] (unconfirmed): Decided X because Y. — source: {_SOURCE}"


def test_a_row_that_is_not_a_decision_gets_no_decision_marks() -> None:
    # No head, source or (unconfirmed); its value is printed as text like every row's.
    rule = _decision("rules/r", "Keep 64.", memory_type="semantic", tags=["constraint"])
    note = _row("note", json.dumps("Mine."), source_resource_id=_SOURCE, namespace="me")
    assert _lines([rule, note])[1:3] == [
        "- Team rule (by alice, 3 days ago) [constraint] (pinned): Keep 64. [id: rules/r]",
        "- note: Mine.",
    ]


def test_decision_lines_stay_inside_the_token_budget() -> None:
    hits = [_decision(f"design/{i}", "w" * 300) for i in range(6)]
    text, _, truncated = mff.render_block("src/a.cpp", hits, 200, home="me", now=_NOW)
    rows = [ln for ln in text.splitlines() if ln.startswith("- Decision")]
    assert truncated is True and 0 < len(rows) < len(hits)
    # a row that does not fit is left out whole, never printed without its source
    assert all(
        ln.endswith(f"source: {_SOURCE} [id: {h['memory_key']}]")
        for ln, h in zip(rows, hits[: len(rows)], strict=True)
    )
    # the "… more" line and the removal footer included, the block fits the budget
    assert text.splitlines()[-2].startswith("- … ")
    assert len(text) <= 200 * mff._CHARS_PER_TOKEN


def test_a_long_decision_and_a_constraint_fit_the_default_budget() -> None:
    source = "knowledge:design-docs/" + "s" * 300
    hits = [
        _row("rules/r", "Keep 64 buckets.", tags=["constraint"], namespace="code-repo"),
        _decision("design/long", "v" * 900, source_resource_id=source),
    ]
    budget = MemoryForFileInput(org="o", namespace="me", path="a").token_budget
    text, _, truncated = mff.render_block("src/a.cpp", hits, budget, home="me", now=_NOW)
    lines = text.splitlines()
    assert truncated is False and budget == 300
    assert lines[1].startswith("- Team rule (") and lines[1].endswith("[id: rules/r]")
    assert lines[2].endswith(f"— source: {source} [id: design/long]")


def test_a_long_source_keeps_its_section() -> None:
    # A cut source would cite another document, or the document without its section.
    source = "knowledge:design-docs/" + "d" * 200 + ".pdf#Slide 7 > Spilling to disk"
    line = _lines([_decision("design/long", source_resource_id=source)])[1]
    assert line.endswith(f" — source: {source} [id: design/long]")


def test_a_source_with_a_line_break_stays_on_its_line() -> None:
    source = "knowledge:design-docs/a  b.pdf#Plan > Step\none\tlater"
    line = _lines([_decision("design/nl", source_resource_id=source)])[1]
    assert line.endswith(
        " — source: knowledge:design-docs/a  b.pdf#Plan > Step one\tlater [id: design/nl]"
    )


def test_a_row_too_long_for_the_budget_does_not_hide_the_rows_after_it() -> None:
    long = _decision("design/long", source_resource_id="knowledge:d/" + "x" * 1500)
    short = _row("rules/short", json.dumps("Keep 64."), namespace="me")
    text, tokens, truncated = mff.render_block("src/a.cpp", [long, short], 300, home="me", now=_NOW)
    lines = text.splitlines()
    assert truncated is True and tokens <= 300
    assert "- rules/short: Keep 64." in lines and "design/long" not in text
    assert lines[-2].startswith("- … 1 more;")


def test_a_block_that_fits_whole_reserves_nothing() -> None:
    # 100 and 30 characters: the block fits in 194 of 200, so nothing is cut or reordered.
    hits = [_row("k0", json.dumps("a" * 94)), _row("k1", json.dumps("b" * 24))]
    text, tokens, truncated = mff.render_block("src/a.cpp", hits, 50, home="me")
    assert truncated is False and tokens <= 50
    assert text.splitlines()[1:] == ["- k0: " + "a" * 94, "- k1: " + "b" * 24]


def test_a_superseded_row_is_left_out_with_the_row_that_supersedes_it() -> None:
    newer = _row("rules/new", json.dumps("n" * 200), namespace="code-repo", tags=["constraint"])
    newer |= {"supersedes_key": "rules/old", "supersedes": "bob", "supersedes_kind": "rule"}
    older = _row("rules/old", json.dumps("Keep 32."), namespace="code-repo")
    text, tokens, truncated = mff.render_block("src/a.cpp", [newer, older], 120, home="me")
    assert truncated is True and tokens <= 120
    assert "rules/new" not in text and "rules/old" not in text
    assert '- … 2 more; memory.for_file(path="src/a.cpp") lists them.' in text.splitlines()


def test_mixed_rows_stay_inside_the_budget() -> None:
    hits = [_row(f"k{i}", json.dumps("r" * (20 + 37 * i % 200))) for i in range(12)]
    for budget in range(50, 600):
        text, tokens, truncated = mff.render_block("src/a.cpp", hits, budget, home="me")
        assert len(text) <= budget * mff._CHARS_PER_TOKEN and tokens <= budget
        assert truncated is text.splitlines()[-1].startswith("- … ")


def test_the_more_line_fits_inside_the_budget() -> None:
    hits = [_row(f"k{i}", json.dumps("r" * 100)) for i in range(12)]
    for budget in range(50, 400):
        text, tokens, truncated = mff.render_block("src/a.cpp", hits, budget, home="me")
        assert len(text) <= budget * mff._CHARS_PER_TOKEN and tokens <= budget
        assert truncated is text.splitlines()[-1].startswith("- … ")


def test_a_block_filling_the_budget_prints_whole_and_one_more_character_cuts_it() -> None:
    def block(second: int, budget: int) -> tuple[str, int, bool]:
        hits = [_row("k0", json.dumps("a" * 100)), _row("k1", json.dumps("b" * second))]
        return mff.render_block("src/a.cpp", hits, budget, home="me")

    # A second row that fills the budget to the character (the block counts a
    # newline after its last line): both rows print and nothing is cut.
    full, _, _ = block(100, 10_000)
    second = 100 + (-(len(full) + 1)) % mff._CHARS_PER_TOKEN
    budget = (len(block(second, 10_000)[0]) + 1) // mff._CHARS_PER_TOKEN
    text, tokens, truncated = block(second, budget)
    assert truncated is False and tokens == budget and "- k1: " in text
    # One character more and it is left out, with room kept for the "… more" line.
    text, tokens, truncated = block(second + 1, budget)
    assert truncated is True and text.splitlines()[-1].startswith("- … 1 more")
    assert tokens <= budget


@pytest.mark.asyncio
async def test_the_source_is_read_into_the_hit_and_the_block(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ch = _CH([_decision("design/spill", namespace=None)])
    monkeypatch.setattr(mff, "get_client", lambda: ch)
    out = await mff.handle_memory_for_file(
        MemoryForFileInput(org="o", namespace="ns", path="src/a.cpp")
    )
    assert out.rendered.splitlines()[1] == (
        f"- design/spill [decision] (pinned): Decided X because Y. — source: {_SOURCE}"
    )
    assert out.hits[0].source_resource_id == _SOURCE


def test_a_row_tagged_decision_is_a_decision() -> None:
    tagged = _decision("design/t", "v", memory_type="semantic", pinned=False)
    assert _lines([tagged])[1] == (
        f"- Decision (by alice, 3 days ago) (unconfirmed): v — source: {_SOURCE} [id: design/t]"
    )


def _conflicting(newer: dict[str, Any], older: dict[str, Any]) -> list[dict[str, Any]]:
    """The pair as the conflict check leaves it, ordered by `_supersede_order`."""
    older["updated_at"] = datetime(2026, 9, 20, 12, 0)
    newer["conflict_with"] = [older["memory_id"]]
    return mff._supersede_order([older, newer])


def test_a_row_superseding_a_tagged_decision_says_decision() -> None:
    old = _decision("design/old", memory_type="semantic", actor="human:bob")
    rows = _conflicting(_decision("design/new", "New.", memory_type="semantic"), old)
    assert rows[0]["supersedes_kind"] == "decision"
    assert _lines(rows)[1].startswith(
        "- Decision (by alice, 3 days ago; supersedes decision by bob) (pinned): New."
    )


def test_two_cross_linked_typed_decisions_are_two_decisions() -> None:
    # The conflict check links close decisions under different keys; neither supersedes.
    old = _decision("design/old", actor="human:bob")
    rows = _conflicting(_decision("design/new", "New."), old)
    assert [r["memory_key"] for r in rows] == ["design/old", "design/new"]
    assert not any(r.get("supersedes_key") for r in rows)


_FOOTER = (
    '- A team {what} wrong or outdated? memory.delete(namespace="code-repo", memory_key=<id>) '
    "removes it if you wrote it or are a maintainer; otherwise {steps}."
)
_RULE_STEP = "write a corrected rule for the file and tell its author"
_DECISION_STEP = "ask a maintainer to record the corrected decision with decision.record"


def test_a_block_of_typed_decisions_says_only_how_to_correct_a_decision() -> None:
    footer = _lines([_decision("design/a"), _decision("design/b")])[-1]
    assert footer == _FOOTER.format(what="decision", steps=_DECISION_STEP)


def test_a_block_of_rules_and_tagged_decisions_says_only_the_rule_step() -> None:
    rule = _decision("rules/r", "Keep 64.", memory_type="semantic", tags=["constraint"])
    tagged = _decision("design/t", memory_type="semantic")
    footer = _lines([rule, tagged])[-1]
    assert footer == _FOOTER.format(what="rule or decision", steps=_RULE_STEP)


def test_a_block_of_rules_and_decisions_says_both() -> None:
    rule = _decision("rules/r", "Keep 64.", memory_type="semantic", tags=["constraint"])
    footer = _lines([rule, _decision("design/a")])[-1]
    steps = f"{_RULE_STEP}; or {_DECISION_STEP}"
    assert footer == _FOOTER.format(what="rule or decision", steps=steps)


def test_a_typed_decision_without_the_tag_is_marked_in_its_own_namespace() -> None:
    own = _decision("design/own", "v", tags=[], namespace="me", source_resource_id="")
    assert _lines([own])[1] == "- design/own [decision] (pinned): v"


def test_a_verified_decision_is_confirmed_and_a_stale_one_is_not() -> None:
    verified = _decision("design/v", pinned=False, verification="human")
    stale = _decision("design/s", pinned=False, verification="stale")
    first, second = _lines([verified, stale])[1:3]
    assert first.startswith("- Decision (by alice, 3 days ago): Decided X because Y.")
    assert second.startswith("- Decision (by alice, 3 days ago) (unconfirmed): ")


def test_an_unconfirmed_decision_whose_code_changed_notes_both() -> None:
    line = _lines([_decision("design/c", pinned=False, code_state="changed")])[1]
    assert line.startswith(
        "- Decision (by alice, 3 days ago) (unconfirmed) (its code changed since it was "
        "written: check before relying on it): Decided X because Y."
    )


@pytest.mark.asyncio
async def test_the_attestation_is_read_and_a_stale_one_leaves_a_decision_unconfirmed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.core.hashing import sha256_value_json

    value = "Decided X because Y."
    good = sha256_value_json(json.dumps(value))
    rows = [
        _decision(
            "design/v",
            value,
            pinned=0,
            namespace=None,
            verified_by="human:carol",
            verified_sha256=good,
            content_sha256=good,
        ),
        _decision(
            "design/s",
            value,
            pinned=0,
            namespace=None,
            verified_by="human:carol",
            verified_sha256="f" * 64,
            content_sha256=good,
        ),
    ]
    monkeypatch.setattr(mff, "get_client", lambda: _CH(rows))
    out = await mff.handle_memory_for_file(
        MemoryForFileInput(org="o", namespace="ns", path="src/a.cpp")
    )
    assert [h.verification for h in out.hits] == ["human", "stale"]
    first, second = out.rendered.splitlines()[1:3]
    assert "(unconfirmed)" not in first and "(unconfirmed)" in second


@pytest.mark.parametrize(
    ("value", "expected"),
    [('"short"', "human"), ('"' + "x" * 599 + '"', "stale")],
)
@pytest.mark.asyncio
async def test_without_a_stored_hash_only_an_uncut_value_confirms(
    monkeypatch: pytest.MonkeyPatch, value: str, expected: str
) -> None:
    from server.core.hashing import sha256_value_json

    row = _decision(
        "design/h",
        value,
        pinned=0,
        namespace=None,
        verified_by="human:carol",
        verified_sha256=sha256_value_json(value),
        content_sha256="",
    )
    # the select cuts the value at 600 bytes and flags it
    row["value_truncated"] = len(value) > mff._VALUE_BYTES
    row["value"] = value[: mff._VALUE_BYTES]
    monkeypatch.setattr(mff, "get_client", lambda: _CH([row]))
    out = await mff.handle_memory_for_file(
        MemoryForFileInput(org="o", namespace="ns", path="src/a.cpp")
    )
    assert out.hits[0].verification == expected
    assert ("(unconfirmed)" in out.rendered) is (expected == "stale")


def test_a_stored_value_is_printed_as_text() -> None:
    # memory.set stores the JSON text: no quotes, and non-ASCII as written.
    line = _lines([_decision("design/u", "Décidé — 決定した.")])[1]
    assert ": Décidé — 決定した. — source: " in line


def test_a_decision_whose_outcome_failed_says_so() -> None:
    failed = _decision("design/f", tags=["decision", "decision:outcome-known", "decision:failure"])
    assert _lines([failed])[1].startswith(
        "- Decision (by alice, 3 days ago) (pinned) (outcome: failed): "
    )


@pytest.mark.asyncio
async def test_a_value_cut_by_the_select_is_printed_as_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The select cuts the stored JSON at 600 bytes and flags it; the handler's block decodes it.
    stored = json.dumps("Décidé " * 120)[:600]
    ch = _CH([_decision("design/long", namespace=None, value_truncated=1) | {"value": stored}])
    monkeypatch.setattr(mff, "get_client", lambda: ch)
    out = await mff.handle_memory_for_file(
        MemoryForFileInput(org="o", namespace="ns", path="src/a.cpp")
    )
    line = out.rendered.splitlines()[1]
    assert "(pinned): Décidé Décidé" in line and "\\u" not in line and '"' not in line


@pytest.mark.parametrize("cut", [0, 1, 6, 11])
def test_a_cut_inside_a_surrogate_pair_leaves_no_half_of_it(cut: int) -> None:
    stored = json.dumps("a" * 10 + "😀" * 5)
    hit = _decision("design/e", value_truncated=True) | {"value": stored[: len(stored) - 1 - cut]}
    line = _lines([hit])[1]
    assert line.encode("utf-8")  # raises on a lone surrogate
    assert ": aaaaaaaaaa" in line


def test_a_lone_surrogate_in_a_whole_value_is_printed_encodable() -> None:
    hit = _decision("design/s", "a\ud800b")
    line = _lines([hit])[1]
    assert line.encode("utf-8") and ": a?b" in line


def test_a_cut_value_shorter_than_the_line_still_ends_in_an_ellipsis() -> None:
    # Escaped CJK fills 600 stored bytes with about 99 characters, well under the line's cap.
    cut = _decision("design/zh", value_truncated=True) | {"value": json.dumps("决定" * 100)[:600]}
    body = _lines([cut])[1].split("(pinned): ", 1)[1]
    assert body.startswith("决定") and body.split(" — source:")[0].endswith("…")


def test_a_rule_tagged_with_a_failure_is_not_a_failed_decision() -> None:
    rule = _row("rules/r", json.dumps("Keep it."), tags=["constraint", "decision:failure"])
    assert "(outcome: failed)" not in _lines([rule])[1]


def test_a_cut_object_value_prints_as_stored() -> None:
    raw = json.dumps({"k": "v" * 700})[:600]
    hit = _decision("design/o", value_truncated=True) | {"value": raw}
    assert raw[:40] in _lines([hit])[1]


def test_a_decision_whose_source_changed_says_so_confirmed_or_not() -> None:
    tags = ["decision", "source-changed"]
    pinned = _lines([_decision("design/p", tags=tags)])[1]
    assert pinned.startswith(
        "- Decision (by alice, 3 days ago) (pinned) (source changed since confirmation): "
    )
    own = _lines([_decision("design/u", tags=tags, pinned=False, namespace="me")])[1]
    assert own.startswith("- design/u [decision] (unconfirmed) (source changed): ")
    # A fact carrying the tag is no decision: nothing is added.
    fact = _row("fact", json.dumps("A plain fact."), tags=["source-changed"], namespace="me")
    assert "(source changed" not in _lines([fact])[1]


@pytest.mark.asyncio
async def test_the_served_block_marks_a_decision_whose_source_changed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rows = [
        _decision("extracted", "Decided spill.", pinned=False, tags=["decision", "source-changed"])
    ]
    monkeypatch.setattr(mff, "get_client", lambda: _CH(rows))
    out = await mff.handle_memory_for_file(
        MemoryForFileInput(org="o", namespace="ns", path="src/a.cpp", token_budget=300)
    )
    assert "(unconfirmed) (source changed): Decided spill." in out.rendered


def test_an_attested_decision_whose_source_changed_reads_since_confirmation() -> None:
    # `verification` as the handler derives it for an attestation binding the current value.
    hit = _decision(
        "design/a", pinned=False, tags=["decision", "source-changed"], verification="human"
    )
    line = _lines([hit])[1]
    assert "(source changed since confirmation)" in line and "(unconfirmed)" not in line
