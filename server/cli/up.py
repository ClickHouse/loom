"""`loom up` — one command from a clean clone to a stack you can talk to.

What it does, in place of `make dev` + a health poll + `loom mint-token`:

  1. bootstrap: copy `.env.example` → `.env` and the ClickHouse
     `named_collections.xml` template when they are missing (the same two
     files `make bootstrap` creates; no `make` needed).
  2. `docker compose --env-file .env -f compose.yaml -f
     dev/docker-compose.dev.yml --profile bundled up -d --build` (`--file` /
     `--no-build` override).
  3. poll `GET <base>/v1/health` until `status: ok`, one progress line every
     5 s with the pending-migrations count, `--timeout` (300 s) with the last
     health body and the server's last 40 log lines on failure.
  4. print how to connect. Stock `.env` = single-user (no login, no token):
     the dashboard URL, a `curl memory.set`, the Python SDK snippet and the
     `claude mcp add` line, all on `dev/local`. Auth on: mint a writer token
     through the server container and print it with the path to save it to.
  5. `--open` opens the dashboard.

Re-running on a running stack re-checks health and reprints (compose `up -d`
is idempotent; `--rebuild` forces the image rebuild a code change needs).

Every subprocess / HTTP / sleep goes through a module-level helper so the
unit tests can swap them (`tests/unit/test_cli_up.py`).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from server.cli._client import TOKEN_FILE, get_json
from server.cli._paths import write_private
from server.core.paths import REPO_ROOT

# The shipped stack, then the source build with development defaults.
DEFAULT_COMPOSE_FILES = ("compose.yaml", "dev/docker-compose.dev.yml")
DEFAULT_ENV_FILE = ".env"
DEFAULT_PORT = 7777
DEFAULT_TIMEOUT_S = 300
NAMED_COLLECTIONS = Path("deploy/clickhouse-config/named_collections.xml")

# The compose file's `loom` service, when the scan below cannot tell.
_FALLBACK_SERVICE = "loom"
_FALLBACK_CONTAINER = "loom-server"

# Seams the tests replace. Named so a test reads as "the health endpoint
# answered X", not as a urllib mock.
_run: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run
_sleep: Callable[[float], None] = time.sleep
_clock: Callable[[], float] = time.monotonic


def _http_get_json(url: str, timeout: float) -> tuple[int, dict[str, Any] | None]:
    """GET `url`; (status, JSON body), or (0, None) when no Loom answer came.
    A 503 (health's "ClickHouse not answering") still carries its body."""
    status, body = get_json(url, "", timeout)
    return (status, body) if status else (0, None)


# ---- 1. bootstrap ------------------------------------------------------------


def repo_root(explicit: str | None) -> Path | None:
    """Where `.env.example` lives: `--root`, else the cwd, else the checkout
    the `server` package was installed from (`pip install -e .`). None when
    no candidate has an `.env.example` — the caller says how to fix that."""
    candidates: list[Path] = []
    if explicit:
        candidates.append(Path(explicit))
    else:
        candidates.append(Path.cwd())
        candidates.append(REPO_ROOT)
    for c in candidates:
        if (c / ".env.example").is_file():
            return c.resolve()
    return None


_EXTRACTOR_TOKEN = "LOOM_DOCUMENT_EXTRACTOR_TOKEN"


def bootstrap(root: Path, env_file: str) -> list[str]:
    """`make bootstrap` without make: create `.env` and the ClickHouse
    named_collections.xml bind-mount file when missing, and give an `.env`
    without an extractor token one. Returns one line per file created or
    changed (empty when there was nothing to do)."""
    created: list[str] = []
    env_path = root / env_file
    if not env_path.exists():
        # Owner-only, like every other file the CLI writes a secret into:
        # the next edit puts provider keys and the JWT secret here.
        template = (root / ".env.example").read_text(encoding="utf-8")
        template = template.replace(
            "LOOM_JWT_SECRET=",
            f"LOOM_JWT_SECRET={secrets.token_urlsafe(32)}",
            1,
        )
        template = template.replace(
            "LOOM_DOCUMENT_EXTRACTOR_TOKEN=",
            f"LOOM_DOCUMENT_EXTRACTOR_TOKEN={secrets.token_hex(24)}",
            1,
        )
        template = template.replace(
            "COMPOSE_PROJECT_NAME=",
            f"COMPOSE_PROJECT_NAME=loom-local-{secrets.token_hex(4)}",
            1,
        )
        write_private(env_path, template)
        created.append(f"created {env_file} from .env.example")
    elif not read_env_file(env_path).get(_EXTRACTOR_TOKEN):
        # An .env from before the extractor service: `make dev` would start it
        # with no token, which it refuses.
        text = env_path.read_text(encoding="utf-8")
        line = f"{_EXTRACTOR_TOKEN}={secrets.token_hex(24)}"
        blank = re.compile(rf"^{_EXTRACTOR_TOKEN}=[ \t]*$", re.MULTILINE)
        text = (
            blank.sub(line, text, count=1)
            if blank.search(text)
            else text.rstrip("\n") + f"\n{line}\n"
        )
        write_private(env_path, text)
        created.append(f"added {_EXTRACTOR_TOKEN} to {env_file}")
    nc = root / NAMED_COLLECTIONS
    named_collections_template = Path(str(nc) + ".template")
    if not nc.exists() and named_collections_template.exists():
        shutil.copyfile(named_collections_template, nc)
        created.append(f"created {NAMED_COLLECTIONS.as_posix()} from template")
    return created


def read_env_file(path: Path) -> dict[str, str]:
    """KEY=VALUE lines of a dotenv file; comments and blanks skipped, a
    surrounding pair of quotes stripped. Enough for the six LOOM_* keys the
    mode detection reads — the container itself gets the file via compose."""
    out: dict[str, str] = {}
    if not path.is_file():
        return out
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[len("export ") :]
        key, _, value = line.partition("=")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        out[key.strip()] = value
    return out


def stack_mode(env: dict[str, str]) -> str:
    """The identity mode the dev compose boots from this `.env`: mirrors
    `Settings.effective_identity_provider()` with the compose file's
    `LOOM_SELF_SIGNUP=open` pin — so auth-on is
    trust-network unless the provider is named explicitly."""
    explicit = env.get("LOOM_IDENTITY_PROVIDER", "").strip()
    if explicit in ("single-user", "trust-network", "gateway"):
        return explicit
    auth_required = env.get("LOOM_AUTH_REQUIRED", "false").strip().lower()
    if auth_required in ("", "0", "false", "no", "off"):
        return "single-user"
    return "trust-network"


# ---- 2. docker compose -------------------------------------------------------


def compose_names(compose_text: str) -> tuple[str, str]:
    """(service, container_name) of the Loom server in a compose file,
    without PyYAML (not a server dependency): scan two-space service keys
    under `services:` and their `container_name:`. Prefers the service named
    `loom`; else the first non-clickhouse service with a container name."""
    services: dict[str, str] = {}
    in_services = False
    current = ""
    for raw in compose_text.splitlines():
        if raw.startswith("services:"):
            in_services = True
            continue
        if not in_services:
            continue
        if raw and not raw[0].isspace() and raw.rstrip().endswith(":"):
            break  # next top-level key (volumes:, networks:, …)
        stripped = raw.strip()
        indent = len(raw) - len(raw.lstrip(" "))
        if indent == 2 and stripped.endswith(":") and not stripped.startswith("#"):
            current = stripped[:-1]
            services.setdefault(current, "")
        elif current and stripped.startswith("container_name:"):
            services[current] = stripped.split(":", 1)[1].strip().strip("\"'")
    if _FALLBACK_SERVICE in services:
        return _FALLBACK_SERVICE, services[_FALLBACK_SERVICE] or _FALLBACK_CONTAINER
    for name, container in services.items():
        if container and "clickhouse" not in name:
            return name, container
    return _FALLBACK_SERVICE, _FALLBACK_CONTAINER


def compose_project_name(root: Path, env_file: str) -> str:
    """A checkout-stable Compose project, with an explicit override.

    Compose normally derives this from the first compose file's directory,
    the checkout's name, so equally named clones would share volumes. Include
    the resolved checkout path in a short digest so they cannot adopt each
    other's local data.
    """
    configured = read_env_file(root / env_file).get("COMPOSE_PROJECT_NAME", "").strip()
    if not configured:
        configured = os.environ.get("COMPOSE_PROJECT_NAME", "").strip()
    if configured:
        return configured
    stem = re.sub(r"[^a-z0-9_-]+", "-", root.name.lower()).strip("-_") or "local"
    digest = hashlib.sha256(str(root.resolve()).encode("utf-8")).hexdigest()[:8]
    return f"loom-{stem[:32]}-{digest}"


def compose_argv(
    root: Path, compose_files: tuple[str, ...], env_file: str, *tail: str
) -> list[str]:
    files = [arg for name in compose_files for arg in ("-f", str(root / name))]
    return [
        "docker",
        "compose",
        "--project-name",
        compose_project_name(root, env_file),
        "--env-file",
        str(root / env_file),
        *files,
        # Starts the bundled ClickHouse whatever COMPOSE_PROFILES the .env names.
        "--profile",
        "bundled",
        *tail,
    ]


def _git_short_head(root: Path) -> str:
    """`LOOM_BUILD_COMMIT` for the image build, as the Makefile passes it;
    `unknown` when git is missing or this is not a checkout."""
    try:
        cp = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=str(root),
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return "unknown"
    if cp.returncode != 0:
        return "unknown"
    return cp.stdout.strip() or "unknown"


def compose_up(
    root: Path,
    compose_files: tuple[str, ...],
    env_file: str,
    *,
    build: bool,
    recreate_service: str = "",
) -> int:
    tail = ["up", "-d", *(["--build"] if build else [])]
    if recreate_service:
        # Compose may consider a manually restarted container current even after its
        # env file changed.  Recreate only Loom so provider/auth changes take effect
        # without disrupting the durable ClickHouse service.
        tail.extend(["--force-recreate", "--no-deps", recreate_service])
    argv = compose_argv(root, compose_files, env_file, *tail)
    env = dict(os.environ)
    # Importing Loom may have loaded the caller checkout's .env into os.environ.
    # Those values have higher Compose interpolation precedence than --env-file and
    # can otherwise leak auth/credentials into a different --root checkout.
    selected_env = read_env_file(root / env_file)
    for key in selected_env:
        env.pop(key, None)
    env["LOOM_BUILD_COMMIT"] = _git_short_head(root)
    print("→ " + " ".join(argv))
    cp = _run(argv, cwd=str(root), env=env, check=False)
    return int(cp.returncode)


def compose_down(
    root: Path, compose_files: tuple[str, ...], env_file: str, *, delete_data: bool = False
) -> int:
    """Stop the checkout's stack; volumes are retained unless explicitly requested."""
    argv = compose_argv(
        root,
        compose_files,
        env_file,
        "down",
        *(["--volumes"] if delete_data else []),
    )
    print("→ " + " ".join(argv))
    cp = _run(argv, cwd=str(root), check=False)
    return int(cp.returncode)


def compose_logs(root: Path, compose_files: tuple[str, ...], env_file: str, service: str) -> None:
    argv = compose_argv(root, compose_files, env_file, "logs", "--tail", "40", service)
    print("→ " + " ".join(argv), file=sys.stderr)
    _run(argv, cwd=str(root), check=False)


# ---- 3. health ---------------------------------------------------------------


def _health_state(status_code: int, body: dict[str, Any] | None) -> str:
    """One phrase for the progress line: what the endpoint said last."""
    if body is None:
        return "not answering yet" if status_code == 0 else f"HTTP {status_code}, no JSON body"
    state = f"status={body.get('status', '?')}"
    pending = body.get("pending_migrations")
    if isinstance(pending, list):
        state += f", pending migrations={len(pending)}"
    schema = body.get("schema")
    if isinstance(schema, dict) and isinstance(schema.get("pending"), list):
        state += f" (ledger lacks {len(schema['pending'])})"
    if body.get("ch_error"):
        state += f", ch_error={str(body['ch_error'])[:80]}"
    return state


def wait_for_health(
    health_url: str, *, timeout_s: float, poll_s: float = 2.0, progress_every_s: float = 5.0
) -> tuple[bool, int, dict[str, Any] | None]:
    """Poll until the body says `status: ok`. Returns (ok, last_status_code,
    last_body). A progress line every `progress_every_s` names what the
    endpoint said last, so a slow first boot (image build + every migration)
    reads as progress rather than a hang."""
    start = _clock()
    next_progress = start + progress_every_s
    status_code, body = 0, None
    while True:
        status_code, body = _http_get_json(health_url, timeout=3.0)
        if body is not None and body.get("status") == "ok":
            return True, status_code, body
        now = _clock()
        if now - start >= timeout_s:
            return False, status_code, body
        if now >= next_progress:
            elapsed = int(now - start)
            print(f"  … waiting for {health_url} ({elapsed} s): {_health_state(status_code, body)}")
            next_progress = now + progress_every_s
        _sleep(poll_s)


def _print_health_failure(
    health_url: str, timeout_s: float, status_code: int, body: dict[str, Any] | None
) -> None:
    print(
        f"✗ {health_url} did not report status=ok within {int(timeout_s)} s "
        f"(last: {_health_state(status_code, body)})",
        file=sys.stderr,
    )
    if body is not None:
        rendered = json.dumps(body, sort_keys=True)
        if len(rendered) > 1200:
            rendered = rendered[:1200] + " …"
        print(f"  last health body: {rendered}", file=sys.stderr)


# ---- 4. connect --------------------------------------------------------------


# A mint is a local signing call; a `docker exec` that has not answered by then
# is a wedged container or daemon, and `loom up` should say so and carry on.
_MINT_TIMEOUT_S = 60


def mint_token_via_container(
    container: str, *, subject: str, org: str, namespace: str
) -> tuple[str, str]:
    """`docker exec <container> loom mint-token …` — the operator mint path
    the README gives, run for the user. Returns (token, error): one is
    empty. The signing secret is the container's, so the token verifies
    against the running server whatever the host has in its shell."""
    argv = [
        "docker",
        "exec",
        container,
        "loom",
        "mint-token",
        "--subject",
        subject,
        "--org",
        org,
        "--namespaces",
        namespace,
        "--roles",
        "writer",
    ]
    try:
        cp = _run(argv, capture_output=True, text=True, check=False, timeout=_MINT_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        return (
            "",
            f"`docker exec {container} loom mint-token` gave no answer in {_MINT_TIMEOUT_S} s",
        )
    except OSError as e:
        return "", f"{type(e).__name__}: {e}"
    token = (cp.stdout or "").strip().splitlines()[-1:] or [""]
    if cp.returncode != 0 or not token[0]:
        err = (cp.stderr or "").strip() or f"exit {cp.returncode}"
        return "", f"`{' '.join(argv)}` failed: {err}"
    return token[0], ""


def _sdk_snippet(base_url: str, org: str, namespace: str, *, with_token: bool) -> str:
    key_arg = ', api_key=os.environ["LOOM_API_KEY"]' if with_token else ""
    imports = "import asyncio, os" if with_token else "import asyncio"
    return "\n".join(
        [
            f"    {imports}",
            "    from loom import Agent",
            "",
            "    async def main():",
            f'        async with Agent(org="{org}", namespace="{namespace}", agent="quickstart",',
            f'                         base_url="{base_url}"{key_arg}) as a:',
            '            await a.memory.set("deploy_window", "Tue and Thu only; never Friday.")',
            '            for hit in await a.memory.search("when can we deploy?"):',
            "                print(hit.content_excerpt)",
            "",
            "    asyncio.run(main())",
        ]
    )


def _curl_snippet(base_url: str, org: str, namespace: str, *, with_token: bool) -> str:
    auth = ' -H "Authorization: Bearer $LOOM_API_KEY"' if with_token else ""
    body = json.dumps(
        {
            "org": org,
            "namespace": namespace,
            "agent": "quickstart",
            "memory_key": "deploy_window",
            "value": "Tue and Thu only; never Friday.",
        }
    )
    return (
        f"    curl -sX POST {base_url}/v1/memory.set{auth} \\\n"
        f"      -H 'Content-Type: application/json' -d '{body}'"
    )


def _claude_mcp_add_line(base_url: str, token: str) -> str:
    """The `claude mcp add` registration line `loom install --client
    claude-code` runs, with the coding toolset on the URL."""
    from server.cli.install import _CLAUDE_CODE_DEFAULT_TOOLSET, _install_mcp_url, _shell_words

    mcp_url = _install_mcp_url(base_url, _CLAUDE_CODE_DEFAULT_TOOLSET)
    argv = ["claude", "mcp", "add", "--transport", "http", "loom", mcp_url]
    if token:
        argv += ["-H", f"Authorization: Bearer {token}"]
    argv += ["-s", "user"]
    return _shell_words(argv)


def print_connect(
    *, base_url: str, mode: str, org: str, namespace: str, token: str, build_commit: str
) -> None:
    """The "how to connect" block: dashboard, first write (curl + SDK), and
    the Claude Code MCP line. Single-user prints tokenless snippets; auth-on
    prints the minted token first and threads it through every snippet."""
    dashboard = f"{base_url}/"
    print(f"\n✓ Loom is up at {base_url} (build {build_commit}, {mode} mode)")
    if mode == "single-user":
        print(f"  No login and no token: every call lands in {org}/{namespace}.")
        print("  Never expose single-user mode to a network — it is a laptop mode.")
    else:
        print(f"  Minted a writer token for {org}/{namespace}; sign-up also works at {dashboard}.")
    print(f"\n  Dashboard:  {dashboard}")
    print(f"  Health:     {base_url}/v1/health")
    if token:
        print(f"\n  Token (save it as {TOKEN_FILE}, mode 0600):")
        print(f"    {token}")
        print(f"    mkdir -p ~/.loom && (umask 077; printf '%s' '{token}' > {TOKEN_FILE})")
        print(f"    export LOOM_API_KEY='{token}'")
    with_token = bool(token)
    print("\n  Save a first memory with curl:")
    print(_curl_snippet(base_url, org, namespace, with_token=with_token))
    print("\n  …or with the Python SDK (pip install ./sdk/python):")
    print(_sdk_snippet(base_url, org, namespace, with_token=with_token))
    print("\n  Wire Claude Code (MCP, coding toolset):")
    print(f"    {_claude_mcp_add_line(base_url, token)}")
    print("  Other clients: `loom install` (Claude Desktop, Cursor, VS Code, …).")


# ---- the command -------------------------------------------------------------


def _add_up_flags(p: argparse.ArgumentParser) -> None:
    p.add_argument(
        "--root",
        default=None,
        help="The loom checkout (where .env.example lives). Default: the current "
        "directory, else the checkout this `loom` was installed from.",
    )
    p.add_argument(
        "--file",
        action="append",
        help="Compose file relative to --root; repeat to stack overrides "
        f"(default {' + '.join(DEFAULT_COMPOSE_FILES)}).",
    )
    p.add_argument(
        "--env-file",
        default=DEFAULT_ENV_FILE,
        help=f"Env file passed to compose as --env-file (default {DEFAULT_ENV_FILE}; "
        "created from .env.example when missing).",
    )
    p.add_argument("--no-build", action="store_true", help="Skip --build (reuse the cached image).")
    p.add_argument(
        "--rebuild",
        action="store_true",
        help="Rebuild the Loom image before reconciling the running stack (use after "
        "a source-code change; configuration changes are applied without this flag).",
    )
    p.add_argument(
        "--port", type=int, default=DEFAULT_PORT, help="Loom's host port (default 7777)."
    )
    p.add_argument(
        "--timeout",
        type=float,
        default=DEFAULT_TIMEOUT_S,
        help=f"Seconds to wait for /v1/health to report ok (default {DEFAULT_TIMEOUT_S}).",
    )
    p.add_argument(
        "--subject",
        default=None,
        help="Token subject when auth is on (default: your login name).",
    )
    p.add_argument(
        "--org", default=None, help="Org (default: LOOM_SELF_SIGNUP_ORG in .env, else dev)."
    )
    p.add_argument(
        "--namespace",
        default=None,
        help="Namespace (default: LOOM_DEFAULT_NAMESPACE in .env, else local).",
    )
    p.add_argument("--open", action="store_true", help="Open the dashboard in a browser when up.")


def _add_down_flags(p: argparse.ArgumentParser) -> None:
    p.add_argument(
        "--root",
        default=None,
        help="The loom checkout. Default: the current directory, else the installed checkout.",
    )
    p.add_argument(
        "--file",
        action="append",
        help="Compose file relative to --root; repeat to stack overrides "
        f"(default {' + '.join(DEFAULT_COMPOSE_FILES)}).",
    )
    p.add_argument(
        "--env-file",
        default=DEFAULT_ENV_FILE,
        help=f"Existing env file used by the stack (default {DEFAULT_ENV_FILE}).",
    )
    p.add_argument(
        "--delete-data",
        action="store_true",
        help="Also delete this stack's named volumes. Irreversible; requires confirmation.",
    )
    p.add_argument(
        "--yes",
        "-y",
        action="store_true",
        help="Confirm --delete-data without the interactive project-name prompt.",
    )


def _missing_file(root: Path, files: tuple[str, ...]) -> Path | None:
    return next((root / f for f in files if not (root / f).is_file()), None)


def _resolve_identity(args: argparse.Namespace, env: dict[str, str]) -> tuple[str, str, str]:
    import getpass

    subject = args.subject
    if not subject:
        try:
            subject = getpass.getuser()
        except Exception:  # noqa: BLE001 — no passwd entry in some containers
            subject = "you"
    org = args.org or env.get("LOOM_SELF_SIGNUP_ORG", "").strip() or "dev"
    namespace = args.namespace or env.get("LOOM_DEFAULT_NAMESPACE", "").strip() or "local"
    return subject, org, namespace


def _cmd_up(args: argparse.Namespace) -> int:
    root = repo_root(args.root)
    if root is None:
        where = args.root or f"{Path.cwd()} or the package's checkout"
        print(
            f"✗ no .env.example under {where} — run `loom up` from the loom clone "
            "(git clone https://github.com/ClickHouse/loom && cd loom), or pass --root.",
            file=sys.stderr,
        )
        return 2
    if shutil.which("docker") is None:
        print(
            "✗ `docker` is not on PATH. Install Docker Desktop / Docker Engine with the "
            "compose plugin (https://docs.docker.com/get-docker/) and re-run `loom up`.",
            file=sys.stderr,
        )
        return 2
    for line in bootstrap(root, args.env_file):
        print(f"✓ {line}")
    env = read_env_file(root / args.env_file)
    mode = stack_mode(env)
    base_url = f"http://localhost:{args.port}"
    health_url = f"{base_url}/v1/health"
    files = tuple(args.file or DEFAULT_COMPOSE_FILES)
    if missing := _missing_file(root, files):
        print(f"✗ compose file not found: {missing}", file=sys.stderr)
        return 2
    # The last file names the containers (the dev override does).
    service, container = compose_names(
        (root / files[-1]).read_text(encoding="utf-8", errors="replace")
    )

    status_code, body = _http_get_json(health_url, timeout=2.0)
    already_up = body is not None and body.get("status") == "ok"
    if already_up:
        print(f"✓ a Loom stack already answers at {health_url}; reconciling configuration.")
    # A healthy container can still have stale environment or Compose settings.  Always
    # reconcile it, but avoid an image build on the ordinary idempotent rerun.
    build = args.rebuild or (not already_up and not args.no_build)
    rc = compose_up(
        root,
        files,
        args.env_file,
        build=build,
        recreate_service=service if already_up else "",
    )
    if rc != 0:
        print(f"✗ docker compose up exited {rc}", file=sys.stderr)
        return rc
    ok, status_code, body = wait_for_health(health_url, timeout_s=args.timeout)
    if not ok:
        _print_health_failure(health_url, args.timeout, status_code, body)
        compose_logs(root, files, args.env_file, service)
        return 1

    subject, org, namespace = _resolve_identity(args, env)
    token = ""
    if mode != "single-user":
        token, err = mint_token_via_container(
            container, subject=subject, org=org, namespace=namespace
        )
        if err:
            print(f"! could not mint a token: {err}", file=sys.stderr)
            print(
                f"  run it by hand once the container is up: docker exec {container} "
                f"loom mint-token --subject {subject} --org {org} --namespaces {namespace} "
                "--roles writer",
                file=sys.stderr,
            )
    build_commit = str((body or {}).get("build_commit") or "unknown")
    print_connect(
        base_url=base_url,
        mode=mode,
        org=org,
        namespace=namespace,
        token=token,
        build_commit=build_commit,
    )
    if args.open:
        import webbrowser

        webbrowser.open(f"{base_url}/")
    return 0 if (mode == "single-user" or token) else 1


def _cmd_down(args: argparse.Namespace) -> int:
    root = repo_root(args.root)
    if root is None:
        where = args.root or f"{Path.cwd()} or the package's checkout"
        print(
            f"✗ no .env.example under {where}; run from the Loom clone or pass --root.",
            file=sys.stderr,
        )
        return 2
    files = tuple(args.file or DEFAULT_COMPOSE_FILES)
    env_path = root / args.env_file
    if missing := _missing_file(root, files):
        print(f"✗ compose file not found: {missing}", file=sys.stderr)
        return 2
    if not env_path.is_file():
        print(
            f"✗ env file not found: {env_path}; refusing to guess which stack to stop.",
            file=sys.stderr,
        )
        return 2
    if shutil.which("docker") is None:
        print("✗ `docker` is not on PATH; install or start Docker and retry.", file=sys.stderr)
        return 2
    project = compose_project_name(root, args.env_file)
    if args.delete_data and not args.yes:
        print(
            f"This permanently deletes the named volumes for Compose project {project!r}.",
            file=sys.stderr,
        )
        try:
            confirmation = input(f"Type {project} to continue: ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nCancelled; the stack and its data were not changed.", file=sys.stderr)
            return 2
        if confirmation != project:
            print("Cancelled; the stack and its data were not changed.", file=sys.stderr)
            return 2
    rc = compose_down(root, files, args.env_file, delete_data=args.delete_data)
    if rc != 0:
        print(f"✗ docker compose down exited {rc}", file=sys.stderr)
        return rc
    if args.delete_data:
        print(f"✓ stopped {project} and deleted its named volumes")
    else:
        print(f"✓ stopped {project}; data volumes were preserved (restart with `loom up`)")
    return 0


_UP_HELP = (
    "Boot the local stack (Loom + ClickHouse) and print how to connect: creates "
    ".env and the ClickHouse config from their templates, runs docker compose up "
    "--build, waits for /v1/health, then prints the dashboard URL and the first "
    "memory.set (curl + Python SDK) and `claude mcp add` snippets — minting a "
    "writer token first when auth is on. Re-run any time."
)


def register_up_cli(sub: Any) -> None:
    p_up = sub.add_parser("up", help=_UP_HELP)
    _add_up_flags(p_up)
    p_up.set_defaults(func=_cmd_up)

    p_down = sub.add_parser(
        "down",
        help=(
            "Stop the local stack while preserving data; --delete-data performs a confirmed reset."
        ),
    )
    _add_down_flags(p_down)
    p_down.set_defaults(func=_cmd_down)
