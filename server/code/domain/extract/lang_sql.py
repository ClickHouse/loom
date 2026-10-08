"""A stateless SQL test as a sequence of statement records.

`tests/queries/0_stateless/*.sql` is what the runner treats it as: a header
(`-- Tags: …`, `-- Random settings limits: …`, `-- Memory limits: …`) and
an ordered list of statements, each with the client's TestHint on its own
line (`-- { serverError BAD_ARGUMENTS }`, `{ echoOn }`). Read as plain
line windows, such a file has no symbols: chunks end mid-statement,
`code.outline` lists nothing, `code.enclosing` answers nothing for any SQL
line, and "which tests SET this setting" is a literal grep over the test
trees on every call.

What one file yields here:

  * a `Symbol` per statement — `kind='statement'`, qualified name
    `<module scope>::stmt_0042`, signature `SELECT · serverError
    BAD_ARGUMENTS`, span = the statement's lines with its own leading
    comments — so the outline, the enclosing read and the grep shim work
    on `.sql` through the symbol rows nothing else had to learn about;
  * chunks packed by statement (adjacent siblings merged): whole
    statements accumulate up to a target size and a statement is never
    cut; one oversized statement splits at line boundaries with an
    elision marker, as an oversized function does;
  * a deterministic context prefix per chunk (contextual retrieval at
    zero token cost: test name, tags, statement kinds, the
    settings, functions, engines and expected errors the chunk touches),
    carried in `context_text` AND ahead of the content in the lexemes;
  * facet rows — (kind, name, ordinal, line, confidence) — the
    name-reference graph "which tests use X" is one point read of
    (`loom.code_test_facets`).

The splitter is the runner's own contract (`splitMultipartQuery`: `;` at
depth 0 outside strings and comments ends a statement; a same-line
trailing comment belongs to it), the hint grammar is `src/Client/
TestHint.cpp`'s (text between the first `{` and the next `}` of a comment
that FOLLOWS the statement's first token; `echo|echoOn|echoOff|retry`, and
`serverError|clientError|error` with a comma list of numbers or bare
names), and the header rule is `tests/clickhouse-test`'s
(`line.startswith(sign) and line[len(sign):].lstrip().startswith("Tags:")`,
any line of the file). Measured on 400 sampled tests (7,059 statements):
the splitter agrees with `clickhouse format --multiquery` on every file
that tool accepts (95.75%), and it never fails — a test that deliberately
holds a syntax error is still a statement with a `clientError` hint.

Facets record what is WRITTEN, with a confidence that says how the name
was read: 1.0 for a keyword-bound name (`SET x`, `SETTINGS x =`,
`ENGINE = X`, `system.x`, `FORMAT X`, an error name in a hint), 0.8 for an
identifier followed by `(` (a function call — or a table function, a
lambda parameter, a UDF). Vocabulary validation against the tree's
registrations is a read-time join, never an extractor input, so the rows
are the same whatever the namespace's commit registers.

This module is outside the extractor stamp on purpose (see
`tests/unit/test_code_extractor_stamp.py`): it is dispatched from `ingest.py` for `sql`
files only, so the deployment that carries it starts no heal; the SQL tree of a
namespace is re-chunked by `loom code backfill --paths-file` (~11.5k files on
ClickHouse).
"""

from __future__ import annotations

import posixpath
from collections import Counter
from typing import Any

from server.code.domain.extract.chunker import Chunk, _assign_ordinals
from server.code.domain.extract.lang_python import module_scope
from server.code.domain.extract.lang_sql_facets import FACET_TYPE, file_facets, statement_facets
from server.code.domain.extract.lang_sql_model import (
    _PREFIX_MAX_NAMES,
    DEFAULT_MAX_CHARS,
    DEFAULT_TARGET_CHARS,
    FACET_ENGINE,
    FACET_ERROR,
    FACET_FORMAT,
    FACET_FUNCTION,
    FACET_SETTING,
    FACET_SYSTEM_TABLE,
    SQL_LANG,
    STATEMENT_KIND,
    Facet,
    Header,
    SqlUnits,
    Statement,
    parse_header,
)
from server.code.domain.extract.lang_sql_split import split_statements
from server.code.domain.extract.tokenize import code_tokens
from server.code.domain.extract.treesitter import ParsedFile, Symbol

# --------------------------------------------------------------------------
# symbols, chunks, prefixes
# --------------------------------------------------------------------------


def statement_symbol(path: str, mod: str, stmt: Statement) -> Symbol:
    return Symbol(
        qualified_name=f"{mod}::{stmt.name}",
        name=stmt.name,
        kind=STATEMENT_KIND,
        signature=stmt.signature(),
        path=path,
        start_line=stmt.start_line,
        end_line=stmt.end_line,
        doc_comment=stmt.comments,
    )


def split_file(path: str, text: str) -> tuple[list[Statement], Any]:
    """The statements of one SQL file and its Jinja units: a templated file is split on
    its NEUTRALIZED text, so every reader of the statements — the parse, the chunker,
    the edge stage — sees the same records with the same ordinals."""
    from server.code.domain.extract.lang_sql_jinja import jinja_units

    ju = jinja_units(path, module_scope(path), text)
    return split_statements(ju.neutralized if ju.present else text), ju


def parse_sql(path: str, source: bytes) -> ParsedFile:
    """The fast-tier parse of a SQL test: one `statement` symbol per record.
    `tree` stays None (no tree-sitter grammar); the chunker for this
    language is `sql_units`, dispatched by `ingest.py`."""
    text = source.decode("utf-8", "replace")
    pf = ParsedFile(path=path, lang=SQL_LANG, tree=None, source=source)
    mod = module_scope(path)
    statements, ju = split_file(path, text)
    for st in statements:
        pf.symbols.append(statement_symbol(path, mod, st))
    pf.symbols.extend(ju.symbols)  # Dbt models and macros
    # The tools' directives — named queries and changesets as symbols, psql includes as
    # edges (set at parse so the edges-only pass derives the same edges as the full
    # ingest)
    from server.code.domain.extract.lang_sql_directives import directive_units
    from server.code.domain.extract.lang_sql_objects import object_symbols

    du = directive_units(path, text, statements)
    pf.symbols.extend(du.symbols)
    pf.symbols.extend(object_symbols(path, mod, statements))
    pf.symbols.extend(_column_symbols(path, mod, statements))
    pf.includes = du.includes
    return pf


def _column_symbols(path: str, mod: str, statements: list[Statement]) -> list[Symbol]:
    """One `column` symbol per column the file defines, when column lineage is on."""
    from server.code.domain.extract.lang_sql_columns import column_lineage_on, column_symbols
    from server.code.domain.extract.lang_sql_objects import schema_contexts

    if not column_lineage_on():
        return []
    return column_symbols(path, mod, statements, schema_contexts(statements))


def _column_facets(statements: list[Statement]) -> list[Facet]:
    """`column_read` / `column_write` facets against the file's own column definitions,
    when column lineage is on."""
    from server.code.domain.extract.lang_sql_columns import (
        column_facets,
        column_lineage_on,
        file_columns,
    )
    from server.code.domain.extract.lang_sql_objects import schema_contexts

    if not column_lineage_on():
        return []
    return column_facets(statements, file_columns(statements, schema_contexts(statements)))


def _scope_of(group: list[Statement]) -> str:
    if len(group) == 1:
        return group[0].name
    return f"{group[0].name}-{group[-1].name}"


def _pack(statements: list[Statement], target: int) -> list[list[Statement]]:
    """Consecutive statements accumulate into one group while the joined
    text stays within `target`; a statement is never cut here."""
    groups: list[list[Statement]] = []
    cur: list[Statement] = []
    size = 0
    for st in statements:
        n = len(st.text)
        if cur and size + n > target:
            groups.append(cur)
            cur, size = [], 0
        cur.append(st)
        size += n
    if cur:
        groups.append(cur)
    return groups


def _split_lines(text: str, start_line: int, max_chars: int) -> list[tuple[str, int, int]]:
    """Line-bounded pieces of one oversized statement, each ending in an
    elision marker that names the next line."""
    lines = text.splitlines(keepends=True)
    out: list[tuple[str, int, int]] = []
    buf: list[str] = []
    size = 0
    first = start_line
    for k, ln in enumerate(lines):
        if buf and size + len(ln) > max_chars - 48:
            content = "".join(buf) + f"… continued at line {start_line + k}\n"
            out.append((content, first, start_line + k - 1))
            buf, size, first = [], 0, start_line + k
        buf.append(ln[:max_chars])
        size += len(ln)
    if buf:
        out.append(("".join(buf), first, start_line + len(lines) - 1))
    return out


def _chunk_text(text_lines: list[str], group: list[Statement]) -> str:
    a = group[0].start_line - 1
    b = group[-1].end_line
    return "".join(text_lines[a:b])


def _group_chunks(
    path: str, text_lines: list[str], group: list[Statement], ids: list[str], max_chars: int
) -> list[Chunk]:
    content = _chunk_text(text_lines, group)
    scope = _scope_of(group)
    if len(content) <= max_chars:
        return [
            Chunk(
                path=path,
                scope=scope,
                kind="other",
                start_line=group[0].start_line,
                end_line=group[-1].end_line,
                content=content,
                symbol_ids=ids,
            )
        ]
    return [
        Chunk(
            path=path,
            scope=scope,
            kind="other",
            start_line=s,
            end_line=e,
            content=piece,
            symbol_ids=ids,
        )
        for piece, s, e in _split_lines(content, group[0].start_line, max_chars)
    ]


def _header_chunk(path: str, text_lines: list[str], first: Statement | None) -> Chunk | None:
    """The lines before the first statement's record (the `-- Tags:` line
    and whatever the author wrote above the first statement)."""
    end = (first.start_line - 1) if first is not None else len(text_lines)
    content = "".join(text_lines[:end])
    if not content.strip():
        return None
    return Chunk(
        path=path, scope="", kind="file_header", start_line=1, end_line=end, content=content
    )


def context_prefix(
    path: str,
    header: Header,
    group: list[Statement],
    extras: list[str] | None = None,
    tail: list[str] | None = None,
) -> str:
    """One line a reader (and both retrieval legs) can use as the chunk's context: the
    test, its tags, what the statements are and touch — and, for a migration or query
    file, its tool, version and direction (the migration-file `extras`)."""
    facets = [f for st in group for f in statement_facets(st)]
    parts = [posixpath.basename(path)]
    if header.tags:
        parts.append("tags: " + ", ".join(header.tags[:_PREFIX_MAX_NAMES]))
    parts.extend(extras or [])
    kinds = Counter(st.kind for st in group)
    span = group[0].name if len(group) == 1 else f"{group[0].name}–{group[-1].name}"
    parts.append(
        span
        + ": "
        + ", ".join(f"{k}×{n}" if n > 1 else k for k, n in kinds.most_common(_PREFIX_MAX_NAMES))
    )
    for label, kind in (
        ("sets", FACET_SETTING),
        ("funcs", FACET_FUNCTION),
        ("types", FACET_TYPE),
        ("engine", FACET_ENGINE),
        ("system", FACET_SYSTEM_TABLE),
        ("format", FACET_FORMAT),
        ("expects", FACET_ERROR),
    ):
        names = Counter(f.name for f in facets if f.kind == kind)
        if names:
            top = [n for n, _ in names.most_common(_PREFIX_MAX_NAMES)]
            parts.append(f"{label}: " + ", ".join(top))
    parts.extend(tail or [])  # Creates / reads / writes
    return " · ".join(parts)


def sql_units(
    pf: ParsedFile, *, target_chars: int = DEFAULT_TARGET_CHARS, max_chars: int = DEFAULT_MAX_CHARS
) -> SqlUnits:
    """Statements, symbols, statement-packed chunks with their context
    prefixes, and facets for one parsed SQL file. Chunk `extra_lexemes`
    carries the prefix's identifiers ahead of the content so the lexical
    leg sees them under the 512-token cap."""
    from server.code.domain.extract.lang_sql_dialect import dialect_facets
    from server.code.domain.extract.lang_sql_directives import directive_units
    from server.code.domain.extract.lang_sql_objects import (
        object_symbols,
        reference_facets,
    )
    from server.code.domain.extract.lang_sql_objects import prefix_parts as object_prefix

    text = pf.source.decode("utf-8", "replace")
    text_lines = text.splitlines(keepends=True)
    statements, ju = split_file(path=pf.path, text=text)
    header = parse_header(text)
    mod = module_scope(pf.path)
    symbols = [statement_symbol(pf.path, mod, st) for st in statements]
    du = directive_units(pf.path, text, statements)
    by_ordinal = {s.ordinal: sym.id for s, sym in zip(statements, symbols, strict=True)}
    chunks: list[Chunk] = []
    prefixes: list[str] = []
    head = _header_chunk(pf.path, text_lines, statements[0] if statements else None)
    if head is not None:
        chunks.append(head)
        prefixes.append(
            posixpath.basename(pf.path)
            + (" · tags: " + ", ".join(header.tags) if header.tags else "")
        )
    for group in _pack(statements, target_chars):
        ids = [by_ordinal[s.ordinal] for s in group]
        dirs = sorted({du.directions[s.ordinal] for s in group if s.ordinal in du.directions})
        extras = du.prefix_parts() + ([", ".join(dirs)] if dirs else [])
        prefix = context_prefix(pf.path, header, group, extras, tail=object_prefix(group))
        for c in _group_chunks(pf.path, text_lines, group, ids, max_chars):
            c.extra_lexemes = " ".join(code_tokens(prefix))
            chunks.append(c)
            prefixes.append(prefix)
    _assign_ordinals(chunks)
    return SqlUnits(
        statements=statements,
        header=header,
        symbols=[
            *symbols,
            *du.symbols,
            *object_symbols(pf.path, mod, statements),
            *ju.symbols,
            *_column_symbols(pf.path, mod, statements),
        ],
        chunks=chunks,
        prefixes=prefixes,
        facets=[
            *file_facets(header, statements),
            *du.facets,
            *reference_facets(statements),
            *ju.facets,
            *dialect_facets(text),  # The file's own dialect
            *_column_facets(statements),
        ],
    )


def is_sql_path(path: str) -> bool:
    """`.sql` and the Jinja-templated `.sql.j2` the runner renders before
    running (the template text splits like SQL; `{% %}` blocks are text)."""
    lower = path.lower()
    return lower.endswith((".sql", ".sql.j2"))


__all__ = [
    "context_prefix",
    "is_sql_path",
    "parse_sql",
    "split_file",
    "sql_units",
]
