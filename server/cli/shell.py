"""Shell clients of a running server (`ingest-transcript`, `memory-search`)
and `set-host` (platform admins in the operator's `.env`).
"""

from __future__ import annotations

import argparse
import hashlib
import sys
import urllib.error
from pathlib import Path
from typing import Any

from server.cli._client import (
    DEFAULT_BASE_URL,
    default_base_url,
    error_line,
    jwt_claims,
    refused_for_agent,
    resolve_token,
)
from server.cli._paths import write_private


def _cmd_memory_search(args: argparse.Namespace) -> int:
    """Run memory.search from the shell.

    Default: human-readable list. With --json-agent: a self-describing
    {"command": "search", "query": ..., "results": [...]} envelope for
    line-oriented-JSON pipelines.
    """
    import asyncio
    import json as _json
    import sys

    import httpx

    token, source = resolve_token(args.token)
    if refused_for_agent(source):
        return 2
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    # The token's org: authz checks the body's org against it.
    org = args.org or str(jwt_claims(token).get("org") or "") or "dev"
    body = {
        "org": org,
        "namespace": args.namespace,
        "agent": args.agent,
        "user_id": args.user_id,
        "query": args.query,
        "top_k": int(args.top_k),
        "search_mode": args.search_mode,
    }

    async def _go() -> int:
        async with httpx.AsyncClient(timeout=30.0) as client:
            try:
                r = await client.post(
                    f"{args.server_url}/v1/memory.search",
                    json=body,
                    headers=headers,
                )
            except httpx.HTTPError as e:  # refused, timed out, dropped
                print(
                    f"  ✗ memory.search: no answer from {args.server_url} ({type(e).__name__})",
                    file=sys.stderr,
                )
                return 1
            if r.status_code != 200:
                line = error_line(r.status_code, r.reason_phrase, r.content)
                print(f"  ✗ memory.search returned {line}", file=sys.stderr)
                return 1
            try:
                data = r.json()
            except ValueError:
                data = None
            if not isinstance(data, dict):
                print(
                    f"  ✗ memory.search: {args.server_url} did not answer a JSON object",
                    file=sys.stderr,
                )
                return 1
            results = [h for h in data.get("results") or [] if isinstance(h, dict)]
            if args.json_agent:
                envelope = {
                    "command": "search",
                    "query": args.query,
                    "namespace": args.namespace,
                    "results": [
                        {
                            "memory_key": h.get("memory_key"),
                            "content": h.get("content_excerpt"),
                            "similarity": h.get("similarity"),
                            "source": h.get("source", "fact"),
                            "temporal_anchor": h.get("temporal_anchor", ""),
                            "namespace": h.get("namespace", ""),
                        }
                        for h in results
                    ],
                }
                print(_json.dumps(envelope, indent=2))
                return 0
            if not results:
                print("  (no hits)")
                return 0
            print(f"  {len(results)} hit(s) for {args.query!r}:")
            for i, h in enumerate(results, 1):
                src = h.get("source", "fact")
                src_tag = "" if src == "fact" else f"[{src}] "
                key = h.get("memory_key", "")
                excerpt = (h.get("content_excerpt") or "").replace("\n", " ")[:160]
                sim = h.get("similarity", 0.0)
                print(f"  {i:2d}. {src_tag}{key}  (sim={sim:.3f})")
                print(f"      {excerpt}")
            return 0

    return asyncio.run(_go())


def _default_thread_id(src: Path) -> str:
    """The thread a transcript file lands in: its resolved path, so editing or
    re-exporting the file appends to the same thread instead of starting one."""
    return f"ingest-{hashlib.sha256(str(src).encode()).hexdigest()[:16]}"


def _turn_msg_ids(turns: list[dict[str, str]]) -> list[str]:
    """One msg_id per turn from its role and content, plus how many identical
    turns came before it. conversation.append skips a (thread, msg_id) that
    already landed, so a re-ingest writes only the turns that are new, and a
    turn inserted mid-file does not shift the ids of the turns after it."""
    seen: dict[tuple[str, str], int] = {}
    out: list[str] = []
    for t in turns:
        pair = (t["role"], t["content"])
        n = seen.get(pair, 0)
        seen[pair] = n + 1
        out.append(hashlib.sha256(f"{pair[0]}\n{n}\n{pair[1]}".encode()).hexdigest()[:32])
    return out


def _http_error_line(e: urllib.error.HTTPError) -> str:
    """`error_line` of an urllib HTTPError."""
    try:
        raw = e.read() or b""
    except OSError:
        raw = b""
    return error_line(e.code, str(e.reason), raw)


def _cmd_ingest_transcript(args: argparse.Namespace) -> int:
    """Read a chat history file, POST each turn to conversation.append.

    Two formats:
      - .json: an array of {role, content} objects.
      - .md (default): markdown with `# user` / `# assistant` H1 headers;
        content is everything between two headers.
    """
    import json as _json
    import re
    import time
    import urllib.request

    token, source = resolve_token(args.token)
    if refused_for_agent(source):
        return 2
    if not token:
        print(
            "ERROR: --token is required (or set LOOM_API_KEY, or save one to ~/.loom/token). "
            "Mint one with `loom mint-token --subject you@x --org default "
            "--namespaces my-ns --roles writer`.",
            file=sys.stderr,
        )
        return 2

    src = Path(args.file).expanduser().resolve()
    if not src.is_file():
        print(f"ERROR: file not found: {src}", file=sys.stderr)
        return 3
    raw = src.read_text(encoding="utf-8")

    turns: list[dict[str, str]] = []
    if src.suffix.lower() == ".json":
        try:
            parsed = _json.loads(raw)
        except _json.JSONDecodeError as e:
            print(f"ERROR: invalid JSON in {src}: {e}", file=sys.stderr)
            return 4
        if not isinstance(parsed, list):
            print(
                "ERROR: JSON must be a top-level array of {role, content} objects",
                file=sys.stderr,
            )
            return 4
        for i, item in enumerate(parsed):
            if not isinstance(item, dict) or "role" not in item or "content" not in item:
                print(f"ERROR: item {i} missing role/content", file=sys.stderr)
                return 4
            turns.append({"role": str(item["role"]), "content": str(item["content"])})
    else:
        # Markdown H1-split, permissive on case + leading whitespace.
        header_re = re.compile(r"^\s*#\s*(user|assistant|system|tool)\s*$", re.IGNORECASE)
        current_role: str | None = None
        current_body: list[str] = []
        for line in raw.splitlines():
            m = header_re.match(line)
            if m:
                if current_role is not None and current_body:
                    turns.append(
                        {
                            "role": current_role,
                            "content": "\n".join(current_body).strip(),
                        }
                    )
                current_role = m.group(1).lower()
                current_body = []
            elif current_role is not None:
                current_body.append(line)
        if current_role is not None and current_body:
            turns.append({"role": current_role, "content": "\n".join(current_body).strip()})

    turns = [t for t in turns if t["content"].strip()]
    if not turns:
        print(
            f"ERROR: no valid turns parsed from {src}. Expected JSON array "
            f"of {{role, content}} or markdown with `# user` / `# assistant` headers.",
            file=sys.stderr,
        )
        return 5

    thread_id = args.thread_id or _default_thread_id(src)
    msg_ids = _turn_msg_ids(turns)

    if args.dry_run:
        # Counts and sizes only: the transcript text stays out of the terminal
        # and whatever log captures it.
        print(f"would ingest {len(turns)} turns into thread {thread_id}")
        for i, t in enumerate(turns):
            print(f"  [{i}] {len(t['content'].encode('utf-8'))} bytes")
        return 0

    base = args.server_url.rstrip("/")
    namespace = args.namespace
    # The token's org, like memory-search: authz checks the body's org against it.
    org = args.org or str(jwt_claims(token).get("org") or "") or "dev"
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {token}",
    }
    # No conversation.create — append works against an unknown thread_id;
    # the thread row is implicit. One append per turn, fail-fast.
    written = 0
    for i, turn in enumerate(turns):
        body = {
            "org": org,
            "namespace": namespace,
            "agent": args.agent,
            "thread_id": thread_id,
            "msg_id": msg_ids[i],
            "role": turn["role"],
            "content": turn["content"],
        }
        data = _json.dumps(body).encode("utf-8")
        req = urllib.request.Request(
            f"{base}/v1/conversation.append",
            data=data,
            headers=headers,
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                resp.read()
        except urllib.error.HTTPError as e:
            print(f"ERROR: turn {i} failed: {_http_error_line(e)}", file=sys.stderr)
            return 6
        except urllib.error.URLError as e:
            print(f"ERROR: turn {i} network error: {e}", file=sys.stderr)
            return 7
        except OSError as e:  # a read timeout or a dropped connection after connect
            print(
                f"ERROR: turn {i}: no answer from {base} ({type(e).__name__}: {e}); "
                f"{written} turn(s) were written before it",
                file=sys.stderr,
            )
            return 1
        written += 1
        # Pace to avoid hammering CH's async-insert queue on large transcripts.
        if i % 30 == 29:
            time.sleep(0.5)

    print(f"✓ ingested {written} turns into thread {thread_id} on {namespace}")
    return 0


def _cmd_set_host(args: argparse.Namespace) -> int:
    """Write LOOM_PLATFORM_ADMINS into the operator's .env so the designated
    hosts auto-promote to platform_admin on next login. Idempotent.

    An email promotes an SSO (OIDC) sign-in with that verified email; a login
    handle (the namespace a person signed up with) promotes that password
    account. Requires a container restart to take effect (the docker compose
    command is printed).
    """
    from server.core.naming import validate_namespace_name

    # Split on commas + flatten + lowercase + dedupe, rejecting anything that
    # is neither an email nor a login handle.
    raw: list[str] = []
    for token in args.emails:
        raw.extend(part.strip() for part in token.split(",") if part.strip())
    incoming: list[str] = []
    for e in raw:
        low = e.lower()
        if "@" in low:
            valid = "." in low.split("@", 1)[1]
        else:
            try:
                validate_namespace_name(low)
                valid = True
            except ValueError:
                valid = False
        if not valid:
            print(f"ERROR: not an email or a login handle: {e!r}", file=sys.stderr)
            return 2
        if low not in incoming:
            incoming.append(low)

    env_path = Path(args.env_file).expanduser().resolve()
    lines: list[str] = []
    if env_path.exists():
        lines = env_path.read_text(encoding="utf-8").splitlines()
    elif not args.dry_run:
        env_path.parent.mkdir(parents=True, exist_ok=True)

    # Locate the LOOM_PLATFORM_ADMINS line; track its value so --append unions.
    key = "LOOM_PLATFORM_ADMINS"
    existing_value = ""
    found_idx = -1
    for i, raw_line in enumerate(lines):
        s = raw_line.lstrip()
        if s.startswith("#"):
            continue
        if "=" not in s:
            continue
        k, _, v = s.partition("=")
        if k.strip() == key:
            found_idx = i
            existing_value = v.strip().strip('"').strip("'")
            break

    if args.append and existing_value:
        existing: list[str] = []
        for part in existing_value.split(","):
            t = part.strip().lower()
            if t and t not in existing:
                existing.append(t)
        final = existing[:]
        for e in incoming:
            if e not in final:
                final.append(e)
    else:
        final = incoming

    new_line = f"{key}={','.join(final)}"

    if args.dry_run:
        print(f"would write to: {env_path}")
        print(f"  old: {key}={existing_value or '(unset)'}")
        print(f"  new: {new_line}")
        return 0

    if found_idx >= 0:
        lines[found_idx] = new_line
    else:
        if lines and lines[-1].strip():
            lines.append("")
        lines.append("# Operators of this Loom — promoted to platform_admin at their next login.")
        lines.append(new_line)

    text = "\n".join(lines)
    write_private(env_path, text if text.endswith("\n") else text + "\n")

    print(f"✓ wrote {key} → {env_path}")
    print(f"  hosts: {', '.join(final)}")
    print("  next: restart Loom so the new env is picked up:")
    print(
        "    docker compose --env-file .env -f compose.yaml -f dev/docker-compose.dev.yml"
        " --profile bundled up -d --force-recreate loom"
    )
    print(
        "  then have each host sign out and back in to the dashboard"
        " to mint a fresh platform_admin JWT."
    )
    return 0


def _add_shell_client_parsers(sub: Any) -> None:
    """The shell clients of a running server: `ingest-transcript`, `memory-search`
    and `set-host`."""
    p_ingest = sub.add_parser(
        "ingest-transcript",
        help=(
            "Pipe a chat-history file (markdown / JSON) into Loom's "
            "conversation table as one thread. Generic ingest for tools "
            "without an MCP integration (Aider, Continue.dev, JetBrains "
            "AI, anything else): export the chat → run this command → "
            "the thread shows in the dashboard's Conversations."
        ),
    )
    p_ingest.add_argument(
        "--server-url",
        default=default_base_url(),
        help=f"Loom server URL (default LOOM_BASE_URL, else {DEFAULT_BASE_URL}).",
    )
    p_ingest.add_argument(
        "--token",
        default=None,
        help="JWT to authenticate with. Defaults to LOOM_API_KEY, then ~/.loom/token.",
    )
    p_ingest.add_argument("--org", default="", help="default: the token's org, else dev")
    p_ingest.add_argument(
        "--namespace",
        required=True,
        help="Namespace to ingest the thread into.",
    )
    p_ingest.add_argument(
        "--agent",
        default="ingest-cli",
        help="Agent identifier on the conversation rows (default ingest-cli).",
    )
    p_ingest.add_argument(
        "--thread-id",
        default=None,
        help="Thread to append to (default: a hash of the file's resolved path, so "
        "re-ingesting an edited file adds only its new turns).",
    )
    p_ingest.add_argument(
        "--file",
        required=True,
        help="Path to the transcript file. Format: markdown with "
        "alternating `# user` / `# assistant` H1 sections, OR JSON "
        "array of {role, content} objects.",
    )
    p_ingest.add_argument(
        "--dry-run",
        action="store_true",
        help="Parse and print the turn count and each turn's size in bytes "
        "(never its text) without writing anything.",
    )
    p_ingest.set_defaults(func=_cmd_ingest_transcript)

    p_msearch = sub.add_parser(
        "memory-search",
        help=(
            "Run a memory.search from the shell. Default human output; "
            "--json-agent emits a structured envelope agents can pipe "
            "through (a `{command, results}` JSON shape)."
        ),
    )
    p_msearch.add_argument("--server-url", default=default_base_url())
    p_msearch.add_argument("--token", default=None)
    p_msearch.add_argument("--org", default="", help="default: the token's org, else dev")
    p_msearch.add_argument("--namespace", required=True)
    p_msearch.add_argument("--agent", default="cli")
    p_msearch.add_argument("--user-id", default="-")
    p_msearch.add_argument("--query", required=True)
    p_msearch.add_argument("--top-k", type=int, default=10)
    p_msearch.add_argument("--search-mode", default="bm25_hybrid")
    p_msearch.add_argument(
        "--json-agent",
        action="store_true",
        help="Emit `{command, query, results}` JSON envelope on stdout "
        "for agent pipelines (Aider, Continue, OpenCode, etc.).",
    )
    p_msearch.set_defaults(func=_cmd_memory_search)

    p_host = sub.add_parser(
        "set-host",
        help=(
            "Designate the Loom host(s) for this deployment. Writes the "
            "given login handle(s) or email(s) to LOOM_PLATFORM_ADMINS in the "
            ".env (--env-file, default ./.env): a handle promotes that password "
            "account to platform_admin, an email promotes an SSO sign-in with "
            "that verified email. Multiple OK (on-call rotation, co-operators, "
            "etc.). Re-login required after to pick up the role."
        ),
    )
    p_host.add_argument(
        "emails",
        nargs="+",
        help="Operator login handle(s) or SSO email(s). Comma-separated also works. Replaces "
        "the existing LOOM_PLATFORM_ADMINS list — pass --append to "
        "extend instead.",
    )
    p_host.add_argument(
        "--env-file",
        default=".env",
        help="Path to the .env to update (default: .env at cwd).",
    )
    p_host.add_argument(
        "--append",
        action="store_true",
        help="Add to the existing host list instead of replacing it.",
    )
    p_host.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would change without writing.",
    )
    p_host.set_defaults(func=_cmd_set_host)
