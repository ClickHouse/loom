"""Grant rows an op writes after reading the grant it changes.

ClickHouse has no row lock, so a write can land after a concurrent change that
its read did not see. Three paths keep older tokens' claims in line:

* `lower` is for an op that takes a role away. It cuts the claims first, so a
  failure writes nothing, then writes the rows, then cuts again: a token minted
  while the insert was in flight read the old grant.
* `write_row(s)` is for every other op. Such an op writes no less than it read,
  so a higher row just before its own in the grant log is a raise it lowered
  without knowing, and that pair is cut.
* `recheck` is for a retried op that finds its rows already written, so it
  cannot tell what its first attempt did: every pair whose grant ranks below
  one the log holds for it is cut again. That also cuts tokens minted since a
  finished lowering; one minted from the grant loses nothing, since a cut claim
  falls back to the live grant, but a `loom mint-token` claim past the grant goes.

Writers that only grant admin, or grant a subject that did not exist before,
cannot lower a grant and insert through the repository directly.
"""

from __future__ import annotations

from typing import Any

from server.auth.context import current_auth
from server.auth.revocation import RESTAMPED, restamp, write_pair_cutoffs
from server.identity.store import grants as grants_repo
from server.operations.base import grant_rank

Pair = tuple[str, str]


async def write_row(row: dict[str, Any]) -> None:
    await grants_repo.insert_row(row)
    await _cut_raised_under([row])


async def write_rows(rows: list[dict[str, Any]], *, ch: Any | None = None) -> None:
    if rows:
        await grants_repo.insert_rows(rows, ch=ch)
        await _cut_raised_under(rows)


async def lower(
    org: str,
    rows: list[dict[str, Any]],
    *,
    reason: str,
    revoked_by: str | None = None,
    ch: Any | None = None,
) -> None:
    """Write `rows`, each taking a role away from its subject, with a cutoff
    before them and another once they have landed."""
    pairs = sorted({(r["namespace"], r["subject"]) for r in rows})
    by = _actor() if revoked_by is None else revoked_by
    await write_pair_cutoffs(org, pairs, reason=reason, revoked_by=by)
    await finish_lowering(org, rows, revoked_by=by, ch=ch)


async def finish_lowering(
    org: str, rows: list[dict[str, Any]], *, revoked_by: str | None = None, ch: Any | None = None
) -> None:
    """`lower` for an op that already wrote the first cutoff."""
    if rows:
        await grants_repo.insert_rows(rows, ch=ch)
        pairs = sorted({(r["namespace"], r["subject"]) for r in rows})
        await restamp(org, pairs, revoked_by=_actor() if revoked_by is None else revoked_by)


async def recheck(org: str, pairs: list[Pair]) -> list[Pair]:
    """Cut again each of `pairs` whose grant ranks below one the log holds for
    it. Returns those pairs."""
    history = await _history(org, pairs)
    fell = sorted(p for p in set(pairs) if p in history and max(history[p]) > history[p][-1])
    await restamp(org, fell, revoked_by=_actor())
    return fell


async def recheck_logged(
    org: str, *, namespace: str | None = None, subject: str | None = None
) -> list[Pair]:
    """`recheck` for every pair the grant log holds on `namespace` or of `subject`."""
    pairs = await grants_repo.logged_pairs(org, namespace=namespace, subject=subject)
    return await recheck(org, pairs)


async def _cut_raised_under(rows: list[dict[str, Any]]) -> None:
    """Cut each written pair whose log row just before its own (by version)
    ranks higher. A row versioned below the one the op read never won, so only
    the last one counts."""
    by_org: dict[str, dict[Pair, int]] = {}
    for row in rows:
        by_org.setdefault(row["org"], {})[(row["namespace"], row["subject"])] = int(row["version"])
    for org, written in by_org.items():
        logged = await grants_repo.logged_rows(
            org, sorted({ns for ns, _ in written}), sorted({s for _, s in written})
        )
        before: dict[Pair, int] = {}
        mine: dict[Pair, int] = {}
        for r in logged:
            pair = (str(r["namespace"]), str(r["subject"]))
            version = written.get(pair)
            if version is None or int(r["version"]) > version:
                continue
            if int(r["version"]) == version:
                mine[pair] = _rank(r)
            else:
                before[pair] = _rank(r)  # oldest first: the last one wins
        raised_under = sorted(p for p in mine if p in before and before[p] > mine[p])
        if raised_under:
            await write_pair_cutoffs(org, raised_under, reason=RESTAMPED, revoked_by=_actor())


async def _history(org: str, pairs: list[Pair]) -> dict[Pair, list[int]]:
    """Each pair's logged ranks, oldest first."""
    if not pairs:
        return {}
    logged = await grants_repo.logged_rows(
        org, sorted({ns for ns, _ in pairs}), sorted({s for _, s in pairs})
    )
    history: dict[Pair, list[int]] = {}
    for r in logged:
        history.setdefault((str(r["namespace"]), str(r["subject"])), []).append(_rank(r))
    return history


def _rank(row: dict[str, Any]) -> int:
    """What a grant log row grants: its role's rank while active, else 0."""
    live = row["revoked_at"] is None and row["status"] == "active"
    return grant_rank(row["role"]) if live else 0


def _actor() -> str:
    auth = current_auth()
    return auth.subject if auth else ""
