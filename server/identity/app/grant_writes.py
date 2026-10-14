"""Grant rows an op writes after reading the grant it changes.

ClickHouse has no row lock, so a concurrent raise can land between that read and
the write, and the write then lowers it without the cutoff a lowering needs.
After the rows land, `revocation.cut_where_lowered` checks each subject against
the grant log and cuts the claims a lowering left standing.

Writers that only grant admin, or grant a subject that did not exist before,
cannot lower a grant and insert through the repository directly.
"""

from __future__ import annotations

from typing import Any

from server.auth.context import current_auth
from server.auth.revocation import cut_where_lowered
from server.identity.store import grants as grants_repo


async def insert_row(row: dict[str, Any]) -> None:
    await grants_repo.insert_row(row)
    await _cut_where_lowered([row])


async def insert_rows(rows: list[dict[str, Any]], *, ch: Any | None = None) -> None:
    await grants_repo.insert_rows(rows, ch=ch)
    await _cut_where_lowered(rows)


async def _cut_where_lowered(rows: list[dict[str, Any]]) -> None:
    by_namespace: dict[tuple[str, str], set[str]] = {}
    for row in rows:
        by_namespace.setdefault((row["org"], row["namespace"]), set()).add(row["subject"])
    auth = current_auth()
    for (org, namespace), subjects in by_namespace.items():
        await cut_where_lowered(
            org, namespace, sorted(subjects), revoked_by=auth.subject if auth else ""
        )
