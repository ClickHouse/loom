"""The convention rules over a PR's tests, changelog and callers: a bug fix carries a
test, a stateless test's number and reference, the changelog category, and callers
of a widely called function that the PR leaves untested."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from server.code.domain.index.conventions import (
    LOWER_BOUND,
    NOT_APPLICABLE,
    SATISFIED,
    UNKNOWN,
    UNUSUAL,
    VIOLATED,
    Diff,
    RuleResult,
    ev,
    stem_of_stateless,
)

STATELESS = "tests/queries/0_stateless/"

# What counts as "a test" for the bug-fix rule: the query suites, the
# integration and performance trees, fuzzers, and the gtest units that live
# beside the source (`src/*/tests/gtest_*.cpp`). `tests/ci/` is CI
# infrastructure, not a test.
TEST_PATH_RE = re.compile(
    r"^(tests/(queries|integration|performance|fuzz|sqllogic)/|src/.*/tests/gtest_)"
)

# The backport bot's titles: the PR ledger of a repository holds every merged
# PR, release-branch backports included; a backport carries its original's
# fix and tests, so the per-PR conventions apply to the original, not to it.
# Searched, not anchored: a re-landed backport reads `Revert "Revert "Backport #N to X.Y: …`.
_BACKPORT_TITLE_RE = re.compile(r"\b(?:Backport|Cherry pick)\s+#(?P<n>\d+)\s+to\s+\d+\.\d+", re.I)

# Bug-fix signals, strongest first: the label, the changelog bullet, the title.
BUGFIX_LABELS = frozenset({"pr-bugfix", "pr-critical-bugfix"})

_BUGFIX_TITLE_RE = re.compile(r"(?i)\bfix(es|ed|ing)?\b")

# The changelog category of a PR from its `pr-*` label (the template's
# menu, as the bot labels it) — canonical names shared by rule 6.
LABEL_CATEGORY = {
    "pr-bugfix": "bug_fix",
    "pr-critical-bugfix": "bug_fix",
    "pr-improvement": "improvement",
    "pr-feature": "new_feature",
    "pr-performance": "performance",
    "pr-not-for-changelog": "not_for_changelog",
    "pr-build": "build",
    "pr-ci": "ci",
    "pr-documentation": "documentation",
    "pr-backward-incompatible": "backward_incompatible",
    "pr-experimental": "experimental",
}

_BULLET_CATEGORY = (
    ("not for changelog", "not_for_changelog"),
    ("critical bug fix", "bug_fix"),
    ("bug fix", "bug_fix"),
    ("performance improvement", "performance"),
    ("build/testing/packaging improvement", "build"),
    ("backward incompatible change", "backward_incompatible"),
    ("ci fix or improvement", "ci"),
    ("experimental feature", "experimental"),
    ("new feature", "new_feature"),
    ("documentation", "documentation"),
    ("improvement", "improvement"),
)

UNUSUAL_SHARE = 0.10


def is_test_path(path: str) -> bool:
    return bool(TEST_PATH_RE.match(path))


def backport_of(title: str) -> int:
    """The original PR number when `title` is the backport bot's, else 0."""
    m = _BACKPORT_TITLE_RE.search(title or "")
    return int(m.group("n")) if m else 0


def category_of(labels: list[str], body_excerpt: str) -> tuple[str, str]:
    """(category, source) — the `pr-*` label first, else the template's
    changelog bullet in the body; ('', '') when neither says."""
    for lab in labels or []:
        cat = LABEL_CATEGORY.get(str(lab).strip().lower())
        if cat:
            return cat, "label:" + str(lab)
    for line in (body_excerpt or "").splitlines():
        s = line.strip().lstrip("-*").strip().lower()
        if not s or len(s) > 100:
            continue
        for needle, cat in _BULLET_CATEGORY:
            if s.startswith(needle):
                return cat, "changelog_bullet"
    return "", ""


def bugfix_signal(title: str, labels: list[str], body_excerpt: str) -> str:
    """Which signal calls the PR a bug fix: `label`, `changelog_bullet`,
    `title`, or '' — the title alone is the weakest and is said so."""
    if any(str(lab).strip().lower() in BUGFIX_LABELS for lab in labels or []):
        return "label"
    cat, src = category_of(labels, body_excerpt)
    if cat == "bug_fix" and src == "changelog_bullet":
        return "changelog_bullet"
    if cat:
        return ""  # the PR declares another category; the title does not override it
    if _BUGFIX_TITLE_RE.search(title or ""):
        return "title"
    return ""


def off_branch_note(rule: RuleResult, off_branch: str, title: str) -> RuleResult:
    """The ledger says the PR merged into `off_branch`, not the namespace's branch: a
    cherry-pick step on the bot's staging branch, a hand-made backport whose title is
    not the bot's, a feature-branch merge. Not this branch's change — `not_applicable`,
    evidence names the branch so the analyst sees why."""
    rule.verdict = NOT_APPLICABLE
    rule.note = f"merged into {off_branch}, not this namespace's branch: not its change"
    rule.evidence = [ev("", 0, title, target_branch=off_branch)]
    return rule


def rule_bugfix_has_test(
    files: list[str],
    statuses: dict[str, str],
    title: str,
    labels: list[str],
    body_excerpt: str,
    off_branch: str = "",
) -> RuleResult:
    r = RuleResult("bugfix_has_test")
    original = backport_of(title)
    if original:
        r.verdict = NOT_APPLICABLE
        r.note = f"backport of #{original}: the convention applies to the original PR"
        r.evidence = [ev("", 0, title, backport_of=original)]
        return r
    if off_branch:
        return off_branch_note(r, off_branch, title)
    signal = bugfix_signal(title, labels, body_excerpt)
    if not signal:
        r.verdict, r.note = NOT_APPLICABLE, "not labelled, categorised or titled as a bug fix"
        return r
    tests = [p for p in files if is_test_path(p)]
    r.evidence.append(
        ev(
            "",
            0,
            title,
            signal=signal,
            labels=[lab for lab in labels if str(lab).startswith("pr-")][:6],
        )
    )
    if tests:
        r.verdict = SATISFIED
        r.evidence += [ev(p, 0, "", status=statuses.get(p, "M")) for p in tests[:12]]
        return r
    r.verdict = VIOLATED
    if signal == "title":
        r.epistemic = LOWER_BOUND
    r.evidence += [ev(p, 0, "", status=statuses.get(p, "M")) for p in files[:12]]
    r.note = (
        f"a bug fix ({signal}) touching {len(files)} file(s), none under tests/queries, "
        "tests/integration, tests/performance, tests/fuzz or src/**/tests/gtest_*"
    )
    return r


def rule_test_only_change(files: list[str]) -> RuleResult:
    r = RuleResult("test_only_change")
    if not files:
        r.verdict, r.note = NOT_APPLICABLE, "no files"
        return r
    non_test = [p for p in files if not is_test_path(p)]
    if non_test:
        r.verdict = NOT_APPLICABLE
        r.note = f"{len(non_test)} of {len(files)} file(s) are not tests"
        return r
    r.verdict = SATISFIED
    r.evidence = [ev(p, 0, "") for p in files[:12]]
    r.note = "only test files touched: no production code changed"
    return r


@dataclass
class NewStatelessTest:
    path: str
    number: str
    stem: str
    line: int = 0


def new_stateless_tests(files: list[str], statuses: dict[str, str]) -> list[NewStatelessTest]:
    """Stateless test files the diff ADDS (status A), one per stem, with a
    `.reference*` file standing for its stem too."""
    out: dict[str, NewStatelessTest] = {}
    for p in files:
        if statuses.get(p, "M") != "A":
            continue
        ns = stem_of_stateless(p)
        if ns is None:
            continue
        num, stem = ns
        # prefer the runnable file as the stem's representative
        if stem not in out or not out[stem].path.endswith((".sql", ".sh", ".sql.j2", ".python")):
            if not p.endswith((".reference", ".reference.j2")) or stem not in out:
                out[stem] = NewStatelessTest(p, num, stem)
    return list(out.values())


def _stateless_settled_early(
    r: RuleResult,
    new_tests: list[NewStatelessTest],
    index_paths: list[str] | None,
    title: str,
    off_branch: str,
) -> bool:
    """The verdicts that need no owner read: no new test, the backport bot's PR, a PR
    the ledger places on another branch, an index that could not be read. True when `r`
    is final."""
    if not new_tests:
        r.verdict, r.note = NOT_APPLICABLE, "no new stateless test"
        return True
    original = backport_of(title)
    if original:
        r.verdict = NOT_APPLICABLE
        r.note = f"backport of #{original}: its tests ride with the original's"
        r.evidence = [ev(t.path, 0, "", number=t.number, backport_of=original) for t in new_tests]
        return True
    if off_branch:
        off_branch_note(r, off_branch, title)
        r.evidence = [
            ev(t.path, 0, "", number=t.number, target_branch=off_branch) for t in new_tests
        ]
        return True
    if index_paths is None:
        r.verdict, r.note = UNKNOWN, "code_files could not be read"
        r.evidence = [ev(t.path, 0, "", number=t.number) for t in new_tests]
        return True
    return False


def rule_stateless_test_number(
    new_tests: list[NewStatelessTest],
    index_paths: list[str] | None,
    first_owner: dict[str, tuple[int, str]],
    *,
    merged_at: str,
    max_number: int,
    preexisting: set[str] | None = None,
    title: str = "",
    deleted: set[str] | None = None,
    off_branch: str = "",
) -> RuleResult:
    """A NAME collision: the tree the PR applies to already holds a file with the same
    stem (number and name) that an earlier merged PR added. Sharing a NUMBER is this
    repository's norm (57 PRs share 04652), so a shared number is reported as a fact on
    `satisfied`, never as the violation. `first_owner[path]` = (pr_number, merged_at) of
    the PR that first added the path; a path the ledger never saw is older than the
    ledger. `preexisting` = the same-stem index paths present in the PR's pre-image (its
    merge commit's first parent); None means the index IS the pre-image (an open PR
    against the branch tip), so every same-stem index path pre-exists. A name a reverted
    PR once used and this PR re-adds is therefore no collision; nor is a same-stem file
    this very diff deletes (`00746_x.sh` → `00746_x.python` is a conversion). A backport
    bot's PR is `not_applicable` outright, and so is a PR the ledger says merged into
    another branch (`off_branch`)."""
    r = RuleResult("stateless_test_number")
    gone = deleted or set()
    if _stateless_settled_early(r, new_tests, index_paths, title, off_branch):
        return r
    own_stems = {t.stem for t in new_tests}
    by_number: dict[str, set[str]] = {}
    same_stem: dict[str, list[str]] = {}
    for p in index_paths or []:
        ns = stem_of_stateless(p)
        if ns is None:
            continue
        num, stem = ns
        by_number.setdefault(num, set()).add(stem)
        if stem in own_stems:
            same_stem.setdefault(stem, []).append(p)
    collisions: list[dict[str, Any]] = []
    reused: list[dict[str, Any]] = []
    for t in new_tests:
        for p in same_stem.get(t.stem, []):
            owner = first_owner.get(p)
            earlier = owner is None or (bool(merged_at) and owner[1] < merged_at)
            if not earlier:
                continue
            row = ev(
                p,
                0,
                "",
                stem=t.stem,
                new_path=t.path,
                owner_pr=owner[0] if owner else 0,
                owner_merged_at=owner[1] if owner else "before_ledger",
            )
            if p in gone:
                row["deleted_in_diff"] = True
                reused.append(row)  # the PR's own rename or conversion
            elif preexisting is not None and p not in preexisting:
                row["in_pre_image"] = False
                reused.append(row)  # the name was used before, the file was gone (a revert)
            else:
                collisions.append(row)
    if collisions:
        r.verdict = VIOLATED
        r.evidence = collisions + reused
        r.note = (
            f"{len(collisions)} new test name(s) already exist in the tree the PR applies "
            "to, added by an earlier PR"
        )
        return r
    r.verdict = SATISFIED
    r.evidence = list(reused)
    for t in new_tests:
        others = len(by_number.get(t.number, set()) - own_stems)
        r.evidence.append(
            ev(
                t.path,
                0,
                "",
                number=t.number,
                other_stems_with_number=others,
                below_max_by=max(0, max_number - int(t.number)) if max_number else 0,
            )
        )
    r.note = "names unique; a shared number is the repository's norm and is reported, not flagged"
    if reused:
        r.note = (
            f"{len(reused)} same-name file(s) are this PR's own conversion or a name a "
            "reverted PR once used: no collision"
        )
    return r


def rule_reference_missing(
    files: list[str],
    statuses: dict[str, str],
    new_tests: list[NewStatelessTest],
) -> RuleResult:
    """A new `.sql` / `.sh` stateless test (also `.sql.j2`) without a
    `.reference` / `.reference.j2` sibling in the same diff. Reference files
    are not indexed, so this reads the diff only; a stem whose other file
    the diff deletes (a `.sh` → `.sql` conversion) keeps its old reference
    and is `not_applicable`."""
    r = RuleResult("reference_missing")
    runnable = [t for t in new_tests if t.path.endswith((".sql", ".sh", ".sql.j2"))]
    if not runnable:
        r.verdict, r.note = NOT_APPLICABLE, "no new .sql/.sh stateless test"
        return r
    stems_in_diff: dict[str, set[str]] = {}
    deleted_stems: set[str] = set()
    for p in files:
        ns = stem_of_stateless(p)
        if ns is None:
            continue
        stems_in_diff.setdefault(ns[1], set()).add(p)
        if statuses.get(p, "M") == "D":
            deleted_stems.add(ns[1])
    missing: list[NewStatelessTest] = []
    converted: list[NewStatelessTest] = []
    for t in runnable:
        sib = stems_in_diff.get(t.stem, set())
        if any(p.endswith((".reference", ".reference.j2")) for p in sib):
            r.evidence.append(ev(next(p for p in sib if ".reference" in p), 0, "", stem=t.stem))
        elif t.stem in deleted_stems:
            converted.append(t)
        else:
            missing.append(t)
    if missing:
        r.verdict = VIOLATED
        r.evidence = [
            ev(t.path, 0, "", stem=t.stem, expected=f"{STATELESS}{t.stem}.reference")
            for t in missing
        ] + r.evidence
        r.note = f"{len(missing)} new test(s) without a .reference sibling in the diff"
        return r
    if converted and not r.evidence:
        r.verdict = NOT_APPLICABLE
        r.evidence = [ev(t.path, 0, "", stem=t.stem) for t in converted]
        r.note = "the stem's other file is deleted in the diff: an existing reference is kept"
        return r
    r.verdict = SATISFIED
    return r


# ---------------------------------------------------------------- leads (rules 6, 7)


def top2(path: str) -> str:
    parts = path.split("/")
    return "/".join(parts[:2]) if len(parts) > 2 else (parts[0] if parts else "")


def dominant_prefixes(files: list[str], *, limit: int = 3) -> list[str]:
    """The first-two-segment prefixes the PR touches most, source trees
    ahead of `tests/` and `docs/` (a fix with one source file and five test
    files is a change to that source area)."""
    counts: dict[str, int] = {}
    for p in files:
        k = top2(p)
        if k:
            counts[k] = counts.get(k, 0) + 1
    ordered = sorted(
        counts.items(), key=lambda kv: (kv[0].startswith(("tests/", "docs/")), -kv[1], kv[0])
    )
    return [k for k, _ in ordered[:limit]]


def rule_changelog_category(
    category: str,
    source: str,
    census: dict[str, dict[str, int] | None],
) -> RuleResult:
    """`census[prefix]` = {category: merged PRs} over the trailing window
    for PRs touching that prefix (None = read failed). `unusual` when the
    PR's category is under 10% of the dominant prefix's history — a lead."""
    r = RuleResult("changelog_category", epistemic=LOWER_BOUND)
    if not category:
        r.verdict, r.note = NOT_APPLICABLE, "no pr-* label and no changelog bullet"
        return r
    if not census:
        r.verdict, r.note = NOT_APPLICABLE, "no files"
        return r
    unusual = False
    for prefix, dist in census.items():
        if dist is None:
            r.evidence.append(ev(prefix, 0, "", census="unread"))
            continue
        total = sum(dist.values())
        share = (dist.get(category, 0) / total) if total else 0.0
        mode = max(dist.items(), key=lambda kv: kv[1])[0] if dist else ""
        mode_share = round(dist.get(mode, 0) / total, 3) if total else 0.0
        r.evidence.append(
            ev(
                prefix,
                0,
                "",
                category=category,
                share=round(share, 3),
                prs=total,
                mode=mode,
                mode_share=mode_share,
            )
        )
        if total >= 20 and share < UNUSUAL_SHARE and prefix == next(iter(census)):
            unusual = True
    if all(d is None for d in census.values()):
        r.verdict, r.note = UNKNOWN, "the PR ledger could not be read"
        return r
    r.verdict = UNUSUAL if unusual else SATISFIED
    r.note = f"category {category} from {source}; " + (
        "below 10% of the dominant prefix's merged history — a lead, not a rule"
        if unusual
        else "in line with the prefix's history"
    )
    return r


@dataclass
class TouchedFunction:
    symbol_id: str
    qualified_name: str
    name: str
    path: str
    start_line: int
    end_line: int
    # every tier's id for this function (the compiler tier records a
    # one-line span, the parser tier the extent; their edges are keyed by
    # their own ids)
    symbol_ids: list[str] = field(default_factory=list)
    caller_dirs: set[str] = field(default_factory=set)
    callers: int = 0


def touched_functions(diff: Diff, symbols: list[dict[str, Any]]) -> list[TouchedFunction]:
    """Functions whose span holds an added line of the diff (new-side lines
    against the indexed spans: exact for a merged PR, approximate before).
    Rows are grouped by (path, qualified_name): the span comes from any row
    that has one, the ids from every tier."""
    added_by_path: dict[str, list[int]] = {}
    for path, fd in diff.files.items():
        if fd.added:
            added_by_path[path] = sorted(ln for ln, _ in fd.added)
    groups: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for s in symbols:
        path = str(s.get("path") or "")
        if path in added_by_path:
            groups.setdefault((path, str(s.get("qualified_name") or "")), []).append(s)
    out: list[TouchedFunction] = []
    for (path, qname), rows in groups.items():
        lines = added_by_path[path]
        spans = [(int(r.get("start_line") or 0), int(r.get("end_line") or 0)) for r in rows]
        extents = [(a, b) for a, b in spans if b > a]
        if extents:
            start, end = min(a for a, _ in extents), max(b for _, b in extents)
        else:
            start, end = spans[0]
        if end < start or not any(start <= ln <= end for ln in lines):
            continue
        ids = list(dict.fromkeys(str(r.get("symbol_id") or "") for r in rows if r.get("symbol_id")))
        if not ids:
            continue
        out.append(
            TouchedFunction(
                ids[0],
                qname,
                str(rows[0].get("name") or ""),
                path,
                start,
                end,
                symbol_ids=ids,
            )
        )
    return out


def caller_dir(path: str) -> str:
    """`src/Storages` for the source tree, the first segment elsewhere —
    "top-level directory" read at the granularity that means something in a
    repository where everything is under `src/`."""
    parts = path.split("/")
    if parts and parts[0] in ("src", "programs", "base", "tests") and len(parts) > 2:
        return "/".join(parts[:2])
    return parts[0] if parts else ""


def attach_callers(funcs: list[TouchedFunction], edges: list[dict[str, Any]]) -> None:
    """Hop-1 caller edges (any tier) onto the function they point at; one
    call site counted once per (caller path, line) across tiers."""
    by_id: dict[str, TouchedFunction] = {}
    for f in funcs:
        for sid in f.symbol_ids or [f.symbol_id]:
            by_id[sid] = f
    seen: set[tuple[str, str, int]] = set()
    for e in edges:
        target = by_id.get(str(e.get("to_symbol_id") or ""))
        if target is None:
            continue
        key = (target.qualified_name, str(e.get("path") or ""), int(e.get("line") or 0))
        if key in seen:
            continue
        seen.add(key)
        target.callers += 1
        target.caller_dirs.add(caller_dir(str(e.get("path") or "")))


def rule_wide_callers_untested(
    funcs: list[TouchedFunction],
    coverage: dict[str, tuple[str, int]],
    *,
    min_dirs: int,
    control_ok: bool,
) -> RuleResult:
    """`coverage[symbol_id]` = (verdict, tests_total) from the coverage claim for each
    wide function checked. `violated` only with the coverage control green; a lead by
    design."""
    r = RuleResult("wide_callers_untested", epistemic=LOWER_BOUND)
    wide = [f for f in funcs if len(f.caller_dirs) > min_dirs]
    if not funcs:
        r.verdict, r.note = NOT_APPLICABLE, "no indexed function holds an added line"
        return r
    if not wide:
        r.verdict = NOT_APPLICABLE
        r.note = (
            f"{len(funcs)} touched function(s), none with hop-1 callers in more than "
            f"{min_dirs} directories"
        )
        return r
    if not control_ok:
        r.verdict, r.note = UNKNOWN, "the coverage control failed: no uncovered claim is made"
        r.evidence = [
            ev(f.path, f.start_line, f.qualified_name, callers=f.callers, dirs=len(f.caller_dirs))
            for f in wide
        ]
        return r
    uncovered = []
    for f in sorted(wide, key=lambda f: -len(f.caller_dirs)):
        verdict, tests_total = coverage.get(f.symbol_id, (UNKNOWN, 0))
        r.evidence.append(
            ev(
                f.path,
                f.start_line,
                f.qualified_name,
                callers=f.callers,
                dirs=len(f.caller_dirs),
                sample_dirs=sorted(f.caller_dirs)[:6],
                coverage=verdict,
                tests_total=tests_total,
            )
        )
        if verdict == "uncovered":
            uncovered.append(f)
    r.verdict = VIOLATED if uncovered else SATISFIED
    r.note = (
        f"{len(uncovered)} of {len(wide)} widely-called function(s) no test names (a lead: "
        "the grep is a textual floor)"
        if uncovered
        else f"{len(wide)} widely-called function(s), each named by at least one test"
    )
    return r
