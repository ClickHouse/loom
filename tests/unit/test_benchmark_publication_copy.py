"""Guard the meaning of public benchmark claims, not only their numbers."""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
README = (ROOT / "README.md").read_text(encoding="utf-8")
RESULTS = (ROOT / "docs" / "benchmarks" / "results.md").read_text(encoding="utf-8")
COVERAGE = (ROOT / "docs" / "benchmarks" / "coverage.md").read_text(encoding="utf-8")


def test_swebench_claim_is_labelled_as_file_localization() -> None:
    assert "file localization, not issue resolution" in README
    assert "file-localization benchmark" in RESULTS
    assert "retrieval, not the official patch-and-test score" in COVERAGE


def test_agent_claim_states_the_exact_success_condition() -> None:
    assert "of a verified answer's key points" in README
    assert "identified all required files" not in README
    assert "counts as a miss at its full cost" in RESULTS
    assert "they do not edit or test" in COVERAGE


def test_retrieval_metrics_keep_their_denominators() -> None:
    # every README row has its baseline; the metric definitions live in the ledger
    assert "Without Loom" in README
    assert "with a changed file in the first 10" in README
    assert "Recall@30 is a per-question hit rate" in RESULTS
    assert "not the mean\nfraction of all supporting sessions retrieved" in RESULTS


def test_efficiency_claims_disclose_material_caveats() -> None:
    # the coding rows say which agent, which codebase and what the cost covers
    assert "Claude Code (`claude-sonnet-5`) on the ClickHouse codebase" in README
    assert "cost is the whole session's model bill" in README
    assert "Tokens are characters / 4" in RESULTS
