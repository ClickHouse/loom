"""The code plane's readers (server/code/store/index/): the search
legs under both scorings and every scope predicate, the edge reads in both
directions with and without the per-match cap, the PR/history readers with
and without the lexeme fallback, and the long tail of point readers."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from tests.analyzer.cases._common import NS, ORG, SMALL_VEC
from tests.analyzer.corpus import Case, RecordingCH

MODULE = "server.code.store.index"
MODEL = "code-embed"


def _repo(ch: RecordingCH) -> Any:
    from server.code.store.index import CodeRepo

    return CodeRepo(ch)  # type: ignore[arg-type]


def _scope(kind: str) -> Any:
    from server.code.domain.search_scope import build_scope

    if kind == "globs":
        return build_scope(
            pattern="parseQuotaKey",
            path_globs=["src/**", "*.{h,cpp}"],
            exclude_globs=["**/tests/*"],
            langs=["cpp", "python"],
        )
    if kind == "regex":
        return build_scope(pattern="parse\\w+Key", regex=True, case_sensitive=True)
    return None


async def _search(ch: RecordingCH, c: dict[str, Any]) -> None:
    repo = _repo(ch)
    scope = _scope(c["scope"])
    prefix = "src/" if c["path_prefix"] else ""
    await repo.lexical_search_chunks(
        ORG,
        NS,
        MODEL,
        ["quota", "key"],
        [1.5, 0.7],
        path_prefix=prefix,
        scope=scope,
    )
    await repo.vector_search_chunks(ORG, NS, MODEL, SMALL_VEC, path_prefix=prefix, scope=scope)
    await repo.vector_shortlist_ids(
        ORG, NS, MODEL, SMALL_VEC, n=500, path_prefix=prefix, scope=scope
    )
    await repo.exact_rerank_by_ids(
        ORG, NS, MODEL, SMALL_VEC, [("src/a.py", "c1"), ("src/b.py", "c2")]
    )
    # The card leg's read (and the off_domain gate's)
    await repo.vector_search_cards(ORG, NS, MODEL, SMALL_VEC, path_prefix=prefix, pool=15)
    # The source-only card read behind the concept leg's vocabulary
    await repo.vector_search_cards(
        ORG, NS, MODEL, SMALL_VEC, path_prefix=prefix, pool=5, skip_tests=True
    )
    await repo.symbol_names_for_paths(ORG, NS, ["src/a.py", "src/b.py"])
    await repo.scope_file_count(ORG, NS, MODEL, scope or _scope("globs"), path_prefix=prefix)
    regex = c["scope"] == "regex"
    await repo.grep_chunks(ORG, NS, "needle", regex=regex, path_prefix=prefix, scope=scope)
    if regex:
        # An alternation gets the needle guard in front of match()
        await repo.grep_chunks(ORG, NS, "needle|other", regex=True, path_prefix=prefix, scope=scope)
    await repo.grep_files(
        ORG,
        NS,
        "needle",
        regex=regex,
        case_sensitive=False,
        path_prefix=prefix,
        scope=scope,
    )
    # Code.grep's unit spans, one keyed read with the side-table subquery
    await repo.symbol_spans_by_ids(ORG, NS, ["sym-1", "sym-2"])
    # The coverage claim's test-tree greps, one statement per round
    await repo.grep_files_many(
        ORG,
        NS,
        ["needle", "Class::method"],
        path_prefixes=["tests/queries/0_stateless/", "tests/integration/"],
        limit=15,
    )
    await repo.chunk_contains(ORG, NS, "src/a.py", "needle")
    # A printed message finds its statement; files by name
    await repo.grep_message_chunks(
        ORG,
        NS,
        ["cannot convert string", "to type"],
        path_prefix=prefix,
        scope=scope,
    )
    # The grep scans under a request deadline (break-on-timeout settings)
    await repo.grep_chunks(ORG, NS, "needle", path_prefix=prefix, scope=scope, max_execution_s=1.5)
    await repo.grep_files(ORG, NS, "needle", path_prefix=prefix, scope=scope, max_execution_s=1.5)
    await repo.grep_message_chunks(
        ORG, NS, ["cannot convert string"], path_prefix=prefix, scope=scope, max_execution_s=1.5
    )
    await repo.list_files(
        ORG,
        NS,
        glob_re="^tests/queries/0_stateless/05138_[^/]*$" if c["scope"] == "globs" else "",
        stem="05138_with_fill",
        path_prefix=prefix,
        limit=50,
    )
    # The coverage claim's readers
    await repo.test_map_detail(ORG, NS, ["src/Core/Settings.cpp"], limit=60)
    await repo.coverage_rows_for(ORG, NS, ["src/Core/Settings.cpp"])
    await repo.coverage_files_for(ORG, NS, ["src/Core/Settings.cpp"])
    # The coverage import's retirement reads
    await repo.live_coverage_pairs(ORG, NS)
    await repo.live_coverage_files(ORG, NS)
    await repo.paths_by_stem(ORG, NS, ["Settings", "MergeTreeData"], limit=40)
    await repo.max_stateless_test_number(ORG, NS)
    # The reference sibling next to a test
    await repo.live_paths_present(ORG, NS, ["tests/queries/0_stateless/00001_a.reference"])
    # The convention rules' index reads
    await repo.stateless_paths_for_numbers(ORG, NS, ["04652", "04653"])
    await repo.paths_containing(
        ORG, NS, ["tests/queries/0_stateless/00001_a.sql"], "ENGINE = MergeTree"
    )
    # The settings-change history is parsed end to end
    await repo.chunks_of_path(ORG, NS, "src/Core/SettingsChangesHistory.cpp")
    await repo.track_path_chunks(
        ORG, NS, MODEL, ["src/a.py"], ["quota"], [1.5], SMALL_VEC if c["path_prefix"] else None
    )


async def _edges(ch: RecordingCH, c: dict[str, Any]) -> None:
    repo = _repo(ch)
    await repo.edges_for_symbols(
        ORG,
        NS,
        ["s1", "s2"],
        direction=c["direction"],
        edge_kinds=c["edge_kinds"],
        limit=500,
        min_confidence=c["min_confidence"],
        per_match=c["per_match"],
    )
    await repo.live_edges_to_symbols(
        ORG,
        NS,
        ["s1"],
        tier="treesitter",
        edge_kinds=c["edge_kinds"],
        max_confidence=0.9 if c["min_confidence"] else None,
    )
    await repo.live_edges_for_path(ORG, NS, "src/a.py", "treesitter")
    # The round-wide forms of the ingest's per-path reads
    await repo.live_edges_for_paths(ORG, NS, ["src/a.py", "src/b.py"], "treesitter")
    await repo.live_symbols_for_paths(ORG, NS, ["src/a.py", "src/b.py"], "treesitter")
    await repo.live_call_misses_for_paths(ORG, NS, ["src/a.py", "src/b.py"])
    await repo.live_call_guards_for_paths(ORG, NS, ["src/a.py", "src/b.py"])
    # The caller-guard readers (per-path diff, per-chain point read, probe)
    await repo.live_call_guards_for_path(ORG, NS, "src/a.py")
    await repo.call_guards_for_callers(ORG, NS, ["s1", "s2"])
    await repo.has_call_guards(ORG, NS)
    await repo.live_edges_for_path(ORG, NS, "src/a.py", "treesitter")
    await repo.include_edges(ORG, NS)
    await repo.symbol_fan_in(ORG, NS)


async def _prs(ch: RecordingCH, c: dict[str, Any]) -> None:
    repo = _repo(ch)
    before = datetime(2026, 1, 1, tzinfo=UTC) if c["before"] else None
    tf = c["variant"]
    await repo.pr_history_search(ORG, NS, ["quota", "key"], [1.2, 0.4], pool=30, before=before)
    await repo.pr_corpus_stats(ORG, NS, ["quota", "key"], before=before)
    await repo.prs_by_number(ORG, NS, [1, 2])
    await repo.prs_linking_issue(ORG, NS, [1, 2])
    await repo.pr_history_for(ORG, NS, paths=["src/a.py"] if c["before"] else None)
    # Code.pr_check's closed-attempt leg and the merged-pool hydration
    await repo.gh_prs_matching(
        ORG, NS, state="open", tokens=["quota", "key"], paths=["src/a.py"], limit=400
    )
    await repo.gh_prs_matching(
        ORG, NS, state="closed", tokens=["quota"], paths=[], days=180, limit=400
    )
    await repo.merged_prs_full(ORG, NS, [1, 2])
    # The ledger leg of code.blame_range, with and without the namespace's branch predicate
    branch = "master" if c["before"] else ""
    await repo.pr_history_in_window(
        ORG,
        NS,
        "src/a.py",
        since=datetime(2026, 1, 1),
        until=datetime(2026, 2, 1),
        limit=200,
        branch=branch,
    )
    # The churn leg of tracker.test_signal (paths, a prefix, both) and the inverse test map
    await repo.pr_churn_touching(
        ORG, NS, paths=["tests/queries/0_stateless/00001_select_1.sql"], since=datetime(2026, 1, 1)
    )
    await repo.pr_churn_touching(
        ORG, NS, paths=[], prefix="tests/integration/test_keeper/", since=datetime(2026, 1, 1)
    )
    await repo.pr_churn_touching(
        ORG,
        NS,
        paths=["src/a.py"],
        prefix="tests/integration/test_keeper/",
        since=datetime(2026, 1, 1),
    )
    await repo.sources_of_tests(ORG, NS, ["tests/queries/0_stateless/00001_select_1.sql"])
    # Co-change of a definition's file with the change at hand
    await repo.co_change_counts(
        ORG, NS, def_paths=["src/a.py", "src/b.py"], near_paths=["src/c.py"]
    )
    # The convention rules' ledger reads
    await repo.pr_facts(ORG, NS, 1)
    await repo.first_owner_of_paths(
        ORG, NS, ["tests/queries/0_stateless/04652_a.sql"], branch=branch
    )
    await repo.pr_category_census(ORG, NS, "src/Storages", days=365, branch=branch)
    # The missing-merge sweep's page
    await repo.merge_shas_page(ORG, NS, below_pr=100 if c["before"] else 0, limit=500)
    await repo.recent_prs(ORG, NS, days=30, limit=10)
    await repo.items_by_number(ORG, NS, [1, 2])
    # The ledger row behind a bisect candidate's marks
    await repo.pr_records_by_number(ORG, NS, [1, 2])
    await repo.gh_item_titles(ORG, NS, [1, 2])
    await repo.pr_churn_for(ORG, NS, ["src/a.py"])
    await repo.pr_churn_ranked(
        ORG, NS, prefixes=("src/",) if c["before"] else (), exclude=("tests/",) if tf else ()
    )
    await repo.pr_file_pairs(ORG, NS)
    await repo.reviewers_by_pr(ORG, NS, [1, 2])
    await repo.top_reviewers(ORG, NS)
    await repo.review_comments_for(
        ORG, NS, ["src/a.py"], exclude_pr=3 if c["before"] else 0, humans_only=tf
    )
    # The review comments on history / blame PR rows, one read
    await repo.review_comments_on_prs(
        ORG,
        NS,
        [1, 2],
        ["src/a.py"],
        lines=(10, 20) if tf else (0, 0),
        bots=["mergify"],
        bot_suffixes=["[bot]"],
    )
    await repo.test_map_for(ORG, NS, ["src/a.py"])


async def _point_readers(ch: RecordingCH, c: dict[str, Any]) -> None:
    repo = _repo(ch)
    tests_flag = c["variant"]
    await repo.get_state(ORG, NS, "k")
    await repo.all_state(ORG, NS)
    await repo.live_chunks_for_path(ORG, NS, "src/a.py", MODEL, with_embedding=tests_flag)
    await repo.live_chunk_index_for_path(ORG, NS, "src/a.py", MODEL)
    # A SQL test's live facet rows, the ingest key-diff's read;
    # the tests-for read with one and two facets (the two-facet form takes
    # the count branch past `limit`), and its positive control
    await repo.live_facets_for_path(ORG, NS, "tests/queries/0_stateless/00001_x.sql")
    await repo.tests_for_facets(ORG, NS, [("setting", "dialect")], limit=50)
    await repo.tests_for_facets(
        ORG,
        NS,
        [("error_code", "BAD_ARGUMENTS"), ("function", "arrayJoin")],
        limit=0,
    )
    await repo.facets_present(ORG, NS)
    # The closest-test candidate read (union + whole facet sets)
    await repo.facet_sets_sharing(
        ORG, NS, [("setting", "dialect"), ("engine", "Memory")], limit=400
    )
    await repo.chunks_full_by_ids(ORG, NS, "src/a.py", MODEL, ["c1"])
    await repo.chunks_by_keys(ORG, NS, MODEL, [("src/a.py", "c1")])
    await repo.chunk_spans_for_path(ORG, NS, "src/a.py")
    await repo.live_symbols_for_path(ORG, NS, "src/a.py", "treesitter")
    await repo.live_call_misses_for_path(ORG, NS, "src/a.py")
    await repo.call_misses_for_names(ORG, NS, ["f"])
    await repo.live_call_misses_for_names(ORG, NS, ["f"])
    await repo.live_files_for_path(ORG, NS, "src/a.py")
    await repo.name_candidates(ORG, NS, ["f", "g"])
    await repo.pending_embed_chunks(ORG, NS, MODEL, 3, 100, include_poisoned=tests_flag)
    # The recovery worker's scan for every repo in one statement,
    # keyed above a version watermark, and the keyed FINAL read of what it found
    await repo.pending_embed_keys(
        "code_chunks", [(ORG, NS), (ORG, "n2")], MODEL, 3, 100, min_version=7 if tests_flag else 0
    )
    await repo.pending_embed_keys("code_summaries", [(ORG, NS)], MODEL, 3, 100)
    await repo.pending_embed_rows_by_keys(
        "code_chunks",
        MODEL,
        [
            {
                "org": ORG,
                "namespace": NS,
                "path": "src/a.py",
                "chunk_id": "00000000-0000-0000-0000-000000000001",
            }
        ],
        3,
    )
    await repo.pending_embed_rows_by_keys(
        "code_summaries",
        MODEL,
        [{"org": ORG, "namespace": NS, "scope_type": "file", "scope_path": "src/a.py"}],
        3,
    )
    await repo.symbols_by_name(ORG, NS, "f")
    await repo.functions_for_paths(ORG, NS, ["src/a.py"], with_extent_only=tests_flag)
    await repo.outline_for_path(ORG, NS, "src/a.py")
    await repo.class_skeleton_chunk(ORG, NS, "src/a.py", "Klass")
    await repo.scip_covered_paths(ORG, NS, ["src/a.py"])
    # Code.verify_citations file rows + chunk spans per cited path
    await repo.file_rows_for_paths(ORG, NS, ["src/a.py", "src/b.py"])
    await repo.chunk_spans_for_paths(ORG, NS, ["src/a.py", "src/b.py"])
    await repo.paths_by_suffix(ORG, NS, ["Parsers/ASTSetQuery.cpp", "configs/keeper.xml"])
    await repo.types_for_paths(ORG, NS, ["src/a.py"])
    await repo.chunk_hashes_for_symbols(
        ORG, NS, ["s1"], rows=[{"symbol_id": "s1", "path": "src/a.py", "start_line": 3}]
    )
    await repo.symbol_ids_for_names(ORG, NS, ["f"])
    await repo.symbols_fuzzy(ORG, NS, "quo")
    await repo.symbols_by_ids(ORG, NS, ["s1"])
    # The commit refresh's reached set, path-only
    await repo.symbol_paths_by_ids(ORG, NS, ["s1"])
    await repo.methods_named(ORG, NS, "f")
    await repo.paths_under_prefix(ORG, NS, MODEL, "src/")
    await repo.code_corpus_stats(ORG, NS, ["quota"])
    await repo.chunks_for_paths(ORG, NS, MODEL, ["src/a.py"])
    await repo.top_summaries(ORG, NS, MODEL, module_prefix="src/" if tests_flag else "")
    await repo.context_pack(ORG, NS, 1)
    await repo.list_context_packs(ORG, NS, limit=10, offset=0, pr_number=1 if tests_flag else 0)
    # The review brief's point read and the two reads a miss
    # builds from, keyed by PR number or by merge commit
    await repo.review_brief(
        ORG, NS, pr_number=1 if tests_flag else 0, merge_sha="" if tests_flag else "a" * 40
    )
    await repo.review_brief(ORG, NS, pr_number=1, merge_sha="a" * 40)
    await repo.pr_merge_facts(
        ORG, NS, pr_number=1 if tests_flag else 0, merge_sha="" if tests_flag else "a" * 40
    )
    # The linked fixes' merge commits, one statement
    await repo.merge_commits_of(ORG, NS, [1, 2])
    # An open PR's head on the items ledger — the PRs the mirror
    # fetches heads for, the rows the worker fills, the carry-forward read, and
    # the one row an open brief takes its title from
    await repo.open_pr_numbers(ORG, NS, limit=100)
    await repo.open_pr_heads(ORG, NS, limit=100)
    await repo.open_prs_for_head_fill(ORG, NS, limit=10)
    await repo.pr_heads_by_number(ORG, NS, [1, 2])
    await repo.gh_pr_head(ORG, NS, 1)
    # The branch ledger's merged backports of one upstream PR
    await repo.backport_prs_titled(ORG, NS, 1)
    await repo.symbol_names_present(ORG, NS, ["DB::IColumn::insert", "loadDataParts"])
    # The hunk-level presence read — the enclosing functions and
    # the chunks carrying one of the patch's lines, on the path range
    await repo.chunks_for_patch(
        ORG,
        NS,
        ["src/DataTypes/IDataType.cpp"],
        ["IDataType::getSubcolumnInfo"],
        ["getSubcolumnNameForStream(subpath,"],
    )
    await repo.caller_counts(ORG, NS, ["s1", "s2"])
    await repo.coverage_gaps(ORG, NS)
    await repo.coverage(ORG, NS)
    await repo.index_errors(ORG, NS, limit=10)
    await repo.recent_indexed_commits(ORG, NS, limit=10)
    await repo.symbol_definition_sites(ORG, NS)
    await repo.live_symbol_stats_ids(ORG, NS)
    await repo.symbol_stats(ORG, NS, ["s1"])
    # The first-pass check and the two index_status fields
    await repo.graph_stats_state(ORG, NS)
    # Its fallback for a namespace with no published summary
    await repo.symbol_stats_newest(ORG, NS)
    # The count the pass publishes as the stats summary
    await repo.symbol_stats_summary(ORG, NS)
    # The precomputed risk row's point read and the two reads
    # its builder adds (every tested path, keyed or namespace-wide; chunk spans)
    await repo.unit_risk_rows(ORG, NS, ["u1"])
    # The weekly leg's resume read and the two index_status fields
    await repo.unit_risk_path_stamps(ORG, NS)
    await repo.unit_risk_summary(ORG, NS)
    await repo.tested_paths(ORG, NS, ["src/a.py"] if tests_flag else None)
    await repo.chunk_units_for_paths(ORG, NS, MODEL, ["src/a.py"])
    await repo.top_symbol_stats(
        ORG, NS, path_prefix="src/" if tests_flag else "", include_tests=tests_flag
    )
    await repo.live_file_hashes(ORG, NS)
    await repo.live_summary_rows(ORG, NS, MODEL)
    await repo.live_summary_rows_other_models(ORG, NS, MODEL)
    await repo.unit_signals(ORG, NS, ["u1"])
    await repo.trace_by_run_id(ORG, NS, "run-1")
    await repo.get_scip_batch(ORG, NS, "b1")
    await repo.pending_scip_batches()
    await repo.scip_parts(ORG, NS, "b1")
    await repo.scip_parts_received(ORG, NS, "b1")
    await repo.table_counts(ORG, NS, fingerprint="fp" if tests_flag else None, max_age_s=0.0)
    await repo.chunk_embed_coverage(ORG, NS)


async def _repo_directory(ch: RecordingCH, c: dict[str, Any]) -> None:
    """The grouped "does anyone maintain it" read the repo
    list adds to its directory state (server/code/app/context.py)."""
    from server.code.app.repos import _maintained_entries

    await _maintained_entries(ch, [(ORG, NS), (ORG, f"{NS}-26-1")])


def cases() -> list[Case]:
    return [
        Case("code_context.repo_directory", "server.code.app.context", _repo_directory, {}),
        Case(
            "code_repo.search",
            MODULE,
            _search,
            {
                "scope": ("none", "globs", "regex"),
                "path_prefix": (False, True),
            },
        ),
        Case(
            "code_repo.edges",
            MODULE,
            _edges,
            {
                "direction": ("callees", "callers"),
                "per_match": (None, 12),
                "min_confidence": (0.0, 0.7),
                "edge_kinds": (None,),
            },
        ),
        Case("code_repo.prs", MODULE, _prs, {"variant": (False, True), "before": (False, True)}),
        Case("code_repo.point_readers", MODULE, _point_readers, {"variant": (False, True)}),
    ]
