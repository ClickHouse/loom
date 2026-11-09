"""The parse step of a file's ingest: which parser a path takes (tree-sitter, a host
language's islands, or a line-window lane) and the facts that ride its symbols."""

from __future__ import annotations

import functools
import logging
from typing import Any

from server.code.app.index.denylist import Denylist, default_denylist
from server.code.app.index.ingest_stats import error_text
from server.code.domain.extract.bounded_parse import ParseTimeout, ParseUnavailable
from server.code.domain.extract.lang_detect import detect_by_extension, detect_shell, is_bats_path
from server.code.domain.extract.lang_hosts import HostUnits, detect_host, host_kind, host_units
from server.code.domain.extract.lang_py_islands import py_sql_units
from server.code.domain.extract.lang_reference import REFERENCE_MAX_BYTES, reference_lang_for_path
from server.code.domain.extract.lang_shell_islands import include_targets
from server.code.domain.extract.lang_sql import is_sql_path, parse_sql
from server.code.domain.extract.lang_sql_model import SQL_LANG
from server.code.domain.extract.treesitter import ParsedFile, lang_for_path, parse_cpp

logger = logging.getLogger("loom.code_index.ingest")


def parse_file(path: str, source: bytes) -> ParsedFile | None:
    """Parse a file for indexing; None means 'not an indexable file'.

    Parse failure — including a missing grammar package — degrades to a
    chunk-only ParsedFile: the file stays searchable, it just has no
    symbols. sql/config/doc are chunk-only BY DESIGN (no symbol semantics
    worth graphing), not for lack of a grammar.
    """
    return parse_for_ingest(path, source)[0]


def parse_for_ingest(path: str, source: bytes) -> tuple[ParsedFile | None, str]:
    """`parse_file` plus the reason it degraded, when it did.

    The reason is returned rather than only logged so `FileIngestStats.error` and
    `code_files.index_error` are set — `code.index_errors`,
    `code.coverage.files_errored` and the Health tile read them; without it a file
    indexed without symbols would look healthy.
    """
    lang = _lane_language(lang_for_path(path), path, source)
    if not lang and _statement_records_on() and is_sql_path(path):
        lang = SQL_LANG  # `.sql.j2`: the runner renders it, the splitter reads it as text
    if lang in ("config", ""):
        # A Helm manifest template (YAML, NOTES.txt or HTML under `templates/` holding
        # an action) is a Go template, not config
        from server.code.domain.extract.lang_gotemplate import LANG_GOTEMPLATE, is_template_manifest
        from server.code.domain.extract.lang_k8s import (
            LANG_K8S,
            is_k8s_manifest,
            is_templated_manifest,
        )

        if is_template_manifest(path, source) or is_templated_manifest(path, source):
            lang = LANG_GOTEMPLATE  # any templated manifest, Helm or not
        elif is_k8s_manifest(path, source):
            lang = LANG_K8S  # A rendered manifest's documents
    if not lang:
        # TypeScript / JavaScript, Rust, Prisma; CSS and MDX chunk-only — detected
        # outside treesitter.py so a new language moves no extractor stamp
        lang = detect_by_extension(path)
    if not lang:
        # Extensionless executables (tests/clickhouse-test, ci wrappers)
        # declare their language on line one — honor the shebang.
        head = source[:120].lstrip()
        if head.startswith(b"#!"):
            line1 = head.split(b"\n", 1)[0]
            if b"python" in line1:
                lang = "python"
            elif b"bash" in line1 or line1.endswith(b"/sh") or b" sh" in line1:
                lang = "shell"
    if not lang:
        # Linguist's Shell names, ShellCheck's directive and shebang order, and the
        # extensionless test-tree heuristic
        lang = detect_shell(path, source)
    if not lang and _host_islands_on():
        # Dockerfile, Makefile, Jenkinsfile index as config files whose shell islands
        # carry facets, edges and symbols
        lang = detect_host(path)
    if not lang:
        # Expected-output fixtures: one unembedded chunk per file. Detected HERE, not in
        # `lang_for_path` — treesitter.py is in every extractor stage's stamp, and a new
        # language there would re-chunk every namespace for files this rule does not
        # touch.
        lang = reference_lang_for_path(path)
        if lang and len(source) > REFERENCE_MAX_BYTES:
            return None, ""  # a multi-megabyte answer set is not indexed at all
    if not lang:
        # A text file no parser claims is still chunked and searchable (Kotlin, Ruby,
        # `.txt`, `.conf`, …); binaries and oversized files stay out. Outside every
        # extractor stamp.
        from server.code.domain.extract.lang_text import text_lang_for_path

        lang = text_lang_for_path(path, source)
    if not lang:
        return None, ""
    parser = _PARSERS.get(lang)
    if lang == SQL_LANG and _statement_records_on():
        parser = parse_sql  # One `statement` symbol per record
    if lang == "shell" and is_bats_path(path):
        parser = _parse_bats  # `@test` blocks as function symbols
    if lang == "config" and _host_islands_on() and host_kind(path):
        parser = _parse_host  # Jobs / targets / stages, invoked scripts
    if parser is not None:
        try:
            pf = parser(path, source)
        except Exception as e:  # noqa: BLE001 — parse failure is per-file, not fatal
            logger.warning("code ingest: parse failed for %s: %s", path, e)
            return ParsedFile(path=path, lang=lang, source=source), error_text("parse", e)
        if lang == "shell":
            # A shell file's include targets are repository paths — resolved HERE, at
            # parse, so the full ingest and the edges-only dependents pass derive the
            # same `includes` edges
            pf.includes = include_targets(pf, source.decode("utf-8", "replace"))
        if lang == "cpp":
            from server.code.domain.extract.error_codes import error_code_symbols

            pf.symbols.extend(error_code_symbols(path, source))
        return pf, ""
    return ParsedFile(path=path, lang=lang, source=source), ""


def parse_timed_out(error: str) -> bool:
    """Whether `parse_for_ingest`'s reason says the parse ran past its limits."""
    return error.startswith(f"parse: {ParseTimeout.__name__}")


def parse_unavailable(error: str) -> bool:
    """Whether `parse_for_ingest`'s reason says the parse could not run here now."""
    return error.startswith(f"parse: {ParseUnavailable.__name__}")


def parse_for_round(
    path: str, source: bytes, denylist: Denylist | None = None
) -> tuple[ParsedFile | None, str] | None:
    """The parse `ingest_file` will make of this source, done ahead for a round's name
    prefetch — or None when `ingest_file` would refuse the file before parsing
    (denylisted, binary, over the byte cap), so no refused file is parsed."""
    from server.settings import settings as _settings

    rules = denylist if denylist is not None else default_denylist()
    if rules.refusal(path, source):
        return None
    cap = int(_settings.code_ingest_max_file_bytes)
    if cap and len(source) > cap:
        return None
    return parse_for_ingest(path, source)


def _parse_python(path: str, source: bytes) -> ParsedFile:
    from server.code.domain.extract.lang_python import parse_python

    if path.lower().endswith(".ipynb"):
        # The notebook's code cells as one Python text with cell markers; a file that is
        # not a notebook stays a text chunk
        from server.code.domain.extract.py_notebooks import notebook_source

        lifted = notebook_source(source)
        if lifted is None:
            return ParsedFile(path=path, lang="python", source=source)
        source = lifted
    pf = parse_python(path, source)
    if _py_islands_on():
        # The SQL the file runs joins the parse's symbols at parse so the full ingest
        # and the edges-only pass see the same rows
        units = py_sql_units(pf)
        known = {(x.qualified_name, x.id) for x in pf.symbols}
        pf.symbols.extend(x for x in units.symbols if (x.qualified_name, x.id) not in known)
    return pf


def _py_islands_on() -> bool:
    """Gate for SQL strings inside Python as statement records, facets and edges. Off
    keeps a Python file as its functions."""
    from server.settings import settings

    return bool(settings.code_py_sql_islands)


def _parse_host(path: str, source: bytes) -> ParsedFile:
    """A host file: chunk-only like every config file, with its jobs / targets / stages
    as symbols and the repository scripts its shell islands invoke as include targets —
    set at parse so the full ingest and the edges-only pass derive the same edges."""
    pf = ParsedFile(path=path, lang="config", source=source)
    hu = host_units_of(path, source)
    if hu is not None:
        pf.symbols = list(hu.symbols)
        pf.includes = list(hu.includes)
    return pf


@functools.lru_cache(maxsize=8)
def host_units_of(path: str, source: bytes) -> HostUnits | None:
    """`host_units` of a host file's text, kept process-wide for the last 8 host texts,
    so the parse and chunk steps of one ingest usually read its shell once. Callers copy
    what they change."""
    return host_units(ParsedFile(path=path, lang="config", source=source))


def _parse_bats(path: str, source: bytes) -> ParsedFile:
    from server.code.domain.extract.lang_bats import parse_bats

    return parse_bats(path, source)


def _parse_bash(path: str, source: bytes) -> ParsedFile:
    from server.code.domain.extract.lang_bash import parse_bash

    return parse_bash(path, source)


TS_LANGS: frozenset[str] = frozenset({"typescript", "javascript"})


def _parse_typescript(path: str, source: bytes) -> ParsedFile:
    from server.code.domain.extract.lang_typescript import parse_typescript

    return parse_typescript(path, source)


def _parse_vue(path: str, source: bytes) -> ParsedFile:
    """A single-file component's script block through the TypeScript lane's grammar; the
    parse returns `typescript` / `javascript` so every downstream TypeScript branch
    applies unchanged."""
    from server.code.domain.extract.lang_vue import parse_vue

    return parse_vue(path, source)


def _parse_angular_template(path: str, source: bytes) -> ParsedFile:
    """An external Angular template as its component's unit."""
    from server.code.domain.extract.lang_angular import parse_angular_template

    return parse_angular_template(path, source)


def _parse_svelte(path: str, source: bytes) -> ParsedFile:
    """A Svelte component through the Vue rule."""
    from server.code.domain.extract.lang_sfc import parse_svelte

    return parse_svelte(path, source)


def _parse_astro(path: str, source: bytes) -> ParsedFile:
    """An Astro component's frontmatter and markup."""
    from server.code.domain.extract.lang_sfc import parse_astro

    return parse_astro(path, source)


def _parse_rust(path: str, source: bytes) -> ParsedFile:
    from server.code.domain.extract.lang_rust import parse_rust

    return parse_rust(path, source)


def _parse_prisma(path: str, source: bytes) -> ParsedFile:
    from server.code.domain.extract.lang_prisma import parse_prisma

    return parse_prisma(path, source)


def _parse_go(path: str, source: bytes) -> ParsedFile:
    from server.code.domain.extract.lang_go import go_sql_units, parse_go

    pf = parse_go(path, source)
    _join_island_symbols(pf, go_sql_units(pf).symbols)
    return pf


def _parse_java(path: str, source: bytes) -> ParsedFile:
    from server.code.domain.extract.lang_java import java_sql_units, parse_java

    pf = parse_java(path, source)
    _join_island_symbols(pf, java_sql_units(pf).symbols)
    return pf


def _parse_proto(path: str, source: bytes) -> ParsedFile:
    from server.code.domain.extract.lang_proto import parse_proto

    return parse_proto(path, source)


def _parse_hcl(path: str, source: bytes) -> ParsedFile:
    from server.code.domain.extract.lang_hcl import parse_hcl

    return parse_hcl(path, source)


def _parse_jsonnet(path: str, source: bytes) -> ParsedFile:
    from server.code.domain.extract.lang_jsonnet import parse_jsonnet

    return parse_jsonnet(path, source)


def _parse_k8s(path: str, source: bytes) -> ParsedFile:
    from server.code.domain.extract.lang_k8s import parse_k8s

    return parse_k8s(path, source)


def _parse_gotemplate(path: str, source: bytes) -> ParsedFile:
    from server.code.domain.extract.lang_gotemplate import parse_gotemplate

    return parse_gotemplate(path, source)


def _join_island_symbols(pf: ParsedFile, symbols: list[Any]) -> None:
    """The SQL islands' symbols join the parse at parse time, so the full ingest and the edges-only
    pass see one set."""
    known = {(x.qualified_name, x.id) for x in pf.symbols}
    pf.symbols.extend(x for x in symbols if (x.qualified_name, x.id) not in known)


CODE_ISLAND_LANGS: frozenset[str] = frozenset({"go", "java"})


def _code_sql_units(pf: ParsedFile) -> Any:
    if pf.lang == "go":
        from server.code.domain.extract.lang_go import go_sql_units

        return go_sql_units(pf)
    from server.code.domain.extract.lang_java import java_sql_units

    return java_sql_units(pf)


def _lane_language(lang: str, path: str, source: bytes) -> str:
    """The languages `lang_for_path` misfiles — a CMake file reads `config` there, and a
    TLC model config is a `.cfg` only its text identifies. Both detected here so
    treesitter.py (in every extractor stamp) stays untouched."""
    if lang == "config":
        from server.code.domain.extract.lang_cmake import LANG_CMAKE, is_cmake_path

        if is_cmake_path(path):
            return LANG_CMAKE
    if not lang:
        from server.code.domain.extract.lang_tla import LANG_TLA_CONFIG, is_tlc_config

        if is_tlc_config(path, source):
            return LANG_TLA_CONFIG
    return lang


def _parse_tla(path: str, source: bytes) -> ParsedFile:
    from server.code.domain.extract.lang_tla import parse_tla

    return parse_tla(path, source)


def _parse_tlc(path: str, source: bytes) -> ParsedFile:
    from server.code.domain.extract.lang_tla import parse_tlc_config

    return parse_tlc_config(path, source)


def _parse_fbs(path: str, source: bytes) -> ParsedFile:
    from server.code.domain.extract.lang_flatbuffers import parse_fbs

    return parse_fbs(path, source)


def _parse_cmake(path: str, source: bytes) -> ParsedFile:
    from server.code.domain.extract.lang_cmake import parse_cmake

    return parse_cmake(path, source)


def _parse_doc(path: str, source: bytes) -> ParsedFile:
    """A Markdown file by its headings; `.rst` / `.mdx` keep the symbol-less, line-window
    reading."""
    from server.code.domain.extract.lang_markdown import is_markdown_path, parse_markdown

    if is_markdown_path(path):
        return parse_markdown(path, source)
    return ParsedFile(path=path, lang="doc", source=source)


_PARSERS = {
    "cpp": parse_cpp,
    "python": _parse_python,
    "shell": _parse_bash,
    "typescript": _parse_typescript,
    "javascript": _parse_typescript,
    "rust": _parse_rust,
    "prisma": _parse_prisma,
    "go": _parse_go,
    "java": _parse_java,
    "vue": _parse_vue,
    "svelte": _parse_svelte,
    "astro": _parse_astro,
    "angular_template": _parse_angular_template,
    "proto": _parse_proto,
    "gotemplate": _parse_gotemplate,
    "hcl": _parse_hcl,
    "k8s": _parse_k8s,
    "jsonnet": _parse_jsonnet,
    "tla": _parse_tla,
    "tla_config": _parse_tlc,
    "flatbuffers": _parse_fbs,
    "cmake": _parse_cmake,
    "doc": _parse_doc,
}
_LANE_INCLUDE_LANGS: frozenset[str] = frozenset(
    {"tla", "tla_config", "flatbuffers", "cmake", "doc"}
)

# The lanes without a tree-sitter grammar — chunks from their symbol spans, edges from
# their reference sites (`lane_units`)
LANE_UNIT_LANGS: frozenset[str] = frozenset({"proto", "gotemplate", "hcl", "k8s", "jsonnet"})


def _lane_module(lang: str) -> Any:
    """The lane module of a tree-less lane: `parse_*`, `reference_sites`,
    `SITE_RULES`, `CHUNK_OPTIONS`, `wanted_paths` / `resolve_imports`."""
    from server.code.domain.extract import lang_gotemplate, lang_hcl, lang_k8s, lang_proto

    if lang == "hcl":
        return lang_hcl
    if lang == "k8s":
        return lang_k8s
    if lang == "jsonnet":
        from server.code.domain.extract import lang_jsonnet

        return lang_jsonnet
    return lang_proto if lang == "proto" else lang_gotemplate


def _shell_islands_on() -> bool:
    """Gate for a shell test's SQL islands as statement records and its leftover chunks
    coalesced. Off keeps the AST chunker's output."""
    from server.settings import settings

    return bool(settings.code_shell_islands)


_FILE_FACT_KINDS = frozenset(
    {
        "http_route",
        "env_var",
        "command",
        "decorator",
        "modifier",
        "entry_point",
        "notebook_cell",
    }
)


def _facets_ride_symbols(
    pf: ParsedFile,
    path: str,
    is_sql: bool,
    facets: list[Any] | None = None,
) -> bool:
    """Which files carry facet rows: SQL tests, shell files with islands on, host files
    with host islands on, and any TypeScript / JavaScript or Python file that serves a
    route or reads the environment; a Python file with a command, a decorator or an
    async def."""
    if is_sql or (pf.lang == "shell" and _shell_islands_on()):
        return True
    if pf.lang in TS_LANGS or pf.lang in ("python", "config"):
        # A file that serves a route, reads the environment, declares an entry point or
        # is a notebook carries those facts whatever its SQL islands hold
        if any(getattr(f, "kind", "") in _FILE_FACT_KINDS for f in facets or []):
            return True
    if pf.lang == "go":
        # A Go file with a directive, a tagged struct, a cobra command or a kubebuilder
        # marker carries facet rows (and, with the islands on, its SQL statements')
        from server.code.domain.extract.go_directives import has_go_facets

        if has_go_facets(pf):
            return True
    if (pf.lang == "python" and _py_islands_on()) or (pf.lang in CODE_ISLAND_LANGS):
        # Only a file that runs SQL carries facet rows — the live-facets read per file
        # is spent on the files that do, not on every Python / Go / Java file of the
        # tree
        return any(s.kind == "statement" for s in pf.symbols)
    if pf.lang == "prisma":
        return True
    if pf.lang in ("tla_config", "flatbuffers", "cmake", "k8s"):
        return True  # Checks, root_type, builds, manifest
    if pf.lang in TS_LANGS:
        # Only a file whose literals held SQL carries facet rows (the island symbols
        # joined the parse in `_chunks_for`) — the live-facets read is spent on the files
        # that do, not on every TypeScript file
        return any(s.kind == "statement" for s in pf.symbols)
    return pf.lang == "config" and _host_islands_on() and bool(host_kind(path))


def _host_islands_on() -> bool:
    """Gate for shell inside CI YAML, Dockerfiles, Makefiles and Jenkinsfiles read for
    facets, edges and symbols."""
    from server.settings import settings

    return bool(settings.code_host_islands)


def _statement_records_on() -> bool:
    """Gate for SQL tests as statement records (parser, packed chunks, facets). Off
    keeps the line-window chunker and no symbols."""
    from server.settings import settings

    return bool(settings.code_sql_statement_records)
