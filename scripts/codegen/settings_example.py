"""Generate loom.example.yaml, the settings file template with every setting in it.

Every setting but the secrets, in the catalog's order (one section per area, with its
headings), commented out at its default under the comment that describes it, and marked
`live` (changes while Loom runs, `loom settings set`) or `restart`. Copy the file,
uncomment a line and change its value; the rest keep their defaults.

    python scripts/codegen/settings_example.py          # rewrite loom.example.yaml
    python scripts/codegen/settings_example.py --check  # exit 1 when it is stale
"""

from __future__ import annotations

import ast
import json
import sys
import textwrap
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:  # run as a file
    sys.path.insert(0, str(ROOT))

from scripts.codegen.common import GeneratedFile, generator_main  # noqa: E402
from server.settings import Settings  # noqa: E402
from server.settings._describe import comment_block, heading_note, trailing  # noqa: E402
from server.settings._env import is_secret_field  # noqa: E402
from server.settings._live import is_live  # noqa: E402

COMMAND = "python scripts/codegen/settings_example.py"
CATALOG = ROOT / "server" / "settings" / "catalog.py"
WIDTH = 88

HEADER = f"""\
# yaml-language-server: $schema=schemas/loom-settings.schema.json
#
# Loom settings file: every setting, at its default. Generated from
# server/settings/catalog.py by
# `{COMMAND}`; do not edit this copy.
#
# Each key is a setting's LOOM_<NAME> environment variable in lowercase: set any line
# here as LOOM_<NAME> in the environment or in .env, which win over this file. A
# deployment that mounts a config file can instead copy this file (for example to
# loom.yaml), point LOOM_SETTINGS_FILE at it, and uncomment the lines it changes;
# every other setting keeps its default.
#
# A setting marked `live` also changes while Loom runs, deployment-wide and without a
# restart: `loom settings set <name> <value>` or the dashboard's Deployment settings
# page. One marked `restart` is read when a process starts.
#
# Features switch on and off with the settings ending in `_enabled`: search this file
# for "_enabled:". Secrets (passwords, keys, tokens) are not here: they go in the
# environment, .env or a secrets manager (.env.example lists them).
#
# A secret, a key given twice, an unsupported version, or a value of the wrong type
# or outside its bounds stops the server at startup, naming the key. A name no
# setting has is logged and ignored.
"""


def _value(name: str) -> str:
    """The default as YAML reads it back; a string holding JSON is single-quoted, so it
    edits as the JSON it is rather than as escaped quotes."""
    info = Settings.model_fields[name]
    value: Any = info.default_factory() if info.default_factory is not None else info.default  # type: ignore[call-arg]
    if isinstance(value, str) and '"' in value and "'" not in value:
        return f"'{value}'"
    return json.dumps(value, ensure_ascii=False)


def _comment(text: str, indent: str) -> list[str]:
    return [
        f"{indent}# {line}".rstrip()
        for line in textwrap.wrap(text, WIDTH - len(indent) - 2, break_on_hyphens=False)
    ]


def _section(cls: ast.ClassDef, lines: list[str]) -> list[str]:
    fields = [
        s
        for s in cls.body
        if isinstance(s, ast.AnnAssign)
        and isinstance(s.target, ast.Name)
        and s.target.id in Settings.model_fields
        and not is_secret_field(s.target.id)
    ]
    if not fields:
        return []
    title = (ast.get_docstring(cls) or cls.name).split("\n\n")[0].replace("\n", " ")
    out = ["", f"  # ===== {title.rstrip('.')} ====="]
    current = None
    for stmt in fields:
        field = stmt.target.id  # type: ignore[union-attr]
        heading, text = comment_block(lines, stmt.lineno)
        if heading and heading != current:
            current = heading
            out += ["", f"  # --- {heading} ---"]
            note = heading_note(lines, stmt.lineno)
            if note:
                out += _comment(note, "  ")
        tail = trailing(lines[(stmt.end_lineno or stmt.lineno) - 1])
        about = " ".join(t for t in (text, tail) if t)
        mode = "live" if is_live(Settings.model_fields[field]) else "restart"
        out.append("")
        out += _comment(f"{about} [{mode}]" if about else f"[{mode}]", "  ")
        out.append(f"  # {field}: {_value(field)}")
    return out


# What a new deployment sets first leads the file; the rest follow in catalog order.
FIRST = ("ClickHouseSettings", "ModelSettings", "ProviderSettings", "AuthSettings")


def render() -> str:
    src = CATALOG.read_text(encoding="utf-8")
    lines = src.splitlines()
    body = [HEADER.rstrip(), "version: 1", "settings:"]
    classes = [n for n in ast.parse(src).body if isinstance(n, ast.ClassDef)]
    order = sorted(classes, key=lambda c: FIRST.index(c.name) if c.name in FIRST else len(FIRST))
    for cls in order:
        body += _section(cls, lines)
    return "\n".join(body).rstrip() + "\n"


TARGET = GeneratedFile(ROOT / "loom.example.yaml", COMMAND)


def main(argv: list[str] | None = None) -> int:
    return generator_main(__doc__, TARGET, render, argv)


if __name__ == "__main__":
    raise SystemExit(main())
