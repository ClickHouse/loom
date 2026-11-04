"""A merged PR's review brief: every section a senior reviewer checks before reading the
diff, assembled ONCE from the code plane's own ops and stored as canonical JSON in
`loom.code_review_briefs`, keyed (org, namespace, pr_number, merge_sha).
`code.review_brief` is a point read of that row; a miss builds the brief on demand and
stores it; the ingest step that writes the PR's context pack builds it for every newly
merged PR.

The brief extends the pack, it does not replace it: same trigger (a merge
subject naming `#N`), same inputs (the PR's changed paths, the diff on the
mirror, the namespace head), the same QA gating (a restricted tracker namespace
never feeds a row cached under a code namespace). The pack is prose for a
model; the brief is the same ground as sectioned facts, each with a
verdict — the measured rule on the consumer side is "facts as decisions,
not lead lists", so a section says `uncovered` / `randomized` /
`regressions` / `test_only` and shows the rows behind it, never a list of
things to go and look at.

Sections, each `{status, verdict, epistemic, causes, rows, rows_total,
tokens, truncated}`:

- `paths` — the class of every changed path and whether
                the change is test-only;
- `symbols`   — the functions the hunks touch, with hop-1 call sites and
                weekly fan-in;
- `tests`     — a coverage claim per touched function and setting, and the
                test file this PR's fingerprint belongs in;
- `settings`  — the facts of every setting the diff declares, changes in the
                history file or uses in a new stateless test;
- `history`   — each touched function's regression record;
- `blame`     — the merged PRs that last touched the lines this PR deleted
                at the parent of the merge commit;
- `qa`        — the tracker record at the PR and its symbols, when bound;
- `risk`      — two calibrated priors: `jit_risk`, the per-repository
                logistic calibration of lines added, files changed and the
                author's revert prior, and `author_revert_prior` itself, both
                scored from the fit the weekly pass stored; ranking signals,
                never a gate, so no `decisions` bit reads them;
- `do_not_flag` — reserved; always `not_built`;
- `rationale` — the design decisions and constraints of the repository's trunk code
                namespace whose files or code links touch the PR's files, each with a
                token check against the diff; read on every call and never stored
                (`rationale_section`).

Tiers select sections and a token budget — trivial 700 (paths + symbols),
standard 2,500 (+ tests, settings, history, risk, rationale), full 4,000 (+ blame,
qa) — and
the budget is applied across the tier's sections by WHOLE rows, largest
section first; a trimmed section keeps its verdict and says `truncated`.

Pure helpers (diff parsing, section verdicts, tiering, canonical JSON) are
module functions; the reads go through the caller's `call(op, body)` — the op
handlers themselves, injected by the handler and the ingest worker
(`server.code.app.review_brief.call_code_op`) — so the brief is
byte-for-byte what a consumer would have assembled from the ops, and a stub
`call` tests the assembly offline.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from server.code.domain.anchor import bare_name, mentions
from server.settings import settings

logger = logging.getLogger(__name__)

PAYLOAD_VERSION = 1

SECTION_ORDER: tuple[str, ...] = (
    "paths",
    "symbols",
    "tests",
    "settings",
    "history",
    "blame",
    "tracker",
    "risk",
    "do_not_flag",
)
# A section each read computes and no build stores: the decisions it lists change after
# the merge, and a stored copy would serve them as they were.
RATIONALE = "rationale"
SERVED_ORDER: tuple[str, ...] = (*SECTION_ORDER, RATIONALE)
TIERS: dict[str, tuple[str, ...]] = {
    "trivial": ("paths", "symbols", "do_not_flag"),
    "standard": (
        "paths",
        "symbols",
        "tests",
        "settings",
        "history",
        "risk",
        "do_not_flag",
        RATIONALE,
    ),
    "full": SERVED_ORDER,
}
# Sections a stored payload may lack (built before the section existed, or never stored):
# a read serves them only from a payload that has them, or when asked for by name.
OPTIONAL_SECTIONS: tuple[str, ...] = ("risk", RATIONALE)
TIER_BUDGET_TOKENS: dict[str, int] = {"trivial": 700, "standard": 2500, "full": 4000}
DEFAULT_TIER = "standard"

# Section statuses.
SERVED = "served"  # rows present
EMPTY = "empty"  # built; nothing to say (e.g. no setting touched)
NOT_BUILT = "not_built"  # a reserved section (do_not_flag)
FAILED = "failed"  # the op raised; the brief fails open to absence
NOT_BOUND = "not_bound"  # qa: no tracker namespace bound to this code namespace
RESTRICTED = "restricted"  # qa: bound namespace is restricted; never cached here

# Per-PR read caps: the brief is built once, but on the ingest path it sits
# between a merge and the watermark advance, so every fan-out is bounded.
MAX_SYMBOL_ROWS = 30
MAX_COVERAGE_SYMBOLS = 6
MAX_HISTORY_SYMBOLS = 6
MAX_SETTINGS = 8
MAX_COVERAGE_SETTINGS = 4
MAX_BLAME_RANGES = 5
MAX_DIFF_CHARS = 200_000  # CodeImpactInput.diff's cap
MAX_BLAME_RANGE_LINES = 400  # code.blame_range's own cap

CallOp = Callable[[str, dict[str, Any]], Awaitable[dict[str, Any]]]

_SETTINGS_FILES = (
    "src/Core/Settings.cpp",
    "src/Storages/MergeTree/MergeTreeSettings.cpp",
    "src/Core/ServerSettings.cpp",
    "src/Core/FormatFactorySettings.h",
)
_HISTORY_FILE = "src/Core/SettingsChangesHistory.cpp"
_STATELESS = "tests/queries/0_stateless/"
_DECL_RE = re.compile(r"DECLARE\w*\(\s*\w+\s*,\s*([a-z][a-z0-9_]{2,80})\s*,")
_OBSOLETE_RE = re.compile(r"MAKE_OBSOLETE\w*\(\s*\w+\s*,\s*\w+\s*,\s*([a-z][a-z0-9_]{2,80})\s*,")
_HISTORY_RE = re.compile(r'\{\s*"([a-z][a-z0-9_]{2,80})"\s*,')
_TEST_SET_RE = re.compile(r"(?i)\b(?:SET|SETTINGS)\s+([a-z][a-z0-9_]{2,80})\s*=")
_TEST_SET_MORE_RE = re.compile(r",\s*([a-z][a-z0-9_]{2,80})\s*=")


# ---------------------------------------------------------------------------
# Pure: diff parsing
# ---------------------------------------------------------------------------


@dataclass
class DiffFacts:
    """What the brief reads off the unified diff: the paths (new side), the
    OLD-side ranges of deleted lines per path (for blame at the parent), the
    setting names the diff declares / changes / uses (with the class of the
    evidence), and the bare source stems for the test-home fingerprint."""

    paths: list[str] = field(default_factory=list)
    deleted: dict[str, list[tuple[int, int]]] = field(default_factory=dict)
    settings: list[tuple[str, str]] = field(default_factory=list)  # (name, source)


def parse_diff(diff: str) -> DiffFacts:
    """One pass over the unified diff. Deleted lines are tracked by their
    OLD-side number through each hunk (`-` lines advance the old cursor,
    context lines too, `+` lines do not) and folded into runs, so the blame
    section reads exactly what the PR removed — the lines to blame — not the
    hunk's span with its context."""
    facts = DiffFacts()
    old_path = new_path = ""
    old_cursor = 0
    in_hunk = False
    seen_settings: dict[str, str] = {}
    for line in diff.splitlines():
        if line.startswith("diff --git "):
            in_hunk = False
            continue
        if line.startswith("--- "):
            p = line[4:].strip()
            old_path = "" if p == "/dev/null" else (p[2:] if p.startswith("a/") else p)
            in_hunk = False
            continue
        if line.startswith("+++ "):
            p = line[4:].strip()
            new_path = "" if p == "/dev/null" else (p[2:] if p.startswith("b/") else p)
            if new_path and new_path not in facts.paths:
                facts.paths.append(new_path)
            in_hunk = False
            continue
        if line.startswith("@@"):
            m = re.match(r"@@ -(\d+)(?:,(\d+))? \+\d+(?:,\d+)? @@", line)
            in_hunk = bool(m)
            old_cursor = int(m.group(1)) if m else 0
            continue
        if not in_hunk:
            continue
        if line.startswith("\\"):
            continue  # "\ No newline at end of file"
        marker = line[:1]
        if marker == " " or line == "":
            old_cursor += 1
            continue
        if marker == "-":
            if old_path:
                _add_deleted(facts.deleted.setdefault(old_path, []), old_cursor)
            old_cursor += 1
        elif marker != "+":
            continue
        for name, source in _setting_names(line[1:], marker=marker, path=new_path or old_path):
            seen_settings.setdefault(name, source)
    facts.settings = list(seen_settings.items())
    return facts


def _setting_names(body: str, *, marker: str, path: str) -> list[tuple[str, str]]:
    """The setting names one changed line names, with the class of the
    evidence: `declared` / `obsoleted` in a settings-defining file, `history`
    in the changes-history file, `test_uses` for a `SET a = …, b = …` or
    `SETTINGS a = …` an ADDED stateless-test line carries."""
    names: list[tuple[str, str]] = []
    if path in _SETTINGS_FILES:
        names += [(n, "declared") for n in _DECL_RE.findall(body)]
        names += [(n, "obsoleted") for n in _OBSOLETE_RE.findall(body)]
    elif path == _HISTORY_FILE:
        names += [(n, "history") for n in _HISTORY_RE.findall(body)]
    elif marker == "+" and path.startswith(_STATELESS) and path.endswith((".sql", ".sh")):
        for m in _TEST_SET_RE.finditer(body):
            names.append((m.group(1), "test_uses"))
            tail = body[m.end() :].split(";")[0]
            names += [(n, "test_uses") for n in _TEST_SET_MORE_RE.findall(tail)]
    return names


def _add_deleted(runs: list[tuple[int, int]], line_no: int) -> None:
    """Fold a deleted line into the last run when it continues it (a gap of
    one context line still counts as the same hunk of removal)."""
    if runs and line_no <= runs[-1][1] + 2:
        runs[-1] = (runs[-1][0], max(runs[-1][1], line_no))
    else:
        runs.append((line_no, line_no))


def deleted_ranges(
    facts: DiffFacts,
    *,
    src_only: Callable[[str], bool],
    cap: int = MAX_BLAME_RANGES,
) -> list[tuple[str, int, int]]:
    """The old-side deleted ranges worth blaming: source paths only, each
    range clamped to blame's line cap, largest first, at most `cap`. The
    deleted lines are what blame reads — what the fix replaced."""
    out: list[tuple[str, int, int]] = []
    for path, ranges in facts.deleted.items():
        if not src_only(path):
            continue
        for start, end in ranges:
            end = min(end, start + MAX_BLAME_RANGE_LINES - 1)
            out.append((path, start, end))
    out.sort(key=lambda r: (-(r[2] - r[1] + 1), r[0], r[1]))
    return out[:cap]


def source_stems(paths: list[str]) -> list[str]:
    """Bare file stems of the non-test source paths, for the test-home
    fingerprint (`MergeTreeData` from src/Storages/MergeTree/MergeTreeData.cpp)."""
    from server.code.domain.index.test_gate import is_test_path

    stems: list[str] = []
    for p in paths:
        if is_test_path(p) or not p.startswith(("src/", "base/", "programs/")):
            continue
        stem = p.rsplit("/", 1)[-1].split(".", 1)[0]
        if stem and stem not in stems:
            stems.append(stem)
    return stems


# ---------------------------------------------------------------------------
# Pure: sections, tokens, tiers
# ---------------------------------------------------------------------------


def canonical(value: Any) -> str:
    """Byte-stable JSON: sorted keys, no whitespace, UTF-8 as is."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def estimate_tokens(value: Any) -> int:
    from server.code.domain.units import estimate_tokens as _est

    return _est(canonical(value))


def section(
    status: str,
    *,
    verdict: str = "",
    epistemic: str = "",
    causes: list[str] | None = None,
    rows: list[dict[str, Any]] | None = None,
    note: str = "",
) -> dict[str, Any]:
    rows = rows or []
    out: dict[str, Any] = {
        "status": status if rows or status != SERVED else EMPTY,
        "verdict": verdict,
        "epistemic": epistemic,
        "causes": list(causes or []),
        "rows": rows,
        "rows_total": len(rows),
        "tokens": estimate_tokens(rows),
        "truncated": False,
    }
    if note:
        out["note"] = note
    return out


def failed_section(op: str, exc: BaseException) -> dict[str, Any]:
    return section(FAILED, verdict="unknown", causes=[f"{op}:{type(exc).__name__}"])


def tier_sections(
    tier: str,
    requested: list[str],
    *,
    present: set[str] | None = None,
) -> list[str]:
    """The sections a read serves: the tier's set, narrowed to `requested`
    when given (unknown names ignored), in SERVED_ORDER. An optional
    section (`risk`, `rationale`) is served from a payload that has it, or when
    asked for by name — never as a placeholder on a brief built before it existed."""
    allowed = set(TIERS.get(tier, TIERS[DEFAULT_TIER]))
    if requested:
        allowed &= {s for s in requested if s in SERVED_ORDER}
    else:
        for opt in OPTIONAL_SECTIONS:
            if present is not None and opt not in present:
                allowed.discard(opt)
    return [s for s in SERVED_ORDER if s in allowed]


def apply_budget(
    sections: dict[str, dict[str, Any]],
    names: list[str],
    budget: int,
) -> tuple[dict[str, dict[str, Any]], int]:
    """Trim whole rows, largest section first, until the served sections fit
    the budget. Every section keeps its verdict (and `rows_total`); a
    trimmed one says `truncated`. Deterministic: ties break on SECTION_ORDER."""
    out: dict[str, dict[str, Any]] = {}
    for n in names:
        sec = sections.get(n) or section(
            NOT_BUILT,
            verdict="",
            note="the build had no such section",
        )
        out[n] = {**sec, "rows": list(sec.get("rows") or [])}
        out[n]["tokens"] = estimate_tokens(out[n]["rows"])
    total = sum(int(s["tokens"]) for s in out.values())
    while total > budget:
        biggest = max(
            (n for n in names if out[n]["rows"]),
            key=lambda n: (int(out[n]["tokens"]), -names.index(n)),
            default=None,
        )
        if biggest is None:
            break
        out[biggest]["rows"].pop()
        out[biggest]["truncated"] = True
        out[biggest]["tokens"] = estimate_tokens(out[biggest]["rows"])
        total = sum(int(s["tokens"]) for s in out.values())
    return out, total


def decisions_of(sections: dict[str, dict[str, Any]]) -> dict[str, bool]:
    """The one-bit answers a consumer routes on, read off the sections'
    verdicts — never off their rows, so a trimmed brief carries the same
    decisions as the full one."""
    v = {n: str((sections.get(n) or {}).get("verdict") or "") for n in SECTION_ORDER}
    # `risk` is a ranking signal, never a gate — no bit reads it
    out = {
        "test_only": v["paths"] == "test_only",
        "symbol_with_regressions": v["history"] == "regressions",
        "uncovered_symbol": v["tests"] == "uncovered",
        "randomized_setting": v["settings"] == "randomized",
        "deleted_lines_blamed": v["blame"] == "candidates",
    }
    return out


def paths_verdict(rows: list[dict[str, Any]]) -> str:
    classes = {str(r.get("class") or "") for r in rows}
    if not classes:
        return "no_paths"
    if classes == {"test"}:
        return "test_only"
    if "src" not in classes:
        return "no_src"
    return "src"


def tests_verdict(rows: list[dict[str, Any]]) -> str:
    claims = [r for r in rows if r.get("kind") in ("symbol", "setting")]
    if not claims:
        return "no_subjects"
    verdicts = {str(r.get("verdict") or "") for r in claims}
    if "uncovered" in verdicts:
        return "uncovered"
    if verdicts <= {"covered_by"}:
        return "covered"
    if "covered_by" in verdicts:
        return "partly_covered"
    # Tests were found for every subject, none names one
    if "weak_evidence" in verdicts and verdicts <= {"weak_evidence", "unknown"}:
        return "weak_evidence"
    return "unknown"


def settings_verdict(rows: list[dict[str, Any]]) -> str:
    if not rows:
        return "none"
    if any(bool(r.get("randomized_in_ci")) for r in rows):
        return "randomized"
    verdicts = {str(r.get("verdict") or "") for r in rows}
    if "known" in verdicts:
        return "known"
    if verdicts <= {"not_a_setting"}:
        return "not_a_setting"
    return "unknown"


def history_verdict(rows: list[dict[str, Any]]) -> str:
    """`regressions` only on function-level evidence — a fix PR that NAMES
    the function (title, body or a review comment in its span; QA's diff
    match when bound), a revert, or an issue traced back to a PR touching
    it. A fix that merely touched the file is `file_fixes`: on a hot file
    nearly every window holds one, so it decides nothing. `clean` when the
    window holds neither."""
    if not rows:
        return "no_symbols"
    if any(
        int(r.get("named_fix_prs") or 0) > 0
        or int(r.get("reverts") or 0) > 0
        or int(r.get("caused_issues") or 0) > 0
        for r in rows
    ):
        return "regressions"
    if any(int(r.get("fix_prs") or 0) > 0 for r in rows):
        return "file_fixes"
    verdicts = {str(r.get("verdict") or "") for r in rows}
    if verdicts <= {"unknown"}:
        return "unknown"
    return "clean"


def blame_verdict(rows: list[dict[str, Any]], *, ranges: int) -> str:
    if not ranges:
        return "no_deleted_lines"
    if any(int(r.get("latest_pr") or 0) > 0 or r.get("candidates") for r in rows):
        return "candidates"
    if rows and all(str(r.get("verdict") or "") == "no_candidates" for r in rows):
        return "no_candidates"
    return "unknown"


# ---------------------------------------------------------------------------
# Pure: the rationale section
# ---------------------------------------------------------------------------

_REJECTED = re.compile(r"\bRejected:(.*)", re.S)
_APPLIES_TO = "Applies to "
_SETTING_NAME = re.compile(r"[a-z][a-z0-9]*(?:_[a-z0-9]+)+")


def checked_names(text: str, *, constraint: bool) -> list[str]:
    """The names the token check looks for in a PR's diff: the backticked names of a
    row's rejected options and, for a constraint, the setting names it states. The
    closing `Applies to` clause names the code the row governs, which a PR touching that
    code names anyway, so it is not read."""
    head = text.split(_APPLIES_TO, 1)[0]
    names: list[str] = []
    rejected = _REJECTED.search(head)
    if rejected:
        names += [m.name for m in mentions(rejected.group(1)) if not m.in_prose]
    if constraint:
        names += [m.name for m in mentions(head) if _SETTING_NAME.fullmatch(m.name)]
    return list(dict.fromkeys(names))


def added_text(diff: str) -> str:
    """The lines a unified diff adds, without their `+`."""
    return "\n".join(
        line[1:]
        for line in diff.splitlines()
        if line.startswith("+") and not line.startswith(("+++ b/", "+++ /dev/null"))
    )


def names_in(added: str, names: list[str]) -> list[str]:
    """The `names` the added text spells as a whole word: the name itself, or its last
    part when that is four characters or more (`DB::HashSet` written as `HashSet`)."""
    found = []
    for name in names:
        forms = {name}
        if len(bare_name(name)) >= 4:
            forms.add(bare_name(name))
        if any(re.search(rf"(?<!\w){re.escape(f)}(?!\w)", added) for f in forms):
            found.append(name)
    return found


def rationale_section(
    decisions: list[dict[str, Any]],
    diff: str | None,
    *,
    diff_cut: bool = False,
    paths_left: int = 0,
) -> dict[str, Any]:
    """The `rationale` section over the rows `decisions_for_paths` read and the PR's diff
    (None: it could not be read). A row whose checked names the diff adds says `check`;
    one with checked names none of which it adds says `clear`; one with nothing to check
    says ''. Constraints first, `check` rows first among them, whole rows up to
    `code_review_brief_rationale_tokens` (the first always); the verdict reads every row:
    check | unknown (a diff that could not be read) | clear | none."""
    added = added_text(diff) if diff is not None else None
    rows = []
    for d in decisions:
        constraint = "constraint" in d["tags"]
        names = checked_names(d["text"], constraint=constraint)
        found = names_in(added, names) if names and added is not None else []
        if not names:
            verdict = ""
        elif added is None:
            verdict = "unknown"
        else:
            verdict = "check" if found else "clear"
        row = {
            "memory_key": d["memory_key"],
            "kind": "constraint" if constraint else "decision",
            "pinned": bool(d["pinned"]),
            "code_state": d["code_state"],
            "verdict": verdict,
            "line": d["line"],
        }
        if found:
            row["names"] = found
        rows.append(row)
    rows.sort(key=lambda r: (r["kind"] != "constraint", r["verdict"] != "check"))
    verdicts = {r["verdict"] for r in rows}
    if not rows:
        verdict = "none"
    elif "check" in verdicts:
        verdict = "check"
    elif "unknown" in verdicts:
        verdict = "unknown"
    else:
        verdict = "clear"
    causes = []
    if "unknown" in verdicts:
        causes.append("diff_unread")
    if diff_cut:
        causes.append("diff_cut")
    if paths_left > 0:
        causes.append(f"paths_not_read:{paths_left}")
    kept = rows[:1]
    for r in rows[1:]:
        if estimate_tokens([*kept, r]) > settings.code_review_brief_rationale_tokens:
            break
        kept.append(r)
    out = section(SERVED, verdict=verdict, epistemic="lower_bound", causes=causes, rows=kept)
    out["rows_total"] = len(rows)
    out["truncated"] = len(kept) < len(rows)
    return out


# ---------------------------------------------------------------------------
# Assembly
# ---------------------------------------------------------------------------


@dataclass
class BriefInputs:
    org: str
    ns: str
    pr_number: int
    merge_sha: str
    title: str
    changed_paths: list[str]
    diff: str
    parent_sha: str = ""
    indexed_commit: str = ""
    path_prefixes: tuple[str, ...] = ()
    author: str = ""  # The PR author's login, for the revert prior


# ---------------------------------------------------------------------------
# Store + serve
# ---------------------------------------------------------------------------


def serve(
    payload: dict[str, Any],
    *,
    tier: str,
    requested: list[str],
    budget: int = 0,
) -> tuple[dict[str, dict[str, Any]], int, int]:
    """(sections, tokens_used, budget) for one read of a stored payload."""
    tier = tier if tier in TIERS else DEFAULT_TIER
    budget = budget or TIER_BUDGET_TOKENS[tier]
    stored = dict(payload.get("sections") or {})
    names = tier_sections(tier, requested, present=set(stored))
    out, used = apply_budget(stored, names, budget)
    return out, used, budget


__all__ = [
    "DEFAULT_TIER",
    "EMPTY",
    "FAILED",
    "NOT_BOUND",
    "NOT_BUILT",
    "OPTIONAL_SECTIONS",
    "RATIONALE",
    "RESTRICTED",
    "SECTION_ORDER",
    "SERVED_ORDER",
    "SERVED",
    "TIERS",
    "TIER_BUDGET_TOKENS",
    "BriefInputs",
    "DiffFacts",
    "added_text",
    "apply_budget",
    "canonical",
    "checked_names",
    "decisions_of",
    "deleted_ranges",
    "names_in",
    "parse_diff",
    "rationale_section",
    "serve",
    "source_stems",
    "tier_sections",
]
