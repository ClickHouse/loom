"""bats-core test files as shell with named test symbols.

bats preprocesses `@test "description" { … }` into a shell function before
running it (`bats-preprocess`); `setup`, `teardown`, `setup_file`,
`teardown_file` are hooks, `load x` sources `x.bash` (then `x`) relative to
the test file, `bats_load_library x` sources `$BATS_LIB_PATH/x/load.bash`,
and `# bats test_tags=a,b` / `# bats file_tags=a,b` tag the next test or
the file. tree-sitter-bash reads `@test "d" {` as a command named `@test`
followed by a compound body — no function and no symbol.

The rewrite here is bats' own, made LENGTH-PRESERVING so every byte offset
of the parse maps onto the original text: `@test "description" {` becomes
`<name>() {` where `<name>` is the description slugged and padded with
`_` (or cut) to exactly the header's width. `lang_bash.parse_bash` then
yields a `function_definition` per test with the correct line span and
byte range; the symbol is renamed to `test_<slug>` with the full
description as its signature, and the ParsedFile's `source` is restored
to the original bytes — chunk text, lexemes and the edge extractor's
`_text()` all read the file as written.

Outside the extractor stamp (see `tests/unit/test_code_extractor_stamp.py`):
dispatched from `ingest.py` for `.bats` paths; a namespace gains their
rows by a targeted backfill over its `.bats` paths.
"""

from __future__ import annotations

import posixpath
import re

from server.code.domain.extract.lang_bash import parse_bash
from server.code.domain.extract.treesitter import ParsedFile, Symbol

_TEST_HEADER_RE = re.compile(
    rb'^(?P<indent>[ \t]*)@test[ \t]+(?P<q>["\'])(?P<desc>.*?)(?P=q)[ \t]*\{', re.M
)
_LOAD_RE = re.compile(r"^[^\S\n]*load\s+(['\"]?)([^'\"\s]+)\1", re.M)
_LOAD_LIB_RE = re.compile(r"^[^\S\n]*bats_load_library\s+(['\"]?)([^'\"\s]+)\1", re.M)
_TAGS_RE = re.compile(r"^[^\S\n]*#\s*bats\s+(test_tags|file_tags)\s*=\s*+(.*\S)\s*$", re.M)
_SLUG_RE = re.compile(r"[^A-Za-z0-9]+")
HOOKS: frozenset[str] = frozenset(
    {
        "setup",
        "teardown",
        "setup_file",
        "teardown_file",
        "setup_suite",
        "teardown_suite",
    }
)
TEST_PREFIX = "test_"
BATS_LIB_PATH = "$BATS_LIB_PATH"
_PLACEHOLDER = "t"


def slug(description: str) -> str:
    s = _SLUG_RE.sub("_", description).strip("_")
    return s or _PLACEHOLDER


def rewrite(source: bytes) -> tuple[bytes, dict[int, str]]:
    """The length-preserving rewrite: `@test "d" {` → `<name>() {` of the
    same width; returns (rewritten bytes, {header line (1-based): description})."""
    out = bytearray()
    pos = 0
    descriptions: dict[int, str] = {}
    for m in _TEST_HEADER_RE.finditer(source):
        out += source[pos : m.start()]
        indent = m.group("indent")
        header_len = m.end() - m.start() - len(indent)  # `@test "d" {`
        desc = m.group("desc").decode("utf-8", "replace")
        name_room = header_len - len("() {")
        name = slug(desc)
        if len(name) > name_room:
            name = name[:name_room]
        name = name.ljust(name_room, "_")
        if not name or name[0].isdigit():
            name = ("t" + name)[:name_room].ljust(name_room, "_") if name_room > 0 else name
        out += indent + name.encode() + b"() {"
        descriptions[source.count(b"\n", 0, m.start()) + 1] = desc
        pos = m.end()
    out += source[pos:]
    assert len(out) == len(source), "bats rewrite must preserve length"
    return bytes(out), descriptions


def load_targets(path: str, text: str) -> list[str]:
    """Include targets for `load x` (bats tries `x.bash`, then `x`, relative to the test
    file's directory — kept SCRIPT-RELATIVE here, like a `source` target, so
    `lang_shell_islands.include_targets` resolves every include of a shell file in one
    place) and `bats_load_library x` (recorded as bats sources it,
    `$BATS_LIB_PATH/<lib>/load.bash`; `include_targets` maps the runtime root onto the
    repository's own library directory)."""
    out: list[str] = []
    for _q, target in _LOAD_RE.findall(text):
        if "$" in target:
            continue
        out.append(target if target.endswith((".bash", ".sh")) else target + ".bash")
    for _q, lib in _LOAD_LIB_RE.findall(text):
        if "$" not in lib:
            out.append(posixpath.join(BATS_LIB_PATH, lib, "load.bash"))
    return list(dict.fromkeys(out))


def tags(text: str) -> tuple[tuple[str, ...], dict[int, tuple[str, ...]]]:
    """(file tags, {line: test tags declared on that line}) from
    `# bats file_tags=…` / `# bats test_tags=…`."""
    file_tags: list[str] = []
    per_line: dict[int, tuple[str, ...]] = {}
    for m in _TAGS_RE.finditer(text):
        values = tuple(t.strip() for t in m.group(2).split(",") if t.strip())
        if m.group(1) == "file_tags":
            file_tags.extend(values)
        else:
            per_line[text.count("\n", 0, m.start()) + 1] = values
    return tuple(dict.fromkeys(file_tags)), per_line


def parse_bats(path: str, source: bytes) -> ParsedFile:
    """A `.bats` file as shell: one `function` symbol per `@test` (named
    `test_<slug>`, the description as signature), the hooks as functions,
    `load` targets as includes, the original bytes as `source`."""
    rewritten, descriptions = rewrite(source)
    pf = parse_bash(path, rewritten)
    pf.source = source
    text = source.decode("utf-8", "replace")
    renamed: list[Symbol] = []
    for s in pf.symbols:
        desc = descriptions.get(s.start_line)
        if desc is None:
            renamed.append(s)
            continue
        name = TEST_PREFIX + slug(desc)
        mod = s.qualified_name.rsplit("::", 1)[0]
        renamed.append(
            Symbol(
                qualified_name=f"{mod}::{name}",
                name=name,
                kind="function",
                signature=f'@test "{desc}"',
                path=path,
                start_line=s.start_line,
                end_line=s.end_line,
                doc_comment=s.doc_comment,
                has_body=True,
            )
        )
    pf.symbols = renamed
    pf.includes = list(dict.fromkeys([*pf.includes, *load_targets(path, text)]))
    return pf


__all__ = [
    "BATS_LIB_PATH",
    "HOOKS",
    "TEST_PREFIX",
    "load_targets",
    "parse_bats",
    "rewrite",
    "slug",
    "tags",
]
