"""Expected-output fixtures as index rows.

`tests/queries/**/*.reference` (and the templated `.reference.j2`, and the
`.expect` client scripts beside them) hold what a stateless test PRINTS.
An agent writing a test needs the convention of the closest existing one,
and a coverage check that looks for a test by number wants to know the
expectation file is there. Without a language here `parse_for_ingest` would
skip every one, `code.files` / `code.test_home` could not name them and
`code.outline` / `code.grep` could not read them.

They are not code. The rule here is deliberately the cheapest shape the
tables allow:

  * language `reference`, ONE chunk per file, kind `file_header` (the
    `kind` column is an Enum8; a new value would be a migration for no
    reader that needs one), scope '';
  * content = the first `REFERENCE_HEAD_CHARS` characters, an elision
    marker when cut, and `context_description` saying so — a 700 KB TPC-H
    answer set is not something to store per row;
  * lexemes from that head, so `code.grep` finds an expected value;
  * NO embedding (`embed_last_error = UNEMBEDDED_BY_DESIGN`, which the
    embed-recovery read excludes), no context text, no symbols, no edges;
    the vector and lexical legs of code.search filter `embedding_dim > 0`,
    so these rows never enter a ranked answer;
  * files over `REFERENCE_MAX_BYTES` are not indexed at all.

This module is outside the extractor stamp on purpose (see
`tests/unit/test_code_extractor_stamp.py`): a stamp heal walks LIVE rows and cannot add
files, and a rule change here is applied by a targeted `loom code backfill --paths-file`
over the reference tree (15k files, minutes) rather than a namespace-wide re-chunk
(hours).
"""

from __future__ import annotations

from server.code.domain.extract.chunker import Chunk

REFERENCE_LANG = "reference"
REFERENCE_PREFIX = "tests/queries/"
# What a test prints (`.reference`, the templated `.reference.j2`) and the
# `.expect` client scripts beside them — the latter are runnable tests with
# an expectation of their own, indexed under the same plain-text rule
# because no parser here reads Tcl.
EXPECTATION_SUFFIXES: tuple[str, ...] = (".reference.j2", ".reference")
REFERENCE_SUFFIXES: tuple[str, ...] = (*EXPECTATION_SUFFIXES, ".expect")
REFERENCE_HEAD_CHARS = 4000
REFERENCE_MAX_BYTES = 1 << 20
# Matched as a prefix of `embed_last_error`, so rows stored under a longer marker
# still read as unembedded by design.
UNEMBEDDED_BY_DESIGN = "unembedded by design: reference"

# Runnable stateless test files whose expectation is `<stem>.reference`.
_RUNNABLE_EXTS: tuple[str, ...] = (".sql", ".sh", ".py", ".expect", ".sql.j2", ".sh.j2")


def is_reference_path(path: str) -> bool:
    """A reference / expect fixture under the queries tree."""
    return path.startswith(REFERENCE_PREFIX) and path.lower().endswith(REFERENCE_SUFFIXES)


def reference_lang_for_path(path: str) -> str:
    return REFERENCE_LANG if is_reference_path(path) else ""


def reference_head(text: str, head_chars: int = REFERENCE_HEAD_CHARS) -> tuple[str, str]:
    """(content, context_description) for one fixture: the head, an elision
    marker when it was cut, and a description that says so ('' when whole)."""
    if len(text) <= head_chars:
        return text, ""
    kept = text[:head_chars]
    marker = f"\n… {len(text) - head_chars} more chars elided (reference head only)\n"
    return kept + marker, f"truncated: head {head_chars} of {len(text)} chars"


def reference_chunk(path: str, source: bytes, head_chars: int = REFERENCE_HEAD_CHARS) -> Chunk:
    """The one chunk of a reference file."""
    text = source.decode("utf-8", "replace")
    content, _desc = reference_head(text, head_chars)
    lines = text.count("\n") + (0 if text.endswith("\n") or not text else 1)
    return Chunk(
        path=path,
        scope="",
        kind="file_header",
        start_line=1,
        end_line=max(1, lines),
        content=content,
    )


def test_stem_path(path: str) -> str:
    """`tests/…/05138_x.sql` → `tests/…/05138_x`; '' when `path` is not a
    runnable stateless test (a `.reference` itself, a data file, source)."""
    if not path.startswith(REFERENCE_PREFIX) or path.lower().endswith(EXPECTATION_SUFFIXES):
        return ""
    lower = path.lower()
    for ext in sorted(_RUNNABLE_EXTS, key=len, reverse=True):
        if lower.endswith(ext):
            return path[: -len(ext)]
    return ""


def reference_candidates(path: str) -> tuple[str, ...]:
    """The paths a test's expectation file may have, in preference order."""
    stem = test_stem_path(path)
    if not stem:
        return ()
    return (stem + ".reference", stem + ".reference.j2")


def reference_sibling(path: str, present: set[str]) -> str | None:
    """The indexed expectation file next to `path`, None when the index holds
    none (or `path` is not a test)."""
    for cand in reference_candidates(path):
        if cand in present:
            return cand
    return None
