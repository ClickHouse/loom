"""MCP transport — caller identity / provenance resolution.

Leaf module: client-name slugification, the session→agent cache, and the
caller's default workspace (its namespace from `server.identity.app.caller_home`,
which the token mint also asks for its `saves_to`). No imports back into
``server.transport.mcp``; constants come from ``mcp_config``.
"""

from __future__ import annotations

import uuid
from contextvars import ContextVar
from typing import Any

from server.core.naming import slugify
from server.identity.app.caller_home import (
    caller_org,
    default_and_readable,
    home_subject,
)
from server.transport.mcp_config import _DEFAULT_MCP_AGENT, _MCP_SESSION_AGENT_CAP

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


# An unaddressed call whose default is NOT the caller's own home namespace
# says where it landed, so a model does not read a team plane's rows as
# "yours". The common case (home, or an explicit namespace) adds nothing.
_DEFAULT_NOTE: ContextVar[str] = ContextVar("loom_default_namespace_note", default="")


def note_default_namespace(filled: bool, namespace: str, auth: Any) -> None:
    """`auth`'s home is its creator's for a delegated token (`home_subject`);
    an agent's, direct or delegated, is its own namespace (`agents.home_of`)."""
    from server.auth.agents import home_of

    subject = home_subject(auth).strip()
    home = home_of(subject) or subject.lower().split("@", 1)[0]
    away = bool(filled and namespace and namespace.lower() != home)
    _DEFAULT_NOTE.set(namespace if away else "")


def default_namespace_note() -> dict[str, str]:
    ns = _DEFAULT_NOTE.get()
    return {"namespace_defaulted": ns} if ns else {}


async def _caller_defaults(
    auth: Any,
    client_name: str | None = None,
) -> dict[str, Any] | None:
    """Resolve the caller's default (org, namespace, agent), plus `readable`
    (the namespaces the token can name) when it has no default namespace.

    Used both to embed defaults in tool descriptions and to fill missing
    args on incoming tool calls. The namespace is `caller_home.default_namespace`,
    the one the token mint answers as `saves_to`. ``client_name`` (the MCP `clientInfo.name`)
    becomes the agent default when present. None when no caller can be
    identified — falls back to handler-level validation errors.
    """
    org = caller_org(auth)
    if org is None:
        return None
    namespace, readable = await default_and_readable(auth, org)
    defaults: dict[str, Any] = {
        "org": org,
        "namespace": namespace,
        "agent": _client_name_to_agent(client_name),
    }
    if not namespace:
        # What a token with no default names on its calls, and searches.
        defaults["readable"] = readable
    return defaults


def no_default_namespace_line(defaults: dict[str, Any]) -> str:
    """What a token with no default namespace names on its calls."""
    readable = defaults.get("readable") or []
    if not readable:
        return "This token has no default namespace and can read no namespace."
    names = ", ".join(f"`{ns}`" for ns in readable)
    # An unaddressed search is addressed to the first one (`mcp_call._address_first_claim`);
    # its fan-out never sweeps another tracker or code namespace.
    others = " (name another on the call to search it)" if len(readable) > 1 else ""
    return (
        f"This token has no default namespace: name one of {names} on each memory "
        f"or tracker call; `loom_search` searches `{readable[0]}`{others}."
    )


async def _caller_default_context(auth: Any, defaults: dict[str, Any] | None = None) -> str:
    """The "Default Loom workspace: ..." block, delivered once via the
    `initialize` result's `instructions` field. Surfaces only `namespace`
    and `agent` — `org` is single-tenant per caller and filled from the JWT
    automatically, so it'd be noise in the LLM's context. ``defaults`` are
    resolved here when not passed.
    """
    defaults = defaults or await _caller_defaults(auth)
    if not defaults:
        return ""
    if not defaults["namespace"]:
        return (
            f"\n\n**Default Loom workspace:** none. {no_default_namespace_line(defaults)} "
            f"agent=`{defaults['agent']}` is filled in when omitted."
        )
    return (
        f"\n\n**Default Loom workspace:** "
        f"namespace=`{defaults['namespace']}`, agent=`{defaults['agent']}`. "
        f"The server fills these in automatically when you omit them — "
        f"call this tool with ONLY the action-specific args (e.g. query, "
        f"memory_key, value) and the defaults will be applied. Don't ask "
        f"the user which namespace; the server knows."
    )
