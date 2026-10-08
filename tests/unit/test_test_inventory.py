"""The test inventory and the feature-to-test ledger stay in step with the tree.

Every tracked file under tests/ has a row with a workflow and a layer; every
annotation names files that exist; every product feature is referenced by a
test or listed in tests/inventory/gaps.toml, and gaps.toml only shrinks.
"""

from __future__ import annotations

import shutil
import warnings
from pathlib import Path

import pytest

from tests.inventory import build, ledger

pytestmark = pytest.mark.skipif(
    shutil.which("git") is None or not (build.ROOT / ".git").exists(),
    reason="the inventory reads `git ls-files`; run from a git checkout",
)


@pytest.fixture(scope="module")
def files() -> list[str]:
    return build.tracked_files()


@pytest.fixture(scope="module")
def annotations() -> list[build.Annotation]:
    return build.load_annotations()


@pytest.fixture(scope="module")
def rows(files: list[str], annotations: list[build.Annotation]) -> list[build.Row]:
    return build.build_rows(files, annotations)


@pytest.fixture(scope="module")
def coverage(rows: list[build.Row]) -> list[ledger.Coverage]:
    return ledger.coverage(rows=rows)


def test_every_tracked_test_file_has_a_row(files: list[str], rows: list[build.Row]) -> None:
    assert [r.path for r in rows] == files


def test_every_row_has_a_workflow(rows: list[build.Row]) -> None:
    homeless = [r.path for r in rows if r.workflow is None]
    assert not homeless, (
        "no workflow claims these paths; add a prefix to _WORKFLOW_PREFIXES "
        f"in tests/inventory/build.py: {homeless}"
    )


def test_every_row_has_a_known_layer_status_and_tier(rows: list[build.Row]) -> None:
    bad = [
        (r.path, r.layer, r.status, r.tier)
        for r in rows
        if r.layer not in build.LAYERS
        or r.status not in build.STATUSES
        or r.tier not in build.TIERS
    ]
    assert not bad


def test_one_annotation_file_per_area() -> None:
    present = sorted(p.stem for p in build.ANNOTATIONS_DIR.glob("*.toml"))
    assert present == sorted(build.AREAS)


def test_every_annotation_matches_a_tracked_file(
    files: list[str], annotations: list[build.Annotation]
) -> None:
    stale = build.unmatched_annotations(files, annotations)
    assert not stale, f"annotations naming no tracked file (delete or fix them): {stale}"


def test_every_feature_has_a_test_or_a_gap(coverage: list[ledger.Coverage]) -> None:
    result = ledger.check_gaps(coverage, ledger.load_gaps())
    lines = "\n".join(ledger.gap_line(fid) for fid in result.uncovered)
    assert not result.uncovered, (
        "these features have no test. Add one, or add these lines to "
        f"tests/inventory/gaps.toml (check the owner):\n{lines}"
    )


def test_gap_lines_no_longer_owed_are_reported(coverage: list[ledger.Coverage]) -> None:
    """A test for a listed gap, or a removed feature, is good news, not a failure:
    it is reported so the next `--prune-gaps` run deletes the line."""
    result = ledger.check_gaps(coverage, ledger.load_gaps())
    done = [*result.stale, *result.unknown]
    if done:
        warnings.warn(
            f"{len(done)} gaps.toml lines are no longer owed ({', '.join(done)}); "
            "run `python -m tests.inventory.ledger --prune-gaps`",
            stacklevel=1,
        )


def test_gap_entries_are_sorted() -> None:
    keys = list(ledger.load_gaps())
    assert keys == sorted(keys), "keep gaps.toml sorted so concurrent edits merge"


# ---------------------------------------------------------------- the machinery


def test_a_new_fixture_language_has_no_workflow_until_claimed() -> None:
    assert build.workflow_of("tests/fixtures/code_lanes/go/app/main.go") == "fixtures-go"
    assert build.workflow_of("tests/fixtures/code_lanes/kotlin/Main.kt") is None


def test_star_crosses_directories() -> None:
    assert build.matches("tests/fixtures/code_lanes/go/*", "tests/fixtures/code_lanes/go/a/b.go")
    assert not build.matches("tests/unit/test_a*.py", "tests/unit/sub/test_b.py")


def test_conflicting_scalar_annotations_are_refused() -> None:
    anns = [
        build.Annotation("code", "tests/unit/test_x.py", {"match": "", "status": "retain"}),
        build.Annotation("memory", "tests/unit/test_*.py", {"match": "", "status": "remove"}),
    ]
    with pytest.raises(ValueError, match="status is 'retain'"):
        build.merge_annotations("tests/unit/test_x.py", anns)


def test_list_annotations_union() -> None:
    anns = [
        build.Annotation("code", "tests/unit/test_x.py", {"match": "", "tags": ["fault"]}),
        build.Annotation("code", "tests/unit/*", {"match": "", "tags": ["security"]}),
    ]
    assert build.merge_annotations("tests/unit/test_x.py", anns)["tags"] == ["fault", "security"]


@pytest.mark.parametrize(
    ("entry", "message"),
    [
        ({"owner": "x"}, "missing `match`"),
        ({"match": "a", "colour": "red"}, "unknown field"),
        ({"match": "a", "status": "dead"}, "status='dead'"),
        ({"match": "a", "tags": ["flaky"]}, "tag 'flaky'"),
        ({"match": "a", "status": "remove"}, "needs `evidence`"),
    ],
)
def test_an_invalid_annotation_is_refused(tmp_path: Path, entry: dict, message: str) -> None:
    body = "\n".join(f"{k} = {v!r}".replace("'", '"') for k, v in entry.items())
    (tmp_path / "code.toml").write_text(f"[[files]]\n{body}\n")
    with pytest.raises(ValueError, match=message):
        build.load_annotations(tmp_path)


def test_a_dotted_reference_matches_any_run_of_segments() -> None:
    toks = ledger._tokens("await a.memory.set(x); LOOM_MEMORY_TTL; loom.memory_versions")
    assert {"memory.set", "a.memory", "LOOM_MEMORY_TTL", "loom.memory_versions"} <= toks


def test_a_feature_counts_by_layer(monkeypatch: pytest.MonkeyPatch) -> None:
    feat = ledger.Feature("op:memory.set", "op", frozenset({"memory.set"}))
    texts = {"tests/unit/test_a.py": "a.memory.set()", "tests/integration/test_b.py": "nothing"}
    monkeypatch.setattr(build, "read_text", texts.__getitem__)
    sources = [("tests/unit/test_a.py", "unit"), ("tests/integration/test_b.py", "integration")]
    [cov] = ledger.coverage([feat], sources, rows=[])
    assert cov.files == {"unit": {"tests/unit/test_a.py"}}
    assert cov.covered


def test_prune_deletes_only_the_named_lines(tmp_path: Path) -> None:
    path = tmp_path / "gaps.toml"
    path.write_text(
        '# header\n[gaps]\n"op:a" = { owner = "code", reason = "r" }\n'
        '"op:b" = { owner = "code", reason = "r" }\n'
    )
    assert ledger.prune_gaps(["op:a", "op:zz"], path) == ["op:a"]
    assert list(ledger.load_gaps(path)) == ["op:b"]
    assert path.read_text().startswith("# header\n[gaps]\n")


def test_the_missing_gap_line_parses(tmp_path: Path) -> None:
    path = tmp_path / "gaps.toml"
    path.write_text("[gaps]\n" + ledger.gap_line("setting:memory_new_knob") + "\n")
    assert ledger.load_gaps(path)["setting:memory_new_knob"].owner == "memory"


def test_a_gap_needs_an_owner_and_a_reason(tmp_path: Path) -> None:
    path = tmp_path / "gaps.toml"
    path.write_text('[gaps]\n"op:x" = { owner = "nobody", reason = "r" }\n')
    with pytest.raises(ValueError, match="owner 'nobody'"):
        ledger.load_gaps(path)
    path.write_text('[gaps]\n"op:x" = "just text"\n')
    with pytest.raises(ValueError, match="must be"):
        ledger.load_gaps(path)


def test_a_listing_file_does_not_count_as_coverage(monkeypatch: pytest.MonkeyPatch) -> None:
    feat = ledger.Feature("op:memory.set", "op", frozenset({"memory.set"}))
    monkeypatch.setattr(
        build, "read_text", {"tests/unit/test_allow.py": "'memory.set'"}.__getitem__
    )
    row = build.Row(
        path="tests/unit/test_allow.py",
        workflow="unit",
        layer="unit",
        owner="x",
        status="retain",
        tier="unit",
        references=False,
    )
    [cov] = ledger.coverage([feat], [("tests/unit/test_allow.py", "unit")], rows=[row])
    assert not cov.covered
