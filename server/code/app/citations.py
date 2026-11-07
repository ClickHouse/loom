"""code.verify_citations: every citation a draft carries resolves, or the draft says which
did not.

The citations are extracted by shape (`server/code/domain/index/citations`)
and verified with keyed reads only: the file row of every cited path
(exists; `loc` bounds the line), the symbol and chunk spans of the paths
cited with a line (is the line inside a known unit — a floor), the items
ledger for every `#N` (state, title), one name lookup per qualified name
(0 / 1 / several definitions). An optional mirror leg (`at_commit`) reads
the bare clone through `gitrepo`'s bounded runner: one `rev-parse`, one
`diff --name-only commit..head` over the cited paths, then `git show` per
path until 20 calls or 5 s are spent — never a fetch, never GitHub.

When the addressed namespace is a release branch, unresolved citations are
retried in the mirror's master namespace (`base_namespace`), where the
PR / issue ledger and the master index live.

A bare function name (`sumMap`, `getClientExitCode`) that no symbol row carries is
retried as the class a factory registration names
(`FunctionSumMap`, `AggregateFunctionSumMap`, …) and then as the quoted
registration string in the mirror's registration trees (`git grep -F
'"sumMap"'`); with `pr_number`, the PR's merge commit (ledger, else the
mirror's first-parent log) is the default `at_commit`, and one `-U0` diff of
it says per citation whether the cited lines sit inside the PR's hunks
(`in_pr_diff`).
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any

from server.code.api.models_verify import CodeVerifyCitationsOutput
from server.code.app.common import (
    index_as_of,
    new_trace_run_id,
    path_coverage,
    require_code_intel,
    resolve_code_identity,
    schedule_code_trace,
)
from server.code.app.index import gitrepo
from server.code.app.index import pr_overlay as po
from server.code.app.index.mirrors import history_dir
from server.code.app.index.sql_names import combinator_stems, registration_classes
from server.code.app.spans import code_span, set_code_span_attributes, span_attrs
from server.code.domain.index import citations as cit
from server.code.domain.index.blame_range import sha_text
from server.code.domain.index.repos import default_git_dir
from server.code.store.index import CodeRepo
from server.settings import settings

logger = logging.getLogger(__name__)

NAME_CONCURRENCY = 8
MAX_CANDIDATES = 5
EPISTEMIC: dict[str, str] = {
    "path": "exact",
    "number": "exact",
    "name": "exact",
    "line": "lower_bound",
    "commit": "exact",
}
# rides on the per-check map only when a PR-diff leg ran
PR_DIFF_EPISTEMIC = "exact"
_RETRY_REASONS = frozenset({"path_not_indexed", "number_unknown", "name_unknown"})
# Where the quoted registration string lives; a literal grep of the mirror at
# the index head, bounded like the at_commit leg.
REGISTRATION_PATHSPECS = (
    "src/Functions",
    "src/AggregateFunctions",
    "src/TableFunctions",
    "src/Storages",
    "src/DataTypes",
    "src/Formats",
    "src/Processors/Formats",
    "src/Dictionaries",
)
REGISTRATION_GREP_CAP = 8
PR_LOG_CANDIDATES = 5
# Aggregate-function combinators and the registration-class convention live in
# `code/app/index/sql_names.py`, shared with code.coverage_claim and the symbol ops'
# registration rule.


ERROR_CODES_SCOPES = ("ErrorCodes::", "DB::ErrorCodes::")
ERROR_CODES_PATH = "src/Common/ErrorCodes.cpp"


@dataclass
class _Index:
    namespace: str
    files: dict[str, dict[str, Any]] = field(default_factory=dict)
    spans: dict[str, list[dict[str, Any]]] = field(default_factory=dict)  # symbol rows per path
    chunks: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    items: dict[int, dict[str, Any]] = field(default_factory=dict)
    names: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    # cited partial path -> the one indexed path ending in it / the several
    alias: dict[str, str] = field(default_factory=dict)
    suffix_multi: dict[str, list[str]] = field(default_factory=dict)
    # cited name -> the shorter suffix (or the registration class) that found definitions,
    # and how: qualifier_mismatch | registration_class
    name_as: dict[str, str] = field(default_factory=dict)
    name_via: dict[str, str] = field(default_factory=dict)
    error_codes: set[str] = field(default_factory=set)


# Each retry is one lookup, and a cited name has no length bound of its own.
MAX_QUALIFIER_RETRIES = 8  # not a setting: deeper than any real C++ name's qualifiers


def _shorter(name: str) -> list[str]:
    """`A::B::C` -> ["B::C", "C"]: the qualifiers a draft may have got
    wrong, longest first (a static free function cited as a method), under
    the last MAX_QUALIFIER_RETRIES qualifiers only."""
    parts = name.split("::")
    first = max(1, len(parts) - MAX_QUALIFIER_RETRIES)
    return ["::".join(parts[i:]) for i in range(first, len(parts))]


def _is_error_code(name: str) -> bool:
    return name.startswith(ERROR_CODES_SCOPES) and name.count("::") == name.startswith("DB::") + 1


def _is_merge_subject(subject: str, pr_number: int) -> bool:
    subject = subject.strip()
    merge, squash = f"Merge pull request #{pr_number} ", f"(#{pr_number})"
    return subject.startswith(merge) or subject.endswith(squash)


def _registration_paths(name: str, paths: list[str]) -> list[str]:
    """The registration files among a grep's hits: test sources are out, and
    when several remain, the ones named after the function win
    (`AggregateFunctionSumMap.cpp` over a data-type file that quotes it)."""
    noise = ("/tests/", "/fuzzers/", "/examples/")
    live = [p for p in paths if not any(n in p for n in noise)]
    named = [p for p in live if name.lower() in p.rsplit("/", 1)[-1].lower()]
    return named or live


# the registration-class names, re-exported for their callers and tests
_registration_classes = registration_classes
_combinator_stems = combinator_stems


async def _read_files(
    repo: CodeRepo, org: str, ns: str, idx: _Index, cits: list[cit.Citation]
) -> None:
    paths = list(dict.fromkeys(c.path for c in cits if c.path))
    idx.files = await repo.file_rows_for_paths(org, ns, paths)
    missing = [p for p in paths if p not in idx.files]
    if missing:
        hits = await repo.paths_by_suffix(org, ns, missing)
        for p, cands in hits.items():
            if len(cands) == 1:
                idx.alias[p] = cands[0]
            elif cands:
                idx.suffix_multi[p] = cands[:MAX_CANDIDATES]
        if idx.alias:
            idx.files.update(
                await repo.file_rows_for_paths(org, ns, list(dict.fromkeys(idx.alias.values())))
            )
    lined = {idx.alias.get(c.path, c.path) for c in cits if c.start_line}
    with_lines = [p for p in idx.files if p in lined]
    if with_lines:
        funcs, types, idx.chunks = await asyncio.gather(
            repo.functions_for_paths(org, ns, with_lines, limit=2000),
            repo.types_for_paths(org, ns, with_lines, limit=2000),
            repo.chunk_spans_for_paths(org, ns, with_lines),
        )
        for r in [*funcs, *types]:
            idx.spans.setdefault(str(r["path"]), []).append(r)


async def _read_names(
    repo: CodeRepo, org: str, ns: str, idx: _Index, cits: list[cit.Citation]
) -> None:
    from server.code.app.message_grep import error_code_names

    names = list(dict.fromkeys(c.name for c in cits if c.kind == "name" and c.name))
    bare = {c.name for c in cits if c.kind == "name" and c.form == "bare_name"}
    if any(_is_error_code(n) for n in names):
        try:
            idx.error_codes = await error_code_names(repo, org, ns)
        except Exception as e:  # noqa: BLE001 — the table is optional; its names stay unresolved
            logger.info("verify_citations: ErrorCodes table unread: %s", e)
    sem = asyncio.Semaphore(NAME_CONCURRENCY)

    async def lookup(name: str) -> list[dict[str, Any]]:
        async with sem:
            try:
                return await repo.symbols_by_name(org, ns, name, limit=20)
            except Exception as e:  # noqa: BLE001 — one name's failure is one unresolved
                logger.info("verify_citations: name lookup failed for %s: %s", name, e)
                return []

    async def one(name: str) -> None:
        if _is_error_code(name):
            return
        rows = await lookup(name)
        for shorter in [] if rows else _shorter(name):
            rows = await lookup(shorter)
            if rows:
                idx.name_as[name], idx.name_via[name] = shorter, "qualifier_mismatch"
                break
        for cls in [] if rows or name not in bare else _registration_classes(name):
            rows = await lookup(cls)
            if rows:
                idx.name_as[name], idx.name_via[name] = cls, "registration_class"
                break
        stems = [] if rows or name not in bare else _combinator_stems(name)
        for cls in (c for stem in stems for c in _registration_classes(stem)):
            rows = await lookup(cls)
            if rows:
                idx.name_as[name], idx.name_via[name] = cls, "registration_combinator"
                break
        idx.names[name] = rows

    await asyncio.gather(*(one(n) for n in names))


def _namespaces(org: str, ns: str, base: str) -> list[str]:
    """The addressed namespace, then the base one when it differs: the
    given `base_namespace`, else the mirror's trunk entry when the addressed
    entry is a release branch."""
    from server.code.domain.index.repos import configured_repos, trunk_entry

    out = [ns]
    if base and base != ns:
        out.append(base)
        return out
    cfgs = configured_repos()
    mine = next((c for c in cfgs if c.org == org and c.namespace == ns), None)
    trunk = trunk_entry(mine, cfgs) if mine is not None else None
    if trunk is not None:
        out.append(trunk.namespace)
    return out


async def _readable_namespaces(org: str, ns: str, base: str) -> list[str]:
    """`_namespaces`, authorized: the transport gated only `namespace`, so a
    `base_namespace` the caller named must be readable (refused otherwise, fail
    closed), and the master entry LOOM adds on its own is read only when the
    caller may read it (left out otherwise)."""
    from server.code.app import search_fanout as fanout

    namespaces = _namespaces(org, ns, base)
    extra = namespaces[1:]
    if not extra:
        return namespaces
    if base and base != ns:
        await fanout.authorize(org, extra, op="code.verify_citations")
        return namespaces
    readable, _skipped = await fanout.readable_subset(org, extra)
    return [ns, *readable]


async def _read_ns(repo: CodeRepo, org: str, ns: str, cits: list[cit.Citation]) -> _Index:
    idx = _Index(namespace=ns)
    numbers = sorted({c.number for c in cits if c.kind == "number" and c.number})

    async def items() -> None:
        for i in range(0, len(numbers), 64):
            idx.items.update(await repo.items_by_number(org, ns, numbers[i : i + 64]))

    await asyncio.gather(
        _read_files(repo, org, ns, idx, cits), items(), _read_names(repo, org, ns, idx, cits)
    )
    return idx


def _judge_path(c: cit.Citation, idx: _Index, cfg: Any) -> None:
    c.epistemic = EPISTEMIC["path"]
    if c.path in idx.suffix_multi:
        c.verdict, c.reason = "ambiguous", "several_files"
        c.candidates = [{"path": p, "namespace": idx.namespace} for p in idx.suffix_multi[c.path]]
        c.target = {"files": len(idx.suffix_multi[c.path]), "namespace": idx.namespace}
        return
    real = idx.alias.get(c.path, c.path)
    row = idx.files.get(real)
    if row is None:
        covered, _prefixes = path_coverage(cfg, c.path)
        c.verdict, c.reason = "unresolved", ("path_not_indexed" if covered else "not_covered")
        return
    target = {
        "path": real,
        "lang": row["lang"],
        "loc": row["loc"],
        "indexed_commit": row["indexed_commit"],
        "namespace": idx.namespace,
    }
    partial = ""
    if real != c.path:
        target["matched_by"] = "basename" if c.form == "basename" else "suffix"
        partial = "partial_path"
        c.path = real
    elif c.form == "line_ref":
        target["matched_by"] = "line_ref"
    if c.kind == "path":
        c.verdict, c.reason, c.target = "resolved", partial, target
        return
    if row["loc"] and c.start_line > row["loc"]:
        c.verdict, c.reason, c.target = "stale", "line_beyond_eof", target
        return
    c.epistemic = EPISTEMIC["line"]
    sym = cit.inside(idx.spans.get(c.path, []), c.start_line)
    if sym is not None:
        c.verdict, c.reason = "resolved", partial
        end = int(sym.get("end_line") or 0)
        c.target = {
            **target,
            "symbol": str(sym["qualified_name"]),
            "kind": str(sym.get("kind") or ""),
            "tier": str(sym.get("tier") or ""),
            "start_line": int(sym.get("start_line") or 0),
            "end_line": end,
            "span_covers_range": end >= (c.end_line or c.start_line),
        }
        return
    unit = cit.inside(idx.chunks.get(c.path, []), c.start_line)
    if unit is not None:
        c.verdict, c.reason = "resolved", partial
        c.target = {
            **target,
            "unit": unit["kind"],
            "scope": unit["scope"],
            "start_line": unit["start_line"],
            "end_line": unit["end_line"],
            "span_covers_range": unit["end_line"] >= (c.end_line or c.start_line),
        }
        return
    c.verdict, c.reason, c.target = "stale", "line_outside_units", target
    near, gap = cit.nearest([*idx.spans.get(c.path, []), *idx.chunks.get(c.path, [])], c.start_line)
    if near is not None:
        c.target["nearest_unit"] = {
            "start_line": int(near.get("start_line") or 0),
            "end_line": int(near.get("end_line") or 0),
            "name": str(near.get("qualified_name") or near.get("scope") or near.get("kind") or ""),
        }
        c.target["gap_lines"] = gap


def _judge_number(c: cit.Citation, idx: _Index) -> None:
    c.epistemic = EPISTEMIC["number"]
    item = idx.items.get(c.number)
    if item is None:
        c.verdict, c.reason = "unresolved", "number_unknown"
        return
    c.verdict, c.reason = "resolved", ""
    c.target = {
        "number": c.number,
        "is_pr": bool(item.get("is_pr")),
        "state": str(item.get("state") or ""),
        "state_reason": str(item.get("state_reason") or ""),
        "title": str(item.get("title") or ""),
        "merged": item.get("merged_at") is not None,
        "namespace": idx.namespace,
    }


def _judge_name(c: cit.Citation, idx: _Index) -> None:
    c.epistemic = EPISTEMIC["name"]
    if _is_error_code(c.name):
        bare = c.name.rsplit("::", 1)[-1]
        if bare in idx.error_codes:
            c.verdict, c.reason = "resolved", ""
            c.target = {
                "qualified_name": f"DB::ErrorCodes::{bare}",
                "path": ERROR_CODES_PATH,
                "kind": "error_code",
                "namespace": idx.namespace,
            }
        else:
            c.verdict, c.reason = "unresolved", "name_unknown"
        return
    mismatch = idx.name_via.get(c.name, "") if c.name in idx.name_as else ""
    defs: dict[str, dict[str, Any]] = {}
    for r in idx.names.get(c.name, []):
        q = str(r.get("qualified_name") or "")
        if q and q not in defs:
            defs[q] = {
                "qualified_name": q,
                "path": str(r.get("path") or ""),
                "kind": str(r.get("kind") or ""),
                "tier": str(r.get("tier") or ""),
                "start_line": int(r.get("start_line") or 0),
                "end_line": int(r.get("end_line") or 0),
                "namespace": idx.namespace,
            }
    if not defs:
        c.verdict, c.reason = "unresolved", "name_unknown"
    elif len(defs) == 1:
        c.verdict, c.reason, c.target = "resolved", mismatch, next(iter(defs.values()))
    else:
        c.verdict, c.reason = "ambiguous", (mismatch or "several_definitions")
        c.candidates = list(defs.values())[:MAX_CANDIDATES]
        c.target = {"definitions": len(defs), "namespace": idx.namespace}
    if mismatch:
        c.target = {**c.target, "matched_as": idx.name_as[c.name]}


def _judge(cits: list[cit.Citation], idx: _Index, cfg: Any) -> None:
    for c in cits:
        if c.kind in ("path", "location"):
            _judge_path(c, idx, cfg)
        elif c.kind == "number":
            _judge_number(c, idx)
        else:
            _judge_name(c, idx)


def _pending(cits: list[cit.Citation]) -> list[cit.Citation]:
    return [c for c in cits if c.verdict == "unresolved" and c.reason in _RETRY_REASONS]


class _GitBudget:
    """At most `code_citations_git_call_cap` subprocesses and `code_citations_git_budget_s`
    of wall time per request."""

    def __init__(self) -> None:
        self.calls = 0
        self.t0 = time.monotonic()
        self.capped = False

    def take(self) -> bool:
        if (
            self.calls >= settings.code_citations_git_call_cap
            or time.monotonic() - self.t0 > settings.code_citations_git_budget_s
        ):
            self.capped = True
            return False
        self.calls += 1
        return True

    def remaining_s(self) -> float:
        return max(
            0.2,
            min(
                settings.code_citations_git_call_timeout_s,
                settings.code_citations_git_budget_s - (time.monotonic() - self.t0),
            ),
        )


def _count_lines(blob: bytes) -> int:
    if not blob:
        return 0
    return blob.count(b"\n") + (0 if blob.endswith(b"\n") else 1)


def _apply_commit(c: cit.Citation, *, lines: int | None, changed: bool) -> None:
    """Fold one path's state at `at_commit` into a citation's verdict."""
    at: dict[str, Any] = {
        "existed": lines is not None,
        "lines": lines or 0,
        "changed_since": changed,
    }
    if lines is None:
        if c.verdict != "unresolved":
            c.verdict, c.reason = "stale", "path_absent_at_commit"
        c.target = {**c.target, "at_commit": at}
        return
    if c.start_line and c.start_line > lines:
        c.verdict, c.reason = "stale", "line_beyond_eof_at_commit"
    elif c.verdict == "unresolved" and c.reason == "path_not_indexed" and changed:
        # existed at the commit, touched or deleted since, absent from the
        # index now; untouched since and absent means the index does not
        # hold that file type — that stays path_not_indexed
        c.verdict, c.reason = "stale", "path_changed_or_removed_since_commit"
    elif c.kind == "location" and changed and c.verdict == "resolved":
        c.verdict, c.reason = "stale", "file_changed_since_commit"
    c.target = {**c.target, "at_commit": at}


async def _mirror_leg(
    git_dir: str,
    requested: str,
    head: str,
    cits: list[cit.Citation],
    budget: _GitBudget | None = None,
) -> dict[str, Any]:
    """Read-only, bounded: rev-parse, one diff --name-only over the cited
    paths, git show per path while the budget lasts. `budget` is shared with
    the PR-diff and registration legs when a request runs several."""
    budget = budget or _GitBudget()
    out: dict[str, Any] = {
        "requested": requested,
        "sha": "",
        "head": head,
        "known": False,
        "calls": 0,
        "capped": False,
        "ms": 0,
        "error": "",
    }
    try:
        budget.take()
        sha = await gitrepo.resolve_commit(git_dir, requested)
    except (ValueError, gitrepo.GitError) as e:
        out["error"] = "unknown_commit" if isinstance(e, ValueError) else "git_error"
        out["calls"], out["ms"] = budget.calls, int((time.monotonic() - budget.t0) * 1000)
        return out
    out["sha"], out["known"] = sha, True
    paths = list(
        dict.fromkeys(c.path for c in cits if c.path and c.verdict and c.reason != "not_covered")
    )
    # a resolved name's definition file rides in the diff only: whether it
    # changed since the commit decides its in_pr_diff
    name_paths = [
        p
        for p in dict.fromkeys(_path_of(c) for c in cits if c.kind == "name")
        if p and p not in paths
    ]
    changed: set[str] = set()
    if (paths or name_paths) and head and head != sha and budget.take():
        try:
            text = await gitrepo.run(
                git_dir,
                "diff",
                "--name-only",
                sha,
                head,
                "--",
                *[*paths, *name_paths][:200],
                timeout_s=budget.remaining_s(),
            )
            changed = {ln.strip() for ln in text.splitlines() if ln.strip()}
        except gitrepo.GitError as e:
            out["error"] = "diff_failed"
            logger.info("verify_citations: diff failed: %s", str(e)[:200])
    for c in cits:
        if c.kind == "name" and _path_of(c) in changed:
            c.target = {**c.target, "at_commit": {"changed_since": True}}
    for path in paths:
        mine = [c for c in cits if c.path == path]
        if not budget.take():
            for c in mine:
                c.target = {**c.target, "at_commit": {"checked": False}}
            continue
        try:
            blob = await gitrepo.show_file(git_dir, sha, path)
        except gitrepo.GitError:
            blob = None
        lines = None if blob is None else _count_lines(blob)
        for c in mine:
            _apply_commit(c, lines=lines, changed=path in changed)
    out["calls"], out["capped"] = budget.calls, budget.capped
    out["ms"] = int((time.monotonic() - budget.t0) * 1000)
    return out


def _no_mirror(leg: dict[str, Any]) -> bool:
    """The leg did not run: this host has no mirror to run it on (`mirrors.history_dir`)."""
    return str(leg.get("error") or "").startswith(("no_git_mirror", "no_git_history"))


def _note(ex: cit.Extraction, at: dict[str, Any], pr: dict[str, Any] | None = None) -> str:
    parts = [
        "path, number and name checks are exact against the index head; "
        "'line inside a unit' is a lower bound (symbol and chunk spans are floors)",
    ]
    if pr and _no_mirror(pr):
        parts.append(f"{pr['error']} and the request sent no pr_diff; in_pr_diff is unknown")
    elif pr and pr.get("source") == "posted":
        parts.append("in_pr_diff compares cited lines with the hunks of the pr_diff sent")
    elif pr and not pr.get("merge_sha"):
        parts.append(
            f"PR {pr.get('pr_number')} is in neither the ledger nor the mirror's first-parent "
            "log; in_pr_diff is unknown"
        )
    elif pr:
        parts.append(
            "in_pr_diff compares cited lines with the merge commit's -U0 hunks; a name's "
            "definition span is read at the index head, so it answers unknown when its file "
            "changed since the merge and no hunk meets the span"
        )
    if _no_mirror(at):
        parts.append(
            f"at_commit {at['requested']!r} was not checked ({at['error']}); head checks only"
        )
    elif at.get("requested") and not at.get("known"):
        parts.append(
            f"at_commit {at['requested']!r} is not a commit the mirror knows; head checks only"
        )
    if at.get("capped"):
        parts.append(
            f"mirror leg capped at {settings.code_citations_git_call_cap} calls / "
            f"{settings.code_citations_git_budget_s:.0f} s; "
            "unchecked paths say checked: false"
        )
    if ex.truncated:
        parts.append("more citations than `limit`; the rest were not verified")
    return "; ".join(parts)


async def _judge_line_refs(
    repo: CodeRepo, org: str, namespaces: list[str], cfg: Any, ex: cit.Extraction
) -> None:
    """`line N` references take the path of the citation before them once
    that one has resolved, then read and judge like any location — one more
    keyed round over just those paths."""
    pending = cit.bind_line_refs(ex.citations, ex.skipped)
    for ns_i in namespaces:
        if not pending:
            return
        with code_span("verify_citations.line_refs", namespace=ns_i, seeds=len(pending)) as sp:
            idx = await _read_ns(repo, org, ns_i, pending)
            span_attrs(sp, results=len(idx.files))
        _judge(pending, idx, cfg)
        pending = _pending(pending)


async def _merge_sha_of(
    repo: CodeRepo,
    org: str,
    namespaces: list[str],
    git_dir: str,
    head: str,
    pr_number: int,
    budget: _GitBudget,
) -> dict[str, Any]:
    """The PR's merge commit: the ledger of each namespace in turn, else the
    newest first-parent commit on the index head whose subject is GitHub's
    merge (`Merge pull request #N from …`) or squash (`… (#N)`) of it — one
    bounded `git log -F --grep`. `source` says which answered."""
    out = _pr_summary(pr_number)
    for ns_i in namespaces:
        try:
            facts = await repo.pr_merge_facts(org, ns_i, pr_number=pr_number)
        except Exception as e:  # noqa: BLE001 — a ledger read failure is a miss
            logger.info("verify_citations: pr_merge_facts failed for %s: %s", ns_i, e)
            facts = None
        sha = sha_text(facts.get("merge_sha")) if facts else ""
        if sha:
            out["merge_sha"], out["source"] = sha, "ledger"
            return out
    if git_dir and head and budget.take():
        try:
            text = await gitrepo.run(
                git_dir,
                "log",
                head,
                "--first-parent",
                f"-{PR_LOG_CANDIDATES}",
                "--format=%H %s",
                "-F",
                f"--grep=#{pr_number}",
                timeout_s=budget.remaining_s(),
            )
            for ln in text.splitlines():
                sha, _, subject = ln.strip().partition(" ")
                if _is_merge_subject(subject, pr_number):
                    out["merge_sha"], out["source"] = sha, "mirror_log"
                    break
        except gitrepo.GitError as e:
            out["error"] = "log_failed"
            logger.info("verify_citations: first-parent log failed: %s", str(e)[:200])
    if not out["merge_sha"] and not out["error"]:
        out["error"] = "pr_unknown"
    return out


async def _registration_leg(
    git_dir: str, rev: str, ns: str, cits: list[cit.Citation], budget: _GitBudget
) -> None:
    """A bare name still unknown after every namespace, looked for as its
    quoted registration string in the mirror's registration trees — one
    bounded `git grep -l -F` per name, at most REGISTRATION_GREP_CAP names."""
    names = [
        c for c in cits if c.kind == "name" and c.form == "bare_name" and c.verdict == "unresolved"
    ]
    if not names or not git_dir or not rev:
        return

    async def grep(name: str) -> list[str]:
        if not budget.take():
            return []
        try:
            paths = await gitrepo.grep_paths(
                git_dir,
                rev,
                f'"{name}"',
                REGISTRATION_PATHSPECS,
                limit=MAX_CANDIDATES + 1,
                timeout_s=budget.remaining_s(),
            )
        except gitrepo.GitError as e:
            logger.info("verify_citations: registration grep failed for %s: %s", name, e)
            return []
        return _registration_paths(name, paths)

    async def one(c: cit.Citation) -> None:
        paths: list[str] = []
        matched = ""
        for literal in [c.name, *_combinator_stems(c.name)]:
            paths = await grep(literal)
            if paths:
                matched = literal
                break
        if not paths:
            return
        target: dict[str, Any] = {"matched_by": "registration_string", "namespace": ns}
        if matched != c.name:
            target["matched_as"] = matched
        if len(paths) == 1:
            c.verdict, c.reason = "resolved", "registration_string"
            c.target = {"path": paths[0], **target}
            return
        c.verdict, c.reason = "ambiguous", "registration_string"
        c.candidates = [{"path": p, "namespace": ns} for p in paths[:MAX_CANDIDATES]]
        c.target = {"files": len(paths), **target}

    await asyncio.gather(*(one(c) for c in names[:REGISTRATION_GREP_CAP]))


def _path_of(c: cit.Citation) -> str:
    """The one indexed path a citation stands for, '' when there is none to
    check: an ambiguous basename, a basename no file ends in, an ambiguous
    or unknown name."""
    if c.kind in ("path", "location"):
        if c.verdict == "ambiguous" or (c.form == "basename" and c.verdict == "unresolved"):
            return ""
        return c.path
    if c.kind == "name" and c.verdict == "resolved":
        return str(c.target.get("path") or "")
    return ""


def _in_pr_diff(
    c: cit.Citation, files: set[str] | None, hunks: dict[str, list[tuple[int, int]]] | None
) -> str:
    """true | false | unknown for one citation against the PR's file list
    and new-side hunks. A path is inside when the PR touched the file; a
    location when a hunk meets its lines; a resolved name when a hunk meets
    its definition span (read at head: unknown when the file changed since
    the merge and no hunk meets it)."""
    if c.kind == "number":
        return ""
    path = _path_of(c)
    if files is None or not path:
        return "unknown"
    if path not in files:
        return "false"
    if c.kind == "path":
        return "true"
    if hunks is None:
        return "unknown"
    ranges = hunks.get(path)
    if ranges is None:
        return "unknown"
    if c.kind == "location":
        return "true" if cit.in_ranges(ranges, c.start_line, c.end_line) else "false"
    start, end = int(c.target.get("start_line") or 0), int(c.target.get("end_line") or 0)
    if not start:
        return "unknown"
    if cit.in_ranges(ranges, start, end):
        return "true"
    changed = bool((c.target.get("at_commit") or {}).get("changed_since"))
    return "unknown" if changed else "false"


async def _pr_diff_leg(
    git_dir: str, pr: dict[str, Any], cits: list[cit.Citation], budget: _GitBudget
) -> None:
    """Two bounded git calls on the merge commit: its file list, then a -U0
    diff restricted to the cited files; every non-number citation gets
    `in_pr_diff` and the summary counts them."""
    t0, calls0 = time.monotonic(), budget.calls
    sha = str(pr.get("merge_sha") or "")
    files: set[str] | None = None
    hunks: dict[str, list[tuple[int, int]]] | None = None
    if sha and git_dir and budget.take():
        try:
            text = await gitrepo.run(
                git_dir,
                "diff",
                "--name-only",
                "--no-renames",
                f"{sha}^1",
                sha,
                timeout_s=budget.remaining_s(),
            )
            files = {cit.normalise_path(ln) for ln in text.splitlines() if ln.strip()}
        except gitrepo.GitError as e:
            pr["error"] = "diff_failed"
            logger.info("verify_citations: PR diff --name-only failed: %s", str(e)[:200])
    cited = [p for p in dict.fromkeys(_path_of(c) for c in cits) if p and files and p in files]
    if cited and budget.take():
        try:
            text = await gitrepo.run(
                git_dir,
                "diff",
                "-U0",
                "--no-renames",
                f"{sha}^1",
                sha,
                "--",
                *cited[:200],
                timeout_s=budget.remaining_s(),
            )
            hunks = cit.parse_hunks(text)
        except gitrepo.GitError as e:
            pr["error"] = "hunks_failed"
            logger.info("verify_citations: PR -U0 diff failed: %s", str(e)[:200])
    elif files is not None:
        hunks = {}
    for c in cits:
        c.in_pr_diff = _in_pr_diff(c, files, hunks)
        key = {"true": "in_diff", "false": "off_diff", "unknown": "unknown"}.get(c.in_pr_diff)
        if key:
            pr[key] += 1
    pr["files_in_diff"] = len(files or ())
    pr["calls"] += budget.calls - calls0
    pr["ms"] += int((time.monotonic() - t0) * 1000)


def _pr_summary(pr_number: int) -> dict[str, Any]:
    """The `pr_diff` block before any leg ran."""
    return {
        "pr_number": pr_number,
        "merge_sha": "",
        "source": "",
        "files_in_diff": 0,
        "in_diff": 0,
        "off_diff": 0,
        "unknown": 0,
        "calls": 0,
        "ms": 0,
        "error": "",
    }


def _posted_pr_leg(img: po.PostImage, cits: list[cit.Citation]) -> dict[str, Any]:
    """The PR-diff leg on the diff the caller sent: its files and -U0 hunks
    decide every non-number citation's `in_pr_diff`, as the merge commit's do."""
    pr = {**_pr_summary(img.pr_number), "source": "posted"}
    files = {cit.normalise_path(p) for p in po.diff_paths(img.diff)}
    hunks = cit.parse_hunks(img.diff)
    for c in cits:
        c.in_pr_diff = _in_pr_diff(c, files, hunks)
        key = {"true": "in_diff", "false": "off_diff", "unknown": "unknown"}.get(c.in_pr_diff)
        if key:
            pr[key] += 1
    pr["files_in_diff"] = len(files)
    return pr


async def _git_legs(
    repo: CodeRepo,
    org: str,
    ns: str,
    namespaces: list[str],
    cfg: Any,
    input: Any,
    as_of: dict[str, Any],
    cits: list[cit.Citation],
) -> tuple[dict[str, Any], dict[str, Any]]:
    """The mirror reads a request may run, on one shared budget: the PR's
    merge commit (which becomes the default `at_commit`), the at_commit
    leg, the registration-string grep, the PR-diff leg. (at_commit, pr_diff)."""
    git_dir, gap = await history_dir(cfg) if cfg is not None else (default_git_dir(), "")
    head = str(as_of.get("commit") or "")
    budget = _GitBudget()
    pr: dict[str, Any] = {}
    at_commit = input.at_commit.strip()
    pr_number = int(getattr(input, "pr_number", 0) or 0)
    img = po.posted(pr_number) if pr_number else None
    if img is not None:
        # The caller sent the PR's diff: in_pr_diff reads it, and no git is.
        with code_span("verify_citations.pr_diff") as sp:
            pr = _posted_pr_leg(img, cits)
            span_attrs(sp, results=pr.get("in_diff", 0))
    elif pr_number and not gap:
        pr = await _merge_sha_of(repo, org, namespaces, git_dir, head, pr_number, budget)
        at_commit = at_commit or str(pr.get("merge_sha") or "")
    elif pr_number:
        pr = {**_pr_summary(pr_number), "error": gap}
    at: dict[str, Any] = {}
    if gap:
        # No mirror here (yet): the commit and registration legs are not run, and say so.
        if at_commit:
            at = {"requested": at_commit, "known": False, "error": gap}
        return at, pr
    if at_commit:
        with code_span("verify_citations.mirror") as sp:
            at = await _mirror_leg(git_dir, at_commit, head, cits, budget=budget)
            span_attrs(sp, results=at.get("calls", 0))
    rev = head or (cfg.git_branch if cfg is not None else "") or settings.code_git_branch
    await _registration_leg(git_dir, rev, ns, cits, budget)
    if pr_number and img is None:
        with code_span("verify_citations.pr_diff") as sp:
            await _pr_diff_leg(git_dir, pr, cits, budget)
            span_attrs(sp, results=pr.get("in_diff", 0))
    if at:
        at["calls"], at["capped"] = budget.calls, budget.capped
    return at, pr


@po.reads_post_image
async def handle_code_verify_citations(input: Any) -> Any:
    from server.code.domain.index.repos import repo_for_namespace

    require_code_intel()
    started = time.monotonic()
    org, ns = resolve_code_identity(input.org, input.namespace)
    repo = CodeRepo()
    cfg = repo_for_namespace(org, ns)
    ex = cit.extract(input.text, limit=input.limit)
    namespaces = await _readable_namespaces(org, ns, input.base_namespace.strip())
    pending = list(ex.citations)
    for ns_i in namespaces:
        if not pending:
            break
        with code_span("verify_citations.reads", namespace=ns_i, seeds=len(pending)) as sp:
            idx = await _read_ns(repo, org, ns_i, pending)
            span_attrs(sp, results=len(idx.files) + len(idx.items) + len(idx.names))
        _judge(pending, idx, cfg)
        pending = _pending(pending)
    await _judge_line_refs(repo, org, namespaces, cfg, ex)
    ex.citations = cit.fold_shorthands(ex.citations)
    as_of = await index_as_of(repo, org, ns)
    at, pr = await _git_legs(repo, org, ns, namespaces, cfg, input, as_of, ex.citations)
    verdict = cit.top_verdict(ex.citations)
    counts = cit.counts_of(ex.citations)
    epistemic = {**EPISTEMIC, **({"in_pr_diff": PR_DIFF_EPISTEMIC} if pr else {})}
    paths = list(dict.fromkeys(c.path for c in ex.citations if c.path))
    trace_run_id = new_trace_run_id()
    schedule_code_trace(
        org=org,
        ns=ns,
        op="code.verify_citations",
        query=f"{len(ex.citations)} citations",
        route="verify_citations",
        returned_ids=[f"{c.kind}:{c.text}" for c in ex.citations][:50],
        returned_paths=paths[:50],
        returned_scores=[],
        top_k=input.limit,
        tokens_returned=0,
        abstain=verdict == "no_citations",
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
        route="verify_citations",
        verdict=verdict,
        results=counts["total"],
        seeds=counts["resolved"],
        abstain=verdict == "no_citations",
        consumer=input.consumer,
    )
    return CodeVerifyCitationsOutput(
        verdict=verdict,
        counts=counts,
        citations=[c.as_dict() for c in ex.citations],
        skipped=ex.skipped,
        truncated=ex.truncated,
        epistemic=epistemic,
        at_commit=at,
        pr_diff=pr,
        namespaces=namespaces,
        took_ms=int((time.monotonic() - started) * 1000),
        as_of=as_of,
        note=_note(ex, at, pr),
        trace_run_id=trace_run_id,
    )
