"""Which tracker namespace reads which upstream rows, with which credential.

A tracker namespace is a directory row and nothing else: no loom table holds its
data. Each `namespaces` entry of `LOOM_TRACKER_SOURCES_JSON` names the source it
reads, the repositories it may name in a predicate (`"repos": ["Org/repo"]`) and
whether it is `restricted`, which decides the credential it reads with:

- a standard namespace's every statement carries `is_private = 0` — loom's
  predicate is the wall (one credential serves every
  namespace, so there is no upstream row policy behind it);
- a restricted namespace reads its own repositories only, and the restricted
  class (incidents, escalations, customers, Slack, instances) is refused on
  a standard namespace before any read.

A namespace in neither the sources JSON nor the self-service registry
(merged by `server/tracker/domain/sources.py`) is unknown to the tracker plane
and every read answers empty. A namespace's `source` names the credential it
reads with and the database its statements name.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field

from server.settings import settings
from server.tracker.domain import sources as _sources

log = logging.getLogger("loom.tracker.namespaces")

CREDENTIAL = _sources.DEFAULT_SOURCE  # the default tracker source


@dataclass(frozen=True)
class TrackerNamespace:
    org: str
    namespace: str
    repos: tuple[str, ...] = field(default_factory=tuple)
    restricted: bool = False
    source: str = CREDENTIAL  # The source (and credential) it reads

    @property
    def credential(self) -> str:
        return self.source

    @property
    def database(self) -> str:
        """The database the namespace's statements name — the source's."""
        ep = _sources.endpoint(self.source)
        return ep.database if ep is not None else ""  # No default database

    @property
    def is_private(self) -> int:
        return 1 if self.restricted else 0


def repo_namespaces() -> dict[str, str]:
    """The allowlist repository → namespace over every source. Invalid JSON
    = no entries from that setting (loud once, never a guess)."""
    out: dict[str, str] = {}
    for entry in _sources.namespace_entries().values():
        for repo in entry.repos:
            out[repo] = entry.namespace
    return out


def restricted_namespaces() -> frozenset[str]:
    """The namespaces the source map flags `restricted`."""
    return frozenset(ns for ns, e in _sources.namespace_entries().items() if e.restricted)


def configured_namespaces() -> dict[str, TrackerNamespace]:
    """Namespace → its source, repositories and classification, from the
    merged sources map (the self-service registry under the sources JSON)."""
    org = settings.tracker_org
    return {
        ns: TrackerNamespace(
            org=org, namespace=ns, repos=e.repos, restricted=e.restricted, source=e.source
        )
        for ns, e in _sources.namespace_entries().items()
    }


def tracker_namespace(org: str, namespace: str) -> TrackerNamespace | None:
    """The configured tracker namespace, or None when (org, namespace) is not one."""
    if org != settings.tracker_org:
        return None
    return configured_namespaces().get(namespace)


def split_source_id(source_id: str) -> tuple[str, int]:
    """`Acme/widgets#42` → (`Acme/widgets`, 42)."""
    repo, _, num = (source_id or "").partition("#")
    try:
        return repo.strip(), int(num)
    except ValueError:
        return repo.strip(), 0


def ref_of(repo: str, number: int) -> str:
    return f"{repo}#{int(number)}"


def doc_id(kind: str, source_id: str) -> str:
    """The upstream document UUID for a `kind` and `source_id`, computed
    locally — a graph read needs no lookup of its own end."""
    return str(uuid.uuid5(uuid.NAMESPACE_DNS, f"{kind}:{source_id}"))


def item_kinds(private: bool) -> tuple[str, str]:
    """The issue and PR kinds of a namespace's repositories."""
    if private:
        return ("github_issue_private", "github_pr_private")
    return ("github_issue", "github_pr")


__all__ = [
    "doc_id",
    "item_kinds",
    "CREDENTIAL",
    "TrackerNamespace",
    "configured_namespaces",
    "tracker_namespace",
    "ref_of",
    "repo_namespaces",
    "restricted_namespaces",
    "split_source_id",
]
