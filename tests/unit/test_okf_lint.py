"""`loom okf-lint`: every finding id on a broken bundle, the
exit contract, the JSON shape, and the acceptance line — a fresh export from
`plan_bundle` lints clean."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

from server.memory.domain import okf_bundle as okf
from server.memory.domain import okf_evidence as okf_ev
from server.memory.domain import okf_lint as lint

_NOW = datetime(2026, 9, 18, 12, 0, tzinfo=UTC)


def _entry(**over: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "memory_key": "deploy_window",
        "memory_type": "semantic",
        "value": "Tue and Thu only; never Friday.",
        "tags": ["ops"],
        "agent": "claude-code",
        "namespace": "coding",
        "org": "o",
        "memory_id": "11111111-1111-1111-1111-111111111111",
        "created_at": "2026-09-01T10:00:00",
        "status": "active",
        "confidence": 1.0,
        "content_sha256": okf_sha("Tue and Thu only; never Friday."),
        "entities": ["deploy"],
    }
    base.update(over)
    return base


def okf_sha(value: Any) -> str:
    from server.core.hashing import sha256_value

    return sha256_value(value)


def _fresh_files() -> dict[str, str]:
    return okf.plan_bundle(
        [
            _entry(),
            _entry(
                memory_key="rollback_steps",
                memory_type="procedural",
                value="1. stop 2. revert 3. verify",
                content_sha256=okf_sha("1. stop 2. revert 3. verify"),
                entities=["deploy"],
            ),
            _entry(
                memory_key="lonely_fact",
                value="Nothing shared here.",
                content_sha256=okf_sha("Nothing shared here."),
                entities=["solo"],
            ),
        ],
        namespace="coding",
        exported_at=_NOW,
    )


def _write(root: Path, files: dict[str, str]) -> None:
    for rel, text in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")


def _with_frontmatter(text: str, **changes: Any) -> str:
    fm_text, body, _ = okf.split_frontmatter(text)
    fm = yaml.safe_load(fm_text)
    for k, v in changes.items():
        if k.startswith("loom."):
            fm.setdefault("loom", {})[k[5:]] = v
        elif v is None:
            fm.pop(k, None)
        else:
            fm[k] = v
    head = yaml.safe_dump(fm, sort_keys=False, allow_unicode=True, width=1000).rstrip("\n")
    return f"---\n{head}\n---\n{body}"


# ---------------- acceptance line ----------------


def test_fresh_export_lints_clean_with_only_the_orphan_info() -> None:
    rep = lint.lint_files(_fresh_files())
    assert rep.criticals == [] and rep.warnings == []
    assert rep.exit_code == 0 and rep.ok
    assert rep.concepts == 3
    assert [f.id for f in rep.infos] == ["orphans"] and "semantic/lonely_fact.md" in rep.infos[
        0
    ].msg


def test_lint_bundle_reads_a_directory_and_json_has_the_contract_shape(tmp_path: Path) -> None:
    _write(tmp_path, _fresh_files())
    rep = lint.lint_bundle(tmp_path)
    d = json.loads(lint.report_json(rep))
    assert d["ok"] is True and d["exit_code"] == 0 and d["concepts"] == 3
    assert set(d) >= {"root", "critical", "warnings", "infos", "counts", "findings", "strict"}
    assert d["counts"] == {"orphans": 1}
    assert all(set(f) == {"id", "path", "msg", "severity"} for f in d["findings"])


def test_missing_directory_is_critical(tmp_path: Path) -> None:
    rep = lint.lint_bundle(tmp_path / "nope")
    assert rep.exit_code == 2 and rep.criticals[0].id == "root"


# ---------------- exit contract ----------------


def test_warnings_exit_1_and_strict_promotes_them_to_2() -> None:
    files = _fresh_files()
    files["semantic/deploy_window.md"] = _with_frontmatter(
        files["semantic/deploy_window.md"], **{"loom.sensitivity": "high"}
    )
    assert lint.lint_files(files).exit_code == 1
    assert lint.lint_files(files, strict=True).exit_code == 2
    assert "FAIL — warnings under --strict" in lint.lint_files(files, strict=True).render()


def test_a_critical_beats_everything() -> None:
    files = _fresh_files()
    files["semantic/deploy_window.md"] = "no frontmatter at all\n"
    rep = lint.lint_files(files)
    assert rep.exit_code == 2 and "FAIL — critical" in rep.render()
    assert rep.counts()["frontmatter"] == 1


# ---------------- finding ids ----------------


def test_frontmatter_missing_type_and_unparseable_yaml() -> None:
    files = _fresh_files()
    files["semantic/deploy_window.md"] = _with_frontmatter(
        files["semantic/deploy_window.md"], type=""
    )
    files["procedural/rollback_steps.md"] = "---\n: [unbalanced\n---\nbody\n"
    ids = [f.id for f in lint.lint_files(files).criticals]
    assert ids.count("frontmatter") == 2


def test_required_keys_and_type_directory_mismatch() -> None:
    files = _fresh_files()
    files["semantic/deploy_window.md"] = _with_frontmatter(
        files["semantic/deploy_window.md"], title="", **{"loom.memory_key": ""}
    )
    files["procedural/rollback_steps.md"] = _with_frontmatter(
        files["procedural/rollback_steps.md"], **{"loom.memory_type": "semantic"}
    )
    rep = lint.lint_files(files)
    msgs = [f.msg for f in rep.criticals if f.id == "required_keys"]
    assert any("`title`" in m for m in msgs) and any("loom.memory_key" in m for m in msgs)
    # the edit is drift from the root pointer by definition
    assert [f.id for f in rep.warnings] == ["type_directory_mismatch", "drift"]


def test_relates_to_resolves_pointers_warn_unknown_kind_warns() -> None:
    files = _fresh_files()
    files["semantic/deploy_window.md"] = _with_frontmatter(
        files["semantic/deploy_window.md"],
        relates_to=[
            {"path": "../procedural/rollback_steps.md", "kind": "supersedes"},  # resolves
            {"path": "procedural/rollback_steps.md", "kind": "implements"},  # bundle-relative
            {"path": "semantic/missing.md", "kind": "supports"},  # critical
            {"path": "loom://o/other-ns/some_key", "kind": "related_to"},  # warning
            {"path": "loom://short", "kind": "related_to"},  # critical
            {"path": "../procedural/rollback_steps.md", "kind": "vibes"},  # unknown kind
            "not-a-mapping",  # critical
        ],
    )
    rep = lint.lint_files(files)
    crit = [(f.id, f.msg) for f in rep.criticals]
    assert sum(1 for i, _ in crit if i == "relates_to") == 3
    assert any("does not resolve" in m for _, m in crit)
    assert any("not loom://org/namespace/key" in m for _, m in crit)
    warn_ids = sorted(f.id for f in rep.warnings if f.id != "drift")  # the edit drifts
    assert warn_ids == ["pointer_unmounted", "unknown_relation"]
    # the resolving edges make lonely_fact the only orphan still
    assert "semantic/lonely_fact.md" in rep.infos[0].msg
    assert "deploy_window" not in rep.infos[0].msg


def test_relates_to_must_be_a_list() -> None:
    files = _fresh_files()
    files["semantic/deploy_window.md"] = _with_frontmatter(
        files["semantic/deploy_window.md"], relates_to={"path": "x.md"}
    )
    assert [f.id for f in lint.lint_files(files).criticals] == ["relates_to"]


def test_edited_since_export_on_a_concept_and_on_a_chunk() -> None:
    files = _fresh_files()
    text = files["semantic/deploy_window.md"]
    files["semantic/deploy_window.md"] = text.replace("never Friday.", "never Monday.")
    chunk = okf_ev.ChunkRecord(
        session_id="s1",
        chunk_index=0,
        content="raw turn text",
        content_sha256=okf_ev.sha256_text("raw turn text"),
    )
    files.update(okf_ev.plan_files([chunk], []))
    rel = chunk.path
    files[rel] = files[rel].replace("raw turn text\n", "raw turn text, edited\n")
    rep = lint.lint_files(files)
    edited = sorted(f.path for f in rep.warnings if f.id == "edited_since_export")
    assert edited == sorted([rel, "semantic/deploy_window.md"])
    assert rep.evidence_files == 1


def test_untouched_concept_hash_matches_what_loom_computes() -> None:
    # the value the body round-trips to hashes to loom.content_sha256, so a
    # JSON value (fenced block) and a text value both pass untouched
    files = okf.plan_bundle(
        [
            _entry(value={"a": 1, "b": [1, 2]}, content_sha256=okf_sha({"a": 1, "b": [1, 2]})),
            _entry(memory_key="other", entities=["deploy"]),
        ],
        namespace="coding",
        exported_at=_NOW,
    )
    assert lint.lint_files(files).warnings == []


def test_derived_needs_confidence_and_high_sensitivity() -> None:
    files = _fresh_files()
    files["semantic/deploy_window.md"] = _with_frontmatter(
        files["semantic/deploy_window.md"],
        **{"loom.source_class": "derived", "loom.confidence": 1.0},
    )
    files["procedural/rollback_steps.md"] = _with_frontmatter(
        files["procedural/rollback_steps.md"],
        **{"loom.source_class": "derived", "loom.confidence": 0.7, "loom.sensitivity": "high"},
    )
    ids = sorted(f.id for f in lint.lint_files(files).warnings)
    assert ids == ["derived_needs_confidence", "drift", "high_sensitivity_present"]


def test_a_row_high_only_by_its_tag_is_reported() -> None:
    # Earlier exports read an empty sensitivity and let such rows through; the tag still says.
    files = _fresh_files()
    files["semantic/deploy_window.md"] = _with_frontmatter(
        files["semantic/deploy_window.md"], tags=["ops", "sensitivity:high"]
    )
    ids = {f.id for f in lint.lint_files(files).warnings}
    assert "high_sensitivity_present" in ids


def test_index_missing_okf_version_and_listing() -> None:
    files = _fresh_files()
    del files["procedural/index.md"]
    files["index.md"] = files["index.md"].replace('okf_version: "0.2"\n', "")
    files["semantic/index.md"] = files["semantic/index.md"].replace(
        "(deploy_window.md)", "(gone.md)"
    )
    rep = lint.lint_files(files)
    crit = sorted((f.id, f.path) for f in rep.criticals)
    assert crit == [("index_missing", "procedural/index.md"), ("okf_version", "index.md")]
    assert [(f.id, f.path) for f in rep.warnings] == [
        ("index_listing", "semantic/deploy_window.md")
    ]

    files = _fresh_files()
    del files["index.md"]
    assert ("index_missing", "index.md") in [
        (f.id, f.path) for f in lint.lint_files(files).criticals
    ]


def test_staging_never_answers(tmp_path: Path) -> None:
    files = _fresh_files()
    files["staging/draft.md"] = "---\ntype: Fact\n---\nnot yet\n"
    assert "staging" in [f.id for f in lint.lint_files(files).criticals]
    _write(tmp_path, _fresh_files())
    (tmp_path / "staging").mkdir()
    rep = lint.lint_bundle(tmp_path)
    assert [f.id for f in rep.criticals] == ["staging"]


def test_generation_digest_definition_and_drift() -> None:
    files = _fresh_files()
    concept_hashes = [
        hashlib.sha256(files[rel].encode()).hexdigest()
        for rel in sorted(files)  # bundle-relative path order
        if rel.rsplit("/", 1)[-1] not in okf.RESERVED_FILES
    ]
    expected = hashlib.sha256("".join(concept_hashes).encode("ascii")).hexdigest()
    assert lint.bundle_digest(files) == expected
    # indexes and log are not part of the digest
    files2 = dict(files)
    files2["log.md"] += "* one more line\n"
    assert lint.bundle_digest(files2) == expected

    files["index.md"] = _with_frontmatter(
        files["index.md"], loom={"corpus_digest": expected, "concepts": 3}
    )
    assert lint.lint_files(files).warnings == []
    files["semantic/deploy_window.md"] = files["semantic/deploy_window.md"].replace(
        "Friday", "Monday"
    )
    rep = lint.lint_files(files)
    assert sorted(f.id for f in rep.warnings) == ["drift", "edited_since_export"]
    assert "root index says 3" in next(f.msg for f in rep.warnings if f.id == "drift")


def test_read_bundle_skips_vcs_dirs_and_escaping_symlinks(tmp_path: Path) -> None:
    _write(tmp_path, _fresh_files())
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "note.md").write_text("---\ntype: X\n---\n")
    outside = tmp_path.parent / f"{tmp_path.name}-outside.md"
    outside.write_text("---\ntype: Fact\n---\nescaped\n")
    (tmp_path / "semantic" / "escape.md").symlink_to(outside)
    files = lint.read_bundle(tmp_path)
    assert ".git/note.md" not in files and "semantic/escape.md" not in files
    assert lint.lint_bundle(tmp_path).criticals == []


def test_cli_okf_lint_exit_codes_and_json(tmp_path: Path, capsys: Any) -> None:
    import argparse

    from server.cli.okf import _cmd_okf_lint

    _write(tmp_path, _fresh_files())
    assert _cmd_okf_lint(argparse.Namespace(path=str(tmp_path), json=True, strict=False)) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["ok"] is True and out["counts"] == {"orphans": 1}
    (tmp_path / "staging").mkdir()
    assert _cmd_okf_lint(argparse.Namespace(path=str(tmp_path), json=False, strict=False)) == 2
    assert "[staging]" in capsys.readouterr().out


def test_import_refuses_a_critical_bundle_unless_forced(tmp_path: Path, capsys: Any) -> None:
    import argparse

    from server.cli.okf import _lint_before_import

    _write(tmp_path, _fresh_files())
    (tmp_path / "staging").mkdir()
    args = argparse.Namespace(no_lint=False, force_import=False)
    assert _lint_before_import(tmp_path, args) == 2
    assert "refusing to import" in capsys.readouterr().err
    assert (
        _lint_before_import(tmp_path, argparse.Namespace(no_lint=False, force_import=True)) is None
    )
    assert (
        _lint_before_import(tmp_path, argparse.Namespace(no_lint=True, force_import=False)) is None
    )
