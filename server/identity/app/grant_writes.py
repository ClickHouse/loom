"""Grant rows an op writes after reading the grant it changes.

ClickHouse has no row lock, so a concurrent raise can land between that read and
the write, and the write then lowers it without the cutoff a lowering needs.
After the rows land, `revocation.cut_where_lowered` cuts the claims a lowering
left standing: again for a subject already cut, which a token minted while the
write was in flight needs, and by the grant log for one never cut.

Writers that only grant admin, or grant a subject that did not exist before,
cannot lower a grant and insert through the repository directly.
"""

from __future__ import annotations

from typing import Any

from server.auth.context import current_auth
from server.auth.revocation import cut_where_lowered, logged_subjects
from server.identity.store import grants as grants_repo


async def insert_row(row: dict[str, Any]) -> None:
    await grants_repo.insert_row(row)
    await _check(row["org"], [(row["namespace"], row["subject"])])


async def insert_rows(rows: list[dict[str, Any]], *, ch: Any | None = None) -> None:
    await grants_repo.insert_rows(rows, ch=ch)
    by_org: dict[str, set[tuple[str, str]]] = {}
    for row in rows:
        by_org.setdefault(row["org"], set()).add((row["namespace"], row["subject"]))
    for org, pairs in by_org.items():
        await _check(org, sorted(pairs))


async def recheck(org: str, pairs: list[tuple[str, str]]) -> None:
    """The check alone, for a retried op that finds its rows already written."""
    await _check(org, pairs)


async def recheck_namespace(org: str, namespace: str) -> None:
    """`recheck` for every subject the grant log holds on `namespace`."""
    subjects = await logged_subjects(org, namespace)
    await recheck(org, [(namespace, s) for s in subjects])


async def _check(org: str, pairs: list[tuple[str, str]]) -> None:
    if not pairs:
        return
    auth = current_auth()
    await cut_where_lowered(org, pairs, revoked_by=auth.subject if auth else "")
