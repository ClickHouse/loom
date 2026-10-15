"""MCP transport — caller identity / provenance resolution.

Leaf module: client-name slugification, the session→agent cache, and the
caller-default-workspace resolution. No imports back into
``server.transport.mcp``; constants come from ``mcp_config``.
"""

from __future__ import annotations

import uuid
from contextvars import ContextVar
from typing import TYPE_CHECKING, Any, cast

from server.auth.authz import live_claims
from server.auth.personal_namespace import derive_personal_slug
from server.core.naming import slugify
from server.identity.app import caller_defaults
from server.transport.mcp_config import _DEFAULT_MCP_AGENT, _MCP_SESSION_AGENT_CAP

if TYPE_CHECKING:
    from server.auth.jwt import AuthInfo

# Bounded session→agent cache (sessions are short-lived; cap so a churning
# long-running process can't grow it unbounded).
_MCP_AGENT_BY_SESSION: dict[str, str] = {}


# A client whose MCP `clientInfo.name` differs from the agent id its own hooks
# write under: Codex's MCP client says `codex-mcp-client`, its
# plugin hooks say `codex`. One agent per client keeps a key the model saved
# over MCP and the facts its hooks captured under the same identity.
_CLIENT_AGENT_ALIASES = {"codex-mcp-client": "codex"}


def _client_name_to_agent(client_name: str | None) -> str:
    """Slugify an MCP `clientInfo.name` into a Loom agent id; fall back to
    ``_DEFAULT_MCP_AGENT`` when absent or empty."""
    if not client_name:
        return _DEFAULT_MCP_AGENT
    slug = slugify(client_name, max_len=64) or _DEFAULT_MCP_AGENT
    return _CLIENT_AGENT_ALIASES.get(slug, slug)


def _remember_client_for_session(session_id: str | None, client_name: str | None) -> None:
    """Cache the slugified agent for a session, keyed on the MCP
    `Mcp-Session-Id` header captured at the `initialize` handshake."""
    if not session_id or not client_name:
        return
    agent = _client_name_to_agent(client_name)
    if agent == _DEFAULT_MCP_AGENT:
        return
    # Drop the whole cache rather than track LRU — a miss only reverts to the
    # default agent, and in-flight sessions re-populate on next initialize.
    if len(_MCP_AGENT_BY_SESSION) >= _MCP_SESSION_AGENT_CAP:
        _MCP_AGENT_BY_SESSION.clear()
    _MCP_AGENT_BY_SESSION[session_id] = agent


def new_session_id(client_name: str | None) -> str:
    """A server-assigned `Mcp-Session-Id` that carries its client's agent
    slug: ``<32 hex>.<slug>``. The session→agent cache is per process and
    the server runs several uvicorn workers, so a `tools/call` that lands on a
    worker which did not serve `initialize` would miss it and write as the
    default agent — one session's calls split between two agent ids, and a key
    read by exact identity (history, related) missing its own writes. The id is
    visible ASCII (the spec's constraint) and names provenance only, never
    authority."""
    sid = uuid.uuid4().hex
    agent = _client_name_to_agent(client_name)
    return sid if agent == _DEFAULT_MCP_AGENT else f"{sid}.{agent}"


def _agent_from_session_id(session_id: str) -> str | None:
    """The slug `new_session_id` embedded, or None for any other shape."""
    head, sep, slug = session_id.partition(".")
    if not sep or len(head) != 32 or not slug:
        return None
    if any(c not in "0123456789abcdef" for c in head):
        return None
    return slug if slugify(slug, max_len=64) == slug else None


def _resolve_client_name(payload: Any, session_id: str | None) -> str | None:
    """Resolve the MCP client name for a request. Priority: inline
    `params.clientInfo.name`, else the value cached against `Mcp-Session-Id`
    at handshake time, else the slug the server-assigned id carries (any
    worker can read it). None when none is available."""
    if isinstance(payload, dict):
        params = payload.get("params")
        if isinstance(params, dict):
            client_info = params.get("clientInfo")
            if isinstance(client_info, dict):
                name = client_info.get("name")
                if isinstance(name, str) and name.strip():
                    return name
    if session_id:
        return _MCP_AGENT_BY_SESSION.get(session_id) or _agent_from_session_id(session_id)
    return None


def home_namespace(accessible: list[str], subject: str, fallback: str = "") -> str:
    """The namespace an unaddressed call addresses first: the one named after
    the caller (`alice` for subject `alice` or `alice@…`); else `fallback`
    when it is in the list (the caller's saved default); else the first of the
    list. Used both for unaddressed searches and for the default a write lands
    in, so a teammate's namespace that sorts first never outranks the
    caller's own."""
    if not accessible:
        return ""
    login = str(subject or "").strip().lower().split("@", 1)[0]
    if login:
        for ns in accessible:
            if str(ns).lower() == login:
                return ns
    if fallback and fallback in accessible:
        return fallback
    return accessible[0]


def pick_default_namespace(
    claimed: list[str],
    kinds: dict[str, str],
    can_write: Any,
    owner_login: str,
    owner_default: str = "",
) -> str:
    """The default a call with no namespace lands in: a MEMORY-kind namespace
    the token can WRITE, the owner's own first. A service-account token minted
    from the Setup tab claims its owner's namespace plus viewer reads on code
    and QA indexes; ranking grant rows alone would see only the viewer rows
    (the claim carries the writable one) and land an unaddressed memory.set in
    a code index, where it fails the role check. "" when nothing qualifies."""
    memory_ns = [n for n in claimed if str(kinds.get(n, "memory") or "memory") in ("memory", "")]
    writable = [n for n in memory_ns if can_write(n)]
    return home_namespace(writable or memory_ns, owner_login, owner_default)


def is_memory_home(kinds: dict[str, str], namespace: str) -> bool:
    """Whether an UNADDRESSED call may land in `namespace` — a memory-kind
    namespace only. A tracker/QA, code or any other plane is
    reached by naming it; never as a default. Unknown kind (not in the
    directory) counts as memory, as the directory's own default does."""
    return str(kinds.get(namespace, "memory") or "memory") == "memory"


def memory_home_from_grants(rows: list[dict[str, Any]], kinds: dict[str, str], subject: str) -> str:
    """The grant ranking's pick among memory-kind namespaces only (role tier
    then recency, as ranked), the caller's own first, so an admin grant on a
    QA plane never outranks the caller's writer grant on their own namespace
    and draws unaddressed writes into it."""
    ordered = [str(r.get("namespace") or "") for r in rows]
    memory = [n for n in ordered if n and is_memory_home(kinds, n)]
    return home_namespace(memory, subject) if memory else ""


# An unaddressed call whose default is NOT the caller's own home namespace
# says where it landed, so a model does not read a team plane's rows as
# "yours". The common case (home, or an explicit namespace) adds nothing.
_DEFAULT_NOTE: ContextVar[str] = ContextVar("loom_default_namespace_note", default="")


def note_default_namespace(filled: bool, namespace: str, subject: str) -> None:
    login = str(subject or "").strip().lower().split("@", 1)[0]
    away = bool(filled and namespace and namespace.lower() != login)
    _DEFAULT_NOTE.set(namespace if away else "")


def default_namespace_note() -> dict[str, str]:
    ns = _DEFAULT_NOTE.get()
    return {"namespace_defaulted": ns} if ns else {}


# The grant roles that make a namespace a write default.
_WRITE_ROLES = frozenset({"writer", "admin"})


def _scoped(auth_info: Any) -> bool:
    try:
        return bool(auth_info is not None and auth_info.has_ns_roles())
    except Exception:  # noqa: BLE001 — an odd auth object is not scoped
        return False


def token_can_write(auth_info: Any, org: str, namespace: str) -> bool:
    """Whether THIS token may write `namespace`. A scoped token's `ns_roles` entry is
    authoritative (viewer there is not a write default) inside its `ns_write` claim; a
    token with an explicit write scope (`ns_write`) answers by it; any other token writes
    wherever its grants do, so the grant role decides (the JWT-only `can_write` does not
    see grants)."""
    if auth_info is None:
        return True
    try:
        if _scoped(auth_info):
            from server.operations.base import Role

            role = auth_info.effective_role(org, namespace)
            if role is None or role < Role.WRITER:
                return False
            return not auth_info.write_namespaces or bool(auth_info.can_write(org, namespace))
        if getattr(auth_info, "write_namespaces", None):
            return bool(auth_info.can_write(org, namespace))
        return True
    except Exception:  # noqa: BLE001 — an odd auth object writes nowhere
        return False


def saved_pick_ok(
    saved: str,
    grant_role: str,
    kinds: dict[str, str],
    auth_info: Any,
    org: str,
    claimed: list[str],
) -> bool:
    """The /me switcher pick is honoured as the default only when it is a
    memory plane, the caller holds a WRITE grant there (a viewer pick is where
    the user was looking, not where writes go), and a scoped token claims it
    (its claim list is authoritative)."""
    if not saved or not is_memory_home(kinds, saved):
        return False
    if _scoped(auth_info) and saved not in claimed:
        return False
    if str(grant_role or "").lower() not in _WRITE_ROLES:
        return False
    return token_can_write(auth_info, org, saved)


def ranked_grant_default(
    rows: list[dict[str, Any]],
    kinds: dict[str, str],
    subject: str,
    auth_info: Any,
    org: str,
    claimed: list[str],
) -> str:
    """The grant ranking's pick. A scoped token ranks only the namespaces it
    claims, in claim order. Writable memory namespaces first, the caller's
    own ahead; the read-only ones only when
    nothing is writable, so a viewer-only caller still reads its planes."""
    if _scoped(auth_info):
        order = {n: i for i, n in enumerate(claimed)}
        rows = sorted(
            (r for r in rows if str(r.get("namespace") or "") in order),
            key=lambda r: order[str(r.get("namespace") or "")],
        )
    writable = [
        r
        for r in rows
        if str(r.get("role") or "").lower() in _WRITE_ROLES
        and token_can_write(auth_info, org, str(r.get("namespace") or ""))
    ]
    return memory_home_from_grants(writable, kinds, subject) or memory_home_from_grants(
        list(rows), kinds, subject
    )


async def _kinds_or_empty(org: str) -> dict[str, str]:
    try:
        from server.identity.app.namespace_kinds import kind_map

        return dict(await kind_map(org))
    except Exception:  # noqa: BLE001 — fail-open: unknown kinds count as memory
        return {}


async def _claimed_home_default(auth_info: Any, org: str, claimed: list[str], ch: Any) -> str:
    """`pick_default_namespace` over live inputs: namespace kinds, the
    token's own write test, and — for a service account — its owner's login
    and saved default. Fail-open to "" (the grant ranking then decides)."""
    if not claimed:
        return ""
    try:
        from server.identity.app.namespace_kinds import kind_map

        kinds = await kind_map(org)
        owner = str(getattr(auth_info, "subject", "") or "")
        if getattr(auth_info, "is_service_account", False):
            from server.auth.service_accounts import sa_creator

            sa = await sa_creator(org, owner, ch=ch)
            owner = sa.subject if sa is not None else owner
        owner_default = await caller_defaults.saved_default(org, owner, ch=ch)

        def _can_write(ns: str) -> bool:
            if _scoped(auth_info):  # a scoped viewer claim is not writable
                return token_can_write(auth_info, org, ns)
            try:
                return bool(auth_info.can_write(org, ns))
            except Exception:  # noqa: BLE001 — an odd auth object writes nowhere
                return False

        return pick_default_namespace(claimed, kinds, _can_write, owner, owner_default)
    except Exception:  # noqa: BLE001 — fail open to the grant ranking
        return ""


def caller_org(auth: Any) -> str | None:
    """The org a caller acts in by default: its token's (the self-signup org for
    a `*` token), or the single-user org with no auth; None when no caller can
    be identified."""
    from server.settings import settings

    if auth is not None:
        auth_info = cast("AuthInfo", auth)
        if not auth_info.subject:
            return None
        if auth_info.org and auth_info.org != "*":
            return auth_info.org
        return settings.self_signup_org
    if settings.effective_identity_provider() == "single-user":
        return settings.self_signup_org
    return None


async def _caller_defaults(
    auth: Any,
    client_name: str | None = None,
) -> dict[str, str] | None:
    """Resolve the caller's default (org, namespace, agent).

    Used both to embed defaults in tool descriptions and to fill missing
    args on incoming tool calls. ``client_name`` (the MCP `clientInfo.name`)
    becomes the agent default when present. None when no caller can be
    identified — falls back to handler-level validation errors.
    """
    from server.settings import settings

    org = caller_org(auth)
    if org is None:
        return None
    if auth is not None:
        auth_info = cast("AuthInfo", auth)
        subject = auth_info.subject
        if subject:
            org = (
                auth_info.org
                if auth_info.org and auth_info.org != "*"
                else settings.self_signup_org
            )
            claimed_namespaces = await live_claims(
                auth_info, org, getattr(auth_info, "namespaces", None) or ()
            )
        else:
            return None
    elif settings.effective_identity_provider() == "single-user":
        subject = "you"
        org = settings.self_signup_org
        claimed_namespaces = []
    else:
        subject = "you"
        claimed_namespaces = []

    # Single-user mode serves exactly ONE namespace (LOOM_DEFAULT_NAMESPACE):
    # the SPA pins its identity to it and the boot-time bootstrap provisions
    # only it, so the MCP/SDK default must be the same name — falling through
    # to `personal-<sub>` (or to a stale user_state row written by a switcher
    # click) silently splits agent writes from the dashboard, the exact
    # orphaned-data case the setting exists to close. A namespace the token
    # explicitly claims (a Setup-tab service-account token) still wins; the
    # user_state / grant lookups below are multi-user machinery and are
    # deliberately skipped.
    if settings.effective_identity_provider() == "single-user":
        return {
            "org": org,
            "namespace": (
                claimed_namespaces[0] if claimed_namespaces else settings.default_namespace
            ),
            "agent": _client_name_to_agent(client_name),
        }

    user_state_subject = subject

    # Base default: a namespace the token explicitly claims (e.g. the one a
    # Setup-tab service-account token was minted for) beats the personal-<sub>
    # slug — otherwise an MCP client lands in a junk personal-sa-<client>
    # namespace instead of the user's real one. user_state / grants below still
    # override when present.
    namespace = claimed_namespaces[0] if claimed_namespaces else derive_personal_slug(subject)
    namespace_resolved = False
    try:
        from server.core.ch import get_client

        ch = get_client()

        from server.auth.aliases import subject_aliases

        subs = await subject_aliases(org, subject, ch=ch)
        kinds = await _kinds_or_empty(org)  # only a memory plane is a home
        # Priority 1: caller's explicit pick in /me's switcher. Validated
        # against current grants before trust — a stale saved default
        # (grant since revoked) must not keep coming back.
        saved_ns = await caller_defaults.saved_default(org, user_state_subject, ch=ch)
        # A switcher pick of a QA/code namespace is where the user was
        # LOOKING, not where an agent's memory goes.
        if saved_ns and is_memory_home(kinds, saved_ns):
            # Validate against current grants before trust — grants are the
            # authz boundary, and a stale saved default (grant since revoked)
            # must not keep coming back. It needs a WRITE grant, and a scoped
            # token's claim. (Single-user mode never reaches here.)
            role = await caller_defaults.live_role_on(org, saved_ns, subs, ch=ch)
            if saved_pick_ok(saved_ns, role, kinds, auth, org, claimed_namespaces):
                namespace = saved_ns
                namespace_resolved = True
        if not namespace_resolved and auth is not None:
            # The token's own writable memory-kind claim, owner first, beats a
            # viewer grant on an index.
            pick = await _claimed_home_default(auth_info, org, claimed_namespaces, ch)
            if pick:
                namespace = pick
                namespace_resolved = True
        if not namespace_resolved:
            # Priority 2 (no/invalid saved default): rank grants by role tier
            # first, recency second. Recency-only ranked a later viewer grant
            # above the user's own admin namespace; role tier is the better
            # primary signal.
            rows = await caller_defaults.ranked_grants(org, subs, ch=ch)
            namespace = (
                ranked_grant_default(
                    list(rows or []), kinds, subject, auth, org, claimed_namespaces
                )
                or namespace
            )
        if not is_memory_home(kinds, namespace):  # never a shared plane by default
            namespace = derive_personal_slug(subject)
    except Exception:  # noqa: BLE001 — default namespace falls back to claim or personal slug
        pass
    return {
        "org": org,
        "namespace": namespace,
        "agent": _client_name_to_agent(client_name),
    }


async def _caller_default_context(auth: Any) -> str:
    """The "Default Loom workspace: ..." block, delivered once via the
    `initialize` result's `instructions` field. Surfaces only `namespace`
    and `agent` — `org` is single-tenant per caller and filled from the JWT
    automatically, so it'd be noise in the LLM's context.
    """
    defaults = await _caller_defaults(auth)
    if not defaults:
        return ""
    return (
        f"\n\n**Default Loom workspace:** "
        f"namespace=`{defaults['namespace']}`, agent=`{defaults['agent']}`. "
        f"The server fills these in automatically when you omit them — "
        f"call this tool with ONLY the action-specific args (e.g. query, "
        f"memory_key, value) and the defaults will be applied. Don't ask "
        f"the user which namespace; the server knows."
    )
