"""Which files are shell, for any repository.

`treesitter.lang_for_path` knows `.sh` and `.bash`; `ingest_parse.parse_for_ingest` adds a
shebang rule. Real repositories keep their shell elsewhere: bats-core has 228 `.bats`
files with 481 `@test` blocks, nvm's 326 tests are extensionless files without a shebang
(urchin runs them), bash-it ships `.plugin.bash` and rc files, and every project has
`bin/` scripts and `.bashrc`-style dotfiles, none of which an extension table catches.

The rules here are the ones the ecosystem already agreed on:

  * GitHub Linguist's `Shell` entry — extensions, filenames and
    interpreters (`languages.yml`);
  * ShellCheck's detection order — directive, then shebang, then
    extension — and its `# shellcheck shell=` directive;
  * shfmt's rule that an extensionless file with a shell shebang is shell;
  * and one heuristic of our own for the extensionless test trees runners
    like urchin execute: a file under a test-ish directory whose text
    parses as bash without an ERROR node and holds at least one command.

`.bats` files are shell to this module; `lang_bats` turns their `@test`
blocks into function symbols.

This module is outside the extractor stamp (see
`tests/unit/test_code_extractor_stamp.py`): it decides WHICH files index,
not the shape of a row — the same reason `denylist.py` is outside — and a
namespace gains the newly recognized files by a targeted backfill over
them, never by a heal (a heal walks live rows; these files have none).
"""

from __future__ import annotations

import posixpath
import re
from functools import lru_cache
from typing import Any

from server.code.domain.extract import bounded_parse

SHELL = "shell"

# GitHub Linguist, `Shell` (languages.yml), minus `.cgi`/`.fcgi` (Perl or
# Python as often as shell) and `.env` (dotenv files are not scripts).
SHELL_EXTENSIONS: frozenset[str] = frozenset(
    {
        ".sh",
        ".bash",
        ".bats",
        ".command",
        ".ksh",
        ".sh.in",
        ".tmux",
        ".tool",
        ".zsh",
        ".mksh",
        ".dash",
    }
)
SHELL_FILENAMES: frozenset[str] = frozenset(
    {
        ".bash_aliases",
        ".bash_functions",
        ".bash_history",
        ".bash_logout",
        ".bash_profile",
        ".bashrc",
        ".cshrc",
        ".env.example",
        ".envrc",
        ".flaskenv",
        ".kshrc",
        ".login",
        ".profile",
        ".zlogin",
        ".zlogout",
        ".zprofile",
        ".zshenv",
        ".zshrc",
        "9fs",
        "PKGBUILD",
        "APKBUILD",
        "bash_aliases",
        "bash_logout",
        "bash_profile",
        "bashrc",
        "cshrc",
        "gradlew",
        "kshrc",
        "login",
        "man",
        "profile",
        "zlogin",
        "zlogout",
        "zprofile",
        "zshenv",
        "zshrc",
    }
)
SHELL_INTERPRETERS: frozenset[str] = frozenset(
    {
        "ash",
        "bash",
        "dash",
        "ksh",
        "mksh",
        "pdksh",
        "sh",
        "zsh",
        "bats",
    }
)
# bats-core's own file conventions beside `.bats`
BATS_FILENAMES: frozenset[str] = frozenset({"setup_suite.bash", "test_helper.bash"})

_SHEBANG_RE = re.compile(rb"^#!\s*(\S+)(?:\s+(\S+))?")
_DIRECTIVE_RE = re.compile(rb"^[^\S\n]*#\s*shellcheck\s+shell=([a-z]+)", re.M)
_TEST_DIR_RE = re.compile(r"(^|/)(tests?|spec|specs|t|scripts?|bin|hooks|ci|\.github|tools)(/|$)")
_BINARY_HEAD = re.compile(rb"[\x00-\x08\x0e-\x1f]")


def shebang_interpreter(source: bytes) -> str:
    """The interpreter named by line one: `bash` from `#!/bin/bash`,
    `#!/usr/bin/env bash`, `#! /bin/sh -e`; '' when there is no shebang."""
    head = source[:200].lstrip()
    m = _SHEBANG_RE.match(head)
    if not m:
        return ""
    prog = posixpath.basename(m.group(1).decode("utf-8", "replace"))
    if prog == "env":
        # `#!/usr/bin/env bash`, `#!/usr/bin/env -S bash -e`: the interpreter
        # is the first word after env that is not an env option
        words = head.split(b"\n", 1)[0].decode("utf-8", "replace").split()[1:]
        prog = next((posixpath.basename(w) for w in words if not w.startswith("-")), "")
    return prog


def shellcheck_directive(source: bytes) -> str:
    """`# shellcheck shell=bash` in the first lines — ShellCheck's own
    override, ranked above the shebang by ShellCheck itself."""
    m = _DIRECTIVE_RE.search(source[:2000])
    return m.group(1).decode() if m else ""


def is_bats_path(path: str) -> bool:
    return path.lower().endswith(".bats") or posixpath.basename(path) in BATS_FILENAMES


def _ext(path: str) -> str:
    name = path.lower().rsplit("/", 1)[-1]
    if name.endswith(".sh.in"):
        return ".sh.in"
    dot = name.rfind(".")
    return name[dot:] if dot > 0 else ""


def shell_by_name(path: str) -> bool:
    """Linguist's extension and filename rules."""
    return _ext(path) in SHELL_EXTENSIONS or posixpath.basename(path) in SHELL_FILENAMES


def shell_by_head(source: bytes) -> bool:
    """ShellCheck's order: a `shell=` directive, then the shebang."""
    d = shellcheck_directive(source)
    if d:
        return d in SHELL_INTERPRETERS
    return shebang_interpreter(source) in SHELL_INTERPRETERS


@lru_cache(maxsize=1)
def _bash_parser() -> Any:
    import tree_sitter
    import tree_sitter_bash

    return tree_sitter.Parser(tree_sitter.Language(tree_sitter_bash.language()))


def _parses_as_shell(source: bytes) -> bool:
    """The extensionless-test-tree heuristic: bash parses it with no ERROR
    node and it holds a command. Cheap (one parse), applied only to
    extensionless files under test-ish directories."""
    if not source.strip() or _BINARY_HEAD.search(source[:4096]):
        return False
    try:
        tree = bounded_parse.parse(_bash_parser(), source)
    except Exception:  # noqa: BLE001 — grammar missing, a parser fault or a timeout: not shell
        return False
    root = tree.root_node
    if root.has_error:
        return False
    stack = [root]
    while stack:
        n = stack.pop()
        if n.type in (
            "command",
            "function_definition",
            "variable_assignment",
            "if_statement",
            "for_statement",
            "case_statement",
            "pipeline",
        ):
            return True
        stack.extend(n.children)
    return False


def shell_by_content(path: str, source: bytes) -> bool:
    """An extensionless file under a test / script directory that parses as
    shell (nvm's urchin tests, bats-core's fixture scripts)."""
    name = posixpath.basename(path)
    if "." in name or not _TEST_DIR_RE.search(path):
        return False
    return _parses_as_shell(source)


def detect_shell(path: str, source: bytes) -> str:
    """`shell` when any rule says so, else ''. Order: name, then the head
    (directive, shebang), then the content heuristic for extensionless
    files in test-ish trees."""
    if shell_by_name(path) or shell_by_head(source) or shell_by_content(path, source):
        return SHELL
    return ""


__all__ = [
    "BATS_FILENAMES",
    "SHELL",
    "SHELL_EXTENSIONS",
    "SHELL_FILENAMES",
    "SHELL_INTERPRETERS",
    "detect_shell",
    "is_bats_path",
    "shebang_interpreter",
    "shell_by_content",
    "shell_by_head",
    "shell_by_name",
    "shellcheck_directive",
]


# --------------------------------------------------------------------------
# The languages `treesitter.lang_for_path` does not name, detected HERE so that adding
# one moves no extractor stamp (treesitter.py is in every stage's stamp)
# --------------------------------------------------------------------------

LANG_BY_EXTENSION: dict[str, str] = {
    ".ts": "typescript",
    ".tsx": "typescript",
    ".mts": "typescript",
    ".cts": "typescript",
    ".js": "javascript",
    ".jsx": "javascript",
    ".mjs": "javascript",
    ".cjs": "javascript",
    ".rs": "rust",
    ".go": "go",
    ".java": "java",
    ".prisma": "prisma",
    ".vue": "vue",  # Parsed as its script's typescript / javascript
    ".svelte": "svelte",
    ".astro": "astro",  # The Vue rule
    ".css": "css",
    ".scss": "css",
    ".less": "css",
    ".mdx": "doc",
    # common in platform monorepos: infrastructure code that chunks line-wise like YAML
    # (no symbol lane yet), and editor rule files that are documentation for agents
    ".tf": "hcl",
    ".tfvars": "hcl",
    ".hcl": "hcl",
    ".jsonnet": "jsonnet",
    ".libsonnet": "jsonnet",
    ".rego": "config",
    ".mdc": "doc",
    ".proto": "proto",
    ".tpl": "gotemplate",
    ".tmpl": "gotemplate",
    ".gotmpl": "gotemplate",
    # plain HTML (dashboards, skill pages) reads as a document; an HTML file under
    # `templates/` holding a template action is a Go template first (ingest's
    # Helm-template rung runs before this table)
    ".html": "doc",
    ".htm": "doc",  # a `.component.html` is Angular (`lang_for_path` below)
    ".tla": "tla",
    ".fbs": "flatbuffers",
    ".c": "cpp",  # C parses with the C++ grammar
    # A stub, a Windows script and a notebook parse with the Python grammar
    # (`lang_python.module_scope` gives a stub its own scope; `py_notebooks` lifts a
    # notebook's code cells)
    ".pyi": "python",
    ".pyw": "python",
    ".ipynb": "python",
}


_CONFIG_BASENAMES: frozenset[str] = frozenset({"go.mod", "go.work"})


def detect_by_extension(path: str) -> str:
    """The language of a path by its extension for the languages the
    lane parsers read (TypeScript / JavaScript, Rust, Prisma, Go, Java,
    protobuf, Go templates) and the two chunk-only ones (CSS, MDX as a
    doc); '' when none applies."""
    lower = path.lower()
    if lower.rsplit("/", 1)[-1] in _CONFIG_BASENAMES:
        return "config"  # Go.mod is read for the module path
    if lower.endswith(".component.html"):
        return "angular_template"  # A component's external template
    dot = lower.rfind(".")
    if dot < 0 or "/" in lower[dot:]:
        return ""
    return LANG_BY_EXTENSION.get(lower[dot:], "")
