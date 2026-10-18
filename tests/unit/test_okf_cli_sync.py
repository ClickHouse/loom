"""`loom import-okf` on a schedule: an unchanged concept or edge is skipped (re-sending
it would count as a reinforcement), a changed or new one is written, and `--prune`
deletes only the rows an earlier import wrote, from a bundle that is a complete export."""

from __future__ import annotations

import argparse
import base64
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from server.cli import okf as cli_okf
from server.core.hashing import sha256_value
from server.memory.domain import okf_bundle as okf
from server.memory.domain import okf_lint
from server.memory.domain.edge_relations import canonical_edge
from server.memory.domain.graph_quality import canonical_entities

_NOW = datetime(2026, 9, 1, tzinfo=UTC)
# An unsigned token the CLI reads claims from; _ACTOR is the actor these claims give.
_TOKEN = (
    "e30."
    + base64.urlsafe_b64encode(json.dumps({"sub": "okf-sync", "sa": True}).encode())
    .decode()
    .rstrip("=")
    + ".x"
)
_ACTOR = "process:okf-sync"


def _entry(key: str, **over: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "memory_key": key,
        "memory_type": "semantic",
        "value": f"{key} fails when the part count passes 300.",
        "tags": ["lesson"],
        "agent": "writer-a",
        "namespace": "src",
        "created_at": "2026-09-01T10:00:00",
        "valid_at": "2026-08-30T08:00:00",
        "status": "active",
        "confidence": 1.0,
        "entities": ["MergeTree"],
    }
    base.update(over)
    return base


def _bundle(
    root: Path,
    entries: list[dict[str, Any]],
    edges: list[dict[str, Any]] | None = None,
    limit: int | None = None,
) -> Path:
    files = okf.plan_bundle(entries, namespace="src", exported_at=_NOW, edges=edges, limit=limit)
    cli_okf._write_bundle(root, files)
    return root


class _FakeLoom:
    """The ops the import calls, storing rows the way memory.set does."""

    def __init__(self) -> None:
        self.rows: dict[tuple[str, str, str], dict[str, Any]] = {}  # (agent, session, key)
        self.edges: list[dict[str, Any]] = []
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def put(self, agent: str, key: str, session_id: str = "-", **row: Any) -> None:
        self.rows[(agent, session_id, key)] = {
            "memory_key": key,
            "session_id": session_id,
            "agent": agent,
            **row,
        }

    async def post(self, op: str, body: dict[str, Any]) -> dict[str, Any]:
        self.calls.append((op, body))
        if op == "memory.list":
            mine = sorted(
                (
                    r
                    for (a, s, _k), r in self.rows.items()
                    if body["agent"] in ("*", a) and body["session_id"] in ("*", s)
                ),
                key=lambda r: r["memory_key"],
            )
            page = mine[body["offset"] : body["offset"] + body["limit"]]
            return {
                "entries": [_project(r, body) for r in page],
                "truncated": body["offset"] + len(page) < len(mine),
            }
        if op == "memory.bulk_get":
            got = {
                k: _project(r, body)
                for (a, s, k), r in self.rows.items()
                if k in body["memory_keys"]
                and body["agent"] in ("*", a)
                and s == body["session_id"]
            }
            return {"results": got}
        if op == "memory.get":
            # The server's fallback: the same key under any agent when ours is gone.
            ident = (body["agent"], body["session_id"], body["memory_key"])
            row = self.rows.get(ident)
            agent = body["agent"]
            if row is None:
                agent, row = next(
                    ((a, r) for (a, _s, k), r in self.rows.items() if k == body["memory_key"]),
                    ("", None),
                )
            return {"found": row is not None, "agent": agent, "actor": (row or {}).get("actor")}
        if op == "memory.verify":
            return {"found": True}
        if op == "memory.export_evidence":
            return {"edges": self.edges, "has_more": False}
        if op == "memory.set":
            # A target that labels the value adds its tag, as memory.set does.
            labels = (
                ["sensitivity:medium"]
                if "@" in str(body["value"]) and "sensitivity:medium" not in body["tags"]
                else []
            )
            self.put(
                body["agent"],
                body["memory_key"],
                body["session_id"],
                memory_type=body["memory_type"],
                confidence=body["confidence"],
                source_trace=body["source_trace"],
                value=body["value"],
                valid_at=body.get("valid_at"),
                valid_to=body.get("valid_to"),
                tags=[*body["tags"], *labels],
                actor=_ACTOR,
                entities=canonical_entities(body.get("entities") or []),
                content_sha256=sha256_value(body["value"]),
                # The server derives `imported` from an okf: trace when none is declared.
                source_class=body.get("source_class")
                or ("imported" if body["source_trace"].startswith("okf:") else "asserted"),
                verification="none",
            )
            return {"status": "active"}
        if op == "memory.link":
            edge = canonical_edge(body["from_key"], body["to_key"], body["relation"])
            self.edges.append(
                {"from_key": edge.from_key, "to_key": edge.to_key, "relation": edge.relation}
            )
            return {"linked": True}
        if op == "memory.delete":
            gone = self.rows.pop((body["agent"], body["session_id"], body["memory_key"]), None)
            return {"tombstoned": gone is not None}
        raise AssertionError(op)

    async def __aenter__(self) -> _FakeLoom:
        return self

    async def __aexit__(self, *exc: object) -> None:
        pass

    def writes(self) -> list[str]:
        return [op for op, _ in self.calls if op in ("memory.set", "memory.link", "memory.delete")]


def _project(row: dict[str, Any], body: dict[str, Any]) -> dict[str, Any]:
    """The row as a read with `fields` returns it: the key and the fields asked for."""
    if "fields" not in body:
        return row
    return {k: v for k, v in row.items() if k == "memory_key" or k in body["fields"]}


@pytest.fixture
def loom(monkeypatch: pytest.MonkeyPatch) -> _FakeLoom:
    fake = _FakeLoom()
    monkeypatch.setattr(cli_okf, "_Client", lambda *_a: fake)
    return fake


def _export(out: Path, *flags: str) -> dict[str, Any]:
    """Export namespace `dst` from the fake and return the root index's pointer."""
    parser = argparse.ArgumentParser()
    cli_okf.register_okf_cli(parser.add_subparsers())
    args = parser.parse_args(
        ["export-okf", "--token", _TOKEN, "--org", "acme", "--namespace", "dst"]
        + ["--out", str(out), "--no-evidence", "--no-links", *flags]
    )
    assert int(args.func(args)) == 0
    return okf_lint.generation_pointer(okf_lint.read_bundle(out))


def _run(root: Path, *flags: str, agent: str | None = "okf-sync", namespace: str = "dst") -> int:
    parser = argparse.ArgumentParser()
    cli_okf.register_okf_cli(parser.add_subparsers())
    args = parser.parse_args(
        ["import-okf", str(root), "--token", _TOKEN, "--org", "acme", "--namespace", namespace]
        + (["--agent", agent] if agent is not None else [])
        + list(flags)
    )
    return int(args.func(args))


def test_a_second_import_of_the_same_bundle_writes_nothing(tmp_path: Path, loom: _FakeLoom):
    edge = {"from_key": "a", "to_key": "b", "relation": "supports", "author": "x"}
    root = _bundle(tmp_path, [_entry("a"), _entry("b")], edges=[edge])
    assert _run(root) == 0
    assert loom.writes() == ["memory.set", "memory.set", "memory.link"]
    loom.calls.clear()
    assert _run(root) == 0
    assert loom.writes() == []


@pytest.mark.parametrize(
    "change",
    [
        {},
        {"value": "a fails when the part count passes 500."},
        {"tags": ["lesson", "flaky"]},
        {"confidence": 0.5},
        {"valid_at": "2026-08-31T08:00:00"},
        {"valid_to": "2026-12-01T00:00:00"},
        {"entities": ["MergeTree", "S3"]},
        {"source_class": "derived"},
    ],
    ids=lambda c: next(iter(c), "nothing"),
)
def test_a_changed_value_or_field_is_written(
    tmp_path: Path, loom: _FakeLoom, change: dict[str, Any]
) -> None:
    # Same directory name each run, so source_trace is the same.
    assert _run(_bundle(tmp_path / "1" / "b", [_entry("a")])) == 0
    loom.calls.clear()
    assert _run(_bundle(tmp_path / "2" / "b", [_entry("a", **change)])) == 0
    sets = [b["memory_key"] for op, b in loom.calls if op == "memory.set"]
    assert sets == (["a"] if change else [])


def test_a_changed_type_is_written(tmp_path: Path, loom: _FakeLoom) -> None:
    root = _bundle(tmp_path / "b", [_entry("a")])
    assert _run(root) == 0
    # Edited in place: an export would also move the file, which changes source_trace.
    concept = root / "semantic" / "a.md"
    concept.write_text(
        concept.read_text().replace("memory_type: semantic", "memory_type: episodic")
    )
    loom.calls.clear()
    assert _run(root) == 0
    assert [b["memory_type"] for op, b in loom.calls if op == "memory.set"] == ["episodic"]


def test_a_copy_into_the_same_namespace_name_is_left_alone(tmp_path: Path, loom: _FakeLoom):
    # The CronJob's setup: the same org and namespace on both sides, so no origin tags
    # and no declared source_class.
    root = _bundle(tmp_path / "b", [_entry("a")])
    assert _run(root, namespace="src") == 0
    assert "source_class" not in next(b for op, b in loom.calls if op == "memory.set")
    loom.calls.clear()
    assert _run(root, namespace="src") == 0
    assert loom.writes() == []


def test_an_import_from_the_current_directory_names_the_bundle(
    tmp_path: Path, loom: _FakeLoom, monkeypatch: pytest.MonkeyPatch
) -> None:
    edge = {"from_key": "a", "to_key": "c", "relation": "supports", "author": "x"}
    root = _bundle(tmp_path / "b", [_entry("a"), _entry("c")], edges=[edge])
    monkeypatch.chdir(root)
    assert _run(Path(".")) == 0
    traces = {b["source_trace"] for op, b in loom.calls if op == "memory.set"}
    assert traces == {"okf:b/semantic/a.md", "okf:b/semantic/c.md"}
    link = next(b for op, b in loom.calls if op == "memory.link")
    assert link["explanation"].startswith("okf bundle b: ")


def test_the_bundle_directory_name_is_part_of_what_is_written(tmp_path: Path, loom: _FakeLoom):
    assert _run(_bundle(tmp_path / "v1", [_entry("a")])) == 0
    loom.calls.clear()
    # source_trace names the bundle directory, so a renamed directory rewrites the row once.
    assert _run(_bundle(tmp_path / "v2", [_entry("a")])) == 0
    assert loom.writes() == ["memory.set"]


def test_only_the_new_key_is_written(tmp_path: Path, loom: _FakeLoom) -> None:
    root = tmp_path / "b"
    assert _run(_bundle(root, [_entry("a")])) == 0
    loom.calls.clear()
    assert _run(_bundle(root, [_entry("a"), _entry("c")])) == 0
    assert [b["memory_key"] for op, b in loom.calls if op == "memory.set"] == ["c"]


def test_live_rows_are_read_past_one_page(
    tmp_path: Path, loom: _FakeLoom, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(cli_okf, "_PAGE", 2)
    monkeypatch.setattr(cli_okf, "_BULK", 2)
    root = _bundle(tmp_path / "b", [_entry(f"k{i}") for i in range(5)])
    assert _run(root) == 0
    loom.calls.clear()
    assert _run(root) == 0
    assert loom.writes() == []
    offsets = [b["offset"] for op, b in loom.calls if op == "memory.list"]
    assert offsets == [0, 2, 4]
    assert [len(b["memory_keys"]) for op, b in loom.calls if op == "memory.bulk_get"] == [2, 2, 1]


def test_prune_deletes_only_rows_the_import_wrote(tmp_path: Path, loom: _FakeLoom) -> None:
    # Each run exports to a fresh directory of the same name, as the CronJob does.
    assert _run(_bundle(tmp_path / "1" / "b", [_entry("a"), _entry("gone")])) == 0
    # Same agent, not stamped by an import: someone else's row under that agent.
    loom.put("okf-sync", "hand-written", source_trace="cli:manual", tags=[])
    # Another writer's row with an import-looking trace.
    loom.put("bot", "bot-note", source_trace="okf:b/x.md", tags=[])
    loom.calls.clear()

    assert _run(_bundle(tmp_path / "2" / "b", [_entry("a")]), "--prune") == 0

    deleted = [b["memory_key"] for op, b in loom.calls if op == "memory.delete"]
    assert deleted == ["gone"]
    assert ("okf-sync", "-", "hand-written") in loom.rows
    assert ("bot", "-", "bot-note") in loom.rows


def test_prune_dry_run_lists_and_writes_nothing(
    tmp_path: Path, loom: _FakeLoom, capsys: pytest.CaptureFixture[str]
) -> None:
    assert _run(_bundle(tmp_path / "1" / "b", [_entry("a"), _entry("gone")])) == 0
    loom.calls.clear()
    root = _bundle(tmp_path / "2" / "b", [_entry("a", tags=["changed"])])
    assert _run(root, "--prune", "--dry-run") == 0
    assert loom.writes() == []
    out = capsys.readouterr().out
    assert "would delete gone" in out
    assert "dry run: 1 of 1 rows" in out
    assert "1 would be pruned" in out


def test_prune_refuses_an_empty_bundle(tmp_path: Path, loom: _FakeLoom) -> None:
    assert _run(tmp_path, "--prune", "--no-lint") == 2
    assert loom.calls == []


def test_prune_refuses_a_partial_or_edited_bundle(tmp_path: Path, loom: _FakeLoom) -> None:
    root = _bundle(tmp_path / "b", [_entry("a"), _entry("b")])
    (root / "semantic" / "b.md").unlink()  # the export stopped before this file
    assert _run(root, "--prune", "--no-lint") == 2
    assert loom.calls == []


def test_prune_refuses_a_bundle_without_a_corpus_digest(tmp_path: Path, loom: _FakeLoom):
    root = _bundle(tmp_path / "b", [_entry("a")])
    index = root / "index.md"
    index.write_text(index.read_text().replace("corpus_digest:", "digest_was:"))
    assert _run(root, "--prune", "--no-lint") == 2
    assert loom.calls == []


def test_timestamps_compare_as_utc_whole_seconds() -> None:
    assert cli_okf._when("2026-08-30T08:00:00.123456Z") == datetime(2026, 8, 30, 8)
    assert cli_okf._when("2026-08-30T10:00:00+02:00") == datetime(2026, 8, 30, 8)
    assert cli_okf._when("next tuesday") != cli_okf._when(None)


def test_a_key_held_by_two_writers_is_written_once_and_then_left(
    tmp_path: Path, loom: _FakeLoom
) -> None:
    older = _entry("k", agent="a", value="old wording", updated_at="2026-08-01T00:00:00")
    newer = _entry("k", agent="b", value="new wording", updated_at="2026-08-02T00:00:00")
    root = _bundle(tmp_path / "b", [newer, older])
    assert _run(root) == 0
    sets = [b for op, b in loom.calls if op == "memory.set"]
    assert [b["value"] for b in sets] == ["new wording"]
    loom.calls.clear()
    assert _run(root) == 0
    assert loom.writes() == []


def test_entities_the_target_extracted_are_not_a_change(tmp_path: Path, loom: _FakeLoom):
    root = _bundle(tmp_path / "b", [_entry("a", entities=[])])
    assert _run(root) == 0
    loom.rows[("okf-sync", "-", "a")]["entities"] = ["mergetree"]  # the target's extractor
    loom.calls.clear()
    assert _run(root) == 0
    assert loom.writes() == []


def test_prune_refuses_a_limited_export(tmp_path: Path, loom: _FakeLoom) -> None:
    root = _bundle(tmp_path / "b", [_entry("a")], limit=1)
    assert _run(root, "--prune", "--no-lint") == 2
    assert loom.calls == []


def test_prune_never_reaches_another_writers_row_through_the_delete_fallback(
    tmp_path: Path,
    loom: _FakeLoom,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert _run(_bundle(tmp_path / "1" / "b", [_entry("a"), _entry("gone")])) == 0
    loom.put("bot", "gone", source_trace="cli:bot", tags=[])
    listed = loom.post

    async def tombstoned_after_the_read(op: str, body: dict[str, Any]) -> dict[str, Any]:
        out = await listed(op, body)
        if op == "memory.list":
            loom.rows.pop(("okf-sync", "-", "gone"), None)  # someone deletes our copy
        return out

    monkeypatch.setattr(loom, "post", tombstoned_after_the_read)
    loom.calls.clear()
    capsys.readouterr()
    assert _run(_bundle(tmp_path / "2" / "b", [_entry("a")]), "--prune") == 0
    assert "memory.delete" not in loom.writes()
    assert ("bot", "-", "gone") in loom.rows
    assert "0 pruned\n" in capsys.readouterr().out  # not ours any more, so not "kept"


def test_verify_re_attests_the_row_in_its_own_session(tmp_path: Path, loom: _FakeLoom) -> None:
    entry = _entry(
        "a", session_id="s1", verification="human", verified_by="human:alice@example.com"
    )
    assert _run(_bundle(tmp_path / "b", [entry]), "--keep-sessions", "--verify") == 0
    verify = [b for op, b in loom.calls if op == "memory.verify"]
    assert [b["session_id"] for b in verify] == ["s1"]


def test_verify_re_attests_a_skipped_row_only_when_it_lost_its_attestation(
    tmp_path: Path, loom: _FakeLoom
) -> None:
    entry = _entry("a", verification="human", verified_by="human:alice@example.com")
    root = _bundle(tmp_path / "b", [entry])
    assert _run(root, "--verify") == 0
    loom.rows[("okf-sync", "-", "a")]["verification"] = "process"
    loom.calls.clear()
    assert _run(root, "--verify") == 0
    assert [op for op, _ in loom.calls if op in ("memory.set", "memory.verify")] == []

    for lost in ("stale", "none"):
        loom.rows[("okf-sync", "-", "a")]["verification"] = lost
        loom.calls.clear()
        assert _run(root, "--verify") == 0
        verify = [b for op, b in loom.calls if op == "memory.verify"]
        assert [(b["session_id"], b["memory_key"]) for b in verify] == [("-", "a")]
        assert "memory.set" not in loom.writes()


def test_an_edge_stored_under_its_canonical_verb_is_not_linked_again(
    tmp_path: Path, loom: _FakeLoom
) -> None:
    # memory.link stores `a blocked_by c` as `c blocks a`.
    edge = {"from_key": "a", "to_key": "c", "relation": "blocked_by", "author": "x"}
    root = _bundle(tmp_path / "b", [_entry("a"), _entry("c")], edges=[edge])
    assert _run(root) == 0
    assert loom.edges == [{"from_key": "c", "to_key": "a", "relation": "blocks"}]
    loom.calls.clear()
    assert _run(root) == 0
    assert loom.writes() == []


def test_a_symlinked_bundle_keeps_the_name_it_was_given(tmp_path: Path, loom: _FakeLoom):
    current = tmp_path / "current"
    current.symlink_to(_bundle(tmp_path / "run-1", [_entry("a")]))
    assert _run(current) == 0
    assert loom.rows[("okf-sync", "-", "a")]["source_trace"] == "okf:current/semantic/a.md"
    current.unlink()
    current.symlink_to(_bundle(tmp_path / "run-2", [_entry("a")]))
    loom.calls.clear()
    assert _run(current) == 0
    assert loom.writes() == []


def test_an_edge_on_every_writer_of_a_key_is_linked_once(tmp_path: Path, loom: _FakeLoom):
    edge = {"from_key": "k", "to_key": "c", "relation": "supports", "author": "x"}
    entries = [
        _entry("k", agent="a", updated_at="2026-08-01T00:00:00"),
        _entry("k", agent="b", updated_at="2026-08-02T00:00:00"),
        _entry("c"),
    ]
    assert _run(_bundle(tmp_path / "b", entries, edges=[edge])) == 0
    assert [op for op in loom.writes() if op == "memory.link"] == ["memory.link"]


def test_prune_with_keep_sessions_deletes_only_the_session_that_lost_the_key(
    tmp_path: Path, loom: _FakeLoom
) -> None:
    both = [_entry("k", session_id="s1"), _entry("k", session_id="s2", agent="writer-b")]
    assert _run(_bundle(tmp_path / "1" / "b", both), "--keep-sessions") == 0
    assert {(s, k) for (_a, s, k) in loom.rows} == {("s1", "k"), ("s2", "k")}
    loom.calls.clear()
    root = _bundle(tmp_path / "2" / "b", [_entry("k", session_id="s1")])
    assert _run(root, "--keep-sessions", "--prune") == 0
    deleted = [(b["session_id"], b["memory_key"]) for op, b in loom.calls if op == "memory.delete"]
    assert deleted == [("s2", "k")]
    assert ("okf-sync", "s1", "k") in loom.rows


def test_a_tag_the_target_adds_is_not_a_change(tmp_path: Path, loom: _FakeLoom) -> None:
    root = _bundle(tmp_path / "b", [_entry("a", value="mail ops@example.com on a failure")])
    assert _run(root) == 0
    assert "sensitivity:medium" in loom.rows[("okf-sync", "-", "a")]["tags"]
    loom.calls.clear()
    assert _run(root) == 0
    assert loom.writes() == []


def test_prune_keeps_a_row_another_token_wrote_under_the_same_agent(
    tmp_path: Path, loom: _FakeLoom
) -> None:
    assert _run(_bundle(tmp_path / "1" / "b", [_entry("a")])) == 0
    # Someone else ran an import with `--agent okf-sync`: same agent and trace, other actor.
    loom.put("okf-sync", "theirs", source_trace="okf:other/x.md", tags=[], actor="human:bob")
    loom.calls.clear()
    assert _run(_bundle(tmp_path / "2" / "b", [_entry("a")]), "--prune") == 0
    assert "memory.delete" not in loom.writes()
    assert ("okf-sync", "-", "theirs") in loom.rows


def test_a_concept_merged_into_another_key_is_counted_apart(
    tmp_path: Path,
    loom: _FakeLoom,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    serve = loom.post

    async def merges(op: str, body: dict[str, Any]) -> dict[str, Any]:
        if op == "memory.set":
            loom.calls.append((op, body))
            return {"status": "", "conflict_verdict": "merge"}
        return await serve(op, body)

    monkeypatch.setattr(loom, "post", merges)
    assert _run(_bundle(tmp_path / "b", [_entry("a")])) == 0
    assert "0 written, 0 unchanged, 0 failed, 1 merged into another key" in capsys.readouterr().out


def test_prune_refuses_an_export_into_a_reused_directory(tmp_path: Path, loom: _FakeLoom):
    root = _bundle(tmp_path / "b", [_entry("a"), _entry("gone")])
    _bundle(root, [_entry("a")])  # the file of `gone` stays behind
    assert _run(root, "--prune", "--no-lint") == 2
    assert loom.calls == []


def test_prune_needs_an_explicit_agent(tmp_path: Path, loom: _FakeLoom) -> None:
    root = _bundle(tmp_path / "b", [_entry("a")])
    assert _run(root, "--prune", agent=None) == 2
    assert _run(root, "--prune", agent="") == 2
    assert loom.calls == []


def test_two_bundles_in_one_namespace_never_prune_each_other(tmp_path: Path, loom: _FakeLoom):
    assert _run(_bundle(tmp_path / "a" / "kb", [_entry("from-a")]), agent="sync-a") == 0
    root_b = _bundle(tmp_path / "b" / "kb", [_entry("from-b")])
    assert _run(root_b, "--prune", agent="sync-b") == 0
    assert "memory.delete" not in loom.writes()
    assert ("sync-a", "-", "from-a") in loom.rows


def test_prune_dry_run_skips_and_counts_rows_another_token_wrote(
    tmp_path: Path, loom: _FakeLoom, capsys: pytest.CaptureFixture[str]
) -> None:
    assert _run(_bundle(tmp_path / "1" / "b", [_entry("a")])) == 0
    loom.put("okf-sync", "theirs", source_trace="okf:other/x.md", tags=[], actor="human:bob")
    capsys.readouterr()
    assert _run(_bundle(tmp_path / "2" / "b", [_entry("a")]), "--prune", "--dry-run") == 0
    out = capsys.readouterr().out
    assert "would delete" not in out
    assert "0 would be pruned (1 kept: written by another token under this --agent)" in out


def test_a_limit_the_export_did_not_reach_is_not_recorded(
    tmp_path: Path, loom: _FakeLoom, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(cli_okf, "_server_build", lambda _url: "")
    assert _run(_bundle(tmp_path / "b", [_entry("a"), _entry("c")])) == 0
    assert "limit" not in _export(tmp_path / "all", "--limit", "10")
    assert _export(tmp_path / "part", "--limit", "1")["limit"] == 1


_LIST = ["content_sha256", "memory_type", "source_trace", "tags", "confidence", "entities"]


@pytest.mark.parametrize(
    ("read", "field"),
    [("_LIVE_FIELDS", f) for f in [*_LIST, "valid_at"]]
    + [("_BULK_FIELDS", "valid_to"), ("_BULK_FIELDS", "source_class")],
    ids=lambda v: str(v),
)
def test_a_field_the_import_does_not_read_would_rewrite_every_row(
    tmp_path: Path, loom: _FakeLoom, monkeypatch: pytest.MonkeyPatch, read: str, field: str
) -> None:
    # The fake returns only the fields asked for, so the reads must name each one compared.
    entry = _entry("a", valid_to="2026-12-01T00:00:00", source_class="derived")
    root = _bundle(tmp_path / "b", [entry])
    assert _run(root) == 0
    loom.calls.clear()
    assert _run(root) == 0
    assert loom.writes() == []
    monkeypatch.setattr(cli_okf, read, [f for f in getattr(cli_okf, read) if f != field])
    loom.calls.clear()
    assert _run(root) == 0
    assert loom.writes() == ["memory.set"]


def test_a_concept_with_a_source_is_written_on_every_run(tmp_path: Path, loom: _FakeLoom):
    root = _bundle(tmp_path / "b", [_entry("a", source_resource_id="doc://runbook")])
    assert _run(root) == 0
    loom.calls.clear()
    assert _run(root) == 0
    assert loom.writes() == ["memory.set"]


def _failing(loom: _FakeLoom, monkeypatch: pytest.MonkeyPatch, failing_op: str) -> None:
    serve = loom.post

    async def post(op: str, body: dict[str, Any]) -> dict[str, Any]:
        if op == failing_op:
            loom.calls.append((op, body))
            raise RuntimeError(f"{op} → 503")
        return await serve(op, body)

    monkeypatch.setattr(loom, "post", post)


def test_a_target_that_cannot_be_read_exits_1_before_any_write(
    tmp_path: Path,
    loom: _FakeLoom,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _failing(loom, monkeypatch, "memory.list")
    assert _run(_bundle(tmp_path / "b", [_entry("a")])) == 1
    assert loom.writes() == []
    assert "cannot read acme/dst" in capsys.readouterr().err


@pytest.mark.parametrize("failing_op", ["memory.set", "memory.link", "memory.delete"])
def test_a_failed_write_exits_1(
    tmp_path: Path, loom: _FakeLoom, monkeypatch: pytest.MonkeyPatch, failing_op: str
) -> None:
    edge = {"from_key": "a", "to_key": "c", "relation": "supports", "author": "x"}
    assert _run(_bundle(tmp_path / "1" / "b", [_entry("a"), _entry("gone")])) == 0
    _failing(loom, monkeypatch, failing_op)
    root = _bundle(tmp_path / "2" / "b", [_entry("a", value="new"), _entry("c")], edges=[edge])
    assert _run(root, "--prune") == 1


def test_a_failed_evidence_batch_exits_1_and_stays_out_of_the_row_count(
    tmp_path: Path,
    loom: _FakeLoom,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    async def evidence_fails(*_a: Any) -> dict[str, int]:
        keys = ("chunks_written", "links_written", "digests_written", "skipped", "edited")
        return dict.fromkeys(keys, 0) | {"failed": 64}

    monkeypatch.setattr(cli_okf, "_walk_evidence", lambda _root: ([object()], []))
    monkeypatch.setattr(cli_okf, "_import_evidence", evidence_fails)
    assert _run(_bundle(tmp_path / "b", [_entry("a")])) == 1
    out = capsys.readouterr().out
    assert "64 FAILED" in out
    assert "1 written, 0 unchanged, 0 failed" in out


def test_a_key_renamed_in_the_source_is_written_under_its_new_name(
    tmp_path: Path, loom: _FakeLoom, monkeypatch: pytest.MonkeyPatch
) -> None:
    serve = loom.post

    async def merges_into_a_live_copy(op: str, body: dict[str, Any]) -> dict[str, Any]:
        # memory.set folds a value another live key already holds into that key's row.
        if op == "memory.set" and any(
            r["memory_key"] != body["memory_key"] and r.get("value") == body["value"]
            for r in loom.rows.values()
        ):
            loom.calls.append((op, body))
            return {"status": "", "conflict_verdict": "merge"}
        return await serve(op, body)

    assert _run(_bundle(tmp_path / "1" / "b", [_entry("x", value="same fact")])) == 0
    monkeypatch.setattr(loom, "post", merges_into_a_live_copy)
    assert _run(_bundle(tmp_path / "2" / "b", [_entry("y", value="same fact")]), "--prune") == 0
    assert [k for (_a, _s, k) in loom.rows] == ["y"]


def test_the_server_bulk_read_returns_every_field_the_import_compares() -> None:
    from server.memory.store import reads

    for sql in (reads._BULK_FETCH_SQL, reads._BULK_FETCH_WHOLE_SQL):
        select = sql.split("FROM")[0]
        assert all(f in select for f in cli_okf._BULK_FIELDS), sql
