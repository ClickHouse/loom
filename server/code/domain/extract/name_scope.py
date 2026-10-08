"""Which repo-wide definitions a call site may bind to, by language class.

`extract_edges_and_misses` resolves a bare callee name in three rungs: same-file
definition, method of the enclosing class, then the unique definition of that name
anywhere in the namespace. The name table the third rung reads
(`CodeRepo.name_candidates`) is one dictionary over every tier and every parser, so
without a language gate a bash `seq` would bind to `DB::QueryRunnerJobCompletion::seq`
and a Python test helper to whichever C++ method shares its bare name. SCIP symbols
carry a scheme for exactly this reason: a name is only unique WITHIN a language's
namespace.

The gate is a class check, not a per-language one, because the side table
(`loom.code_symbol_names`) holds neither path nor language. It does not
need to: C++ qualified names can never contain a `.` (an identifier
character set that excludes it), while every script symbol is
module-scoped (`lang_python.module_scope`: `tests.queries.shell_config::
wait_for_query_to_start`), so the scope part of the qualified name says
which class a definition belongs to. Shell and Python remain one class —
a shell helper and a pytest helper of the same bare name are still
ambiguous to each other; that residue is accepted, not guessed away here.

Shell callers also consult a command specification table first: external commands are black boxes
with known specifications, never symbols. A name in `SHELL_COMMANDS` is a program, so it binds
nothing; the extractor's own `_NOISE_BASH` stoplist drops the most common of them before they
reach the name table, this table covers the rest without moving the edge stage's stamp.

This module is outside the extractor stamp on purpose (see
`tests/unit/test_code_extractor_stamp.py`): it narrows the candidate table
`ingest_edges._derive_edges` hands the stamped resolver, and the rows already
derived under the wider table are repaired by `loom code backfill
--paths-file` over the script paths of each namespace (a few thousand
files, minutes; content unchanged so chunks and symbols are kept and only
edges re-derive) — never by a namespace-wide heal of the edge stage, which
re-resolves every C++ file for rows the key-diff would keep.
"""

from __future__ import annotations

import re

NATIVE = "native"
SCRIPT = "script"

# Characters a script module scope never contains, and native scopes do whenever their
# qualified name carries a `.` (paths, template arguments). Brackets, parens, `$`, `@`
# and `+` are NOT native marks — a framework route segment carries them inside a dotted
# module scope (`src.pages.posts.[...slug].index`, `docs.src.routes.(app).docs`, Remix's
# `$id`, Next.js's `@modal`, SvelteKit's `+page`), and the C++ shapes that carry them
# (`foo(int)::Local`, a lambda's `$`) have no `.` in the scope.
_NATIVE_MARKS: frozenset[str] = frozenset("/<>{}*&~ ,")
# The same marks as one search: `qualified_name_class` runs once per candidate per call
# site, too often for a character-by-character generator over the scope.
_NATIVE_MARKS_RE = re.compile("[" + re.escape("".join(sorted(_NATIVE_MARKS))) + "]")

_CLASS_BY_LANG: dict[str, str] = {
    "cpp": NATIVE,
    "python": SCRIPT,
    "shell": SCRIPT,
    # TypeScript / JavaScript symbols are module-scoped like Python's (a dotted scope),
    # so their qualified names already read as script definitions; Rust names are crate
    # paths with `::` only, the native shape C++ has
    "typescript": SCRIPT,
    # Go and Java names are package-path scoped like Python's
    "go": SCRIPT,
    "java": SCRIPT,
    "javascript": SCRIPT,
    "rust": NATIVE,
    # Protobuf and Go-template names are module-scoped `<path>::Name` like Python's
    "proto": SCRIPT,
    "gotemplate": SCRIPT,
    "hcl": SCRIPT,  # Dotted directory scopes
    "k8s": SCRIPT,  # Dotted file scopes
    "jsonnet": SCRIPT,
}

# Programs a shell test calls that are not, and can never be, symbols of
# the repository: coreutils, the shell's own builtins the parser reports as
# commands, and the client binaries the tests drive. Kept in one place so a
# reader can see what a shell call site is allowed to bind to (anything
# not here that a script in the namespace defines).
SHELL_COMMANDS: frozenset[str] = frozenset(
    {
        # coreutils / POSIX utilities
        "awk",
        "base64",
        "basename",
        "bc",
        "cat",
        "chmod",
        "chown",
        "cmp",
        "comm",
        "cp",
        "curl",
        "cut",
        "date",
        "dd",
        "df",
        "diff",
        "dirname",
        "du",
        "echo",
        "env",
        "expr",
        "false",
        "file",
        "find",
        "fold",
        "gawk",
        "grep",
        "gzip",
        "head",
        "hexdump",
        "id",
        "jq",
        "kill",
        "ln",
        "ls",
        "md5sum",
        "mkdir",
        "mkfifo",
        "mktemp",
        "mv",
        "nc",
        "nproc",
        "od",
        "paste",
        "perl",
        "pgrep",
        "pkill",
        "printf",
        "ps",
        "pwd",
        "python",
        "python3",
        "readlink",
        "realpath",
        "rm",
        "rmdir",
        "sed",
        "seq",
        "sha1sum",
        "sha256sum",
        "shuf",
        "sleep",
        "sort",
        "split",
        "stat",
        "strings",
        "tac",
        "tail",
        "tar",
        "tee",
        "test",
        "time",
        "timeout",
        "touch",
        "tr",
        "true",
        "truncate",
        "uniq",
        "wc",
        "wget",
        "which",
        "xargs",
        "xxd",
        "yes",
        "zcat",
        # shell builtins / keywords tree-sitter-bash reports as commands
        "alias",
        "break",
        "cd",
        "command",
        "continue",
        "declare",
        "eval",
        "exec",
        "exit",
        "export",
        "getopts",
        "hash",
        "let",
        "local",
        "popd",
        "pushd",
        "read",
        "readonly",
        "return",
        "set",
        "shift",
        "source",
        "trap",
        "type",
        "typeset",
        "ulimit",
        "umask",
        "unalias",
        "unset",
        "wait",
        # the binaries the stateless tests drive
        "clickhouse",
        "clickhouse-client",
        "clickhouse-local",
        "clickhouse-server",
        "clickhouse-benchmark",
        "clickhouse-format",
        "clickhouse-keeper",
        "clickhouse-compressor",
        "clickhouse-obfuscator",
        "clickhouse-disks",
        "clickhouse-test",
        "docker",
        "git",
        "make",
        "cmake",
        "ninja",
        "gdb",
        "lldb",
        "strace",
        "ltrace",
        "valgrind",
        "openssl",
        "ssh",
        "scp",
        "rsync",
        "sudo",
        "nohup",
        "setsid",
        "bash",
        "sh",
        "zsh",
        # Runtimes, package managers and utilities common OSS shell repositories (nvm,
        # bash-it, bats-core) call
        "install",
        "npm",
        "npx",
        "node",
        "yarn",
        "pnpm",
        "bun",
        "deno",
        "pip",
        "pip3",
        "brew",
        "apt",
        "apt-get",
        "yum",
        "dnf",
        "apk",
        "pacman",
        "bats",
        "shellcheck",
        "shfmt",
        "yq",
        "gunzip",
        "zip",
        "unzip",
        "tput",
        "stty",
        "column",
        "uname",
        "hostname",
        "whoami",
        "groups",
        "getent",
        "ip",
        "ifconfig",
        "netstat",
        "ss",
        "lsof",
        "systemctl",
        "service",
        "crontab",
    }
)


def language_class(lang: str) -> str:
    """`native` for the compiled language, `script` for the interpreted ones,
    '' for a language whose call sites are never resolved (sql, config…)."""
    return _CLASS_BY_LANG.get(lang, "")


def qualified_name_class(qualified_name: str) -> str:
    """The class a definition belongs to, read off its qualified name: a
    dotted module scope before the last `::` is a script definition
    (`tests.queries.shell_config::wait_for_query_to_start`,
    `tests.integration.helpers.cluster::ClickHouseCluster::start`); a scope
    without one is native (`DB::MergeTreeData::getSettings`, a bare
    `parseQuery`). C++ identifiers cannot contain `.`; the native shapes that
    do — an anonymous-namespace symbol keyed by its file path, a variadic
    pack in a template argument list — are told apart by the marks a module
    scope never carries."""
    scope, sep, _ = qualified_name.rpartition("::")
    if not sep:
        return NATIVE
    # Two native shapes carry a `.`: an anonymous-namespace symbol keyed by
    # its translation unit's path (`DB::$anonymous_namespace_src/IO/Foo.cpp::
    # Visitor`) and a variadic template pack in a template argument list
    # (`DB::ArraySinkCreator<Type, Types...>::create`). A
    # module scope (`lang_python.module_scope`) is letters, digits, `_`, `-`
    # and `.` only — never a path separator, a `$`, a bracket or a space —
    # so any of those marks says native before the `.` is even consulted.
    if _NATIVE_MARKS_RE.search(scope):
        return NATIVE
    return SCRIPT if "." in scope else NATIVE


# The objects a SQL file creates are symbols of SQL alone — a Python `users()` call
# never binds to the `users` table, and a SQL reference to `users` never binds to a
# Python function of that name (`function` is the one kind SQL shares: a PL/pgSQL
# function and a Python function are both callable by name from either side).
SQL_OBJECT_KINDS: frozenset[str] = frozenset(
    {
        "table",
        "view",
        "index",
        "trigger",
        "procedure",
        "sequence",
        "dictionary",
        "schema",
        "database",
        "role",
        "user",
        "policy",
        "extension",
        "domain",
        "aggregate",
        "type",
        "model",
        "macro",  # Dbt models and macros
        "column",  # A table's column, `<table>.<column>`
    }
)
SQL_LANG = "sql"


# `type` is shared the way `function` is — a Go defined type (`type Kind int`), a
# TypeScript alias and a Rust type live under it too — so it is not SQL's alone; every
# other SQL object kind still is.
SQL_ONLY_KINDS: frozenset[str] = SQL_OBJECT_KINDS - {"type"}


def _binds(caller_lang: str, qualified_name: str, kind: str = "") -> bool:
    if kind in SQL_ONLY_KINDS:
        return caller_lang == SQL_LANG
    caller_class = language_class(caller_lang)
    if not caller_class:
        return True  # no rule for this language; the resolver's own rungs decide
    return qualified_name_class(qualified_name) == caller_class


def gate_name_table(
    name_table: dict[str, list[tuple[str, str, str]]], caller_lang: str
) -> dict[str, list[tuple[str, str, str]]]:
    """The candidate table for one file: every definition a call site of
    `caller_lang` may bind to. Native callers keep native definitions,
    script callers keep script definitions; a shell caller additionally
    drops every name in `SHELL_COMMANDS` (a program, not a symbol). Names
    whose candidate list empties are removed, so rung 3 records a miss
    under them exactly as it does for a name nobody defines."""
    if not name_table:
        return name_table
    out: dict[str, list[tuple[str, str, str]]] = {}
    for name, candidates in name_table.items():
        if caller_lang == "shell" and name in SHELL_COMMANDS:
            continue
        kept = [c for c in candidates if _binds(caller_lang, c[1], c[2] if len(c) > 2 else "")]
        if kept:
            out[name] = kept
    return out


_TEST_DIRS: frozenset[str] = frozenset({"test", "tests", "spec", "specs", "__tests__", "t"})


def drop_test_local_shell_defs(
    name_table: dict[str, list[tuple[str, str, str]]], caller_path: str
) -> dict[str, list[tuple[str, str, str]]]:
    """A shell function defined under a test directory is a mock or a helper for THAT
    directory's tests (`git() { echo …; }` in a bats file, `setup_repo` in
    `test/helpers.sh`); it binds a call site in the same directory and none elsewhere.
    Candidates whose module sits under a test directory are dropped unless the caller's
    directory is that directory or below it."""
    here = caller_path.split("/")[:-1]
    out: dict[str, list[tuple[str, str, str]]] = {}
    for name, candidates in name_table.items():
        kept = []
        for c in candidates:
            mod = c[1].partition("::")[0].split(".")[:-1]
            if any(seg in _TEST_DIRS for seg in mod) and here[: len(mod)] != mod:
                continue
            kept.append(c)
        if kept:
            out[name] = kept
    return out


__all__ = [
    "LANGUAGE_EXTENSIONS",
    "NATIVE",
    "SCRIPT",
    "SHELL_COMMANDS",
    "SQL_OBJECT_KINDS",
    "defined_in_language",
    "drop_test_local_shell_defs",
    "go_package_dirs",
    "scope_stems",
    "gate_name_table",
    "language_class",
    "qualified_name_class",
]


# Which present paths a module-scoped qualified name of each language can come from. The
# script languages share one qualified-name scheme (`<path minus extension, / →
# .>::Name`; Go: the package directory plus the package clause), so a scope alone does
# not say which language defined it — the paths the namespace holds do.
LANGUAGE_EXTENSIONS: dict[str, tuple[str, ...]] = {
    "python": (".py", ".pyi", ".pyw", ".ipynb"),
    "typescript": (
        ".ts",
        ".tsx",
        ".mts",
        ".cts",
        ".js",
        ".jsx",
        ".mjs",
        ".cjs",
        ".vue",
        ".svelte",
        ".astro",
    ),
    "javascript": (
        ".ts",
        ".tsx",
        ".mts",
        ".cts",
        ".js",
        ".jsx",
        ".mjs",
        ".cjs",
        ".vue",
        ".svelte",
        ".astro",
    ),
    "shell": (".sh", ".bash", ".bats", ""),
    "go": (".go",),
}
_MAX_DOTTED_TAIL = 2  # `foo.test.ts` → scope `foo.test`: keep up to two dots of the file name


def scope_stems(scope: str) -> list[str]:
    """The path stems a dotted scope may stand for: every dot a directory
    separator first, then the last one and two dots kept as part of the file
    name (`a.b.c` → `a/b/c`, `a/b.c`, `a.b.c`)."""
    parts = scope.split(".")
    out: list[str] = []
    for k in range(0, min(_MAX_DOTTED_TAIL, len(parts) - 1) + 1):
        head = parts[: len(parts) - k]
        tail = parts[len(parts) - k :]
        stem = "/".join(head) + ("." + ".".join(tail) if tail else "")
        if stem not in out:
            out.append(stem)
    return out


def go_package_dirs(present: set[str]) -> frozenset[str]:
    """The directories holding a `.go` file — a Go qualified name's scope is
    such a directory plus its package clause."""
    return frozenset(p.rsplit("/", 1)[0] if "/" in p else "" for p in present if p.endswith(".go"))


def defined_in_language(
    qualified_name: str,
    lang: str,
    present: set[str],
    go_dirs: frozenset[str] | None = None,
) -> bool:
    """Can a definition with this qualified name be `lang`'s, given the namespace's
    present paths? A native-class name, a language without a table, or a SQL-only kind
    is never gated (True). For Python and the TypeScript family the scope's stems must
    exist under one of the language's extensions; for shell also as an extensionless
    script; for Go the scope minus its package clause (or the whole scope) must be a
    directory that holds a `.go` file."""
    exts = LANGUAGE_EXTENSIONS.get(lang)
    if exts is None or qualified_name_class(qualified_name) != SCRIPT:
        return True
    scope = qualified_name.split("::", 1)[0]
    if lang == "go":
        dirs = go_dirs if go_dirs is not None else go_package_dirs(present)
        parts = scope.split(".")
        for stem in scope_stems(scope):
            if stem in dirs:
                return True
        return "/".join(parts[:-1]) in dirs
    for stem in scope_stems(scope):
        for ext in exts:
            if stem + ext in present:
                return True
    return False
