"""code.conventions: the ClickHouse repository's recurring maintainer checks as
deterministic facts over one PR's diff and what the index, the PR ledger and the bare
mirror already hold.

Seven rules, each a verdict with the evidence rows (`path`, `line`, `text`)
that decide it and an epistemic label. Five are checks (`setting_history`,
`setting_docs`, `error_code_declared`, `bugfix_has_test` + its inverse
`test_only_change`, `stateless_test_number` + `reference_missing`); two are
leads by design (`changelog_category`, `wide_callers_untested`). No model.

The diff comes from the ledger's merge commit on the bare mirror
(`git diff -U0 <merge>^1 <merge>`, bounded), from a `head_sha` the mirror
knows (its merge-base with the indexed branch), or from the caller
(`files` + `diff`, an open PR). Every read is keyed and bounded; a read that
fails leaves its rule `unknown`, never the call.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any, cast

from server.code.api.models_conventions import CodeConventionsOutput
from server.code.app.common import (
    new_trace_run_id,
    require_code_intel,
    resolve_code_identity,
    schedule_code_trace,
)
from server.code.app.coverage import claim_tests, positive_control
from server.code.app.index import gitrepo
from server.code.app.index.mirrors import history_dir
from server.code.app.settings import history_chunks
from server.code.app.spans import code_span, set_code_span_attributes
from server.code.app.ttl_cache import TtlCache
from server.code.domain.index import conventions as cv
from server.code.domain.index import conventions_rules
from server.code.domain.index import coverage_claim as cc
from server.code.domain.index.blame_range import sha_text
from server.code.domain.index.repos import repo_for_namespace
from server.code.store.index import CodeRepo
from server.core.errors import ValidationError
from server.settings import settings

logger = logging.getLogger(__name__)

# (kind, key...) -> value: docs mentions per (tip, name), category census per
# (org, ns, prefix)
_CACHE: TtlCache[tuple[Any, ...], Any] = TtlCache(600.0, maxsize=1024)
_SHA_RE = re.compile(r"^[0-9a-f]{7,40}$")
_DOCS_PATHSPECS = (
    "docs/",
    ":(exclude,glob)docs/??/**",
    ":(exclude)docs/_site",
    ":(exclude)docs/zh-cn",
    ":(exclude)docs/pt-br",
)
_MAX_SETTINGS = 4
_MAX_ERROR_CODE_FILES = 30
_MAX_SRC_FILES = 20
_MAX_TOUCHED = 40
_MAX_WIDE = 5
_MAX_PREFIXES = 3


# ---------------------------------------------------------------- the source


@dataclass
class Source:
    pr_number: int = 0
    title: str = ""
    labels: list[str] = field(default_factory=list)
    body_excerpt: str = ""
    files: list[str] = field(default_factory=list)
    statuses: dict[str, str] = field(default_factory=dict)
    merged_at: str = ""
    merge_sha: str = ""
    head_sha: str = ""
    # the branch the ledger says the PR merged into ('' = unknown)
    target_branch: str = ""
    # ledger | head_sha | supplied
    source: str = ""
    # the commit whose tree is the post-image of the diff ('' = none in the mirror)
    post_sha: str = ""
    diff: cv.Diff = field(default_factory=cv.Diff)
    notes: list[str] = field(default_factory=list)


def _iso(ts: Any) -> str:
    return ts.isoformat(sep=" ") if hasattr(ts, "isoformat") else str(ts or "")


async def _rev_ok(git_dir: str, rev: str) -> bool:
    try:
        await gitrepo.run(git_dir, "rev-parse", "--verify", "-q", f"{rev}^{{commit}}")
        return True
    except gitrepo.GitError:
        return False


async def _diff_between(
    git_dir: str, base: str, head: str, *, max_bytes: int
) -> tuple[cv.Diff, list[cv.FileDiff]]:
    """(parsed -U0 patch, name-status entries) between two commits, bounded."""
    text, truncated = await gitrepo.run_bounded(
        git_dir,
        "diff",
        "-U0",
        "--no-renames",
        "--no-color",
        base,
        head,
        max_bytes=max_bytes,
        timeout_s=settings.code_conventions_diff_timeout_s,
    )
    diff = cv.parse_unified_diff(text, truncated=truncated)
    if truncated:
        # the last section may be cut mid-file: drop it, keep the complete ones
        last = list(diff.files)[-1] if diff.files else ""
        if last:
            diff.files.pop(last, None)
    entries = await gitrepo.diff_files(git_dir, base, head)
    return diff, [cv.FileDiff(path=e.path, status=e.status) for e in entries]


async def _from_mirror(src: Source, git_dir: str, base: str, head: str, *, max_bytes: int) -> None:
    diff, entries = await _diff_between(git_dir, base, head, max_bytes=max_bytes)
    src.diff = diff
    if entries:
        src.files = [e.path for e in entries]
        src.statuses = {e.path: e.status for e in entries}
    for path, fd in diff.files.items():
        src.statuses.setdefault(path, fd.status)
        if path not in src.files:
            src.files.append(path)
    if diff.truncated:
        src.notes.append(
            f"diff cut at {max_bytes} bytes: {len(diff.files)} of {len(src.files)} files parsed"
        )


async def resolve_source(repo: CodeRepo, org: str, ns: str, cfg: Any | None, inp: Any) -> Source:
    src = Source(pr_number=int(inp.pr_number or 0), head_sha=(inp.head_sha or "").strip().lower())
    max_bytes = int(settings.code_conventions_diff_max_bytes)
    git_dir, gap = await history_dir(cfg)
    if src.pr_number:
        row = await repo.pr_facts(org, ns, src.pr_number)
        if row:
            src.title = str(row.get("title") or "")
            src.labels = [str(x) for x in (row.get("labels") or [])]
            src.body_excerpt = str(row.get("body_excerpt") or "")
            src.files = [str(f) for f in (row.get("files") or [])]
            src.merged_at = _iso(row.get("merged_at"))
            src.merge_sha = sha_text(row.get("merge_commit_sha"))
            src.target_branch = str(row.get("target_branch") or "")
            src.source = "ledger"
            branch = cfg.git_branch if cfg is not None else settings.code_git_branch
            if src.target_branch and src.target_branch != branch:
                src.notes.append(f"merged into {src.target_branch}, not {branch}")
        else:
            src.notes.append("pr_not_in_ledger")
    if inp.diff or inp.files:
        # the supplied change defines the file list; a ledger row for the same
        # number contributes title, labels and body only
        src.diff = cv.parse_unified_diff(inp.diff) if inp.diff else cv.Diff()
        src.source = "supplied"
        src.files = list(dict.fromkeys([*inp.files, *src.diff.files]))
        src.statuses = {path: fd.status for path, fd in src.diff.files.items()}
        if git_dir and inp.diff:
            # no post-image in the mirror: file reads use the branch tip as pre-image
            src.notes.append("supplied diff: file reads use the indexed branch tip as pre-image")
        return src
    if not git_dir:
        src.notes.append(
            f"{gap or 'no bare mirror configured for this namespace'}: "
            "diff-dependent rules are unknown"
        )
        return src
    try:
        if src.head_sha and _SHA_RE.match(src.head_sha) and await _rev_ok(git_dir, src.head_sha):
            branch = cfg.git_branch if cfg is not None else settings.code_git_branch
            tip = await gitrepo.head_sha(git_dir, branch)
            base = (await gitrepo.run(git_dir, "merge-base", tip, src.head_sha)).strip()
            await _from_mirror(src, git_dir, base, src.head_sha, max_bytes=max_bytes)
            src.post_sha = src.head_sha
            src.source = "head_sha"
        elif src.merge_sha and await _rev_ok(git_dir, src.merge_sha):
            if src.head_sha:
                src.notes.append("head_sha not in the mirror: the ledger's merge commit was used")
            await _from_mirror(
                src, git_dir, f"{src.merge_sha}^1", src.merge_sha, max_bytes=max_bytes
            )
            src.post_sha = src.merge_sha
        elif src.merge_sha:
            src.notes.append("merge commit not in the mirror: diff-dependent rules are unknown")
    except gitrepo.GitError as e:
        logger.info("conventions: mirror diff failed: %s", str(e)[:200])
        src.notes.append("mirror diff failed: diff-dependent rules are unknown")
    return src


# ---------------------------------------------------------------- reads per rule


async def _docs_mentions(git_dir: str, tip: str, names: list[str]) -> dict[str, list[str] | None]:
    async def one(name: str) -> list[str] | None:
        key = ("docs", git_dir, tip, name)
        hit = _CACHE.get(key)
        if hit is not None:
            return cast(list[str], hit)
        try:
            paths = await gitrepo.grep_paths(git_dir, tip, name, _DOCS_PATHSPECS, limit=20)
        except gitrepo.GitError as e:
            logger.info("conventions: docs grep failed for %s: %s", name, str(e)[:120])
            return None
        _CACHE.put(key, paths)
        return paths

    got = await asyncio.gather(*(one(n) for n in names[:_MAX_SETTINGS]))
    return dict(zip(names[:_MAX_SETTINGS], got, strict=True))


async def _externs(
    repo: CodeRepo,
    org: str,
    ns: str,
    git_dir: str,
    post_sha: str,
    uses: list[cv.ErrorCodeUse],
    branch: str,
) -> tuple[dict[tuple[str, str], bool | None], bool]:
    """(declared[(path, name)], exact) — from the file at `post_sha` on the
    mirror (the post-image) or at the indexed branch tip (the pre-image of a
    supplied diff: the namespace's own branch, a release branch's for a release-branch
    namespace); the index's chunks when the mirror cannot serve it."""
    by_path: dict[str, list[str]] = {}
    for u in uses:
        by_path.setdefault(u.path, []).append(u.name)
    paths = list(by_path)[:_MAX_ERROR_CODE_FILES]
    out: dict[tuple[str, str], bool | None] = {}
    exact = True
    rev = post_sha
    if git_dir and not rev:
        try:
            rev = await gitrepo.head_sha(git_dir, branch)
        except gitrepo.GitError:
            rev = ""

    async def from_mirror(path: str) -> bytes | gitrepo.BlobTooLarge | None:
        try:
            return await gitrepo.show_file(
                git_dir, rev, path, max_bytes=settings.code_ingest_max_file_bytes
            )
        except gitrepo.BlobTooLarge as e:
            return e  # over the cap: unread; the index holds no chunks of these bytes
        except gitrepo.GitError:
            return None

    async def from_index(path: str, name: str) -> bool | None:
        try:
            rows = await repo.chunk_contains(org, ns, path, cv.extern_pattern(name), limit=1)
        except Exception as e:  # noqa: BLE001 — a failed read is an unknown, not a verdict
            logger.info("conventions: extern read failed: %s", e)
            return None
        return bool(rows)

    if git_dir and rev:
        blobs = await asyncio.gather(*(from_mirror(p) for p in paths))
        for path, blob in zip(paths, blobs, strict=True):
            if isinstance(blob, gitrepo.BlobTooLarge):
                for name in by_path[path]:
                    out[(path, name)] = None  # unknown, not undeclared
                exact = False
                continue
            if blob is None:
                # a file the post-image lacks (deleted later, or a supplied
                # diff adding it): fall through to the index
                for name in by_path[path]:
                    out[(path, name)] = await from_index(path, name)
                    exact = False
                continue
            text = blob.decode("utf-8", "replace")
            for name in by_path[path]:
                out[(path, name)] = cv.extern_pattern(name) in text
        return out, exact
    for path in paths:
        for name in by_path[path]:
            out[(path, name)] = await from_index(path, name)
    return out, False


async def _preexisting(git_dir: str, post_sha: str, paths: list[str]) -> set[str] | None:
    """Which of `paths` exist in the PR's pre-image (the merge commit's
    first parent). None when there is no post-image in the mirror — then
    the index is the pre-image (an open PR against the branch tip)."""
    if not git_dir or not post_sha or not paths:
        return None if not post_sha else set()
    out: set[str] = set()

    async def one(path: str) -> None:
        try:
            await gitrepo.run(git_dir, "rev-parse", "--verify", "-q", f"{post_sha}^1:{path}")
            out.add(path)
        except gitrepo.GitError:
            return

    await asyncio.gather(*(one(p) for p in paths[:40]))
    return out


async def _census(
    repo: CodeRepo, org: str, ns: str, prefixes: list[str], branch: str = ""
) -> dict[str, dict[str, int] | None]:
    async def one(prefix: str) -> dict[str, int] | None:
        key = ("census", org, ns, prefix, branch)
        hit = _CACHE.get(key)
        if hit is not None:
            return cast(dict[str, int], hit)
        try:
            labels = await repo.pr_category_census(org, ns, prefix, days=365, branch=branch)
        except Exception as e:  # noqa: BLE001 — a lead's prior; unknown is fine
            logger.info("conventions: census read failed for %s: %s", prefix, e)
            return None
        dist: dict[str, int] = {}
        for lab, n in labels.items():
            cat = conventions_rules.LABEL_CATEGORY.get(lab)
            if cat:
                dist[cat] = dist.get(cat, 0) + int(n)
        _CACHE.put(key, dist)
        return dist

    got = await asyncio.gather(*(one(p) for p in prefixes[:_MAX_PREFIXES]))
    return dict(zip(prefixes[:_MAX_PREFIXES], got, strict=True))


async def _wide_callers(
    repo: CodeRepo,
    org: str,
    ns: str,
    src: Source,
    *,
    min_dirs: int,
) -> tuple[list[conventions_rules.TouchedFunction], dict[str, tuple[str, int]], bool, bool]:
    """Touched functions with their hop-1 caller directories, the coverage
    verdict for the widest few, the coverage control's state, and whether
    the control ran."""
    control_ok, _probe = await positive_control(repo, org, ns)
    paths = [
        p
        for p, fd in src.diff.files.items()
        if fd.status != "D"
        and p.startswith(("src/", "programs/", "base/"))
        and p.endswith((".cpp", ".h"))
        and fd.added
    ]
    paths = sorted(paths, key=lambda p: -len(src.diff.files[p].added))[:_MAX_SRC_FILES]
    if not paths:
        return [], {}, control_ok, True
    try:
        # both tiers: the compiler tier's one-line spans carry the edges, the
        # parser tier's extents say which function an added line falls in
        symbols = await repo.functions_for_paths(org, ns, paths)
    except Exception as e:  # noqa: BLE001 — the check reports unknown
        logger.info("conventions: functions read failed: %s", e)
        return [], {}, False, True
    funcs = conventions_rules.touched_functions(src.diff, symbols)[:_MAX_TOUCHED]
    if not funcs:
        return [], {}, control_ok, True
    try:
        ids = [sid for f in funcs for sid in f.symbol_ids]
        edges = await repo.edges_for_symbols(
            org,
            ns,
            ids,
            direction="callers",
            limit=6000,
            per_match=400,
        )
    except Exception as e:  # noqa: BLE001 — the check reports unknown
        logger.info("conventions: callers read failed: %s", e)
        return funcs, {}, False, True
    conventions_rules.attach_callers(funcs, edges)
    wide = sorted(
        (f for f in funcs if len(f.caller_dirs) > min_dirs), key=lambda f: -len(f.caller_dirs)
    )[:_MAX_WIDE]
    coverage: dict[str, tuple[str, int]] = {}
    if wide and control_ok:

        async def one(f: conventions_rules.TouchedFunction) -> tuple[str, int]:
            try:
                ordered, total = await claim_tests(
                    repo, org, ns, "symbol", f.name, [f.path], limit=5
                )
            except Exception as e:  # noqa: BLE001 — the check reports unknown
                logger.info("conventions: coverage read failed: %s", e)
                return cv.UNKNOWN, 0
            return cc.coverage_verdict(ordered, control_ok=True), max(len(ordered), total)

        got = await asyncio.gather(*(one(f) for f in wide))
        coverage = {f.symbol_id: v for f, v in zip(wide, got, strict=True)}
    return funcs, coverage, control_ok, True


# ---------------------------------------------------------------- the handler


def _wanted(inp: Any) -> set[str]:
    return set(inp.rules) if inp.rules else set(cv.RULES)


@dataclass
class Reads:
    """Everything the rules need from the index, the ledger and the mirror,
    gathered in one round; a failed read is None / an empty default."""

    diff_known: bool = False
    changes: list[cv.SettingChange] = field(default_factory=list)
    uses: list[cv.ErrorCodeUse] = field(default_factory=list)
    new_tests: list[conventions_rules.NewStatelessTest] = field(default_factory=list)
    category: str = ""
    cat_source: str = ""
    min_dirs: int = 5
    docs: dict[str, list[str] | None] = field(default_factory=dict)
    hist: list[dict[str, Any]] | None = None
    # the branch the ledger says the PR merged into when it is not the
    # namespace's ('' = on-branch or unknown)
    off_branch: str = ""
    index_paths: list[str] | None = None
    owners: dict[str, tuple[int, str]] = field(default_factory=dict)
    preexisting: set[str] | None = None
    max_number: int = 0
    declared: dict[tuple[str, str], bool | None] = field(default_factory=dict)
    externs_exact: bool = True
    census: dict[str, dict[str, int] | None] = field(default_factory=dict)
    funcs: list[conventions_rules.TouchedFunction] = field(default_factory=list)
    coverage: dict[str, tuple[str, int]] = field(default_factory=dict)
    control_ok: bool = False
    control_ran: bool = False


async def _gather(
    repo: CodeRepo,
    org: str,
    ns: str,
    cfg: Any | None,
    src: Source,
    wanted: set[str],
    has_diff_input: bool,
) -> Reads:
    git_dir = (await history_dir(cfg))[0]
    branch = cfg.git_branch if cfg is not None else settings.code_git_branch
    rd = Reads(diff_known=bool(src.diff.files) or has_diff_input)
    if src.target_branch and src.target_branch != branch:
        rd.off_branch = src.target_branch  # not this branch's change
    rd.changes = cv.setting_changes(src.diff) if rd.diff_known else []
    rd.uses = cv.error_code_uses(src.diff) if rd.diff_known else []
    rd.new_tests = conventions_rules.new_stateless_tests(src.files, src.statuses)
    rd.category, rd.cat_source = conventions_rules.category_of(src.labels, src.body_excerpt)
    rd.min_dirs = int(settings.code_conventions_wide_caller_dirs)
    prefixes = conventions_rules.dominant_prefixes(src.files)
    tracked = [c for c in rd.changes if c.file in cv.HISTORY_TRACKED_FILES]

    async def _docs() -> dict[str, list[str] | None]:
        if not rd.changes or "setting_docs" not in wanted or not git_dir:
            return {c.name: None for c in rd.changes}
        tip = src.post_sha
        if not tip:
            try:
                tip = await gitrepo.head_sha(git_dir, branch)
            except gitrepo.GitError:
                return {c.name: None for c in rd.changes}
        return await _docs_mentions(git_dir, tip, [c.name for c in rd.changes])

    async def _hist() -> list[dict[str, Any]] | None:
        if not tracked or "setting_history" not in wanted:
            return []
        try:
            return await history_chunks(repo, org, ns)
        except Exception as e:  # noqa: BLE001 — the check reports unknown
            logger.info("conventions: history read failed: %s", e)
            return None

    async def _numbers() -> tuple[
        list[str] | None, dict[str, tuple[int, str]], set[str] | None, int
    ]:
        if not rd.new_tests or "stateless_test_number" not in wanted:
            return [], {}, None, 0
        try:
            paths, max_number = await asyncio.gather(
                repo.stateless_paths_for_numbers(org, ns, [t.number for t in rd.new_tests]),
                repo.max_stateless_test_number(org, ns),
            )
        except Exception as e:  # noqa: BLE001 — the check reports unknown
            logger.info("conventions: stateless read failed: %s", e)
            return None, {}, None, 0
        own = {t.stem for t in rd.new_tests}
        same = [p for p in paths if (cv.stem_of_stateless(p) or ("", ""))[1] in own]
        owners: dict[str, tuple[int, str]] = {}
        pre: set[str] | None = None
        if same:
            try:
                owners, pre = await asyncio.gather(
                    repo.first_owner_of_paths(org, ns, same, branch=branch),
                    _preexisting(git_dir, src.post_sha, same),
                )
            except Exception as e:  # noqa: BLE001 — the check reports unknown
                logger.info("conventions: owner read failed: %s", e)
        return paths, owners, pre, int(max_number or 0)

    with code_span("conventions.reads", seeds=len(src.files)):
        docs, hist, numbers, externs, census, wide = await asyncio.gather(
            _docs(),
            _hist(),
            _numbers(),
            _externs(repo, org, ns, git_dir, src.post_sha, rd.uses, branch)
            if rd.uses and "error_code_declared" in wanted
            else _pair(),
            _census(repo, org, ns, prefixes, branch)
            if rd.category and "changelog_category" in wanted
            else _empty_dict(),
            _wide_callers(repo, org, ns, src, min_dirs=rd.min_dirs)
            if rd.diff_known and "wide_callers_untested" in wanted
            else _triple(),
        )
    rd.docs, rd.hist, rd.census = docs, hist, census
    rd.index_paths, rd.owners, rd.preexisting, rd.max_number = numbers
    rd.declared, rd.externs_exact = externs
    rd.funcs, rd.coverage, rd.control_ok, rd.control_ran = wide
    return rd


def _evaluate(src: Source, rd: Reads, wanted: set[str]) -> list[cv.RuleResult]:
    diff = src.diff
    known, files = rd.diff_known, bool(src.files)
    out: list[cv.RuleResult] = []

    def add(rule: str, ok: bool, make: Any) -> None:
        if rule in wanted:
            out.append(make() if ok else _unknown(rule))

    add("setting_history", known, lambda: cv.rule_setting_history(rd.changes, diff, rd.hist))
    add("setting_docs", known, lambda: cv.rule_setting_docs(rd.changes, diff, rd.docs))
    add("error_code_declared", known, lambda: _error_code_rule(rd, diff))
    add(
        "bugfix_has_test",
        files,
        lambda: conventions_rules.rule_bugfix_has_test(
            src.files,
            src.statuses,
            src.title,
            src.labels,
            src.body_excerpt,
            off_branch=rd.off_branch,
        ),
    )
    add("test_only_change", files, lambda: conventions_rules.rule_test_only_change(src.files))
    add(
        "stateless_test_number",
        files,
        lambda: conventions_rules.rule_stateless_test_number(
            rd.new_tests,
            rd.index_paths,
            rd.owners,
            merged_at=src.merged_at,
            max_number=rd.max_number,
            preexisting=rd.preexisting,
            title=src.title,
            deleted={p for p, st in src.statuses.items() if st == "D"},
            off_branch=rd.off_branch,
        ),
    )
    add(
        "reference_missing",
        files,
        lambda: conventions_rules.rule_reference_missing(src.files, src.statuses, rd.new_tests),
    )
    add(
        "changelog_category",
        files,
        lambda: conventions_rules.rule_changelog_category(rd.category, rd.cat_source, rd.census),
    )
    add(
        "wide_callers_untested",
        known,
        lambda: conventions_rules.rule_wide_callers_untested(
            rd.funcs, rd.coverage, min_dirs=rd.min_dirs, control_ok=rd.control_ok
        ),
    )
    return out


def _error_code_rule(rd: Reads, diff: cv.Diff) -> cv.RuleResult:
    r = cv.rule_error_code_declared(rd.uses, diff, rd.declared)
    if not rd.externs_exact and r.verdict in (cv.VIOLATED, cv.SATISFIED):
        r.epistemic = cv.LOWER_BOUND
        r.note = (
            r.note + "; " if r.note else ""
        ) + "declarations read from the index's chunks (a floor)"
    return r


async def handle_code_conventions(input: Any) -> Any:
    require_code_intel()
    started = time.monotonic()
    org, ns = resolve_code_identity(input.org, input.namespace)
    if not (input.pr_number or input.head_sha.strip() or input.files or input.diff):
        # after the gate, so a refused principal never learns the input shape
        raise ValidationError("pr_number, head_sha, or files/diff is required")
    repo = CodeRepo()
    cfg = repo_for_namespace(org, ns)
    wanted = _wanted(input)
    with code_span("conventions.source", seeds=1):
        src = await resolve_source(repo, org, ns, cfg, input)
    rd = await _gather(repo, org, ns, cfg, src, wanted, bool(input.diff))
    rules = _evaluate(src, rd, wanted)
    summ = cv.summary(rules)
    verdict = (
        "violated" if summ.get(cv.VIOLATED) else ("unusual" if summ.get(cv.UNUSUAL) else "clean")
    )
    trace_run_id = new_trace_run_id()
    schedule_code_trace(
        org=org,
        ns=ns,
        op="code.conventions",
        query=f"pr:{src.pr_number}" if src.pr_number else "diff",
        route="conventions",
        returned_ids=[r.rule for r in rules if r.verdict == cv.VIOLATED],
        returned_paths=src.files[:20],
        returned_scores=[],
        top_k=len(rules),
        tokens_returned=0,
        abstain=not rd.diff_known,
        retrieval_risk="low",
        embedding_model="",
        started_monotonic=started,
        trace_run_id=trace_run_id,
        consumer=input.consumer,
        agent=input.agent,
        user_id=input.user_id,
        verdict=verdict,
    )
    set_code_span_attributes(
        route="conventions", results=summ.get(cv.VIOLATED, 0), consumer=input.consumer
    )
    return CodeConventionsOutput(
        pr_number=src.pr_number,
        source=src.source,
        title=src.title,
        labels=src.labels,
        category=rd.category,
        category_source=rd.cat_source,
        merged_at=src.merged_at,
        merge_commit_sha=src.merge_sha,
        head_sha=src.post_sha if src.source == "head_sha" else src.head_sha,
        files_total=len(src.files),
        files_in_diff=len(src.diff.files),
        diff_bytes=src.diff.bytes,
        diff_truncated=src.diff.truncated,
        rules=[r.as_dict() for r in rules],
        summary=summ,
        control={
            "coverage_symbol": str(settings.code_coverage_control_symbol),
            "ok": rd.control_ok,
            "ran": rd.control_ran,
        },
        notes=src.notes,
        trace_run_id=trace_run_id,
    )


def _unknown(rule: str) -> cv.RuleResult:
    return cv.RuleResult(
        rule,
        verdict=cv.UNKNOWN,
        epistemic=cv.LOWER_BOUND,
        note="no diff: the PR is not in the ledger, or the mirror could not serve its commit",
    )


async def _pair() -> tuple[dict[tuple[str, str], bool | None], bool]:
    return {}, True


async def _empty_dict() -> dict[str, dict[str, int] | None]:
    return {}


async def _triple() -> tuple[
    list[conventions_rules.TouchedFunction], dict[str, tuple[str, int]], bool, bool
]:
    return [], {}, False, False
