"""Environment-variable reads as facets.

`process.env.DATABASE_URL`, `import.meta.env.VITE_API`, `Deno.env.get("X")`,
`Bun.env.X`, a destructuring `const { A, B } = process.env`, and in Python
`os.environ["X"]`, `os.environ.get("X")`, `os.getenv("X")` (also the bare
`environ` / `getenv` after `from os import ...`). Each read is an `env_var`
facet named by the variable at the line of the read, so `code.tests_for`
answers "where is DATABASE_URL read" with file + line in one indexed read.

Pure: regexes over the source. A facet is never worth a lost file, so the
caller wraps this fail-open.
"""

from __future__ import annotations

import re

from server.code.domain.extract.lang_sql_model import Facet
from server.code.domain.extract.text_lines import line_at, newline_offsets
from server.code.domain.extract.treesitter import ParsedFile

FACET_KIND = "env_var"

_NAME = r"([A-Za-z_][A-Za-z0-9_]*)"
_STR = r"""['"`]""" + _NAME + r"""['"`]"""

_JS_RES = (
    re.compile(r"\bprocess\.env\." + _NAME),
    re.compile(r"\bprocess\.env\[\s*" + _STR + r"\s*\]"),
    re.compile(r"\bimport\.meta\.env\." + _NAME),
    re.compile(r"\bDeno\.env\.get\(\s*" + _STR),
    re.compile(r"\bBun\.env\." + _NAME),
)
_JS_DESTRUCTURE_RE = re.compile(r"\{([^{}]*)\}\s*=\s*process\.env\b")
_PY_RES = (
    re.compile(r"\b(?:os\.)?environ\[\s*" + _STR + r"\s*\]"),
    re.compile(r"\b(?:os\.)?environ\.get\(\s*" + _STR),
    re.compile(r"\b(?:os\.)?getenv\(\s*" + _STR),
)
_LOWER_RE = re.compile(r"^[a-z]")

JS_LANGS = frozenset({"typescript", "tsx", "javascript", "jsx"})


def env_facets(pf: ParsedFile) -> list[Facet]:
    """One `env_var` facet per (variable, line) read in the file."""
    if pf.lang in JS_LANGS:
        return _collect(pf.source.decode("utf-8", "replace"), _JS_RES, destructure=True)
    if pf.lang == "python":
        return _collect(pf.source.decode("utf-8", "replace"), _PY_RES, destructure=False)
    return []


def _collect(text: str, regexes: tuple[re.Pattern[str], ...], *, destructure: bool) -> list[Facet]:
    seen: set[tuple[str, int]] = set()
    newlines = newline_offsets(text)

    def add(name: str, line: int) -> None:
        # `process.env.npm_config_x` style lower-case reads are tool internals
        if name and not _LOWER_RE.match(name):
            seen.add((name, line))

    for rx in regexes:
        for m in rx.finditer(text):
            add(m.group(1), line_at(newlines, m.start()))
    if destructure:
        for m in _JS_DESTRUCTURE_RE.finditer(text):
            line = line_at(newlines, m.start())
            for part in m.group(1).split(","):
                name = part.split(":")[0].split("=")[0].strip()
                if re.fullmatch(_NAME, name):
                    add(name, line)
    return [
        Facet(kind=FACET_KIND, name=name, ordinal=i, line=line, confidence=1.0)
        for i, (line, name) in enumerate(sorted((ln, nm) for nm, ln in seen), 1)
    ]
