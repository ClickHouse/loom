"""code.repos: the repositories a caller can see, each with its index status, bound
trackers and directory access state."""

from __future__ import annotations

import asyncio
import functools
import logging
import re
from typing import Any

from server.code.api.models_repos import CodeReposInput, CodeReposOutput, CodeRepoSummary
from server.code.app.common import require_code_intel
from server.code.app.index_status import _repo_status_cached, _s, _status_cache
from server.code.app.spans import set_code_span_attributes
from server.code.store.index import CodeRepo
from server.core.grant_sql import LIVE_GRANT
from server.operations.read_projection import project_rows

logger = logging.getLogger(__name__)


def repo_slug(value: str) -> str:
    """`owner/name`, lowercased, from a repository name or URL — the same reduction the
    plugin's shunt applies to `git remote get-url origin` (`repoSlug` in lib/code-shunt.mjs), so
    the two sides agree on the key. '' when the value has no owner/name."""
    v = str(value or "").strip().rstrip("/")
    if v.endswith(".git"):
        v = v[:-4]
    parts = [x for x in re.split(r"[:/]", v) if x]
    return f"{parts[-2]}/{parts[-1]}".lower() if len(parts) >= 2 else ""


async def handle_code_repos(input: CodeReposInput) -> CodeReposOutput:
    """Every configured repo entry in the org the caller can read. The
    transport only role-gated this call (skip_namespace_authz); each entry is
    checked here against the caller's own claims/grants and silently omitted
    on failure — a viewer with no code grant sees an empty list, not a 403.
    `repos` candidates resolve to the first one the caller
    reads, else to a candidate fork's upstream (`_resolve_candidates`)."""
    require_code_intel()
    slugs = _candidate_slugs(input)
    out = await _code_repo_rows(input, set(slugs) if slugs else None)
    if slugs and input.repos:
        out = await _resolve_candidates(input, slugs, out)
    set_code_span_attributes(results=len(out), consumer=input.consumer)
    # tier_health always: a caller asking for it by name would get a 422 from a replica
    # of the build before it during a rolling upgrade
    project_rows(out, CodeRepoSummary, input.fields, always=frozenset({"namespace", "tier_health"}))
    return CodeReposOutput(repos=out)


def _candidate_slugs(input: CodeReposInput) -> list[str]:
    """`repo` then each `repos` entry, reduced to owner/name, de-duplicated, order kept; [] =
    the full listing (an empty `repo`)."""
    raw = [input.repo, *input.repos]
    return [s for s in dict.fromkeys(repo_slug(r) for r in raw) if s]


def _rows_for(rows: list[Any], slug: str) -> list[Any]:
    return [r for r in rows if repo_slug(r.repo) == slug]


def _first_readable(rows: list[Any], pairs: list[tuple[str, str]], via: str) -> list[Any]:
    """The rows of the first `(checkout_slug, slug)` pair with a readable
    row, stamped with how they matched; [] when none has one."""
    for checkout, slug in pairs:
        mine = _rows_for(rows, slug)
        if any(r.readable for r in mine):
            for r in mine:
                r.matched_via, r.checkout_slug = via, checkout
            return mine
    return []


async def _resolve_candidates(
    input: CodeReposInput, slugs: list[str], rows: list[Any]
) -> list[Any]:
    """The first candidate the caller reads wins. None → ask GitHub whether the first
    candidates are forks and list the parent / source through the same per-entry gate (no new
    access path): only a readable upstream answers. Nothing readable → the first candidate that
    has request-only rows, else []."""
    from server.code.app.index.fork_upstream import fork_upstreams

    hit = _first_readable(rows, [(s, s) for s in slugs], "candidate")
    if hit:
        return hit
    pairs = [(c, u) for c, u in await fork_upstreams(slugs) if u not in slugs]
    if pairs:
        up_rows = await _code_repo_rows(input, {u for _, u in pairs})
        hit = _first_readable(up_rows, pairs, "fork_of")
        if hit:
            return hit
    return next((m for m in (_rows_for(rows, s) for s in slugs) if m), [])


async def _code_repo_rows(input: CodeReposInput, wanted: set[str] | None) -> list[Any]:
    """The listing's rows, gated per entry; `wanted` (owner/name slugs)
    restricts it to those repositories, None = every configured entry."""
    from server.auth.context import current_auth
    from server.code.app.index.bootstrap import is_searchable

    # Repositories added from the page appear here as soon as
    # the snapshot refreshes (TTL-bounded; forced by the add itself).
    from server.code.app.index.repo_registry import refresh_db_repos
    from server.code.domain.index.repos import configured_repos, docs_namespaces

    await refresh_db_repos()
    auth = current_auth()
    out: list[CodeRepoSummary] = []
    # input.org is the transport's authz identity, not a filter: a code plane
    # is a deployment-level asset, and a caller whose own org differs (dev
    # mode's "dev", a federated login) may still hold a grant on it. Every
    # row carries its org; the per-entry check below is the real gate.
    # The directory's join policy and the caller's standing
    # ride every row so the Code view can offer Join / Request / Cancel.
    policies, standing, roles, maintained = await _repo_directory_state(auth)
    from server.identity.app.namespace_classification import is_restricted_cached

    entries = list(configured_repos())
    # A caller that names its repository gets that one's
    # entries; filter before the status prefetch so nothing else is warmed.
    if wanted is not None:
        entries = [c for c in entries if repo_slug(getattr(c, "repo", "")) in wanted]
    # Gate first: an entry the caller may not read is either hidden or listed
    # request-only, and neither needs its status, so the git calls, counts
    # reads and snapshot writes below run only for the readable entries.
    gated: list[tuple[Any, bool]] = []
    for cfg in entries:
        readable = await _listing_gate(
            auth, cfg, policies=policies, roles=roles, grants_read=maintained is not None
        )
        if readable is not None:
            gated.append((cfg, readable))
    readable_entries = [c for c, readable in gated if readable]
    # Warm every readable entry's status concurrently (cache misses
    # only); the loop below then reads them without a round trip each.
    # Every namespace's state — the shared counts
    # row included — in one statement, handed to each status computation,
    # so a cold listing issues one read for the table instead of two per
    # namespace (74 statements → a handful).
    cold = [c for c in readable_entries if (c.org, c.namespace) not in _status_cache]
    states: dict[tuple[str, str], dict[str, Any]] = {}
    if cold:
        try:
            states = await CodeRepo().all_state_many([(c.org, c.namespace) for c in cold])
        except Exception as e:  # noqa: BLE001 — each status then reads its own state
            logger.debug("code.repos: batched state read failed, per-namespace reads follow: %s", e)
    await asyncio.gather(
        *(
            _repo_status_cached(c, state=states.get((c.org, c.namespace)), listing=True)
            for c in readable_entries
        ),
        return_exceptions=True,
    )
    docs_ns = docs_namespaces()  # every entry, not only the listed ones
    bound = await _bound_trackers()
    for cfg, readable in gated:
        key = (cfg.org, cfg.namespace)
        # the class rides every row (readable or not): the view's pill and
        # request copy read it, never the viewer's own standing
        restricted = is_restricted_cached(cfg.org, cfg.namespace)
        # Fail-open — a failed grants read (maintained is
        # None) says "maintained" so the page never offers adopt on a guess.
        has_maintainer = maintained is None or key in maintained
        if readable:
            st = await _repo_status_cached(cfg)
            out.append(
                CodeRepoSummary(
                    org=st.org,
                    namespace=st.namespace,
                    repo=st.repo,
                    branch=st.branch,
                    git_head=st.git_head,
                    clone_head=st.clone_head,
                    clone_fetched_at=str(getattr(st, "clone_fetched_at", "") or ""),
                    fetch_overdue=bool(getattr(st, "fetch_overdue", False)),
                    fast_tier_lag=st.fast_tier_lag,
                    scip_lag=st.scip_lag,
                    scip_py_lag=st.scip_py_lag,
                    scip_producer=str(getattr(st, "scip_producer", "") or ""),
                    tier_health=list(st.tier_health),
                    indexed_at=(st.state.get("git_head") or {}).get("updated_at", ""),
                    last_refresh_at=st.last_refresh_at,
                    counts=st.counts,
                    join_policy=policies.get((cfg.org, cfg.namespace), "approval"),
                    classification="restricted" if restricted else "standard",
                    my_status=standing.get((cfg.org, cfg.namespace), "none"),
                    my_role=roles.get((cfg.org, cfg.namespace), ""),
                    source=cfg.source,
                    stage=_s((st.state.get("bootstrap_stage") or {}).get("value")),
                    stage_detail=_s((st.state.get("bootstrap_stage") or {}).get("detail")),
                    chunks_total=st.chunks_total,
                    chunks_embedded=st.chunks_embedded,
                    embedding_coverage=st.embedding_coverage,
                    searchable=is_searchable(st.embedding_coverage),
                    has_maintainer=has_maintainer,
                    docs_namespace=docs_ns.get((cfg.org, cfg.repo), ""),
                    primary_namespace=docs_ns.get((cfg.org, cfg.repo), ""),
                    # The links as they are, said only to the maintainer
                    bound_trackers=(
                        bound(cfg.org, cfg.namespace) if roles.get(key, "") == "admin" else None
                    ),
                )
            )
        else:
            out.append(
                CodeRepoSummary(
                    org=cfg.org,
                    namespace=cfg.namespace,
                    repo=cfg.repo,
                    branch=cfg.git_branch,
                    readable=False,
                    join_policy=policies.get(key, "approval"),
                    classification="restricted" if restricted else "standard",
                    my_status=standing.get(key, "none"),
                    my_role=roles.get(key, ""),
                    source=cfg.source,
                    has_maintainer=has_maintainer,
                    unreadable_reason=_unreadable_reason(auth, cfg, restricted, roles.get(key, "")),
                )
            )
    return out


async def _bound_trackers() -> Any:
    """The trackers each (org, code namespace) links, primary
    first — the maintainer's links, else the deployment's map ([] = none) —
    from the snapshot `code.repo.set_source` refreshes; a failed refresh
    keeps the last one. The page shows the binding as it is, not a guess
    from which record names the repository. The operator's repositories are read
    once for the whole listing."""
    from server.tracker.app import binding

    await binding.refresh_overrides()  # never raises: a failed one keeps the last snapshot
    return functools.partial(
        binding.tracker_namespaces_for, operator=binding.operator_repositories()
    )


def _unreadable_reason(auth: Any, cfg: Any, restricted: bool, role: str) -> str:
    """Why the gate refused this row, in the vocabulary the
    row's own Join / Request buttons already speak — never more. `role` is
    the caller's ACTIVE grant on the entry (empty = none). A restricted entry
    (rule 2) is refused to its own grant holder when the
    sign-in token does not name it: that is `token_scope`, the one case the
    caller can fix alone (re-sign-in with a token naming the namespace)."""
    from server.auth.authz import names_namespace

    if not role:
        return "not_member"
    if restricted and not names_namespace(auth, cfg.org, cfg.namespace):
        return "token_scope"
    return "closed"


_Keyed = dict[tuple[str, str], str]
_KeySet = set[tuple[str, str]]


async def _listing_gate(
    auth: Any,
    cfg: Any,
    *,
    policies: dict[tuple[str, str], str],
    roles: dict[tuple[str, str], str],
    grants_read: bool,
) -> bool | None:
    """How `code.repos` lists one entry to this caller: True readable, False
    request-only, None hidden. The grants were read once for the whole listing
    (`_repo_directory_state`); the gate takes that answer instead of one grants
    read per repository. A failed grants read leaves the gate to its own."""
    from server.auth.authz import check_authorized
    from server.identity.app.namespace_classification import is_restricted_cached
    from server.operations.base import Role

    if auth is None:
        return True
    key = (cfg.org, cfg.namespace)
    try:
        await check_authorized(
            auth,
            Role.VIEWER,
            org=cfg.org,
            namespace=cfg.namespace,
            **_known_grant_kw(roles, key, grants_read=grants_read),
        )
        return True
    except Exception:  # noqa: BLE001 — PermissionError / IdentityIncomplete
        # A principal of the same org sees every
        # repository it may ask to join — open (one click) or
        # approval (a maintainer decides) — as a request-only entry:
        # name, branch, rule, own standing; no counts, no freshness.
        # Closed repositories, and every repository to a caller from
        # another org, stay hidden. Flat-ADMIN callers still see a
        # standard closed entry flagged unreadable (the adopt path).
        # A restricted entry is listed only when its
        # rule is approval — the request path the user asked for —
        # and never to another org.
        policy = policies.get(key, "approval")
        if getattr(auth, "org", None) not in ("*", cfg.org):
            return None
        restricted = is_restricted_cached(cfg.org, cfg.namespace)
        if policy == "closed" and (restricted or not auth.has_role(Role.ADMIN)):
            return None
        return False


def _known_grant_kw(
    roles: dict[tuple[str, str], str], key: tuple[str, str], *, grants_read: bool
) -> dict[str, Any]:
    """The `known_grant` argument for the gate — the
    principal's granted role on `key` from the listing's one grants read, or
    None when that read found none; nothing (the gate reads for itself) when
    the grants read failed."""
    from server.operations.base import Role  # the ops package imports this module

    if not grants_read:
        return {}
    by_name = {"viewer": Role.VIEWER, "writer": Role.WRITER, "admin": Role.ADMIN}
    return {"known_grant": by_name.get(str(roles.get(key, "")).lower())}


async def _repo_directory_state(auth: Any) -> tuple[_Keyed, _Keyed, _Keyed, _KeySet | None]:
    """(org, namespace) → resolved join policy, → the caller's standing
    (`member` / `pending` / `none`), → the caller's active role, and the set
    of entries that HAVE an active admin (None when that read
    failed, which the caller renders as "maintained"), for every configured
    code namespace. Three statements, fail-open to defaults: the repo list
    must never fail because the directory could not be read."""
    from server.auth.default_namespace import auth_off_subject
    from server.code.domain.index.repos import configured_repos
    from server.core.ch import get_client
    from server.identity.domain import join_policy as _jp

    keys = [(c.org, c.namespace) for c in configured_repos()]
    if not keys:
        return {}, {}, {}, set()
    policies: _Keyed = {}
    standing: _Keyed = {}
    roles: _Keyed = {}
    maintained: _KeySet | None = None
    try:
        ch = get_client()
        # A delegated token reads as its creator, whose grants its gate checks.
        subject = (
            str(getattr(auth, "creator", "") or getattr(auth, "subject", "") or "")
            if auth is not None
            else auth_off_subject()
        )
        org = str(getattr(auth, "org", "") or "") if auth is not None else ""

        async def _aliases() -> list[str]:
            if not subject:
                return []
            # The login handle and the verified email
            # are one person; a grant under either spelling is the caller's
            from server.auth.aliases import subject_aliases

            return await subject_aliases(org, subject, ch=ch) if org else [subject]

        # The three independent reads leave together
        # (policies, maintainers, the caller's aliases); only the grants read
        # waits, on the aliases.
        rows, maintained_now, subs = await asyncio.gather(
            ch.query_all(
                """
                SELECT org, namespace, toString(visibility) AS visibility, requires_approval,
                       ifNull(kind, 'memory') AS kind, toString(join_policy) AS join_policy
                FROM loom.namespaces FINAL
                WHERE (org, namespace) IN {keys:Array(Tuple(String, String))}
                """,
                params={"keys": keys},
            ),
            _maintained_entries(ch, keys),
            _aliases(),
        )
        for r in rows:
            policies[(str(r["org"]), str(r["namespace"]))] = _jp.effective_join_policy(r)
        if subject:
            grants = await ch.query_all(
                """
                SELECT org, namespace, ifNull(status, '') AS status, toString(role) AS role
                FROM loom.namespace_grants FINAL
                WHERE subject IN {subs:Array(String)} AND revoked_at IS NULL
                  AND (org, namespace) IN {keys:Array(Tuple(String, String))}
                """,
                params={"subs": subs, "keys": keys},
            )
            for g in grants:
                key = (str(g["org"]), str(g["namespace"]))
                if str(g.get("status") or "active") in ("", "active"):
                    roles[key] = str(g.get("role") or "")
                pending = str(g.get("status") or "active") == "pending"
                standing[key] = "pending" if pending else "member"
        # Set only once the grants are read: a non-None `maintained` tells the
        # gate the listing read them, so a failed read must leave it None and
        # the gate reads each grant for itself.
        maintained = maintained_now
    except Exception:  # noqa: BLE001 — directory state is an affordance, not a gate
        return policies, standing, roles, maintained
    return policies, standing, roles, maintained


async def _maintained_entries(ch: Any, keys: list[tuple[str, str]]) -> _KeySet:
    """The configured entries on which ANY subject holds an
    active admin grant — one grouped statement over the grants table for all
    keys, the same predicate `grant_role_for` applies to one subject. Not
    the caller's grants: the question is "does anyone maintain it", so the
    page offers adopt only on a truly ownerless entry."""
    rows = await ch.query_all(
        """
        SELECT org, namespace
        FROM loom.namespace_grants FINAL
        WHERE role = 'admin' AND {live}
          AND (org, namespace) IN {keys:Array(Tuple(String, String))}
        GROUP BY org, namespace
        """.replace("{live}", LIVE_GRANT),
        params={"keys": keys},
    )
    return {(str(r["org"]), str(r["namespace"])) for r in rows}
