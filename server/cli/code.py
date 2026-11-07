"""`loom code ...` — operator CLI for the code intelligence plane
(`loom code --help` lists the subcommands).

Heavy one-time work lives HERE, not in the server workers ("not
file-format aware in-server"): backfill runs for hours against a full
checkout; the workers only handle per-merge deltas. Every phase is
resumable: the git watermark, the pr cursor, and dim=0 stubs ARE the
resume state — rerunning skips what's done.
"""

from __future__ import annotations

import argparse
import sys
from typing import Any

from server.cli._apply import add_apply_flags
from server.cli.code_backfill import _backfill
from server.cli.code_maintenance import (
    _answers_purge,
    _compact,
    _coverage_import,
    _crossref,
    _describe,
    _export_matrix,
    _export_pairs,
    _init,
    _lint_instructions,
    _reconcile,
    _reembed,
    _repack,
    _stats,
    _vector_twin,
    _warm,
)
from server.cli.code_prs import (
    _backfill_assignees,
    _backfill_items,
    _backfill_prs,
    _backfill_reviews,
)
from server.cli.code_run import _identity, _run
from server.cli.code_scip import _entries, _entry_locked, _scip_delta, _scip_full, _scip_retry
from server.code.domain.index.tier_health import COMPILER_TIERS


def _add_common(p: argparse.ArgumentParser) -> None:
    p.add_argument("--org", default=None, help="default: LOOM_CODE_ORG")
    p.add_argument("--namespace", default=None, help="default: LOOM_CODE_NAMESPACE")


def _add_backfill_pass_flags(p_bf: argparse.ArgumentParser) -> None:
    """The backfill's per-run passes and context knobs."""
    p_bf.add_argument(
        "--edges-second-pass",
        action="store_true",
        help="after the index phase, derive every file's edges again "
        "against the finished name table (a fresh namespace binds cross-file "
        "references only once their targets are indexed). Implied by a FIRST "
        "full backfill of a namespace; --no-edges-second-pass opts out",
    )
    p_bf.add_argument(
        "--no-edges-second-pass",
        action="store_true",
        help="skip the implied second pass of a first full backfill",
    )
    p_bf.add_argument(
        "--dialect-folder-pass",
        action="store_true",
        help="after the index phase, give each undecided SQL file the "
        "dialect its own directory's decided files agree on (confidence 0.5)",
    )
    p_bf.add_argument(
        "--embedding-model-suffix",
        default="",
        help="twin label: write rows under <model><suffix> (e.g. "
        "'+ctx2') beside the live label — its own ORDER BY range, its own "
        "backfill cursor, no git_head watermark write; readable through "
        "code.search's embedding_model override",
    )
    p_bf.add_argument(
        "--context-callees",
        choices=["off", "calls", "calls_callers"],
        default="off",
        help="callee outline in the context text for THIS run",
    )
    p_bf.add_argument(
        "--callers-from",
        default="",
        help="namespace whose code_edges supply the 'Called by:' names (default: the "
        "indexed namespace; a prefix twin reads the full index's graph)",
    )


def _add_scip_full_parser(csub: Any) -> None:
    """`loom code scip-full` and its flags, and `scip-lock-path` (kept out of
    `register_code_cli` for the function-length gate)."""
    p_full = csub.add_parser(
        "scip-full",
        help="Full compiler-tier re-index by hand (the code_scip_full worker "
        "runs it weekly by default: heals the TU-cap degrades of per-merge deltas "
        "and any drift). Heavy: ~50 min of scip-clang + apply, on this host "
        "rather than in the server.",
    )
    p_full.add_argument("--checkout", default="", help="default: LOOM_CODE_SCIP_CHECKOUT_DIR")
    p_full.add_argument("--jobs", type=int, default=0, help="scip-clang -j (0 = nproc/2)")
    p_full.add_argument(
        "--keep-delta",
        default="",
        help="copy the harness's delta.json here after indexing (so an "
        "interrupted apply can be re-run with --apply-delta instead of "
        "paying the ~50 min index again)",
    )
    p_full.add_argument(
        "--apply-delta",
        default="",
        help="skip checkout + scip-clang and apply this delta.json (from "
        "--keep-delta); scip_commit is set to the delta's commit",
    )
    _add_common(p_full)
    p_full.set_defaults(func=_cmd_scip_full)

    p_lock = csub.add_parser(
        "scip-lock-path",
        help="Print the entry's compiler-tier lock file on this host. A manual "
        "harness run goes under `loom code entry-lock`, which takes this file and "
        "the deployment lease; scip-full and scip-delta take both themselves.",
    )
    _add_common(p_lock)
    p_lock.set_defaults(func=_print_scip_lock_path)

    p_run = csub.add_parser(
        "entry-lock",
        help="Run a command under the entry's compiler-tier lock (this host's file and "
        "the deployment's lease): exit 75 when another pass holds it, 3 when the "
        "lease is lost mid-run (the command is stopped).",
    )
    _add_common(p_run)
    p_run.add_argument(
        "--tier",
        choices=COMPILER_TIERS,
        default="",
        help="Record the command's outcome as this compiler tier's health on the entry: "
        "exit 0 clears a failure, except that for go the modules its output names as not "
        "built fail it; exit 3 (the window's base is gone) records nothing; any other exit "
        "records a failure with the end of the output, a timeout when a SIGTERM to entry-lock "
        "(the caller's time limit) stopped it. The command's output, stderr included, is "
        "relayed to stdout.",
    )
    p_run.add_argument("command", nargs=argparse.REMAINDER, help="-- <command> [args…]")
    p_run.set_defaults(func=_cmd_entry_lock)


def _add_backfill_parser(csub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    """`loom code backfill`: full-tree indexing."""
    p_bf = csub.add_parser("backfill", help="Full-tree indexing (resumable).")
    _add_common(p_bf)
    p_bf.add_argument(
        "--phase",
        choices=["index", "embed"],
        default="index",
        help="index: parse+chunk+symbols+edges (stubs unless --embed); "
        "embed: fill pending embedding stubs",
    )
    p_bf.add_argument("--embed", action="store_true", help="embed inline during index")
    p_bf.add_argument(
        "--include-poisoned",
        action="store_true",
        help="--phase embed: also retry rows that exhausted code_embed_max_attempts",
    )
    p_bf.add_argument(
        "--allow-fake-embedder",
        action="store_true",
        help="dev only: permit --embed with the fake n-gram fallback embedder",
    )
    p_bf.add_argument(
        "--no-fetch",
        action="store_true",
        help="index the clone's current tip without fetching upstream",
    )
    p_bf.add_argument("--limit", type=int, default=0, help="max files (0 = all)")
    p_bf.add_argument(
        "--commit",
        default="",
        help="index THIS commit (sha or ref present in the clone) instead of the "
        "branch tip; implies --no-fetch. Paired arms pin the "
        "same tree while the shared mirror keeps moving",
    )
    p_bf.add_argument(
        "--paths-file",
        default="",
        help="repair mode: re-ingest ONLY these repo-relative paths (one per line) at "
        "the branch tip; leaves the git_head watermark and cursor untouched",
    )
    p_bf.add_argument("--concurrency", type=int, default=4)
    _add_backfill_pass_flags(p_bf)
    p_bf.set_defaults(func=_cmd_backfill)


def _add_reembed_parser(csub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    """`loom code reembed`: live chunks under a second embedding model."""
    p_re = csub.add_parser(
        "reembed",
        help="Write the namespace's live chunks under a second embedding model's label: "
        "in-database, resumable, no checkout.",
    )
    _add_common(p_re)
    p_re.add_argument(
        "--embedding-model",
        required=True,
        help="second model spec, e.g. prefix:512:text-embedding-3-large (must be listed in "
        "LOOM_CODE_SEARCH_VECTOR_MODELS; its table in LOOM_CODE_VECTOR_TABLES)",
    )
    p_re.add_argument("--source-model", default="", help="default: the served embedder's label")
    p_re.add_argument("--prefix", default="", help="path prefix to cover (default: the namespace)")
    p_re.add_argument("--batch", type=int, default=2000, help="source rows read per pass")
    p_re.set_defaults(func=_cmd_reembed)


def _add_pr_history_parsers(csub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    """`repack`: regenerate stored PR context packs in the current shape."""
    p_rp = csub.add_parser(
        "repack",
        help="Regenerate context packs for recently merged PRs in the current pack shape.",
    )
    p_rp.add_argument("--org", default=None, help="default: LOOM_CODE_ORG")
    p_rp.add_argument("--namespace", default=None, help="default: LOOM_CODE_NAMESPACE")
    p_rp.add_argument("--days", type=int, default=60)
    p_rp.add_argument("--limit", type=int, default=400)
    p_rp.add_argument("--dry-run", action="store_true")
    p_rp.set_defaults(func=_cmd_repack)


def _add_describe_parser(csub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    """`loom code describe`: LLM one-liners for the top-PageRank chunks."""
    p_desc = csub.add_parser("describe", help="LLM contextual one-liners for top-PageRank chunks.")
    _add_common(p_desc)
    p_desc.add_argument("--top-pct", type=float, default=20.0)
    p_desc.add_argument("--limit", type=int, default=0, help="max chunks (0 = all in pct)")
    p_desc.add_argument(
        "--suffix",
        default="",
        help="only files with this suffix (e.g. .cpp — implementation files "
        "move conceptual retrieval; headers dominate PageRank)",
    )
    p_desc.add_argument("--concurrency", type=int, default=8)
    p_desc.set_defaults(func=_cmd_describe)


def _add_answers_purge_parser(csub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    """`loom code answers-purge`: tombstone cached code.ask answers."""
    p_purge = csub.add_parser(
        "answers-purge",
        help="Tombstone cached code.ask answers (after a synthesis-model "
        "upgrade, stale weaker-model answers would otherwise be served "
        "forever — the cache only self-invalidates on cited-file change). "
        "Dry run by default (counts and samples the matches); --apply tombstones.",
    )
    p_purge.add_argument(
        "--llm-model",
        default="",
        help="only entries synthesized by this model (e.g. gpt-6-luna)",
    )
    p_purge.add_argument("--all", action="store_true", help="purge every cached answer")
    add_apply_flags(p_purge, writes="tombstone the matched answers")
    _add_common(p_purge)
    p_purge.set_defaults(func=_cmd_answers_purge)


def _add_github_backfill_parsers(csub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    """`backfill-reviews`, `backfill-assignees`, `backfill-items`: GitHub history fills."""
    p_rev = csub.add_parser(
        "backfill-reviews",
        help="Inline review comments for merged PRs already in code_pr_history "
        "that have none stored yet (newest first, one GitHub request per PR).",
    )
    _add_common(p_rev)
    p_rev.add_argument("--prs", type=int, default=300, help="how many PRs to fetch this run")
    p_rev.set_defaults(func=_cmd_backfill_reviews)

    p_asg = csub.add_parser(
        "backfill-assignees",
        help="Fill code_pr_history.assignees (who the repo assigned to review) for "
        "merged PRs already stored, walking the newest closed PRs 100 per request.",
    )
    _add_common(p_asg)
    p_asg.add_argument("--pages", type=int, default=20, help="list pages of 100 closed PRs to walk")
    p_asg.set_defaults(func=_cmd_backfill_assignees)

    p_items = csub.add_parser(
        "backfill-items",
        help="Fill code_gh_items (issues + PRs, any state) updated since a date, "
        "100 per request, oldest update first; moves gh_items_cursor.",
    )
    _add_common(p_items)
    p_items.add_argument(
        "--since",
        default="",
        help="ISO date; default: the worker cursor or code_gh_items_backfill_days ago",
    )
    p_items.add_argument("--pages", type=int, default=50, help="pages of 100 items to walk")
    p_items.set_defaults(func=_cmd_backfill_items)


def _add_export_matrix_parser(csub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    """`loom code export-matrix`: the co-change maintenance matrix."""
    p_matrix = csub.add_parser(
        "export-matrix",
        help="Maintenance matrix from merged-PR co-change: 'when this file changes, "
        "these usually change too', with evidence, as markdown for an "
        "instructions file (AGENTS.md / copilot-instructions.md).",
    )
    _add_common(p_matrix)
    p_matrix.add_argument("--prs", type=int, default=3000, help="window of most recent merged PRs")
    p_matrix.add_argument("--min-together", type=int, default=4)
    p_matrix.add_argument("--min-conf", type=float, default=0.5)
    p_matrix.add_argument("--max-partners", type=int, default=6)
    p_matrix.add_argument(
        "--max-rows", type=int, default=40, help="cap on source files (0 = all); ~55 tokens per row"
    )
    p_matrix.add_argument("--out", default="", help="write markdown here instead of stdout")
    p_matrix.add_argument("--json", action="store_true")
    p_matrix.set_defaults(func=_cmd_export_matrix)


def _add_lint_instructions_parser(
    csub: argparse._SubParsersAction[argparse.ArgumentParser],
) -> None:
    """`loom code lint-instructions`: stale references in agent-instruction files."""
    p_lint = csub.add_parser(
        "lint-instructions",
        help="Agent-instruction files (CLAUDE.md, AGENTS.md, copilot-instructions, "
        ".claude/**.md, skills) whose path or symbol references no longer "
        "resolve at the clone's head.",
    )
    _add_common(p_lint)
    p_lint.add_argument(
        "--file",
        action="append",
        default=[],
        help="lint only these repo-relative files (repeatable)",
    )
    p_lint.add_argument(
        "--glob",
        action="append",
        default=[],
        help="also lint tree paths matching this fnmatch pattern, e.g. 'docs/*.md' (repeatable)",
    )
    p_lint.add_argument(
        "--symbols",
        action="store_true",
        help="also resolve `Qualified::names` against loom.code_symbols",
    )
    p_lint.add_argument(
        "--all", action="store_true", help="list clean files too (default: stale only)"
    )
    p_lint.add_argument("--json", action="store_true")
    p_lint.add_argument(
        "--fail-on-stale", action="store_true", help="exit 1 when any reference is stale (for CI)"
    )
    p_lint.set_defaults(func=_cmd_lint_instructions)


def register_code_cli(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    p = sub.add_parser("code", help="Code intelligence plane operations.")
    csub = p.add_subparsers(dest="code_cmd", required=True)

    p_init = csub.add_parser("init", help="Create the bare clone + seed state.")
    _add_common(p_init)
    p_init.set_defaults(func=_cmd_init)

    _add_backfill_parser(csub)
    _add_reconcile_parser(csub)
    _add_compact_parser(csub)
    _add_reembed_parser(csub)
    _add_vector_twin_parser(csub)

    p_xref = csub.add_parser(
        "crossref",
        help="SQL/shell test -> implementing C++ reference pass "
        "(writes code_test_map method=content_reference; idempotent).",
    )
    _add_common(p_xref)
    p_xref.set_defaults(func=_cmd_crossref)

    p_cov = csub.add_parser(
        "coverage-import",
        help="Import the newest per-test line coverage run of the configured source "
        "(writes code_test_map method=coverage and code_coverage_files).",
    )
    _add_common(p_cov)
    p_cov.add_argument(
        "--dry-run", action="store_true", help="read and plan; print the counts, write nothing"
    )
    p_cov.set_defaults(func=_cmd_coverage_import)

    p_pairs = csub.add_parser(
        "export-pairs",
        help="Export (query -> acted-on unit, hard negatives) retrieval "
        "pairs from real traffic (trace x outcome join) for embedder/"
        "reranker fine-tuning.",
    )
    _add_common(p_pairs)
    p_pairs.add_argument("--days", type=int, default=90)
    p_pairs.add_argument("--out", required=True, help="output JSONL path")
    p_pairs.set_defaults(func=_cmd_export_pairs)

    _register_backfill_prs(csub)
    _add_pr_history_parsers(csub)
    _add_describe_parser(csub)
    _add_answers_purge_parser(csub)

    _add_scip_full_parser(csub)

    p_delta = csub.add_parser(
        "scip-delta",
        help="One compiler-tier delta pass (same code as the code_scip_delta "
        "worker), by hand: git_head vs scip_commit per configured "
        "repo, scip-clang over the changed TUs, apply. Prints `RESULT: ...`.",
    )
    p_delta.set_defaults(func=_cmd_scip_delta)

    p_entries = csub.add_parser(
        "entries",
        help="One line per configured repository: org namespace checkout "
        "lock-file (the lock path resolved; a manual run takes it through "
        "`loom code entry-lock`).",
    )
    p_entries.set_defaults(func=_cmd_entries)

    p_warm = csub.add_parser(
        "warm",
        help="Pre-seed the code.ask answer cache from a question list (one "
        "question per line, or JSONL with a 'question' field). Each miss "
        "pays one synthesis; every future asker fleet-wide gets the "
        "cached answer. Safe to re-run: hits are free.",
    )
    p_warm.add_argument("file", help="questions file (text lines or JSONL)")
    _add_common(p_warm)
    p_warm.set_defaults(func=_cmd_warm)

    p_retry = csub.add_parser("scip-retry", help="Reset a failed SCIP batch to pending.")
    _add_common(p_retry)
    p_retry.add_argument("batch_id")
    p_retry.set_defaults(func=_cmd_scip_retry)

    _add_github_backfill_parsers(csub)

    p_stats = csub.add_parser("stats", help="Watermarks + live row counts.")
    _add_common(p_stats)
    p_stats.set_defaults(func=_cmd_stats)

    _add_export_matrix_parser(csub)
    _add_lint_instructions_parser(csub)


def _register_backfill_prs(csub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    p_prs = csub.add_parser("backfill-prs", help="Merged-PR history from GitHub.")
    _add_common(p_prs)
    p_prs.add_argument("--months", type=int, default=None)
    p_prs.add_argument("--max-prs", type=int, default=100000)
    p_prs.set_defaults(func=_cmd_backfill_prs)


# ---------------------------------------------------------------------------


def _cmd_init(args: argparse.Namespace) -> int:
    return _run("init", _init(args))


def _cmd_export_pairs(args: argparse.Namespace) -> int:
    return _run("export-pairs", _export_pairs(args))


def _add_reconcile_parser(csub: Any) -> None:
    p_rec = csub.add_parser(
        "reconcile",
        help="Content-based staleness check: paths whose chunks predate their last "
        "change at the branch head; --heal re-ingests them (watermark untouched).",
    )
    p_rec.add_argument("--org", default=None, help="default: LOOM_CODE_ORG")
    p_rec.add_argument("--namespace", default=None, help="default: LOOM_CODE_NAMESPACE")
    p_rec.add_argument("--heal", action="store_true", help="re-ingest the stale paths")
    p_rec.add_argument("--limit", type=int, default=0, help="max paths to heal (0 = all)")
    p_rec.add_argument("--max-commits", type=int, default=1000)
    p_rec.add_argument("--no-fetch", action="store_true")
    p_rec.add_argument("--show", type=int, default=20, help="stale paths to print")
    p_rec.set_defaults(func=_cmd_reconcile)


def _cmd_reconcile(args: argparse.Namespace) -> int:
    return _run("reconcile", _reconcile(args))


def _cmd_reembed(args: argparse.Namespace) -> int:
    return _run("reembed", _reembed(args))


def _add_vector_twin_parser(csub: Any) -> None:
    """`loom code vector-twin`."""
    p_tw = csub.add_parser(
        "vector-twin",
        help="Fill a namespace's Matryoshka-prefix twin of the chunk vectors "
        "inside ClickHouse: no re-embed, resumable.",
    )
    _add_common(p_tw)
    p_tw.add_argument(
        "--embedding-model",
        default="",
        help="twin label prefix:<dim>:<served model> (default: the configured twin of "
        "the served model, from LOOM_CODE_VECTOR_TABLES)",
    )
    p_tw.add_argument(
        "--all-namespaces",
        action="store_true",
        help="every configured code namespace of the org, served ones first",
    )
    p_tw.set_defaults(func=_cmd_vector_twin)


def _cmd_vector_twin(args: argparse.Namespace) -> int:
    return _run("vector-twin", _vector_twin(args))


def _add_compact_parser(csub: Any) -> None:
    """`loom code compact`."""
    p_c = csub.add_parser(
        "compact",
        help="OPTIMIZE ... FINAL a code vector table when its physical copies per key "
        "reach --min-ratio, and wait for the merge. Dry run by default (measures the "
        "ratio); --apply optimizes.",
    )
    p_c.add_argument(
        "--table",
        default="code_chunks_prefix",
        help="code_chunks_prefix (default), code_chunks (50 GiB: announced window only), "
        "all (both), or a table named in LOOM_CODE_VECTOR_TABLES",
    )
    p_c.add_argument(
        "--min-ratio", type=float, default=None, help="default: LOOM_CODE_COMPACT_MIN_RATIO (1.5)"
    )
    add_apply_flags(p_c, writes="run the OPTIMIZE when the ratio is reached")
    p_c.add_argument(
        "--timeout-s", type=float, default=1800.0, help="how long to wait for each merge to land"
    )
    p_c.set_defaults(func=_cmd_compact)


def _cmd_compact(args: argparse.Namespace) -> int:
    return _run("compact", _compact(args))


def _cmd_coverage_import(args: argparse.Namespace) -> int:
    return _run("coverage-import", _coverage_import(args))


def _cmd_crossref(args: argparse.Namespace) -> int:
    return _run("crossref", _crossref(args))


def _cmd_backfill(args: argparse.Namespace) -> int:
    return _run("backfill", _backfill(args))


def _cmd_backfill_prs(args: argparse.Namespace) -> int:
    return _run("backfill-prs", _backfill_prs(args))


def _print_scip_lock_path(args: argparse.Namespace) -> int:
    from server.code.app.index.entry_lock import entry_lock_path

    org, ns = _identity(args)
    print(entry_lock_path(org, ns).resolve())
    return 0


def _cmd_entry_lock(args: argparse.Namespace) -> int:
    return _run("entry-lock", _entry_locked(args))


def _cmd_scip_full(args: argparse.Namespace) -> int:
    return _run("scip-full", _scip_full(args))


def _cmd_entries(args: argparse.Namespace) -> int:
    return _run("entries", _entries(args))


def _cmd_scip_delta(args: argparse.Namespace) -> int:
    return _run("scip-delta", _scip_delta(args))


def _cmd_warm(args: argparse.Namespace) -> int:
    return _run("warm", _warm(args))


def _cmd_answers_purge(args: argparse.Namespace) -> int:
    if not args.llm_model and not getattr(args, "all", False):
        print("answers-purge requires --llm-model <name> or --all", file=sys.stderr)
        return 2
    return _run("answers-purge", _answers_purge(args))


def _cmd_describe(args: argparse.Namespace) -> int:
    return _run("describe", _describe(args))


def _cmd_scip_retry(args: argparse.Namespace) -> int:
    return _run("scip-retry", _scip_retry(args))


def _cmd_stats(args: argparse.Namespace) -> int:
    return _run("stats", _stats(args))


def _cmd_backfill_reviews(args: argparse.Namespace) -> int:
    return _run("backfill-reviews", _backfill_reviews(args))


def _cmd_backfill_items(args: argparse.Namespace) -> int:
    return _run("backfill-items", _backfill_items(args))


def _cmd_backfill_assignees(args: argparse.Namespace) -> int:
    return _run("backfill-assignees", _backfill_assignees(args))


def _cmd_export_matrix(args: argparse.Namespace) -> int:
    return _run("export-matrix", _export_matrix(args))


def _cmd_lint_instructions(args: argparse.Namespace) -> int:
    return _run("lint-instructions", _lint_instructions(args))


def _cmd_repack(args: argparse.Namespace) -> int:
    return _run("repack", _repack(args))
