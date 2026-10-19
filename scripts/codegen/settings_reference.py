"""Generate docs/reference/configuration.md from the settings mixins.

Each field's row carries its env var, type, default and the comment block written above
it (plus any trailing comment), so the reference is exactly as current as the code.

    python scripts/codegen/settings_reference.py          # rewrite docs/reference/configuration.md
    python scripts/codegen/settings_reference.py --check  # exit 1 when it is stale
"""

from __future__ import annotations

import ast
import re
import sys
from pathlib import Path
from typing import Literal, get_args, get_origin

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:  # run as a file
    sys.path.insert(0, str(ROOT))

from scripts.codegen.common import GeneratedFile, generator_main  # noqa: E402
from server.settings import Settings  # noqa: E402
from server.settings._env import is_secret_field  # noqa: E402

COMMAND = "python scripts/codegen/settings_reference.py"
SETTINGS_DIR = ROOT / "server" / "settings"
HEADING = re.compile(r"^#\s*-{2,}\s*(.*?)\s*-*\s*$")

HEADER = f"""# Configuration reference

Every setting is an environment variable `LOOM_<FIELD>` read once at startup into
`server.settings.settings`. Set them in `.env` or the container environment;
`.env.example` lists the common ones. `LOOM_ENV_FILE` names a different dotenv file,
and empty reads none.

This page is generated from the comments above each field in `server/settings/`
by `{COMMAND}`; edit the comment, not this file.
"""


def _comment_block(lines: list[str], lineno: int) -> tuple[str | None, str]:
    """The heading (a `# --- x ---` line) and the comment text directly above `lineno`,
    up to an empty `#` line: what a section's first field has above that is the
    section's lead-in, not its meaning."""
    out: list[str] = []
    heading = None
    own = True
    i = lineno - 2
    while i >= 0 and lines[i].strip().startswith("#"):
        text = lines[i].strip()
        m = HEADING.match(text)
        if m:
            heading = m.group(1)
            break
        if text == "#":
            own = False
        elif own:
            out.append(text.lstrip("#").strip())
        i -= 1
    return heading, " ".join(reversed(out)).strip()


def _trailing(line: str) -> str:
    m = re.search(r"\s#\s?(.*)$", line)
    if not m or "noqa" in m.group(1) or "type:" in m.group(1):
        return ""
    return m.group(1).strip()


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
    return ["| Variable | Type | Default | Meaning |", "| --- | --- | --- | --- |", *rows]


def _row(name: str, meaning: str) -> str:
    return (
        f"| `{_env_name(name)}` | `{_cell(_type_name(name))}` | "
        f"{_cell(_default(name))} | {_cell(meaning)} |"
    )


def _section(path: Path, tree: ast.Module, cls: ast.ClassDef, lines: list[str]) -> list[str]:
    fields = [
        s
        for s in cls.body
        if isinstance(s, ast.AnnAssign)
        and isinstance(s.target, ast.Name)
        and s.target.id in Settings.model_fields
    ]
    if not fields:
        return []
    doc = ast.get_docstring(cls) or ast.get_docstring(tree) or ""
    title = doc.split("\n\n")[0].replace("\n", " ")
    out = [f"\n## {cls.name.removesuffix('Settings') or cls.name}\n"]
    if title:
        out.append(f"{title}\n")
    out.append(f"Source: `server/settings/{path.name}`\n")
    current, rows = None, []
    for stmt in fields:
        name = stmt.target.id  # type: ignore[union-attr]
        heading, text = _comment_block(lines, stmt.lineno)
        if heading and heading != current:
            out += _table(rows)
            rows, current = [], heading
            out.append(f"\n### {heading[:1].upper()}{heading[1:]}\n")
        tail = _trailing(lines[(stmt.end_lineno or stmt.lineno) - 1])
        rows.append(_row(name, " ".join(t for t in (text, tail) if t)))
    return out + _table(rows)


def render() -> str:
    parts = [HEADER]
    for path in sorted(SETTINGS_DIR.glob("_*.py")):
        src = path.read_text(encoding="utf-8")
        tree = ast.parse(src)
        for cls in (n for n in tree.body if isinstance(n, ast.ClassDef)):
            parts += _section(path, tree, cls, src.splitlines())
    return "\n".join(parts).rstrip() + "\n"


TARGET = GeneratedFile(ROOT / "docs" / "reference" / "configuration.md", COMMAND)


def main(argv: list[str] | None = None) -> int:
    return generator_main(__doc__, TARGET, render, argv)


if __name__ == "__main__":
    raise SystemExit(main())
