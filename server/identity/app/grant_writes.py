"""Grant rows an op writes after reading the grant it changes.

ClickHouse has no row lock, so a write can land after a concurrent change that
its read did not see. Two paths keep older tokens' claims in line:

* `lower` is for an op that takes a role away. It cuts the claims first, so a
  failure writes nothing, then writes the rows, then cuts again: a token minted
  while the insert was in flight read the old grant.
* `write_row(s)` is for every other op. Such an op writes no less than it read,
  so a higher row just before its own in the grant log is a raise it lowered
  without knowing, and that pair is cut.

Writers that only grant admin, or grant a subject that did not exist before,
cannot lower a grant and insert through the repository directly.
"""

from __future__ import annotations

from typing import Any

from server.auth.context import current_auth
from server.auth.revocation import (
    cut_where_raised_under,
    logged_subjects,
    pending_restamps,
    restamp,
    write_pair_cutoffs,
)
from server.identity.store import grants as grants_repo

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
    """For a retried op that finds its rows already written: cut again every
    pair whose lowering never got its second cutoff. Returns those pairs."""
    pending = await pending_restamps(org, pairs)
    await restamp(org, pending, revoked_by=_actor())
    return pending


async def recheck_namespace(org: str, namespace: str) -> list[Pair]:
    """`recheck` for every subject the grant log holds on `namespace`."""
    subjects = await logged_subjects(org, namespace)
    return await recheck(org, [(namespace, s) for s in subjects])


async def _cut_raised_under(rows: list[dict[str, Any]]) -> None:
    by_org: dict[str, dict[Pair, int]] = {}
    for row in rows:
        pair = (row["namespace"], row["subject"])
        by_org.setdefault(row["org"], {})[pair] = int(row["version"])
    for org, written in by_org.items():
        await cut_where_raised_under(org, written, revoked_by=_actor())


def _actor() -> str:
    auth = current_auth()
    return auth.subject if auth else ""
