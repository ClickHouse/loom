"""`POST /v1/me/setup/mcp_config`: the dashboard's Setup tab mints a scoped
token for one MCP client and returns that client's config entry."""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, HTTPException, Request

from server.auth.delegation import Delegated, delegate
from server.auth.jwt import AuthInfo
from server.core.datetimes import naive_utc_now_seconds
from server.core.errors import BackendUnavailableError
from server.identity.app import me_callers, token_grants
from server.identity.app.caller_home import caller_org, default_namespace
from server.identity.app.me_service_accounts import (
    dev_parent,
    new_token_subject,
    parse_ttl_days,
    save_token,
)
from server.identity.app.restricted_access import note_restricted_grants
from server.settings import settings

router = APIRouter()


def _mcp_client_config(client: str, mcp_url: str, minted: str) -> dict[str, object]:
    """The per-client half of the Setup endpoint: the config-file key, the
    server entry (URL + bearer, the `mcp-remote` bridge for stdio clients,
    `bearer_token_env_var` for Codex), the file's own text
    when it is not JSON, the one-line registration command, and the file's
    path per platform. Pure; the route mints the token and persists it."""
    # Per-client shape. VS Code uses "servers", Zed uses "context_servers",
    # everyone else uses "mcpServers".
    if client == "vscode":
        config_key = "servers"
    elif client == "zed":
        config_key = "context_servers"
    elif client == "codex":
        config_key = "mcp_servers"
    else:
        config_key = "mcpServers"
    # stdio-only clients need the `mcp-remote` npx shim to reach a remote
    # HTTP MCP server; the rest speak remote MCP natively (URL + headers,
    # with `type: "http"` for claude-code / vscode). When a client gains
    # native HTTP support, drop it out of _STDIO_VIA_NPX.
    _STDIO_VIA_NPX = {"claude-desktop", "zed", "cline"}
    auth_header = f"Authorization: Bearer {minted}"
    if client in _STDIO_VIA_NPX:
        entry: dict[str, object] = {
            "command": "npx",
            "args": [
                "-y",
                "mcp-remote",
                mcp_url,
                "--allow-http",
                "--header",
                auth_header,
            ],
        }
    elif client == "codex":
        # Codex names the env var the bearer lives in; the token itself never sits in
        # config.toml. The `coding` toolset rides the URL as it does for the other coding
        # agents.
        codex_url = mcp_url if "toolset=" in mcp_url else f"{mcp_url}?toolset=coding"
        entry = {"url": codex_url, "bearer_token_env_var": "LOOM_API_KEY"}
    else:
        entry = {
            "url": mcp_url,
            "headers": {"Authorization": f"Bearer {minted}"},
        }
        if client in {"claude-code", "vscode"}:
            entry["type"] = "http"
    config_block = {config_key: {"loom": entry}}
    config_toml = (
        f'[mcp_servers.loom]\nurl = "{entry["url"]}"\nbearer_token_env_var = "LOOM_API_KEY"\n'
        if client == "codex"
        else ""
    )
    command = (
        f'codex mcp add loom --url "{entry["url"]}" --bearer-token-env-var LOOM_API_KEY'
        if client == "codex"
        else ""
    )

    config_paths = {
        "claude-desktop": {
            "darwin": "~/Library/Application Support/Claude/claude_desktop_config.json",
            "linux": "~/.config/Claude/claude_desktop_config.json",
            "win32": "%APPDATA%\\Claude\\claude_desktop_config.json",
        },
        "claude-code": {
            "darwin": "~/.claude.json",
            "linux": "~/.claude.json",
            "win32": "%USERPROFILE%\\.claude.json",
        },
        "codex": {
            "darwin": "~/.codex/config.toml",
            "linux": "~/.codex/config.toml",
            "win32": "%USERPROFILE%\\.codex\\config.toml",
        },
        "cursor": {
            "darwin": "~/.cursor/mcp.json",
            "linux": "~/.cursor/mcp.json",
            "win32": "%USERPROFILE%\\.cursor\\mcp.json",
        },
        "vscode": {
            "darwin": "~/.vscode/mcp.json",
            "linux": "~/.vscode/mcp.json",
            "win32": "%USERPROFILE%\\.vscode\\mcp.json",
        },
        "cline": {
            "darwin": "~/Library/Application Support/Code/User/globalStorage/saoudrizwan.claude-dev/settings/cline_mcp_settings.json",  # noqa: E501
            "linux": "~/.config/Code/User/globalStorage/saoudrizwan.claude-dev/settings/cline_mcp_settings.json",  # noqa: E501
            "win32": "%APPDATA%\\Code\\User\\globalStorage\\saoudrizwan.claude-dev\\settings\\cline_mcp_settings.json",  # noqa: E501
        },
        "windsurf": {
            "darwin": "~/.codeium/windsurf/mcp_config.json",
            "linux": "~/.codeium/windsurf/mcp_config.json",
            "win32": "%USERPROFILE%\\.codeium\\windsurf\\mcp_config.json",
        },
        "zed": {
            "darwin": "~/.config/zed/mcp.json",
            "linux": "~/.config/zed/mcp.json",
            "win32": "%APPDATA%\\Zed\\mcp.json",
        },
    }
    return {
        "config_key": config_key,
        "config": config_block,
        "config_toml": config_toml,
        "command": command,
        "config_path": config_paths[client],
    }


# The MCP clients the Setup tab can mint a token for, with their display
# labels. One table for the validation set, the service-account name and
# the dashboard's client picker (frontend/src/lib/clients.ts mirrors it).
MCP_SETUP_CLIENT_LABELS: dict[str, str] = {
    "claude-desktop": "Claude Desktop",
    "claude-code": "Claude Code",
    "codex": "Codex",
    "cursor": "Cursor",
    "vscode": "VS Code",
    "cline": "Cline",
    "windsurf": "Windsurf",
    "zed": "Zed",
}


async def _validate_restricted_request(org: str, subject: str, raw: object) -> list[str]:
    """Parse `restricted_namespaces` on the Setup mint: each must be classified restricted
    (422 NOT_RESTRICTED otherwise — a standard namespace needs no naming). The grant
    belongs to the token's own subject and dies with its revocation. `delegate` then
    refuses (403) one the caller holds no grant on."""
    from server.identity.app.namespace_classification import is_restricted

    if not raw:
        return []
    if not isinstance(raw, list) or not all(isinstance(x, str) and x.strip() for x in raw):
        raise HTTPException(
            status_code=422,
            detail={
                "code": "VALIDATION_FAILED",
                "message": "restricted_namespaces must be an array of namespace names",
            },
        )
    names: list[str] = []
    for ns in raw:
        ns = ns.strip()
        if ns in names:
            continue
        if not await is_restricted(org, ns):
            raise HTTPException(
                status_code=422,
                detail={
                    "code": "NOT_RESTRICTED",
                    "message": (
                        f"{ns} is not a restricted namespace; a standard "
                        "namespace is read through your membership"
                    ),
                },
            )
        names.append(ns)
    return await _with_bound_tracker(org, subject, names)


async def _with_bound_tracker(org: str, subject: str, names: list[str]) -> list[str]:
    """A restricted repository added to a token brings its bound tracker record when that
    record is itself restricted and the caller holds a direct grant on it. The
    history-shaped code ops (blame, bisect, backport gaps, first build, symbol history) read
    issues and PRs from that record through the tracker namespace's own two-factor gate, so
    a token naming the repository but not the record would answer without them, silently.
    A standard record needs nothing: it is read by whoever reads the repository. Order is
    kept; nothing is removed."""
    from server.identity.app.namespace_classification import is_restricted
    from server.tracker.app.binding import (
        operator_repositories,
        refresh_overrides,
        tracker_namespaces_for,
    )

    out = list(names)
    await refresh_overrides()  # the links as of the refresh interval, branches included
    operator = operator_repositories()  # once for every name
    for ns in names:
        for qa in tracker_namespaces_for(org, ns, operator=operator):  # every linked tracker
            if qa in out:
                continue
            if not await is_restricted(org, qa):
                continue
            if await me_callers.caller_role_on(org, qa, subject) is None:
                continue
            out.append(qa)
    return out


def _mcp_token_name(client_label: str, now: datetime, sa_hash: str) -> str:
    """The Tokens-list name of a Setup-page token. `loom.service_accounts` is
    a ReplacingMergeTree keyed on (org, name): two mints whose names collide
    leave ONE row, and the earlier token — whose subject that row no longer
    names — dies at its first request. The client plus the minute alone would collide for
    two mints in one minute; seconds plus the subject hash make it unique per mint and still
    readable."""
    return f"{client_label} · {now.strftime('%Y-%m-%d %H:%M:%S')} · {sa_hash}"


async def _save_mcp_token(
    *,
    subject: str,
    client: str,
    delegated: Delegated,
    restricted_names: list[str],
) -> tuple[str, str, datetime | None, list[str]]:
    """Track a Setup-page token in `service_accounts` (kind='mcp') so it is revocable from
    the Tokens list, minted under its own `sa:` subject so revocation-by-sub kills this
    token and not the person's JWT. Restricted namespaces get the token's own direct grant.
    Returns (minted token, sa_subject, the saved row's expires_at, the restricted
    namespaces granted)."""
    org = delegated.org
    sa_subject = new_token_subject(org, client, subject)
    sa_hash = sa_subject.removeprefix(f"sa:{client}-")
    minted = delegated.mint(sa_subject)
    now = naive_utc_now_seconds()  # after the mint: the row is never older than the token's iat
    client_label = MCP_SETUP_CLIENT_LABELS.get(client, client)
    token_name = _mcp_token_name(client_label, now, sa_hash)
    await save_token(
        token_name, sa_subject, delegated, created_by=subject, now=now, kind="mcp", client=client
    )
    granted: list[str] = []
    if restricted_names:
        caller_roles = {
            ns: (await me_callers.caller_role_on(org, ns, subject) or "viewer")
            for ns in restricted_names
        }
        granted = await token_grants.grant_restricted_to_token(
            org, sa_subject, subject, caller_roles
        )

    return minted, sa_subject, delegated.expires_at(now), granted


def _setup_parent(request: Request, payload: dict[str, object]) -> AuthInfo:
    """The token the setup token is delegated from: the caller's bearer, or in dev
    mode (auth off) `dev_parent`."""
    from server.auth.bearer import missing_bearer, verify_bearer_header

    if not settings.auth_actually_required():
        return dev_parent(request, payload)
    info = verify_bearer_header(request.headers.get("Authorization"))
    if info is None:
        raise missing_bearer()
    return info


@router.post("/v1/me/setup/mcp_config")
async def me_setup_mcp_config(request: Request, payload: dict[str, object]) -> dict[str, object]:
    """Generate an MCP-client config block + fresh token for the caller.

    Powers the dashboard's Settings page: mints a Loom JWT delegated from the caller's
    (`server.auth.delegation`) and returns the config JSON to paste into the MCP client.

    Body:
      {
        client: str,             # a key of MCP_SETUP_CLIENT_LABELS (claude-code, codex, cursor, …)
        namespace?: str,         # defaults to caller's namespace
        ttl_days?: int | null,   # null = 90 days, 0 = never
      }

    The token is saved to the caller's token list, where it is revoked.

    Returns:
      {
        client: str,
        config_path: { darwin: str, linux: str, win32: str },
        config_key: str,         # "mcpServers" (most clients) or "servers" (VS Code)
        config: dict,            # the JSON block to merge into the client's config file
        token: str,              # the minted JWT
        sa_subject: str,         # its subject in the token list
        expires_at: str | null,  # ISO-8601; null: never expires
        namespace: str,          # where unaddressed calls land; "": none, calls must name one
        …                        # and the namespace, scope and URL it reads
      }
    """
    parent = _setup_parent(request, payload)
    # auth off: `parent` is the dev stand-in, no caller, so no audit row
    caller = parent if settings.auth_actually_required() else None
    subject, org = parent.subject, parent.org

    client = str(payload.get("client") or "")
    allowed_clients = set(MCP_SETUP_CLIENT_LABELS)
    if client not in allowed_clients:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "VALIDATION_FAILED",
                "message": f"client must be one of: {sorted(allowed_clients)}",
            },
        )

    # Default: `*`, which reads wherever the caller holds a grant, now and later (or
    # the caller's own namespaces, under a token narrower than that). An explicit
    # `namespace` scopes it down. None = the highest role the caller may delegate.
    namespace_payload = payload.get("namespace")
    if namespace_payload:
        requested: dict[str, str | None] = {str(namespace_payload): None}
    else:
        requested = {"*": None}
    # Restricted namespaces the token should read are named in the claim and, once the token
    # has a subject, granted to it directly.
    restricted_names = await _validate_restricted_request(
        org, subject, payload.get("restricted_namespaces")
    )
    for ns in restricted_names:
        requested.setdefault(ns, None)

    delegated = await delegate(
        parent,
        org=org,
        requested=requested,
        scoped=False,
        ttl_seconds=parse_ttl_days(payload.get("ttl_days")),
        is_service_account=True,
    )
    minted, sa_subject, expires_at, granted = await _save_mcp_token(
        subject=subject,
        client=client,
        delegated=delegated,
        restricted_names=restricted_names,
    )
    await note_restricted_grants(
        caller,
        op="me.setup.mcp_config",
        org=org,
        namespaces=granted,
    )
    reads_everywhere = "*" in delegated.namespaces
    # The page names where this token's unaddressed MCP calls land: the MCP
    # transport's own resolver, asked about the token just minted.
    token = delegated.as_auth(sa_subject)
    try:
        displayed_namespace: str | None = await default_namespace(token, caller_org(token) or org)
    except BackendUnavailableError:
        # The token is already minted and listed: hand it over without a name
        # rather than fail, or a retry would mint a second one. None (not "",
        # which means it has no default): each call resolves it.
        displayed_namespace = None

    # Base URL: LOOM_PUBLIC_BASE_URL (required off loopback), else the request's own origin.
    from server.auth.public_url import public_base_url

    base_url = public_base_url(request)
    mcp_url = f"{base_url}/mcp"

    shape = _mcp_client_config(client, mcp_url, minted)

    return {
        "client": client,
        # Codex's file is TOML, not JSON; the dashboard renders `config_toml` (and the
        # one-line `command`) instead of the JSON block.
        **shape,
        "token": minted,
        "subject": subject,
        "org": org,
        "namespace": displayed_namespace,
        "namespace_scope": "all" if reads_everywhere else "single",
        # Every read is rechecked against the creator's live grants. Under `*` that is the
        # whole read set (every namespace the person belongs to, now and later); otherwise
        # the token reads only the namespaces it names.
        "read_scope": "inherited_grants" if reads_everywhere else "named_namespaces",
        # Restricted namespaces this token reads by name, each with a direct grant of its own
        # that dies with the token.
        "restricted_namespaces": restricted_names,
        # The namespace claim as minted: `*`, or the namespaces the token reads (a `*`
        # request under a narrower sign-in becomes that sign-in's list).
        "namespaces": list(delegated.namespaces),
        "mcp_url": mcp_url,
        "sa_subject": sa_subject,
        "expires_at": expires_at.isoformat() if expires_at else None,
        # `persist` is ignored: every Setup token is saved and listed.
        **({"ignored_args": ["persist"]} if "persist" in payload else {}),
    }
