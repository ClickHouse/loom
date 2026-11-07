"""Which tracker namespaces a code namespace reads its facts from.

A repository links an ordered list of trackers. The FIRST is its primary:
the record whose numbers are the repository's own issue and PR numbers, so
every lookup BY NUMBER (an item, a PR's facts, blame, backports, first
build) reads it and only it — `#7` in a help desk is not the repository's
`#7`. The others are searched: the duplicate check, `tracker.similar`, the
answer route and bisect's candidate scan read every linked tracker and say
which one each result came from. The map is operator configuration —
`LOOM_TRACKER_CODE_NAMESPACE_BINDINGS_JSON`, glob keys → a namespace or a
list — or a maintainer's links (`code.repo.set_source`), never a guess from
names.

Links are keyed by (org, code namespace). A code namespace name is derived
from the repository and branch, so two orgs can hold the same one: a
maintainer's link applies only in the org that set it. The operator's map
applies only to the repositories the operator configured (`source ==
"config"`): a self-service repository, or a namespace with no repository
entry, whose name matches a glob gets nothing from the map. A branch added
from an operator repository (`code.repo.add_branch`, its `qa_branch_of` row
and its self-service entry) reads that repository's links when read, so it
follows the map as the repository does; a `qa_branch_of` row with no entry
beside it is not followed.

The bound namespace is read inside a request the transport authorized
against the CODE namespace, so the read runs the gate again for the current
principal (`row_in_scope`): a restricted tracker namespace bound to a public code
namespace is never reached by a principal the gate would refuse.
Unauthorized or unbound → "" and the caller adds nothing.
"""

from __future__ import annotations

import fnmatch
import json
import logging
import time
from collections.abc import Collection
from typing import Any

from server.settings import settings

log = logging.getLogger("loom.tracker.binding")

_warned_bad_json = False


def parse_binding(value: Any) -> list[str]:
    """A binding value as the ordered, de-duplicated list of
    tracker namespaces it names — a list, or the comma-joined stored form.
    Namespace names never hold a comma (`_NS_RE`), so the join is
    unambiguous."""
    items = value if isinstance(value, (list, tuple)) else str(value or "").split(",")
    out: list[str] = []
    for it in items:
        name = str(it or "").strip()
        if name and name not in out:
            out.append(name)
    return out


def encode_binding(names: list[str]) -> str:
    """The stored form of a binding (`code_index_state.state_value`)."""
    return ",".join(parse_binding(names))


def bindings() -> dict[str, str]:
    """The deployment's map, glob key → the stored form of its binding (one
    name, or several comma-joined — `parse_binding` reads either)."""
    global _warned_bad_json
    raw = settings.tracker_code_namespace_bindings_json.strip()
    if not raw:
        return {}
    try:
        data = json.loads(raw)
        out = {str(k): encode_binding(parse_binding(v)) for k, v in dict(data).items() if k and v}
        return {k: v for k, v in out.items() if v}
    except Exception:  # noqa: BLE001 — a malformed bindings setting binds nothing, logged once
        if not _warned_bad_json:
            _warned_bad_json = True
            log.error("LOOM_TRACKER_CODE_NAMESPACE_BINDINGS_JSON is not a JSON object; no QA facts")
        return {}


# A maintainer's binding, written by `code.repo.set_source` as
# the `OVERRIDE_KEY` state row of the code namespace; "" clears the config's.
OVERRIDE_KEY = "qa_binding"  # storage key
_OVERRIDES: dict[tuple[str, str], str] = {}
# The repository a branch was added from (`code.repo.add_branch`), the
# `qa_branch_of` state row of the branch's namespace.
BRANCH_OF_KEY = "qa_branch_of"  # storage key
_BRANCH_OF: dict[tuple[str, str], str] = {}
_OVERRIDES_AT = 0.0
# Bumped by `forget_links`: a refresh whose read started before it may hold the rows
# just emptied, so it keeps nothing.
_FORGOTTEN = 0


async def refresh_overrides(*, force: bool = False) -> dict[tuple[str, str], str]:
    """Re-read the `OVERRIDE_KEY` and `BRANCH_OF_KEY` state rows when the snapshot is
    older than `code_repos_refresh_seconds` (or `force`); never raises — a directory
    that cannot be read keeps the last snapshot."""
    global _OVERRIDES, _BRANCH_OF, _OVERRIDES_AT
    now = time.monotonic()
    if not force and now - _OVERRIDES_AT < float(settings.code_repos_refresh_seconds):
        return dict(_OVERRIDES)
    try:
        from server.code.app.reads import code_index
        from server.core.ch import get_client

        if getattr(get_client(), "_client", "absent") is None:
            return dict(_OVERRIDES)
        forgotten = _FORGOTTEN
        rows = await code_index().state_values_for_keys([OVERRIDE_KEY, BRANCH_OF_KEY])
        if forgotten != _FORGOTTEN:
            return dict(_OVERRIDES)  # the next call reads again
        _OVERRIDES, _BRANCH_OF = rows[OVERRIDE_KEY], rows[BRANCH_OF_KEY]
        _OVERRIDES_AT = now
    except Exception as e:  # noqa: BLE001 — keep the last snapshot
        log.warning("qa bindings: override refresh failed, keeping %d: %s", len(_OVERRIDES), e)
    return dict(_OVERRIDES)


def forget_links(org: str, code_namespace: str) -> None:
    """Drop the org's namespace from this replica's snapshot, whose rows were just
    emptied, and discard any refresh already reading."""
    global _FORGOTTEN
    _FORGOTTEN += 1
    _OVERRIDES.pop((org, code_namespace), None)
    _BRANCH_OF.pop((org, code_namespace), None)


def override_for(org: str, code_namespace: str) -> str | None:
    """The maintainer's binding for the org's namespace ("" = cleared), None
    when no row exists."""
    return _OVERRIDES.get((org, code_namespace))


def reset_overrides_for_tests(
    rows: dict[tuple[str, str], str] | None = None,
    branches: dict[tuple[str, str], str] | None = None,
) -> None:
    global _OVERRIDES, _BRANCH_OF, _OVERRIDES_AT
    _OVERRIDES = dict(rows or {})
    _BRANCH_OF = dict(branches or {})
    _OVERRIDES_AT = 0.0


def operator_repositories() -> frozenset[tuple[str, str]]:
    """(org, namespace) of every repository the operator configured, the only
    kind the deployment's map links. One read of the repository list: a caller
    looking up many namespaces builds it once and passes it down."""
    from server.code.domain.index import repos

    return frozenset((c.org, c.namespace) for c in repos.configured_repos() if c.source == "config")


def tracker_namespaces_for(
    org: str, code_namespace: str, *, operator: Collection[tuple[str, str]] | None = None
) -> list[str]:
    """Every tracker namespace linked to the org's `code_namespace`, primary
    first: a maintainer's links first ("" there clears the config's); then,
    for a repository the operator configured (`operator`, read when not
    given), the config's exact key, then the first glob that matches; for a
    branch added from an operator repository, that repository's links, read
    now; [] when none."""
    if not code_namespace:
        return []
    over = override_for(org, code_namespace)
    if over is not None:
        return parse_binding(over)
    source = _BRANCH_OF.get((org, code_namespace), "")
    m = bindings()
    if not m and not source:
        return []
    if operator is None:
        operator = operator_repositories()
    if (org, code_namespace) not in operator:
        # Issue and PR numbers are repository-wide: a branch reads its repository's
        # trackers. Only an operator source is followed, one level, and only for a
        # registered branch: a removal or a late copy can leave the row behind.
        if source and (org, source) in operator:
            from server.code.domain.index import repos

            if (org, code_namespace) in repos.registry_namespaces():
                return tracker_namespaces_for(org, source, operator=operator)
        return []
    if not m:
        return []
    if code_namespace in m:
        return parse_binding(m[code_namespace])
    for pattern, target in m.items():
        if fnmatch.fnmatchcase(code_namespace, pattern):
            return parse_binding(target)
    return []


def tracker_namespace_for(
    org: str, code_namespace: str, *, operator: Collection[tuple[str, str]] | None = None
) -> str:
    """The PRIMARY tracker namespace of `code_namespace` — the record whose
    numbers are the repository's — or "" (lookups by number
    read only it)."""
    linked = tracker_namespaces_for(org, code_namespace, operator=operator)
    return linked[0] if linked else ""


def code_namespaces_linking(
    org: str,
    tracker_ns: str,
    code_namespaces: list[str],
    *,
    operator: Collection[tuple[str, str]] | None = None,
) -> list[str]:
    """Of the org's `code_namespaces`, those that link `tracker_ns` (primary or
    not) — the reverse lookup the binding gate needs for a tracker read by the
    readers of a repository it is linked to. The operator's repositories are
    read once for the whole list when `operator` is not given."""
    if not code_namespaces:
        return []
    if operator is None:
        operator = operator_repositories()
    return [
        c
        for c in code_namespaces
        if tracker_ns in tracker_namespaces_for(org, c, operator=operator)
    ]


async def _gated(org: str, code_namespace: str, ns: str, *, hides_answer: bool) -> bool:
    """The binding gate for one linked tracker, as the current principal:
    a STANDARD one by whoever may read the code namespace, a RESTRICTED one
    (or one the tracker plane does not know) by its own two-factor gate.
    Closed on any error."""
    try:
        from server.auth.authz import row_in_scope
        from server.operations.base import Role
        from server.tracker.domain.namespaces import tracker_namespace

        target = tracker_namespace(org, ns)
        standard = target is not None and not target.restricted
        return bool(
            await row_in_scope(
                org, code_namespace if standard else ns, Role.VIEWER, hides_answer=hides_answer
            )
        )
    except Exception as e:  # noqa: BLE001 — the binding gate fails closed on any error
        log.debug("qa binding authorization failed (closed): %s", e)
        return False


async def authorized_tracker_namespaces(
    org: str, code_namespace: str, *, hides_answer: bool = True
) -> list[str]:
    """Every linked tracker the current principal may read,
    primary first; a refused one is left out in silence (never named).
    `hides_answer`: see `authz.row_in_scope`."""
    await refresh_overrides()
    return [
        ns
        for ns in tracker_namespaces_for(org, code_namespace)
        if await _gated(org, code_namespace, ns, hides_answer=hides_answer)
    ]


async def authorized_tracker_namespace(
    org: str,
    code_namespace: str,
    *,
    hides_answer: bool = True,
    operator: Collection[tuple[str, str]] | None = None,
) -> str:
    """`tracker_namespace_for` passed through the gate for the current principal;
    "" when unbound, unauthorized, or the check itself fails (closed).

    Which gate depends on the target. A STANDARD tracker namespace
    bound to a code namespace is read by whoever may read that code
    namespace — the right the transport already proved for the code op —
    so a reader of the repository never needs a grant, a join or a token
    naming the tracker namespace to see its public QA facts. A RESTRICTED QA
    namespace, or one the tracker plane does not know, keeps its
    own two-factor gate on the tracker namespace itself. `hides_answer`: see
    `authz.row_in_scope`; `operator`: see `tracker_namespaces_for`."""
    await refresh_overrides()
    ns = tracker_namespace_for(org, code_namespace, operator=operator)
    if not ns or not await _gated(org, code_namespace, ns, hides_answer=hides_answer):
        return ""
    return ns


async def resolve_binding(org: str, namespace: str) -> dict[str, Any]:
    """What the addressed namespace may ask the tracker plane:
    a tracker namespace answers itself; a code namespace answers the QA
    namespace bound to it, passed through the gate for the current
    principal; anything else — unbound, refused, or not a tracker namespace at
    all — is one indistinguishable answer (`kind = none`, `authorized =
    false`, no name), so the reply never confirms that a restricted
    namespace exists. `templates` are the catalogue templates the principal
    may run: the public ones, plus the restricted class when the answering
    namespace is restricted."""
    from server.tracker.domain.catalogue import template_names
    from server.tracker.domain.namespaces import tracker_namespace

    none: dict[str, Any] = {
        "tracker_namespace": "",
        "restricted": False,
        "kind": "none",
        "authorized": False,
        "templates": [],
    }
    await refresh_overrides()
    own = tracker_namespace(org, namespace)
    if own is not None:
        return {
            "tracker_namespace": namespace,
            "restricted": own.restricted,
            "kind": "tracker",
            "authorized": True,
            "templates": template_names(own.restricted),
        }
    primary = tracker_namespace_for(org, namespace)
    target = tracker_namespace(org, primary)
    if target is None:  # unbound, or bound to a name the tracker plane does not serve
        return none
    # Every linked tracker this principal may read, primary
    # first; a refused primary is `none` whatever else is linked (never named)
    linked = await authorized_tracker_namespaces(org, namespace)
    if not linked or linked[0] != primary:
        return none
    return {
        "tracker_namespace": primary,
        "tracker_namespaces": linked,
        "restricted": target.restricted,
        "kind": "code",
        "authorized": True,
        "templates": template_names(target.restricted),
    }


__all__ = [
    "BRANCH_OF_KEY",
    "OVERRIDE_KEY",
    "authorized_tracker_namespace",
    "authorized_tracker_namespaces",
    "bindings",
    "code_namespaces_linking",
    "encode_binding",
    "forget_links",
    "operator_repositories",
    "parse_binding",
    "resolve_binding",
    "tracker_namespace_for",
    "tracker_namespaces_for",
]
