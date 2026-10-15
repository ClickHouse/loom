"""What a duplicate check says about the fix.

A reporter asks "is this already reported?" to learn whether to file, and
the next question is always "is it fixed, and in which release?". The
tracker record's `fixes` edges (PR -> issue) hold the merged closing PR, so
the answer carries it and the model does not have to open GitHub for it
(`gh issue view`, `gh pr view`).

Pure half: which linked PRs an issue row shows, the release its fix ships
in, and the one sentence that leads the answer. The reads are
`TrackerSource.fix_prs_of_issues` (one statement per namespace) and the
wiring is `server.tracker.app.dupcheck._with_links`.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any

LINKS_PER_ISSUE = 2
BACKPORTS_SHOWN = 4
RELATED_PRS_MAX = 3
_TRUNK = {"master", "main", ""}
_ISSUE_URL = re.compile(r"github\.com/([^/]+/[^/]+)/(?:issues|pull)/\d+")


def repo_of(row: dict[str, Any], default: str = "") -> str:
    """The repository a shown row belongs to: its url's `owner/name`, else
    the row's own `repo`, else the namespace's only repository."""
    m = _ISSUE_URL.search(str(row.get("url") or ""))
    if m:
        return m.group(1)
    return str(row.get("repo") or default)


def _version_key(v: str) -> tuple[int, ...]:
    parts = [int(p) for p in re.findall(r"\d+", v)]
    return tuple(parts) if parts else (10**6,)


def _trunk_merged(p: dict[str, Any]) -> bool:
    return (
        bool(p.get("is_merged"))
        and not p.get("is_backport_pr")
        and (str(p.get("base_branch") or "") in _TRUNK)
    )


def _day(v: Any) -> str | None:
    return v.date().isoformat() if isinstance(v, datetime) else None


def _state(p: dict[str, Any]) -> str:
    if p.get("is_merged"):
        return "merged"
    return "open" if str(p.get("state") or "").lower() == "open" else "closed"


def fixed_in(prs: list[dict[str, Any]]) -> str:
    """The release the tracker estimates the fix ships in: the lowest
    `minor_version` of a merged trunk PR, the version master carried when
    it merged. It is one release early for a PR merged just after that
    release's branch was cut (3 of 43 benchmark fixes); `release_of_merges`
    replaces it with the mirror's exact answer when the code index can
    reach the merge commit. Blank rows claim no release."""
    versions = [str(p.get("minor_version") or "") for p in prs if _trunk_merged(p)]
    versions = [v for v in versions if v]
    return min(versions, key=_version_key) if versions else ""


def backported_to(prs: list[dict[str, Any]]) -> list[str]:
    """The release lines a merged backport of the fix landed on."""
    out = {
        str(p.get("minor_version") or "")
        for p in prs
        if p.get("is_merged") and p.get("is_backport_pr") and p.get("minor_version")
    }
    return sorted(out, key=_version_key)[:BACKPORTS_SHOWN]


def pick_links(prs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """At most two linked PRs, the one a reader acts on first: a merged
    trunk fix (newest first), then an open PR (a fix in progress), then any
    other (a backport, a closed attempt). Lean rows."""

    def rank(p: dict[str, Any]) -> tuple[int, int]:
        if _trunk_merged(p):
            tier = 0
        elif _state(p) == "open":
            tier = 1
        elif p.get("is_merged"):
            tier = 2
        else:
            tier = 3
        return (tier, -int(p.get("pr_number") or 0))

    out: list[dict[str, Any]] = []
    for p in sorted(prs, key=rank)[:LINKS_PER_ISSUE]:
        row: dict[str, Any] = {
            "number": int(p.get("pr_number") or 0),
            "title": str(p.get("title") or "")[:120],
            "state": _state(p),
        }
        day = _day(p.get("merged_at")) if p.get("is_merged") else None
        if day:
            row["merged_at"] = day
        if _trunk_merged(p) and p.get("minor_version"):
            row["fixed_in"] = str(p["minor_version"])
        out.append(row)
    return out


def as_of(prs: list[dict[str, Any]], wall: datetime | None) -> list[dict[str, Any]]:
    """The links a replay at `wall` could have seen: merged fixes merged
    before it (an open PR's age is not on the row, so it stays)."""
    if wall is None:
        return prs
    return [
        p
        for p in prs
        if not (p.get("is_merged") and isinstance(p.get("merged_at"), datetime))
        or p["merged_at"] < wall
    ]


def attach(row: dict[str, Any], prs: list[dict[str, Any]]) -> None:
    """Put an issue row's links, release and backports on it, in place.
    Nothing is added when the tracker records no linked PR."""
    if not prs:
        return
    row["links"] = pick_links(prs)
    release = fixed_in(prs)
    if release:
        row["fixed_in"] = release
    lines = backported_to(prs)
    if lines:
        row["backported_to"] = lines


def _fix_clause(row: dict[str, Any]) -> str:
    links = row.get("links") or []
    merged = [lk for lk in links if lk.get("state") == "merged"]
    open_ = [lk for lk in links if lk.get("state") == "open"]
    if merged:
        where = f", in {row['fixed_in']}" if row.get("fixed_in") else ""
        if row.get("fixed_in_source") == "not_released":
            where = ", not in a release yet"
        back = row.get("backported_to") or []
        also = f" (backported to {', '.join(back)})" if back else ""
        return f"fixed by #{merged[0]['number']}{where}{also}"
    if open_:
        return f"a fix is in progress: #{open_[0]['number']} (open)"
    return "no fix PR is linked in the tracker"


def _row_ref(row: dict[str, Any]) -> str:
    return f"#{row.get('number')} ({row.get('state') or 'unknown'})"


def lead_issue(out: dict[str, Any]) -> dict[str, Any] | None:
    """The issue the answer states as this report: the served calibrated
    duplicate (`duplicate_of`), else none. A bisect borrows a fix from this
    row only — a look-alike's fix never leads a bisect."""
    items = [r for r in (out.get("items") or []) if isinstance(r, dict)]
    if str(out.get("verdict") or "") in ("duplicate_of", "duplicate") and items:
        return items[0]
    return None


def _hedged(row: dict[str, Any]) -> str:
    """A lead's fix, stated as the tracker records it, never as this bug's."""
    links = row.get("links") or []
    merged = [lk for lk in links if lk.get("state") == "merged"]
    open_ = [lk for lk in links if lk.get("state") == "open"]
    if merged:
        where = f" in {row['fixed_in']}" if row.get("fixed_in") else ""
        return f", fixed by #{merged[0]['number']}{where}"
    if open_:
        return f", fix in progress #{open_[0]['number']}"
    return ""


def lead_row(out: dict[str, Any], lead_bar: float) -> dict[str, Any] | None:
    """The nearest row to offer as a lead: the rerank's highest `same` label
    at or above `lead_bar`. Never a verdict."""
    best, conf = None, -1.0
    for r in out.get("nearest") or []:
        rr = r.get("rerank") if isinstance(r, dict) else None
        if not isinstance(rr, dict):
            continue
        c = float(rr.get("confidence") or 0.0)
        if str(rr.get("label") or "") == "same" and c >= lead_bar and c > conf:
            best, conf = r, c
    return best


def answer_line(out: dict[str, Any], lead_bar: float = 0.9) -> str:
    """One sentence: already reported? fixed? in which release? — or a lead
    to check, or a PR that may already address it. Empty when there is
    nothing to say beyond the verdict. Only a calibrated duplicate is stated
    as this bug's report and fix: saying a new bug is already fixed when it
    is not is the most harmful thing this answer can do."""
    row = lead_issue(out)
    if row is not None:
        return f"already reported as {_row_ref(row)}; {_fix_clause(row)}"
    lead = lead_row(out, lead_bar)
    if lead is not None:
        state = str(lead.get("state") or "unknown")
        return (
            f"no calibrated duplicate; nearest reported: #{lead.get('number')} "
            f"({state}{_hedged(lead)}) — may be related, check it"
        )
    prs = out.get("related_prs") or []
    if prs:
        p = prs[0]
        return (
            f"no reported issue matches; a PR may already address this: "
            f"#{p.get('number')} ({p.get('state')})"
        )
    return ""


def related_prs(pr_check_out: Any, exclude: set[int]) -> list[dict[str, Any]]:
    """code.pr_check's items as lean rows: PRs that may
    already address the draft, never ones an issue row already links."""
    dump = getattr(pr_check_out, "model_dump", None)
    d = dump() if callable(dump) else dict(pr_check_out or {})
    if str(d.get("verdict") or "none") == "none":
        return []
    out: list[dict[str, Any]] = []
    for it in d.get("items") or []:
        n = int(it.get("pr_number") or 0)
        if n <= 0 or n in exclude:
            continue
        row: dict[str, Any] = {
            "number": n,
            "title": str(it.get("title") or "")[:120],
            "state": str(it.get("state") or ""),
            "relation": str(it.get("relation") or ""),
            "score": it.get("score"),
        }
        if it.get("merged_at"):
            row["merged_at"] = str(it["merged_at"])[:10]
        out.append(row)
        if len(out) >= RELATED_PRS_MAX:
            break
    return out


def merged_trunk_fix(row: dict[str, Any]) -> int:
    """The merged fix PR an issue row links, or 0 (bisect's input).
    `pick_links` orders a merged trunk fix first."""
    for lk in row.get("links") or []:
        if lk.get("state") == "merged":
            return int(lk.get("number") or 0)
    return 0


async def release_of_merges(
    org: str, code_ns: str, rows: list[dict[str, Any]], *, timeout_s: float = 2.0
) -> None:
    """Set each shown row's `fixed_in` from the mirror — the
    first release line whose stable/LTS tags contain the linked fix's merge
    commit — in place, with `fixed_in_source: release_tag`. A row the mirror
    cannot answer keeps the tracker's estimate (`fixed_in_source: tracker`),
    and a merge no release tag contains yet says `not_released`. One ledger
    read for all fixes, then one bounded `git tag --contains` each (≈20 ms
    on a large mirror); every failure keeps what was there."""
    import asyncio

    from server.code.app.index import gitrepo
    from server.code.app.index.mirrors import history_dir
    from server.code.app.reads import code_index
    from server.code.domain.index.repos import configured_repos

    wanted = {merged_trunk_fix(r): r for r in rows if r.get("links") and merged_trunk_fix(r)}
    for r in rows:
        if r.get("fixed_in"):
            r["fixed_in_source"] = "tracker"
    if not wanted or not code_ns:
        return
    cfg = next((c for c in configured_repos() if c.org == org and c.namespace == code_ns), None)
    # no mirror on this host (yet): the tracker's estimates stand
    git_dir = (await history_dir(cfg))[0] if cfg is not None else ""
    if not git_dir:
        return
    shas = await code_index().merge_commits_of(org, code_ns, list(wanted))

    async def one(pr: int, sha: str) -> tuple[int, str | None]:
        try:
            return pr, await gitrepo.release_containing(git_dir, sha, timeout_s=timeout_s)
        except Exception:  # noqa: BLE001 — the tracker's estimate stands
            return pr, None

    for pr, rel in await asyncio.gather(*(one(p, sha) for p, sha in shas.items())):
        row = wanted.get(pr)
        if row is None or rel is None:
            continue
        if rel:
            row["fixed_in"], row["fixed_in_source"] = rel, "release_tag"
        else:
            row.pop("fixed_in", None)
            row["fixed_in_source"] = "not_released"
