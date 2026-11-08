"""Statements over loom.code_repos, the self-service repository registry: the rows the
snapshot is read from, a namespace's row, the count an org's cap is checked against, a
new version row, and the deactivation that mirrors a row with `active = false`.
"""

from __future__ import annotations

import time
from typing import Any

from server.code.store.index.base import CodeRepoBase


class CodeReposMixin(CodeRepoBase):
    """The self-service repository registry."""

    async def registry_rows(self) -> list[dict[str, Any]]:
        """The rows the registry snapshot is read from, in one statement: every active
        row, the columns a `RepoConfig` is built from, and the removed branches a member
        added from the page, which an operator entry is never listed for
        (`repos._with_registry`)."""
        rows: list[dict[str, Any]] = await self._ch.query_all(
            """
            SELECT org, namespace, repo, git_remote, git_branch,
                   if(active, path_prefixes, []) AS path_prefixes,
                   if(active, exclude, []) AS exclude,
                   toString(classification) AS classification,
                   toString(join_policy) AS join_policy,
                   if(active, github_token_enc, '') AS github_token_enc,
                   exclude_generated, toString(scip_producer) AS scip_producer,
                   toString(row_kind) AS row_kind, active
            FROM loom.code_repos FINAL
            WHERE active OR row_kind = 'page'
            ORDER BY org, namespace
            """
        )
        return rows

    async def active_code_repo_count(self, org: str) -> int:
        row = await self._ch.query_one(
            # An operator entry's access row is not a repository the org added.
            "SELECT count() AS n FROM loom.code_repos FINAL"
            " WHERE org = {org:String} AND active AND row_kind != 'access'",
            params={"org": org},
        )
        return int((row or {}).get("n") or 0)

    async def code_repo_row(self, org: str, ns: str) -> dict[str, Any] | None:
        """The namespace's registry row, every column (a new version mirrors it whole)."""
        row: dict[str, Any] | None = await self._ch.query_one(
            "SELECT * FROM loom.code_repos FINAL "
            "WHERE org = {org:String} AND namespace = {ns:String}",
            {"org": org, "ns": ns},
        )
        return row

    async def insert_code_repo_row(self, row: dict[str, Any]) -> None:
        await self._ch.insert("loom.code_repos", [row])

    async def deactivate_code_repo(self, org: str, ns: str) -> bool:
        """The active row → a new version with `active = false`, every column mirrored
        (RMT); False when the namespace has no active row. The live row is filtered in a
        subquery: a REPLACE alias is visible to the WHERE of its own SELECT, so
        `... REPLACE (false AS active) ... WHERE active` would test `false` and write
        nothing."""
        row = await self._ch.query_one(
            """
            SELECT count() AS n FROM loom.code_repos FINAL
            WHERE org = {org:String} AND namespace = {ns:String} AND active
            """,
            {"org": org, "ns": ns},
        )
        if not row or not int(row.get("n") or 0):
            return False
        await self._ch.execute(
            """
            INSERT INTO loom.code_repos
            SELECT * REPLACE (false AS active, now() AS updated_at, toUInt64({v:UInt64}) AS version)
            FROM (
                SELECT * FROM loom.code_repos FINAL
                WHERE org = {org:String} AND namespace = {ns:String} AND active
            )
            """,
            {"org": org, "ns": ns, "v": time.time_ns()},
        )
        return True
