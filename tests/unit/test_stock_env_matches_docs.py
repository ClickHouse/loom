"""The stock `.env.example` boots the stack the quickstart describes.

`make dev` copies `.env.example` to `.env` and runs the dev compose file with
`--env-file .env`, so the values in the template — not the compose file's
`${VAR:-default}` fallbacks — decide the first run, and the quickstart's promises
(the "Sign up" tab, "set OPENAI_API_KEY and real providers boot
automatically") must hold for the stock file.

Pure Python, no docker: parse the template, load the compose file the
Makefile's `dev` target names, resolve the `loom` service's environment the
way compose does (env_file first, `environment:` wins), and pin the result to
the constants the quickstart text states. Also: every `LOOM_*` variable the
quickstart names must be a real Settings field (or an SDK-side credential),
and every dev-compose command in the onboarding docs must carry
`--env-file .env`.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
ENV_EXAMPLE = ROOT / ".env.example"
MAKEFILE = ROOT / "Makefile"
QUICKSTART = ROOT / "docs" / "getting-started" / "quickstart.md"

# What docs/getting-started/quickstart.md §1 ("What the stock `.env` boots") and §2 say the
# stock stack is. Change these together with that table, README §Quickstart,
# docs/operations/deployment.md §Local stack and the STOCK DEV DEFAULT comments in
# .env.example — they are one story.
EXPECTED_CONTAINER_ENV = {
    "LOOM_AUTH_REQUIRED": "false",
    "LOOM_IDENTITY_PROVIDER": "",
    "LOOM_LLM_PROVIDER": "fake",
    "LOOM_LLM_MODEL": "fake-echo-1",
    "LOOM_EMBEDDING_MODEL": "fake-ngram-3072",
    "LOOM_EMBEDDING_DIM": "3072",
    # Pinned in the compose file's `environment:` (wins over .env), which is
    # why the quickstart's one-line flip to sign-up is LOOM_AUTH_REQUIRED alone.
    "LOOM_SELF_SIGNUP": "open",
}
EXPECTED_IDENTITY_MODE = "single-user"
EXPECTED_AUTH_ACTUALLY_REQUIRED = False

# Strings the quickstart must literally contain so the prose cannot drift
# from the constants above without failing here.
QUICKSTART_MUST_MENTION = (
    "LOOM_AUTH_REQUIRED=false",
    "`single-user`",
    "LOOM_LLM_PROVIDER=fake",
    "LOOM_EMBEDDING_MODEL=fake-ngram-3072",
    "LOOM_EMBEDDING_DIM=3072",
    "--env-file .env",
    'docker compose --project-directory "$HOME/.loom" exec loom loom mint-token',
)

# `LOOM_*` names the quickstart may use that are not server Settings fields.
# The SDK reads its credential from LOOM_API_KEY; Compose consumes
# the release-bundle image pins before the server
# environment is built; the installer consumes its offline bundle paths
# and LOOM_SKIP_START to prepare files without starting the stack.
NON_SETTINGS_VARS = {
    "LOOM_API_KEY",
    "LOOM_CLICKHOUSE_IMAGE",
    "LOOM_EXTRACTOR_IMAGE_REPOSITORY",
    "LOOM_IMAGE_REPOSITORY",
    "LOOM_INSTALL_BUNDLE",
    "LOOM_INSTALL_CHECKSUM",
    "LOOM_PORT",
    "LOOM_SKIP_START",
    "LOOM_VERSION",
}

# Docs whose dev-compose commands must match what `make dev` runs.
DOCS_WITH_COMPOSE_COMMANDS = (
    ROOT / "README.md",
    ROOT / "CONTRIBUTING.md",
    ROOT / "docs" / "getting-started" / "quickstart.md",
    ROOT / "docs" / "operations" / "deployment.md",
)

_INTERP = re.compile(
    r"\$\{(?P<name>[A-Za-z_][A-Za-z0-9_]*)(?:(?P<op>:?-)(?P<default>[^}]*))?\}"
    r"|\$(?P<bare>[A-Za-z_][A-Za-z0-9_]*)"
)


def parse_env_file(path: Path) -> dict[str, str]:
    """KEY=VALUE lines; comments and blanks ignored; surrounding quotes stripped."""
    out: dict[str, str] = {}
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if key.startswith("export "):
            key = key[len("export ") :].strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        out[key] = value
    return out


def interpolate(template: str, env: dict[str, str]) -> str:
    """Compose-style `${VAR}`, `${VAR:-default}`, `${VAR-default}`, `$VAR`."""

    def sub(m: re.Match[str]) -> str:
        name = m.group("name") or m.group("bare")
        op = m.group("op")
        default = m.group("default") or ""
        present = name in env
        value = env.get(name, "")
        if op == ":-":
            return value if (present and value != "") else default
        if op == "-":
            return value if present else default
        return value

    return _INTERP.sub(sub, template)


def dev_compose_files_from_makefile() -> tuple[list[Path], str]:
    """The compose files the `dev:` target stacks, and the command line itself."""
    text = MAKEFILE.read_text()
    m = re.search(r"^dev:.*?\n((?:\t.*\n)+)", text, re.M)
    assert m, "Makefile has no `dev:` target"
    recipe = m.group(1)
    cmd = re.search(r"docker compose (?:.*?-f \S+)+[^\n]* up[^\n]*", recipe)
    assert cmd, f"`dev:` recipe has no `docker compose … -f <file> up`:\n{recipe}"
    return [ROOT / f for f in re.findall(r"-f (\S+)", cmd.group(0))], cmd.group(0)


def resolved_loom_service_env() -> dict[str, str]:
    """The `loom` service's environment over every stacked file, later files
    winning, relative paths resolved from the first file's directory."""
    paths, _ = dev_compose_files_from_makefile()
    dotenv = parse_env_file(ENV_EXAMPLE)
    env_files: list[str] = []
    environment: dict[str, str] = {}
    for path in paths:
        service = yaml.safe_load(path.read_text())["services"]["loom"]
        for entry in service.get("env_file", []) or []:
            env_files.append(entry["path"] if isinstance(entry, dict) else entry)
        env = service.get("environment", {}) or {}
        if isinstance(env, list):
            env = dict(item.split("=", 1) for item in env)
        environment.update(env)

    # 1. env_file entries, in order.
    container: dict[str, str] = {}
    for rel in env_files:
        target = (paths[0].parent / rel).resolve()
        # `make bootstrap` copies .env.example to .env; the test reads the
        # template in its place.
        if target.name == ".env":
            target = ENV_EXAMPLE
        container.update(parse_env_file(target))

    # 2. `environment:` wins; its values are interpolated against `--env-file`.
    for key, value in environment.items():
        container[key] = interpolate("" if value is None else str(value), dotenv)
    return container


def test_make_dev_passes_env_file() -> None:
    _, cmd = dev_compose_files_from_makefile()
    assert "--env-file .env" in cmd, cmd


def test_stock_env_resolves_to_the_documented_stack() -> None:
    env = resolved_loom_service_env()
    got = {k: env.get(k) for k in EXPECTED_CONTAINER_ENV}
    assert got == EXPECTED_CONTAINER_ENV


def test_stock_env_infers_single_user_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    """The server's own inference, not a re-implementation of it."""
    from server.settings import Settings

    env = resolved_loom_service_env()
    for key in ("LOOM_AUTH_REQUIRED", "LOOM_IDENTITY_PROVIDER", "LOOM_SELF_SIGNUP"):
        monkeypatch.setenv(key, env[key])
    settings = Settings(_env_file=None)
    assert settings.effective_identity_provider() == EXPECTED_IDENTITY_MODE
    assert settings.auth_actually_required() is EXPECTED_AUTH_ACTUALLY_REQUIRED


def test_env_example_pins_the_provider_lines_uncommented() -> None:
    """Commenting these out would flip a keyless `make dev` to the compose
    file's OpenAI fallbacks: ERROR logs, fake fallback at dim 1024 against a
    schema built for 1536."""
    dotenv = parse_env_file(ENV_EXAMPLE)
    for key in (
        "LOOM_LLM_PROVIDER",
        "LOOM_LLM_MODEL",
        "LOOM_EMBEDDING_MODEL",
        "LOOM_EMBEDDING_DIM",
    ):
        assert dotenv.get(key) == EXPECTED_CONTAINER_ENV[key], key


def test_quickstart_states_the_stock_values() -> None:
    text = QUICKSTART.read_text()
    missing = [s for s in QUICKSTART_MUST_MENTION if s not in text]
    assert not missing, missing


def test_quickstart_names_only_live_settings() -> None:
    from server.settings import Settings

    prefix = Settings.model_config.get("env_prefix", "LOOM_")
    fields = {f"{prefix}{name.upper()}" for name in Settings.model_fields}
    mentioned = set(re.findall(r"\bLOOM_[A-Z0-9_]+\b", QUICKSTART.read_text()))
    dead = mentioned - fields - NON_SETTINGS_VARS
    assert not dead, f"quickstart names settings that do not exist: {sorted(dead)}"


def test_sdk_side_vars_are_read_by_the_sdk() -> None:
    agent_py = (ROOT / "sdk" / "python" / "loom" / "agent.py").read_text()
    for var in {"LOOM_API_KEY"}:
        assert var in agent_py, var


def test_docs_dev_compose_commands_carry_env_file() -> None:
    paths, _ = dev_compose_files_from_makefile()
    rel = paths[-1].relative_to(ROOT).as_posix()
    offenders: list[str] = []
    for doc in DOCS_WITH_COMPOSE_COMMANDS:
        for n, line in enumerate(doc.read_text().splitlines(), 1):
            if f"-f {rel}" in line and " up" in line and "--env-file .env" not in line:
                offenders.append(f"{doc.relative_to(ROOT)}:{n}: {line.strip()}")
    assert not offenders, "\n".join(offenders)
