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
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

from server.code.domain.anchor import bare_name, mentions

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
# The changed files the brief's impact read seeds from; the stored paths section lists
# these plus the files of the diff's hunks, so a list this long may be cut.
# not a setting: CodeImpactInput.files' max_length
IMPACT_SEED_FILES = 50
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


@dataclass
class HunkLine:
    """One line inside a hunk: `marker` is `+`, `-` or ` `, `old_no` its old-side line
    number (`-` and ` ` lines), the paths those of the file it is in."""

    old_path: str
    new_path: str
    marker: str
    body: str
    old_no: int


@dataclass
class Hunks:
    """A unified diff read hunk by hunk: the new-side paths in diff order, every line
    inside a hunk, and whether a hunk's header miscounted its lines."""

    paths: list[str] = field(default_factory=list)
    lines: list[HunkLine] = field(default_factory=list)
    malformed: bool = False


_HUNK_HEADER = re.compile(r"@@ -(\d+)(?:,(\d+))? \+\d+(?:,(\d+))? @@")
# What may follow a hunk whose counted lines ran out (`--- ` starts the next file of a
# plain `diff -u` with no `diff` lines).
_AFTER_HUNK = ("diff ", "@@", "\\", "--- ")


@dataclass
class _Cursor:
    """Where a walk of a diff is: the file it is in and what is left of the hunk."""

    old_path: str = ""
    new_path: str = ""
    old_left: int = 0
    new_left: int = 0
    old_no: int = 0
    ended: bool = False  # a hunk's counts just ran out

    def in_hunk(self) -> bool:
        return self.old_left > 0 or self.new_left > 0


def read_hunks(diff: str) -> Hunks:
    """Walk `diff` hunk by hunk, counting each hunk's lines off its `@@ -a,b +c,d @@`
    header, so a file header (with or without `a/` and `b/`) is never read as a line and
    a line that looks like one (`--- x` removing a SQL comment) is. Lines split on `\\n`
    only. `malformed` when a header miscounts: lines go on after its counts ran out, or
    the next file starts, or the diff ends, before they did."""
    out = Hunks()
    cur = _Cursor()
    lines = diff.split("\n")
    if lines and lines[-1] == "":
        lines.pop()  # the newline that ends the last line
    for line in lines:
        if cur.in_hunk() and _hunk_line(out, cur, line):
            continue
        _between_hunks(out, cur, line)
    if cur.in_hunk():
        out.malformed = True  # the diff ended inside a hunk
    return out


def _hunk_line(out: Hunks, cur: _Cursor, line: str) -> bool:
    """Read one line inside a hunk; False when it ends the hunk early (the next file
    starts, or a line no hunk can hold), so the caller reads it as a header."""
    if line.startswith("\\"):
        return True  # "\ No newline at end of file"
    marker = line[:1] or " "
    if marker not in "+- ":  # `diff --git` of the next file among them
        out.malformed = True
        cur.old_left = cur.new_left = 0
        return False
    out.lines.append(HunkLine(cur.old_path, cur.new_path, marker, line[1:], cur.old_no))
    if marker != "+":
        cur.old_left -= 1
        cur.old_no += 1
    if marker != "-":
        cur.new_left -= 1
    if cur.old_left < 0 or cur.new_left < 0:
        out.malformed = True
    cur.ended = not cur.in_hunk()
    return True


def _between_hunks(out: Hunks, cur: _Cursor, line: str) -> None:
    """Read one line outside a hunk: a file header, a hunk header, or anything else."""
    if cur.ended and line.startswith("\\"):
        return  # "\ No newline at end of file" after the hunk's last line
    if cur.ended and not line.startswith(_AFTER_HUNK):
        out.malformed = True  # lines go on after the counts ran out
    cur.ended = False
    if line.startswith("--- "):
        p = line[4:].strip()
        cur.old_path = "" if p == "/dev/null" else (p[2:] if p.startswith("a/") else p)
    elif line.startswith("+++ "):
        p = line[4:].strip()
        cur.new_path = "" if p == "/dev/null" else (p[2:] if p.startswith("b/") else p)
        if cur.new_path and cur.new_path not in out.paths:
            out.paths.append(cur.new_path)
    elif line.startswith("@@"):
        m = _HUNK_HEADER.match(line)
        if m is None:
            out.malformed = True
            return
        cur.old_no = int(m.group(1))
        cur.old_left = int(m.group(2) if m.group(2) is not None else 1)
        cur.new_left = int(m.group(3) if m.group(3) is not None else 1)
        cur.ended = not cur.in_hunk()


def parse_diff(diff: str) -> DiffFacts:
    """One pass over the unified diff's hunks (`read_hunks`). Deleted lines are tracked
    by their OLD-side number and folded into runs, so the blame section reads exactly
    what the PR removed — the lines to blame — not the hunk's span with its context."""
    hunks = read_hunks(diff)
    facts = DiffFacts(paths=list(hunks.paths))
    seen_settings: dict[str, str] = {}
    for h in hunks.lines:
        if h.marker == " ":
            continue
        if h.marker == "-" and h.old_path:
            _add_deleted(facts.deleted.setdefault(h.old_path, []), h.old_no)
        for name, source in _setting_names(h.body, marker=h.marker, path=h.new_path or h.old_path):
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
# decision.record's value closes its rejected options with the realized outcome
_REALIZED = "Realized:"
_SETTING_NAME = re.compile(r"[a-z][a-z0-9]*(?:_[a-z0-9]+)+")


def checked_names(text: str, *, constraint: bool) -> list[str]:
    """The names the token check looks for in a PR's diff: the code-shaped names of the
    row's `Rejected:` clause, up to a `Realized:` one (backticked, qualified like `A::b`,
    or calls like `f()`) and, for a constraint, the snake_case names anywhere in its
    value, backticked or not."""
    names: list[str] = []
    # `mentions` caps how many it returns; no text holds more names than characters
    cap = len(text)
    rejected = _REJECTED.search(text)
    if rejected:
        options = rejected.group(1).split(_REALIZED, 1)[0]
        names += [m.name for m in mentions(options, cap=cap) if not m.in_prose]
    if constraint:
        # `Setting::distinct_spill_ratio` is the setting `distinct_spill_ratio`
        names += [
            m.name for m in mentions(text, cap=cap) if _SETTING_NAME.fullmatch(bare_name(m.name))
        ]
    return list(dict.fromkeys(names))


def added_text(diff: str) -> tuple[str, bool]:
    """(the lines a unified diff adds without their `+`, whether a hunk miscounted)."""
    hunks = read_hunks(diff)
    return "\n".join(h.body for h in hunks.lines if h.marker == "+"), hunks.malformed


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


def row_names(d: dict[str, Any]) -> list[str]:
    """The names the token check looks for in a `decisions_for_paths` row."""
    return checked_names(d["text"], constraint="constraint" in d["tags"])


def has_check(d: dict[str, Any]) -> bool:
    """Whether the token check has something to look at in a `decisions_for_paths` row:
    names to look for, or a value cut at the read that may hide some."""
    return bool(d.get("value_truncated") or row_names(d))


def _checked_row(d: dict[str, Any], added: str, *, unsure: bool) -> dict[str, Any]:
    """One served row with its token check against the `added` lines: `check` when the
    diff adds one of its names; else `unknown` when `unsure` (the diff could not be read
    whole: unread, cut or malformed) or the row's value was cut at the read, `clear`
    otherwise; '' when it has nothing to check."""
    constraint = "constraint" in d["tags"]
    value_cut = bool(d.get("value_truncated"))
    names = row_names(d)
    found = names_in(added, names) if names else []
    if not names and not value_cut:
        verdict = ""
    elif found:
        verdict = "check"
    elif unsure or value_cut:
        verdict = "unknown"
    else:
        verdict = "clear"
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
    return row


def rationale_section(
    decisions: list[dict[str, Any]],
    diff: str | None,
    *,
    diff_cut: bool = False,
    paths_left: int = 0,
    left_out: Sequence[dict[str, Any]] = (),
    paths_full: int = 0,
    links_skipped: int = 0,
    refused: bool = False,
    paths_maybe_cut: bool = False,
    no_repo: bool = False,
    no_paths: bool = False,
    budget_tokens: int,
) -> dict[str, Any]:
    """The `rationale` section over the rows `decisions_for_paths` read and the PR's diff
    (None: it could not be read): each row's token check (`_checked_row`), `check` rows
    first, then constraints, then the rest, whole rows up to `budget_tokens` (the first
    always). The verdict reads every row: check | unknown | clear | none (no row has
    anything to check). It is `clear` or `none` only when every changed path and every
    matching row was checked; otherwise `unknown`, each flag and count naming its cause
    (the causes table on the review-brief page of the docs)."""
    added, malformed = added_text(diff) if diff is not None else (None, False)
    malformed = malformed and not diff_cut  # a cut diff ends inside a hunk anyway
    unsure = added is None or diff_cut or malformed
    rows = [_checked_row(d, added or "", unsure=unsure) for d in decisions]
    rows.sort(key=lambda r: (r["verdict"] != "check", r["kind"] != "constraint"))
    verdicts = {r["verdict"] for r in rows}
    verdict = next((v for v in ("check", "unknown", "clear") if v in verdicts), "none")
    left_to_check = sum(1 for d in left_out if has_check(d))
    counts = (paths_left, paths_full, links_skipped, left_to_check)
    flags = (no_repo, no_paths, refused, paths_maybe_cut)
    incomplete = any(flags) or any(n > 0 for n in counts)
    if incomplete and verdict in ("clear", "none"):
        verdict = "unknown"
    causes = ["no_repo"] if no_repo else []
    if no_paths:
        causes.append("no_paths")
    if refused:
        causes.append("trunk_not_readable")
    if diff is None and "unknown" in verdicts:
        causes.append("diff_unread")
    if diff_cut:
        causes.append("diff_cut")
    if malformed:
        causes.append("diff_malformed")
    if any(d.get("value_truncated") for d in decisions):
        causes.append("value_cut")
    if paths_maybe_cut:
        causes.append("paths_maybe_cut")
    if paths_left > 0:
        causes.append(f"paths_not_read:{paths_left}")
    if left_to_check:
        causes.append(f"rows_not_read:{left_to_check}")
    if paths_full > 0:
        causes.append(f"paths_at_row_ceiling:{paths_full}")
    if links_skipped > 0:
        causes.append(f"links_not_read:{links_skipped}")
    kept = rows[:1]
    for r in rows[1:]:
        if estimate_tokens([*kept, r]) > budget_tokens:
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
    "IMPACT_SEED_FILES",
    "NOT_BOUND",
    "NOT_BUILT",
    "OPTIONAL_SECTIONS",
    "RATIONALE",
    "RESTRICTED",
    "SECTION_ORDER",
    "SERVED",
    "SERVED_ORDER",
    "TIERS",
    "TIER_BUDGET_TOKENS",
    "BriefInputs",
    "DiffFacts",
    "HunkLine",
    "Hunks",
    "added_text",
    "apply_budget",
    "canonical",
    "checked_names",
    "decisions_of",
    "deleted_ranges",
    "has_check",
    "names_in",
    "parse_diff",
    "rationale_section",
    "read_hunks",
    "row_names",
    "serve",
    "source_stems",
    "tier_sections",
]
