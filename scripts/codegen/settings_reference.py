"""Generate docs/reference/configuration.md from server/settings/catalog.py.

Each field's row carries its env var, type, default and the comment block written above
it (plus any trailing comment), so the reference is exactly as current as the code.

    python scripts/codegen/settings_reference.py          # rewrite docs/reference/configuration.md
    python scripts/codegen/settings_reference.py --check  # exit 1 when it is stale
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path
from typing import Literal, get_args, get_origin

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:  # run as a file
    sys.path.insert(0, str(ROOT))

from scripts.codegen.common import GeneratedFile, generator_main  # noqa: E402
from server.settings import Settings  # noqa: E402
from server.settings._describe import comment_block, heading_note, trailing  # noqa: E402
from server.settings._env import is_secret_field  # noqa: E402
from server.settings._history import DEFAULT_CHANGES  # noqa: E402
from server.settings._live import is_live  # noqa: E402

COMMAND = "python scripts/codegen/settings_reference.py"
CATALOG = ROOT / "server" / "settings" / "catalog.py"

HEADER = f"""# Configuration reference

Every setting is declared in one file, `server/settings/catalog.py`, and is an
environment variable `LOOM_<FIELD>` read at startup into
`server.settings.settings`. Set them in `.env` or the container environment;
`.env.example` lists the common ones. `LOOM_ENV_FILE` names a different dotenv file,
and empty reads none.

The same settings, named in lowercase, can go in a YAML settings file, read when
`LOOM_SETTINGS_FILE` names it.

```yaml
# yaml-language-server: $schema=schemas/loom-settings.schema.json
version: 1
settings:
  memory_search_default_mode: rrf
```

A value comes from the first of: the environment, the dotenv file, the settings file,
the default. A secret (keep those in the environment or a secrets manager), a key
given twice, a `version` this build does not read or a value of the wrong type or
outside its bounds (also one the environment or the dotenv file overrides) stops the
server at startup; a name no setting has is logged and ignored, since builds N-1 and N
share the file during a rolling upgrade. `schemas/loom-settings.schema.json` is the
file's JSON Schema (`python scripts/codegen/settings_schema.py`), and
`loom.example.yaml` the template with every setting at its default.

A setting marked **live** also takes a deployment-wide override while Loom runs, which
wins over all of these: `loom settings set <field> <value>` or
`POST /v1/admin/settings.set`. Every process applies it within
`LOOM_RUNTIME_OVERRIDES_REFRESH_SECONDS`, and it outlives a restart until
`loom settings reset <field>`. `loom settings list` shows each setting's value in force
and where it came from ([runtime settings](../operations/runtime-settings.md)).

This page is generated from the comments above each field in `server/settings/`
by `{COMMAND}`; edit the comment, not this file.
"""


def _env_name(field: str) -> str:
    return f"LOOM_{field.upper()}"


def _type_name(field: str) -> str:
    ann = Settings.model_fields[field].annotation
    if get_origin(ann) is Literal:
        return " | ".join(repr(a) for a in get_args(ann))
    text = str(ann) if get_args(ann) else getattr(ann, "__name__", str(ann))
    return text.replace("typing.", "")


def _default(field: str) -> str:
    info = Settings.model_fields[field]
    if is_secret_field(field):
        return "*(unset)*" if not info.default else "*(set)*"
    value = info.default
    if info.default_factory is not None:
        value = info.default_factory()  # type: ignore[call-arg]
    text = repr(value) if isinstance(value, str) else str(value)
    return f"`{text}`" if len(text) <= 60 else f"`{text[:57]}…`"


def _cell(text: str) -> str:
    return text.replace("|", "\\|").replace("\n", " ")


def _table(rows: list[str]) -> list[str]:
    if not rows:
        return []
    return [
        "| Variable | Type | Default | Live | Meaning |",
        "| --- | --- | --- | --- | --- |",
        *rows,
    ]


def _row(name: str, meaning: str) -> str:
    live = "live" if is_live(Settings.model_fields[name]) else ""
    return (
        f"| `{_env_name(name)}` | `{_cell(_type_name(name))}` | "
        f"{_cell(_default(name))} | {live} | {_cell(meaning)} |"
    )


def _section(cls: ast.ClassDef, lines: list[str]) -> list[str]:
    fields = [
        s
        for s in cls.body
        if isinstance(s, ast.AnnAssign)
        and isinstance(s.target, ast.Name)
        and s.target.id in Settings.model_fields
    ]
    if not fields:
        return []
    doc = ast.get_docstring(cls) or ""
    title = doc.split("\n\n")[0].replace("\n", " ")
    out = [f"\n## {cls.name.removesuffix('Settings') or cls.name}\n"]
    if title:
        out.append(f"{title}\n")
    current, rows = None, []
    for stmt in fields:
        name = stmt.target.id  # type: ignore[union-attr]
        heading, text = comment_block(lines, stmt.lineno)
        if heading and heading != current:
            out += _table(rows)
            rows, current = [], heading
            out.append(f"\n### {heading[:1].upper()}{heading[1:]}\n")
            note = heading_note(lines, stmt.lineno)
            if note:
                out.append(f"{note}\n")
        tail = trailing(lines[(stmt.end_lineno or stmt.lineno) - 1])
        rows.append(_row(name, " ".join(t for t in (text, tail) if t)))
    return out + _table(rows)


def _default_changes() -> list[str]:
    out = [
        "\n## Default changes\n",
        "Every change to a setting's default, from `server/settings/_history.py`. A "
        "deployment that never set the variable changes with each one.\n",
    ]
    if not DEFAULT_CHANGES:
        return [*out, "No default has changed since the history began.\n"]
    rows = [
        f"| {c.version} | `{_env_name(c.name)}` | `{c.old!r}` | `{c.new!r}` | {_cell(c.why)} |"
        for c in reversed(DEFAULT_CHANGES)
    ]
    return [
        *out,
        "| Release | Variable | Was | Is | Why |",
        "| --- | --- | --- | --- | --- |",
        *rows,
    ]


def render() -> str:
    parts = [HEADER, *_default_changes()]
    src = CATALOG.read_text(encoding="utf-8")
    tree = ast.parse(src)
    for cls in (n for n in tree.body if isinstance(n, ast.ClassDef)):
        parts += _section(cls, src.splitlines())
    return "\n".join(parts).rstrip() + "\n"


TARGET = GeneratedFile(ROOT / "docs" / "reference" / "configuration.md", COMMAND)


def main(argv: list[str] | None = None) -> int:
    return generator_main(__doc__, TARGET, render, argv)


if __name__ == "__main__":
    raise SystemExit(main())
