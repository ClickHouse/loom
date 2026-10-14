"""Grant rows an op writes after reading the grant it changes.

ClickHouse has no row lock, so a concurrent raise can land between that read and
the write, and the write then lowers it without the cutoff a lowering needs.
After the rows land, `revocation.cut_where_lowered` cuts again the pairs the op
lowered (`restamp`, cut before the write: a token minted while it was in flight
read the old grant) and, by the grant log, any other pair that fell.

Writers that only grant admin, or grant a subject that did not exist before,
cannot lower a grant and insert through the repository directly.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from server.auth.context import current_auth
from server.auth.revocation import cut_pairs, cut_where_lowered, logged_subjects
from server.identity.store import grants as grants_repo

Pair = tuple[str, str]


async def insert_row(row: dict[str, Any], *, restamp: bool = False) -> None:
    """Insert one grant row; `restamp` when the op lowered it after a cutoff."""
    await grants_repo.insert_row(row)
    pair = (row["namespace"], row["subject"])
    await _check(row["org"], [pair], {pair} if restamp else set())


async def insert_rows(
    rows: list[dict[str, Any]], *, restamp: Iterable[Pair] = (), ch: Any | None = None
) -> None:
    await grants_repo.insert_rows(rows, ch=ch)
    by_org: dict[str, set[Pair]] = {}
    for row in rows:
        by_org.setdefault(row["org"], set()).add((row["namespace"], row["subject"]))
    lowered = set(restamp)
    for org, pairs in by_org.items():
        await _check(org, sorted(pairs), lowered & pairs)


async def recheck(org: str, pairs: list[Pair], *, lowering: bool) -> None:
    """The check alone, for a retried op that finds its rows already written.
    A lowering op cannot tell whether its first attempt cut again, so every
    pair it already cut is."""
    restamp = await cut_pairs(org, pairs) if lowering else set()
    await _check(org, pairs, restamp)


async def recheck_namespace(org: str, namespace: str) -> None:
    """`recheck` of a lowering for every subject the grant log holds on `namespace`."""
    subjects = await logged_subjects(org, namespace)
    await recheck(org, [(namespace, s) for s in subjects], lowering=True)


async def _check(org: str, pairs: list[Pair], restamp: set[Pair]) -> None:
    if not pairs:
        return
    auth = current_auth()
    await cut_where_lowered(org, pairs, restamp=restamp, revoked_by=auth.subject if auth else "")
