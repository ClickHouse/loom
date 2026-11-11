"""The SWE-bench localization bench scores files the gold patch edits, never the
files it adds, its BM25 baseline ranks by the textbook formula, and its index step
reads the repository registry before it looks up the entry."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest

from bench import swebench_localization as S

_PATCH = """diff --git a/pkg/models.py b/pkg/models.py
index 1111111..2222222 100644
--- a/pkg/models.py
+++ b/pkg/models.py
@@ -1,2 +1,2 @@
-a
+b
diff --git a/pkg/new_helper.py b/pkg/new_helper.py
new file mode 100644
index 0000000..3333333
--- /dev/null
+++ b/pkg/new_helper.py
@@ -0,0 +1 @@
+x
diff --git a/pkg/old.py b/pkg/renamed.py
similarity index 90%
rename from pkg/old.py
rename to pkg/renamed.py
--- a/pkg/old.py
+++ b/pkg/renamed.py
@@ -1 +1 @@
-y
+z
diff --git a/pkg/models.py b/pkg/models.py
--- a/pkg/models.py
+++ b/pkg/models.py
@@ -9 +9 @@
-c
+d
"""


def test_gold_is_the_pre_image_of_modified_files_and_added_files_are_apart() -> None:
    modified, added = S.gold_files(_PATCH)
    assert modified == ("pkg/models.py", "pkg/old.py")
    assert added == ("pkg/new_helper.py",)


def test_instance_without_findable_gold_has_empty_gold() -> None:
    patch = "diff --git a/x.py b/x.py\nnew file mode 100644\n--- /dev/null\n+++ b/x.py\n"
    inst = S.instance_from_row(
        {"instance_id": "o__r-1", "repo": "o/r", "base_commit": "abc", "patch": patch}
    )
    assert inst.gold == () and inst.added == ("x.py",)


def test_query_is_cut_to_the_wire_limit_and_says_so() -> None:
    assert S.query_text("  short issue \n") == ("short issue", False)
    text, cut = S.query_text("x" * 2500)
    assert len(text) == S.QUERY_MAX_CHARS and cut


def test_file_metrics_rank_the_first_gold_file() -> None:
    ranked = S.ranked_files(["a.py", "a.py", "b.py", "", "c.py", "d.py"])
    assert ranked == ["a.py", "b.py", "c.py", "d.py"]
    m = S.file_metrics(ranked, ["c.py", "z.py"])
    assert m["first_gold_rank"] == 3 and m["rr"] == 1 / 3
    assert (m["hit@1"], m["hit@5"], m["hit@10"]) == (False, True, True)
    assert m["all_gold@10"] is False
    assert S.file_metrics(ranked, ["a.py", "d.py"])["all_gold@10"] is True
    miss = S.file_metrics(ranked, ["z.py"])
    assert miss["rr"] == 0.0 and miss["first_gold_rank"] is None and not miss["hit@10"]


def test_tokenize_splits_identifiers_and_keeps_the_whole_word() -> None:
    toks = S.tokenize("QuerySet.bulk_create(x) HTTPResponse 2")
    assert "queryset" in toks and "query" in toks and "set" in toks
    assert "bulk_create" in toks and "bulk" in toks and "create" in toks
    assert "httpresponse" in toks and "http" in toks and "response" in toks
    assert "x" not in toks and "2" not in toks


def test_bm25_prefers_the_rarer_term_and_drops_zero_scores() -> None:
    docs = {
        "a.py": S.bm25_doc("a.py", "common common rareword"),
        "b.py": S.bm25_doc("b.py", "common common common"),
        "c.py": S.bm25_doc("c.py", "unrelated text here"),
    }
    assert S.bm25_rank("rareword common", docs) == ["a.py", "b.py"]
    assert S.bm25_rank("", docs) == []
    assert S.bm25_rank("anything", {}) == []


def test_bm25_counts_path_tokens() -> None:
    docs = {
        "django/db/models/sql/compiler.py": S.bm25_doc("django/db/models/sql/compiler.py", "x"),
        "other.py": S.bm25_doc("other.py", "y"),
    }
    assert S.bm25_rank("the sql compiler", docs) == ["django/db/models/sql/compiler.py"]


def _inst(i: int, repo: str) -> S.Instance:
    return S.Instance(f"{repo}-{i}", repo, "sha", "issue", ("f.py",))


def test_stratified_sample_keeps_every_repo_and_is_seeded() -> None:
    insts = [_inst(i, "big/a") for i in range(80)] + [_inst(i, "small/b") for i in range(5)]
    insts += [_inst(0, "tiny/c")]
    got = S.stratified_sample(insts, 20, seed=3)
    assert len(got) == 20
    assert {x.repo for x in got} == {"big/a", "small/b", "tiny/c"}
    assert [x.instance_id for x in got] == [
        x.instance_id for x in S.stratified_sample(insts, 20, seed=3)
    ]
    assert S.stratified_sample(insts, 0, seed=3) == insts


def test_summary_counts_hits_errors_and_latency() -> None:
    def row(repo: str, rank: int | None, ms: float) -> dict:
        m = S.file_metrics(["g.py"] if rank == 1 else ["x.py", "g.py"] if rank else [], ["g.py"])
        return {
            "repo": repo,
            "loom": {**m, "ms": ms, "tokens_returned": 100, "abstain": False},
            "loom_page1": m,
            "bm25": S.file_metrics([], ["g.py"]),
            "gold_indexed": True,
        }

    rows = [row("o/a", 1, 10.0), row("o/a", 2, 30.0), row("o/b", None, 20.0)]
    rows.append({"repo": "o/b", "error": "index step failed"})
    s = S.summarize(rows)
    assert s["instances"] == 4 and s["errors"] == 1
    assert s["loom"]["n"] == 3 and s["loom"]["hit@1"] == round(1 / 3, 4)
    assert s["loom"]["hit@5"] == round(2 / 3, 4)
    assert s["loom"]["mrr"] == round((1 + 0.5) / 3, 4)
    assert s["loom"]["latency_ms_p50"] == 20.0 and s["loom"]["latency_ms_p95"] == 30.0
    assert s["bm25"]["hit@10"] == 0.0
    assert s["by_repo"]["o/a"]["n"] == 2


def test_namespace_is_prefixed_and_slugged() -> None:
    assert S.namespace_for("scikit-learn/scikit-learn") == "swe-scikit-learn-scikit-learn"
    assert S.namespace_for("psf/requests") == "swe-psf-requests"


def test_chains_split_on_a_big_step_and_on_the_running_total() -> None:
    # 0 -> 1 small, 1 -> 2 too big for one delta, then the total passes 10
    assert S.split_chains([3, 50, 4, 4, 4], chain_files=10, rebase_files=20) == [0, 2, 5]
    assert S.split_chains([], chain_files=10, rebase_files=20) == [0]
    assert S.split_chains([1, 1, 1], chain_files=10, rebase_files=20) == [0]


def test_path_class_tells_docs_tests_and_source_apart() -> None:
    assert S.path_class("docs/releases/1.10.txt") == "doc"
    assert S.path_class("README.rst") == "doc"
    assert S.path_class("tests/validators/tests.py") == "test"
    assert S.path_class("sympy/core/tests/test_basic.py") == "test"
    assert S.path_class("test_requests.py") == "test"
    assert S.path_class("django/core/validators.py") == "source"
    rows = [{"f": ["a.py", "docs/x.rst"]}, {"f": ["tests/t.py"]}]
    mix = S.result_mix(rows, lambda r: r["f"], 5)
    assert mix == {"source": 0.333, "test": 0.333, "doc": 0.333}


def test_the_index_step_reads_the_registry_before_it_looks_up_the_entry(
    monkeypatch, tmp_path
) -> None:
    """The index step runs in a fresh process: it reads the self-service registry before
    `repo_for_namespace`, which refuses until it is read, and stops naming why when the
    read fails."""
    from server.code.app.index import repo_registry
    from server.code.domain.index import repos
    from server.core import ch
    from server.observability import cost_accumulator

    # The step wraps report_embed to count tokens; put the original back afterwards.
    monkeypatch.setattr(cost_accumulator, "report_embed", cost_accumulator.report_embed)
    monkeypatch.setattr(ch, "get_client", lambda: AsyncMock())
    monkeypatch.setattr(repo_registry, "read_registry", AsyncMock(return_value=False))
    looked_up = []
    monkeypatch.setattr(repos, "repo_for_namespace", lambda org, ns: looked_up.append(ns))

    with pytest.raises(
        SystemExit, match="swebench_localization index-step: .*registry could not be read; retry"
    ):
        asyncio.run(S._index_step("org", "code-x", "abc", 1, str(tmp_path / "paths")))
    assert looked_up == []
