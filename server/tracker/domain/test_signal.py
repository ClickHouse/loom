"""Pure helpers for `tracker.test_signal`: a failing test read
against its own CI history before anyone blames the change.

Three history-only mechanisms, each a feature the reader computes from the
`checks` rows of one test:

- **flip rate and outcome entropy** over a window: status changes between
  consecutive runs of the same sequence, and the entropy of the outcome
  distribution, rank flaky tests without re-running them;
- **rerun flips** — the same commit with both outcomes: the pass-on-rerun
  history is the strongest single history predictor of flakiness;
- **co-failure hours** — the test failed in an hour in which many unrelated
  tests failed across different PRs: false test alarms co-occur, so an
  association over simultaneous failures separates infrastructure from the
  test itself.

Loom's own leg is churn: merged PRs of the ledger that touched the test's
home file(s) or the sources the test map ties to it (the test map, or the
path convention of a stateless / integration test name).

The verdict is one of `flaky | regression_candidate | infra_candidate |
no_signal | unknown_test | tracker_unavailable`; `no_signal` is silence, not
"healthy". `unknown_test` is a name no test carries while
tests whose names start with or contain it do: the answer lists them instead
of reading a family's runs as one test's. Every threshold is a setting
and echoed in the answer.
"""

from __future__ import annotations

import math
import re
from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Any

FLAKY = "flaky"
REGRESSION = "regression_candidate"
INFRA = "infra_candidate"
NO_SIGNAL = "no_signal"
UNAVAILABLE = "tracker_unavailable"
UNKNOWN_TEST = "unknown_test"
VERDICTS = (FLAKY, REGRESSION, INFRA, NO_SIGNAL, UNKNOWN_TEST, UNAVAILABLE)
# How many near names an `unknown_test` answer lists
MAX_CANDIDATES = 5

# `test_status` values that are a failure of the test itself; SKIPPED,
# NOT_FAILED, unstable, slower, XFAIL and '' are not (over 14 days of CI:
# OK 1.43 B, SKIPPED 133 M, FAIL 55 k, ERROR 248, failure 457).
FAIL_STATUSES: tuple[str, ...] = ("FAIL", "ERROR", "failure", "error")
# A hot test fails in many hours; the co-failure read names at most this many
# (the most recent), so its IN list stays bounded.
MAX_FAIL_HOURS = 168

_STATELESS_RE = re.compile(r"^(\d{5}_[A-Za-z0-9_]+?)(?:\.(?:sql|sh|py|expect|j2|sql\.j2|sh\.j2))?$")
_INTEGRATION_RE = re.compile(r"^(test_[A-Za-z0-9_]+)(?:/|::|$)")
# A file name as a user writes it — `.sql`, `.sh`, a
# `.reference`, the repo path in front
_FILE_SUFFIX_RE = re.compile(r"\.(?:sql\.j2|sh\.j2|sql|sh|py|expect|j2|reference)$")
_QUERIES_DIR_RE = re.compile(r"^(?:\./)?tests/queries/[^/]+/")


@dataclass(frozen=True)
class Thresholds:
    min_runs: int = 5
    flip_rate: float = 0.02
    # a storm hour holds ≥ storm_k(median) OTHER failing tests across ≥ cofail_prs PRs
    cofail_k: int = 100
    cofail_multiple: float = 5.0
    cofail_prs: int = 3
    cofail_share: float = 0.5

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)

    def storm_k(self, median_failing_tests_per_hour: int) -> int:
        """K for the window: the floor, or `cofail_multiple` × the window's
        median distinct failing tests per hour, whichever is larger — a
        storm is an outlier hour, not a busy one (79% of hours hold ≥ 20
        failing tests across ≥ 3 PRs; the median hour holds 41)."""
        scaled = int(self.cofail_multiple * max(0, median_failing_tests_per_hour))
        return max(int(self.cofail_k), scaled)


def wilson_lower(k: int, n: int, z: float = 1.96) -> float:
    """Wilson score interval, lower bound: the least a rate can plausibly be
    given `k` of `n` — 0 when nothing ran."""
    if n <= 0:
        return 0.0
    p = k / n
    denom = 1.0 + z * z / n
    centre = p + z * z / (2 * n)
    spread = z * math.sqrt((p * (1 - p) + z * z / (4 * n)) / n)
    return round(max(0.0, (centre - spread) / denom), 4)


def binary_entropy(p: float) -> float:
    """Entropy in bits of a pass/fail outcome with failure probability `p`."""
    if p <= 0.0 or p >= 1.0:
        return 0.0
    return round(-(p * math.log2(p) + (1 - p) * math.log2(1 - p)), 4)


def _iso(v: Any) -> str | None:
    if v is None:
        return None
    if isinstance(v, datetime):
        if v.timestamp() <= 0 or v.year <= 1970:
            return None
        return v.isoformat()
    text = str(v)
    return None if text.startswith("1970-01-01") or not text else text


def features(row: dict[str, Any] | None) -> dict[str, Any]:
    """The history features from the one aggregated row of the test's runs
    (`TrackerSource.test_history`); all zero when the window holds no run."""
    r = row or {}
    runs = int(r.get("runs_total") or 0)
    fails = int(r.get("fails_total") or 0)
    steps = int(r.get("steps_total") or 0)
    flips = int(r.get("flips_total") or 0)
    fail_rate = round(fails / runs, 4) if runs else 0.0
    hours = [h for h in (r.get("fail_hours_all") or []) if _iso(h)]
    prs_by_hour: dict[str, set[int]] = {}
    for pair in r.get("fail_hour_prs") or []:
        try:
            h, pr = pair[0], int(pair[1])
        except (TypeError, ValueError, IndexError):
            continue
        key = _iso(h)
        if key and pr > 0:  # branch heads (pr = 0) are one head, not a PR
            prs_by_hour.setdefault(key, set()).add(pr)
    return {
        "runs": runs,
        "fails": fails,
        "fail_rate": fail_rate,
        "flips": flips,
        "steps": steps,
        "flip_rate": round(flips / steps, 4) if steps else 0.0,
        "entropy": binary_entropy(fail_rate),
        "rerun_flips": int(r.get("rerun_flips_total") or 0),
        "prs": int(r.get("prs_total") or 0),
        "failing_prs": int(r.get("failing_prs") or 0),
        "branch_fails": int(r.get("branch_fails") or 0),
        "last_true_fail": _iso(r.get("last_true_fail")),
        "last_true_fail_head": str(r.get("last_true_fail_head") or "") or None,
        "last_fail_at": _iso(r.get("last_fail_at")),
        "first_run_at": _iso(r.get("first_run_at")),
        "fail_hours": [str(_iso(h)) for h in hours][-MAX_FAIL_HOURS:],
        # distinct PR heads this test failed on, per failing hour (≥ 2 only)
        "fail_hour_prs": {h: len(v) for h, v in sorted(prs_by_hour.items()) if len(v) >= 2},
    }


def multi_pr_hours(feat: dict[str, Any], min_prs: int) -> list[str]:
    """Hours in which THIS test failed on ≥ `min_prs` different PR heads — a
    cause the PRs share (the runner, or a master breakage every PR
    inherits), not any one change."""
    return sorted(h for h, n in (feat.get("fail_hour_prs") or {}).items() if n >= min_prs)


def co_fail_hours(
    hour_rows: list[dict[str, Any]], *, k: int, min_prs: int
) -> tuple[int, list[str]]:
    """Hours in which ≥ `k` OTHER tests failed across ≥ `min_prs` PRs — the
    storm hours — from the co-failure read; (count, hours)."""
    hours: list[str] = []
    for r in hour_rows:
        if int(r.get("other_tests") or 0) >= k and int(r.get("other_prs") or 0) >= min_prs:
            h = _iso(r.get("h"))
            if h:
                hours.append(h)
    hours.sort()
    return len(hours), hours


def verdict(
    feat: dict[str, Any], storm_hours: int, th: Thresholds, *, storm_k: int | None = None
) -> tuple[str, list[str], float]:
    """(verdict, why-lines, lower_bound). `lower_bound` is the Wilson lower
    bound of the failure rate over the window's runs — the least the test
    can plausibly fail, whatever the verdict; a small-n verdict carries a
    small bound and the reader sees it."""
    runs, fails = feat["runs"], feat["fails"]
    lb = wilson_lower(fails, runs)
    fail_hours = len(feat["fail_hours"])
    if runs < th.min_runs:
        return (
            NO_SIGNAL,
            [f"{runs} runs in the window, fewer than {th.min_runs}: nothing to read"],
            lb,
        )
    if fails == 0:
        return NO_SIGNAL, [f"{runs} runs, none failed in the window"], lb
    if feat["rerun_flips"] >= 1:
        return (
            FLAKY,
            [
                f"{feat['rerun_flips']} commit(s) had both a failure and a pass of this test "
                "(a rerun of the same commit passed)",
                f"fail rate {feat['fail_rate']} over {runs} runs, flip rate {feat['flip_rate']}",
            ],
            lb,
        )
    shared = multi_pr_hours(feat, th.cofail_prs)
    if shared:
        return (
            INFRA,
            [
                f"failed on ≥ {th.cofail_prs} different PR heads within one hour "
                f"({len(shared)} such hour(s), first {shared[0]}): a cause the PRs share, "
                "not one change",
            ],
            lb,
        )
    if fail_hours and storm_hours / fail_hours >= th.cofail_share:
        k = th.cofail_k if storm_k is None else storm_k
        return (
            INFRA,
            [
                f"{storm_hours} of its {fail_hours} failing hour(s) held ≥ {k} other "
                f"failing tests across ≥ {th.cofail_prs} PRs (co-failure, not this test)",
            ],
            lb,
        )
    if feat["flip_rate"] >= th.flip_rate or (feat["failing_prs"] >= 3 and feat["fail_rate"] < 0.5):
        return (
            FLAKY,
            [
                f"flip rate {feat['flip_rate']} over {feat['steps']} consecutive-run steps "
                f"(threshold {th.flip_rate}); failed on {feat['failing_prs']} PR head(s) "
                f"at fail rate {feat['fail_rate']}",
            ],
            lb,
        )
    why = [
        f"{fails} failure(s) in {runs} runs on {feat['failing_prs']} PR head(s), no pass on a rerun"
    ]
    if feat["last_true_fail"]:
        why.append(
            f"failed on branch head {feat['last_true_fail_head'] or '?'} "
            f"at {feat['last_true_fail']} (a true failure, not a PR head)"
        )
    elif feat["branch_fails"] == 0:
        why.append("never failed on a branch head in the window")
    return REGRESSION, why, lb


def home_of(test_name: str) -> tuple[list[str], str]:
    """(basename stems, path prefix) of the test's home file(s) by naming
    convention: a stateless `NNNNN_name[.ext]` is `tests/queries/*/NNNNN_name.*`,
    an integration `test_x/test.py::test_y[...]` is `tests/integration/test_x/`;
    anything else (a unit test, a check-level row) has no home."""
    name = (test_name or "").strip()
    m = _STATELESS_RE.match(name)
    if m:
        return [m.group(1)], ""
    m = _INTEGRATION_RE.match(name)
    if m:
        return [], f"tests/integration/{m.group(1)}/"
    return [], ""


def is_test_home_path(path: str) -> bool:
    """A candidate home path is a runnable test file, never its `.reference`
    expectation (the code index holds those as files too)."""
    p = path.lower()
    return not (p.endswith(".reference") or p.endswith(".reference.j2"))


def churn_summary(
    home_paths: list[str],
    home_prs: list[dict[str, Any]],
    source_paths: list[str],
    source_prs: list[dict[str, Any]],
    *,
    days: int,
) -> dict[str, Any]:
    def _prs(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return [
            {
                "pr_number": int(r.get("pr_number") or 0),
                "title": str(r.get("title") or "")[:120],
                "merged_at": _iso(r.get("merged_at")),
            }
            for r in rows[:10]
        ]

    return {
        "available": True,
        "days": days,
        "home_paths": home_paths,
        "home_prs": len(home_prs),
        "home_recent": _prs(home_prs),
        "source_paths": source_paths,
        "source_prs": len(source_prs),
        "source_recent": _prs(source_prs),
    }


def churn_unavailable(reason: str, *, days: int) -> dict[str, Any]:
    return {
        "available": False,
        "reason": reason,
        "days": days,
        "home_paths": [],
        "home_prs": 0,
        "home_recent": [],
        "source_paths": [],
        "source_prs": 0,
        "source_recent": [],
    }


def name_needle(name: str) -> str:
    """The test name a user's spelling points at — the
    stateless file's stem without `tests/queries/<suite>/` or its suffix.
    Integration names (`test_x/test.py::test_y`) are kept as written."""
    n = (name or "").strip()
    if _INTEGRATION_RE.match(n):
        return n
    n = _QUERIES_DIR_RE.sub("", n)
    return _FILE_SUFFIX_RE.sub("", n)


def name_candidates(
    name: str, needle: str, rows: list[dict[str, Any]], *, prefix: bool
) -> dict[str, Any] | None:
    """What the candidate read says about a name with no run
    of its own — None when no test matches; else `resolved` (the one test to
    answer for: the only match, or a case-insensitive exact one), the listed
    `candidates` (most runs first), `matches` (every matching test) and the
    `why` line. Nothing is summed across tests: a family is listed, not read
    as one test."""
    if not rows:
        return None
    cands: list[dict[str, Any]] = [
        {
            "test_name": str(r.get("test_name") or ""),
            "runs": int(r.get("runs_in_window") or 0),
            "fails": int(r.get("fails_in_window") or 0),
        }
        for r in rows[:MAX_CANDIDATES]
    ]
    matches = max(int(rows[0].get("matches") or 0), len(rows))
    exact = next((c["test_name"] for c in cands if c["test_name"].lower() == needle.lower()), "")
    how = "start with" if prefix else "contain"
    if exact or matches == 1:
        resolved = exact or cands[0]["test_name"]
        why = f"no test named {name!r}; answered for {resolved!r}, the only test that matches"
        return {"resolved": resolved, "candidates": [], "matches": matches, "why": why}
    why = (
        f"no test named {name!r} in the window; {matches} tests {how} {needle!r} — "
        f"`candidates` holds the {len(cands)} with the most runs; pass one as test_name"
    )
    return {"resolved": "", "candidates": cands, "matches": matches, "why": why}


__all__ = [
    "FAIL_STATUSES",
    "FLAKY",
    "INFRA",
    "MAX_CANDIDATES",
    "MAX_FAIL_HOURS",
    "NO_SIGNAL",
    "REGRESSION",
    "UNAVAILABLE",
    "UNKNOWN_TEST",
    "VERDICTS",
    "Thresholds",
    "binary_entropy",
    "churn_summary",
    "churn_unavailable",
    "co_fail_hours",
    "features",
    "home_of",
    "is_test_home_path",
    "multi_pr_hours",
    "name_candidates",
    "name_needle",
    "verdict",
    "wilson_lower",
]
