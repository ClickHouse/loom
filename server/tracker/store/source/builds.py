"""Build-history reads: the CI runs that built a commit, and the bases CI
built on after a moment.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from server.settings import settings
from server.tracker.domain.errors import TrackerUnavailable
from server.tracker.store.source.base import SourceBase


class BuildReads(SourceBase):
    """The build-history view's readers."""

    def _builds_table(self) -> str:
        """The build-history view for this namespace. The
        views carry no `is_private` column: the public view is public by
        construction (upstream keeps the public repository's runs in it)
        and the private one holds the private repository's; a standard
        namespace reads the former, a restricted one the latter."""
        if self.cfg is None:
            raise TrackerUnavailable(
                f"{self.org}/{self.namespace} is not a configured tracker namespace"
            )
        return "version_history_private" if self.cfg.restricted else "version_history"

    @staticmethod
    def _builds_budget() -> int:
        """Budget of a build-history read: the view proxies a remote table
        (one more network hop), so it gets the text-index budget, not the
        keyed-read one."""
        return int(settings.tracker_live_text_budget_ms)

    async def builds_naming(
        self, org: str, ns: str, sha: str, pr_number: int = 0
    ) -> list[dict[str, Any]]:
        """`version_history` rows that name `sha` — as the built commit or
        as a parent of the synthetic merge CI built — or that are the PR's
        own head builds (`pull_request_number`), oldest first, ≤ 64. One
        keyed read on two low-cardinality columns."""
        sha = str(sha or "").strip().lower()
        pr = int(pr_number or 0)
        if not sha and not pr:
            return []
        # an abbreviated sha is matched by prefix; a full one by equality
        clauses: list[str] = []
        if sha:
            clauses.append(
                "startsWith(commit_sha, {sha:String})"
                " OR arrayExists(p -> startsWith(p, {sha:String}), parent_commits_sha)"
            )
        if pr:
            clauses.append("pull_request_number = {pr:UInt32}")
        sql = f"""
            SELECT check_start_time, pull_request_number, commit_sha, parent_commits_sha,
                   version, docker_tag, git_ref, minor_version
            FROM {self.db}.{self._builds_table()}
            WHERE {" OR ".join(clauses)}
            ORDER BY check_start_time
            LIMIT 64
        """
        return await self._rows(
            "builds_naming",
            sql,
            {"sha": sha, "pr": pr},
            cache_key=[sha, pr],
            budget_ms=self._builds_budget(),
        )

    async def builds_after(
        self, org: str, ns: str, since: datetime, hours: int = 72
    ) -> list[dict[str, Any]]:
        """The distinct base commits CI built on in the `hours` after
        `since`, earliest first, each with its first run's facts and its
        run count (≤ 64 bases). `parent_commits_sha[1]` is the base branch
        head of the synthetic merge; one range read on the view's sort key."""
        until = since + timedelta(hours=max(1, int(hours)))
        sql = f"""
            SELECT parent_commits_sha[1] AS base_sha,
                   min(check_start_time) AS first_seen,
                   count() AS builds,
                   argMin(pull_request_number, check_start_time) AS first_pr,
                   argMin(commit_sha, check_start_time) AS first_commit,
                   argMin(version, check_start_time) AS first_version,
                   argMin(docker_tag, check_start_time) AS first_docker_tag,
                   argMin(git_ref, check_start_time) AS first_git_ref,
                   argMin(minor_version, check_start_time) AS first_minor_version
            FROM {self.db}.{self._builds_table()}
            WHERE check_start_time >= {{since:DateTime}} AND check_start_time < {{until:DateTime}}
              AND length(parent_commits_sha) = 2
            GROUP BY base_sha
            ORDER BY first_seen
            LIMIT 64
        """
        key = [since.replace(microsecond=0).isoformat(), int(hours)]
        return await self._rows(
            "builds_after",
            sql,
            {"since": since.replace(microsecond=0), "until": until.replace(microsecond=0)},
            cache_key=key,
            budget_ms=self._builds_budget(),
        )
