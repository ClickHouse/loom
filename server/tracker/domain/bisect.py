"""Pure helpers for `code.bisect_candidates`: the release
range a culprit must have shipped in, the frame names worth searching for,
the ranking of candidate PRs, and the verdict.

A ClickHouse release is `YY.M` (`25.12` → `26.1`). The tracker source stamps a
merged PR with `minor_version`, the FIRST release that contains it. An
issue that reproduces on `bad` and is clean on `good` was introduced by a
PR whose `minor_version` lies in `(good, bad]` — that interval is the
search range; with no clean version only `bad` itself is searched (a
floor, not a proof of range).
"""

from __future__ import annotations

import bisect
import math
import re
from dataclasses import dataclass, field
from datetime import datetime
from functools import lru_cache
from typing import Any

from server.settings import settings

CANDIDATES = "candidates"
NO_CANDIDATES = "no_candidates"
ALREADY_BISECTED = "already_bisected"
# the tracker graph holds a `caused_by` edge from the issue to a PR: the culprit is
# named before any scan
GRAPH_NAMED = "graph_named"
UNKNOWN = "unknown"

_VERSION_RE = re.compile(r"^(\d{2})\.(\d{1,2})(?:\.\d+)*$")
# Bare names that name nothing about an area: never a search token.
GENERIC_NAMES = frozenset(
    {
        "get",
        "set",
        "run",
        "execute",
        "init",
        "main",
        "read",
        "write",
        "insert",
        "size",
        "begin",
        "end",
        "next",
        "reset",
        "clear",
        "add",
        "remove",
        "create",
        "destroy",
        "check",
        "process",
        "update",
        "start",
        "stop",
        "finish",
        "operator",
        "operator()",
        "lambda",
        "function",
        "invoke",
        "call",
        "apply",
        "visit",
        "impl",
        "data",
        "value",
        "type",
        "name",
    }
)
_MAX_VERSIONS = 24
# A PR whose title says it moved code without changing behaviour ranks below
# one that changed behaviour. Such commits are demoted rather than skipped,
# because "refactor" titles lie often enough.
_META_TITLE_RE = re.compile(
    r"(?i)\b(refactor(?:ing)?|rename|cleanup|clean up|typo|typos|docs?|documentation|"
    r"comment(?:s)?|style|formatting|format|cosmetic|reformat|whitespace|clang-tidy|"
    r"update (?:changelog|contributors|version)|bump)\b"
)
# Identifiers worth grepping a diff for, taken from an issue's text: a
# CamelCase or snake_case token of eight or more characters, an
# UPPER_CASE error code, or a ClickHouse setting name. Short or generic
# words never: short greppable patterns derived from the report, searched over
# the candidate set's diffs, are what identifies the bug-introducing change.
_IDENT_RE = re.compile(r"(?<![@\w])([A-Za-z_][A-Za-z0-9_]{7,})\b")
# Text a bot wrote INTO the report is not the report. A
# triage comment carries the bisect's own answer (`introduced_by_pr`, the
# introducer's handle, a commit sha) — feeding it back is circular at triage
# time and a leak in any measurement; a version-info block names the fix.
# A block between paired markers (`<!-- x:start --> … <!-- x:end -->`,
# `<!-- x --> … <!-- /x -->`) is the bot's, comment or not: (opener, closer).
_BOT_MARKERS = (
    (re.compile(r"<!--\s*([\w-]+):start\s*-->"), re.compile(r"<!--\s*([\w-]+):end\s*-->")),
    (re.compile(r"<!--\s*([\w-]+)\s*-->"), re.compile(r"<!--\s*/([\w-]+)\s*-->")),
)
_COMMENT_RE = re.compile(r"<!--.*?-->", re.S)
_BOT_BLOCK_RES = (
    # a report template's metadata sections: what the triage found, not
    # what the reporter saw
    # … up to the next heading, rule, code fence or blank line before prose
    re.compile(
        r"(?ms)^#{2,4}[ \t]*(?:Bisect|Affects|Triage metadata|Version info|Suggested next step|"
        r"Suggested assignees|Tagged owners)\b.*?"
        r"(?=^#{1,4}[ \t]|^---[ \t]*$|^```|\n[ \t]*\n(?![ \t]*[-*|])|\Z)"
    ),
    re.compile(r"(?im)^[ \t]*cc introducing PR .*$"),
)
# a commit sha (or its prefix), a finding id (`h_issue_112031`) or a hash
# (`__set_16854109006789820482_…`): an identifier that names nothing in a diff
_OPAQUE_ID_RE = re.compile(r"^(?:[0-9a-f]{7,}|[A-Za-z_]+_\d{5,}|.*\d{8,}.*)$")
# the reproduction's own object names (`CREATE TABLE t_minmax_float …`,
# `INSERT INTO repro_topk …`, `FROM demo_ctrl`): never in the tree
_REPRO_NAME_RE = re.compile(
    r"(?i)\b(?:CREATE\s+(?:OR\s+REPLACE\s+)?(?:TEMPORARY\s+)?(?:TABLE|VIEW|DATABASE|DICTIONARY)"
    r"(?:\s+IF\s+NOT\s+EXISTS)?|DROP\s+(?:TABLE|VIEW|DATABASE)(?:\s+IF\s+EXISTS)?|INSERT\s+INTO|"
    r"FROM|JOIN|INTO|ATTACH\s+TABLE|DETACH\s+TABLE|ALTER\s+TABLE|OPTIMIZE\s+TABLE|TRUNCATE\s+TABLE)"
    r"\s+`?([A-Za-z_][\w]*(?:\.[A-Za-z_][\w]*)?)`?"
)
# a pattern's vocabulary tokens: the upstream `tokens()` splits on
# non-alphanumerics, so `apply_patch_parts` is three tokens there
_SUBTOKEN_RE = re.compile(r"[A-Za-z0-9]+")
# a token below the vocabulary's df ≥ 3 floor is rare, not unknown
_UNSEEN_DF = 2
_PATTERN_STOP = frozenset(
    {
        "clickhouse",
        "exception",
        "received",
        "version",
        "function",
        "database",
        "settings",
        "expected",
        "actual",
        "message",
        "traceback",
        "attempted",
        "official",
        "describe",
        "reproduce",
        "additional",
        "information",
        "checklist",
        "released",
        "unexpected",
        "behaviour",
        "behavior",
        "following",
        "example",
        "results",
        "instead",
        "returns",
        "returned",
        "because",
        "problem",
        "sometimes",
        "randomly",
        "queries",
        "request",
        "response",
        "keeper",
        "mergetree",
        "stateless",
        "integration",
        "clickhouse_test",
        "docker",
        "container",
        "master",
        "localhost",
        "default",
        "warning",
        "critical",
        "logical",
        "error_code",
        "exception_code",
        # A triage bot's vocabulary
        "converged",
        "ancient",
        "incomplete_range",
        "misconverged",
        "budget_exceeded",
        "sqlancer",
        "buzzhouse",
        "observed",
        "required",
        "analytical",
        "reproduced",
        "regressed",
        "regression",
    }
)
_MAX_PATTERNS = 12
# `Foo.cpp`, `Foo.h:123`, `test.py`, `x.sql` — a file reference, not a frame.
_FILE_REF_RE = re.compile(r"\.(?:cpp|cc|h|hpp|py|sql|sh|reference)(?::\d+)?$")


_BRANCH_PREFIXES = ("refs/heads/", "release/", "releases/", "rel/")


def parse_version(text: str) -> tuple[int, int] | None:
    """`26.4`, `26.4.1.12`, `v26.4` → (26, 4); a release BRANCH name such as
    `release/26.4` or `refs/heads/release/26.4` too (some repositories name
    their release branches that way, others `26.4`; without this a branch
    namespace carries no version and `code.backport_gap` answers
    `no_branch_version`). Anything else → None."""
    s = (text or "").strip()
    changed = True
    while changed:
        changed = False
        for pre in _BRANCH_PREFIXES:
            if s.startswith(pre):
                s = s[len(pre) :]
                changed = True
    m = _VERSION_RE.match(s.lstrip("v"))
    if not m:
        return None
    year, month = int(m.group(1)), int(m.group(2))
    if not 1 <= month <= 12:
        return None
    return year, month


def minor_of(text: str) -> str:
    v = parse_version(text)
    return f"{v[0]}.{v[1]}" if v else ""


LOOKBACK_MINORS = 3  # with no clean release, search this many minors below the failing one
# (the handler passes `code_bisect_lookback_minors`, 6 by default)
STAMP_SLACK_MINORS = 1  # the release stamp can lag the branch the bug reproduces on
# A bug that reproduces on master only, with a clean release,
# was introduced after that release — the minors above it, bounded
LOOKAHEAD_MINORS = 3
# The development branch goes by several names across
# repositories; any of them is "the bug reproduces on the development branch"
DEV_BRANCHES = frozenset({"master", "main", "trunk", "develop", "development", "dev", "head"})


def master_reproduces(versions: list[str]) -> bool:
    """The triage saw the bug on the development branch."""
    return any(str(v).strip().lower() in DEV_BRANCHES for v in versions)


def _step(year: int, month: int, delta: int) -> tuple[int, int]:
    idx = year * 12 + (month - 1) + delta
    return idx // 12, idx % 12 + 1


def versions_between(
    good: str,
    bad: str,
    *,
    lookback: int = LOOKBACK_MINORS,
    slack: int = STAMP_SLACK_MINORS,
    master: bool = False,
    lookahead: int = LOOKAHEAD_MINORS,
) -> list[str]:
    """Release minors a culprit's first release may carry, oldest first.
    With a clean release: `(good, bad + slack]`. Without one: the `lookback`
    minors below `bad` up to `bad + slack` — a bounded floor instead of an
    unbounded one. With no failing RELEASE but the bug on master and a clean
    release (`master=True`): `(good, good + lookahead]` — the culprit
    shipped after the last clean minor (8 of 59 frameless
    gold issues, every culprit stamped `good + 1`). `[]` when nothing
    parses. Capped at two years.

    The slack exists because about one converged bisect in thirteen has its culprit stamped
    one release LATER than the branch the bug reproduces on (the stamp or the edge lags);
    one extra minor keeps them in the set at the cost of one more release of candidates."""
    b = parse_version(bad)
    g = parse_version(good) if good else None
    if b is None:
        if g is None or not master:
            return []
        top = _step(g[0], g[1], max(1, int(lookahead)))
    else:
        if g is None or g >= b:
            g = _step(b[0], b[1], -max(0, int(lookback)) - 1)
        top = _step(b[0], b[1], max(0, int(slack)))
    out: list[str] = []
    year, month = g
    while (year, month) < top and len(out) < _MAX_VERSIONS:
        year, month = _step(year, month, 1)
        out.append(f"{year}.{month}")
    return out


def window_name(good: str, bad: str, versions: list[str], *, before: bool = False) -> str:
    """How the release range was drawn: `clean_to_bad_plus_slack`,
    `lookback_from_bad`, `master_after_clean`, `merged_before` (a
    merge-time bound instead of releases), or empty when none was."""
    if not versions:
        return "merged_before" if before else ""
    if not bad:
        return "master_after_clean"
    return "clean_to_bad_plus_slack" if good else "lookback_from_bad"


# Files nearly every PR touches: a frame that resolves here says nothing about the culprit,
# and the file filter drowns the list (20 candidates on the culprit's source files, 210 once
# two Settings files join). Dropped from the path filter when any other path remains.
_HUB_RE = re.compile(
    r"(?:^|/)(Settings\.cpp|Settings\.h|SettingsChangesHistory\.cpp|MergeTreeSettings\.cpp|"
    r"ServerSettings\.cpp|FormatFactorySettings\.h|ErrorCodes\.cpp|CMakeLists\.txt|"
    r"SettingsEnums\.cpp|SettingsEnums\.h|version\.txt|autogenerated_versions\.txt)$"
    r"|^(docs|tests|utils|programs/server/config)/"
)


def is_hub_path(path: str) -> bool:
    return bool(_HUB_RE.search(path or ""))


def drop_hub_paths(paths: list[str]) -> tuple[list[str], list[str]]:
    """(kept, dropped): hub files leave the filter while a specific path
    remains; a list of hub files only is kept as it is (some signal beats
    none) and the caller says so."""
    hubs = [p for p in paths if is_hub_path(p)]
    kept = [p for p in paths if not is_hub_path(p)]
    if not kept:
        return list(paths), []
    return kept, hubs


def is_older(version: str, than: str) -> bool:
    """`version` parses and is older than `than`; with no `than`, any
    parsable version counts."""
    v = parse_version(version)
    if v is None:
        return False
    t = parse_version(than) if than else None
    return t is None or v < t


def oldest(versions: list[str]) -> str:
    parsed = sorted({v for v in (parse_version(x) for x in versions) if v})
    return f"{parsed[0][0]}.{parsed[0][1]}" if parsed else ""


def newest(versions: list[str]) -> str:
    parsed = sorted({v for v in (parse_version(x) for x in versions) if v})
    return f"{parsed[-1][0]}.{parsed[-1][1]}" if parsed else ""


def bare_name(symbol: str) -> str:
    """`DB::MergeTreeData::loadDataParts(bool)` → `loadDataParts`;
    `MergeTreeData.cpp:123` → ``; a Python `mod.fn` → `fn`."""
    s = (symbol or "").strip()
    if not s or "/" in s or _FILE_REF_RE.search(s):
        return ""
    s = s.split("(")[0].strip()
    s = s.split("::")[-1]
    if "." in s and " " not in s:
        s = s.split(".")[-1]
    s = re.sub(r"[^A-Za-z0-9_~]", "", s)
    return s


def report_text(text: str) -> str:
    """The report minus what a bot wrote into it: paired
    marker blocks and HTML comments (a triage comment carries the bisect's
    own verdict, an enrichment block the owners the bot tagged), the
    template's metadata sections (Bisect, Affects, Version info — the fix
    is named there), the `cc introducing PR author` lines. What remains is
    what the reporter saw."""
    out = text or ""
    for opener, closer in _BOT_MARKERS:
        out = _without_blocks(out, opener, closer)
    # no comment closes after the last `-->`, so an unclosed one is not rescanned
    end = out.rfind("-->")
    if end >= 0:
        out = _COMMENT_RE.sub(" ", out[: end + 3]) + out[end + 3 :]
    for rx in (*_BOT_BLOCK_RES, *_bot_signature_res(settings.code_bot_authors)):
        out = rx.sub(" ", out)
    return out


def _without_blocks(text: str, opener: re.Pattern[str], closer: re.Pattern[str]) -> str:
    """Each opener through the first closer of its name after it, replaced by a
    blank. The closers are indexed once, so an opener with none is not a scan
    to the end of the text."""
    closers: dict[str, list[tuple[int, int]]] = {}
    for m in closer.finditer(text):
        closers.setdefault(m.group(1), []).append(m.span())
    parts: list[str] = []
    pos = 0
    for m in opener.finditer(text):
        if m.start() < pos:
            continue
        spans = closers.get(m.group(1), [])
        i = bisect.bisect_left(spans, (m.end(), -1))
        if i < len(spans):
            parts += [text[pos : m.start()], " "]
            pos = spans[i][1]
    return "".join(parts) + text[pos:]


@lru_cache(maxsize=4)
def _bot_signature_res(bots: str) -> tuple[re.Pattern[str], ...]:
    """A line naming one of `code_bot_authors` is that bot's signature."""
    names = [re.escape(b.strip()) for b in bots.split(",") if b.strip()]
    if not names:
        return ()
    return (re.compile(rf"(?im)^.*(?<![a-z0-9])(?:{'|'.join(names)})(?![a-z0-9]).*$"),)


def repro_names(text: str) -> set[str]:
    """The reproduction's own object names — the tables, views and
    databases its SQL creates, fills, reads or drops. A
    reporter names them freely (`t_minmax_float`, `repro_topk`, `demo_ctrl`)
    and no diff in the tree carries them."""
    out: set[str] = set()
    for m in _REPRO_NAME_RE.finditer(text or ""):
        name = m.group(1)
        out.add(name.lower())
        if "." in name:
            out.add(name.split(".", 1)[1].lower())
    return out


def _shaped(tok: str) -> bool:
    """An identifier, not a word: an underscore, a digit, a capital AFTER
    the first character (`KeyCondition`, `toDateTime64`) or all capitals
    (`LOGICAL_ERROR`). A capitalised word (`Reproduced`, `Widening`) is
    prose; counted as a pattern, it lands among the rarest "patterns" of one
    report in three."""
    tail = tok[1:]
    return ("_" in tok) or any(ch.isdigit() for ch in tok) or tail != tail.lower() or tok.isupper()


def pattern_weights(patterns: list[str], stats: dict[str, Any] | None) -> dict[str, float]:
    """IDF over the issue corpus per pattern: the mean
    `ln((N + 1) / (df + 1))` of the pattern's vocabulary tokens, `stats` in
    `TrackerSource.token_stats`'s shape (`n`, `df` per lower-cased token; the
    vocabulary keeps df ≥ 3, so an unseen token counts df = 2). Without a
    vocabulary every pattern weighs 1.0 (plain counting)."""
    n = int((stats or {}).get("n") or 0)
    df = (stats or {}).get("df") or {}
    if n <= 0 or not isinstance(df, dict):
        return {p: 1.0 for p in patterns}
    out: dict[str, float] = {}
    for p in patterns:
        subs = _SUBTOKEN_RE.findall(p)
        if not subs:
            out[p] = 1.0
            continue
        idfs = [math.log((n + 1) / (int(df.get(t.lower(), _UNSEEN_DF)) + 1)) for t in subs]
        out[p] = round(max(0.0, sum(idfs) / len(idfs)), 3)
    return out


def derive_patterns(
    text: str,
    *,
    exclude: list[str] | None = None,
    stats: dict[str, Any] | None = None,
) -> list[str]:
    """Greppable literals from an issue's title and body: identifiers of
    eight or more characters that carry a case change, an underscore or a
    digit (`applyPatchesMergeOnKey`, `apply_patch_parts`, `LOGICAL_ERROR`),
    stop-listed words, opaque ids (a sha, a finding id), `@handles` and the
    caller's frames and the reproduction's own object names removed, the
    bot-written blocks cut first. Ordered by
    frequency × IDF over the issue corpus when `stats` is given, by frequency
    alone otherwise. At most twelve —
    every pattern costs one needle per candidate diff upstream."""
    counts: dict[str, int] = {}
    seen = report_text(text)
    skip = {x.lower() for x in (exclude or [])} | repro_names(seen)
    for m in _IDENT_RE.finditer(seen):
        tok = m.group(1)
        # Long encoded payloads in issue bodies are not useful diff needles.
        # Bounding them also keeps the candidate scan and the returned
        # pattern list from carrying kilobytes of opaque text.
        if len(tok) > 128:
            continue
        low = tok.lower()
        if low in _PATTERN_STOP or low in skip or tok.isdigit() or _OPAQUE_ID_RE.match(tok):
            continue
        if not _shaped(tok):
            continue
        counts[tok] = counts.get(tok, 0) + 1
    weights = pattern_weights(list(counts), stats)
    ranked = sorted(
        counts.items(), key=lambda kv: (-kv[1] * weights.get(kv[0], 1.0), -len(kv[0]), kv[0])
    )
    return [t for t, _ in ranked[:_MAX_PATTERNS]]


def scope_patterns(patterns: list[str], weights: dict[str, float], k: int) -> list[str]:
    """The `k` rarest patterns — the needles that bound a frameless scan to
    the PRs whose diff carries one of them: highest IDF
    first, longer first on a tie."""
    if k <= 0:
        return []
    ranked = sorted(patterns, key=lambda p: (-weights.get(p, 1.0), -len(p), p))
    return ranked[:k]


def kind_of(title: str) -> str:
    """`meta` for a refactor / rename / docs / style title, else `change`."""
    return "meta" if _META_TITLE_RE.search(title or "") else "change"


def frame_names(symbols: list[str]) -> list[str]:
    """Distinct bare names worth grepping a diff for: specific (≥ 6 chars,
    not generic), order of first appearance."""
    out: list[str] = []
    for sym in symbols:
        name = bare_name(sym)
        if len(name) < 6 or name.lower() in GENERIC_NAMES or name in out:
            continue
        out.append(name)
    return out[:32]


@dataclass
class Candidate:
    pr_number: int
    title: str
    author: str
    merged_at: str
    minor_version: str
    matched_paths: list[str] = field(default_factory=list)
    matched_symbols: list[str] = field(default_factory=list)
    matched_patterns: list[str] = field(default_factory=list)
    added_patterns: list[str] = field(default_factory=list)
    kind: str = "change"
    has_test: int = -1
    # Later PRs whose description names this one as the cause
    named_cause_by: list[dict[str, Any]] = field(default_factory=list)
    # The fix's rewritten lines, blamed at its parent, name this PR
    blamed_by_fix: dict[str, Any] | None = None
    url: str = ""
    score: float = 0.0
    # caused_by_edge when the tracker graph, not the scan, put the PR here
    named_by: str = ""
    # frame names in the diff's hunk headers (the change lands inside them)
    hunk_symbols: list[str] = field(default_factory=list)
    # The blame marks (None when the caller asked for none, so
    # the dicts stay byte-identical to the unmarked shape)
    mark: Any = None
    # The fusion signals that ranked this PR in their top 10
    # (`named_by_report` first when the report itself names it)
    why: list[str] = field(default_factory=list)
    # The commit that landed the PR: stored (the ledger) | mirror (a first-parent
    # search) | "" when neither knows it
    merge_commit_sha: str = ""
    merge_sha_source: str = ""

    def as_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "pr_number": self.pr_number,
            "title": self.title[:160],
            "author": self.author,
            "merged_at": self.merged_at,
            "minor_version": self.minor_version,
            "matched_paths": self.matched_paths[:10],
            "matched_symbols": self.matched_symbols[:10],
            # the candidate's diff changes one of the query's paths (hub paths were dropped
            # from the scope before the scan) — a caller's file-touch rule without a
            # GitHub round trip
            "touches_affected": bool(self.matched_paths),
            "kind": self.kind,
            "has_test": None if self.has_test < 0 else bool(self.has_test),
            "url": self.url,
            "score": round(self.score, 3),
            "merge_commit_sha": self.merge_commit_sha,
            "merge_sha_source": self.merge_sha_source,
        }
        if self.matched_patterns:
            out["matched_patterns"] = self.matched_patterns[:12]
        if self.added_patterns:
            out["added_patterns"] = self.added_patterns[:12]
        if self.named_by:
            out["named_by"] = self.named_by
        if self.named_cause_by:
            out["named_cause_by"] = self.named_cause_by[:3]
        if self.blamed_by_fix:
            out["blamed_by_fix"] = self.blamed_by_fix
        if self.why:
            out["why"] = self.why[:6]
        if self.mark is not None:
            self.mark.fold_into(out)
            out.pop("last_touched_rank", None)
        return out


def _weighted_share(hits: list[str], weights: dict[str, float]) -> float:
    """Σ weight of the patterns hit / Σ weight of all patterns, in [0, 1]:
    a rare identifier the diff added counts for more than a common one;
    with unit weights, the plain share of patterns hit."""
    total = sum(weights.values())
    if total <= 0:
        return 0.0
    return min(1.0, sum(weights.get(h, 0.0) for h in hits) / total)


def rank_candidates(
    rows: list[dict[str, Any]],
    *,
    frames: list[str],
    paths: list[str],
    limit: int,
    patterns: list[str] | None = None,
    pattern_weights: dict[str, float] | None = None,
) -> list[Candidate]:
    """Score, saturating per term: 2.0 × share of crashing frames the diff
    names; 1.5 × IDF-weighted share of the issue's patterns the diff ADDED
    (a line beginning `+` — the pickaxe signal: the change that brought the
    text in, not one that merely touched it); 1.0 × weighted share of
    patterns present anywhere in the diff; 1.0 × share of the frames' files
    touched; −0.75 for a refactor / rename / docs / style title. The
    weights are IDF over the issue corpus or 1.0 each.
    Ties: newest first."""
    pats = [p for p in (patterns or []) if p]
    weights = {p: float((pattern_weights or {}).get(p, 1.0)) for p in pats}
    n_frames, n_paths = max(1, len(frames)), max(1, len(paths))
    out: list[Candidate] = []
    for r in rows:
        ms = [str(x) for x in (r.get("matched_symbols") or [])]
        mp = [str(x) for x in (r.get("matched_paths") or [])]
        mpat = [str(x) for x in (r.get("matched_patterns") or [])]
        apat = [str(x) for x in (r.get("added_patterns") or [])]
        hunk = [str(x) for x in (r.get("hunk_symbols") or [])]
        title = str(r.get("title") or "")
        kind = kind_of(title)
        score = (
            2.0 * min(1.0, len(ms) / n_frames)
            + 1.5 * _weighted_share(apat, weights)
            + 1.0 * _weighted_share(mpat, weights)
            + 1.0 * min(1.0, len(mp) / n_paths)
            - (0.75 if kind == "meta" else 0.0)
        )
        merged = r.get("merged_at")
        merged_iso = merged.isoformat() if isinstance(merged, datetime) else str(merged or "")
        raw_test = r.get("has_test")
        has_test = -1 if raw_test is None else int(raw_test)
        out.append(
            Candidate(
                pr_number=int(r.get("pr_number") or 0),
                title=title,
                author=str(r.get("author") or ""),
                merged_at=merged_iso,
                minor_version=str(r.get("minor_version") or ""),
                matched_paths=mp,
                matched_symbols=ms,
                matched_patterns=mpat,
                added_patterns=apat,
                kind=kind,
                has_test=has_test,
                url=str(r.get("url") or ""),
                score=score,
                hunk_symbols=hunk,
            )
        )
    out.sort(key=lambda c: (-c.score, -_ordinal(c.merged_at)))
    return out[:limit]


# ---- the blame marks on bisect candidates -------------------------------------

# what a mark is worth on top of the candidate score: a hunk header naming the
# frame (the change lands inside it), the frame named in the title / body,
# a re-land inheriting either; disclosed per candidate, never hidden in `score`
MARK_BONUS = {"true": 0.5, "named": 0.25}


def mark_bisect_candidates(
    candidates: list[Candidate],
    *,
    records: dict[int, dict[str, Any]],
    frames: list[str],
    diff_kinds: dict[int, str],
    diff_touch: dict[int, bool] | None = None,
) -> dict[int, Any]:
    """The blame marks on the bisect candidates: backport /
    cherry-pick / sync titles, reverts, reverted PRs (unless re-landed),
    re-lands (`reland_of`, inheriting the original's symbol marks), the
    bounded diff's whitespace-only / rename-only verdict, `touches_symbol`
    from the QA diff's hunk headers (`true`; `false` when the diff was
    scanned for frames and no header names one) or the ledger's title /
    body (`named`). `records` is the ledger row per PR (title, body_excerpt,
    merged_at); a PR without one is marked from its title alone. `diff_touch`
    is the mirror diff's hunk-header verdict per PR where one ran — it stands
    in for the QA headers on a candidate the graph named without a scan."""
    from server.code.domain.index import blame_introduced as intro
    from server.code.domain.index.blame_range import unix_of

    def body_of(n: int) -> str:
        return str((records.get(n) or {}).get("body_excerpt") or "")

    relands = intro.detect_relands(
        [(c.pr_number, c.title, body_of(c.pr_number), unix_of(c.merged_at)) for c in candidates]
    )
    relanded = {r.original: r.pr_number for r in relands.values() if r.original}
    reverted: set[int] = set()
    for c in candidates:
        if c.pr_number not in relands and intro.is_revert(c.title, body_of(c.pr_number)):
            reverted |= intro.reverted_prs(body_of(c.pr_number))
    marks: dict[int, Any] = {}
    for c in candidates:
        m = intro.Mark(meta=c.kind == "meta", relanded_by=relanded.get(c.pr_number, 0))
        reland = relands.get(c.pr_number)
        if intro.is_derivative(c.title):
            m.demoted = "backport"
        elif reland is not None:
            m.reland_of, m.reland_via = reland.original, reland.via
        elif intro.is_revert(c.title, body_of(c.pr_number)):
            m.demoted = "revert"
        elif c.pr_number in reverted and not m.relanded_by:
            m.demoted = "reverted"
        elif diff_kinds.get(c.pr_number):
            m.demoted = diff_kinds[c.pr_number]
        _symbol_marks(m, c, frames, body_of(c.pr_number), (diff_touch or {}).get(c.pr_number))
        marks[c.pr_number] = m
    intro.inherit_reland_marks(marks)
    return marks


def _symbol_marks(m: Any, c: Candidate, frames: list[str], body: str, touch: bool | None) -> None:
    """`touches_symbol` for one candidate: a hunk header naming a frame (the
    QA diff's, or the mirror diff's) is `true` (promoted `touches_symbol`);
    the frame in the title or body is `named` (promoted `names_symbol`); a
    diff scanned for frames whose headers name none is `false` — the frame
    may be a call site in it; a graph-named PR nobody scanned stays unknown."""
    if not frames:
        return
    names_it = any(f in f"{c.title}\n{body}" for f in frames)
    scanned = not c.named_by or touch is not None
    if c.hunk_symbols or touch:
        m.touches_symbol = "true"
        m.promoted.append("touches_symbol")
    elif names_it:
        m.touches_symbol = "named"
    elif scanned:
        m.touches_symbol = "false"
    if names_it:
        m.promoted.append("names_symbol")


def rank_marked(candidates: list[Candidate], marks: dict[int, Any]) -> list[Candidate]:
    """The marked order: hard-demoted candidates last (a meta title stays a
    soft tier: its −0.75 is already in `score`), then the candidate score plus the
    symbol bonus, then recency."""

    def key(c: Candidate) -> tuple[float, ...]:
        m = marks.get(c.pr_number)
        demoted = 1.0 if (m is not None and m.demoted) else 0.0
        bonus = MARK_BONUS.get(m.touches_symbol, 0.0) if m is not None else 0.0
        return (demoted, -(c.score + bonus), -_ordinal(c.merged_at))

    return sorted(candidates, key=key)


def _words(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9_]{3,}", str(text or "").lower())}


def relevant_statement(
    stmt: dict[str, Any], *, paths: list[str], frames: list[str], patterns: list[str]
) -> bool:
    """A cause statement is about THIS bug when the naming PR changed one of
    the query's paths, or its title carries one of the query's frames or
    patterns (live, 3 of 7 statements naming a pool
    candidate were about other regressions, and none of those 3 matched)."""
    files = {str(f) for f in (stmt.get("files") or [])}
    if files & {str(p) for p in paths}:
        return True
    title = _words(str(stmt.get("title") or ""))
    squashed = str(stmt.get("title") or "").lower().replace(" ", "")
    for term in [*frames, *patterns]:
        t = str(term or "").strip().lower()
        if len(t) >= 4 and (t in title or t in squashed):
            return True
    return False


def promote_named_causes(
    candidates: list[Candidate],
    statements: list[dict[str, Any]],
    *,
    paths: list[str],
    frames: list[str],
    patterns: list[str],
) -> list[Candidate]:
    """Candidates a relevant later PR names as the cause first (their order
    otherwise kept), each carrying who named it; unchanged with none."""
    by_number = {c.pr_number: c for c in candidates}
    for st in statements:
        if not relevant_statement(st, paths=paths, frames=frames, patterns=patterns):
            continue
        merged = st.get("merged_at")
        iso = merged.isoformat() if isinstance(merged, datetime) else str(merged or "")
        for n in st.get("names") or []:
            c = by_number.get(int(n))
            if c is None or _ordinal(iso) < _ordinal(c.merged_at):
                continue
            entry = {
                "pr_number": int(st.get("pr_number") or 0),
                "title": str(st.get("title") or "")[:120],
            }
            if entry not in c.named_cause_by:
                c.named_cause_by.append(entry)
    named = [c for c in candidates if c.named_cause_by]
    return named + [c for c in candidates if not c.named_cause_by] if named else candidates


def split_after_report(
    candidates: list[Candidate], opened_at: datetime | None
) -> tuple[list[Candidate], list[Candidate]]:
    """(kept, dropped): a PR merged AFTER the issue was opened cannot have
    introduced what the report already saw — it is the fix, or nothing
    (a temporal upper bound). The release-stamp slack admits such PRs; this removes them. With no
    opening time everything is kept."""
    if opened_at is None:
        return list(candidates), []
    cutoff = opened_at.timestamp()
    kept = [c for c in candidates if not (_ordinal(c.merged_at) > cutoff > 0)]
    dropped = [c for c in candidates if _ordinal(c.merged_at) > cutoff > 0]
    return kept, dropped


def graph_candidates(rows: dict[int, dict[str, Any]], numbers: list[int]) -> list[Candidate]:
    """Candidates the tracker graph named: the PRs at the end of the issue's
    `caused_by` edges, in edge order, scored above any scan hit."""
    out: list[Candidate] = []
    for n in numbers:
        r = rows.get(n) or {}
        merged: Any = r.get("merged_at") or r.get("closed_at")
        iso = getattr(merged, "isoformat", None)
        out.append(
            Candidate(
                pr_number=n,
                title=str(r.get("title") or ""),
                author=str(r.get("author") or ""),
                merged_at=str(iso()) if callable(iso) else str(merged or ""),
                minor_version=str(r.get("minor_version") or ""),
                kind=kind_of(str(r.get("title") or "")),
                has_test=int(r.get("has_test", -1) if r.get("has_test") is not None else -1),
                url=str(r.get("url") or ""),
                score=3.0,
                named_by="caused_by_edge",
            )
        )
    return out


def _ordinal(iso: str) -> float:
    try:
        return datetime.fromisoformat(iso).timestamp()
    except ValueError:
        return 0.0


def ordinal_of(iso: str) -> float:
    """The recency key of a candidate's `merged_at` (0 when unparsable)."""
    return _ordinal(iso)


def verdict_of(
    *,
    candidates: list[Candidate],
    versions: list[str],
    paths: list[str],
    introduced_commit: str,
    qa_ok: bool,
    graph_named: bool = False,
    widened: bool = False,
    time_window: bool = False,
) -> tuple[str, list[str]]:
    """The verdict and its causes. `already_bisected` when the issue's QA row
    already names the introducing commit; `graph_named` when a `caused_by` edge names the PR
    and no commit is recorded (about two thirds of resolved edges); `unknown` names what was
    missing. A scan bounded by directories or patterns instead of paths (`widened`) is a
    scope: `no_resolved_paths` is not a cause then."""
    causes: list[str] = []
    if introduced_commit:
        return ALREADY_BISECTED, ["introduced_commit_known"]
    if graph_named:
        return GRAPH_NAMED, ["caused_by_edge"]
    if not qa_ok:
        causes.append("tracker_unavailable")
    if not versions and not time_window:
        causes.append("no_failing_version")
    if not paths and not widened:
        causes.append("no_resolved_paths")
    if causes:
        return UNKNOWN, causes
    return (CANDIDATES if candidates else NO_CANDIDATES), []


__all__ = [
    "ALREADY_BISECTED",
    "CANDIDATES",
    "GRAPH_NAMED",
    "NO_CANDIDATES",
    "UNKNOWN",
    "Candidate",
    "bare_name",
    "derive_patterns",
    "drop_hub_paths",
    "frame_names",
    "graph_candidates",
    "MARK_BONUS",
    "is_hub_path",
    "is_older",
    "kind_of",
    "mark_bisect_candidates",
    "master_reproduces",
    "minor_of",
    "newest",
    "oldest",
    "ordinal_of",
    "parse_version",
    "pattern_weights",
    "rank_candidates",
    "rank_marked",
    "report_text",
    "repro_names",
    "scope_patterns",
    "split_after_report",
    "verdict_of",
    "versions_between",
    "window_name",
]


def promote_fix_blame(
    candidates: list[Candidate], votes: list[Any], *, fix_pr: int, top: int = 5
) -> list[Candidate]:
    """The PRs the fix's rewritten lines blame go first, in
    `votes`' order (`fix_blame.rank_votes`: blame lines plus the scan's own
    score, out-of-window PRs last), each carrying `blamed_by_fix` (the fix,
    the weighted lines and the files); a blamed PR the report-side scan never
    reached is added from its blame row. The rest keep their order. (Blending
    blame into one score with the scan, measured on the benchmark's dev half,
    lost more culprits than it won: 24 → 20 top-1.)"""
    if not votes:
        return candidates
    by_number = {c.pr_number: c for c in candidates}
    lead: list[Candidate] = []
    for v in votes[:top]:
        c = by_number.get(v.pr_number) or Candidate(
            pr_number=v.pr_number,
            title=v.title,
            author=v.author,
            merged_at=v.merged_at,
            minor_version=v.first_release,
            url=v.url,
        )
        c.blamed_by_fix = {
            "fix_pr": fix_pr,
            "lines": round(float(v.lines), 1),
            "ranges": v.ranges,
            "paths": v.paths[:5],
        }
        lead.append(c)
    led = {c.pr_number for c in lead}
    return lead + [c for c in candidates if c.pr_number not in led]
