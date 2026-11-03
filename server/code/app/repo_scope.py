"""The repository namespace a request may read.

A repository keeps what agents read about it beyond its code (its documents
and its decision memories) in one place: its trunk branch's code namespace
(`repos.docs_namespaces`). Every branch of the repository reads that one
namespace, and it carries the repository's own access, so a restricted
repository's documents and decisions are as restricted as its code.
"""

from __future__ import annotations

import logging

from server.auth.authz import row_in_scope
from server.code.domain.index.repos import configured_repos, docs_namespaces, repo_for_namespace
from server.code.domain.scope import current_code_scope
from server.operations.base import Role

log = logging.getLogger("loom.repo_scope")


def trunk_namespace_of(org: str, code_namespace: str) -> str:
    """The trunk code namespace of the repository `code_namespace` indexes;
    "" when it is not a repository index."""
    cfg = repo_for_namespace(org, code_namespace)
    if cfg is None:
        return ""
    return docs_namespaces().get((org, str(cfg.repo)), "")


async def readable_repo_namespace(org: str, code_namespace: str) -> str:
    """`trunk_namespace_of`, passed through the gate for the current
    principal (VIEWER); "" when unbound, refused, or the check fails (closed)."""
    ns = trunk_namespace_of(org, code_namespace)
    if not ns:
        return ""
    try:
        return ns if await row_in_scope(org, ns, Role.VIEWER, hides_answer=False) else ""
    except Exception as e:  # noqa: BLE001 — a gate that cannot check refuses
        # One line per scoped request while the grants read is down.
        log.warning("repository namespace gate failed (closed): %s", e)
        return ""


def repo_trunk(org: str, repo: str) -> tuple[str, str]:
    """(trunk code namespace, `repo:<id>` tag) of the repository `repo` names (owner/name
    or a remote URL), matched as code.repos matches its candidates; ("", "") when no
    repository configured in `org` is that one. It reads the configured snapshot: the
    caller refreshes it (`refresh_db_repos`) and gates."""
    from server.code.app.repos import repo_slug
    from server.code.domain.index.remote import repo_tag

    slug = repo_slug(repo)
    entries = [c for c in configured_repos() if slug and c.org == org and repo_slug(c.repo) == slug]
    if not entries:
        return "", ""
    # The entries the trunk is chosen from (`docs_namespaces` keys on the spelling of
    # `repo`); any of them may be the one that names the remote.
    same = [c for c in entries if c.repo == entries[0].repo]
    tag = next((t for c in same if (t := repo_tag(c.git_remote or c.repo))), "")
    return trunk_namespace_of(org, entries[0].namespace), tag


def repo_names(org: str) -> dict[str, tuple[str, str]]:
    """`repo:<id>` tag -> (owner/name, trunk code namespace) of each repository
    configured in `org`."""
    from server.code.app.repos import repo_slug
    from server.code.domain.index.remote import repo_tag

    names: dict[str, tuple[str, str]] = {}
    for c in configured_repos():
        if c.org == org and (tag := repo_tag(c.git_remote or c.repo)):
            name = repo_slug(c.repo) or c.repo
            names.setdefault(tag, (name, trunk_namespace_of(org, c.namespace)))
    return names


async def repo_namespace_in_scope(org: str) -> str:
    """The trunk code namespace of the repository the request's code scope
    names, when it is in `org` and the caller may read it; "" otherwise. The
    one gate for `loom_search`'s repository legs (documents and decisions)."""
    scope = current_code_scope()
    if not scope or scope[0] != org:
        return ""
    return await readable_repo_namespace(org, scope[1])
