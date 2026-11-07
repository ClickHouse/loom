"""`loom init --agent NAME` — an agent signs itself up.

One command, no human, no operator: POST `/v1/auth/agent-signup` on a
server that has `LOOM_SELF_SIGNUP=open`, write the returned token where the
hooks, the SDK and every `loom` subcommand look for one (`~/.loom/token`,
0600) and the server URL + namespace + org where the coding-agent plugins
look (`~/.loom/config.json`, 0600), then print the two facts the operator of
the bot needs: how the bot connects, and the claim code a person enters once
at the claim page, with a password of their own, to take ownership of the namespace.

Every HTTP call goes through `_post_json` so the unit tests swap it
(`tests/unit/test_agent_signup.py`).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from server.cli._client import TOKEN_FILE, agent_mode, default_base_url, post_json
from server.cli._paths import write_private

CONFIG_PATH = "~/.loom/config.json"


def _post_json(url: str, body: dict[str, Any], timeout: float = 15.0) -> tuple[int, dict[str, Any]]:
    """The sign-up POST (no token yet); the tests replace it."""
    return post_json(url, body, "", timeout)


def explain_failure(status: int, body: dict[str, Any], base_url: str) -> str:
    """One line a person can act on, per status: say what happened and how to
    get in."""
    raw = body.get("detail") if isinstance(body, dict) else None
    detail = raw if isinstance(raw, dict) else {}
    code = str(detail.get("code") or "")
    msg = str(detail.get("message") or body.get("message") or (raw if isinstance(raw, str) else ""))
    if status == 0:
        return f"could not reach {base_url}: {msg}. Is the server up? (`loom up`, or --server-url)"
    if status == 404 and code in {"SELF_SIGNUP_DISABLED", "SELF_SIGNUP_SSO"}:
        return (
            f"{base_url} does not accept agent sign-ups ({code}): {msg}. Ask the operator "
            "for a token (`loom mint-token --save`) or to set LOOM_SELF_SIGNUP=open."
        )
    if status == 404:
        return f"{base_url} has no agent-signup route (check --server-url): {msg}"
    if status == 409:
        return f"name taken: {msg}"
    if status == 422:
        return f"rejected: {msg}"
    if status == 429:
        return f"slow down: {msg}"
    return f"HTTP {status}: {msg or body}"


def write_token(token: str, path: Path) -> Path:
    """Write `token` to `path` readable by the owner only (0600), replacing
    any existing file (one a `> ~/.loom/token` redirect left at the umask)."""
    return write_private(path, token.strip() + "\n")


def write_config(path: Path, *, url: str, namespace: str, org: str) -> Path:
    """Merge url / namespace / org into `~/.loom/config.json` (the file both
    coding-agent plugins' hooks read after the env), keeping other keys."""
    existing: dict[str, Any] = {}
    if path.exists():
        try:
            loaded = json.loads(path.read_text())
            existing = loaded if isinstance(loaded, dict) else {}
        except json.JSONDecodeError:
            existing = {}
    existing.update({"url": url.rstrip("/"), "namespace": namespace, "org": org})
    return write_private(path, json.dumps(existing, indent=2) + "\n")


def connect_lines(out: dict[str, Any], *, base_url: str, token_path: str, config_path: str) -> str:
    ns, org = out["namespace"], out["org"]
    agent = ns[len("agent-") :] or ns
    mcp_url = f"{base_url}/mcp?toolset=coding"
    claim_url = out.get("claim_url") or f"{base_url}/"
    lines = [
        f"✓ signed up: {org}/{ns} (subject {out['subject']})",
        f"  token   → {token_path} (0600); url, namespace, org → {config_path}",
        "",
        "  Connect:",
        f'    export LOOM_API_KEY="$(cat {token_path})"',
        f"    export LOOM_BASE_URL={base_url}",
        "    # Python:  pip install ./sdk/python  →  "
        f"Datapack(org={org!r}, namespace={ns!r}).agent({agent!r})",
        f'    # Claude Code:  claude mcp add --transport http loom "{mcp_url}" '
        '-H "Authorization: Bearer $LOOM_API_KEY" -s user',
        f'    # Codex:        codex mcp add loom --url "{mcp_url}" '
        "--bearer-token-env-var LOOM_API_KEY",
        "",
        "  Hand the namespace to a person (the code works once; keep it out of logs):",
        f"    open {claim_url}, enter name {ns!r}, code {out['claim_code']} and a password",
        "    — they sign in as its admin from then on; this agent's token keeps working.",
    ]
    if out.get("expires_at"):
        lines.append(
            f"  Token expires {out['expires_at']}; re-run `loom init --agent` before then "
            "or mint one in the dashboard."
        )
    return "\n".join(lines)


def _cmd_init(args: argparse.Namespace) -> int:
    if agent_mode():
        # An agent signs in as its workload trust or not at all: a signed-up
        # identity would be a second one, and its files would replace the saved config.
        print(
            "ERROR: not signing up in agent mode: this agent signs in through its workload "
            "token (`loom call` exchanges it). To sign a new agent up, unset LOOM_CLIENT_ID and "
            "pass --no-files or --token-path and --config-path, so the saved files stay.",
            file=sys.stderr,
        )
        return 2
    if not args.agent:
        print("loom init: pass --agent NAME (a person signs up in the dashboard).", file=sys.stderr)
        return 2
    base_url = str(args.server_url).rstrip("/")
    body: dict[str, Any] = {"name": args.agent, "purpose": args.purpose or ""}
    if args.org:
        body["org"] = args.org
    if args.ttl_days is not None:
        body["ttl_days"] = int(args.ttl_days)
    status, out = _post_json(f"{base_url}/v1/auth/agent-signup", body)
    if status != 200 or "token" not in out:
        print(f"ERROR: {explain_failure(status, out, base_url)}", file=sys.stderr)
        return 1
    token_path = Path(args.token_path).expanduser()
    config_path = Path(args.config_path).expanduser()
    if args.json:
        # With --no-files the JSON is the only place the token lands, so it
        # rides inside the object: stdout stays one parseable document.
        shown = out if args.no_files else {k: v for k, v in out.items() if k != "token"}
        print(json.dumps(shown, indent=2, default=str))
    if not args.no_files:
        try:
            write_token(out["token"], token_path)
            write_config(config_path, url=base_url, namespace=out["namespace"], org=out["org"])
        except OSError as e:
            where = e.filename or "the token file"
            print(f"ERROR: signed up, but could not write {where}: {e}", file=sys.stderr)
            print(f"  token: {out['token']}", file=sys.stderr)
            return 1
    elif not args.json:
        print(out["token"])
    if not args.json:
        print(
            connect_lines(
                out, base_url=base_url, token_path=str(token_path), config_path=str(config_path)
            )
        )
    return 0


_INIT_HELP = (
    "Create an identity on a Loom server. `--agent NAME` signs an agent up by "
    "itself (server needs LOOM_SELF_SIGNUP=open): mints a writer token on a fresh "
    "agent-<slug> namespace, saves it to ~/.loom/token, and prints the claim code "
    "a person enters once, with a password of their own, to claim the namespace."
)


def _add_init_flags(p: argparse.ArgumentParser) -> None:
    p.add_argument(
        "--agent", metavar="NAME", default="", help="Sign this agent up (becomes agent-<slug>)."
    )
    p.add_argument("--server-url", default=default_base_url())
    p.add_argument("--org", default=None, help="Org (default: the server's self-signup org).")
    p.add_argument("--purpose", default="", help="One line stored as the namespace description.")
    p.add_argument("--ttl-days", type=int, default=None, help="Token lifetime (default 365).")
    p.add_argument(
        "--token-path", default=TOKEN_FILE, help=f"Where to save the token (default {TOKEN_FILE})."
    )
    p.add_argument(
        "--config-path",
        default=CONFIG_PATH,
        help=f"Where to save url/namespace/org (default {CONFIG_PATH}).",
    )
    p.add_argument(
        "--no-files",
        action="store_true",
        help="Print the token instead of saving files (CI secrets).",
    )
    p.add_argument(
        "--json", action="store_true", help="Print the sign-up result (minus the token) as JSON."
    )


def register_init_cli(sub: Any) -> None:
    p = sub.add_parser("init", help=_INIT_HELP)
    _add_init_flags(p)
    p.set_defaults(func=_cmd_init)
