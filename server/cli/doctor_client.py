"""`loom doctor --client` — the doctor examines the machine a coding agent
runs on.

`loom doctor` reads the server's settings and says what is wrong with a
deployment. This says what is wrong on the *other* side: the laptop or CI
box where Claude Code or Codex runs the Loom plugin. There, every failure is
silent by design — the hooks fail open, a 401 looks like "no memories", a
missing writer role looks like a quiet session. These checks run from the
client's point of view, with the client's own resolution order for the
server URL and the token, and each line says what to do.

Pure functions take their I/O as arguments (`get_json`, `post_json`, `home`,
`tmp`, `now`) so the unit tests run them offline. `node_checks` is the
exception: it runs the installed plugin's own launcher, so that one
implementation decides which Node the hooks get.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
import tomllib
from collections.abc import Callable
from pathlib import Path
from typing import Any

from server.cli._client import (
    AGENT_SOURCE,
    CONFIG_SOURCE,
    agent_mode,
    jwt_claims,
    loom_config,
    resolve_server_url,
    resolve_token,
)

PASS, WARN, FAIL = "PASS", "WARN", "FAIL"
Check = tuple[str, str, str]
GetJson = Callable[[str, str, float], tuple[int, dict[str, Any]]]
PostJson = Callable[[str, str, dict[str, Any], str, float], tuple[int, dict[str, Any]]]

STALE_FLUSH_S = 600
EXPIRING_SOON_S = 7 * 86400
_FLUSH_RE = re.compile(r"^loom-flush-(\d{13})-\d+\.json$")


# ---- what the hooks would resolve ------------------------------------------------


def intended_namespace(home: Path, env: dict[str, str], claims: dict[str, Any]) -> str:
    """The namespace the hooks will write to: LOOM_NAMESPACE, the config
    file (never for an agent), the token's first non-wildcard claim, else
    `coding` (resolveScope)."""
    if env.get("LOOM_NAMESPACE", "").strip():
        return env["LOOM_NAMESPACE"].strip()
    cfg = "" if agent_mode(env) else str(loom_config(home, env).get("namespace") or "").strip()
    if cfg:
        return cfg
    ns = claims.get("namespaces") or []
    if isinstance(ns, list):
        for n in ns:
            if n and n != "*":
                return str(n)
    return "coding"


# ---- the checks --------------------------------------------------------------------


def probe_body(claims: dict[str, Any]) -> dict[str, Any]:
    """The body the auth probe sends. A token whose authority is per-namespace
    (`ns_roles`, or a namespace list without the wildcard) is refused with 400
    unless the request names a namespace — the server cannot apply a scoped
    authority to an unscoped call — so the
    probe names the token's first concrete namespace when it has one."""
    body: dict[str, Any] = {"org": str(claims.get("org") or "dev")}
    ns = claims.get("namespaces") or []
    first = next((str(n) for n in ns if n and n != "*"), "") if isinstance(ns, list) else ""
    if first:
        body["namespace"] = first
    return body


def server_check(base_url: str, source: str, get_json: GetJson) -> tuple[Check, dict[str, Any]]:
    status, body = get_json(f"{base_url}/v1/health", "", 5.0)
    name = "client:server"
    if status == 0:
        detail = body.get("detail") if isinstance(body, dict) else ""
        return (
            (
                name,
                FAIL,
                f"cannot reach {base_url} (from {source}): {detail}. Fix: start it (`loom up`) or "
                "point the client at it (LOOM_BASE_URL, or `url` in ~/.loom/config.json).",
            ),
            {},
        )
    from server.observability.problems import NOT_DEPLOYMENT_FAULTS

    problems = body.get("problems") if isinstance(body, dict) else None
    # an error about the data the server holds (a repository's failing tier) is not one
    # of the server's: it never degrades `status`
    errors = [
        p.get("code")
        for p in problems or []
        if isinstance(p, dict)
        and p.get("severity") == "error"
        and p.get("code") not in NOT_DEPLOYMENT_FAULTS
    ]
    build = str(body.get("build_commit") or "?")[:9] if isinstance(body, dict) else "?"
    health = (
        str(body.get("status") or ("ok" if status == 200 else status))
        if isinstance(body, dict)
        else str(status)
    )
    if status >= 500 or errors:
        return (
            (
                name,
                WARN,
                f"{base_url} answers (build {build}) but reports {health}: "
                f"{', '.join(map(str, errors)) or status}. "
                "Fix: `loom doctor` on the server, or the dashboard's Health page.",
            ),
            body if isinstance(body, dict) else {},
        )
    return (name, PASS, f"{base_url} (from {source}) — build {build}, status {health}"), body


def _agent_token_fix(base_url: str) -> str:
    # An agent's token came from --token or LOOM_API_KEY, and `loom init` refuses in agent mode.
    return (
        "drop --token and LOOM_API_KEY; the hooks and "
        f"`loom call --server {base_url} namespace.list_mine` exchange the workload token"
    )


def token_check(
    token: str, source: str, claims: dict[str, Any], now: float, base_url: str, *, agent: bool
) -> Check:
    name = "client:token"
    if not token and source == AGENT_SOURCE:
        return (
            name,
            WARN,
            "agent mode (LOOM_CLIENT_ID with a workload token): these checks act for no one. "
            "The hooks exchange the workload token themselves; "
            f"`loom call --server {base_url} namespace.list_mine` runs that exchange against "
            "this server and lists what the agent reaches, or says why it was refused.",
        )
    if not token:
        return (
            name,
            WARN,
            "no token: LOOM_API_KEY unset, no ~/.loom/config.json `token`, no ~/.loom/token. "
            "Fine for a single-user stack; otherwise `loom mint-token --save`, "
            "`loom init --agent NAME`, or mint one in the dashboard (Settings → Tokens).",
        )
    if not claims:
        return (
            name,
            FAIL,
            f"the token from {source} is not a JWT (three dot-separated parts). "
            f"Fix: {_agent_token_fix(base_url) if agent else 're-copy it'}.",
        )
    exp = claims.get("exp")
    if isinstance(exp, (int, float)):
        if exp <= now:
            return (
                name,
                FAIL,
                f"the token from {source} expired {int((now - exp) // 3600)} h ago. "
                f"Fix: {_agent_token_fix(base_url) if agent else 'mint a new one'}.",
            )
        if exp - now < EXPIRING_SOON_S:
            return (
                name,
                WARN,
                f"the token from {source} expires in {int((exp - now) // 3600)} h. "
                f"Fix: {_agent_token_fix(base_url) if agent else 'mint a new one soon'}.",
            )
    who = f"{claims.get('sub', '?')} @ {claims.get('org', '?')}"
    kind = "service account" if claims.get("sa") else "user"
    return (
        name,
        PASS,
        f"{kind} {who} from {source}; namespaces {claims.get('namespaces') or ['*']}",
    )


def token_shadow_check(token: str, source: str, home: Path, env: dict[str, str]) -> list[Check]:
    """A second token that differs from the one the doctor
    resolved. `LOOM_API_KEY` in the environment wins over `~/.loom/config.json`
    for the hooks and for the plugin's MCP headers helper alike, so a stale
    export (a shell profile, or `env` in ~/.claude/settings.json, which Claude
    Code applies to its hooks and MCP servers) keeps a dead token in play
    after the config file was refreshed — and the doctor, run from a shell
    without that export, would report the fresh one and pass."""
    if agent_mode(env):
        return []  # an agent uses none of the tokens saved here
    others: list[tuple[str, str]] = []
    cfg = str(loom_config(home, env).get("token") or "").strip()
    if cfg and source != CONFIG_SOURCE:
        others.append(("~/.loom/config.json `token`", cfg))
    env_tok = env.get("LOOM_API_KEY", "").strip()
    if env_tok and source != "LOOM_API_KEY":
        others.append(("LOOM_API_KEY", env_tok))
    try:
        settings_env = json.loads(_read(home / ".claude" / "settings.json") or "{}").get("env")
        claude_tok = str((settings_env or {}).get("LOOM_API_KEY") or "").strip()
    except (ValueError, AttributeError):
        claude_tok = ""
    if claude_tok:
        others.append(("~/.claude/settings.json env.LOOM_API_KEY", claude_tok))
    differing = [where for where, t in others if t != token]
    if not differing:
        return []
    return [
        (
            "client:token-shadow",
            WARN,
            f"{', '.join(differing)} hold(s) a different token from the one {source} gave "
            "this run. LOOM_API_KEY beats ~/.loom/config.json for the hooks and the plugin's "
            "MCP headers, and Claude Code applies settings.json `env` to both — so a "
            "process started with the stale one is rejected while this run passes. Fix: "
            "remove the stale copy (`grep -l LOOM_API_KEY ~/.zshenv ~/.zshrc ~/.bashrc "
            "~/.claude/settings.json`), or paste the current token there too.",
        )
    ]


def auth_check(
    base_url: str, token: str, claims: dict[str, Any], post_json: PostJson, *, agent: bool
) -> tuple[Check, list[dict[str, Any]]]:
    name = "client:auth"
    if not token:
        return (name, WARN, "skipped: no token to present"), []
    org = str(claims.get("org") or "dev")
    status, body = post_json(base_url, "namespace.list_mine", probe_body(claims), token, 10.0)
    if status == 0:
        return (name, WARN, "skipped: the server was not reachable"), []
    if status == 401:
        msg = _detail(body)
        if agent:
            fix = _agent_token_fix(base_url)
            why = (
                "the hooks send LOOM_API_KEY, when set, before the workload token, "
                "so a rejected one makes captures vanish"
            )
        else:
            fix = (
                "mint a new token (`loom mint-token --save`, Settings → Tokens in the "
                "dashboard, or `loom init --agent`)"
            )
            why = "the hooks fail open, so this is why captures vanish"
        return (
            (
                name,
                FAIL,
                f"the server rejected the token: {msg}. Fix: {fix} — {why}.",
            ),
            [],
        )
    if status != 200:
        return (name, WARN, f"namespace.list_mine answered HTTP {status}: {_detail(body)}"), []
    rows = body.get("rows") if isinstance(body, dict) else None
    rows = [r for r in rows or [] if isinstance(r, dict)]
    roles = sorted({str(r.get("role")) for r in rows})
    return (
        name,
        PASS,
        f"token accepted; {len(rows)} namespace(s) in {org}, roles {roles or ['-']}",
    ), rows


def reads_check(
    base_url: str,
    token: str,
    claims: dict[str, Any],
    rows: list[dict[str, Any]],
    post_json: PostJson,
    auth_ok: bool,
) -> Check:
    """What the token reads, in the words the Setup page uses: every namespace the token's
    creator belongs to, the repositories among them grouped by repository, and the
    restricted repositories it still cannot read — a client token runs under its own `sa:`
    subject with no direct grant, so the restricted-namespace gate refuses it there.
    `code.repos` is asked only when the membership list holds a repository row."""
    name = "client:reads"
    if not token or not auth_ok:
        return (name, WARN, "skipped: check auth first")
    memory = sorted(str(r.get("namespace")) for r in rows if r.get("kind") not in {"code"})
    code_rows = [r for r in rows if r.get("kind") == "code"]
    parts = [f"{len(memory)} namespace(s): {', '.join(memory) or '-'}"]
    if code_rows:
        org = str(claims.get("org") or "dev")
        status, body = post_json(
            base_url, "code.repos", {"org": org, "consumer": "loom-doctor"}, token, 10.0
        )
        repos = body.get("repos") if status == 200 and isinstance(body, dict) else None
        facts = {str(r.get("namespace")): r for r in repos or [] if isinstance(r, dict)}
        # A restricted repository the token NAMES in its own
        # claim carries a direct grant of its own and is
        # readable; only an unnamed restricted repository is excluded.
        named = {str(n) for n in (claims.get("namespaces") or [])}
        readable: dict[str, int] = {}
        named_restricted: dict[str, int] = {}
        excluded: dict[str, int] = {}
        for r in code_rows:
            ns = str(r.get("namespace"))
            f = facts.get(ns) or {}
            repo = str(f.get("repo") or ns)
            if f.get("classification") == "restricted":
                bucket = named_restricted if ns in named else excluded
            else:
                bucket = readable
            bucket[repo] = bucket.get(repo, 0) + 1

        def _fmt(d: dict[str, int]) -> str:
            return ", ".join(f"{k} ({v} branches)" if v > 1 else k for k, v in sorted(d.items()))

        if readable:
            parts.append(f"repositories: {_fmt(readable)}")
        if named_restricted:
            parts.append(f"restricted, added to this token: {_fmt(named_restricted)}")
        if excluded:
            parts.append(
                f"not included: {_fmt(excluded)} — restricted; a client token's own "
                "subject holds no direct grant there. Fix: add the "
                "repository when minting in the dashboard (Settings → Tokens), or "
                "`loom mint-token --subject <you> --namespaces <ns>` for your own subject."
            )
    return (name, PASS, "reads " + " · ".join(parts))


def namespace_check(namespace: str, rows: list[dict[str, Any]], token: str, auth_ok: bool) -> Check:
    name = "client:namespace"
    if not token or not auth_ok:
        return (name, WARN, f"skipped: hooks would write to {namespace!r}; check auth first")
    mine = {str(r.get("namespace")): str(r.get("role")) for r in rows}
    role = mine.get(namespace)
    if role in {"writer", "admin", "jwt", "org_admin", "platform_admin"}:
        return (name, PASS, f"hooks write to {namespace!r} as {role}")
    if role == "viewer":
        return (
            name,
            FAIL,
            f"hooks write to {namespace!r} but the token is a viewer there; "
            "observe.record needs writer. "
            "Fix: mint a writer token, or set LOOM_NAMESPACE to one you can write.",
        )
    return (
        name,
        FAIL,
        f"hooks write to {namespace!r} but the token has no grant there "
        f"(it has {sorted(mine) or 'none'}). Fix: set LOOM_NAMESPACE / `namespace` in "
        "~/.loom/config.json to one of those, or join it in the dashboard.",
    )


def recording_check(
    base_url: str,
    token: str,
    claims: dict[str, Any],
    namespace: str,
    post_json: PostJson,
    auth_ok: bool,
) -> Check:
    """Whether the namespace the hooks write to keeps the
    conversation turns they send. The owner's Recording switch in the dashboard
    (`conversation_logging_enabled`) decides; with it off every append is
    accepted and nothing is kept, which the hooks alone would never notice."""
    name = "client:recording"
    if not token or not auth_ok:
        return (name, WARN, "skipped: check auth first")
    org = str(claims.get("org") or "dev")
    status, body = post_json(
        base_url, "namespace.get", {"org": org, "namespace": namespace}, token, 10.0
    )
    if status != 200 or not isinstance(body, dict) or not body.get("found"):
        return (
            name,
            WARN,
            f"could not read the Recording switch for {namespace!r} (HTTP {status})",
        )
    if body.get("conversation_logging_enabled") is False:
        return (
            name,
            WARN,
            f"conversation recording is off for {namespace!r} (the owner's Recording switch "
            "in the dashboard): the hooks send no turns and none are kept. Turn it on there if "
            "you want transcripts.",
        )
    return (name, PASS, f"conversation recording is on for {namespace!r}")


def claude_code_checks(
    home: Path,
    base_url: str,
    *,
    post_json: PostJson | None = None,
    now: float | None = None,
) -> list[Check]:
    if not (home / ".claude").is_dir():
        return []
    out: list[Check] = []
    servers: dict[str, Any] = {}
    try:
        servers = json.loads((home / ".claude.json").read_text()).get("mcpServers") or {}
    except (OSError, ValueError, AttributeError):
        servers = {}
    hit = [
        n
        for n, s in servers.items()
        if isinstance(s, dict) and _same_server(str(s.get("url") or ""), base_url)
    ]
    plugin = claude_plugin_installed(home)
    if hit:
        out.append(
            (
                "client:claude-code:mcp",
                PASS,
                f"user-scope MCP server {hit[0]!r} points at {base_url}",
            )
        )
        # The token Claude Code itself sends: a static
        # `Authorization: Bearer …` in the entry. A rejected one is the
        # "failed to connect" the hooks' token never explains.
        entry = servers[hit[0]]
        out.append(
            registration_token_check(
                "client:claude-code:mcp-token",
                _bearer_from_headers(entry.get("headers")),
                f"~/.claude.json mcpServers.{hit[0]}",
                base_url,
                post_json,
                now,
                absent_note=(
                    "no static bearer in the entry (OAuth or a headersHelper supplies it)"
                    if entry.get("headersHelper") or not entry.get("headers")
                    else "no Authorization header in the entry"
                ),
                fix="`claude mcp remove <name>` then "
                "`loom install --client claude-code --token <new>`, "
                "or Settings → Claude Code in the dashboard",
            )
        )
        if plugin:
            out.append(duplicate_registration_check(hit[0], servers[hit[0]]))
    elif plugin:
        # The plugin mounts the server from its own `.mcp.json`
        # (`${user_config.server_url}/mcp`, token via the headers helper, same
        # resolution order as `client:token`). A user-scope entry on top would
        # register the same server twice, so this is the wired state, not a gap.
        out.append(
            (
                "client:claude-code:mcp",
                PASS,
                "mounted by the loom plugin (its .mcp.json; the token is the one "
                "`client:token` resolved). No user-scope entry in ~/.claude.json — "
                "do not add one with `loom install --client claude-code`, it would "
                "register the server twice.",
            )
        )
    else:
        out.append(
            (
                "client:claude-code:mcp",
                WARN,
                f"no user-scope MCP server in ~/.claude.json points at {base_url} "
                "and the plugin is not installed. Fix: `claude plugin marketplace add "
                "ClickHouse/loom && claude plugin install loom@loom`, or "
                "`loom install --client claude-code`.",
            )
        )
    out.append(
        ("client:claude-code:plugin", PASS, "the loom plugin (hooks + skills) is present")
        if plugin
        else (
            "client:claude-code:plugin",
            WARN,
            "the loom plugin is not installed: no hooks, no primer, no capture. Fix: "
            "`claude plugin marketplace add ClickHouse/loom && claude plugin install loom@loom`.",
        )
    )
    return out


def claude_plugin_installed(home: Path) -> bool:
    """Whether Claude Code has the loom plugin (hooks, skills and its own MCP
    mount from `.mcp.json`) — the same three signals the doctor always read."""
    return (
        (home / ".claude" / "plugins" / "data" / "loom").is_dir()
        or _mentions(
            home / ".claude" / "plugins" / "known_marketplaces.json", "ClickHouse/loom", "loom"
        )
        or _mentions(home / ".claude" / "settings.json", "loom@loom")
    )


# Tools/list size per roster (tool count, chars/4 tokens), as measured against the
# trimmed schemas. The plugin mounts `coding`.
_ROSTER_LISTING = {"core": (41, 13265), "coding": (20, 8498), "all": (109, 31943)}


def duplicate_registration_check(name: str, entry: dict[str, Any]) -> Check:
    """The plugin mounts the server AND a user-scope entry
    points at it. Every session lists both rosters; the model calls whichever
    copy it finds first, and the user-scope copy has no headers helper, so
    it carries neither the checkout's code scope nor the token the hooks
    resolve. This check reports the pair on a machine where it happened."""
    from urllib.parse import parse_qs, urlsplit

    url = str(entry.get("url") or "")
    toolset = (parse_qs(urlsplit(url).query).get("toolset") or ["core"])[0]
    tools, tokens = _ROSTER_LISTING.get(toolset, (0, 0))
    size = f"{tools} more tools, ≈{tokens:,} tokens per turn" if tools else "a second roster"
    return (
        "client:claude-code:mcp-duplicate",
        WARN,
        f"Loom is registered twice: the plugin's own mount and the user-scope entry "
        f"{name!r} (toolset={toolset}: {size}). The model calls either copy; the "
        f"user-scope one carries no code scope for this checkout and its own static "
        f"bearer. Fix: `claude mcp remove {name} -s user` (the plugin keeps the tools).",
    )


def _checkout_slug(cwd: Path) -> str:
    """owner/name of the checkout's origin remote, "" outside a git repo."""
    try:
        url = subprocess.run(
            ["git", "-C", str(cwd), "config", "--get", "remote.origin.url"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return ""
    m = re.search(r"[:/]([^/:]+)/([^/]+?)(?:\.git)?/?$", url)
    return f"{m.group(1)}/{m.group(2)}" if m else ""


def code_access_check(
    slug: str,
    base_url: str,
    credentials: list[tuple[str, str]],
    post_json: PostJson | None,
) -> list[Check]:
    """Can each credential this machine's MCP connections use
    read the checkout's code index? code.repos (named repo)
    omits an entry the caller cannot read, so "no row" for every credential
    is the state in which every loom_code_* call refuses and the model retries
    them on each connection."""

    if not slug or post_json is None or not credentials:
        return []
    seen: dict[str, list[str]] = {}
    for label, token in credentials:
        if not token:
            continue
        claims = jwt_claims(token)
        status, body = post_json(
            base_url, "code.repos", {"org": claims.get("org") or "", "repo": slug}, token, 10.0
        )
        if status != 200:
            continue
        rows = [r for r in (body.get("repos") or []) if r.get("readable", True)]
        who = f"{label} ({claims.get('sub', '?')})"
        seen[who] = sorted({str(r.get("namespace")) for r in rows})
    if not seen:
        return []
    readers = [k for k, v in seen.items() if v]
    blind = [k for k, v in seen.items() if not v]
    if not blind:
        ns = sorted({n for v in seen.values() for n in v})
        return [
            ("client:code-access", PASS, f"{slug}: readable as {', '.join(ns)} by every credential")
        ]
    fix = (
        "claim this machine's self-signup namespace in the dashboard (the code is in "
        "~/.loom/claim-*.txt) so its token acts as you, or save a token for your own "
        "user (`loom mint-token --save`), or request access from the dashboard's Spaces page"
    )
    if readers:
        return [
            (
                "client:code-access",
                WARN,
                f"{slug}: readable by {', '.join(readers)} but NOT by {', '.join(blind)} — "
                f"code calls on that connection refuse. Fix: {fix}.",
            )
        ]
    return [
        (
            "client:code-access",
            WARN,
            f"{slug}: no credential on this machine reads an index for it "
            f"({', '.join(blind)}). If the server indexes it (restricted repositories are "
            f"hidden from callers without a grant), every loom_code_* call refuses. Fix: {fix}.",
        )
    ]


def _codex_plugin_cache(home: Path) -> Path:
    """The loom marketplace's copies of the Codex plugin (`loom@loom`), the ones the skills
    and the header helper read; another marketplace's copy is not this plugin."""
    return home / ".codex" / "plugins" / "cache" / "loom" / "loom"


def codex_checks(
    home: Path,
    base_url: str,
    *,
    env: dict[str, str] | None = None,
    post_json: PostJson | None = None,
    now: float | None = None,
) -> list[Check]:
    cfg = home / ".codex" / "config.toml"
    if not (home / ".codex").is_dir():
        return []
    out: list[Check] = []
    text = _read(cfg)
    env = dict(os.environ if env is None else env)
    plugin_url = codex_plugin_mcp_url(home, text)
    if re.search(r"^\[mcp_servers\.[^\]]+\]", text, re.M) and _same_server(
        _toml_url(text), base_url
    ):
        out.append(
            (
                "client:codex:mcp",
                PASS,
                f"[mcp_servers] in ~/.codex/config.toml points at {base_url}",
            )
        )
        # Codex reads the bearer from the variable the entry NAMES, in the
        # shell Codex starts from.
        var = _toml_str(text, "bearer_token_env_var")
        if var:
            value = env.get(var, "").strip()
            if not value:
                out.append(
                    (
                        "client:codex:mcp-token",
                        FAIL,
                        f"~/.codex/config.toml reads the bearer from ${var}, which is not set in "
                        f"this shell — a Codex started here connects without a token. Fix: "
                        f"`export {var}=…` in the shell profile (`loom install --client codex` "
                        "prints the line).",
                    )
                )
            else:
                out.append(
                    registration_token_check(
                        "client:codex:mcp-token",
                        value,
                        f"${var} (named by ~/.codex/config.toml)",
                        base_url,
                        post_json,
                        now,
                        absent_note="",
                        fix=f"re-export {var} with a fresh token "
                        "(`loom mint-token --save` prints one)",
                    )
                )
    elif plugin_url and _same_server(plugin_url, base_url):
        out.append(
            (
                "client:codex:mcp",
                PASS,
                "mounted by the enabled loom plugin (its .mcp.json; the bearer is "
                "resolved by its header helper from `client:token`).",
            )
        )
    else:
        out.append(
            (
                "client:codex:mcp",
                WARN,
                f"neither an enabled loom plugin MCP entry nor a [mcp_servers.*] entry "
                f"points at {base_url}. Fix: enable `loom@loom`, or configure "
                "[mcp_servers.loom] for a different server.",
            )
        )
    plugin = codex_plugin_enabled(text) and any(
        p.is_dir() for p in _codex_plugin_cache(home).glob("*")
    )
    out.append(
        ("client:codex:plugin", PASS, "the loom plugin (hooks + skills) is present")
        if plugin
        else (
            "client:codex:plugin",
            WARN,
            "the loom plugin is not installed or enabled: no hooks, no primer, no capture. "
            "Fix: `codex plugin marketplace add ClickHouse/loom && codex plugin add loom@loom`.",
        )
    )
    return out


def codex_plugin_enabled(config_text: str) -> bool:
    try:
        config = tomllib.loads(config_text)
        plugins = config.get("plugins", {})
        entry = plugins.get("loom@loom", {}) if isinstance(plugins, dict) else {}
        return isinstance(entry, dict) and entry.get("enabled") is True
    except tomllib.TOMLDecodeError:
        return False


def codex_plugin_mcp_url(home: Path, config_text: str) -> str:
    """Codex mounts a plugin's .mcp.json only while that plugin is enabled."""
    if not codex_plugin_enabled(config_text):
        return ""
    config = tomllib.loads(config_text)
    # An explicit entry replaces the bundled entry, including when it points
    # at another server. The caller checks that entry separately.
    if "loom" in config.get("mcp_servers", {}):
        return ""
    cache = _codex_plugin_cache(home)
    versions = sorted(
        cache.glob("*/.mcp.json"),
        key=lambda p: _plugin_version(p.parent.name),
        reverse=True,
    )
    for path in versions:
        try:
            entry = json.loads(path.read_text())["mcpServers"]["loom"]
            url = entry.get("url", "")
            if isinstance(url, str):
                return url
        except (OSError, ValueError, KeyError, TypeError):
            continue
    return ""


def registration_token_check(
    name: str,
    token: str,
    source: str,
    base_url: str,
    post_json: PostJson | None,
    now: float | None,
    *,
    absent_note: str,
    fix: str,
) -> Check:
    """One line for the token a client's MCP registration carries: absent
    (a note, never a failure — OAuth and header helpers exist), expired or
    not a JWT (FAIL), rejected by the server (FAIL, with the fix), accepted
    (PASS naming the subject). No `post_json` = shape checks only."""

    if not token:
        return (name, PASS, absent_note or "no static bearer")
    claims = jwt_claims(token)
    if not claims:
        return (name, FAIL, f"the bearer in {source} is not a JWT. Fix: {fix}.")
    exp = claims.get("exp")
    t = time.time() if now is None else now
    if isinstance(exp, (int, float)) and exp <= t:
        return (
            name,
            FAIL,
            f"the bearer in {source} expired {int((t - exp) // 3600)} h ago. Fix: {fix}.",
        )
    if post_json is None:
        return (name, PASS, f"bearer in {source} for {claims.get('sub', '?')} (not probed)")
    status, body = post_json(base_url, "namespace.list_mine", probe_body(claims), token, 10.0)
    if status == 401:
        return (
            name,
            FAIL,
            f"the server rejects the bearer in {source}: {_detail(body)}. This is the "
            f"client's 'failed to connect' / 'needs authentication'. Fix: {fix}.",
        )
    if status == 0:
        return (name, WARN, "skipped: the server was not reachable")
    if status != 200:
        return (name, WARN, f"probe answered HTTP {status}: {_detail(body)}")
    return (name, PASS, f"the bearer in {source} is accepted ({claims.get('sub', '?')})")


def _user_scope_bearers(home: Path, base_url: str) -> list[tuple[str, str]]:
    """The static bearers of ~/.claude.json entries pointing at this server."""
    try:
        servers = json.loads((home / ".claude.json").read_text()).get("mcpServers") or {}
    except (OSError, ValueError, AttributeError):
        return []
    return [
        (f"user-scope {n!r}", _bearer_from_headers(s.get("headers")))
        for n, s in servers.items()
        if isinstance(s, dict) and _same_server(str(s.get("url") or ""), base_url)
    ]


def _bearer_from_headers(headers: Any) -> str:
    if not isinstance(headers, dict):
        return ""
    for k, v in headers.items():
        if str(k).lower() == "authorization" and isinstance(v, str):
            m = re.match(r"^\s*Bearer\s+(\S+)\s*$", v, re.I)
            return m.group(1) if m else ""
    return ""


def _toml_str(text: str, key: str) -> str:
    m = re.search(rf'^\s*{re.escape(key)}\s*=\s*"([^"]*)"', text, re.M)
    return m.group(1) if m else ""


# The hook bounds derive from a measured round trip; these env values
# override them.
_HOOK_BOUND_DEFAULT_MS = 3000
_HOOK_OVERRIDES = ("LOOM_HOOK_TIMEOUT_MS", "LOOM_HOOK_EXIT_MS", "LOOM_SHUNT_READ_CAP")


def hook_override_check(home: Path, env: dict[str, str]) -> list[Check]:
    """One WARN per hook override found in the environment or in
    ~/.claude/settings.json `env` (which Claude Code applies to its hooks),
    saying what each one does."""
    settings_env = _claude_settings_env(home)
    found: list[tuple[str, str, str]] = []
    for name in _HOOK_OVERRIDES:
        sources = ((env, "the environment"), (settings_env, "~/.claude/settings.json env"))
        for where, source in sources:
            raw = str((where or {}).get(name) or "").strip()
            if raw:
                found.append((name, raw, source))
    out: list[Check] = []
    for name, raw, source in found:
        try:
            value = float(raw)
        except ValueError:
            value = -1.0
        if name == "LOOM_SHUNT_READ_CAP":
            why = (
                f"{name}={raw} in {source}: the ranged-read cap is a bench arm "
                "(it measured no effect), not a product default — remove it unless you are "
                "measuring it."
            )
        elif value == _HOOK_BOUND_DEFAULT_MS:
            why = (
                f"{name}={raw} in {source} equals the built-in floor; it is redundant — remove it."
            )
        elif value > _HOOK_BOUND_DEFAULT_MS:
            why = (
                f"{name}={raw} in {source}: every blocking hook (session primer, code shunt, file "
                f"context, blast radius) may hold a tool call for {value / 1000:.0f} s. The "
                "bound derives from the round trip measured at session start "
                "(5× rtt, 3–8 s) — remove the override."
            )
        else:
            why = (
                f"{name}={raw} in {source} is below the built-in floor; captures over a slow link "
                "will be dropped — remove it and let the measured round trip set the bound."
            )
        out.append(("client:hook-override", WARN, why))
    return out


def _claude_settings(home: Path, key: str) -> dict[str, Any]:
    """One object of ~/.claude/settings.json (`env`, `enabledPlugins`); {}
    when the file or the key is missing or not an object."""
    try:
        value = json.loads(_read(home / ".claude" / "settings.json") or "{}").get(key)
    except (ValueError, AttributeError):
        return {}
    return value if isinstance(value, dict) else {}


def _claude_settings_env(home: Path) -> dict[str, str]:
    """~/.claude/settings.json `env`, which Claude Code applies to its hooks."""
    return {k: v for k, v in _claude_settings(home, "env").items() if isinstance(v, str)}


# The PATH an app started from the Dock or a desktop launcher gets: no shell
# profile has run. (IDEs such as VS Code load the login shell's.)
GUI_PATH = "/usr/bin:/bin:/usr/sbin:/sbin"
WINDOWS = sys.platform == "win32"
# Of what Codex passes its MCP header helper (codex-rs rmcp-client
# DEFAULT_ENV_VARS), the part the launcher reads: no LOOM_NODE, no version
# manager's variables. LOOM_NODE_SYSTEM_DIRS is kept, as ensure-node.sh keeps
# it, so a test can replace the fixed directories.
CODEX_HELPER_ENV = ("HOME", "PATH", "LOOM_NODE_SYSTEM_DIRS")


def _plugin_version(name: str) -> tuple[int, ...]:
    """A plugin cache directory's version (1.2.10) as a sortable tuple."""
    return tuple(int(n) for n in re.findall(r"\d+", name))


def posix_sh(env: dict[str, str]) -> str | None:
    """The sh Claude Code runs hooks with: on Windows Git Bash, found the
    way Claude Code finds it (CLAUDE_CODE_GIT_BASH_PATH, else beside git)."""
    if not WINDOWS:
        return shutil.which("sh")
    if env.get("CLAUDE_CODE_GIT_BASH_PATH"):
        bash: Path | None = Path(env["CLAUDE_CODE_GIT_BASH_PATH"])
    else:
        git = shutil.which("git")
        bash = Path(git).parents[1] / "bin" / "bash.exe" if git else None
    return str(bash) if bash and bash.is_file() else None


def claude_plugin_install(home: Path) -> Path | None:
    """The user-scope loom plugin directory from Claude Code's own records;
    None when it is not installed there or user settings disable it. A
    project-scope install is enabled per project and is not checked."""
    if _claude_settings(home, "enabledPlugins").get("loom@loom") is False:
        return None
    try:
        installed = json.loads(_read(home / ".claude" / "plugins" / "installed_plugins.json"))
        installs = installed["plugins"]["loom@loom"]
        user = next(i for i in installs if i.get("scope") == "user")
        return Path(user["installPath"])
    except (ValueError, KeyError, TypeError, AttributeError, StopIteration):
        return None


def _launcher_check(sh: str, launcher: Path, env: dict[str, str]) -> tuple[int, str]:
    """`sh <launcher> --check`: its exit code and its one line. Raises
    OSError or TimeoutExpired when the launcher itself does not run."""
    r = subprocess.run(
        [sh, str(launcher), "--check"],
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    return r.returncode, (r.stdout or r.stderr).strip()


_NO_NODE = 'No hook runs, and the MCP entry gets no token ("needs authentication").'
_NO_SH = "no POSIX sh: the hooks and the MCP header helper run `sh` (Git Bash on Windows)."


def node_checks(home: Path, env: dict[str, str]) -> list[Check]:
    """The Node each installed plugin's hooks and MCP header helper run
    under, as that plugin's own launcher finds it in that client's
    environment."""
    out: list[Check] = []
    install = claude_plugin_install(home)
    if install is not None:
        out.append(_claude_node_check(home, env, install))
    # Like the header helper, take the newest copy with a launcher; with none,
    # the newest copy, so a pre-launcher plugin is named.
    cached = [p for p in _codex_plugin_cache(home).glob("*") if p.is_dir()]
    launched = [p for p in cached if (p / "bin" / "loom-node").is_file()]
    codex = max(launched or cached, key=lambda p: _plugin_version(p.name), default=None)
    enabled = codex_plugin_enabled(_read(home / ".codex" / "config.toml"))
    # On Windows, Codex runs hooks and the helper through cmd.exe, not the launcher.
    if codex is not None and enabled and not WINDOWS:
        out.append(_codex_node_check(env, codex))
    return out


def _predates_launcher(name: str, install: Path, client: str, fix: str) -> Check:
    return (
        name,
        WARN,
        f"the installed plugin at {install} predates the Node launcher: its hooks need "
        f"`node` on {client}'s PATH. Fix: {fix}.",
    )


def _claude_node_check(home: Path, env: dict[str, str], install: Path) -> Check:
    """Claude Code runs hooks and the helper with its own environment plus
    its settings `env`: this shell's, or a GUI launch's with no profile."""
    name = "client:claude-code:node"
    if not install.is_dir():
        return (
            name,
            WARN,
            f"the installed plugin at {install} is not on disk. Fix: "
            "`claude plugin uninstall loom@loom && claude plugin install loom@loom`.",
        )
    launcher = install / "bin" / "loom-node"
    if not launcher.is_file():
        return _predates_launcher(name, install, "Claude Code", "`claude plugin update loom@loom`")
    settings = _claude_settings_env(home)
    client_env = {**env, **settings}
    sh = posix_sh(client_env)
    if sh is None:
        return (name, FAIL, _NO_SH)
    try:
        code, line = _launcher_check(sh, launcher, client_env)
        if code != 0:
            return (name, FAIL, f"{line} {_NO_NODE} Fix: /loom:loom-status.")
        if WINDOWS:  # a Windows GUI app gets the user's PATH
            return (name, PASS, line)
        gui = {"HOME": str(home), "PATH": GUI_PATH, **settings}
        gui_code, _ = _launcher_check(sh, launcher, gui)
    except (OSError, subprocess.TimeoutExpired) as e:
        return (name, WARN, f"could not run {launcher}: {e}")
    if gui_code != 0:
        return (
            name,
            WARN,
            f"{line}, but only with this shell's environment: Claude Code started from "
            "the Dock or a desktop launcher finds no Node. Fix: set LOOM_NODE to that "
            "path in the `env` block of ~/.claude/settings.json.",
        )
    return (name, PASS, line)


def _codex_node_check(env: dict[str, str], install: Path) -> Check:
    """Codex runs hooks through a login shell, and the MCP header helper
    with only a few variables kept, LOOM_NODE not among them."""
    name = "client:codex:node"
    launcher = install / "bin" / "loom-node"
    if not launcher.is_file():
        return _predates_launcher(name, install, "Codex", "`codex plugin marketplace upgrade loom`")
    sh = shutil.which("sh")
    if sh is None:
        return (name, FAIL, _NO_SH)
    helper_env = {k: env[k] for k in CODEX_HELPER_ENV if k in env}
    try:
        code, line = _launcher_check(sh, launcher, env)
        if code != 0:
            return (name, FAIL, f"{line} {_NO_NODE} Fix: $loom-status.")
        helper_code, _ = _launcher_check(sh, launcher, helper_env)
    except (OSError, subprocess.TimeoutExpired) as e:
        return (name, WARN, f"could not run {launcher}: {e}")
    if helper_code != 0:
        return (
            name,
            WARN,
            f"{line} for the hooks, but the MCP header helper, which Codex runs without "
            "LOOM_NODE or a version manager's variables, finds no Node. Fix: put that "
            "Node's directory on the PATH Codex starts with.",
        )
    return (name, PASS, line)


def flush_check(tmp: Path, now: float) -> Check:
    """Captures the hooks handed to a detached flusher that never finished:
    `loom-flush-<ms>-<pid>.json` files older than STALE_FLUSH_S."""
    name = "client:flush"
    stale = 0
    total = 0
    try:
        names = [p.name for p in tmp.iterdir() if p.is_file()]
    except OSError:
        names = []
    for n in names:
        m = _FLUSH_RE.match(n)
        if not m:
            continue
        total += 1
        if now - int(m.group(1)) / 1000 > STALE_FLUSH_S:
            stale += 1
    if stale:
        return (
            name,
            WARN,
            f"{stale} capture job(s) in {tmp} older than {STALE_FLUSH_S // 60} min never reached "
            "the server (the hooks fail open). Fix: the auth and namespace checks above; "
            "then delete loom-flush-*.json.",
        )
    return (
        name,
        PASS,
        f"no stranded capture jobs in {tmp}" + (f" ({total} in flight)" if total else ""),
    )


# ---- the driver ----------------------------------------------------------------------


def client_checks(
    args: argparse.Namespace,
    *,
    get_json: GetJson,
    post_json: PostJson,
    home: Path | None = None,
    env: dict[str, str] | None = None,
    tmp: Path | None = None,
    now: float | None = None,
) -> list[Check]:
    """Every client-side line, in reading order: server, token, auth,
    namespace, the two clients' wiring, the hooks' Node, stranded captures."""
    home = home or Path.home()
    env = dict(os.environ if env is None else env)
    tmp = tmp or Path(env.get("TMPDIR") or "/tmp")
    now = time.time() if now is None else now
    base_url, url_src = resolve_server_url(getattr(args, "server_url", None), home, env)
    token, tok_src = resolve_token(getattr(args, "token", None), home, env)
    claims = jwt_claims(token) if token else {}

    checks: list[Check] = []
    server, _ = server_check(base_url, url_src, get_json)
    checks.append(server)
    agent = agent_mode(env)
    checks.append(token_check(token, tok_src, claims, now, base_url, agent=agent))
    checks.extend(token_shadow_check(token, tok_src, home, env))
    checks.extend(hook_override_check(home, env))
    auth, rows = (
        auth_check(base_url, token, claims, post_json, agent=agent)
        if server[1] != FAIL
        else (
            ("client:auth", WARN, "skipped: the server was not reachable"),
            [],
        )
    )
    checks.append(auth)
    target = intended_namespace(home, env, claims)
    if tok_src == AGENT_SOURCE:
        # What the agent's own token reaches is the server's to say (the
        # token check names the command); none of these can ask it.
        why = "skipped in agent mode: these checks act for no one"
        where = (
            f"{target!r} (LOOM_NAMESPACE)"
            if env.get("LOOM_NAMESPACE", "").strip()
            else "the exchanged token's namespace"
        )
        checks.append(("client:namespace", WARN, f"{why}; the hooks write to {where}"))
        checks.append(("client:recording", WARN, why))
        checks.append(("client:reads", WARN, why))
    else:
        checks.append(namespace_check(target, rows, token, auth[1] == PASS))
        checks.append(recording_check(base_url, token, claims, target, post_json, auth[1] == PASS))
        checks.append(reads_check(base_url, token, claims, rows, post_json, auth[1] == PASS))
    probe = post_json if server[1] != FAIL else None
    checks.extend(claude_code_checks(home, base_url, post_json=probe, now=now))
    if (home / ".claude").is_dir():  # A Claude Code machine
        checks.extend(
            code_access_check(
                _checkout_slug(Path(env.get("PWD") or os.getcwd())),
                base_url,
                [("plugin/hooks", token), *_user_scope_bearers(home, base_url)],
                probe,
            )
        )
    checks.extend(codex_checks(home, base_url, env=env, post_json=probe, now=now))
    checks.extend(node_checks(home, env))
    checks.append(flush_check(tmp, now))
    return checks


# ---- small helpers -------------------------------------------------------------------


def _detail(body: Any) -> str:
    d = body.get("detail") if isinstance(body, dict) else None
    if isinstance(d, dict):
        return str(d.get("message") or d.get("code") or d)[:200]
    return str(d or body)[:200]


def _read(p: Path) -> str:
    try:
        return p.read_text()
    except OSError:
        return ""


def _mentions(p: Path, *needles: str) -> bool:
    text = _read(p)
    return any(n in text for n in needles)


def _toml_url(text: str) -> str:
    m = re.search(r'^\s*url\s*=\s*"([^"]+)"', text, re.M)
    return m.group(1) if m else ""


def _same_server(url: str, base_url: str) -> bool:
    """Same scheme+host+port, ignoring path and query (`/mcp?toolset=coding`)."""
    from urllib.parse import urlsplit

    if not url:
        return False
    a, b = urlsplit(url), urlsplit(base_url)
    return (a.scheme, a.netloc.lower()) == (b.scheme, b.netloc.lower())
