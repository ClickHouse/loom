"""Jinja-templated SQL — dbt models and macros, Airflow templates, ClickHouse `.sql.j2`
tests.

A templated SQL file is SQL with holes. The runner fills them (`dbt compile`, Airflow's
`render_template`, `clickhouse-test`'s Jinja pass) and runs the result; the plane reads
the source. Read as plain SQL text, `{% for algo in [...] %}` becomes a statement of
kind `FOR`, `{% endfor %}` one of kind `ENDFOR` (610 such records in 185 ClickHouse
tests), and `{{ ref('orders') }}` — dbt's whole dependency graph — is two braces in a
SELECT.

This module:

  * **neutralizes** the template LINE-PRESERVINGLY before the splitter
    runs: `{# … #}` comments and `{% … %}` statements become blank (their
    newlines kept), `{{ … }}` expressions become the placeholder
    `__jinja__` — a name no facet keeps — so the statements, their line
    spans and the chunks (cut from the ORIGINAL text by line) stay right;
  * **reads dbt's own grammar** from the original text: `{{ ref('m') }}` /
    `ref('pkg', 'm')` (a read of model `m`), `{{ source('s', 't') }}` (a
    read of source table `t`, recorded as `source: s.t`), `{{ config(
    materialized='table', tags=[…]) }}` (facets), `{% macro name(args) %}`
    / `{% test name(…) %}` / `{% snapshot name %}` (symbols), and
    `{{ pkg.macro(…) }}` / `{{ macro(…) }}` calls (edges to macros);
  * **names the model**: a `.sql` file under `models/`, `snapshots/` or
    `analyses/` that carries dbt cues is one `model` symbol, its filename
    stem, spanning the file — the name `ref()` resolves to.

Edges ride the SQL edge stage: model → ref'd model, model / macro → called macro, bound
repository-unique by bare name through the name table (kinds `model` and `macro`;
SQL-only, the SQL name gate). Outside the extractor stamp with `lang_sql.py`.
"""

from __future__ import annotations

import bisect
import itertools
import posixpath
import re
from dataclasses import dataclass, field

from server.code.domain.extract.edges_treesitter import Edge
from server.code.domain.extract.lang_sql_model import JINJA_PLACEHOLDER, Facet
from server.code.domain.extract.treesitter import Symbol, file_symbol_id

PLACEHOLDER = JINJA_PLACEHOLDER
KIND_MODEL = "model"
KIND_MACRO = "macro"
FACET_SOURCE = "source"
FACET_MATERIALIZED = "materialized"
FACET_TAG = "tag"
FACET_TABLE_READ = "table_read"
FACET_MACRO_CALL = "macro_call"

# An unclosed comment, statement or raw block runs to the end, as Jinja's lexer reads it,
# instead of being searched on to the end again from every later opener.
_COMMENT_RE = re.compile(r"\{#.*?(?:#\}|\Z)", re.S)
_STATEMENT_RE = re.compile(r"\{%-?.*?(?:-?%\}|\Z)", re.S)
_EXPR_RE = re.compile(r"\{\{-?(?:(?!\{\{).)*?-?\}\}", re.S)  # stops at the next `{{`
_RAW_RE = re.compile(r"\{%-?\s*+raw\s*+-?%\}(.*?)(?:\{%-?\s*endraw\s*-?%\}|\Z)", re.S)

_REF_RE = re.compile(r"""\bref\(\s*(?:['"]([^'"]+)['"]\s*,\s*)?['"]([^'"]+)['"]""")
_SOURCE_RE = re.compile(r"""\bsource\(\s*['"]([^'"]+)['"]\s*,\s*['"]([^'"]+)['"]""")
_CONFIG_RE = re.compile(r"\{\{-?\s*config\s*\(((?:(?!\{\{).)*?)\)\s*-?\}\}", re.S)
_MATERIALIZED_RE = re.compile(r"""materialized\s*=\s*['"]([\w-]+)['"]""")
_TAGS_RE = re.compile(r"""tags\s*=\s*(\[[^\]]*\]|['"][^'"]+['"])""")
_MACRO_DEF_RE = re.compile(
    r"\{%-?\s*(?P<word>macro|test|snapshot|materialization)\s+(?P<name>[A-Za-z_]\w*)"
    r"(?P<args>\s*\((?:(?!\{%)[^)])*\))?",
    re.S,
)
_MACRO_END_RE = {
    "macro": re.compile(r"\{%-?\s*endmacro\s*-?%\}"),
    "test": re.compile(r"\{%-?\s*endtest\s*-?%\}"),
    "snapshot": re.compile(r"\{%-?\s*endsnapshot\s*-?%\}"),
    "materialization": re.compile(r"\{%-?\s*endmaterialization\s*-?%\}"),
}
# `{{ dbt_utils.star(...) }}`, `{{ my_macro(1) }}`, `{% do log(...) %}`; not the
# Jinja / dbt builtins a template uses for control
_CALL_RE = re.compile(r"(?<![\w.])(?:(?P<pkg>[A-Za-z_]\w*)\.)?(?P<name>[A-Za-z_]\w*)\s*\(")
_JINJA_BUILTINS = frozenset(
    {
        "ref",
        "source",
        "config",
        "var",
        "env_var",
        "this",
        "return",
        "log",
        "range",
        "dict",
        "list",
        "set",
        "namespace",
        "loop",
        "lipsum",
        "cycler",
        "joiner",
        "adapter",
        "dispatch",
        "exceptions",
        "raise_compiler_error",
        "warn",
        "modules",
        "run_query",
        "execute",
        "load_result",
        "statement",
        "target",
        "flags",
        "graph",
        "tojson",
        "fromjson",
        "toyaml",
        "fromyaml",
        "zip",
        "print",
        "dbt_version",
        "invocation_id",
        "builtins",
        "context",
        "api",
        "is_incremental",
        "should_full_refresh",
        "schema",
        "database",
        "identifier",
        "quote",
        "length",
        "upper",
        "lower",
        "replace",
        "join",
        "trim",
        "int",
        "float",
        "string",
        "bool",
        "tuple",
        "type",
        "default",
        "first",
        "last",
        "map",
        "select",
        "reject",
        "selectattr",
        "rejectattr",
        "sort",
        "unique",
        "batch",
        "slice",
        "round",
        "abs",
        "min",
        "max",
        "sum",
        "count",
        "items",
        "keys",
        "values",
        "get",
        "append",
        "extend",
        "pop",
        "update",
        "format",
        "startswith",
        "endswith",
        "split",
        "strip",
        "lstrip",
        "rstrip",
        "title",
        "capitalize",
        "run_started_at",
        "column",
        "columns",
        "relation",
        "date_trunc",
        "dateadd",
        "datediff",
        "concat",
        "hash",
        "cast",
        "safe_cast",
        "any_value",
        "listagg",
        "escape_single_quotes",
        "current_timestamp",
        "type_string",
        "type_int",
        "type_numeric",
        "type_timestamp",
        "type_float",
        "type_bigint",
        "type_boolean",
        "except",
        "intersect",
        "position",
        "right",
        "left",
        "split_part",
        "string_literal",
        "bool_or",
        "last_day",
        "array_construct",
        "array_append",
        "array_concat",
        "cast_bool_to_text",
        "row_number",
        "not",
        "and",
        "or",
        "in",
        "if",
        "else",
        "elif",
        "endif",
        "for",
        "endfor",
        "do",
        "with",
        "endwith",
        "call",
        "endcall",
        "filter",
        "endfilter",
        "block",
        "endblock",
        "extends",
        "include",
        "import",
        "from",
        "as",
        "macro",
        "endmacro",
        "test",
        "endtest",
        "snapshot",
        "endsnapshot",
        "materialization",
        "endmaterialization",
        "raw",
        "endraw",
        "autoescape",
        "endautoescape",
    }
)
_DBT_DIRS = ("models", "snapshots", "analyses", "analysis", "macros", "tests", "seeds")
_MODEL_DIRS = ("models", "snapshots", "analyses", "analysis")


@dataclass
class JinjaUnits:
    present: bool
    neutralized: str
    dbt: bool = False
    model: str = ""
    symbols: list[Symbol] = field(default_factory=list)
    facets: list[Facet] = field(default_factory=list)
    refs: list[str] = field(default_factory=list)  # model names this file reads
    macro_calls: list[str] = field(default_factory=list)  # bare macro names it calls
    call_lines: dict[str, int] = field(default_factory=dict)  # macro name -> first call line
    ref_lines: dict[str, int] = field(default_factory=dict)


def has_jinja(text: str) -> bool:
    return "{{" in text or "{%" in text


def _blank_keep_lines(m: re.Match[str]) -> str:
    return "\n" * m.group(0).count("\n")


def _placeholder_keep_lines(m: re.Match[str]) -> str:
    return PLACEHOLDER + "\n" * m.group(0).count("\n")


def neutralize(text: str) -> str:
    """The template as SQL the splitter can read: comments and statements
    blanked, expressions replaced by `__jinja__`, every line kept in
    place; `{% raw %}` bodies pass through as written."""
    raws: list[str] = []

    def keep_raw(m: re.Match[str]) -> str:
        raws.append(m.group(1))
        return f"\x00RAW{len(raws) - 1}\x00"

    out = _RAW_RE.sub(keep_raw, text)
    out = _COMMENT_RE.sub(_blank_keep_lines, out)
    out = _STATEMENT_RE.sub(_blank_keep_lines, out)
    out = _EXPR_RE.sub(_placeholder_keep_lines, out)
    for k, body in enumerate(raws):
        out = out.replace(f"\x00RAW{k}\x00", body)
    return out


def _newlines(text: str) -> list[int]:
    return [m.start() for m in re.finditer("\n", text)]


def _line_at(newlines: list[int], pos: int) -> int:
    """`pos`'s line, by bisection in the text's newlines (found once per file): a count
    from the top per tag is quadratic in a file of them."""
    return bisect.bisect_left(newlines, pos) + 1


def _is_dbt(path: str, text: str) -> bool:
    """A dbt file: a `.sql` under dbt's model directories (`models/`,
    `snapshots/`, `analyses/` — the directory IS dbt's convention; a plain
    `select 1` there is a model, as dbt-utils' integration tests show), a
    file defining a macro / test / snapshot, or one carrying dbt's calls
    (`ref`, `source`, `config`, `is_incremental`, `this`) under any dbt dir."""
    parts = path.split("/")
    if any(p in _MODEL_DIRS for p in parts[:-1]):
        return True
    in_dir = any(p in _DBT_DIRS for p in parts[:-1])
    cues = (
        _REF_RE.search(text)
        or _SOURCE_RE.search(text)
        or _CONFIG_RE.search(text)
        or "is_incremental()" in text
        or "{{ this }}" in text
    )
    return bool(_MACRO_DEF_RE.search(text)) or (bool(cues) and in_dir)


def _model_name(path: str) -> str:
    parts = path.split("/")
    if not any(p in _MODEL_DIRS for p in parts[:-1]):
        return ""
    stem = posixpath.basename(path)
    return stem[: stem.index(".")] if "." in stem else stem


def _macro_symbols(path: str, mod: str, text: str, nl: list[int]) -> list[Symbol]:
    out: list[Symbol] = []
    # each kind's end tags (start, end), found once rather than searched for per definition
    ends: dict[str, list[tuple[int, int]]] = {}
    for m in _MACRO_DEF_RE.finditer(text):
        word, name = m.group("word"), m.group("name")
        if word not in ends:
            ends[word] = [(e.start(), e.end()) for e in _MACRO_END_RE[word].finditer(text)]
        k = bisect.bisect_left(ends[word], (m.end(), 0))  # the first starting after it
        e_end = ends[word][k][1] if k < len(ends[word]) else 0
        end_line = _line_at(nl, e_end - 1) if e_end else len(nl) + 1
        args = (m.group("args") or "").strip()
        out.append(
            Symbol(
                qualified_name=f"{mod}::{name}",
                name=name,
                kind=KIND_MACRO,
                signature=f"{word} {name}{args}",
                path=path,
                start_line=_line_at(nl, m.start()),
                end_line=max(_line_at(nl, m.start()), end_line),
            )
        )
    return out


def _calls(text: str, own: set[str], nl: list[int]) -> dict[str, int]:
    """{bare macro name: first call line} for the macros a template calls:
    `pkg.name(` or `name(` inside `{{ }}` / `{% %}`, minus Jinja's and
    dbt's builtins and the file's own macros."""
    out: dict[str, int] = {}
    for tag in [*_EXPR_RE.finditer(text), *_STATEMENT_RE.finditer(text)]:
        if not tag.group(0).endswith("}"):
            continue  # a `{%` that never closes: the text after it is SQL, not Jinja
        for m in _CALL_RE.finditer(tag.group(0)):
            name = m.group("name")
            if name in _JINJA_BUILTINS or name in own or name in out:
                continue
            if m.group("pkg") in ("adapter", "modules", "exceptions", "loop", "this", "target"):
                continue
            out[name] = _line_at(nl, tag.start() + m.start())
    return out


def jinja_units(path: str, mod: str, text: str) -> JinjaUnits:
    """Neutralized text plus dbt symbols, facets and references; a file
    without Jinja comes back `present=False` with its text unchanged."""
    if not has_jinja(text) and not _is_dbt(path, text):
        return JinjaUnits(present=False, neutralized=text)
    templated = has_jinja(text)
    units = JinjaUnits(present=templated, neutralized=neutralize(text) if templated else text)
    units.dbt = _is_dbt(path, text)
    if not units.dbt:
        return units
    total_lines = text.count("\n") + 1
    nl = _newlines(text)
    units.symbols = _macro_symbols(path, mod, text, nl)
    own = {s.name for s in units.symbols}
    model = _model_name(path)
    if model and not units.symbols:
        units.model = model
        cfg = _CONFIG_RE.search(text)
        cfg_line = _line_at(nl, cfg.start()) if cfg else 1
        mat = _MATERIALIZED_RE.search(cfg.group(1)) if cfg else None
        sig = f"model {model}" + (f" · materialized: {mat.group(1)}" if mat else "")
        units.symbols.append(
            Symbol(
                qualified_name=f"{mod}::{model}",
                name=model,
                kind=KIND_MODEL,
                signature=sig,
                path=path,
                start_line=1,
                end_line=total_lines,
            )
        )
        if mat:
            units.facets.append(Facet(FACET_MATERIALIZED, mat.group(1), 0, cfg_line, 1.0))
        tm = _TAGS_RE.search(cfg.group(1)) if cfg else None
        if tm:
            for t in re.findall(r"""['"]([^'"]+)['"]""", tm.group(1)):
                units.facets.append(Facet(FACET_TAG, t, 0, cfg_line, 1.0))
    seen: set[tuple[str, str]] = set()
    for m in _REF_RE.finditer(text):
        name = m.group(2)
        if name not in units.ref_lines:  # a dict: a list's `in` per ref is quadratic
            units.refs.append(name)
            units.ref_lines[name] = _line_at(nl, m.start())
        key = (FACET_TABLE_READ, name)
        if key not in seen:
            seen.add(key)
            units.facets.append(Facet(FACET_TABLE_READ, name, 0, _line_at(nl, m.start()), 1.0))
    for m in _SOURCE_RE.finditer(text):
        src, tbl = m.group(1), m.group(2)
        for key in ((FACET_SOURCE, f"{src}.{tbl}"), (FACET_TABLE_READ, tbl)):
            if key not in seen:
                seen.add(key)
                units.facets.append(Facet(key[0], key[1], 0, _line_at(nl, m.start()), 1.0))
    units.call_lines = _calls(text, own, nl)
    units.macro_calls = list(units.call_lines)
    for name, line in units.call_lines.items():
        units.facets.append(Facet(FACET_MACRO_CALL, name, 0, line, 1.0))
    return units


def _enclosing(units: JinjaUnits, path: str, line: int, reach: list[int]) -> tuple[str, str]:
    """(symbol_id, qualified_name) of the first model or macro holding `line`; the file
    itself when nothing does (a macro-less template). `reach` is the running maximum of
    the symbols' end lines, in their (start-line) order: the first one reaching `line` is
    the first that holds it, when it starts by then."""
    k = bisect.bisect_left(reach, line)
    if k < len(reach) and units.symbols[k].start_line <= line:
        return units.symbols[k].id, units.symbols[k].qualified_name
    return file_symbol_id(path), path


def jinja_edges(
    path: str,
    units: JinjaUnits,
    name_table: dict[str, list[tuple[str, str, str]]],
    *,
    rw_kinds: bool = False,
) -> list[Edge]:
    """model → ref'd model and model / macro → called macro, bound
    repository-unique by bare name (kinds `model`, `macro`); nothing for a
    name the tree does not define (a package's macro, a seed)."""
    edges: list[Edge] = []
    reach = list(itertools.accumulate((s.end_line for s in units.symbols), max))
    for names, kind, lines, edge_kind in (
        (units.refs, KIND_MODEL, units.ref_lines, "reads" if rw_kinds else "calls"),
        (units.macro_calls, KIND_MACRO, units.call_lines, "calls"),
    ):
        for name in names:
            cands = {(sid, qn) for sid, qn, k in name_table.get(name, []) if k == kind}
            if len({qn for _, qn in cands}) != 1:
                continue
            to_id, to_qn = sorted(cands)[0]
            line = lines.get(name, 1)
            from_id, from_qn = _enclosing(units, path, line, reach)
            if from_id == to_id:
                continue
            edges.append(
                Edge(
                    edge_kind=edge_kind,
                    from_symbol_id=from_id,
                    to_symbol_id=to_id,
                    from_qualified_name=from_qn,
                    to_qualified_name=to_qn,
                    path=path,
                    line=line,
                    confidence=0.9,
                )
            )
    return edges


__all__ = [
    "FACET_MACRO_CALL",
    "FACET_MATERIALIZED",
    "FACET_SOURCE",
    "KIND_MACRO",
    "KIND_MODEL",
    "PLACEHOLDER",
    "JinjaUnits",
    "has_jinja",
    "jinja_edges",
    "jinja_units",
    "neutralize",
]
