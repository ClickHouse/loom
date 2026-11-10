"""Extractor version stamp: what the parser tier's rows were produced BY.

Chunks are content-addressed and symbol ids are hashes of (qualified name,
signature), so an unchanged file re-ingests to the same rows and is kept.
That memo has a hole: it keys on the INPUT (file bytes) and never on the
EXTRACTOR. A fix to the chunker, a symbol-id normalization change, a new
tree-sitter grammar release — none of these touch a file's content hash, so
none of them invalidates a single existing row.

The stamp is a sha256 over the CODE of every extractor module (its tokens,
without comments, blank lines or docstrings, an import counted as the names
it binds; see `logic_tokens`) plus the installed tree-sitter distribution
versions, truncated to 16 hex chars. A timestamp on one entry file is both
too loose (a `lang_python.py` fix changes resolved edges without touching
`treesitter.py`) and too tight (an identical rewrite moves mtime); a
hand-kept version constant fails silently when someone forgets to bump it. Hashing
the code is the one identity that cannot drift from it, and leaving the prose
out lets a comment be corrected without re-deriving every indexed repository.

Consumed by the ingest worker: a stamp that differs from the one recorded
in code_index_state re-chunks the namespace in paced batches (see
`code_ingest._maybe_rechunk`). An empty recorded stamp means "first
observation" and is recorded, never acted on — we cannot know what the
existing rows were produced by.

Per-stage stamps. One hash over every module is one bit: a chunker fix would re-derive
symbols, edges and call sites for every file of every namespace, most of them rows the
key-diff then keeps. The modules are therefore partitioned into the three stages of
`ingest_file` — `chunk`, `symbol`, `edge` — and a stage's stamp covers its own modules
PLUS the modules of the stages it reads from: every stage consumes the parse (chunk
spans and symbol ids come out of `treesitter.py` and the `lang_*` parsers), and the
chunk stage reads call sites for the callee outline. So a `chunker.py` change moves only
`chunk`; an `edges_treesitter.py` change moves `edge` and `chunk`; a parser change moves
all three. The worker heals a namespace with exactly the moved stages and `ingest_file`
skips the blocks whose stamp still matches when the file's content is unchanged. The
combined stamp is unchanged by this (same module set, order-free hash), so the
deployment that introduces the stage stamps does not itself start a heal: a namespace at
the combined stamp with no stage rows is seeded, not healed.

A fourth stage, `guard`, covers the caller-guard records (`call_guards.py`,
`loom.code_caller_guards`). It reads the parse and the edge stage's resolved call edges,
so its stamp moves with theirs; nothing reads it back, so a `call_guards.py` change
moves `guard` alone, and a heal of it re-derives the guard rows from the path's LIVE
edges without re-resolving them. A namespace whose stage keys predate the stage has no
`guard` key: `moved_stages` counts the missing key as moved and the worker heals exactly
that stage, not the three that are recorded. The combined stamp moves once, when the
module joins.
"""

from __future__ import annotations

import ast
import hashlib
import io
import pathlib
import tokenize
from collections.abc import Mapping
from functools import lru_cache

# The stages of `ingest_file`, in the order the blocks run.
STAGES: tuple[str, ...] = ("chunk", "symbol", "edge", "guard")

# Every module whose logic decides what a row looks like, by the stage whose
# rows it produces. Adding a language module here is part of adding the
# language; a module under code_index/ that belongs to no stage must be
# named in `tests/unit/test_code_extractor_stamp.py`'s non-extractor list
# with the reason, or that test fails.
STAGE_MODULES: dict[str, tuple[str, ...]] = {
    "chunk": ("chunker.py", "context_text.py", "tokenize.py", "callee_outline.py"),
    "symbol": (
        "treesitter.py",
        "lang_python.py",
        "py_members.py",
        "lang_bash.py",
        "text_lines.py",
    ),
    "edge": (
        "edges_treesitter.py",
        "crossref.py",
        "registration.py",
        "ts_receivers.py",
        "py_receivers.py",
        "py_library_methods.py",
        "ts_config.py",
        "ts_reexports.py",
        "cpp_receivers.py",
        "ts_workspace.py",
        "py_layouts.py",
    ),
    "guard": ("call_guards.py",),
}

# What a stage's rows are derived from besides its own modules. Every stage consumes the
# parse (`symbol`'s modules); the chunk stage also reads the edge stage's call sites
# (`edges_treesitter.call_sites`) for the callee outline, so an edge-module change
# re-derives chunks too — cheaply, since unchanged content re-chunks to the same ids.
# The guard stage derives its records from the edge stage's call edges and nothing
# derives from it.
STAGE_INPUTS: dict[str, tuple[str, ...]] = {
    "chunk": ("symbol", "edge"),
    "symbol": (),
    "edge": ("symbol",),
    "guard": ("edge",),
}

# The union — the combined stamp over every stage's modules. Order-free, so the
# same set hashes to the same value.
EXTRACTOR_MODULES: tuple[str, ...] = tuple(
    sorted({m for mods in STAGE_MODULES.values() for m in mods})
)

_HERE = pathlib.Path(__file__).resolve().parent


def stage_modules(stage: str) -> tuple[str, ...]:
    """The modules a stage's stamp hashes: its own plus its inputs' (transitively)."""
    seen: set[str] = set()
    out: set[str] = set()
    todo = [stage]
    while todo:
        s = todo.pop()
        if s in seen:
            continue
        seen.add(s)
        out.update(STAGE_MODULES[s])
        todo.extend(STAGE_INPUTS.get(s, ()))
    return tuple(sorted(out))


# The grammars the STAGE modules import: only these move the stamp. A lane grammar
# (typescript, javascript, rust, go, java, css) lives outside the stamp like the lane
# module that reads it — a host venv carrying one the image lacks, or an image gaining
# one at a deploy, must not re-chunk every namespace's C++ / Python / shell rows.
STAMPED_GRAMMARS: frozenset[str] = frozenset(
    {
        "tree-sitter",
        "tree-sitter-cpp",
        "tree-sitter-python",
        "tree-sitter-bash",
    }
)


def grammar_versions() -> dict[str, str]:
    """Installed tree-sitter distributions the stage parsers use -> version.
    A grammar upgrade changes node types and spans, so it is part of the
    extractor identity; a grammar no stage module imports is not."""
    try:
        from importlib.metadata import distributions
    except Exception:  # noqa: BLE001 — metadata unavailable: stamp on source only
        return {}
    out: dict[str, str] = {}
    for d in distributions():
        name = (d.metadata["Name"] or "").lower() if d.metadata else ""
        if name.replace("_", "-") in STAMPED_GRAMMARS:
            out[name.replace("_", "-")] = str(d.version or "")
    return dict(sorted(out.items()))


# Bumped when `logic_tokens` changes what it keeps, so the move is deliberate.
# tokens-v2 names an import by what it binds, not the path it binds from, so
# moving a module between packages leaves the stamps where they are.
STAMP_SCHEME = "tokens-v2"


def _docstring_spans(tree: ast.AST) -> list[tuple[tuple[int, int], tuple[int, int]]]:
    spans = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            body = node.body
            if (
                body
                and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
                and isinstance(body[0].value.value, str)
            ):
                d = body[0]
                spans.append(((d.lineno, d.col_offset), (d.end_lineno or 0, d.end_col_offset or 0)))
    return spans


def _import_forms(tree: ast.AST) -> dict[tuple[int, int], tuple[tuple[int, int], list[str]]]:
    """start -> (end, tokens) for every import statement: the names it binds
    (`import x as y` binds y from x's last component; `import a.b` binds the
    dotted name the code then spells out), never the package they come from."""
    forms: dict[tuple[int, int], tuple[tuple[int, int], list[str]]] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            toks = ["import"]
            for a in node.names:
                toks += [a.name, "as", a.asname] if a.asname else [a.name]
        elif isinstance(node, ast.Import):
            toks = ["import"]
            for a in node.names:
                toks += [a.name.rpartition(".")[2], "as", a.asname] if a.asname else [a.name]
        else:
            continue
        end = (node.end_lineno or 0, node.end_col_offset or 0)
        forms[(node.lineno, node.col_offset)] = (end, toks)
    return forms


def logic_tokens(source: str) -> str:
    """The module's tokens minus comments, blank lines and docstrings: what the
    code does, not how it is explained. Tokens rather than an AST dump, because
    the tokenizer's output does not change with the Python version. An import
    contributes the names it binds."""
    tree = ast.parse(source)
    spans = _docstring_spans(tree)
    imports = _import_forms(tree)
    out: list[str] = []
    after_docstring = False
    skip_until: tuple[int, int] | None = None
    for tok in tokenize.generate_tokens(io.StringIO(source).readline):
        if skip_until is not None:
            if tok.end <= skip_until:
                continue
            skip_until = None
        if tok.type in (tokenize.COMMENT, tokenize.NL):
            continue
        if tok.type == tokenize.STRING and any(a <= tok.start and tok.end <= b for a, b in spans):
            after_docstring = True
            continue
        if after_docstring and tok.type == tokenize.NEWLINE:
            after_docstring = False
            continue
        after_docstring = False
        form = imports.get(tok.start)
        if form is not None:
            skip_until, names = form
            out.extend(names)
            continue
        out.append(tokenize.tok_name[tok.type] if tok.type in _STRUCTURAL else tok.string)
    return " ".join(out)


_STRUCTURAL = frozenset({tokenize.NEWLINE, tokenize.INDENT, tokenize.DEDENT, tokenize.ENDMARKER})


def compute_stamp(
    module_dir: pathlib.Path,
    modules: tuple[str, ...],
    grammars: dict[str, str],
) -> str:
    """Pure: sha256 over the scheme, (module name, `logic_tokens`)* and
    (grammar, version)*. Rewording a comment or docstring does not move it."""
    h = hashlib.sha256(f"{STAMP_SCHEME}\0".encode())
    for name in sorted(modules):
        p = module_dir / name
        h.update(name.encode())
        h.update(b"\0")
        try:
            source = p.read_text()
            h.update(logic_tokens(source).encode())
        except OSError:
            h.update(b"<missing>")
        h.update(b"\0")
    for k, v in sorted(grammars.items()):
        h.update(f"{k}={v}\0".encode())
    return h.hexdigest()[:16]


def compute_stage_stamps(module_dir: pathlib.Path, grammars: dict[str, str]) -> dict[str, str]:
    """Pure: one `compute_stamp` per stage over `stage_modules(stage)`. The
    grammars go into every stage — a grammar upgrade moves the parse, and
    every stage reads the parse."""
    return {s: compute_stamp(module_dir, stage_modules(s), grammars) for s in STAGES}


def moved_stages(recorded: Mapping[str, str], current: Mapping[str, str]) -> frozenset[str]:
    """The stages whose recorded stamp differs from the running one. A stage
    with no recorded stamp counts as moved, so an empty record heals every
    stage."""
    return frozenset(s for s in STAGES if str(recorded.get(s) or "") != str(current.get(s) or ""))


# ---------------------------------------------------------------------------
# The heal is scoped by language. Every stage module shapes either every file's rows or
# one language family's; a stamp per family says whether that family's files must
# re-derive, so a Python-only change (`lang_python.py`) heals the `.py` files and leaves
# the C++ tree alone.
# ---------------------------------------------------------------------------
EVERY_LANGUAGE = "*"
MODULE_LANGUAGES: dict[str, str] = {
    "lang_python.py": "python",
    "py_receivers.py": "python",
    "cpp_receivers.py": "cpp",
    "py_library_methods.py": "python",
    "py_members.py": "python",
    "py_layouts.py": "python",
    "lang_bash.py": "shell",
    "ts_receivers.py": "typescript",
    "ts_config.py": "typescript",
    "ts_reexports.py": "typescript",
    "ts_workspace.py": "typescript",
}
GRAMMAR_LANGUAGES: dict[str, str] = {
    "tree-sitter": EVERY_LANGUAGE,
    "tree-sitter-python": "python",
    "tree-sitter-bash": "shell",
    "tree-sitter-cpp": "cpp",
}
LANGUAGE_FAMILIES: tuple[str, ...] = ("python", "shell", "typescript", "cpp", "other")
_FAMILY_BY_EXT: dict[str, str] = {
    ".py": "python",
    ".pyi": "python",
    ".pyw": "python",
    ".ipynb": "python",
    ".sh": "shell",
    ".bash": "shell",
    ".bats": "shell",
    ".ts": "typescript",
    ".tsx": "typescript",
    ".mts": "typescript",
    ".cts": "typescript",
    ".js": "typescript",
    ".jsx": "typescript",
    ".mjs": "typescript",
    ".cjs": "typescript",
    ".vue": "typescript",
    ".svelte": "typescript",
    ".astro": "typescript",
    ".cpp": "cpp",
    ".cc": "cpp",
    ".cxx": "cpp",
    ".h": "cpp",
    ".hpp": "cpp",
    ".hxx": "cpp",
    ".ipp": "cpp",
    ".inl": "cpp",
    ".c": "cpp",
}


def language_of_module(module: str) -> str:
    """The family a stage module shapes, `*` when it shapes every file."""
    return MODULE_LANGUAGES.get(module, EVERY_LANGUAGE)


def language_modules(lang: str) -> tuple[str, ...]:
    """The stamped modules whose change moves `lang`'s rows: its own and
    the ones shaping every language."""
    return tuple(
        sorted(m for m in EXTRACTOR_MODULES if language_of_module(m) in (EVERY_LANGUAGE, lang))
    )


def language_family_of_path(path: str) -> str:
    """Which family's stamp a file's rows follow, by extension; `other` for
    the line-window languages that read the shared modules only."""
    lower = path.lower()
    dot = lower.rfind(".")
    ext = lower[dot:] if dot >= 0 and "/" not in lower[dot:] else ""
    return _FAMILY_BY_EXT.get(ext, "other")


def compute_language_stamps(module_dir: pathlib.Path, grammars: dict[str, str]) -> dict[str, str]:
    """Pure: one `compute_stamp` per family over `language_modules(lang)` and
    the grammars that family's parse reads (the base library and its own)."""
    out: dict[str, str] = {}
    for lang in LANGUAGE_FAMILIES:
        gr = {
            k: v
            for k, v in grammars.items()
            if GRAMMAR_LANGUAGES.get(k, EVERY_LANGUAGE) in (EVERY_LANGUAGE, lang)
        }
        out[lang] = compute_stamp(module_dir, language_modules(lang), gr)
    return out


def moved_languages(recorded: Mapping[str, str], current: Mapping[str, str]) -> frozenset[str]:
    """The families whose recorded stamp differs from the running one; a
    family with no record counts as moved, so an empty record heals every
    family."""
    return frozenset(
        lang
        for lang in LANGUAGE_FAMILIES
        if str(recorded.get(lang) or "") != str(current.get(lang) or "")
    )


@lru_cache(maxsize=1)
def language_stamps() -> dict[str, str]:
    """The running process's per-family identities, memoized like `extractor_stamp`."""
    return dict(compute_language_stamps(_HERE, grammar_versions()))


@lru_cache(maxsize=1)
def extractor_stamp() -> str:
    """The running process's extractor identity (memoized; source does not
    change under a running server, and a deploy is a new process)."""
    return compute_stamp(_HERE, EXTRACTOR_MODULES, grammar_versions())


@lru_cache(maxsize=1)
def stage_stamps() -> dict[str, str]:
    """The running process's per-stage identities, memoized like `extractor_stamp`."""
    return dict(compute_stage_stamps(_HERE, grammar_versions()))
