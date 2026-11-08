"""A shell test's SQL islands and its coalesced chunks.

Most stateless `.sh` tests are one function-free script of `$CLICKHOUSE_CLIENT -q "…"`
blocks: 3,690 files, 25,598 client invocations, 24,655 of them with an inline query, 513
heredocs in the ClickHouse checkout. Two things go wrong when the plane reads them
naively:

  * the AST chunker covers shell FUNCTIONS and emits every blank-line-
    separated leftover run as its own `other` chunk, so a typical test
    becomes 8 chunks at p50 (17 at p90, 143 at the maximum) of 5 lines
    each — 35,408 rows for 10 MB of source, 84% of them leftovers;
  * the SQL inside the strings — the part of the test that matters —
    is opaque: no statement records, no facets, no enclosing unit for a
    top-level line.

This module post-processes one parsed shell file:

  1. **Coalesce** consecutive `other` chunks separated only by blank
     lines into one chunk while the joined text stays within a target
     (the chunker's leftover merge, scoped to shell where it is 84% of
     the rows); a merge never crosses a function chunk, so
     `code.enclosing` keeps answering with the right unit.
  2. **Lift the islands**: tree-sitter-bash `command` nodes whose name
     expands a ClickHouse client (`$CLICKHOUSE_CLIENT`, `${CLICKHOUSE_LOCAL}`,
     `clickhouse-client`, …) and carry a query string after `-q`,
     `--query`, `-n`/`-m`/`--multiquery`, or `--query=…`; the heredoc body of
     a `redirected_statement` around such a command; and the `echo`/
     `printf` string piped into one. Expansions inside the string become
     `__var__` placeholders (a `$table` name is not a name the tree
     defines), then `lang_sql.split_statements` reads the island as SQL.
  3. **Records anchored at the shell line**: one `statement` symbol per
     island statement (`<module>::stmt_NNNN`, ordinals over the file's
     islands in source order), facets as for a `.sql` test — with the
     confidence floored at `CONF_INTERPOLATED` when the statement carried a
     placeholder — the `# Tags:` header as ordinal-0 tag facets, and the
     deterministic context prefix on every chunk that holds an island.

Editors inject SQL into bash strings the same way (tree-sitter `injections.scm`, ctags
`_guest` regions); no shipped rule keys on `$CLICKHOUSE_CLIENT`, so this is ours. Client
calls fed by a pipe from something other than an `echo` string (a `cat` of a data file, a
`$()`) are not islands: they are counted, not guessed at.

Outside the extractor stamp (see `tests/unit/test_code_extractor_stamp.py`):
dispatched from `ingest_chunks.py` for `shell` files behind `code_shell_islands`,
so the deployment that carries it starts no heal; a namespace's shell tree
is re-chunked by `loom code backfill --paths-file` (~3.8k files).
"""

from __future__ import annotations

import posixpath
import re
from dataclasses import dataclass, field
from typing import Any

from server.code.domain.extract.chunker import Chunk, _assign_ordinals
from server.code.domain.extract.lang_bats import tags as bats_tags
from server.code.domain.extract.lang_detect import is_bats_path
from server.code.domain.extract.lang_python import module_scope
from server.code.domain.extract.lang_sql import context_prefix, statement_symbol
from server.code.domain.extract.lang_sql_facets import statement_facets
from server.code.domain.extract.lang_sql_model import (
    FACET_SETTING,
    FACET_TAG,
    Facet,
    Header,
    Statement,
    parse_header,
)
from server.code.domain.extract.lang_sql_split import split_statements
from server.code.domain.extract.tokenize import code_tokens
from server.code.domain.extract.treesitter import ParsedFile, Symbol, node_text

DEFAULT_TARGET_CHARS = 2000
CONF_INTERPOLATED = 0.7
PLACEHOLDER = "__var__"


@dataclass(frozen=True)
class SqlClient:
    """A program that takes SQL on its command line: how its name is written in a
    script, which flags introduce the query, and the dialect the island is read under."""

    name_re: re.Pattern[str]
    dialect: str
    query_flags: frozenset[str]
    query_eq: tuple[str, ...] = ()
    # `sqlite3 db.sqlite "SELECT …"`: the query is the Nth positional word
    positional_query: int = 0


# The client table. The ClickHouse row comes first; the others are the binaries a
# repository's scripts drive most often.
SQL_CLIENTS: tuple[SqlClient, ...] = (
    SqlClient(
        re.compile(
            r"CLICKHOUSE_(?:CLIENT|LOCAL|CURL|BENCHMARK|FORMAT|OBFUSCATOR|COMPRESSOR|KEEPER_CLIENT)"
            r"(?:_BINARY|_OPT)?\b|clickhouse-(?:client|local)\b"
            r"|(?<![\w-])clickhouse\s+(?:client|local)\b"
        ),
        "clickhouse",
        frozenset(
            {
                "-q",
                "--query",
                "-n",
                "-m",
                "-mn",
                "-nm",
                "--multiquery",
                # `${CLICKHOUSE_CURL} … -d "SELECT …"`: the HTTP body IS the query
                "-d",
                "--data",
                "--data-binary",
                "--data-raw",
                "--data-urlencode",
            }
        ),
        ("--query=", "-q=", "--data=", "--data-binary=", "--data-raw="),
    ),
    SqlClient(
        re.compile(r"(?<![\w-])psql\b|\bPSQL\b"),
        "postgres",
        frozenset({"-c", "--command"}),
        ("--command=",),
    ),
    SqlClient(
        re.compile(r"(?<![\w-])(?:mysql|mariadb)\b|\bMYSQL\b"),
        "mysql",
        frozenset({"-e", "--execute"}),
        ("--execute=",),
    ),
    SqlClient(re.compile(r"(?<![\w-])sqlite3?\b"), "sqlite", frozenset(), (), 2),
    SqlClient(re.compile(r"(?<![\w-])duckdb\b"), "duckdb", frozenset({"-c"}), (), 2),
    SqlClient(
        re.compile(r"(?<![\w-])snowsql\b"), "snowflake", frozenset({"-q", "--query"}), ("--query=",)
    ),
    SqlClient(re.compile(r"(?<![\w-])bq\b"), "bigquery", frozenset(), (), 2),
    SqlClient(re.compile(r"(?<![\w-])sqlcmd\b"), "tsql", frozenset({"-Q", "-q"})),
    SqlClient(
        re.compile(r"(?<![\w-])trino\b|(?<![\w-])presto\b"),
        "trino",
        frozenset({"--execute"}),
        ("--execute=",),
    ),
)
FACET_DIALECT = "dialect"


def client_for(name: str) -> SqlClient | None:
    for c in SQL_CLIENTS:
        if c.name_re.search(name):
            return c
    return None


# every client's flags, for the callers that only need "is this a query flag"
_QUERY_FLAGS = frozenset().union(*(c.query_flags for c in SQL_CLIENTS))
_QUERY_EQ = tuple(dict.fromkeys(e for c in SQL_CLIENTS for e in c.query_eq))
_ECHO_CMDS = frozenset({"echo", "printf"})
# `$CLICKHOUSE_CLIENT --max_threads=1 --async_insert 1 -q "…"`: any
# `--name[=value]` the client accepts that is not one of its own options is a
# server SETTING for that query (the runner injects randomized settings the
# same way). The client's own options are not settings.
_CLIENT_OPTIONS = frozenset(
    {
        "query",
        "queries-file",
        "database",
        "host",
        "port",
        "user",
        "password",
        "format",
        "multiquery",
        "multiline",
        "config-file",
        "config",
        "secure",
        "progress",
        "time",
        "stacktrace",
        "echo",
        "verbose",
        "log-level",
        "server_logs_file",
        "send_logs_level",
        "query_id",
        "history_file",
        "interactive",
        "vertical",
        "ignore-error",
        "pager",
        "testmode",
        "suggestion_limit",
        "compression",
        "no-warnings",
        "allow_repeated_settings",
        "allow_merge_tree_settings",
        "param_",
        "structure",
        "input-format",
        "output-format",
        "file",
        "table",
        "name",
        "help",
        "version",
        "jwt",
        "ssh-key-file",
        "ssh-key-passphrase",
        "connection",
        "accept-invalid-certificate",
        "processed-rows",
        "max_client_network_bandwidth",
        "hardware-utilization",
        "print-profile-events",
        "profile-events-delay-ms",
        "wait_for_async_insert",
    }
)
_SETTING_FLAG_RE = re.compile(r"^--([a-z][a-z0-9_]{2,})(?:=(.*))?$")
# `${CLICKHOUSE_CURL} "${CLICKHOUSE_URL}&async_insert=1&wait_for_async_insert=0"`:
# the HTTP interface takes settings as URL parameters.
_URL_PARAM_RE = re.compile(r"[?&]([a-z][a-z0-9_]{2,})=")
_URL_NOT_SETTINGS = frozenset(
    {
        "query",
        "database",
        "user",
        "password",
        "session_id",
        "query_id",
        "default_format",
        "compress",
        "decompress",
        "enable_http_compression",
        "http_zlib_compression_level",
    }
)


@dataclass
class Island:
    """One SQL string found in the shell file: its text (placeholders
    substituted), the file line its first character sits on, how it was
    found, and whether it carried an expansion."""

    text: str
    line: int
    via: str  # inline | heredoc | pipe
    interpolated: bool
    # server settings passed on the client's command line (`--max_threads=1`)
    settings: tuple[str, ...] = ()
    dialect: str = "clickhouse"


@dataclass
class ShellUnits:
    statements: list[Statement]
    header: Header
    symbols: list[Symbol]
    chunks: list[Chunk]
    prefixes: dict[int, str]  # chunk index -> context prefix (chunks holding an island)
    facets: list[Facet] = field(default_factory=list)
    islands: list[Island] = field(default_factory=list)


# --------------------------------------------------------------------------
# tree walking
# --------------------------------------------------------------------------


_EXPANSION_TYPES = frozenset(
    {
        "expansion",
        "simple_expansion",
        "command_substitution",
        "arithmetic_expansion",
    }
)


def _string_value(node: Any, source: bytes) -> tuple[str, bool]:
    """The value a string-like node denotes, with every expansion replaced
    by the placeholder; (value, had_expansion). Built from the node's own
    byte range: tree-sitter-bash splits a multi-line string into several
    `string_content` children and the newlines between them belong to no
    child, so joining children would lose them."""
    t = node.type
    if t == "raw_string":
        return node_text(node, source)[1:-1], False
    if t in _EXPANSION_TYPES:
        return PLACEHOLDER, True
    lo, hi = node.start_byte, node.end_byte
    if t == "string" and hi - lo >= 2:
        lo, hi = lo + 1, hi - 1  # the quotes
    parts: list[str] = []
    had = False
    pos = lo

    def emit(a: int, b: int) -> None:
        if b > a:
            parts.append(source[a:b].decode("utf-8", "replace"))

    def walk(n: Any) -> None:
        nonlocal pos, had
        for c in n.children:
            if c.type in _EXPANSION_TYPES:
                emit(pos, max(pos, c.start_byte))
                parts.append(PLACEHOLDER)
                had = True
                pos = c.end_byte
            elif c.type in ("string", "raw_string", "concatenation", "heredoc_body"):
                emit(pos, max(pos, c.start_byte))
                v, h = _string_value(c, source)
                parts.append(v)
                had = had or h
                pos = c.end_byte
            elif c.children:
                walk(c)

    walk(node)
    emit(pos, hi)
    return "".join(parts), had


def _is_client(name_node: Any, source: bytes) -> bool:
    return name_node is not None and client_for(node_text(name_node, source)) is not None


def _dialect_of(cmd: Any, source: bytes) -> str:
    name = cmd.child_by_field_name("name")
    c = client_for(node_text(name, source)) if name is not None else None
    return c.dialect if c else "clickhouse"


def _command_args(cmd: Any) -> list[Any]:
    return [c for c in cmd.children if c.is_named and c.type != "command_name"]


def command_settings(cmd: Any, source: bytes) -> tuple[str, ...]:
    """Server settings on a client's command line: `--name=value` and
    `--name value` for every long option that is not one of the client's
    own (`--max_threads=1`, `--async_insert 1`), and the URL parameters of
    an HTTP call (`"$CLICKHOUSE_URL&async_insert=1"`), in order,
    deduplicated."""
    out: list[str] = []
    for a in _command_args(cmd):
        raw = node_text(a, source)
        if a.type in ("word", "concatenation"):
            m = _SETTING_FLAG_RE.match(raw.split("=", 1)[0] + ("=" if "=" in raw else ""))
            if m:
                name = m.group(1)
                if name not in _CLIENT_OPTIONS and not name.startswith("param_"):
                    if name not in out:
                        out.append(name)
                continue
        if a.type in ("string", "concatenation", "word") and ("?" in raw or "&" in raw):
            for name in _URL_PARAM_RE.findall(raw):
                if name not in _URL_NOT_SETTINGS and name not in out:
                    out.append(name)
    return tuple(out)


def _inline_islands(cmd: Any, source: bytes) -> list[Island]:
    """`-q "…"`, `--query "…"`, `--query="…"`, `-n "…"`, curl's `-d "…"` on
    one client call, each carrying the command line's settings."""
    out: list[Island] = []
    args = _command_args(cmd)
    settings = command_settings(cmd, source)
    name = cmd.child_by_field_name("name")
    client = client_for(node_text(name, source)) if name is not None else None
    dialect = client.dialect if client else "clickhouse"
    flags = client.query_flags if client else _QUERY_FLAGS
    eqs = client.query_eq if client else _QUERY_EQ
    for i, a in enumerate(args):
        raw = node_text(a, source)
        if raw in flags and i + 1 < len(args):
            nxt = args[i + 1]
            if nxt.type in ("string", "raw_string", "concatenation"):
                v, had = _string_value(nxt, source)
                if v.strip() and not v.strip().startswith("@"):  # `-d @file` is data
                    out.append(Island(v, nxt.start_point[0] + 1, "inline", had, settings, dialect))
        elif eqs and raw.startswith(eqs) and a.type == "concatenation":
            # --query="SELECT …": the concatenation's string children hold it
            parts = [c for c in a.children if c.type in ("string", "raw_string")]
            if parts:
                v, had = _string_value(parts[0], source)
                if v.strip():
                    out.append(
                        Island(v, parts[0].start_point[0] + 1, "inline", had, settings, dialect)
                    )
    if not out and client and client.positional_query:
        # `sqlite3 db.sqlite "SELECT …"` / `bq query "SELECT …"`: the query is
        # the Nth word that is not a flag
        words = [a for a in args if not node_text(a, source).startswith("-")]
        if len(words) >= client.positional_query:
            node = words[client.positional_query - 1]
            if node.type in ("string", "raw_string", "concatenation"):
                v, had = _string_value(node, source)
                if _looks_like_sql(v):
                    out.append(Island(v, node.start_point[0] + 1, "inline", had, settings, dialect))
    if not out and settings:
        # `$CLICKHOUSE_CLIENT --max_threads=1 < file.sql`: no query text, but
        # the settings are still facts about the file
        out.append(Island("", cmd.start_point[0] + 1, "flags", False, settings, dialect))
    return out


_SQL_HEAD_RE = re.compile(
    r"^\s*(SELECT|INSERT|CREATE|DROP|ALTER|WITH|UPDATE|DELETE|PRAGMA|ATTACH|DETACH|EXPLAIN|"
    r"SHOW|DESCRIBE|OPTIMIZE|SYSTEM|TRUNCATE|GRANT|REVOKE|COPY|VACUUM|ANALYZE|BEGIN|SET)\b",
    re.I,
)


def _looks_like_sql(text: str) -> bool:
    """A positional argument is a query when it starts like one; a database
    path or a table name does not."""
    return bool(_SQL_HEAD_RE.match(text))


def _heredoc_island(stmt: Any, source: bytes) -> Island | None:
    """`$CLICKHOUSE_CLIENT <<EOF … EOF` — the body of the heredoc redirect."""
    body = None
    for c in stmt.children:
        if c.type == "heredoc_redirect":
            for d in c.children:
                if d.type == "heredoc_body":
                    body = d
        elif c.type == "heredoc_body":
            body = c
    if body is None:
        return None
    v, had = _string_value(body, source)
    if not v.strip():
        return None
    cmd = next((c for c in stmt.children if c.type == "command"), None)
    dialect = _dialect_of(cmd, source) if cmd is not None else "clickhouse"
    return Island(v, body.start_point[0] + 1, "heredoc", had, (), dialect)


def _pipe_island(pipeline: Any, source: bytes) -> Island | None:
    """`echo "SELECT …" | $CLICKHOUSE_CLIENT`: the echo's string when a
    later command of the pipeline is a client with no inline query."""
    cmds = [c for c in pipeline.children if c.type == "command"]
    if len(cmds) < 2:
        return None
    first, rest = cmds[0], cmds[1:]
    name = first.child_by_field_name("name")
    if name is None or node_text(name, source) not in _ECHO_CMDS:
        return None
    if not any(_is_client(c.child_by_field_name("name"), source) for c in rest):
        return None
    clients = [c for c in rest if _is_client(c.child_by_field_name("name"), source)]
    if any(_inline_islands(c, source) for c in clients):
        return None
    strings = [
        a for a in _command_args(first) if a.type in ("string", "raw_string", "concatenation")
    ]
    if not strings:
        return None
    vals = [_string_value(s, source) for s in strings]
    text = " ".join(v for v, _ in vals)
    if not text.strip():
        return None
    dialect = _dialect_of(clients[0], source) if clients else "clickhouse"
    return Island(text, strings[0].start_point[0] + 1, "pipe", any(h for _, h in vals), (), dialect)


def find_islands(pf: ParsedFile) -> list[Island]:
    """Every SQL island of a parsed shell file, in source order."""
    if pf.tree is None:
        return []
    source = pf.source
    out: list[Island] = []

    def walk(node: Any) -> None:
        t = node.type
        if t == "command" and _is_client(node.child_by_field_name("name"), source):
            out.extend(_inline_islands(node, source))
        elif t == "redirected_statement":
            body = next((c for c in node.children if c.type == "command"), None)
            if body is not None and _is_client(body.child_by_field_name("name"), source):
                isl = _heredoc_island(node, source)
                if isl is not None:
                    out.append(isl)
        elif t == "pipeline":
            isl = _pipe_island(node, source)
            if isl is not None:
                out.append(isl)
        for c in node.children:
            walk(c)

    walk(pf.tree.root_node)
    out.sort(key=lambda i: i.line)
    return out


# --------------------------------------------------------------------------
# records
# --------------------------------------------------------------------------


def island_statements(islands: list[Island]) -> list[tuple[Statement, Island]]:
    """The islands' statements re-anchored to file lines, with ordinals
    running over the whole file in source order."""
    out: list[tuple[Statement, Island]] = []
    for isl in islands:
        if not isl.text.strip():
            continue
        for st in split_statements(isl.text):
            shift = isl.line - 1
            st.start_line += shift
            st.end_line += shift
            st.ordinal = len(out) + 1
            out.append((st, isl))
    return out


def island_facets(
    header: Header, pairs: list[tuple[Statement, Island]], islands: list[Island] | None = None
) -> list[Facet]:
    """Tag facets from the header, statement facets per island statement
    (confidence floored when interpolated, placeholder names dropped), and
    the command-line settings of every island as `setting` facets on its
    first statement (ordinal 0 for a flags-only island)."""
    out = [Facet(FACET_TAG, t, 0, 1, 1.0) for t in header.tags]
    first_ordinal: dict[int, int] = {}
    for st, isl in pairs:
        first_ordinal.setdefault(id(isl), st.ordinal)
        out.append(Facet(FACET_DIALECT, isl.dialect, st.ordinal, st.start_line, 1.0))
        for f in statement_facets(st):
            if isl.interpolated and f.confidence > CONF_INTERPOLATED:
                f = Facet(f.kind, f.name, f.ordinal, f.line, CONF_INTERPOLATED)
            if PLACEHOLDER in f.name:
                continue
            out.append(f)
    seen = {(f.kind, f.name, f.ordinal) for f in out}
    for isl in islands or []:
        for name in isl.settings:
            key = (FACET_SETTING, name, first_ordinal.get(id(isl), 0))
            if key not in seen:
                seen.add(key)
                out.append(Facet(FACET_SETTING, name, key[2], isl.line, 1.0))
    return out


# --------------------------------------------------------------------------
# chunks
# --------------------------------------------------------------------------


def coalesce_leftovers(
    path: str, source_lines: list[str], chunks: list[Chunk], target: int
) -> list[Chunk]:
    """Consecutive `other` chunks join while their joined text (the lines
    between them included) stays within `target`; a function or header
    chunk between them ends the run. The lines between two leftovers are
    blank lines or runs the chunker dropped under its 40-char floor, so the
    merged chunk covers what nothing else indexes."""
    out: list[Chunk] = []
    run: list[Chunk] = []

    def flush() -> None:
        if not run:
            return
        if len(run) == 1:
            out.append(run[0])
        else:
            a, b = run[0].start_line, run[-1].end_line
            out.append(
                Chunk(
                    path=path,
                    scope="",
                    kind="other",
                    start_line=a,
                    end_line=b,
                    content="".join(source_lines[a - 1 : b]),
                )
            )
        run.clear()

    ordered = sorted(chunks, key=lambda x: (x.start_line, x.end_line))
    # lines a function or header chunk owns; a merge never swallows them.
    # Lines no chunk owns (a run under the chunker's 40-char floor — a lone
    # `}` or a short `$CLICKHOUSE_CLIENT -q "…"` opener) are mergeable: the
    # coalesced chunk is the only place they will ever be indexed.
    owned = [False] * (len(source_lines) + 2)
    for c in ordered:
        if c.kind != "other":
            for i in range(c.start_line, min(c.end_line, len(source_lines)) + 1):
                owned[i] = True
    for c in ordered:
        if c.kind != "other":
            flush()
            out.append(c)
            continue
        if run:
            prev = run[-1]
            gap_owned = any(owned[i] for i in range(prev.end_line + 1, c.start_line))
            joined = sum(len(x) for x in source_lines[run[0].start_line - 1 : c.end_line])
            if not gap_owned and joined <= target:
                run.append(c)
                continue
            flush()
        run.append(c)
    flush()
    return out


_SHELLCHECK_SOURCE_RE = re.compile(r"^[^\S\n]*#\s*shellcheck\s+source=(\S+)", re.M)
_INTERPRETERS = frozenset(
    {
        "bash",
        "sh",
        "zsh",
        "ksh",
        "dash",
        "source",
        ".",
        "python",
        "python3",
        "node",
        "perl",
        "ruby",
        "bats",
    }
)
# a command NAME is a repository script when it is written as a path (a bare
# `todo.sh` is a PATH lookup); an interpreter's ARGUMENT may be a bare file
_PATH_CMD_RE = re.compile(r"^(\./|\.\./|/|[\w.-]+/)[\w./-]+$")
_PATHLIKE_RE = re.compile(r"^(\./|\.\./|/|[\w.-]+/)[\w./-]+$|^[\w.-]+\.(sh|bash|bats|py|js|pl|rb)$")
FACET_PROGRAM = "program"
# bash builtins and POSIX utilities every script uses: a `program` facet
# for `set` or `echo` distinguishes nothing, so they never become one
SHELL_BUILTINS: frozenset[str] = frozenset(
    {
        ":",
        ".",
        "alias",
        "bg",
        "bind",
        "break",
        "builtin",
        "caller",
        "cd",
        "command",
        "compgen",
        "complete",
        "continue",
        "declare",
        "dirs",
        "disown",
        "echo",
        "enable",
        "eval",
        "exec",
        "exit",
        "export",
        "false",
        "fc",
        "fg",
        "getopts",
        "hash",
        "help",
        "history",
        "jobs",
        "kill",
        "let",
        "local",
        "logout",
        "mapfile",
        "popd",
        "printf",
        "pushd",
        "pwd",
        "read",
        "readarray",
        "readonly",
        "return",
        "set",
        "shift",
        "shopt",
        "source",
        "suspend",
        "test",
        "times",
        "trap",
        "true",
        "type",
        "typeset",
        "ulimit",
        "umask",
        "unalias",
        "unset",
        "wait",
        "[",
        "[[",
        "cat",
        "cp",
        "cut",
        "date",
        "dirname",
        "basename",
        "env",
        "grep",
        "head",
        "ls",
        "mkdir",
        "mv",
        "rm",
        "rmdir",
        "sed",
        "sleep",
        "sort",
        "tail",
        "tee",
        "touch",
        "tr",
        "uniq",
        "wc",
        "which",
        "xargs",
        "awk",
        "seq",
        "sh",
        "bash",
        "setopt",
        "unsetopt",
        "emulate",
        "zmodload",
        "autoload",
    }
)


def resolve_include(path: str, target: str) -> str:
    """A `source`/`.`/`load` target as a repository path: relative targets
    resolve against the sourcing file's directory (`../../nvm.sh` from
    `test/fast/x` → `nvm.sh`), the way the shell resolves them at run time
    from the script's directory. Absolute targets (`/dev/null`,
    `/etc/bash_completion`) are not repository files and resolve to '', and
    so do `$`-bearing targets (a runtime value the tree does not carry;
    `# shellcheck source=` is how a script names the file behind them),
    except bats' `$BATS_LIB_PATH/<lib>/load.bash`, mapped onto the root."""
    if not target or target.startswith("/"):
        return ""
    if target.startswith("$BATS_LIB_PATH/"):
        # bats' library root: the repository's own copy lives at the root
        # (`bats-support/load.bash`), the one place a namespace can hold it
        return posixpath.normpath(target[len("$BATS_LIB_PATH/") :])
    if "$" in target:
        return ""
    resolved = posixpath.normpath(posixpath.join(posixpath.dirname(path), target))
    return "" if resolved.startswith("../") else resolved


CWD_MARK = "?cwd:"


def cwd_alternate(path: str, target: str) -> str:
    """A relative `source lib/x.sh` resolves from the shell's WORKING directory, not the
    script's — CI scripts run from the repository root, `cd "$(dirname "$0")"` scripts
    from their own. The parse cannot know which; it names the root-relative reading as a
    marked alternate (`?cwd:lib/x.sh`) that the ingest keeps only when the namespace
    holds that file (`_resolve_lane_includes`), so a wrong guess is never an edge. ''
    when both readings coincide or the target is not a repository path."""
    if not target or target.startswith("/") or "$" in target:
        return ""
    root_relative = posixpath.normpath(target)
    if root_relative.startswith("../") or root_relative in (".", ""):
        return ""
    if root_relative == resolve_include(path, target):
        return ""
    return CWD_MARK + root_relative


def settle_cwd_includes(includes: list[str], present: set[str]) -> list[str]:
    """The includes with each `?cwd:` alternate replaced by its path when
    `present` (the namespace's or corpus's files) holds it, dropped
    otherwise; order kept, duplicates removed."""
    out: list[str] = []
    for inc in includes:
        if inc.startswith(CWD_MARK):
            inc = inc[len(CWD_MARK) :]
            if inc not in present:
                continue
        out.append(inc)
    return list(dict.fromkeys(out))


def _directive_include(path: str, target: str) -> str:
    """`# shellcheck source=…` names the file ShellCheck should read for a
    `source "$ROOT/lib/x.bash"` it cannot follow. Repositories write it two
    ways and both are read: `../shell_config.sh` (ClickHouse: 3,600 tests,
    script-relative, what `-P SCRIPTDIR` and the `./`/`../` spelling mean)
    and `lib/bats-core/common.bash` (bats-core: relative to the working
    directory ShellCheck runs from, the repository root). `SCRIPTDIR/x` is
    the explicit script-relative form; `/dev/null` is "do not follow"."""
    if not target or target.startswith("/") or "$" in target:
        return ""
    if target.startswith("SCRIPTDIR/"):
        return resolve_include(path, target[len("SCRIPTDIR/") :])
    if target.startswith(("./", "../")):
        return resolve_include(path, target)
    return posixpath.normpath(target)


def include_targets(pf: ParsedFile, text: str) -> list[str]:
    """The file→file edges a shell script declares: its `source`/`.` targets resolved to
    repository paths, `# shellcheck source=` overrides, and the repository scripts it
    INVOKES — a command whose name is a path (`./scripts/x.sh`, `tools/build.sh`) or an
    interpreter given a path (`bash x.sh`, `python3 tools/y.py`). The edge kind stays
    `includes` (the table's enum); an invocation is a file dependency of the same shape."""
    out: list[str] = []
    for inc in pf.includes:
        out.append(resolve_include(pf.path, inc))
        out.append(cwd_alternate(pf.path, inc))
    for m in _SHELLCHECK_SOURCE_RE.findall(text):
        out.append(_directive_include(pf.path, m))
    if pf.tree is not None:
        source = pf.source

        def walk(n: Any) -> None:
            if n.type == "command":
                name = n.child_by_field_name("name")
                nm = node_text(name, source) if name is not None else ""
                if nm and _PATH_CMD_RE.match(nm) and "$" not in nm:
                    out.append(resolve_include(pf.path, nm))
                    out.append(cwd_alternate(pf.path, nm))
                elif nm in _INTERPRETERS:
                    for a in _command_args(n):
                        w = node_text(a, source).strip("'\"")
                        if w.startswith("-"):
                            continue
                        if _PATHLIKE_RE.match(w) and "$" not in w:
                            out.append(resolve_include(pf.path, w))
                            out.append(cwd_alternate(pf.path, w))
                        break
            for c in n.children:
                walk(c)

        walk(pf.tree.root_node)
    return [t for t in dict.fromkeys(out) if t and t != pf.path]


def program_facets(pf: ParsedFile) -> list[Facet]:
    """The programs a script runs: every command whose name is in
    `name_scope.SHELL_COMMANDS` and not a builtin or a POSIX utility every script uses,
    one `program` facet per name at its first line — rather than an unresolved-call
    miss."""
    from server.code.domain.extract.name_scope import SHELL_COMMANDS

    if pf.tree is None:
        return []
    seen: dict[str, int] = {}
    source = pf.source

    def walk(n: Any) -> None:
        if n.type == "command":
            name = n.child_by_field_name("name")
            nm = node_text(name, source) if name is not None else ""
            if nm in SHELL_COMMANDS and nm not in SHELL_BUILTINS and nm not in seen:
                seen[nm] = n.start_point[0] + 1
        for c in n.children:
            walk(c)

    walk(pf.tree.root_node)
    return [Facet(FACET_PROGRAM, nm, 0, line, 1.0) for nm, line in seen.items()]


# shell syntax the parser reports as a command name, and bats' own test DSL
# (`skip`, `load`, `run` are bats functions no repository defines)
_NOT_A_NAME = frozenset(
    {
        ":",
        "{",
        "}",
        "[",
        "[[",
        "!",
        '"$@"',
        "$@",
        "skip",
        "load",
        "bats_load_library",
        "bats_require_minimum_version",
        "run",
    }
)


def drop_program_misses(misses: list[Any]) -> list[Any]:
    """The resolver's unresolved-call rows for a shell file, minus the ones that are not
    names anybody defines: programs (`SHELL_COMMANDS`, recorded as `program` facets
    instead), builtins, `$var` commands, paths, quoted words, numbers, and shell syntax
    the parser reports as a command name (`:`, `[`, `"$@"`)."""
    from server.code.domain.extract.name_scope import SHELL_COMMANDS

    def keep(name: str) -> bool:
        name = name.lstrip("\\")  # `\grep`: the alias-bypassing spelling of grep
        return (
            bool(name)
            and not name.isdigit()
            and name not in SHELL_COMMANDS
            and name not in SHELL_BUILTINS
            and name not in _NOT_A_NAME
            and not name.startswith(("$", "/", "./", "../", '"', "'"))
        )

    return [m for m in misses if keep(m.callee_name)]


def shell_units(
    pf: ParsedFile, chunks: list[Chunk], *, target_chars: int = DEFAULT_TARGET_CHARS
) -> ShellUnits:
    """Coalesced chunks, island statement symbols, facets and per-chunk
    context prefixes for one parsed shell file. `chunks` is the AST
    chunker's output for the file."""
    text = pf.source.decode("utf-8", "replace")
    lines = text.splitlines(keepends=True)
    header = parse_header(text, sign="#")
    if is_bats_path(pf.path):
        # `# bats file_tags=` / `test_tags=` join the tag facets
        file_tags, per_test = bats_tags(text)
        all_tags = tuple(
            dict.fromkeys([*header.tags, *file_tags, *(t for ts in per_test.values() for t in ts)])
        )
        header = Header(
            tags=all_tags,
            random_settings_limits=header.random_settings_limits,
            memory_limits=header.memory_limits,
        )
    islands = find_islands(pf)
    pairs = island_statements(islands)
    statements = [st for st, _ in pairs]
    mod = module_scope(pf.path)
    symbols = [statement_symbol(pf.path, mod, st) for st in statements]
    by_ordinal = {st.ordinal: sym.id for st, sym in zip(statements, symbols, strict=True)}
    merged = coalesce_leftovers(pf.path, lines, chunks, target_chars)
    prefixes: dict[int, str] = {}
    for i, c in enumerate(merged):
        inside = [st for st in statements if c.start_line <= st.start_line <= c.end_line]
        if not inside:
            continue
        c.symbol_ids = list(
            dict.fromkeys([*c.symbol_ids, *(by_ordinal[s.ordinal] for s in inside)])
        )
        prefix = context_prefix(pf.path, header, inside)
        c.extra_lexemes = " ".join(code_tokens(prefix))
        prefixes[i] = prefix
    _assign_ordinals(merged)
    return ShellUnits(
        statements=statements,
        header=header,
        symbols=symbols,
        chunks=merged,
        prefixes=prefixes,
        facets=[*island_facets(header, pairs, islands), *program_facets(pf)],
        islands=islands,
    )


__all__ = [
    "CWD_MARK",
    "cwd_alternate",
    "CONF_INTERPOLATED",
    "DEFAULT_TARGET_CHARS",
    "FACET_DIALECT",
    "FACET_PROGRAM",
    "PLACEHOLDER",
    "SHELL_BUILTINS",
    "SQL_CLIENTS",
    "Island",
    "ShellUnits",
    "SqlClient",
    "client_for",
    "coalesce_leftovers",
    "command_settings",
    "drop_program_misses",
    "find_islands",
    "include_targets",
    "island_facets",
    "island_statements",
    "program_facets",
    "resolve_include",
    "settle_cwd_includes",
    "shell_units",
]
