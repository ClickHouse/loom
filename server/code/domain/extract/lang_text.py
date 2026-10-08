"""A text file with no parser is still indexed.

A file whose language the ingest does not recognise — Kotlin, Ruby, PHP,
Swift, Scala, Lua, Zig, C#, Dart, Elixir sources; `.txt`, `.cfg`, `.conf`,
`.properties`, `.gradle`, GraphQL schemas — would otherwise be invisible to
`code.grep` and `code.search` although the tree holds it. Such a file is
chunked line-wise without symbols, the way config and doc files
are: searchable, greppable, citable; no symbols, no edges, nothing the
extractor stamp sees (this module sits outside `treesitter.py`).

`.c` is C++'s grammar for the index's purposes (a `.c` file parses to the
same functions and calls); it is mapped here, not in `treesitter.py`, so
the mapping moves no extractor stamp.
"""

from __future__ import annotations

TEXT_LANG = "text"

# Bounded: a lock file or a generated data dump is not what an agent greps
# for, and a multi-megabyte text file would be thousands of chunks.
TEXT_MAX_BYTES = 512 * 1024

# Source languages without a symbol parser: read as text so their functions
# are at least findable by name and by sentence.
_CODE_TEXT_EXTENSIONS: frozenset[str] = frozenset(
    {
        ".kt",
        ".kts",
        ".rb",
        ".php",
        ".swift",
        ".scala",
        ".sc",
        ".lua",
        ".zig",
        ".cs",
        ".dart",
        ".ex",
        ".exs",
        ".erl",
        ".hrl",
        ".hs",
        ".ml",
        ".mli",
        ".r",
        ".jl",
        ".pl",
        ".pm",
        ".m",
        ".mm",
        ".groovy",
        ".gradle",
        ".ps1",
        ".psm1",
        ".bat",
        ".cmd",
        ".nim",
        ".v",
        ".sv",
        ".vhd",
        ".sol",
        ".clj",
        ".cljs",
        ".fs",
        ".fsx",
        ".vb",
        ".pas",
        ".d",
        ".cr",
        ".elm",
        ".purs",
        ".re",
        ".hx",
        ".graphql",
        ".gql",
        ".thrift",
        ".avsc",
        ".capnp",
        ".asm",
        ".s",
        ".S",
        ".cu",
        ".cuh",
        ".cl",
        ".glsl",
        ".hlsl",
        ".wgsl",
        ".metal",
        ".tex",
        ".bib",
        ".adoc",
        ".org",
        ".txt",
        ".text",
        ".cfg",
        ".conf",
        ".properties",
        ".env",
        ".editorconfig",
        ".gitattributes",
        ".gitignore",
        ".dockerignore",
        ".npmrc",
        ".yarnrc",
        ".flake8",
        ".pylintrc",
        ".csv",
        ".tsv",
        ".pyx",
        ".pxd",
        ".pxi",  # Cython reads as text (no grammar)
    }
)
_TEXT_BASENAMES: frozenset[str] = frozenset(
    {
        "dockerfile",
        "makefile",
        "gnumakefile",
        "jenkinsfile",
        "vagrantfile",
        "procfile",
        "gemfile",
        "rakefile",
        "brewfile",
        "justfile",
        "license",
        "licence",
        "copying",
        "notice",
        "authors",
        "contributors",
        "changelog",
        "changes",
        "todo",
        "readme",
        "codeowners",
        ".mailmap",
        ".clang-format",
        ".clang-tidy",
        ".clangd",
        ".gitmodules",
    }
)
# Not text an agent reads: images, archives, fonts, compiled artefacts.
_NEVER_TEXT: frozenset[str] = frozenset(
    {
        ".png",
        ".jpg",
        ".jpeg",
        ".gif",
        ".webp",
        ".ico",
        ".bmp",
        ".tiff",
        ".svg",
        ".pdf",
        ".zip",
        ".gz",
        ".tgz",
        ".bz2",
        ".xz",
        ".zst",
        ".7z",
        ".rar",
        ".jar",
        ".war",
        ".class",
        ".o",
        ".a",
        ".so",
        ".dylib",
        ".dll",
        ".exe",
        ".bin",
        ".dat",
        ".db",
        ".sqlite",
        ".parquet",
        ".orc",
        ".avro",
        ".woff",
        ".woff2",
        ".ttf",
        ".otf",
        ".eot",
        ".mp3",
        ".mp4",
        ".mov",
        ".wav",
        ".lock",
        ".min.js",
        ".map",
        ".pyc",
        ".pyo",
        ".wasm",
    }
)


def _ext(lower: str) -> str:
    base = lower.rsplit("/", 1)[-1]
    dot = base.rfind(".")
    return base[dot:] if dot >= 0 else ""


def text_lang_for_path(path: str, source: bytes) -> str:
    """`text` for a file the index would otherwise drop and an agent may
    still grep for; '' for binaries, oversized files and everything the
    other detectors already claim (they run first)."""
    lower = path.lower()
    base = lower.rsplit("/", 1)[-1]
    ext = _ext(lower)
    if ext in _NEVER_TEXT or lower.endswith(".min.js"):
        return ""
    if len(source) > TEXT_MAX_BYTES or not source.strip():
        return ""
    if b"\x00" in source[:8192]:
        return ""
    if ext in _CODE_TEXT_EXTENSIONS or base in _TEXT_BASENAMES:
        return TEXT_LANG
    return ""
