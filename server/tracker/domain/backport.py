"""Pure helpers for `code.backport_gap`: which release
branches still carry the bug a merged fix removed, and the exact PR to
cherry-pick there.

Three legs meet per branch:

- the RECORD — the tracker source's backport PRs (`is_backport_pr = 1`,
  `upstream_pr_number` or the "Backport #N to X:" title, `base_branch`),
  the fix's `minor_version` (the first release that contains it: a branch
  cut at or after it carries the fix natively), the issue's `fix_versions`;
  and loom's own ledger of the branch namespace, where a merged backport
  is a row titled "Backport #N to X";
- PRESENCE — the per-namespace present/absent verdict for the functions the
  fix's hunks touch (`@@ … @@ <enclosing declaration>` from the QA diff),
  falling back to the fix's source files on the branch's file table;
- the CHERRY-PICK — the fix's merge commit from loom's ledger.

The verdict per branch:

- `covered`      — a merged backport (record or ledger, `exact`), the branch
                   was cut at or after the fix's first release (`exact`), or
                   the issue's `fix_versions` names the branch (`lower_bound`:
                   the record names no PR);
- `pending`      — a backport PR exists and is open (`exact`);
- `gap`          — no backport, the branch predates the fix, and the touched
                   functions (or, without hunks, the touched files) are
                   present on the branch. Always `lower_bound`: a symbol's
                   presence says the code exists there, not that the bug
                   does — cherry-picks often need non-trivial edits, so
                   patch presence is the stronger test;
- `not_affected` — every touched function answered `absent` on an indexed
                   branch (or, without hunks, every touched file), `lower_bound`;
- `unknown`      — the record was unavailable while the code is present
                   (`tracker_unavailable`), presence hit the deadline or had no
                   index, or the branch has no version to match the record on.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from server.tracker.domain.bisect import GENERIC_NAMES, parse_version

GAP = "gap"
COVERED = "covered"
PENDING = "pending"
NOT_AFFECTED = "not_affected"
UNKNOWN = "unknown"
VERDICTS = (GAP, PENDING, UNKNOWN, NOT_AFFECTED, COVERED)
# the order branches are listed in: what needs a hand first
_RANK = {v: i for i, v in enumerate(VERDICTS)}

# "Backport #115670 to 26.7: Fix …" — the upstream number and the branch
BACKPORT_TITLE_RE = re.compile(r"^\s*Backport\s+#(\d+)\s+to\s+(\d{2}\.\d{1,2})\b")
# `std::unique_ptr<X> IDataType::getSubcolumnInfo(` → the qualified name
# before the FIRST parenthesis; template arguments are dropped first
_TEMPLATE_ARGS_RE = re.compile(r"<[^<>]*>")
_DECL_NAME_RE = re.compile(r"([A-Za-z_][A-Za-z0-9_]*(?:::~?[A-Za-z_][A-Za-z0-9_]*)*)\s*\($")
_TYPE_DECL_RE = re.compile(
    r"\b(?:class|struct|enum(?:\s+class)?|union)\s+([A-Za-z_][A-Za-z0-9_:]*)"
)
_PY_DEF_RE = re.compile(r"^\s*(?:async\s+)?def\s+([A-Za-z_][A-Za-z0-9_]*)\s*\(")
_PY_CLASS_RE = re.compile(r"^\s*class\s+([A-Za-z_][A-Za-z0-9_]*)")
_MIN_NAME = 4
_MAX_SYMBOLS = 8
_MAX_FILES = 40
TEST_PREFIXES = ("tests/", "test/", "utils/", "docs/", "ci/")


# ---- what the fix touched --------------------------------------------------------


def parse_backport_title(title: str) -> tuple[int, str]:
    """`Backport #115670 to 26.7: …` → (115670, "26.7"); (0, "") otherwise."""
    m = BACKPORT_TITLE_RE.match(title or "")
    if not m:
        return 0, ""
    return int(m.group(1)), m.group(2)


def hunk_symbols(contexts: list[str], *, limit: int = _MAX_SYMBOLS) -> list[str]:
    """The declarations the hunk headers name, most specific form first:
    `IDataType::getSubcolumnInfo` from a C++ definition line, `ISerialization`
    from a `class` line, `read_prefix` from a Python `def`. Generic or
    short names and `test_*` never; order of first appearance; at most
    `limit` (every symbol is one presence fan-out)."""
    out: list[str] = []
    for raw in contexts:
        name = decl_name(str(raw or ""))
        if not name:
            continue
        bare = name.split("::")[-1].lstrip("~")
        if len(bare) < _MIN_NAME or bare.lower() in GENERIC_NAMES or bare.startswith("test_"):
            continue
        if name not in out:
            out.append(name)
        if len(out) >= limit:
            break
    return out


def decl_name(context: str) -> str:
    text = context.strip()
    if not text:
        return ""
    m = _PY_DEF_RE.match(text) or _PY_CLASS_RE.match(text)
    if m:
        return m.group(1)
    m = _TYPE_DECL_RE.search(text)
    if m and "(" not in text.split(m.group(0), 1)[0]:
        return m.group(1).strip(":")
    if "(" not in text:
        # git cuts the context at 80 bytes: a declaration whose parameter list
        # is gone may have lost the tail of its NAME too — never a lookup
        return ""
    head = text.split("(", 1)[0]
    # `Foo<Bar>::baz` → `Foo::baz`; nested templates fall in two passes
    for _ in range(3):
        head = _TEMPLATE_ARGS_RE.sub("", head)
    m = _DECL_NAME_RE.search(head.rstrip() + "(")
    if not m:
        return ""
    name = m.group(1)
    # a control-flow line git sometimes prints as context (`if (`, `for (`)
    if name.lower() in ("if", "for", "while", "switch", "return", "else", "catch", "sizeof"):
        return ""
    return name


def source_files(paths: list[str], *, limit: int = _MAX_FILES) -> list[str]:
    """The fix's source files: tests, docs and CI never (a test file is
    present on every branch the test suite runs on)."""
    out: list[str] = []
    for p in paths:
        s = str(p or "").strip()
        if not s or s.startswith(TEST_PREFIXES):
            continue
        if s not in out:
            out.append(s)
    return out[:limit]


# ---- versions --------------------------------------------------------------------


def version_key(text: str) -> tuple[int, int] | None:
    return parse_version(text)


def branch_contains(branch: str, fix_minor: str) -> bool | None:
    """Whether a branch cut as release `branch` carries a PR whose first
    release is `fix_minor`: True when branch ≥ fix_minor. None when either
    side is unparseable, or the fix has no first release yet (an unstamped
    PR is on master only — newer than every branch)."""
    b, f = version_key(branch), version_key(fix_minor)
    if b is None or f is None:
        return None
    return b >= f


# ---- the verdict ------------------------------------------------------------------


@dataclass
class BranchInputs:
    namespace: str
    branch: str  # release version the namespace tracks ("26.4"); "" when unknown
    # backport rows for THIS branch: {pr_number, state open|merged|closed, url,
    # merged_at, source qa|ledger, merge_sha}
    backports: list[dict[str, Any]] = field(default_factory=list)
    # symbol → present|absent|unknown; {} when no hunks
    symbols: dict[str, str] = field(default_factory=dict)
    symbol_reasons: dict[str, str] = field(default_factory=dict)
    files_present: list[str] = field(default_factory=list)
    files_absent: list[str] = field(default_factory=list)
    files_checked: bool = False
    indexed: bool = True  # the branch's file table answered under a watermark


@dataclass
class BranchVerdict:
    namespace: str
    branch: str
    verdict: str
    epistemic: str  # exact | lower_bound
    basis: str  # backport_pr | ledger | in_branch | fix_versions | symbol | file | ""
    reason: str = ""
    backport: dict[str, Any] = field(default_factory=dict)
    presence: dict[str, Any] = field(default_factory=dict)
    cherry_pick: dict[str, Any] = field(default_factory=dict)
    evidence: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "namespace": self.namespace,
            "branch": self.branch,
            "verdict": self.verdict,
            "epistemic": self.epistemic,
            "basis": self.basis,
            "reason": self.reason,
            "backport": self.backport,
            "presence": self.presence,
            "cherry_pick": self.cherry_pick,
            "evidence": self.evidence,
        }


def _pick_backport(rows: list[dict[str, Any]], state: str) -> dict[str, Any]:
    for r in sorted(rows, key=lambda x: (x.get("source") != "qa", -int(x.get("pr_number") or 0))):
        if str(r.get("state") or "") == state:
            return dict(r)
    return {}


def _presence_block(inp: BranchInputs) -> dict[str, Any]:
    by = {
        v: sorted(s for s, x in inp.symbols.items() if x == v)
        for v in ("present", "absent", "unknown")
    }
    block: dict[str, Any] = {
        "symbols_present": by["present"],
        "symbols_absent": by["absent"],
        "symbols_unknown": by["unknown"],
        "files_present": list(inp.files_present),
        "files_absent": list(inp.files_absent),
    }
    reasons = {s: r for s, r in inp.symbol_reasons.items() if r and inp.symbols.get(s) == "unknown"}
    if reasons:
        block["symbol_reasons"] = reasons
    return block


def verdict_for(
    inp: BranchInputs,
    *,
    fix_minor: str,
    fix_versions: list[str],
    qa_ok: bool,
    cherry_pick: dict[str, Any],
    fix_reverted: bool = False,
    backport_required: bool = False,
) -> BranchVerdict:
    """One branch's verdict from the record, presence and the fix's release.
    A reverted fix is `unknown(fix_reverted)` everywhere — never a pick; the
    maintainers' backport label rides as evidence on a gap, so a gap on a
    fix nobody meant to backport reads as the candidate it is."""
    ev: list[str] = []
    presence = _presence_block(inp)
    if fix_reverted:
        ev.append("the fix was reverted on the trunk; nothing to cherry-pick")
        return BranchVerdict(
            namespace=inp.namespace,
            branch=inp.branch,
            verdict=UNKNOWN,
            epistemic="exact",
            basis="",
            reason="fix_reverted",
            presence=presence,
            evidence=ev,
        )

    def done(
        verdict: str,
        epistemic: str,
        basis: str,
        *,
        reason: str = "",
        backport: dict[str, Any] | None = None,
        pick: bool = False,
    ) -> BranchVerdict:
        return BranchVerdict(
            namespace=inp.namespace,
            branch=inp.branch,
            verdict=verdict,
            epistemic=epistemic,
            basis=basis,
            reason=reason,
            backport=backport or {},
            presence=presence,
            cherry_pick=dict(cherry_pick) if pick else {},
            evidence=ev,
        )

    merged = _pick_backport(inp.backports, "merged")
    if merged:
        src = str(merged.get("source") or "qa")
        ev.append(f"backport #{merged.get('pr_number')} merged into {inp.branch} ({src} record)")
        return done(COVERED, "exact", "backport_pr" if src == "qa" else "ledger", backport=merged)
    contains = branch_contains(inp.branch, fix_minor)
    if contains:
        ev.append(f"branch {inp.branch} was cut at or after the fix's first release {fix_minor}")
        return done(COVERED, "exact", "in_branch")
    if inp.branch and inp.branch in fix_versions:
        ev.append(f"issue fix_versions names {inp.branch}; no backport PR named")
        return done(COVERED, "lower_bound", "fix_versions")
    open_pr = _pick_backport(inp.backports, "open")
    if open_pr:
        ev.append(f"backport #{open_pr.get('pr_number')} to {inp.branch} is open")
        return done(PENDING, "exact", "backport_pr", backport=open_pr)
    if not inp.branch:
        ev.append("the namespace tracks no release version; the record cannot be matched")
        return done(UNKNOWN, "lower_bound", "", reason="no_branch_version")
    if fix_minor and contains is None:
        ev.append(f"fix minor_version {fix_minor!r} is not a release")
    present = presence["symbols_present"]
    absent = presence["symbols_absent"]
    unknown = presence["symbols_unknown"]
    if inp.symbols:
        if present:
            ev.append(
                f"{len(present)} of {len(inp.symbols)} touched functions defined on {inp.branch}"
            )
            if not qa_ok:
                ev.append("the tracker record did not answer; a backport may exist")
                return done(UNKNOWN, "lower_bound", "symbol", reason="tracker_unavailable")
            ev.append("no backport PR in the record or the ledger; branch predates the fix")
            ev.append(_label_line(backport_required))
            return done(GAP, "lower_bound", "symbol", pick=True)
        if absent and not unknown:
            ev.append(f"all {len(absent)} touched functions absent from {inp.branch}'s index")
            return done(NOT_AFFECTED, "lower_bound", "symbol")
        reason = _first_reason(inp) or "no_index"
        ev.append(f"presence did not answer for {len(unknown)} function(s): {reason}")
        return done(UNKNOWN, "lower_bound", "symbol", reason=reason)
    if inp.files_checked:
        if inp.files_present:
            ev.append(f"{len(inp.files_present)} of the fix's source files present on {inp.branch}")
            if not qa_ok:
                ev.append("the tracker record did not answer; a backport may exist")
                return done(UNKNOWN, "lower_bound", "file", reason="tracker_unavailable")
            ev.append("no backport PR in the record or the ledger; branch predates the fix")
            ev.append(_label_line(backport_required))
            return done(GAP, "lower_bound", "file", pick=True)
        if inp.files_absent and inp.indexed:
            ev.append(
                f"none of the fix's {len(inp.files_absent)} source files exist on {inp.branch}"
            )
            return done(NOT_AFFECTED, "lower_bound", "file")
        ev.append("the branch's file table answered without an index watermark")
        return done(UNKNOWN, "lower_bound", "file", reason="no_index")
    ev.append("nothing to test presence with: no hunks and no source files")
    return done(UNKNOWN, "lower_bound", "", reason="no_touched_code")


def _label_line(backport_required: bool) -> str:
    if backport_required:
        return "the fix is labelled for backport (pr-must-backport / backport_required)"
    return "the fix carries no backport label: a candidate, not a missed backport"


def _first_reason(inp: BranchInputs) -> str:
    for s, v in inp.symbols.items():
        if v == "unknown" and inp.symbol_reasons.get(s):
            return str(inp.symbol_reasons[s])
    return ""


def rank(verdicts: list[BranchVerdict]) -> list[BranchVerdict]:
    """Gaps first, newest branch first within a verdict."""
    return sorted(
        verdicts,
        key=lambda v: (
            _RANK.get(v.verdict, 9),
            tuple(-x for x in (version_key(v.branch) or (0, 0))),
        ),
    )


def summarize(verdicts: list[BranchVerdict]) -> dict[str, list[str]]:
    return {v: [b.branch or b.namespace for b in verdicts if b.verdict == v] for v in VERDICTS}


def overall(verdicts: list[BranchVerdict], *, fix_found: bool) -> tuple[str, list[str]]:
    """The one-word answer and its causes."""
    if not fix_found:
        return UNKNOWN, ["no_fix"]
    if not verdicts:
        return UNKNOWN, ["no_branches"]
    kinds = {v.verdict for v in verdicts}
    if GAP in kinds:
        return GAP, []
    if PENDING in kinds:
        return PENDING, []
    if UNKNOWN in kinds:
        causes = sorted({v.reason for v in verdicts if v.verdict == UNKNOWN and v.reason})
        return UNKNOWN, causes
    if COVERED in kinds:
        return COVERED, []
    return NOT_AFFECTED, []


__all__ = [
    "BACKPORT_TITLE_RE",
    "COVERED",
    "GAP",
    "NOT_AFFECTED",
    "PENDING",
    "UNKNOWN",
    "VERDICTS",
    "BranchInputs",
    "BranchVerdict",
    "branch_contains",
    "hunk_symbols",
    "overall",
    "parse_backport_title",
    "rank",
    "source_files",
    "summarize",
    "verdict_for",
    "version_key",
]
