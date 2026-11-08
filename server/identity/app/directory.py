"""The identity domain's answers to the authorization gate's namespace
questions (`server.auth.directory.NamespaceDirectory`): the classification
cache, the code-plane repo config and the kind directory, read at call time; and
whether a name a path would register as new already holds data or grants."""

from __future__ import annotations

from server.code.domain.index import repos
from server.code.domain.index.repos import RepoConfig
from server.core.ch import sequential_reads
from server.core.repo_base import _CHClient
from server.erasure.app import ledger_reads
from server.identity.app import namespace_classification, namespace_kinds, namespace_visibility
from server.identity.store import namespace_census
from server.tracker.domain.namespaces import tracker_namespace


class IdentityDirectory:
    async def is_restricted(self, org: str, namespace: str) -> bool:
        return await namespace_classification.is_restricted(org, namespace)

    async def restricted_set(self, org: str) -> frozenset[str]:
        return await namespace_classification.restricted_set(org)

    def is_restricted_cached(self, org: str, namespace: str) -> bool:
        return namespace_classification.is_restricted_cached(org, namespace)

    def config_classification(self, org: str, namespace: str) -> str:
        return namespace_classification.config_classification(org, namespace)

    def code_entry(self, org: str, namespace: str) -> RepoConfig | None:
        return repos.repo_for_namespace(org, namespace)

    def restricted_code_namespaces(self, org: str) -> set[str]:
        return {
            cfg.namespace
            for cfg in repos.configured_repos()
            if cfg.org == org and cfg.classification == "restricted"
        }

    async def fanout_namespaces(
        self, org: str, namespaces: list[str], kinds: str | None = None
    ) -> list[str]:
        """Unknown kinds stay: the kind directory can only narrow, never widen."""
        keep = namespace_kinds.fanout_kinds(kinds)
        if not namespaces or not keep:
            return namespaces
        return namespace_kinds.keep_kinds(namespaces, await namespace_kinds.kind_map(org), keep)

    async def public_namespaces(self, org: str, namespaces: list[str]) -> set[str]:
        visibility = await namespace_visibility.visibility_map(org, namespaces)
        return {ns for ns, vis in visibility.items() if vis == "public"}

    async def in_use(
        self,
        org: str,
        namespace: str,
        *,
        ch: _CHClient | None = None,
        skip: frozenset[str] = frozenset(),
    ) -> bool:
        """A configured code-plane entry or tracker namespace is in use before its
        registry row or first row lands; any other name when Postgres holds a memory of it
        or `namespace_census.in_use` finds a row, read sequentially: one another replica
        wrote a moment ago counts."""
        if self.code_entry(org, namespace) is not None:
            return True
        if tracker_namespace(org, namespace) is not None:
            return True
        if await ledger_reads.pg_holds_namespace(org, namespace):
            return True
        tables = [t for t in ledger_reads.namespace_tables() if t not in skip]
        with sequential_reads():
            return await namespace_census.in_use(org, namespace, tables, ch=ch)
