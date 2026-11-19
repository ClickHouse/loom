"""The source-bound push registry; every read resolves complete version rows."""

from __future__ import annotations

from dataclasses import asdict, fields
from typing import Any

from server.core.repo_base import _CHClient, _default_client
from server.knowledge.domain.source_push import PushRegistration

TABLE = "loom.knowledge_source_push"
_COLUMNS = ", ".join(f.name for f in fields(PushRegistration))


def _registration(row: dict[str, Any]) -> PushRegistration:
    return PushRegistration(
        **{f.name: str(row.get(f.name) or "") for f in fields(PushRegistration)}
    )


async def get(
    org: str, namespace: str, source_id: str, *, ch: _CHClient | None = None
) -> PushRegistration | None:
    row = await (ch or _default_client()).query_one(
        f"SELECT {_COLUMNS} FROM {TABLE} FINAL "
        "WHERE org = {org:String} AND namespace = {ns:String} "
        "AND source_id = {source:String} LIMIT 1",
        params={"org": org, "ns": namespace, "source": source_id},
    )
    return _registration(row) if row else None


async def put(registration: PushRegistration, *, ch: _CHClient | None = None) -> None:
    row = {**asdict(registration), "source_class": "derived"}
    await (ch or _default_client()).insert(TABLE, [row])


async def drive_page(
    after: tuple[str, str, str], *, ch: _CHClient | None = None
) -> list[PushRegistration]:
    rows = await (ch or _default_client()).query_all(
        f"SELECT {_COLUMNS} FROM {TABLE} FINAL "
        "WHERE provider = 'gdrive' AND status = 'active' "
        "AND (org, namespace, source_id) > {after:Tuple(String, String, String)} "
        "ORDER BY org, namespace, source_id LIMIT 20",
        params={"after": after},
    )
    return [_registration(row) for row in rows]


async def sealed_page(
    after: tuple[str, str, str], *, ch: _CHClient | None = None
) -> list[PushRegistration]:
    rows = await (ch or _default_client()).query_all(
        f"SELECT {_COLUMNS} FROM {TABLE} FINAL "
        "WHERE secret_enc != '' "
        "AND (org, namespace, source_id) > {after:Tuple(String, String, String)} "
        "ORDER BY org, namespace, source_id LIMIT 100",
        params={"after": after},
    )
    return [_registration(row) for row in rows]
