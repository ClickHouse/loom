"""Who to ask about a change: recency-weighted PR authorship AND review over
the paths a change reaches.

Not git blame (a formatter commit owns every line), not CODEOWNERS (maps directories to
teams, not people who know the code today). Merged PRs whose `files` overlap the
affected paths, each PR weighted 0.5^(age / half-life) so a maintainer who rotated off a
year ago decays below a floor while the person who merged three PRs there last month
ranks first. Bots are dropped; the caller can exclude itself (the agent's own PR author
is not a reviewer of it). The constants (HALF_LIFE 120 days, MIN_SHARE 0.15, ≤3
reviewers) are applied to the PR history loom already holds instead of a git log per
area.

Reviewers count too (bench/owners_precision.py): authorship alone scored hit@3 0.337 on
300 held-out ClickHouse PRs — BELOW the 0.370 of naming the namespace's three busiest
reviewers for every PR. The people who reviewed the earlier PRs in an area are the
ground truth the bench scores against, so each pool PR's human reviewers earn the same
decayed weight as its author, scaled by REVIEW_WEIGHT. The bench sweeps the scale; the
default is the measured best (0.5 / 1 / 2 are flat within 0.017). Weighting reviewers by
their comment volume (log1p(comments)) moved hit@3 by +0.003 — noise — so the hook stays
(`reviewer_weights` on a PR row) but code.impact does not pay a per-comment read for it.

And a namespace prior backs the area signal (same bench): area ranking with reviews
reached hit@3 0.360; topping the list up to three with the namespace's busiest human
reviewers of the trailing PRIOR_DAYS reached 0.447 (coverage 0.943 → 1.0, precision
0.194 → 0.156). Filled rows carry `via: "namespace"` so a consumer can tell "knows this
area" from "reviews everything here"; area rows carry `via: "area"`. With 1,035 PRs
carrying inline review comments: 0.450 vs 0.370.

Assignees are reviewers too. ClickHouse assigns the reviewer as a GitHub
assignee and that person often approves without an inline comment, so
`code_pr_history.assignees` joins the inline commenters in the review signal. Bench with
assignees for the newest ~10,900 merged PRs (300 held-out):
    truth = inline commenters: 0.450 → 0.480 hit@3
    truth = commenters ∪ assignees: 0.557 → 0.623;
    area signal alone 0.473 → 0.543 against a 0.370 busiest-reviewers
    baseline — the area ranking clears the baseline without the prior.

Should the prior abstain? bench/owners_precision.py scores fill policies (`fill_policy`:
never / empty / weak / always, and a cap on prior names), truth = commenters ∪
assignees:
    never             hit@3 0.547  precision 0.289  names/PR 2.19
    empty (area=0)          0.567            0.280           2.33
    weak (pool<40|top<.35)  0.623            0.250           2.90
    always, max 1 prior     0.603            0.257           2.69
    always (default)        0.623            0.242           3.00
Precision buys hit@3 roughly one for one along that curve; no policy dominates, so the
default "always fill, label via=namespace" stays and the consumer decides with `via`.

`owners` answers "who to ask", not "who to assign": workload balancing and knowledge
spreading are the assigner's concern. An agent is a permanent newcomer to the code,
which is where a recommendation adds the most.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from server.settings import settings

HALF_LIFE_DAYS = 120.0
# Floor on an author's decayed share. Measured on a hub file
# (MergeTreeData.cpp: 347 PRs, 57 authors in the trailing year) the top
# human holds ~24% and the next two ~12% and ~9%: a flat 0.15 named one
# person or nobody. So the floor is the SMALLER of MIN_SHARE and a third of
# the top author's share, never below MIN_SHARE_ABS — a clear leader still
# brings the two people who know the area next, the long tail stays out.
MIN_SHARE = 0.15
MIN_SHARE_ABS = 0.08
MAX_OWNERS = 3
# PRs considered per call. 50 recent PRs on a hub file are mostly CI
# robots; 200 reaches back far enough for humans to register.
PR_WINDOW = 200
# Weight of one review of a pool PR relative to authoring it (1.0 = equal).
# Chosen by bench/owners_precision.py --sweep; see the module docstring.
REVIEW_WEIGHT = 1.0
# Namespace prior: busiest human inline reviewers over this many trailing
# days, used only to top an area list up to MAX_OWNERS.
PRIOR_DAYS = 180
_BOT_SUFFIXES = ("[bot]", "-bot", "_bot", "-ci", "-ci-1", "-ci-2")
_BOT_NAMES = frozenset(
    {
        "dependabot",
        "github-actions",
        "robot-clickhouse",
        "robot-ch-test-poll",
        "clickhouse-gh",
        "renovate",
        "mergify",
        # autonomous agents that author PRs are not the people to ask; a
        # deployment names its own in `code_bot_authors`
        "devin-ai-integration",
        "cursor-agent",
        "codex",
    }
)


def is_bot(author: str) -> bool:
    a = (author or "").strip().lower()
    return (
        not a
        or a in _BOT_NAMES
        or a.endswith(_BOT_SUFFIXES)
        or a.startswith("robot-")
        or a in settings.code_bot_author_names()
    )


def _as_dt(v: Any) -> datetime | None:
    if isinstance(v, datetime):
        return v if v.tzinfo else v.replace(tzinfo=UTC)
    if isinstance(v, str) and v:
        try:
            d = datetime.fromisoformat(v.replace("Z", "+00:00"))
            return d if d.tzinfo else d.replace(tzinfo=UTC)
        except ValueError:
            return None
    return None


def rank_owners(
    prs: list[dict[str, Any]],
    *,
    now: datetime | None = None,
    half_life_days: float = HALF_LIFE_DAYS,
    min_share: float = MIN_SHARE,
    max_owners: int = MAX_OWNERS,
    exclude: tuple[str, ...] | list[str] = (),
    review_weight: float = REVIEW_WEIGHT,
    author_weight: float = 1.0,
) -> list[dict[str, Any]]:
    """[{author, share, prs, reviews, last_merged_at}] by decayed share, desc.

    Each PR row contributes its decayed weight once to its `author`
    (× author_weight) and once to every human in its `reviewers` list
    (× review_weight; a reviewer who is also the author counts once, as
    author). `share` is a person's weight over the total AFTER bots and
    `exclude` are removed, so excluding the change's own author
    redistributes the mass instead of leaving a hole. Empty when nothing
    survives — no guessed owner is better than a wrong one."""
    now = now or datetime.now(UTC)
    excluded = {e.strip().lower() for e in exclude if e and e.strip()}
    # GitHub logins are case-insensitive: key on the casefold, show the
    # first spelling seen
    display: dict[str, str] = {}
    weight: dict[str, float] = {}
    count: dict[str, int] = {}
    reviewed: dict[str, int] = {}
    last: dict[str, datetime] = {}

    def credit(login: str, w: float, merged: datetime | None, *, as_reviewer: bool) -> None:
        key = login.lower()
        display.setdefault(key, login)
        weight[key] = weight.get(key, 0.0) + w
        if as_reviewer:
            reviewed[key] = reviewed.get(key, 0) + 1
        else:
            count[key] = count.get(key, 0) + 1
        if merged and (key not in last or merged > last[key]):
            last[key] = merged

    for pr in prs or []:
        merged = _as_dt(pr.get("merged_at"))
        if merged is not None:
            age_days = max(0.0, (now - merged).total_seconds() / 86400.0)
        else:
            age_days = half_life_days * 4  # unknown date: treat as old
        w = 0.5 ** (age_days / half_life_days) if half_life_days > 0 else 1.0
        author = str(pr.get("author") or "").strip()
        if author and not is_bot(author) and author.lower() not in excluded and author_weight > 0:
            credit(author, w * author_weight, merged, as_reviewer=False)
        if review_weight <= 0:
            continue
        # optional per-reviewer multiplier (e.g. comment volume:
        # log1p(comments)); absent → every reviewer of the PR weighs 1
        mult = pr.get("reviewer_weights") or {}
        seen: set[str] = set()
        for rv in pr.get("reviewers") or []:
            r = str(rv or "").strip()
            key = r.lower()
            if not r or key in seen or key == author.lower() or is_bot(r) or key in excluded:
                continue
            seen.add(key)
            m = float(mult.get(r, mult.get(key, 1.0)) or 0.0) if isinstance(mult, dict) else 1.0
            if m <= 0:
                continue
            credit(r, w * review_weight * m, merged, as_reviewer=True)
    total = sum(weight.values())
    if total <= 0:
        return []
    ranked = sorted(weight.items(), key=lambda kv: (-kv[1], kv[0]))
    top_share = ranked[0][1] / total
    floor = max(MIN_SHARE_ABS, min(min_share, top_share / 3.0))
    out: list[dict[str, Any]] = []
    for key, w in ranked:
        share = w / total
        if share < floor:
            break
        out.append(
            {
                "author": display[key],
                "share": round(share, 3),
                "prs": count.get(key, 0),
                "reviews": reviewed.get(key, 0),
                "last_merged_at": last[key].isoformat() if key in last else "",
            }
        )
        if len(out) >= max_owners:
            break
    return out


def fill_with_prior(
    owners: list[dict[str, Any]],
    prior: list[str],
    *,
    exclude: tuple[str, ...] | list[str] = (),
    max_owners: int = MAX_OWNERS,
) -> list[dict[str, Any]]:
    """Top `owners` (area ranking, `via: "area"`) up to max_owners with the
    namespace's busiest reviewers in order (`via: "namespace"`, share 0 —
    they earned nothing in THIS area). Never reorders or drops an area row;
    bots, `exclude` and people already named are skipped."""
    out = [dict(o, via="area") for o in owners[:max_owners]]
    named = {str(o["author"]).lower() for o in out}
    excluded = {e.strip().lower() for e in exclude if e and e.strip()}
    for login in prior:
        if len(out) >= max_owners:
            break
        name = str(login or "").strip()
        key = name.lower()
        if not name or key in named or key in excluded or is_bot(name):
            continue
        named.add(key)
        out.append(
            {
                "author": name,
                "share": 0.0,
                "prs": 0,
                "reviews": 0,
                "last_merged_at": "",
                "via": "namespace",
            }
        )
    return out
