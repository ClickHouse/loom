"""OKF bundle ⇄ memory rows: the mapping round-trips, the files are valid
OKF v0.2 shapes (frontmatter, actor strings, index listing lines, relative
links), and the optional conformance test runs the reference `okf` binary
when one is on PATH or named by $OKF_BIN."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
import yaml

from server.memory.domain import okf_bundle as okf

_NOW = datetime(2026, 9, 11, 12, 0, tzinfo=UTC)


def _root_pointer(text: str) -> dict[str, Any]:
    """The `loom:` mapping of a bundle's root `index.md` ({} when absent)."""
    fm_text, _body, has = okf.split_frontmatter(text)
    if not has:
        return {}
    raw = (yaml.safe_load(fm_text) or {}).get("loom")
    return dict(raw) if isinstance(raw, dict) else {}


def _entry(**over: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "memory_key": "deploy_window",
        "memory_type": "semantic",
        "value": "Tue and Thu only; never Friday.",
        "tags": ["ops", "project:loom"],
        "agent": "claude-code",
        "namespace": "coding",
        "memory_id": "11111111-1111-1111-1111-111111111111",
        "created_at": "2026-09-01T10:00:00",
        "status": "active",
        "confidence": 1.0,
        "content_sha256": "abc",
        "entities": ["deploy", "loom"],
    }
    base.update(over)
    return base


# ---------------- memory → concept ----------------


def test_concept_carries_okf_families_and_loom_identity() -> None:
    c = okf.memory_to_concept(
        _entry(
            verification="human",
            verified_by="human:alice",
            verified_at="2026-09-10T09:00:00",
            source_resource_id="docs/runbook.md",
            expires_at="2026-12-01T00:00:00",
        )
    )
    assert c.id == "semantic/deploy_window" and c.type == "Fact"
    assert c.title == "Deploy window"
    assert c.description == "Tue and Thu only; never Friday."
    assert c.generated == {"by": "agent:claude-code", "at": "2026-09-01T10:00:00Z"}
    assert c.verified == [{"by": "human:alice", "at": "2026-09-10T09:00:00Z"}]
    assert c.status == "stable" and c.stale_after == "2026-12-01"
    assert c.sources == [{"resource": "docs/runbook.md", "id": "src"}]
    assert c.extra["loom"]["memory_key"] == "deploy_window"
    assert c.extra["loom"]["memory_id"].startswith("1111")
    assert c.extra["loom"]["entities"] == ["deploy", "loom"]


def test_stale_or_missing_verification_is_not_exported_as_verified() -> None:
    for state in ("stale", "none", None):
        c = okf.memory_to_concept(_entry(verification=state, verified_by="human:alice"))
        assert c.verified == [], state


def test_status_maps_onto_okf_lifecycle() -> None:
    assert okf.memory_to_concept(_entry(status="conflicted")).status == "draft"
    assert okf.memory_to_concept(_entry(status="superseded")).status == "deprecated"
    assert okf.memory_to_concept(_entry(tombstoned_at="2026-09-02T00:00:00")).status == "deprecated"


def test_json_values_become_a_fenced_block_and_come_back() -> None:
    v = {"window": ["tue", "thu"], "never": "fri"}
    c = okf.memory_to_concept(_entry(value=v))
    assert c.body.startswith("```json") and c.description == "Fact Deploy window"
    assert okf.body_to_value(c.body) == v


def test_description_is_one_line_bounded_and_delimiter_free() -> None:
    long = "First sentence here. " * 20 + "\n---\nsecond --- line"
    c = okf.memory_to_concept(_entry(value=long))
    assert "\n" not in c.description and "---" not in c.description
    assert len(c.description) <= okf.MAX_DESCRIPTION + 1


def test_slug_is_file_safe_and_collisions_get_suffixes() -> None:
    assert okf.slugify("user/prefs: dark mode!") == "user-prefs-dark-mode"
    files = okf.plan_bundle(
        [_entry(memory_key="a/b"), _entry(memory_key="a b")], namespace="ns", exported_at=_NOW
    )
    assert "semantic/a-b.md" in files and "semantic/a-b-1.md" in files


# ---------------- serialisation shapes ----------------


def test_serialized_concept_is_valid_frontmatter_with_valid_actors() -> None:
    c = okf.memory_to_concept(
        _entry(verification="process", verified_by="process:ci", verified_at=_NOW)
    )
    text = okf.serialize_concept(c)
    fm_text, body, has = okf.split_frontmatter(text)
    assert has
    fm = yaml.safe_load(fm_text)
    assert fm["type"] == "Fact" and fm["title"] == "Deploy window"
    assert okf._ACTOR_RE.match(fm["generated"]["by"])
    assert okf._ACTOR_RE.match(fm["verified"][0]["by"])
    assert body.strip().startswith("# Deploy window")
    # okf's own YAML reader is line-oriented: nested `loom:` must be a block mapping
    assert re.search(r"^loom:\n  namespace:", fm_text, re.M)


def test_bundle_has_root_index_sub_indexes_log_and_drift_free_listings() -> None:
    files = okf.plan_bundle(
        [
            _entry(),
            _entry(
                memory_key="rollback_steps",
                memory_type="procedural",
                value="1. stop 2. revert 3. verify",
                entities=["deploy"],
            ),
        ],
        namespace="coding",
        exported_at=_NOW,
    )
    assert set(files) >= {
        "index.md",
        "log.md",
        "semantic/index.md",
        "procedural/index.md",
        "semantic/deploy_window.md",
        "procedural/rollback_steps.md",
    }
    root = files["index.md"]
    assert root.startswith('---\nokf_version: "0.2"\nloom:\n  corpus_digest: ')
    # The generation pointer names the concepts, not the indexes
    pointer = _root_pointer(root)
    assert pointer["concepts"] == 2 and pointer["exported_at"] == "2026-09-11"
    assert pointer["corpus_digest"] == okf.corpus_digest(files) and "build_commit" not in pointer
    assert "complete" not in pointer  # import-okf --prune refuses it
    whole = okf.plan_bundle([_entry()], namespace="ns", exported_at=_NOW, complete=True)
    assert _root_pointer(whole["index.md"])["complete"] is True
    assert "* [Deploy window](semantic/deploy_window.md) - Tue and Thu only; never Friday." in root
    sub = files["semantic/index.md"]
    assert not sub.startswith("---")  # sub-indexes carry no frontmatter
    assert "* [Deploy window](deploy_window.md) - Tue and Thu only; never Friday." in sub
    assert files["log.md"].startswith("## 2026-09-11\n* **Export**:")
    # listing description == concept description (okf validate --drift)
    c = okf.parse_concept("semantic/deploy_window.md", files["semantic/deploy_window.md"])
    assert f"- {c.description}" in sub


def test_shared_entities_produce_symmetric_relative_links_and_no_orphans() -> None:
    files = okf.plan_bundle(
        [
            _entry(memory_key="deploy_window", entities=["deploy"]),
            _entry(memory_key="rollback_steps", memory_type="procedural", entities=["deploy"]),
            _entry(memory_key="lonely_fact", entities=["nothing-shared"]),
        ],
        namespace="coding",
        exported_at=_NOW,
    )
    a = files["semantic/deploy_window.md"]
    b = files["procedural/rollback_steps.md"]
    assert (
        "# Related Concepts\n- [Rollback steps](../procedural/rollback_steps.md): shares entity deploy"
        in a
    )
    assert "- [Deploy window](../semantic/deploy_window.md): shares entity deploy" in b
    assert okf.bundle_orphans(files) == ["semantic/lonely_fact"]
    assert "1 without links" in files["log.md"]


def test_relative_link_resolution() -> None:
    assert okf.relative_link("semantic/a", "semantic/b") == "b.md"
    assert okf.relative_link("semantic/a", "procedural/b") == "../procedural/b.md"
    assert okf.relative_link("a", "x/b") == "x/b.md"


# ---------------- concept → memory (round trip) ----------------


def test_round_trip_preserves_key_type_value_tags_and_source() -> None:
    src = _entry(
        value="Tue and Thu only; never Friday.",
        source_resource_id="docs/runbook.md",
        valid_at="2026-08-01T00:00:00",
    )
    files = okf.plan_bundle(
        [src, _entry(memory_key="other", entities=["deploy"])], namespace="coding", exported_at=_NOW
    )
    c = okf.parse_concept("semantic/deploy_window.md", files["semantic/deploy_window.md"])
    back = okf.concept_to_memory(c)
    assert back["memory_key"] == "deploy_window"
    assert back["memory_type"] == "semantic"
    assert back["value"] == src["value"]  # heading + related section stripped
    assert back["tags"][:2] == ["ops", "project:loom"] and "okf:type:Fact" in back["tags"]
    assert back["source_resource_id"] == "docs/runbook.md"
    assert back["entities"] == ["deploy", "loom"]
    assert back["valid_at"] == "2026-08-01T00:00:00"
    assert back["confidence"] == 1.0


def test_round_trip_json_value() -> None:
    v = {"a": [1, 2], "b": "x"}
    files = okf.plan_bundle([_entry(value=v)], namespace="ns", exported_at=_NOW)
    c = okf.parse_concept("semantic/deploy_window.md", files["semantic/deploy_window.md"])
    assert okf.concept_to_memory(c)["value"] == v


def test_foreign_okf_concept_maps_by_type_and_folder() -> None:
    text = (
        "---\ntype: Decision\ntitle: Ed25519 Stateless JWT Tokens\n"
        "description: Adopt Ed25519 JWTs.\ntags: [decision, jwt]\n"
        "generated: { by: agent/gemini, at: 2026-08-27T12:00:00Z }\nstatus: stable\n"
        "verified:\n  - by: human:lead\n    at: 2026-08-28T09:00:00Z\n"
        "sources:\n  - resource: docs/RFC-004.md\n    id: rfc\n---\n\n"
        "# Decision: Ed25519 Stateless JWT Tokens\n\n## Context\nFast verification.\n"
    )
    c = okf.parse_concept("decisions/jwt-tokens.md", text)
    assert c.type == "Decision" and c.tags == ["decision", "jwt"]
    assert c.generated == {"by": "agent/gemini", "at": "2026-08-27T12:00:00Z"}
    assert okf.human_verified_actor(c) == "human:lead"
    m = okf.concept_to_memory(c)
    assert m["memory_key"] == "jwt-tokens"  # file name: no loom identity
    assert m["memory_type"] == "semantic"  # Decision → semantic
    assert m["value"].startswith("# Decision: Ed25519")  # a foreign heading is content
    assert m["source_resource_id"] == "docs/RFC-004.md"
    assert "okf:type:Decision" in m["tags"]


def test_unknown_frontmatter_keys_survive_parse() -> None:
    c = okf.parse_concept("x/y.md", "---\ntype: Fact\ncustom_key: 42\nnested:\n  a: 1\n---\nbody\n")
    assert c.extra == {"custom_key": 42, "nested": {"a": 1}}


def test_memory_type_mapping_is_total() -> None:
    assert okf.okf_type_for("semantic") == "Fact" and okf.okf_type_for("weird_type") == "Weird_type"
    assert okf.memory_type_for("Fact") == "semantic"
    assert okf.memory_type_for("Runbook") == "procedural"
    assert okf.memory_type_for("Client", "coaching") == "client"
    assert okf.memory_type_for("Event", "episodic") == "episodic"


# ---------------- conformance against the reference validator ----------------


def _okf_bin() -> str | None:
    return os.environ.get("OKF_BIN") or shutil.which("okf")


@pytest.mark.skipif(_okf_bin() is None, reason="reference `okf` binary not available (set OKF_BIN)")
def test_planned_bundle_passes_the_reference_validator(tmp_path: Path) -> None:
    files = okf.plan_bundle(
        [
            _entry(
                memory_key="deploy_window",
                entities=["deploy"],
                verification="human",
                verified_by="human:alice",
                verified_at="2026-09-10T09:00:00",
                source_resource_id="docs/runbook.md",
                expires_at="2027-01-01T00:00:00",
            ),
            _entry(
                memory_key="rollback_steps",
                memory_type="procedural",
                value="1. stop\n2. revert\n3. verify",
                entities=["deploy"],
            ),
            _entry(
                memory_key="prefs",
                memory_type="preference",
                value={"theme": "dark"},
                entities=["deploy"],
            ),
        ],
        namespace="coding",
        exported_at=_NOW,
    )
    for rel, text in files.items():
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
    out = subprocess.run(
        [_okf_bin() or "okf", "validate", str(tmp_path), "--strict", "--drift", "--json"],
        capture_output=True,
        text=True,
        timeout=60,
    )
    report = json.loads(out.stdout)
    assert report["is_conformant"] is True, report
    assert report["errors"] == [] and report["warnings"] == [], report
    assert report["gate_passed"] is True, report
    assert report["concept_count"] == 3


def test_a_copy_remembers_where_it_came_from() -> None:
    """The exporter writes org / namespace / owner into the
    concept; an import into another place keeps them as origin:* tags; an
    import back into the same org and namespace carries none."""
    from server.memory.domain.okf_bundle import concept_to_memory, memory_to_concept, origin_tags

    entry = {
        "org": "acme",
        "namespace": "shop",
        "user_id": "u1",
        "agent": "bot",
        "memory_key": "user_editor",
        "memory_type": "preference",
        "value": "vim",
        "tags": ["editor"],
        "created_at": "2026-09-12T10:00:00Z",
        "status": "active",
    }
    c = memory_to_concept(entry)
    loom = c.extra["loom"]
    assert loom["org"] == "acme" and loom["namespace"] == "shop" and loom["user_id"] == "u1"
    # a row with no user carries no owner
    assert "user_id" not in memory_to_concept({**entry, "user_id": "-"}).extra["loom"]

    away = concept_to_memory(c, target_org="acme", target_namespace="support")
    assert [t for t in away["tags"] if t.startswith("origin:")] == [
        "origin:org:acme",
        "origin:namespace:shop",
        "origin:user:u1",
        "origin:memory_key:user_editor",
    ]
    assert away["source_class"] == "imported"
    other_org = concept_to_memory(c, target_org="globex", target_namespace="shop")
    assert "origin:org:acme" in other_org["tags"]
    home = concept_to_memory(c, target_org="acme", target_namespace="shop")
    assert not [t for t in home["tags"] if t.startswith("origin:")]
    assert "source_class" not in home
    # no target named (older callers): unchanged behaviour
    assert not [t for t in concept_to_memory(c)["tags"] if t.startswith("origin:")]
    # a foreign OKF concept with no loom block has no origin to remember
    assert origin_tags({}, target_org="acme", target_namespace="x") == []


def test_bundle_keeps_the_writers_session_and_import_can_use_it() -> None:
    """The same key under two sessions is two concepts
    (slug-deduped), each carrying its session; a keep-sessions import reads
    it back, a plain import ignores it."""
    a = _entry(session_id="conv:session_3", value="I went camping two weekends ago.")
    b = _entry(session_id="conv:session_9", value="I went camping with the kids.")
    c = _entry(session_id="-")
    files = okf.plan_bundle([a, b, c], namespace="coding", exported_at=_NOW)
    concept_files = {
        k: v for k, v in files.items() if k.startswith("semantic/") and not k.endswith("index.md")
    }
    assert len(concept_files) == 3  # one file per row, not per key
    back = [okf.parse_concept(k, v) for k, v in sorted(concept_files.items())]
    sessions = sorted(okf.concept_session_id(x) for x in back)
    assert sessions == ["", "conv:session_3", "conv:session_9"]
    for x in back:
        assert "session_id" not in okf.concept_to_memory(x)  # identity stays the caller's


# ---------------- typed edges and provenance ----------------


def _edge(from_key: str, to_key: str, relation: str, confidence: float = 1.0) -> dict[str, Any]:
    return {
        "from_key": from_key,
        "to_key": to_key,
        "relation": relation,
        "confidence": confidence,
        "explanation": "",
        "created_at": "",
    }


def test_authored_edges_become_relates_to_and_the_digest_chain_stays_home() -> None:
    entries = [
        _entry(memory_key="deploy_window", entities=[]),
        _entry(memory_key="rollback_steps", memory_type="procedural", entities=[]),
        _entry(memory_key="freeze_rule", entities=[]),
    ]
    edges = [
        _edge("rollback_steps", "deploy_window", "supports"),
        _edge("rollback_steps", "deploy_window", "supports"),  # duplicate row
        _edge("deploy_window", "freeze_rule", "precedes"),  # digest chain: excluded
        _edge("freeze_rule", "deploy_window", "refines", 0.6),
        _edge("freeze_rule", "not_exported", "supports"),  # other end missing
        _edge("freeze_rule", "freeze_rule", "supports"),  # self edge
    ]
    files = okf.plan_bundle(entries, namespace="coding", exported_at=_NOW, edges=edges)
    rb = okf.parse_concept("procedural/rollback_steps.md", files["procedural/rollback_steps.md"])
    assert rb.relates_to == [{"path": "semantic/deploy_window.md", "kind": "supports"}]
    fr = okf.parse_concept("semantic/freeze_rule.md", files["semantic/freeze_rule.md"])
    assert fr.relates_to == [
        {"path": "semantic/deploy_window.md", "kind": "refines", "confidence": "0.60"}
    ]
    dw = okf.parse_concept("semantic/deploy_window.md", files["semantic/deploy_window.md"])
    assert dw.relates_to == []  # precedes never lands
    # frontmatter is authoritative; the body mirrors the edge with its kind
    assert (
        "# Related Concepts\n- [Deploy window](../semantic/deploy_window.md): supports"
        in (files["procedural/rollback_steps.md"])
    )
    # no shared entities, but the edges make the graph: nobody is an orphan
    assert okf.bundle_orphans(files) == []
    assert "2 typed edges in `relates_to`" in files["log.md"]
    # the body mirror is stripped on the way back; the value round-trips
    assert okf.concept_to_memory(rb)["value"] == "Tue and Thu only; never Friday."


def test_supersedes_is_written_on_the_newer_row_from_superseded_by() -> None:
    newer = _entry(memory_key="deploy_window", memory_id="aaaaaaaa-0000-0000-0000-000000000001")
    older = _entry(
        memory_key="deploy_window_v1",
        status="superseded",
        superseded_by="aaaaaaaa-0000-0000-0000-000000000001",
        memory_id="aaaaaaaa-0000-0000-0000-000000000002",
        valid_to="2026-08-01T00:00:00",
    )
    files = okf.plan_bundle(
        [newer, older],
        namespace="coding",
        exported_at=_NOW,
        edges=[_edge("deploy_window", "deploy_window_v1", "supersedes")],
    )
    n = okf.parse_concept("semantic/deploy_window.md", files["semantic/deploy_window.md"])
    assert n.relates_to == [{"path": "semantic/deploy_window_v1.md", "kind": "supersedes"}]
    o = okf.parse_concept("semantic/deploy_window_v1.md", files["semantic/deploy_window_v1.md"])
    assert o.relates_to == [] and o.status == "deprecated"
    assert o.extra["loom"]["superseded_by"] == "aaaaaaaa-0000-0000-0000-000000000001"
    assert o.extra["loom"]["valid_to"] == "2026-08-01T00:00:00Z"
    assert okf.concept_to_memory(o)["valid_to"] == "2026-08-01T00:00:00"


def test_relates_to_resolves_to_link_payloads_and_reports_foreign_targets() -> None:
    a = okf.parse_concept(
        "semantic/a.md",
        (
            "---\ntype: Fact\ntitle: A\nrelates_to:\n- path: semantic/b.md\n  kind: supports\n"
            "- path: loom://acme/other/c\n  kind: derived_from\n- path: semantic/missing.md\n"
            "  kind: refines\n- role: related\n  path: semantic/b.md\nloom:\n  memory_key: key_a\n---\n# A\n"
        ),
    )
    b = okf.parse_concept("semantic/b.md", "---\ntype: Fact\ntitle: B\n---\n# B\n")
    assert a.relates_to[3] == {"path": "semantic/b.md", "kind": "related"}
    links, unresolved = okf.resolve_relates_to([a, b])
    assert links == [
        {"from_key": "key_a", "to_key": "b", "relation": "supports", "path": "semantic/a.md"},
        {"from_key": "key_a", "to_key": "b", "relation": "related", "path": "semantic/a.md"},
    ]
    assert [u["target"] for u in unresolved] == ["loom://acme/other/c", "semantic/missing.md"]


def test_provenance_tier_travels_and_only_downgrades_are_declared() -> None:
    """Derived / imported / external keep their class through a
    round trip; a first-hand class is the server's to decide."""
    for cls in ("derived", "imported", "external"):
        c = okf.memory_to_concept(_entry(source_class=cls))
        assert c.extra["loom"]["source_class"] == cls
        assert okf.concept_to_memory(c)["source_class"] == cls
        assert (
            okf.concept_to_memory(c, target_org="acme", target_namespace="x")["source_class"] == cls
        )
    for cls in ("asserted", "extracted", "observed", ""):
        c = okf.memory_to_concept(_entry(source_class=cls))
        assert "source_class" not in okf.concept_to_memory(c)
        # a copy elsewhere is still `imported`
        assert (
            okf.concept_to_memory(c, target_org="acme", target_namespace="x")["source_class"]
            == "imported"
        )
    # empty tiers are dropped from the block so an unchanged row does not churn the diff
    c = okf.memory_to_concept(_entry())
    assert "source_class" not in c.extra["loom"] and "sensitivity" not in c.extra["loom"]
    assert okf.memory_to_concept(_entry(sensitivity="high")).extra["loom"]["sensitivity"] == "high"


def test_high_sensitivity_rows_are_not_exportable_by_default() -> None:
    assert okf.is_exportable(_entry(sensitivity="high")) is False
    assert okf.is_exportable(_entry(sensitivity="high"), include_high=True) is True
    assert okf.is_exportable(_entry(sensitivity="medium")) and okf.is_exportable(_entry())
    # A read that leaves the column empty: the tag memory.set adds still keeps it home.
    assert not okf.is_exportable(_entry(sensitivity="", tags=["sensitivity:high"]))


def test_corpus_digest_covers_concepts_only_and_moves_with_them() -> None:
    """The digest is over the concept files; the indexes, the
    log and the evidence planes are outside it."""
    from server.memory.domain import okf_lint

    a = okf.plan_bundle(
        [_entry(content_sha256="")], namespace="ns", exported_at=_NOW, build_commit="abc1234"
    )
    assert _root_pointer(a["index.md"])["build_commit"] == "abc1234"
    assert okf.corpus_digest(a) == okf_lint.bundle_digest(a)  # one definition
    assert [f.id for f in okf_lint.lint_files(a).warnings] == []  # the gate agrees
    assert [p for p in sorted(a) if okf.is_concept_path(p)] == ["semantic/deploy_window.md"]
    same = dict(a)
    same["log.md"] = "## another day\n"
    same["evidence/s1/00001.md"] = "---\ntype: Evidence\n---\nchunk\n"
    assert okf.corpus_digest(same) == okf.corpus_digest(a)
    edited = dict(a)
    edited["semantic/deploy_window.md"] = a["semantic/deploy_window.md"].replace("Thu", "Wed")
    assert okf.corpus_digest(edited) != okf.corpus_digest(a)
    b = okf.plan_bundle([_entry(), _entry(memory_key="other")], namespace="ns", exported_at=_NOW)
    assert okf.corpus_digest(b) != okf.corpus_digest(a)
    assert _root_pointer('---\nokf_version: "0.2"\n---\n# old bundle\n') == {}
