"""Each setting's description: the comment written above its field.

The configuration reference (`scripts/codegen/settings_reference.py`), the settings
file template and schema, and `/v1/admin/settings.list` all read it from here, so they
say the same thing. A `# --- x ---` heading groups every field below it up to the next
heading; the comment block directly above a heading is that group's note, which the
reference and the template print once under the heading. A field's own comment stands
alone without the note, since the listing and the schema show no headings.
"""

from __future__ import annotations

import ast
import functools
import inspect
import re
import textwrap

HEADING = re.compile(r"^#\s*-{2,}\s*(.*?)\s*-*\s*$")


def comment_block(lines: list[str], lineno: int) -> tuple[str | None, str]:
    """The heading (a `# --- x ---` line) and the comment text directly above `lineno`."""
    out: list[str] = []
    heading = None
    i = lineno - 2
    while i >= 0 and lines[i].strip().startswith("#"):
        text = lines[i].strip()
        m = HEADING.match(text)
        if m:
            heading = m.group(1)
            break
        out.append(text.lstrip("#").strip())
        i -= 1
    return heading, " ".join(reversed(out)).strip()


def heading_note(lines: list[str], lineno: int) -> str:
    """The note of the heading directly above the field at `lineno` (its comment block
    included): the comment block above that heading. '' when the field opens no group."""
    i = lineno - 2
    while i >= 0 and lines[i].strip().startswith("#"):
        if HEADING.match(lines[i].strip()):
            return comment_block(lines, i + 1)[1]
        i -= 1
    return ""


def trailing(line: str) -> str:
    """A comment at the end of the field's last line."""
    m = re.search(r"\s#\s?(.*)$", line)
    if not m or "noqa" in m.group(1) or "type:" in m.group(1):
        return ""
    return m.group(1).strip()


@functools.cache
def descriptions(cls: type) -> dict[str, str]:
    """field -> its comment, for every field the settings class `cls` composes."""
    out: dict[str, str] = {}
    for base in reversed(cls.__mro__):
        if not base.__dict__.get("__annotations__") or base.__module__.startswith("pydantic"):
            continue
        try:
            source = inspect.getsourcelines(base)[0]
        except OSError:  # no source shipped: the listing goes without descriptions
            continue
        lines = textwrap.dedent("".join(source)).splitlines()
        for stmt in ast.parse("\n".join(lines)).body[0].body:  # type: ignore[attr-defined]
            if (
                isinstance(stmt, ast.AnnAssign)
                and isinstance(stmt.target, ast.Name)
                and stmt.target.id in cls.model_fields  # type: ignore[attr-defined]
            ):
                _, text = comment_block(lines, stmt.lineno)
                tail = trailing(lines[(stmt.end_lineno or stmt.lineno) - 1])
                out[stmt.target.id] = " ".join(t for t in (text, tail) if t)
    return out
