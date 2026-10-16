"""Every variable `.env.example` names is one the code reads.

The template lists what a deployer sets; a name nothing reads any more is a
dead line that tells an operator a knob exists when it does not. A variable
passes when it is a `Settings` field (`LOOM_<FIELD>`) or when a module under
`server/` or `sdk/` names it as a string literal (the environment read, or the
constant an environment read goes through). Commented examples
(`# LOOM_X=...`) count: they are suggestions a deployer will uncomment.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
ENV_EXAMPLE = ROOT / ".env.example"

_VAR_LINE = re.compile(r"^#?\s*([A-Z][A-Z0-9_]*)=", re.M)
# `os.environ.get("X")`, `os.getenv("X")`, `_X_ENV = "X"`.
_ENV_NAME_LITERAL = re.compile(r"""["']([A-Z][A-Z0-9]*_[A-Z0-9_]+)["']""")


def example_vars() -> set[str]:
    return set(_VAR_LINE.findall(ENV_EXAMPLE.read_text(encoding="utf-8")))


def settings_env_names() -> set[str]:
    from server.settings import Settings

    prefix = Settings.model_config.get("env_prefix", "LOOM_")
    return {f"{prefix}{name.upper()}" for name in Settings.model_fields}


def env_name_literals() -> set[str]:
    names: set[str] = set()
    for base in ("server", "sdk"):
        for path in (ROOT / base).rglob("*.py"):
            names.update(_ENV_NAME_LITERAL.findall(path.read_text(encoding="utf-8")))
    return names


def test_env_example_names_variables() -> None:
    assert len(example_vars()) > 10


# Read by Docker Compose itself, not by Loom.
COMPOSE_VARS = {"COMPOSE_PROFILES"}


def test_every_env_example_variable_is_read() -> None:
    dead = example_vars() - settings_env_names() - env_name_literals() - COMPOSE_VARS
    assert not dead, f".env.example names variables nothing reads: {sorted(dead)}"


def test_active_lines_carry_no_inline_comment() -> None:
    """Compose, python-dotenv and the stock-env test parse `KEY=value # note`
    differently; keep notes on their own line."""
    offenders = [
        line
        for line in ENV_EXAMPLE.read_text(encoding="utf-8").splitlines()
        if re.match(r"^[A-Z][A-Z0-9_]*=", line) and re.search(r"\s#", line)
    ]
    assert not offenders, offenders
